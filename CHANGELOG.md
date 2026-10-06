# Changelog

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

