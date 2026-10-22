"""Seeded DuckDB warehouse (raw → staging → marts) with read-only query and discovery helpers."""

from __future__ import annotations

import hashlib
import re
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


def schema_overview() -> str:
    """Every table and column with descriptions, for display in the UI."""
    out = []
    for t in list_tables():
        out.append(f"{t['table']}  ({t['rows']:,} rows)\n  {t['description']}")
        for c in describe_table(t['table'])['columns']:
            out.append(f"    {c['name']} {c['type']}" + (f"  -- {c['description']}" if c['description'] else ''))
        out.append('')
    return '\n'.join(out)


def run_readonly(sql: str, max_rows: int = MAX_ROWS) -> dict:
    """Run a single SELECT/WITH query. Returns columns, rows (capped) and total row count."""
    stripped = sql.strip().rstrip(';').strip()
    if not _READ_ONLY_START.match(stripped):
        raise ValueError('Only a single SELECT or WITH query is allowed.')
    if ';' in stripped:
        raise ValueError('Only a single statement is allowed.')
    cur = connection().cursor()
    cur.execute(stripped)
    columns = [d[0] for d in cur.description]
    rows = cur.fetchall()
    return {
        'columns': columns,
        'rows': [[_jsonable(v) for v in row] for row in rows[:max_rows]],
        'row_count': len(rows),
    }


def result_set(sql: str) -> list[tuple]:
    """Full, order-insensitive result set for comparing two queries."""
    data = run_readonly(sql, max_rows=10_000)
    return sorted((tuple(map(_normalize, r)) for r in data['rows']), key=repr)


def _normalize(v: object) -> object:
    if isinstance(v, float):
        return round(v, 2)
    if isinstance(v, str) and v.endswith(' 00:00:00'):
        return v[:-9]  # a midnight timestamp and a date are the same day (date_trunc returns either)
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
