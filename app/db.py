"""Seeded DuckDB warehouse (raw → staging → marts) with read-only query and discovery helpers."""

from __future__ import annotations

import hashlib
import itertools
import math
import re
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

import duckdb

SQL_DIR = Path(__file__).resolve().parent / 'warehouse'
SQL_FILES = sorted(SQL_DIR.glob('*.sql'))
# The file name carries a hash of the build SQL, so editing the SQL rebuilds the database automatically.
_BUILD_HASH = hashlib.sha256(b''.join(f.read_bytes() for f in SQL_FILES)).hexdigest()[:10]
DB_PATH = Path(__file__).resolve().parent.parent / 'data' / f'warehouse-{_BUILD_HASH}.duckdb'
MAX_ROWS = 50
SAMPLE_ROWS = 3

_READ_ONLY_START = re.compile(r'^\s*(select|with)\b', re.IGNORECASE)
_YEAR_MONTH = re.compile(r'^\d{4}-\d{2}$')
_TABLE_NAME = re.compile(r'^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$')


def build() -> None:
    """(Re)create the warehouse file from app/warehouse/*.sql, removing stale builds."""
    DB_PATH.parent.mkdir(exist_ok=True)
    for old in DB_PATH.parent.glob('*.duckdb'):
        old.unlink()
    with duckdb.connect(str(DB_PATH)) as con:
        for f in SQL_FILES:
            con.execute(f.read_text())


_con: duckdb.DuckDBPyConnection | None = None


def connection() -> duckdb.DuckDBPyConnection:
    """Shared read-only connection; callers take a cursor per query."""
    global _con
    if _con is None:
        if not DB_PATH.exists():
            build()
        _con = duckdb.connect(str(DB_PATH), read_only=True)
        # Malloy-generated SQL uses DuckDB's older `x -> expr` lambda syntax, which DuckDB 2.x disables by default.
        _con.execute("SET GLOBAL lambda_syntax = 'ENABLE_SINGLE_ARROW'")
    return _con


def list_tables(schema: str | None = None) -> list[dict]:
    """Every table (optionally one schema) with its row count and documentation."""
    rows = connection().cursor().execute(
        """
        SELECT schema_name || '.' || table_name, estimated_size, coalesce(comment, '')
        FROM duckdb_tables()
        WHERE NOT temporary AND database_name = current_database() AND ($schema IS NULL OR schema_name = $schema)
        ORDER BY schema_name, table_name
        """,
        {'schema': schema},
    ).fetchall()
    return [{'table': t, 'rows': n, 'description': d} for t, n, d in rows]


def describe_table(table: str) -> dict:
    """Columns (name, type, description) plus a few sample rows for a schema-qualified table."""
    table = table.strip().lower()
    if not _TABLE_NAME.match(table):
        raise ValueError('Use a schema-qualified name like marts.fct_orders.')
    schema, name = table.split('.')
    cur = connection().cursor()
    meta = cur.execute(
        'SELECT coalesce(comment, \'\') FROM duckdb_tables() WHERE schema_name = ? AND table_name = ?', [schema, name]
    ).fetchone()
    if meta is None:
        raise ValueError(f'Unknown table {table!r}. Call list_tables to see what exists.')
    columns = cur.execute(
        """
        SELECT column_name, data_type, coalesce(comment, '')
        FROM duckdb_columns() WHERE schema_name = ? AND table_name = ? ORDER BY column_index
        """,
        [schema, name],
    ).fetchall()
    return {
        'table': table,
        'description': meta[0],
        'columns': [{'name': c, 'type': t, 'description': d} for c, t, d in columns],
        'sample_rows': run_readonly(f'SELECT * FROM {table} LIMIT {SAMPLE_ROWS}')['rows'],
    }


def schema_ddl(schema: str) -> str:
    """CREATE SCHEMA/TABLE statements (types only, no data) for one schema. Used by the Malloy compiler sidecar."""
    rows = connection().cursor().execute(
        """
        SELECT table_name, column_name, data_type FROM duckdb_columns()
        WHERE schema_name = ? AND database_name = current_database()
        ORDER BY table_name, column_index
        """,
        [schema],
    ).fetchall()
    tables: dict[str, list[str]] = {}
    for table, column, dtype in rows:
        tables.setdefault(table, []).append(f'"{column}" {dtype}')
    return ';\n'.join([f'CREATE SCHEMA {schema}'] + [f'CREATE TABLE {schema}.{t} ({", ".join(c)})' for t, c in tables.items()])


def _read_only(sql: str) -> str:
    """The query, if it is a single SELECT/WITH statement."""
    stripped = sql.strip().rstrip(';').strip()
    if not _READ_ONLY_START.match(stripped) or ';' in stripped:
        raise ValueError('Only a single SELECT or WITH query is allowed.')
    return stripped


def run_readonly(sql: str, max_rows: int = MAX_ROWS) -> dict:
    """Run a single SELECT/WITH query. Returns columns, rows (capped) and total row count."""
    cur = connection().cursor()
    cur.execute(_read_only(sql))
    columns = [d[0] for d in cur.description]
    rows = cur.fetchall()
    return {
        'columns': columns,
        'rows': [[_jsonable(v) for v in row] for row in rows[:max_rows]],
        'row_count': len(rows),
    }


REL_TOL = 0.005  # numbers within 0.5% of each other match


def all_rows(sql: str) -> list[tuple]:
    """Every row of a read-only query, unrounded (Decimals as floats), for comparing results."""
    rows = connection().cursor().execute(_read_only(sql)).fetchmany(10_000)
    return [tuple(float(v) if isinstance(v, Decimal) else v for v in r) for r in rows]


def _is_number(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _cells_equal(a: object, b: object, rel_tol: float) -> bool:
    if _is_number(a) and _is_number(b):
        return math.isclose(a, b, rel_tol=rel_tol, abs_tol=1e-9)
    if a is None or b is None:
        return a is None and b is None
    return _normalize(str(a) if not isinstance(a, (str, bool)) else a) == _normalize(str(b) if not isinstance(b, (str, bool)) else b)


def _same_rows(actual: list[tuple], expected: list[tuple], rel_tol: float) -> bool:
    """Row multisets equal, cell by cell with numeric tolerance (greedy pairing; rows are rarely near-duplicates)."""
    if len(actual) != len(expected):
        return False
    unused = list(actual)
    for row in expected:
        hit = next((i for i, cand in enumerate(unused) if all(_cells_equal(x, y, rel_tol) for x, y in zip(cand, row))), None)
        if hit is None:
            return False
        unused.pop(hit)
    return True


def _numeric_columns(rows: list[Sequence]) -> list[tuple]:
    """Keep only the columns that hold numbers (labels, names, dates and flags dropped)."""
    if not rows:
        return rows
    keep = [i for i in range(len(rows[0])) if all(r[i] is None or _is_number(r[i]) for r in rows)
            and any(_is_number(r[i]) for r in rows)]
    return [tuple(r[i] for i in keep) for r in rows]


def results_match(actual_sql: str, expected_sql: str, mode: str = 'rows') -> bool:
    """rows_match on the results of two queries."""
    return rows_match(all_rows(actual_sql), all_rows(expected_sql), mode)


def rows_match(actual: list[Sequence], expected: list[Sequence], mode: str = 'rows', rel_tol: float = REL_TOL) -> bool:
    """Same rows as the expected ones, ignoring row and column order (Spider-style execution match), with numbers
    equal within rel_tol. Extra or missing columns still fail. mode='numbers' compares only the numeric columns,
    for questions whose labels can legitimately vary (TRUE/FALSE vs 'first'/'repeat')."""
    if mode == 'numbers':
        actual, expected = _numeric_columns(actual), _numeric_columns(expected)
    if not actual or not expected:
        return not actual and not expected
    if len(actual[0]) != len(expected[0]):
        return False
    perms = itertools.permutations(range(len(actual[0]))) if len(actual[0]) <= 6 else [tuple(range(len(actual[0])))]
    return any(_same_rows([tuple(r[i] for i in perm) for r in actual], expected, rel_tol) for perm in perms)


def _normalize(v: object) -> object:
    if isinstance(v, float):
        return round(v, 2)
    if isinstance(v, str) and v.endswith(' 00:00:00'):
        return v[:-9]  # a midnight timestamp and a date are the same day (date_trunc returns either)
    if isinstance(v, str) and _YEAR_MONTH.match(v):
        return f'{v}-01'  # a month written as YYYY-MM is its first day, as date_trunc('month') gives it
    return v


def _jsonable(v: object) -> object:
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return round(v, 4)
    try:
        return float(v)  # Decimal
    except (TypeError, ValueError):
        return str(v)  # dates, timestamps, etc.


if __name__ == '__main__':
    build()
    for t in list_tables():
        print(f"{t['table']:<45} {t['rows']:>7,}  {t['description'][:70]}")
