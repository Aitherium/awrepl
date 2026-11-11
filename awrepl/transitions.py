"""Every ``execute()`` as an observed transition in an awm world model. Optional.

A REPL that only counts its timeouts learns nothing from what it ran. This module
turns one call into the triple a world model needs, and hands it to awm's transition
table (``awm.world``: RECALLED / GENERALIZED / NONE prediction, surprise, calibration)
through the same calls adk's backend uses -- ``MemoryStore.predict_outcome`` and
``MemoryStore.observe_transition``. Nothing here touches awm's tables directly.

What is recorded, and what never is:

- **State** (slots under the ``awrepl.`` prefix): a 16-hex digest of the sorted
  ``(variable name, type name)`` pairs, the sorted distinct type names (capped), a
  BUCKET of the number of prior calls, and whether the worker was just restarted.
  The call count is bucketed on purpose: awm recalls by exact state, and a raw
  counter would make every state unique, so nothing could ever be RECALLED.
- **Action**: a token derived from the code's AST shape (``action_token``), never
  from its text: top-level statement kinds, imported top-level modules, names of
  called functions / attributes, a size bucket. A syntax error is its own token.
- **Outcome** (two slots only the AFTER state carries): ``ok`` |
  ``error:<ExceptionClassName>`` | ``timeout``, and a duration bucket.
- **Never**: code text, variable values, variable names (digest only), exception
  messages, stdout, stderr.

Recording is on by default (``AWREPL_RECORD_TRANSITIONS``, "0" = off, and off is the
exact previous behaviour), but only where the environment already names an awm
scope: ``AITHER_AGENT_WM_AWM_SCOPE``, the template adk's awm backend reads. Its
``{agent}`` segment is filled with ``AWREPL_TRANSITIONS_AGENT`` (default
``awrepl``), so the REPL's rows sit beside an agent's own, never inside them. No
template, or one without ``{agent}`` (adk refuses that too): nothing is recorded --
a scope is never invented. The file is adk's as well: ``AITHER_AGENT_WM_AWM_DB``,
else ``<AITHER_AGENT_WM_DIR or ~/.aither/wm>/awm_world.db``, opened with
``auto_migrate=False`` (migrating a shared file is its owner's decision).

awm is OPTIONAL and recording may never cost a call. Every awm call runs on one
daemon thread; the session waits at most ``AWREPL_TRANSITIONS_BUDGET_MS`` (250) for
it, treats a late or failed answer as "no prediction", and after
``MAX_CONSECUTIVE_FAILURES`` in a row switches recording off for the process with
one warning. awm absent, an old awm, an unreadable file, a raising store: the call
returns exactly what it would have returned.
"""

from __future__ import annotations

import ast
import logging
import os
import queue
import re
import threading
import time
import warnings
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger("awrepl.transitions")

ENV_FLAG = "AWREPL_RECORD_TRANSITIONS"
ENV_AGENT = "AWREPL_TRANSITIONS_AGENT"
ENV_BUDGET_MS = "AWREPL_TRANSITIONS_BUDGET_MS"
#: adk's own variables (awdk/adk/world.py, awdk/adk/worldmodel.py): read, never defined here.
ENV_SCOPE = "AITHER_AGENT_WM_AWM_SCOPE"
ENV_DB = "AITHER_AGENT_WM_AWM_DB"
ENV_ROOT = "AITHER_AGENT_WM_DIR"

DEFAULT_AGENT = "awrepl"
DEFAULT_BUDGET_MS = 250.0
MAX_CONSECUTIVE_FAILURES = 3
QUEUE_SIZE = 64
#: ``source`` stamped on every row this module writes.
SOURCE = "awrepl"

PREFIX = "awrepl"
VARS_SLOT = PREFIX + ".vars"
TYPES_SLOT = PREFIX + ".types"
CALLS_SLOT = PREFIX + ".calls"
RESTARTED_SLOT = PREFIX + ".restarted"
OUTCOME_SLOT = PREFIX + ".outcome"
DURATION_SLOT = PREFIX + ".duration"

#: Code longer than this is not parsed (parsing is paid on the caller's thread).
MAX_PARSE_CHARS = 200_000
#: Cap on imported-module names and on called names in one action token.
MAX_NAMES = 20
MAX_NAME_CHARS = 48
MAX_TYPES = 12

SYNTAX_ERROR_TOKEN = "py:syntax-error"
OUTCOME_OK = "ok"
OUTCOME_TIMEOUT = "timeout"
#: The worker exited mid-call (``os._exit``, a crash). The session is dead after it.
OUTCOME_DIED = "error:WorkerDied"
OUTCOME_UNKNOWN = "error:Unknown"

_OFF = {"0", "false", "off", "no"}
_SIG = re.compile(r"^[0-9a-f]{16}$")
#: What ``{agent}`` may not expand to (adk's rule for the same template).
_AGENT_SEGMENT_BAD = re.compile(r"[*:\x00-\x1f\x7f]")
#: The digest of an empty namespace, as the worker computes it (sha256 of b"").
EMPTY_SIG = "e3b0c44298fc1c14"


# ------------------------------------------------------------ pure: action
def _bucket(value: float, edges: Tuple[Tuple[float, str], ...], last: str) -> str:
    for limit, label in edges:
        if value < limit:
            return label
    return last


def size_bucket(n_chars: int) -> str:
    return _bucket(n_chars, ((80, "xs"), (400, "s"), (2000, "m"), (10000, "l")), "xl")


def duration_bucket(ms: float) -> str:
    return _bucket(ms, ((10, "<10ms"), (100, "<100ms"), (1000, "<1s"), (10000, "<10s")),
                   ">=10s")


def calls_bucket(n: int) -> str:
    return _bucket(n, ((10, "0-9"), (100, "10-99")), "100+")


def _names(found: set) -> str:
    return ",".join(sorted(n[:MAX_NAME_CHARS] for n in found)[:MAX_NAMES])


def action_token(code: str) -> str:
    """The canonical action for ``code``: its AST shape, never its text.

    ``py:k=<kinds>|i=<modules>|c=<calls>|s=<size>`` -- sorted distinct top-level
    statement kinds; sorted top-level names of imported modules (``.`` for a
    relative import); sorted names of called functions (``name``) and attributes
    (``.attr``); a size bucket. Module and call lists are capped at MAX_NAMES.
    Only identifiers reach the token: no literal, comment or string ever does.
    Code that does not parse is ``py:syntax-error``; code that was not parsed
    (too long, or the parser gave up) is ``py:unparsed|s=<size>``.
    """
    size = size_bucket(len(code))
    if len(code) > MAX_PARSE_CHARS:
        return f"py:unparsed|s={size}"
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # a SyntaxWarning is the worker's to print
            tree = ast.parse(code)
    except SyntaxError:
        return SYNTAX_ERROR_TOKEN
    except (ValueError, RecursionError, MemoryError):
        return f"py:unparsed|s={size}"
    kinds = sorted({type(node).__name__ for node in tree.body})
    imports: set = set()
    calls: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add("." if node.level or not node.module else node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                calls.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                calls.add("." + node.func.attr)
    return f"py:k={','.join(kinds)}|i={_names(imports)}|c={_names(calls)}|s={size}"


# ------------------------------------------------------------ pure: state, outcome
def _type_name(name: Any) -> str:
    # ``type("any text at all", (), {})`` is legal: only an identifier is kept.
    return name if isinstance(name, str) and name.isidentifier() \
        and len(name) <= MAX_NAME_CHARS else "?"


def clean_shape(shape: Any) -> Optional[Dict[str, Any]]:
    """The worker's namespace shape, validated; None when it is not one."""
    if not isinstance(shape, dict):
        return None
    sig, types = shape.get("sig"), shape.get("types")
    if not isinstance(sig, str) or not _SIG.match(sig) or not isinstance(types, list):
        return None
    return {"sig": sig, "types": sorted({_type_name(t) for t in types})[:MAX_TYPES],
            "more": bool(shape.get("more")) or len(types) > MAX_TYPES}


def state_slots(shape: Optional[Dict[str, Any]], calls: int, restarted: bool) -> Dict[str, str]:
    """The session's state as awm slots. ``shape`` None = an empty namespace."""
    shape = shape or {"sig": EMPTY_SIG, "types": [], "more": False}
    return {
        VARS_SLOT: shape["sig"],
        TYPES_SLOT: ",".join(shape["types"]) + ("+" if shape["more"] else ""),
        CALLS_SLOT: calls_bucket(calls),
        RESTARTED_SLOT: "1" if restarted else "0",
    }


def outcome_token(exception: Optional[str], *, timed_out: bool = False,
                  died: bool = False) -> str:
    """``ok`` | ``error:<ExceptionClassName>`` | ``timeout``. Never the message."""
    if died:
        return OUTCOME_DIED
    if timed_out:
        return OUTCOME_TIMEOUT
    if not exception:
        return OUTCOME_OK
    cls = exception.split(":", 1)[0].strip()
    if cls.isidentifier() and len(cls) <= 64:
        return "error:" + cls
    return OUTCOME_UNKNOWN


def describe_prediction(pred: Any) -> Optional[Dict[str, Any]]:
    """What a caller sees of an awm ``Prediction``: where it came from, and what it expects."""
    if pred is None:
        return None
    delta = getattr(pred, "delta", None) or {}
    return {
        "source": str(getattr(pred, "source", "NONE")),
        "confidence": float(getattr(pred, "confidence", 0.0) or 0.0),
        "support": int(getattr(pred, "support", 0) or 0),
        "outcome": delta.get(OUTCOME_SLOT),
        "duration": delta.get(DURATION_SLOT),
    }


# ------------------------------------------------------------ awm, guarded
def _load_awm() -> Any:
    """``(awm, awm.world)``. Raises when awm is absent or has no world model."""
    import awm  # type: ignore[import-not-found]
    from awm import world as awm_world  # type: ignore[import-not-found]
    if not hasattr(awm_world, "observe_transition"):
        raise RuntimeError("awm %s has no world model" % getattr(awm, "__version__", "?"))
    return awm, awm_world


class _Box:
    __slots__ = ("done", "value", "error")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.value: Any = None
        self.error: Optional[BaseException] = None


class TransitionRecorder:
    """One awm store + scope, used from one daemon thread. No method raises."""

    def __init__(self, db_path: Path, scope_text: str, *,
                 budget_s: float = DEFAULT_BUDGET_MS / 1000.0) -> None:
        self.db_path = Path(db_path)
        self.scope_text = scope_text
        self.budget_s = budget_s
        #: Why recording is off for good (None = on).
        self.disabled: Optional[str] = None
        self.recorded = 0
        self.failures = 0
        self._streak = 0
        self._store: Any = None
        self._scope: Any = None
        self._world: Any = None
        self._jobs: "queue.Queue[Optional[Tuple[Callable[[], Any], _Box]]]" = \
            queue.Queue(maxsize=QUEUE_SIZE)
        self._thread = threading.Thread(target=self._run, name="awrepl-transitions",
                                        daemon=True)
        self._thread.start()

    # -- the recorder thread ------------------------------------------------
    def _open(self) -> None:
        awm, self._world = _load_awm()
        self._scope = awm.Scope.parse(self.scope_text)
        store = awm.MemoryStore(self.db_path, auto_migrate=False)
        if getattr(store, "compat", False):
            version = getattr(store, "file_version", "?")
            store.close()
            raise RuntimeError("awm file is schema v%s: it needs migrating first" % version)
        self._store = store

    def _run(self) -> None:
        try:
            self._open()
        except Exception as exc:  # noqa: BLE001 -- awm is optional
            self._disable("awm unavailable: %s: %s" % (type(exc).__name__, exc))
        while True:
            job = self._jobs.get()
            if job is None:
                break
            fn, box = job
            try:
                if self._store is None:
                    raise RuntimeError(self.disabled or "no awm store")
                box.value = fn()
            except Exception as exc:  # noqa: BLE001 -- reported to the waiter
                box.error = exc
            box.done.set()
        store, self._store = self._store, None
        if store is not None:
            try:
                store.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("awrepl: closing the awm store failed: %s", exc)

    def _state(self, slots: Dict[str, str], ts: float) -> Any:
        return self._world.WorldState(digest=self._world.state_digest(slots), slots=dict(slots),
                                      scope=str(self._scope), prefix=PREFIX, as_of=None, ts=ts)

    def _now(self) -> float:
        return float(getattr(self._store, "_clock", time.time)())

    # -- the caller's side --------------------------------------------------
    def _disable(self, reason: str) -> None:
        if self.disabled is None:
            self.disabled = reason
            logger.warning("awrepl: transition recording is off for this process (%s)", reason)

    def _fail(self, why: str) -> None:
        self.failures += 1
        self._streak += 1
        logger.debug("awrepl: transition recording failed (%s)", why)
        if self._streak >= MAX_CONSECUTIVE_FAILURES:
            self._disable("%d failures in a row, last: %s" % (self._streak, why))

    def _call(self, fn: Callable[[], Any]) -> Any:
        if self.disabled is not None:
            return None
        box = _Box()
        try:
            self._jobs.put_nowait((fn, box))
        except queue.Full:
            self._fail("queue full")
            return None
        if not box.done.wait(self.budget_s):
            self._fail("no answer within %.0f ms" % (self.budget_s * 1000))
            return None
        if box.error is not None:
            self._fail("%s: %s" % (type(box.error).__name__, box.error))
            return None
        self._streak = 0
        return box.value

    def predict(self, before: Dict[str, str], action: str) -> Any:
        """awm's ``Prediction`` for (state, action), made BEFORE the call runs; or None."""
        return self._call(lambda: self._store.predict_outcome(
            self._scope, self._state(before, self._now()), action))

    def observe(self, before: Dict[str, str], action: str, after: Dict[str, str],
                predicted: Any = None) -> Any:
        """Record the transition, scored against ``predicted``; awm's ``Transition`` or None."""
        def job() -> Any:
            now = self._now()
            done = self._store.observe_transition(
                self._scope, self._state(before, now), action, self._state(after, now),
                predicted, source=SOURCE)
            self.recorded += 1
            return done
        return self._call(job)

    def close(self) -> None:
        """Stop the thread and close the store. Idempotent; for tests and clean exits."""
        if self.disabled is None:
            self.disabled = "closed"
        try:
            self._jobs.put(None, timeout=1.0)
        except queue.Full:
            return
        self._thread.join(timeout=5.0)


# ------------------------------------------------------------ configuration
_RECORDERS: Dict[Tuple[str, str], TransitionRecorder] = {}
_RECORDERS_LOCK = threading.Lock()
#: Why the last ``recorder_from_env`` call recorded nothing (None = it records).
_last_reason: Optional[str] = None


def resolve_target(environ: Mapping[str, str]) -> Tuple[Optional[Tuple[Path, str]], str]:
    """``((db path, scope text), "")`` from the environment, or ``(None, why not)``."""
    if environ.get(ENV_FLAG, "1").strip().lower() in _OFF:
        return None, "%s is off" % ENV_FLAG
    template = environ.get(ENV_SCOPE, "").strip()
    if not template:
        return None, "%s is not set: no awm scope is defined, none is invented" % ENV_SCOPE
    if "{agent}" not in template:
        return None, "%s has no {agent} segment (adk refuses it too)" % ENV_SCOPE
    agent = environ.get(ENV_AGENT, "").strip() or DEFAULT_AGENT
    if len(agent) > 128 or _AGENT_SEGMENT_BAD.search(agent):
        return None, "%s=%r cannot be one scope segment" % (ENV_AGENT, agent)
    scope_text = template.replace("{agent}", agent)
    if len(scope_text.split(":")) != 3:
        return None, "%s=%r is not tenant:user:project" % (ENV_SCOPE, template)
    db = environ.get(ENV_DB, "").strip()
    if not db:
        root = environ.get(ENV_ROOT) or os.path.join(os.path.expanduser("~"), ".aither", "wm")
        db = os.path.join(root, "awm_world.db")
    return (Path(db), scope_text), ""


def recorder_from_env(environ: Optional[Mapping[str, str]] = None) -> Optional[TransitionRecorder]:
    """The process's recorder for the environment's awm scope, or None. Never raises."""
    global _last_reason
    try:
        env = os.environ if environ is None else environ
        target, why = resolve_target(env)
        if target is None:
            _last_reason = why
            return None
        db, scope_text = target
        try:
            budget = float(env.get(ENV_BUDGET_MS, "") or DEFAULT_BUDGET_MS) / 1000.0
        except ValueError:
            budget = DEFAULT_BUDGET_MS / 1000.0
        key = (str(db), scope_text)
        with _RECORDERS_LOCK:
            rec = _RECORDERS.get(key)
            if rec is None:
                rec = _RECORDERS[key] = TransitionRecorder(db, scope_text, budget_s=budget)
        _last_reason = None
        return rec
    except Exception as exc:  # noqa: BLE001 -- recording is never a reason to fail
        _last_reason = "%s: %s" % (type(exc).__name__, exc)
        return None


def status() -> Dict[str, Any]:
    """Whether this process records, where, and why not when it does not."""
    with _RECORDERS_LOCK:
        recs: List[TransitionRecorder] = list(_RECORDERS.values())
    return {
        "reason": _last_reason,
        "recorders": [{"db": str(r.db_path), "scope": r.scope_text, "disabled": r.disabled,
                       "recorded": r.recorded, "failures": r.failures} for r in recs],
    }


def close_all() -> None:
    """Close every recorder and forget them (tests; a host shutting down)."""
    with _RECORDERS_LOCK:
        recs = list(_RECORDERS.values())
        _RECORDERS.clear()
    for rec in recs:
        rec.close()
