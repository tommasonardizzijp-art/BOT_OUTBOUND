"""Run Alembic migrations to head. Use as a deploy step, not at API boot.

Usage from backend/: python -m scripts.migrate
"""
import os
import sys
from collections import namedtuple
from urllib.parse import urlsplit
import platform
from pathlib import Path

sys.path.insert(0, ".")

# Guardia: questo repo non ha uno staging Postgres separato, quindi ogni
# migration su un DB non-locale scrive sul Postgres condiviso (dev+prod).
# Un ALTER TABLE lanciato per errore (worktree sbagliato, .env copiato senza
# guardare) non da' un secondo avviso prima di eseguire. Bypass esplicito con
# MIGRATE_CONFIRM=1 per deploy automatici/CI.
_HOST_LOCALI = {"localhost", "127.0.0.1", "::1"}


def _conferma_host_non_locale(db_url: str) -> None:
    host = urlsplit(db_url).hostname
    if not host or host in _HOST_LOCALI or "sqlite" in db_url.split(":", 1)[0]:
        return
    if os.environ.get("MIGRATE_CONFIRM") == "1":
        print(f"MIGRATE_CONFIRM=1: procedo senza chiedere (host: {host}).")
        return
    print(f"ATTENZIONE: questa migration scrive su un DB non locale: {host}")
    risposta = input("Confermi? (scrivi 'si' per procedere): ").strip().lower()
    if risposta != "si":
        print("Migration annullata.")
        sys.exit(1)

if sys.platform.startswith("win"):
    _UnameResult = namedtuple("uname_result", "system node release version machine processor")
    platform.machine = lambda: "AMD64"
    platform.uname = lambda: _UnameResult("Windows", "", "", "", "AMD64", "AMD64")


def main() -> int:
    from alembic import command
    from alembic.config import Config

    from app.config import settings
    from app.utils.db_dialect import to_async_database_url

    _conferma_host_non_locale(settings.database_url)

    ini_path = Path(__file__).parent.parent / "alembic.ini"
    cfg = Config(str(ini_path))
    db_url = to_async_database_url(settings.database_url).replace("%", "%%")
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "head")
    print("Migrations applied to head.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
