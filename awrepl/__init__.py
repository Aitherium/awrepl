"""
awrepl: A REPL an agent can actually use.

Gives an agent a Python session whose state survives between turns, so the
next question is asked of the live object instead of of the agent's memory.

Example:
    >>> from awrepl import ReplSession, SessionPool
    >>> session = ReplSession("my-agent")
    >>> result = session.execute("x = 42")
    >>> result = session.execute("print(x)")
    >>> print(result.stdout)
    42
    >>> session.close()

    >>> pool = SessionPool()
    >>> s1 = pool.create_session("agent1")
    >>> pool.get_session(s1).execute("data = [1, 2, 3]")
    >>> # Namespace persists for the next turn
"""

__version__ = "0.1.0"

from .pool import SessionPool
from .session import ExecResult, ReplSession

__all__ = [
    "ReplSession",
    "ExecResult",
    "SessionPool",
]
