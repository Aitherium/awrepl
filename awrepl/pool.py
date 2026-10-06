"""
SessionPool: Manage multiple REPL sessions indexed by ID.

Allows different agents or turns to maintain separate execution namespaces
without interfering with each other.
"""

import threading
import uuid
from typing import Dict, Optional

from .session import ReplSession


class SessionPool:
    """Pool of ReplSession instances, managed by session ID."""

    def __init__(self, timeout_ms: int = 30000, max_output_bytes: int = 65536) -> None:
        """
        Initialize the session pool.

        Args:
            timeout_ms: Default timeout for new sessions
            max_output_bytes: Default max output bytes for new sessions
        """
        self._sessions: Dict[str, ReplSession] = {}
        self._lock = threading.Lock()
        self.timeout_ms = timeout_ms
        self.max_output_bytes = max_output_bytes

    def create_session(self, session_id: Optional[str] = None) -> str:
        """
        Create a new REPL session.

        Args:
            session_id: Optional custom ID; generated if not provided

        Returns:
            The session ID
        """
        if session_id is None:
            session_id = str(uuid.uuid4())

        with self._lock:
            if session_id in self._sessions:
                raise ValueError(f"Session {session_id} already exists")

            session = ReplSession(
                session_id,
                timeout_ms=self.timeout_ms,
                max_output_bytes=self.max_output_bytes,
            )
            self._sessions[session_id] = session

        return session_id

    def get_session(self, session_id: str) -> ReplSession:
        """
        Get an existing session by ID.

        Args:
            session_id: Session ID

        Returns:
            The ReplSession

        Raises:
            KeyError if session not found
        """
        with self._lock:
            if session_id not in self._sessions:
                raise KeyError(f"Session {session_id} not found")
            return self._sessions[session_id]

    def delete_session(self, session_id: str) -> None:
        """
        Close and remove a session.

        The pool lock is held only for the map removal; ``close()`` runs
        outside it. Consequently deletion is NOT atomic with respect to
        ``get_session``: a caller that already holds the ``ReplSession`` may
        find it closed underneath it and must handle the dead-session
        ``RuntimeError`` (see ``ReplSession.execute``).

        Args:
            session_id: Session ID
        """
        # pop under the pool lock, close outside it: close() can take a while,
        # and holding the pool lock across it stalled every other pool call --
        # measured: an in-flight 3 s execute() stalled a concurrent
        # delete_session() by ~2.5 s and, through the pool lock, an unrelated
        # list_sessions() with it.
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is not None:
            session.close()

    def list_sessions(self) -> list[str]:
        """
        List all active session IDs.

        Returns:
            List of session IDs
        """
        with self._lock:
            return list(self._sessions.keys())

    def close_all(self) -> None:
        """Close all sessions in the pool.

        Best-effort teardown, not a barrier: sessions created concurrently
        after the map is cleared are not closed (the pool is empty to all
        readers the moment the lock is released).
        """
        # detach the sessions under the pool lock and close them outside it,
        # for the same reason as delete_session()
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.close()

    def __del__(self) -> None:
        """Cleanup on garbage collection."""
        self.close_all()
