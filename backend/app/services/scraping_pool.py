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
