"""Every execute() is an observed transition in awm -- optional, private, never in the way.

awm is a sibling brick, not a dependency: the tests that need it put the sibling's
source on sys.path and are skipped where it is not there. The tests that prove
awrepl works WITHOUT awm need nothing.
"""

import dataclasses
import json
import logging
import sqlite3
import sys
import time
from pathlib import Path

import pytest
from awrepl import ExecResult, ReplSession, transitions

AWM_SRC = Path(__file__).resolve().parents[2] / "awm"
SCOPE = "t:u:awrepl"
ORIGINAL_FIELDS = ["stdout", "stderr", "value", "exception", "traceback", "duration_ms",
                   "truncated", "truncated_bytes"]


@pytest.fixture
def env(tmp_path, monkeypatch):
    """An environment that names an awm scope and file; recorders closed afterwards."""
    db = tmp_path / "wm" / "awm_world.db"
    monkeypatch.setenv(transitions.ENV_SCOPE, "t:u:{agent}")
    monkeypatch.setenv(transitions.ENV_DB, str(db))
    monkeypatch.setenv(transitions.ENV_BUDGET_MS, "20000")
    monkeypatch.delenv(transitions.ENV_FLAG, raising=False)
    monkeypatch.delenv(transitions.ENV_AGENT, raising=False)
    yield db
    transitions.close_all()


@pytest.fixture
def awm(env, monkeypatch):
    if not (AWM_SRC / "awm" / "world.py").exists():
        pytest.importorskip("awm.world")
    else:
        monkeypatch.syspath_prepend(str(AWM_SRC))
    import awm as awm_mod
    return awm_mod


def rows(awm_mod, db, scope=SCOPE):
    """What the store holds, read back through awm after the recorder let go of it."""
    transitions.close_all()
    store = awm_mod.MemoryStore(db, create=False)
    try:
        return store.transitions(awm_mod.Scope.parse(scope))
    finally:
        store.close()


def raw_store_text(db):
    """Every byte on disk plus every cell of every table, as one lowercase string."""
    transitions.close_all()
    blob = b"".join(p.read_bytes() for p in sorted(db.parent.iterdir()) if p.is_file())
    con = sqlite3.connect(str(db))
    cells = []
    try:
        for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            for row in con.execute('SELECT * FROM "%s"' % name):
                cells.extend(str(c) for c in row)
    finally:
        con.close()
    return (blob.decode("utf-8", "replace") + "\n" + "\n".join(cells)).lower()


# ------------------------------------------------------------ the action token
class TestActionToken:
    def test_shape_not_text(self):
        code = ("import os.path, json\nfrom collections import OrderedDict\n"
                "x = foo(1)\ny.bar('ZQLITERAL')  # ZQCOMMENT\n")
        token = transitions.action_token(code)
        assert token == ("py:k=Assign,Expr,Import,ImportFrom|i=collections,json,os"
                         "|c=.bar,foo|s=" + transitions.size_bucket(len(code)))
        assert "ZQ" not in token

    def test_same_shape_different_literals_is_one_action(self):
        a = transitions.action_token("total = compute('alpha', 1)")
        b = transitions.action_token("total = compute('omega', 2)")
        assert a == b
        assert a != transitions.action_token("total = other('alpha', 1)")

    def test_syntax_error_has_its_own_token(self):
        assert transitions.action_token("if True") == "py:syntax-error"
        assert transitions.action_token("x = (") == "py:syntax-error"
        assert transitions.action_token("x = 1").startswith("py:k=Assign|")

    def test_called_names_are_capped_at_20(self):
        code = "\n".join("f%02d()" % i for i in range(40))
        calls = transitions.action_token(code).split("|c=")[1].split("|")[0].split(",")
        assert calls == ["f%02d" % i for i in range(20)]

    def test_size_bucket_is_part_of_the_action(self):
        small = transitions.action_token("f()")
        big = transitions.action_token("f()\n" * 2000)
        assert small.endswith("|s=xs") and big.endswith("|s=l") and small[:-2] == big[:-1]

    def test_unparsed_code_is_bounded(self):
        token = transitions.action_token("x = 1\n" * 60000)  # past MAX_PARSE_CHARS
        assert token == "py:unparsed|s=xl"

    def test_outcome_is_a_class_never_a_message(self):
        out = transitions.outcome_token
        assert out(None) == "ok"
        assert out("ZeroDivisionError: division by zero") == "error:ZeroDivisionError"
        assert out("JSON decode error: Expecting value") == "error:Unknown"
        assert out("TimeoutError: execution exceeded", timed_out=True) == "timeout"
        assert out("TimeoutError: raised by the user's own code") == "error:TimeoutError"


# ------------------------------------------------------------ prediction and recall
class TestPredictionAndSurprise:
    def test_same_failing_action_twice_is_none_then_recalled(self, awm, env, monkeypatch):
        monkeypatch.setattr(transitions, "duration_bucket", lambda ms: "<10ms")
        with ReplSession("t-recall") as session:
            first = session.execute("1/0")
            second = session.execute("1/0")
        assert first.prediction["source"] == "NONE"
        assert first.prediction["outcome"] is None and first.surprise is None
        assert first.transition["outcome"] == "error:ZeroDivisionError"
        assert first.transition["recorded"] is True
        assert second.prediction["source"] == "RECALLED"
        assert second.prediction["outcome"] == "error:ZeroDivisionError"
        assert second.prediction["support"] == 1 and second.prediction["confidence"] == 1.0
        assert second.surprise == 0.0
        got = rows(awm, env)
        assert [t.action for t in got] == [transitions.action_token("1/0")] * 2
        assert [t.source for t in got] == ["awrepl", "awrepl"]
        assert all(t.delta["awrepl.outcome"] == "error:ZeroDivisionError" for t in got)
        assert all(t.delta["awrepl.duration"] == "<10ms" for t in got)
        assert got[0].state_digest == got[1].state_digest
        assert got[0].surprise is None and got[1].surprise == 0.0

    def test_a_wrong_expectation_is_a_surprise(self, awm, env, monkeypatch):
        monkeypatch.setattr(transitions, "duration_bucket", lambda ms: "<10ms")
        with ReplSession("t-surprise") as session:
            session.execute("x = 0")
            session.execute("y = int(1 / x)")          # error:ZeroDivisionError
            session.execute("x = 1")              # same types, same names -> same state
            again = session.execute("y = int(1 / x)")  # RECALLED an error, got ok (+ new var)
        assert again.prediction["source"] == "RECALLED"
        assert again.prediction["outcome"] == "error:ZeroDivisionError"
        assert again.transition["outcome"] == "ok"
        assert again.surprise is not None and again.surprise > 0.0

    def test_state_is_names_and_types_never_values(self, awm, env):
        with ReplSession("t-state") as session:
            session.execute("x = 1")
            session.execute("x.missing")                   # AttributeError, state {x:int}
            session.execute("x = 2")                       # another VALUE: same state
            same = session.execute("x.missing")
            session.execute("del x")
            session.execute("y = 3")                       # another NAME: another state
            renamed = session.execute("y.missing")
        assert same.prediction["source"] == "RECALLED"
        assert renamed.prediction["source"] == "GENERALIZED"
        assert renamed.prediction["outcome"] == "error:AttributeError"

    def test_reset_returns_the_state_to_empty(self, awm, env):
        with ReplSession("t-reset") as session:
            session.execute("1/0")
            session.execute("x = 1")
            session.reset()
            after = session.execute("1/0")
        assert after.prediction["source"] == "RECALLED"

    def test_timeout_is_recorded_and_marks_the_restart(self, awm, env):
        with ReplSession("t-timeout") as session:
            session.execute("x = 41")
            slow = session.execute("while True:\n    pass", timeout_ms=300)
            assert slow.exception.startswith("TimeoutError") and session.restarts == 1
            assert slow.transition == {"action": transitions.action_token("while True:\n    pass"),
                                       "outcome": "timeout", "duration": "<10s",
                                       "recorded": True}
            nxt = session.execute("1 + 1")
        assert nxt.transition["outcome"] == "ok"
        got = rows(awm, env)
        assert [t.delta["awrepl.outcome"] for t in got] == ["ok", "timeout", "ok"]
        assert got[1].delta["awrepl.restarted"] == "1"
        assert got[1].delta["awrepl.vars"] == transitions.EMPTY_SIG
        assert got[2].delta["awrepl.restarted"] == "0"  # the next state said "just restarted"

    def test_a_worker_that_dies_is_recorded_and_still_raises(self, awm, env):
        session = ReplSession("t-died")
        try:
            with pytest.raises(RuntimeError):
                session.execute("import os\nos._exit(3)")
        finally:
            session.close()
        got = rows(awm, env)
        assert [t.delta["awrepl.outcome"] for t in got] == ["error:WorkerDied"]

    def test_sessions_share_the_scope_the_environment_names(self, awm, env, monkeypatch):
        with ReplSession("a") as one:
            one.execute("1/0")
        with ReplSession("b") as two:
            assert two.execute("1/0").prediction["source"] == "RECALLED"
        monkeypatch.setenv(transitions.ENV_AGENT, "agent.atlas")
        with ReplSession("c") as three:
            assert three.execute("1/0").prediction["source"] == "NONE"  # a sibling scope
        assert len(rows(awm, env, "t:u:agent.atlas")) == 1
        assert len(rows(awm, env)) == 2


# ------------------------------------------------------------ privacy
class TestNothingPrivateIsWritten:
    def test_no_code_text_values_names_or_output_reach_the_store(self, awm, env):
        code = (
            "import sys\n"
            "zq_private_name = 'ZQVALUE_9931'  # ZQCOMMENT_13\n"
            "print('ZQSTDOUT_77')\n"
            "sys.stderr.write('ZQSTDERR_55')\n"
            "raise ValueError('ZQEXC_42 ' + zq_private_name)\n"
        )
        with ReplSession("t-private") as session:
            first = session.execute(code)
            session.execute("zq_other = {'ZQKEY_1': 'ZQVALUE_2'}")
            session.execute("zq_other['ZQKEY_404']")
            session.execute("def broken(:\n    'ZQSYNTAX_8'")
            shown = session.execute("print(zq_other)\nsys.stderr.write('ZQSTDERR_66')")
        assert "ZQEXC_42 ZQVALUE_9931" in first.exception  # it really ran, and really leaks
        assert "ZQVALUE_2" in shown.stdout and "ZQSTDERR_66" in shown.stderr
        assert first.transition["outcome"] == "error:ValueError"
        text = raw_store_text(env)
        assert "awrepl.outcome" in text and "error:valueerror" in text  # the store was read
        assert "error:keyerror" in text and "py:syntax-error" in text
        for marker in ("zqvalue", "zqcomment", "zqstdout", "zqstderr", "zqexc", "zqkey",
                       "zqsyntax", "zq_private", "zq_other", "private_name",
                       "raise valueerror", "print(", "sys.stderr"):
            assert marker not in text, marker
        for result in (first, shown):
            exposed = json.dumps([result.prediction, result.surprise, result.transition])
            assert "ZQ" not in exposed and "zq" not in exposed

    def test_rows_are_small_and_bounded(self, awm, env):
        defs = "\n".join("v%03d = %d" % (i, i) for i in range(300))
        kinds = "\n".join("class K%02d: pass\nk%02d = K%02d()" % (i, i, i) for i in range(40))
        with ReplSession("t-bounded") as session:
            session.execute(defs)
            session.execute(kinds)
        for t in rows(awm, env):
            assert len(t.action) < 2100 and len(json.dumps(t.delta)) < 1000
            assert len(t.delta["awrepl.types"].split(",")) <= transitions.MAX_TYPES


# ------------------------------------------------------------ off means off
class _Tap:
    """Stands in for the worker's stdin and keeps every line written to it."""

    def __init__(self, real):
        self.real, self.lines = real, []

    def write(self, text):
        self.lines.append(text)
        return self.real.write(text)

    def __getattr__(self, name):
        return getattr(self.real, name)


def _run_tapped(code):
    session = ReplSession("t-tap")
    try:
        tap = session._process.stdin = _Tap(session._process.stdin)
        return session, session.execute(code), tap.lines
    finally:
        session.close()


class TestFlagOff:
    CODE = "x = 2\nprint(x * 21)"
    REQUEST = json.dumps({"action": "execute", "code": CODE, "timeout_ms": 30000,
                          "max_output_bytes": 65536}) + "\n"

    def test_fields_keep_their_order_and_repr(self):
        names = [f.name for f in dataclasses.fields(ExecResult)]
        assert names[:8] == ORIGINAL_FIELDS
        assert names[8:] == ["prediction", "surprise", "transition"]

    def test_flag_off_is_the_previous_behaviour_byte_for_byte(self, env, monkeypatch):
        monkeypatch.setenv(transitions.ENV_FLAG, "0")
        session, result, lines = _run_tapped(self.CODE)
        assert session._recorder is None
        assert lines[0] == self.REQUEST  # exactly the request the worker always got
        assert (result.stdout, result.stderr, result.value, result.exception, result.traceback,
                result.truncated, result.truncated_bytes) == ("42\n", "", None, None, "", False, 0)
        assert (result.prediction, result.surprise, result.transition) == (None, None, None)
        text = repr(dataclasses.replace(result, duration_ms=0.0))
        assert text == ("ExecResult(stdout='42\\n', stderr='', value=None, exception=None, "
                        "traceback='', duration_ms=0.0, truncated=False, truncated_bytes=0)")
        assert not env.exists() and not env.parent.exists()  # nothing was opened or created
        assert transitions.status()["recorders"] == []

    def test_recording_changes_no_existing_field(self, awm, env, monkeypatch):
        on_session, on, on_lines = _run_tapped(self.CODE)
        assert on_session._recorder is not None and on.transition["recorded"] is True
        assert json.loads(on_lines[0]) == dict(json.loads(self.REQUEST), shape=True)
        monkeypatch.setenv(transitions.ENV_FLAG, "0")
        _, off, _ = _run_tapped(self.CODE)
        assert dataclasses.replace(on, duration_ms=0.0) == dataclasses.replace(off, duration_ms=0.0)
        assert repr(dataclasses.replace(on, duration_ms=0.0)) == \
            repr(dataclasses.replace(off, duration_ms=0.0))

    def test_no_scope_in_the_environment_records_nothing(self, env, monkeypatch):
        monkeypatch.delenv(transitions.ENV_SCOPE)
        session, result, lines = _run_tapped(self.CODE)
        assert session._recorder is None and lines[0] == self.REQUEST
        assert result.transition is None and not env.parent.exists()
        assert transitions.ENV_SCOPE in transitions.status()["reason"]

    def test_a_scope_is_never_invented_or_pooled(self, env):
        resolve = transitions.resolve_target
        base = {transitions.ENV_DB: str(env)}
        assert resolve(base)[0] is None
        assert resolve(dict(base, **{transitions.ENV_SCOPE: "acme:ops:shared"}))[0] is None
        assert resolve(dict(base, **{transitions.ENV_SCOPE: "acme:{agent}"}))[0] is None
        bad = dict(base, **{transitions.ENV_SCOPE: "a:b:{agent}", transitions.ENV_AGENT: "x:y"})
        assert resolve(bad)[0] is None
        ok = dict(base, **{transitions.ENV_SCOPE: "acme:ops:{agent}"})
        assert resolve(ok)[0] == (env, "acme:ops:awrepl")
        assert resolve(dict(ok, **{transitions.ENV_FLAG: "0"}))[0] is None
        root = {transitions.ENV_SCOPE: "acme:ops:{agent}", transitions.ENV_ROOT: "/somewhere/wm"}
        assert resolve(root)[0] == (Path("/somewhere/wm/awm_world.db"), "acme:ops:awrepl")


# ------------------------------------------------------------ awm is optional
def _disabled(rec, wait_s=10.0):
    """The recorder's reason for being off, waiting for its thread to reach one."""
    deadline = time.monotonic() + wait_s
    while rec.disabled is None and time.monotonic() < deadline:
        time.sleep(0.01)
    return rec.disabled or ""


def _exercise(session):
    """A few calls whose results must be right whatever awm does."""
    a = session.execute("x = 20")
    b = session.execute("x + 22")
    c = session.execute("1/0")
    d = session.execute("print(x)")
    assert a.exception is None and "42" in (b.value or "")
    assert c.exception.startswith("ZeroDivisionError") and d.stdout == "20\n"
    return [a, b, c, d]


class TestAwmIsOptional:
    def test_awm_missing_works(self, env, monkeypatch, caplog):
        monkeypatch.setitem(sys.modules, "awm", None)  # `import awm` raises ImportError
        with caplog.at_level(logging.WARNING, logger="awrepl.transitions"):
            with ReplSession("t-missing") as session:
                results = _exercise(session)
                rec = session._recorder
        assert rec is not None and _disabled(rec).startswith("awm unavailable")
        assert all(r.prediction is None and r.surprise is None for r in results)
        assert all(r.transition is None or r.transition["recorded"] is False for r in results)
        assert not env.exists()
        assert len([r for r in caplog.records if "recording is off" in r.getMessage()]) == 1

    def test_awm_raising_works_and_switches_itself_off(self, awm, env, monkeypatch, caplog):
        def boom(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")
        monkeypatch.setattr(awm.MemoryStore, "predict_outcome", boom)
        monkeypatch.setattr(awm.MemoryStore, "observe_transition", boom)
        with caplog.at_level(logging.WARNING, logger="awrepl.transitions"):
            with ReplSession("t-raising") as session:
                results = _exercise(session) + _exercise(session)
                rec = session._recorder
        assert rec.disabled is not None and "failures in a row" in rec.disabled
        assert rec.failures == transitions.MAX_CONSECUTIVE_FAILURES and rec.recorded == 0
        assert all(r.prediction is None and r.surprise is None for r in results)
        assert results[0].transition["recorded"] is False and results[-1].transition is None
        assert len([r for r in caplog.records if "recording is off" in r.getMessage()]) == 1
        assert rows(awm, env) == []

    def test_a_slow_awm_never_holds_a_call_past_its_budget(self, awm, env, monkeypatch):
        def stuck(*args, **kwargs):
            time.sleep(1.5)
        monkeypatch.setattr(awm.MemoryStore, "predict_outcome", stuck)
        monkeypatch.setattr(awm.MemoryStore, "observe_transition", stuck)
        monkeypatch.setenv(transitions.ENV_BUDGET_MS, "50")
        with ReplSession("t-slow") as session:
            session._recorder._thread.join(0.5)  # let the store open before timing calls
            t0 = time.monotonic()
            results = _exercise(session) + _exercise(session)
            waited = time.monotonic() - t0
            assert session._recorder.disabled is not None
        assert waited < 1.4, "8 calls waited %.2f s on a store that sleeps 1.5 s a call" % waited
        assert all(r.prediction is None for r in results)

    def test_an_unmigrated_file_is_left_alone(self, awm, env, monkeypatch):
        monkeypatch.setattr(awm.MemoryStore, "compat", property(lambda self: True))
        with ReplSession("t-compat") as session:
            _exercise(session)
            assert "needs migrating" in _disabled(session._recorder)

    def test_a_recorder_that_raises_never_reaches_the_caller(self, env):
        class Broken:
            disabled = None

            def predict(self, *args, **kwargs):
                raise RuntimeError("recorder bug")

            observe = predict

        with ReplSession("t-broken") as session:
            session._recorder = Broken()
            results = _exercise(session)
        assert all(r.prediction is None and r.transition is None for r in results)

    def test_a_worker_reporting_a_strange_shape_is_bounded_and_sanitised(self):
        sig = "0" * 16
        many = transitions.clean_shape({"sig": sig, "types": ["T%02d" % i for i in range(50)]})
        assert many["types"] == ["T%02d" % i for i in range(12)] and many["more"] is True
        odd = transitions.clean_shape({"sig": sig, "types": ["int", "any text at all", 7]})
        assert odd == {"sig": sig, "types": ["?", "int"], "more": False}
        assert transitions.clean_shape({"sig": "not hex", "types": []}) is None
        assert transitions.clean_shape("x = 1") is None
        slots = transitions.state_slots(many, 3, False)
        assert slots["awrepl.types"].endswith("T11+") and slots["awrepl.calls"] == "0-9"
        assert transitions.state_slots(None, 250, True) == {
            "awrepl.vars": transitions.EMPTY_SIG, "awrepl.types": "", "awrepl.calls": "100+",
            "awrepl.restarted": "1"}

    def test_a_recorder_that_fails_after_the_call_never_reaches_the_caller(self, env):
        class BrokenLate:
            disabled = None

            def predict(self, *args, **kwargs):
                return None

            def observe(self, *args, **kwargs):
                raise RuntimeError("recorder bug")

        with ReplSession("t-broken-late") as session:
            session._recorder = BrokenLate()
            results = _exercise(session)
        assert all(r.prediction is None and r.transition is None for r in results)
