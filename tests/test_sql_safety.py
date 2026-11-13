"""Static safety scans: no SQL outside app/db.py, and no SQL built from pieces.

These read the source code (they do not run it), using Python's ``ast`` module
so comments and docstrings that merely mention ``execute(`` are not flagged.
"""

import ast
import pathlib

APP_DIR = pathlib.Path(__file__).resolve().parent.parent / "app"
DB_FILE = APP_DIR / "db.py"

EXECUTE_NAMES = {"execute", "executemany", "executescript"}

# The one place db.py is allowed to put a table name into SQL text. count_rows
# checks the name against a hard-coded whitelist first (see its docstring).
ALLOWED_FSTRING_FUNCTIONS = {"count_rows"}


def _calls_to_execute(tree):
    """Yield (enclosing function name, Call node) for every .execute*(...) call."""
    for func in ast.walk(tree):
        if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(func):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in EXECUTE_NAMES
                ):
                    yield func.name, node


def _unsafe_sql_pieces(sql_arg):
    """Describe any f-string, +, % or .format() used to build a SQL argument."""
    problems = []
    for node in ast.walk(sql_arg):
        if isinstance(node, ast.JoinedStr):
            problems.append("f-string")
        elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
            problems.append("+ or % operator")
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "format"
        ):
            problems.append(".format()")
    return problems


def _scan_db_source(source):
    """Return a list of 'function: problem' strings for one source file's text."""
    found = []
    for func_name, call in _calls_to_execute(ast.parse(source)):
        if not call.args:
            continue
        for problem in _unsafe_sql_pieces(call.args[0]):
            if problem == "f-string" and func_name in ALLOWED_FSTRING_FUNCTIONS:
                continue
            found.append(f"{func_name}(): SQL built with {problem}")
    return found


def test_no_sql_outside_the_data_access_module():
    offenders = []
    for path in sorted(APP_DIR.rglob("*.py")):
        if path == DB_FILE:
            continue
        for _func, call in _calls_to_execute(ast.parse(path.read_text(encoding="utf-8"))):
            offenders.append(f"{path.relative_to(APP_DIR.parent)}:{call.lineno}")
    assert offenders == [], f"SQL execution found outside app/db.py: {offenders}"


def test_no_database_driver_imported_outside_the_data_access_module():
    offenders = []
    for path in sorted(APP_DIR.rglob("*.py")):
        if path == DB_FILE:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            if any(n.split(".")[0] in {"pymysql", "sqlite3", "MySQLdb"} for n in names):
                offenders.append(str(path.relative_to(APP_DIR.parent)))
    assert offenders == [], f"database driver imported outside app/db.py: {offenders}"


def test_sql_in_db_py_is_never_built_from_pieces():
    problems = _scan_db_source(DB_FILE.read_text(encoding="utf-8"))
    assert problems == [], problems


def test_the_scanner_really_finds_execute_calls():
    """Guards against the scan silently checking nothing."""
    calls = list(_calls_to_execute(ast.parse(DB_FILE.read_text(encoding="utf-8"))))
    assert len(calls) > 25


def test_the_scanner_catches_unsafe_sql():
    """Self-check: each bad pattern is flagged, a parameterized query is not."""
    bad_snippets = [
        'def f(conn, x):\n    conn.execute(f"SELECT * FROM users WHERE id = {x}")',
        'def f(conn, x):\n    conn.execute("SELECT * FROM users WHERE id = " + x)',
        'def f(conn, x):\n    conn.execute("SELECT * FROM users WHERE id = %s" % x)',
        'def f(conn, x):\n    conn.execute("SELECT * FROM users WHERE id = {}".format(x))',
    ]
    for snippet in bad_snippets:
        assert _scan_db_source(snippet), snippet
    good = 'def f(conn, x):\n    conn.execute("SELECT * FROM users WHERE id = ?", (x,))'
    assert _scan_db_source(good) == []
