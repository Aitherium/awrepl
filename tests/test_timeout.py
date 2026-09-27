"""timeout_ms is enforced: a runaway execute() returns, the worker is replaced,
and the caller is told its namespace is gone (it was: the process was killed)."""

import time

from awrepl import ReplSession


def test_a_runaway_execute_returns_within_its_timeout():
    session = ReplSession("t-timeout")
    try:
        session.execute("x = 41")
        t0 = time.monotonic()
        result = session.execute("while True:\n    pass", timeout_ms=500)
        waited = time.monotonic() - t0
        assert waited < 8.0, "execute() blocked %.1f s past a 500 ms timeout" % waited
        assert result.exception is not None and result.exception.startswith("TimeoutError")
        assert "namespace is now EMPTY" in result.exception
        assert session.restarts == 1
        # the session keeps working on a fresh worker, and says so by losing x
        after = session.execute("print(1 + 1)")
        assert after.stdout == "2\n" and after.exception is None
        gone = session.execute("x")
        assert gone.exception is not None and "NameError" in gone.exception
    finally:
        session.close()


def test_a_fast_execute_is_untouched_by_the_deadline():
    session = ReplSession("t-fast", timeout_ms=5000)
    try:
        session.execute("y = 20")
        result = session.execute("y + 22")
        assert "42" in (result.value or "") and session.restarts == 0
    finally:
        session.close()
