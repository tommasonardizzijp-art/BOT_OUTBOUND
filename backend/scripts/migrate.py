"""Run Alembic migrations to head. Use as a deploy step, not at API boot.

Usage from backend/: python -m scripts.migrate
"""
import sys
from pathlib import Path

sys.path.insert(0, ".")


def main() -> int:
    from alembic import command
    from alembic.config import Config

    from app.config import settings
    from app.utils.db_dialect import to_async_database_url

    ini_path = Path(__file__).parent.parent / "alembic.ini"
    cfg = Config(str(ini_path))
    db_url = to_async_database_url(settings.database_url).replace("%", "%%")
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "head")
    print("Migrations applied to head.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
