"""
ReplSession: A persistent Python REPL backed by a subprocess worker.

Maintains execution state across multiple execute() calls, allowing agents
to build up context and explore it without re-running the entire history.
"""

import contextlib
import json
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional


@dataclass
class ExecResult:
    """Result of executing code in the REPL."""

    stdout: str
    stderr: str
    value: Optional[str]  # repr of last expression, if any
    exception: Optional[str]  # error message if execution failed
    traceback: str  # full traceback if exception occurred
    duration_ms: float  # wall-clock time for execution
    truncated: bool  # output was truncated due to size limit
    truncated_bytes: int  # how many bytes were dropped


class ReplSession:
    """
    A persistent Python REPL session backed by a subprocess worker.

    The session maintains a namespace across multiple execute() calls,
    allowing an agent to build up state and explore it programmatically.
    """

    def __init__(
        self,
        session_id: str,
        timeout_ms: int = 30000,
        max_output_bytes: int = 65536,
    ) -> None:
        """
        Initialize a new REPL session.

        Args:
            session_id: Unique identifier for this session (for logging/pooling)
            timeout_ms: Default timeout per execute call (ms)
            max_output_bytes: Maximum output size before truncation (bytes)
        """
        self.session_id = session_id
        self.timeout_ms = timeout_ms
        self.max_output_bytes = max_output_bytes
        self._process: Optional[subprocess.Popen[str]] = None
        self._lines: "queue.Queue[Optional[str]]" = queue.Queue()
        self._lock = threading.Lock()
        #: set once by close(). Deliberately lock-free (close must never wait
        #: on the execute lock) and relied upon as a single-word store:
        #: readers re-check it under _lock, and any interleaving that slips
        #: through still lands on the OSError -> dead-session mapping below
        #: instead of a None dereference.
        self._closed = False
        #: times a worker was killed for overrunning ``timeout_ms`` (its namespace is gone)
        self.restarts = 0
        self._start_worker()

    def _start_worker(self) -> None:
        """Start the worker subprocess."""
        worker_path = Path(__file__).parent / "worker.py"
        self._process = subprocess.Popen(
            [sys.executable, str(worker_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        if self._process.stdin is None or self._process.stdout is None:
            raise RuntimeError("Failed to create worker subprocess")
        # a blocking readline cannot honour a deadline: one reader thread per worker
        # feeds a queue, and the caller waits on the queue with the timeout
        self._lines = queue.Queue()
        threading.Thread(target=self._pump, args=(self._process.stdout, self._lines),
                         name="awrepl-%s" % self.session_id, daemon=True).start()

    @staticmethod
    def _pump(stream: Any, lines: "queue.Queue[Optional[str]]") -> None:
        try:
            for line in iter(stream.readline, ""):
                lines.put(line)
        except (OSError, ValueError):
            pass
        lines.put(None)  # EOF: the worker exited

    def _read_line(self, timeout_s: Optional[float]) -> str:
        """The worker's next reply line; '' when it exited; TimeoutError past the deadline."""
        try:
            line = self._lines.get(timeout=timeout_s)
        except queue.Empty:
            raise TimeoutError from None
        return line or ""

    def _kill_and_restart(self) -> None:
        proc = self._process
        if proc is None or self._closed:
            # close() ran while this execute() was in flight: restarting here
            # would resurrect a session that is already closed
            raise RuntimeError(f"Session {self.session_id} worker is dead")
        proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)
        self.restarts += 1
        self._start_worker()
        if self._closed:
            # close() raced past its own detach while the restart above was
            # spawning; do not leave a worker behind for a session nobody owns
            stranded, self._process = self._process, None
            if stranded is not None:
                with contextlib.suppress(OSError):
                    stranded.terminate()
            raise RuntimeError(f"Session {self.session_id} worker is dead")

    def execute(self, code: str, timeout_ms: Optional[int] = None) -> ExecResult:
        """
        Execute Python code in the persistent namespace.

        Args:
            code: Python code to execute
            timeout_ms: Override the session's default timeout (ms)

        Returns:
            ExecResult with stdout, stderr, value, exception, etc.
        """
        if self._process is None or self._process.poll() is not None:
            raise RuntimeError(f"Session {self.session_id} worker is dead")

        if timeout_ms is None:
            timeout_ms = self.timeout_ms

        with self._lock:
            # the liveness check above is only a fast path: close() detaches the
            # process without this lock, so re-check under the lock and pin the
            # process in a local. Before this, a close() landing between the
            # check above and this line made the dereferences below raise
            # AttributeError ('NoneType' object has no attribute 'stdin')
            # instead of the dead-session error (reproduced with a close()
            # interleaved exactly there).
            proc = self._process
            if proc is None or self._closed or proc.poll() is not None:
                raise RuntimeError(f"Session {self.session_id} worker is dead")

            request = {
                "action": "execute",
                "code": code,
                "timeout_ms": timeout_ms,
                "max_output_bytes": self.max_output_bytes,
            }

            start_time = time.time()

            try:
                if proc.stdin is None or proc.stdout is None:
                    raise RuntimeError("Worker subprocess broken")

                # Send request
                try:
                    proc.stdin.write(json.dumps(request) + "\n")
                    proc.stdin.flush()
                except OSError as exc:
                    # the worker died between the liveness check and the send
                    # (a concurrent close() does exactly that; measured: Windows
                    # raises OSError(22, 'Invalid argument') on the write into a
                    # dead worker's stdin) -- report the dead session, not the
                    # raw pipe error
                    raise RuntimeError(f"Session {self.session_id} worker is dead") from exc

                # Read response, bounded by the timeout (plus process overhead)
                try:
                    response_line = self._read_line(timeout_ms / 1000.0 + 2.0)
                except TimeoutError:
                    self._kill_and_restart()
                    elapsed_ms = (time.time() - start_time) * 1000
                    return ExecResult(
                        stdout="",
                        stderr="",
                        value=None,
                        exception=(
                            "TimeoutError: execution exceeded %d ms; the worker was killed "
                            "and restarted, so the namespace is now EMPTY" % timeout_ms
                        ),
                        traceback="",
                        duration_ms=elapsed_ms,
                        truncated=False,
                        truncated_bytes=0,
                    )
                elapsed_ms = (time.time() - start_time) * 1000

                if not response_line:
                    raise RuntimeError("Worker subprocess closed unexpectedly")

                response = json.loads(response_line)

                return ExecResult(
                    stdout=response.get("stdout", ""),
                    stderr=response.get("stderr", ""),
                    value=response.get("value"),
                    exception=response.get("exception"),
                    traceback=response.get("traceback", ""),
                    duration_ms=elapsed_ms,
                    truncated=response.get("truncated", False),
                    truncated_bytes=response.get("truncated_bytes", 0),
                )

            except json.JSONDecodeError as e:
                elapsed_ms = (time.time() - start_time) * 1000
                return ExecResult(
                    stdout="",
                    stderr="",
                    value=None,
                    exception=f"JSON decode error: {e}",
                    traceback="",
                    duration_ms=elapsed_ms,
                    truncated=False,
                    truncated_bytes=0,
                )

    def variables(self) -> Dict[str, str]:
        """
        Get currently bound variables in the namespace.

        Returns:
            Dict of {name: "type: repr"}
        """
        if self._process is None or self._process.poll() is not None:
            raise RuntimeError(f"Session {self.session_id} worker is dead")

        with self._lock:
            # re-checked under the lock for the same reason as execute()
            proc = self._process
            if proc is None or self._closed or proc.poll() is not None:
                raise RuntimeError(f"Session {self.session_id} worker is dead")

            request = {"action": "variables"}

            try:
                if proc.stdin is None or proc.stdout is None:
                    raise RuntimeError("Worker subprocess broken")

                try:
                    proc.stdin.write(json.dumps(request) + "\n")
                    proc.stdin.flush()
                except OSError as exc:
                    # worker died between check and send: same treatment as execute()
                    raise RuntimeError(f"Session {self.session_id} worker is dead") from exc

                response_line = self._read_line(30.0)
                if not response_line:
                    raise RuntimeError("Worker subprocess closed unexpectedly")

                response = json.loads(response_line)
                return response.get("variables", {})

            except json.JSONDecodeError:
                return {}

    def inspect(self, name: str) -> Dict[str, Any]:
        """
        Get detailed info about a variable.

        Args:
            name: Variable name to inspect

        Returns:
            Dict with type, repr, docstring, dir (first 20 attrs), etc.
        """
        if self._process is None or self._process.poll() is not None:
            raise RuntimeError(f"Session {self.session_id} worker is dead")

        with self._lock:
            # re-checked under the lock for the same reason as execute()
            proc = self._process
            if proc is None or self._closed or proc.poll() is not None:
                raise RuntimeError(f"Session {self.session_id} worker is dead")

            request = {"action": "inspect", "name": name}

            try:
                if proc.stdin is None or proc.stdout is None:
                    raise RuntimeError("Worker subprocess broken")

                try:
                    proc.stdin.write(json.dumps(request) + "\n")
                    proc.stdin.flush()
                except OSError as exc:
                    # worker died between check and send: same treatment as execute()
                    raise RuntimeError(f"Session {self.session_id} worker is dead") from exc

                response_line = self._read_line(30.0)
                if not response_line:
                    raise RuntimeError("Worker subprocess closed unexpectedly")

                response = json.loads(response_line)
                return response

            except json.JSONDecodeError:
                return {"error": "JSON decode error"}

    def reset(self) -> None:
        """Clear all user-defined variables in the namespace."""
        if self._process is None or self._process.poll() is not None:
            raise RuntimeError(f"Session {self.session_id} worker is dead")

        with self._lock:
            # re-checked under the lock for the same reason as execute()
            proc = self._process
            if proc is None or self._closed or proc.poll() is not None:
                raise RuntimeError(f"Session {self.session_id} worker is dead")

            request = {"action": "reset"}

            try:
                if proc.stdin is None or proc.stdout is None:
                    raise RuntimeError("Worker subprocess broken")

                try:
                    proc.stdin.write(json.dumps(request) + "\n")
                    proc.stdin.flush()
                except OSError as exc:
                    # worker died between check and send: same treatment as execute()
                    raise RuntimeError(f"Session {self.session_id} worker is dead") from exc

                response_line = self._read_line(30.0)
                if not response_line:
                    raise RuntimeError("Worker subprocess closed unexpectedly")

                json.loads(response_line)  # Validate response

            except json.JSONDecodeError:
                pass

    def close(self) -> None:
        """
        Close the session and terminate the worker process.

        Idempotent, and never waits on ``self._lock``: an in-flight execute()
        holds that lock for its whole send/read, and waiting here stalled
        SessionPool.delete_session() for the full execute() duration (measured:
        a 3 s execute() stalled a concurrent delete_session() for ~2.5 s). The
        worker is detached from the session first, so a racing execute() ends
        with the dead-session error instead of a None dereference.
        """
        self._closed = True
        proc = self._process
        if proc is None:
            return
        self._process = None
        with contextlib.suppress(OSError, ValueError):
            # a polite quit into a worker that is already gone: on Windows the
            # write raises OSError(22, 'Invalid argument') (measured, both when
            # the worker was reaped and when it was not); a closed wrapper
            # raises ValueError. BrokenPipeError is an OSError subclass.
            # Best-effort: a racing execute() may be mid-write on the same
            # pipe, and the terminate() below is what actually matters.
            if proc.stdin is not None:
                proc.stdin.write(json.dumps({"action": "quit"}) + "\n")
                proc.stdin.flush()
        with contextlib.suppress(OSError):
            # TerminateProcess can also refuse an already-exited worker
            proc.terminate()
        with contextlib.suppress(OSError):
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(OSError):
                    proc.kill()
                    proc.wait(timeout=2)  # reap; kill() alone leaves a zombie

    def __enter__(self) -> "ReplSession":
        """Context manager entry."""
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Context manager exit."""
        self.close()

    def __del__(self) -> None:
        """Cleanup on garbage collection."""
        self.close()
