"""Round-robin pool of pre-logged-in scraping accounts for a campaign.

Approccio C: tutti gli account scraping/both assegnati alla campagna vengono
loggati una volta e tenuti in memoria; il bio-fetch alterna gli account per-lead
(round-robin) così il carico è condiviso dall'inizio, ognuno sul proprio IP/proxy.
Job singolo seriale — nessun worker parallelo.
"""
import json
from datetime import datetime

from loguru import logger

from app.services.account_manager import has_scrape_budget


class ScrapingPoolEmpty(Exception):
    """Nessun account utilizzabile nel pool (tutti a cap o nessuno loggato)."""


class ScrapingPool:
    def __init__(self, entries: list[dict]):
        # entries: list[{"account": InstagramAccount, "client": Client, "slot_owned": bool}]
        self._entries = list(entries)
        self._idx = 0

    @property
    def size(self) -> int:
        return len(self._entries)

    def all_accounts(self) -> list:
        return [e["account"] for e in self._entries]

    def next(self, campaign):
        """Round-robin: ritorna (account, client) con budget residuo, o None se tutti a cap/vuoto."""
        n = len(self._entries)
        if n == 0:
            return None
        for _ in range(n):
            entry = self._entries[self._idx % n]
            self._idx = (self._idx + 1) % n
            acct = entry["account"]
            if has_scrape_budget(acct, campaign):
                return acct, entry["client"]
        return None

    @classmethod
    async def build(cls, db, campaign) -> "ScrapingPool":
        """Pre-logga tutti gli account scraping/both della campagna nel pool."""
        from app.services.scraper import _eligible_scraping_accounts
        from app.utils.instagrapi_client import (
            acquire_scraping_slot, release_scraping_slot, login as _login,
        )

        accounts = await _eligible_scraping_accounts(db, campaign.id)
        if not accounts:
            raise ScrapingPoolEmpty(
                "Nessun account con ruolo 'scraping' o 'both' assegnato a questa campagna."
            )

        entries: list[dict] = []
        for acct in accounts:
            slot_owned = await acquire_scraping_slot(acct.id)
            if not slot_owned:
                logger.warning(
                    f"[ScrapingPool] Slot @{acct.username} già occupato da un'altra campagna — escluso dal pool"
                )
                continue
            try:
                client = await _login(acct, db)
            except Exception as e:
                await release_scraping_slot(acct.id)
                logger.warning(f"[ScrapingPool] Login fallito per @{acct.username}: {e} — escluso dal pool")
                continue
            entries.append({"account": acct, "client": client, "slot_owned": True})

        if not entries:
            raise ScrapingPoolEmpty(
                "Nessun account scraping disponibile/loggato per la campagna (slot occupati o login falliti)."
            )
        logger.info(f"[ScrapingPool] Pool costruito con {len(entries)} account: "
                    f"{', '.join('@' + e['account'].username for e in entries)}")
        return cls(entries)

    async def release(self) -> None:
        """Rilascia gli slot di tutti gli account del pool."""
        from app.utils.instagrapi_client import release_scraping_slot
        for e in self._entries:
            if e["slot_owned"]:
                await release_scraping_slot(e["account"].id)

    async def save_sessions(self, db) -> None:
        """Salva session_data + last_activity per ogni account del pool (evita re-login)."""
        for e in self._entries:
            try:
                e["account"].session_data = json.dumps(e["client"].get_settings())
                e["account"].last_activity_at = datetime.utcnow()
            except Exception as exc:
                logger.warning(f"[ScrapingPool] save session @{e['account'].username} fallito: {exc}")
        await db.commit()
