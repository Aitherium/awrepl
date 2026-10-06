"""
Tests for ReplSession - core execution and state persistence.
"""

import threading
import time

import pytest
from awrepl import ExecResult, ReplSession


class TestBasicExecution:
    """Test basic code execution."""

    def test_simple_code_execution(self):
        """Execute simple code and capture output."""
        session = ReplSession("test")
        result = session.execute("print('hello')")
        assert result.stdout == "hello\n"
        assert result.exception is None
        session.close()

    def test_last_expression_value(self):
        """Capture the value of the last expression."""
        session = ReplSession("test")
        result = session.execute("42")
        assert "42" in (result.value or "")
        session.close()

    def test_syntax_error(self):
        """Handle syntax errors gracefully."""
        session = ReplSession("test")
        result = session.execute("if True")
        assert result.exception is not None
        assert "SyntaxError" in result.exception
        session.close()

    def test_runtime_error(self):
        """Handle runtime errors gracefully."""
        session = ReplSession("test")
        result = session.execute("x = 1 / 0")
        assert result.exception is not None
        assert "ZeroDivisionError" in result.exception
        session.close()


class TestStatePersistence:
    """Test that state persists across multiple execute() calls."""

    def test_variable_persists(self):
        """A variable defined in one call is accessible in another."""
        session = ReplSession("test")
        session.execute("x = 42")
        result = session.execute("print(x)")
        assert "42" in result.stdout
        session.close()

    def test_function_persists(self):
        """A function defined in one call is callable in another."""
        session = ReplSession("test")
        session.execute("def double(n):\n    return n * 2")
        result = session.execute("print(double(21))")
        assert "42" in result.stdout
        session.close()

    def test_import_persists(self):
        """An import in one call is accessible in another."""
        session = ReplSession("test")
        session.execute("import math")
        result = session.execute("print(math.pi)")
        assert "3.14" in result.stdout
        session.close()

    def test_multiple_statements(self):
        """Multiple statements build on each other."""
        session = ReplSession("test")
        session.execute("items = []")
        session.execute("items.append(1)")
        session.execute("items.append(2)")
        result = session.execute("print(len(items))")
        assert "2" in result.stdout
        session.close()

    def test_exception_doesnt_kill_session(self):
        """After an exception, the session still works."""
        session = ReplSession("test")
        session.execute("x = 10")
        session.execute("y = 1 / 0")  # This raises
        result = session.execute("print(x)")
        assert "10" in result.stdout
        assert result.exception is None
        session.close()


class TestOutputCapture:
    """Test stdout/stderr capture."""

    def test_stdout_capture(self):
        """Capture stdout."""
        session = ReplSession("test")
        result = session.execute("print('test output')")
        assert result.stdout == "test output\n"
        session.close()

    def test_stderr_capture(self):
        """Capture stderr."""
        session = ReplSession("test")
        result = session.execute("import sys; sys.stderr.write('error\\n')")
        assert result.stderr == "error\n"
        session.close()

    def test_mixed_output(self):
        """Capture both stdout and stderr."""
        session = ReplSession("test")
        result = session.execute(
            "import sys; print('out'); sys.stderr.write('err\\n')"
        )
        assert "out" in result.stdout
        assert "err" in result.stderr
        session.close()

    def test_output_truncation(self):
        """Large output is truncated with a flag."""
        session = ReplSession("test", max_output_bytes=50)
        big_output = "x" * 1000
        result = session.execute(f"print('{big_output}')")
        assert result.truncated is True
        assert result.truncated_bytes > 0
        assert len(result.stdout) <= 50
        session.close()


class TestVariables:
    """Test variables() and inspect() methods."""

    def test_variables_lists_bound_names(self):
        """variables() returns all bound variable names."""
        session = ReplSession("test")
        session.execute("foo = 'bar'")
        session.execute("baz = [1, 2, 3]")
        vars_dict = session.variables()
        assert "foo" in vars_dict
        assert "baz" in vars_dict
        session.close()

    def test_variables_excludes_builtins(self):
        """variables() does not include __builtins__."""
        session = ReplSession("test")
        session.execute("x = 1")
        vars_dict = session.variables()
        assert "__builtins__" not in vars_dict
        session.close()

    def test_inspect_returns_type_and_repr(self):
        """inspect() returns type and repr of a variable."""
        session = ReplSession("test")
        session.execute("x = [1, 2, 3]")
        info = session.inspect("x")
        assert "type" in info
        assert info["type"] == "list"
        assert "repr" in info
        session.close()

    def test_inspect_nonexistent_variable(self):
        """inspect() handles nonexistent variables."""
        session = ReplSession("test")
        info = session.inspect("does_not_exist")
        assert "error" in info
        session.close()

    def test_inspect_includes_docstring(self):
        """inspect() includes docstring if available."""
        session = ReplSession("test")
        session.execute(
            "def my_func():\n    '''This is a docstring'''\n    pass"
        )
        info = session.inspect("my_func")
        assert "docstring" in info
        session.close()


class TestReset:
    """Test reset() method."""

    def test_reset_clears_variables(self):
        """reset() clears all user-defined variables."""
        session = ReplSession("test")
        session.execute("x = 42")
        session.execute("y = 'hello'")
        session.reset()
        vars_dict = session.variables()
        assert "x" not in vars_dict
        assert "y" not in vars_dict
        session.close()

    def test_reset_keeps_builtins(self):
        """reset() keeps builtins available."""
        session = ReplSession("test")
        session.execute("x = 42")
        session.reset()
        result = session.execute("print(len([1, 2, 3]))")
        assert "3" in result.stdout
        session.close()


class TestContextManager:
    """Test context manager support."""

    def test_context_manager_closes_session(self):
        """Session can be used as a context manager."""
        with ReplSession("test") as session:
            session.execute("x = 42")
            result = session.execute("print(x)")
            assert "42" in result.stdout
        # Session should be closed after exiting context


class _ProbedLock:
    """threading.Lock stand-in that records when execute() reaches acquire()."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.acquire_called = threading.Event()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        self.acquire_called.set()
        return self._lock.acquire(blocking, timeout)

    def release(self) -> None:
        self._lock.release()

    def __enter__(self) -> None:
        self.acquire()

    def __exit__(self, *exc_info: object) -> None:
        self.release()


class TestConcurrentClose:
    """close() racing execute() must not corrupt the session."""

    def test_execute_racing_close_never_raises_attribute_error(self):
        """A close() landing between execute()'s liveness check and its lock must not
        make execute() dereference a None worker.

        The interleave is forced: the session lock starts held (as an in-flight
        execute() would hold it), a second execute() passes the pre-lock liveness
        check and blocks on the lock, and the worker is detached in that window.
        Pre-fix this raised AttributeError: 'NoneType' object has no attribute
        'stdin' instead of the dead-session RuntimeError.
        """
        session = ReplSession("race", timeout_ms=5000)
        real_lock = session._lock
        real_process = session._process
        probe = _ProbedLock()
        session._lock = probe
        caught: list[BaseException] = []

        def run_execute() -> None:
            try:
                session.execute("1 + 1")
            except BaseException as exc:  # the exception type is the assertion
                caught.append(exc)

        try:
            probe.acquire()  # hold the lock like an in-flight execute()
            worker = threading.Thread(target=run_execute, daemon=True)
            worker.start()
            assert probe.acquire_called.wait(5), "execute() never reached the lock"
            session._process = None  # the detach close() performs
            probe.release()
            worker.join(timeout=5)
            assert not worker.is_alive()
            assert len(caught) == 1, f"expected one error, got {caught!r}"
            assert not isinstance(caught[0], AttributeError), (
                f"execute() dereferenced the closed worker: {caught[0]!r}"
            )
            assert isinstance(caught[0], RuntimeError)
            assert "worker is dead" in str(caught[0])
        finally:
            session._lock = real_lock
            if session._process is None:
                session._process = real_process
            session.close()

    def test_close_on_reaped_dead_worker_is_clean(self):
        """close() after the worker exited (and was waited on) returns cleanly.

        Pre-fix on Windows the polite-quit write into the dead worker's stdin
        raised OSError(22, 'Invalid argument'), which the
        except (BrokenPipeError, ValueError) around it did not catch.
        """
        session = ReplSession("dead", timeout_ms=2000)
        process = session._process
        process.kill()
        process.wait(timeout=5)

        session.close()  # must not raise

        assert session._process is None

    def test_close_on_unwaited_dead_worker_is_clean(self):
        """close() after the worker exited on its own (nobody waited) returns cleanly."""
        session = ReplSession("dead-unwaited", timeout_ms=2000)
        process = session._process
        process.kill()
        time.sleep(0.5)  # exited, but wait() was never called

        session.close()  # must not raise

        assert session._process is None

    def test_close_is_idempotent_after_worker_died(self):
        """A second close() on an already-closed session is a no-op."""
        session = ReplSession("twice", timeout_ms=2000)
        session._process.kill()
        session.close()
        session.close()
        assert session._process is None

    def test_execute_send_after_worker_death_reports_dead_session(self):
        """A worker that dies between execute()'s liveness check and its send must
        surface the dead-session RuntimeError, not a raw pipe OSError.

        Windows measured: writing into a dead worker's stdin raises
        OSError(22, 'Invalid argument') -- reachable now that close() terminates
        without waiting for an in-flight execute().
        """

        class _DeadPipe:
            def write(self, data: str) -> int:
                raise OSError(22, "Invalid argument")

            def flush(self) -> None:
                pass

        class _FakeProc:
            stdin = _DeadPipe()
            stdout = object()

            def poll(self) -> None:
                return None

        session = ReplSession("send-race", timeout_ms=1000)
        real_process = session._process
        session._process = _FakeProc()
        try:
            with pytest.raises(RuntimeError, match="worker is dead"):
                session.execute("1 + 1")
        finally:
            session._process = real_process
            session.close()


class TestExecResult:
    """Test ExecResult dataclass."""

    def test_exec_result_fields(self):
        """ExecResult has all expected fields."""
        session = ReplSession("test")
        result = session.execute("x = 1")
        assert isinstance(result, ExecResult)
        assert hasattr(result, "stdout")
        assert hasattr(result, "stderr")
        assert hasattr(result, "value")
        assert hasattr(result, "exception")
        assert hasattr(result, "traceback")
        assert hasattr(result, "duration_ms")
        assert hasattr(result, "truncated")
        assert hasattr(result, "truncated_bytes")
        session.close()

    def test_exec_result_success(self):
        """ExecResult reflects successful execution."""
        session = ReplSession("test")
        result = session.execute("x = 42")
        assert result.exception is None
        assert isinstance(result.duration_ms, float)
        assert result.truncated is False
        session.close()

    def test_exec_result_failure(self):
        """ExecResult reflects failed execution."""
        session = ReplSession("test")
        result = session.execute("raise ValueError('test')")
        assert result.exception is not None
        assert "ValueError" in result.exception
        session.close()
