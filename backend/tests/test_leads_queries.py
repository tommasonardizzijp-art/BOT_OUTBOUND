from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite

from app.api.leads import _sources_subquery


def _compile_sources_query(dialect) -> str:
    return str(select(_sources_subquery()).compile(dialect=dialect))


def test_sources_query_uses_postgres_string_aggregation():
    sql = _compile_sources_query(postgresql.dialect())

    assert "string_agg" in sql
    assert "group_concat" not in sql
    assert "SELECT DISTINCT" in sql


def test_sources_query_uses_sqlite_string_aggregation():
    sql = _compile_sources_query(sqlite.dialect())

    assert "group_concat" in sql
    assert "SELECT DISTINCT" in sql
