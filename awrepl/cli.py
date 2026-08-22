"""
Command-line interface for awrepl.

Provides:
  - awrepl run "code"                # Execute code once
  - awrepl serve                      # Start interactive session
  - awrepl --session ID run "code"   # Use specific session from pool
  - awrepl --self-test               # Verify the REPL contract
"""

import argparse
import json
import sys

from .pool import SessionPool
from .session import ReplSession


def cmd_run(args: argparse.Namespace, pool: SessionPool) -> int:
    """Execute code once (ephemeral session) or in a pool session."""
    if args.session:
        try:
            session = pool.get_session(args.session)
        except KeyError:
            session_id = pool.create_session(args.session)
            session = pool.get_session(session_id)
    else:
        # Ephemeral session
        session = ReplSession("ephemeral")

    try:
        result = session.execute(args.code)

        if args.json:
            output = {
                "stdout": result.stdout,
                "stderr": result.stderr,
                "value": result.value,
                "exception": result.exception,
                "duration_ms": result.duration_ms,
                "truncated": result.truncated,
            }
            print(json.dumps(output))
        else:
            if result.stdout:
                print(result.stdout, end="")
            if result.stderr:
                print(result.stderr, end="", file=sys.stderr)
            if result.value:
                print(result.value)
            if result.exception:
                print(f"ERROR: {result.exception}", file=sys.stderr)
                if args.traceback and result.traceback:
                    print(result.traceback, file=sys.stderr)

        return 0 if result.exception is None else 1

    finally:
        if not args.session:
            session.close()


def cmd_serve(args: argparse.Namespace, pool: SessionPool) -> int:
    """Interactive REPL session (not fully interactive on stdin, but runnable)."""
    session_id = args.session or "default"
    try:
        session = pool.get_session(session_id)
    except KeyError:
        session_id = pool.create_session(session_id)
        session = pool.get_session(session_id)

    print(f"awrepl REPL [session: {session_id}]")
    print("Type 'exit' to quit, 'help' for info")
    print()

    try:
        while True:
            try:
                prompt = f"awrepl[{session_id}]> "
                code = input(prompt).strip()

                if not code:
                    continue
                if code.lower() in ("exit", "quit"):
                    break
                if code.lower() == "help":
                    print("Commands: exit, quit, vars, inspect <name>")
                    continue
                if code.lower() == "vars":
                    vars_dict = session.variables()
                    for name, info in vars_dict.items():
                        print(f"  {name}: {info}")
                    continue
                if code.lower().startswith("inspect "):
                    name = code[8:].strip()
                    info = session.inspect(name)
                    print(json.dumps(info, indent=2))
                    continue

                result = session.execute(code)
                if result.stdout:
                    print(result.stdout, end="")
                if result.stderr:
                    print(result.stderr, end="", file=sys.stderr)
                if result.value:
                    print(result.value)
                if result.exception:
                    print(f"ERROR: {result.exception}")

            except EOFError:
                break
            except KeyboardInterrupt:
                print()
                continue

    finally:
        if not args.session:
            pool.delete_session(session_id)

    return 0


def cmd_self_test(args: argparse.Namespace) -> int:
    """Verify the REPL contract without network."""
    tests_passed = 0
    tests_total = 0

    # Test 1: Variable persistence across calls
    tests_total += 1
    try:
        session = ReplSession("test")
        result1 = session.execute("x = 42")
        result2 = session.execute("print(x)")
        if "42" in result2.stdout and not result2.exception:
            print("  PASS  Variables persist across calls")
            tests_passed += 1
        else:
            print("  FAIL  Variables persist across calls")
        session.close()
    except Exception as e:
        print(f"  FAIL  Variables persist across calls: {e}")

    # Test 2: Syntax error doesn't kill session
    tests_total += 1
    try:
        session = ReplSession("test2")
        result1 = session.execute("x = 10")
        result2 = session.execute("y = ")  # Syntax error
        result3 = session.execute("print(x)")  # Should still work
        if "SyntaxError" in result2.exception and "10" in result3.stdout:
            print("  PASS  Syntax error doesn't kill session")
            tests_passed += 1
        else:
            print("  FAIL  Syntax error doesn't kill session")
        session.close()
    except Exception as e:
        print(f"  FAIL  Syntax error doesn't kill session: {e}")

    # Test 3: Exception is reported and session survives
    tests_total += 1
    try:
        session = ReplSession("test3")
        result1 = session.execute("x = 1 / 0")  # ZeroDivisionError
        result2 = session.execute("y = 42")  # Should still work
        result3 = session.execute("print(y)")
        if "ZeroDivisionError" in result1.exception and "42" in result3.stdout:
            print("  PASS  Exception doesn't kill session")
            tests_passed += 1
        else:
            print("  FAIL  Exception doesn't kill session")
        session.close()
    except Exception as e:
        print(f"  FAIL  Exception doesn't kill session: {e}")

    # Test 4: Output truncation
    tests_total += 1
    try:
        session = ReplSession("test4", max_output_bytes=100)
        big_str = "x" * 1000
        result = session.execute(f"print('{big_str}')")
        if result.truncated and result.truncated_bytes > 0:
            print("  PASS  Output truncation works")
            tests_passed += 1
        else:
            print("  FAIL  Output truncation works")
        session.close()
    except Exception as e:
        print(f"  FAIL  Output truncation works: {e}")

    # Test 5: Variables() returns bound names
    tests_total += 1
    try:
        session = ReplSession("test5")
        session.execute("foo = 'bar'")
        session.execute("baz = [1, 2, 3]")
        vars_dict = session.variables()
        if "foo" in vars_dict and "baz" in vars_dict:
            print("  PASS  variables() lists bound names")
            tests_passed += 1
        else:
            print("  FAIL  variables() lists bound names")
        session.close()
    except Exception as e:
        print(f"  FAIL  variables() lists bound names: {e}")

    # Test 6: SessionPool isolation
    tests_total += 1
    try:
        pool = SessionPool()
        s1 = pool.create_session("s1")
        s2 = pool.create_session("s2")
        pool.get_session(s1).execute("x = 'session1'")
        pool.get_session(s2).execute("x = 'session2'")
        r1 = pool.get_session(s1).execute("print(x)")
        r2 = pool.get_session(s2).execute("print(x)")
        if "session1" in r1.stdout and "session2" in r2.stdout:
            print("  PASS  Pool sessions are isolated")
            tests_passed += 1
        else:
            print("  FAIL  Pool sessions are isolated")
        pool.close_all()
    except Exception as e:
        print(f"  FAIL  Pool sessions are isolated: {e}")

    print()
    if tests_passed == tests_total:
        print(f"SELF-TEST: awrepl ok ({tests_passed}/{tests_total})")
        return 0
    else:
        print(f"SELF-TEST: FAILED ({tests_passed}/{tests_total})")
        return 1


def main() -> int:
    """Main CLI entry point."""
    # GENERATED doctor intercept (gen_aw_doctor.py) -- do not edit
    _dv = locals().get("argv")
    if (_dv if _dv is not None else __import__("sys").argv[1:])[:1] == ["doctor"]:
        from ._doctor import report
        return report()
    parser = argparse.ArgumentParser(
        description="awrepl - A REPL an agent can actually use"
    )

    parser.add_argument("--session", help="Session ID (for pool-based calls)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--traceback", action="store_true", help="Show full traceback")
    parser.add_argument("--self-test", action="store_true", help="Run self-tests")

    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Execute code once")
    run_parser.add_argument("code", help="Python code to execute")

    subparsers.add_parser("serve", help="Interactive REPL")

    args = parser.parse_args()

    if args.self_test:
        return cmd_self_test(args)

    pool = SessionPool()

    try:
        if args.command == "run":
            return cmd_run(args, pool)
        elif args.command == "serve":
            return cmd_serve(args, pool)
        else:
            parser.print_help()
            return 1
    finally:
        pool.close_all()


if __name__ == "__main__":
    sys.exit(main())
