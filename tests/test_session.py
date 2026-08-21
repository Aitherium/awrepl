"""
Tests for ReplSession - core execution and state persistence.
"""

from awrepl import ExecResult, ReplSession


class TestBasicExecution:
    """Test basic code execution."""

    def test_simple_code_execution(self):
        """Execute simple code and capture output."""
        session = ReplSession("test")
        result = session.execute("print('hello')")
        assert result.stdout == "hello\n"
        assert result.exception is None
        session.close()

    def test_last_expression_value(self):
        """Capture the value of the last expression."""
        session = ReplSession("test")
        result = session.execute("42")
        assert "42" in (result.value or "")
        session.close()

    def test_syntax_error(self):
        """Handle syntax errors gracefully."""
        session = ReplSession("test")
        result = session.execute("if True")
        assert result.exception is not None
        assert "SyntaxError" in result.exception
        session.close()

    def test_runtime_error(self):
        """Handle runtime errors gracefully."""
        session = ReplSession("test")
        result = session.execute("x = 1 / 0")
        assert result.exception is not None
        assert "ZeroDivisionError" in result.exception
        session.close()


class TestStatePersistence:
    """Test that state persists across multiple execute() calls."""

    def test_variable_persists(self):
        """A variable defined in one call is accessible in another."""
        session = ReplSession("test")
        session.execute("x = 42")
        result = session.execute("print(x)")
        assert "42" in result.stdout
        session.close()

    def test_function_persists(self):
        """A function defined in one call is callable in another."""
        session = ReplSession("test")
        session.execute("def double(n):\n    return n * 2")
        result = session.execute("print(double(21))")
        assert "42" in result.stdout
        session.close()

    def test_import_persists(self):
        """An import in one call is accessible in another."""
        session = ReplSession("test")
        session.execute("import math")
        result = session.execute("print(math.pi)")
        assert "3.14" in result.stdout
        session.close()

    def test_multiple_statements(self):
        """Multiple statements build on each other."""
        session = ReplSession("test")
        session.execute("items = []")
        session.execute("items.append(1)")
        session.execute("items.append(2)")
        result = session.execute("print(len(items))")
        assert "2" in result.stdout
        session.close()

    def test_exception_doesnt_kill_session(self):
        """After an exception, the session still works."""
        session = ReplSession("test")
        session.execute("x = 10")
        session.execute("y = 1 / 0")  # This raises
        result = session.execute("print(x)")
        assert "10" in result.stdout
        assert result.exception is None
        session.close()


class TestOutputCapture:
    """Test stdout/stderr capture."""

    def test_stdout_capture(self):
        """Capture stdout."""
        session = ReplSession("test")
        result = session.execute("print('test output')")
        assert result.stdout == "test output\n"
        session.close()

    def test_stderr_capture(self):
        """Capture stderr."""
        session = ReplSession("test")
        result = session.execute("import sys; sys.stderr.write('error\\n')")
        assert result.stderr == "error\n"
        session.close()

    def test_mixed_output(self):
        """Capture both stdout and stderr."""
        session = ReplSession("test")
        result = session.execute(
            "import sys; print('out'); sys.stderr.write('err\\n')"
        )
        assert "out" in result.stdout
        assert "err" in result.stderr
        session.close()

    def test_output_truncation(self):
        """Large output is truncated with a flag."""
        session = ReplSession("test", max_output_bytes=50)
        big_output = "x" * 1000
        result = session.execute(f"print('{big_output}')")
        assert result.truncated is True
        assert result.truncated_bytes > 0
        assert len(result.stdout) <= 50
        session.close()


class TestVariables:
    """Test variables() and inspect() methods."""

    def test_variables_lists_bound_names(self):
        """variables() returns all bound variable names."""
        session = ReplSession("test")
        session.execute("foo = 'bar'")
        session.execute("baz = [1, 2, 3]")
        vars_dict = session.variables()
        assert "foo" in vars_dict
        assert "baz" in vars_dict
        session.close()

    def test_variables_excludes_builtins(self):
        """variables() does not include __builtins__."""
        session = ReplSession("test")
        session.execute("x = 1")
        vars_dict = session.variables()
        assert "__builtins__" not in vars_dict
        session.close()

    def test_inspect_returns_type_and_repr(self):
        """inspect() returns type and repr of a variable."""
        session = ReplSession("test")
        session.execute("x = [1, 2, 3]")
        info = session.inspect("x")
        assert "type" in info
        assert info["type"] == "list"
        assert "repr" in info
        session.close()

    def test_inspect_nonexistent_variable(self):
        """inspect() handles nonexistent variables."""
        session = ReplSession("test")
        info = session.inspect("does_not_exist")
        assert "error" in info
        session.close()

    def test_inspect_includes_docstring(self):
        """inspect() includes docstring if available."""
        session = ReplSession("test")
        session.execute(
            "def my_func():\n    '''This is a docstring'''\n    pass"
        )
        info = session.inspect("my_func")
        assert "docstring" in info
        session.close()


class TestReset:
    """Test reset() method."""

    def test_reset_clears_variables(self):
        """reset() clears all user-defined variables."""
        session = ReplSession("test")
        session.execute("x = 42")
        session.execute("y = 'hello'")
        session.reset()
        vars_dict = session.variables()
        assert "x" not in vars_dict
        assert "y" not in vars_dict
        session.close()

    def test_reset_keeps_builtins(self):
        """reset() keeps builtins available."""
        session = ReplSession("test")
        session.execute("x = 42")
        session.reset()
        result = session.execute("print(len([1, 2, 3]))")
        assert "3" in result.stdout
        session.close()


class TestContextManager:
    """Test context manager support."""

    def test_context_manager_closes_session(self):
        """Session can be used as a context manager."""
        with ReplSession("test") as session:
            session.execute("x = 42")
            result = session.execute("print(x)")
            assert "42" in result.stdout
        # Session should be closed after exiting context


class TestExecResult:
    """Test ExecResult dataclass."""

    def test_exec_result_fields(self):
        """ExecResult has all expected fields."""
        session = ReplSession("test")
        result = session.execute("x = 1")
        assert isinstance(result, ExecResult)
        assert hasattr(result, "stdout")
        assert hasattr(result, "stderr")
        assert hasattr(result, "value")
        assert hasattr(result, "exception")
        assert hasattr(result, "traceback")
        assert hasattr(result, "duration_ms")
        assert hasattr(result, "truncated")
        assert hasattr(result, "truncated_bytes")
        session.close()

    def test_exec_result_success(self):
        """ExecResult reflects successful execution."""
        session = ReplSession("test")
        result = session.execute("x = 42")
        assert result.exception is None
        assert isinstance(result.duration_ms, float)
        assert result.truncated is False
        session.close()

    def test_exec_result_failure(self):
        """ExecResult reflects failed execution."""
        session = ReplSession("test")
        result = session.execute("raise ValueError('test')")
        assert result.exception is not None
        assert "ValueError" in result.exception
        session.close()
