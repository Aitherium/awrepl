"""
Tests for SessionPool - multiple session management and isolation.
"""

import pytest
from awrepl import SessionPool


class TestSessionCreation:
    """Test creating sessions in a pool."""

    def test_create_session_with_auto_id(self):
        """create_session() generates an ID if not provided."""
        pool = SessionPool()
        session_id = pool.create_session()
        assert session_id is not None
        assert isinstance(session_id, str)
        pool.close_all()

    def test_create_session_with_custom_id(self):
        """create_session() accepts a custom ID."""
        pool = SessionPool()
        session_id = pool.create_session("my-session")
        assert session_id == "my-session"
        pool.close_all()

    def test_create_duplicate_session_raises(self):
        """Creating a session with an existing ID raises ValueError."""
        pool = SessionPool()
        pool.create_session("duplicate")
        with pytest.raises(ValueError):
            pool.create_session("duplicate")
        pool.close_all()

    def test_get_nonexistent_session_raises(self):
        """get_session() raises KeyError for nonexistent session."""
        pool = SessionPool()
        with pytest.raises(KeyError):
            pool.get_session("nonexistent")
        pool.close_all()


class TestSessionIsolation:
    """Test that sessions don't interfere with each other."""

    def test_separate_namespaces(self):
        """Each session has its own namespace."""
        pool = SessionPool()
        s1 = pool.create_session("s1")
        s2 = pool.create_session("s2")

        pool.get_session(s1).execute("x = 'session1'")
        pool.get_session(s2).execute("x = 'session2'")

        r1 = pool.get_session(s1).execute("print(x)")
        r2 = pool.get_session(s2).execute("print(x)")

        assert "session1" in r1.stdout
        assert "session2" in r2.stdout
        pool.close_all()

    def test_function_isolation(self):
        """Functions defined in one session don't affect another."""
        pool = SessionPool()
        s1 = pool.create_session("s1")
        s2 = pool.create_session("s2")

        pool.get_session(s1).execute("def add(a, b):\n    return a + b")
        pool.get_session(s2).execute("def add(a, b):\n    return a - b")

        r1 = pool.get_session(s1).execute("print(add(5, 3))")
        r2 = pool.get_session(s2).execute("print(add(5, 3))")

        assert "8" in r1.stdout  # 5 + 3
        assert "2" in r2.stdout  # 5 - 3
        pool.close_all()

    def test_import_isolation(self):
        """Imports in one session don't affect another."""
        pool = SessionPool()
        s1 = pool.create_session("s1")
        s2 = pool.create_session("s2")

        pool.get_session(s1).execute("import math")
        result = pool.get_session(s2).execute("print(math.pi)")

        assert result.exception is not None
        assert "NameError" in result.exception
        pool.close_all()


class TestSessionManagement:
    """Test pool management operations."""

    def test_list_sessions(self):
        """list_sessions() returns all session IDs."""
        pool = SessionPool()
        pool.create_session("s1")
        pool.create_session("s2")
        pool.create_session("s3")

        sessions = pool.list_sessions()
        assert len(sessions) == 3
        assert "s1" in sessions
        assert "s2" in sessions
        assert "s3" in sessions
        pool.close_all()

    def test_delete_session(self):
        """delete_session() removes a session from the pool."""
        pool = SessionPool()
        s1_id = pool.create_session("s1")
        pool.create_session("s2")

        pool.delete_session(s1_id)

        sessions = pool.list_sessions()
        assert len(sessions) == 1
        assert "s2" in sessions
        assert "s1" not in sessions
        pool.close_all()

    def test_delete_nonexistent_session_is_noop(self):
        """delete_session() on nonexistent session is a no-op."""
        pool = SessionPool()
        pool.delete_session("nonexistent")  # Should not raise
        pool.close_all()

    def test_close_all(self):
        """close_all() removes all sessions."""
        pool = SessionPool()
        pool.create_session("s1")
        pool.create_session("s2")
        pool.create_session("s3")

        pool.close_all()

        sessions = pool.list_sessions()
        assert len(sessions) == 0


class TestPoolConfiguration:
    """Test pool configuration options."""

    def test_pool_timeout_config(self):
        """Pool passes timeout to new sessions."""
        pool = SessionPool(timeout_ms=5000)
        s1 = pool.create_session("s1")
        session = pool.get_session(s1)
        assert session.timeout_ms == 5000
        pool.close_all()

    def test_pool_max_output_bytes_config(self):
        """Pool passes max_output_bytes to new sessions."""
        pool = SessionPool(max_output_bytes=1024)
        s1 = pool.create_session("s1")
        session = pool.get_session(s1)
        assert session.max_output_bytes == 1024
        pool.close_all()
