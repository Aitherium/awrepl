# Changelog

## Unreleased

- Every `execute()` can be recorded as an observed transition in an
  [awm](https://github.com/Aitherium/awm) world model (`awrepl/transitions.py`):
  a value-free state digest, an action token derived from the code's AST shape,
  and `ok` / `error:<Class>` / `timeout` plus a duration bucket. awm's prediction
  (asked before the call) and the surprise are exposed as three additive
  `ExecResult` fields: `prediction`, `surprise`, `transition` (kept out of `repr`
  and `==`). No code text, value, variable name, message or output is stored.
- awm is optional and recording never fails or holds a call: one daemon thread, a
  250 ms budget per awm call, off for the process after three failures in a row.
- `AWREPL_RECORD_TRANSITIONS` (default `1`; `0` = the exact previous behaviour).
  Records only where `AITHER_AGENT_WM_AWM_SCOPE` names an awm scope template;
  `{agent}` is filled from `AWREPL_TRANSITIONS_AGENT` (default `awrepl`).
- The worker answers an `execute` request carrying `"shape": true` with the
  namespace's shape (a digest and type names). Requests without it are unchanged.

## 0.1.1 — 2026-10-05

- Fixed `SessionPool.delete_session`/`close_all` holding the pool lock across
  `session.close()` — a long `execute()` stalled unrelated pool calls for its
  full duration (measured: a 3 s execute stalled a concurrent delete ~2.5 s,
  and an unrelated `list_sessions()` through the pool lock with it).
- Fixed `ReplSession.close()` racing an in-flight `execute()`: the caller
  could hit `AttributeError: 'NoneType' object has no attribute 'stdin'` or
  `OSError(22, 'Invalid argument')` (Windows, writing into a dead worker's
  stdin) instead of the dead-session `RuntimeError`. All call paths now pin
  the process under the lock and map the send failure to the dead-session
  error; `close()` is idempotent and never waits on the execute lock.
- Added concurrency tests pinning both regressions: delete/list not blocked
  behind execute, and close-racing-execute / close-of-dead-worker cleanliness.
- Contract notes for callers: `delete_session` is no longer atomic with
  respect to `get_session` — a handle obtained just before the pop may be
  closed underneath its holder, which must handle the dead-session
  `RuntimeError`; `close_all` is best-effort teardown, not a barrier —
  sessions created concurrently after the map is cleared survive it.

## 0.1.0 — 2026-08-19

Initial public release.

