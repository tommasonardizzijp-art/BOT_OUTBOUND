"""Database URL helpers for SQLite and Supabase/Postgres."""
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from typing import Any


def database_dialect(url: str) -> str:
    low = (url or "").lower()
    if low.startswith("sqlite"):
        return "sqlite"
    if low.startswith("postgresql") or low.startswith("postgres://"):
        return "postgresql"
    return low.split(":", 1)[0] if ":" in low else "unknown"


def is_sqlite(url: str) -> bool:
    return database_dialect(url) == "sqlite"


def is_postgres(url: str) -> bool:
    return database_dialect(url) == "postgresql"


def to_async_database_url(url: str) -> str:
    """Normalize DB URLs to async SQLAlchemy drivers."""
    if url.startswith("sqlite+aiosqlite://"):
        return url
    if url.startswith("sqlite://"):
        return url.replace("sqlite://", "sqlite+aiosqlite://", 1)
    if url.startswith("postgresql+asyncpg://"):
        return _with_asyncpg_pooler_safe_params(url)
    if url.startswith("postgresql://"):
        return _with_asyncpg_pooler_safe_params(
            url.replace("postgresql://", "postgresql+asyncpg://", 1)
        )
    if url.startswith("postgres://"):
        return _with_asyncpg_pooler_safe_params(
            url.replace("postgres://", "postgresql+asyncpg://", 1)
        )
    return url


def _with_asyncpg_pooler_safe_params(url: str) -> str:
    """Disable asyncpg prepared statement cache for PgBouncer/Supabase poolers."""
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.setdefault("prepared_statement_cache_size", "0")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def upsert_ignore(table_class: Any, values: dict, conflict_col: str, url: str) -> Any:
    """INSERT ... ON CONFLICT DO NOTHING — returns a dialect-aware insert clause.

    Usage:
        stmt = upsert_ignore(GlobalContact, {...}, "ig_user_id", settings.database_url)
        result = await db.execute(stmt)
        inserted = result.rowcount == 1
    """
    if is_sqlite(url):
        from sqlalchemy.dialects.sqlite import insert
    else:
        from sqlalchemy.dialects.postgresql import insert  # type: ignore[no-redef]
    return insert(table_class).values(**values).on_conflict_do_nothing(index_elements=[conflict_col])
