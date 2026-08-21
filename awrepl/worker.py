"""
Worker process for executing Python code in a persistent namespace.

This module is run as a subprocess and communicates via stdin/stdout using JSON.
It maintains a single persistent namespace across multiple execute() calls.
"""

import json
import sys
import traceback
from typing import Any, Dict


class PersistentNamespace:
    """Maintains a persistent namespace for code execution."""

    def __init__(self) -> None:
        self.namespace: Dict[str, Any] = {}
        self.namespace["__builtins__"] = __builtins__

    def execute(
        self, code: str, timeout_ms: int, max_output_bytes: int
    ) -> Dict[str, Any]:
        """Execute code and return result as dict."""
        result = {
            "stdout": "",
            "stderr": "",
            "value": None,
            "exception": None,
            "traceback": "",
            "truncated": False,
            "truncated_bytes": 0,
        }

        try:
            # Capture output by redirecting stdout/stderr
            import io

            old_stdout = sys.stdout
            old_stderr = sys.stderr
            stdout_capture = io.StringIO()
            stderr_capture = io.StringIO()

            try:
                sys.stdout = stdout_capture
                sys.stderr = stderr_capture

                # Try to compile as eval (for bare expressions like "42")
                # If that fails, compile as exec (for statements)
                try:
                    compiled = compile(code, "<string>", "eval")
                    last_value = eval(compiled, self.namespace)
                    result["value"] = repr(last_value)[:1000]
                except SyntaxError:
                    # Not a bare expression, execute as statement
                    compiled = compile(code, "<string>", "exec")
                    exec(compiled, self.namespace)

            finally:
                sys.stdout = old_stdout
                sys.stderr = old_stderr

            # Capture output
            captured_stdout = stdout_capture.getvalue()
            captured_stderr = stderr_capture.getvalue()

            # Truncate if needed
            total_output = captured_stdout + captured_stderr
            if len(total_output) > max_output_bytes:
                truncated_bytes = len(total_output) - max_output_bytes
                result["stdout"] = captured_stdout[: max_output_bytes // 2]
                result["stderr"] = captured_stderr[: max_output_bytes // 2]
                result["truncated"] = True
                result["truncated_bytes"] = truncated_bytes
            else:
                result["stdout"] = captured_stdout
                result["stderr"] = captured_stderr

        except SyntaxError as e:
            result["exception"] = f"SyntaxError: {e.msg}"
            result["traceback"] = traceback.format_exc()
        except Exception as e:
            result["exception"] = f"{type(e).__name__}: {str(e)}"
            result["traceback"] = traceback.format_exc()

        return result

    def get_variables(self) -> Dict[str, str]:
        """Return dict of {name: type_and_repr} for all bound variables."""
        variables = {}
        for name, value in self.namespace.items():
            if name.startswith("__"):
                continue
            try:
                type_str = type(value).__name__
                # Get a short repr
                repr_str = repr(value)
                if len(repr_str) > 100:
                    repr_str = repr_str[:97] + "..."
                variables[name] = f"{type_str}: {repr_str}"
            except Exception:
                variables[name] = f"{type(value).__name__}: <repr failed>"
        return variables

    def inspect(self, name: str) -> Dict[str, Any]:
        """Return detailed info about a variable."""
        if name not in self.namespace:
            return {"error": f"Variable '{name}' not found"}

        value = self.namespace[name]
        info = {
            "name": name,
            "type": type(value).__name__,
            "repr": repr(value)[:500],
        }

        try:
            info["dir"] = [x for x in dir(value) if not x.startswith("_")][:20]
        except Exception:
            pass

        try:
            info["docstring"] = value.__doc__[:200] if value.__doc__ else None
        except Exception:
            pass

        try:
            if hasattr(value, "__len__"):
                info["len"] = len(value)
        except Exception:
            pass

        return info

    def reset(self) -> None:
        """Clear all user variables (keep builtins)."""
        self.namespace = {"__builtins__": __builtins__}


def main() -> None:
    """Main worker loop: read JSON commands, execute, return results."""
    ns = PersistentNamespace()

    try:
        for line in sys.stdin:
            if not line.strip():
                continue

            try:
                request = json.loads(line)
                action = request.get("action")

                if action == "execute":
                    code = request.get("code", "")
                    timeout_ms = request.get("timeout_ms", 30000)
                    max_output_bytes = request.get("max_output_bytes", 65536)
                    result = ns.execute(code, timeout_ms, max_output_bytes)
                    sys.stdout.write(json.dumps(result) + "\n")
                    sys.stdout.flush()

                elif action == "variables":
                    variables = ns.get_variables()
                    sys.stdout.write(json.dumps({"variables": variables}) + "\n")
                    sys.stdout.flush()

                elif action == "inspect":
                    name = request.get("name", "")
                    info = ns.inspect(name)
                    sys.stdout.write(json.dumps(info) + "\n")
                    sys.stdout.flush()

                elif action == "reset":
                    ns.reset()
                    sys.stdout.write(json.dumps({"status": "ok"}) + "\n")
                    sys.stdout.flush()

                elif action == "quit":
                    break

                else:
                    sys.stdout.write(
                        json.dumps({"error": f"Unknown action: {action}"}) + "\n"
                    )
                    sys.stdout.flush()

            except json.JSONDecodeError as e:
                sys.stdout.write(json.dumps({"error": f"JSON decode error: {e}"}) + "\n")
                sys.stdout.flush()
            except Exception as e:
                sys.stdout.write(
                    json.dumps({"error": f"Unexpected error: {e}"}) + "\n"
                )
                sys.stdout.flush()

    except KeyboardInterrupt:
        pass
    except EOFError:
        pass


if __name__ == "__main__":
    main()
