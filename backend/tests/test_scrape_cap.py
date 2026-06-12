from datetime import datetime, timedelta

from app.services.account_manager import (
    scrape_daily_limit_for, has_scrape_budget, effective_scrape_lookups, bump_scrape_lookup,
)


def _today():
    return datetime.utcnow().strftime("%Y-%m-%d")


def _yesterday():
    return (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")


_UNSET = object()


class _Acct:
    def __init__(self, lookups, date=_UNSET):
        self.scrape_lookups_today = lookups
        # default: il conteggio appartiene a OGGI (caso "usato oggi").
        # date=None esplicito => simula riga pre-migrazione (NULL).
        self.scrape_lookups_date = _today() if date is _UNSET else date


class _Camp:
    def __init__(self, override=None):
        self.scrape_daily_limit = override


def test_limit_uses_env_default_when_no_override(monkeypatch):
    from app.services import account_manager
    monkeypatch.setattr(account_manager.settings, "scrape_daily_limit", 180, raising=False)
    assert scrape_daily_limit_for(_Acct(0), _Camp(None)) == 180


def test_limit_uses_campaign_override():
    assert scrape_daily_limit_for(_Acct(0), _Camp(50)) == 50


def test_has_budget_true_below_limit():
    assert has_scrape_budget(_Acct(10), _Camp(50)) is True


def test_has_budget_false_at_limit():
    assert has_scrape_budget(_Acct(50), _Camp(50)) is False


def test_has_budget_false_above_limit():
    assert has_scrape_budget(_Acct(99), _Camp(50)) is False


# ── Lazy daily reset (migrazione 018) ──────────────────────────────────────

def test_stale_counter_reads_as_zero():
    """Contatore di ieri => effective 0, budget disponibile (no dipendenza dal cron)."""
    a = _Acct(300, date=_yesterday())
    assert effective_scrape_lookups(a) == 0
    assert has_scrape_budget(a, _Camp(300)) is True


def test_null_date_reads_as_zero():
    """Riga pre-migrazione (date NULL) => trattata come stale => 0."""
    a = _Acct(300, date=None)
    assert effective_scrape_lookups(a) == 0
    assert has_scrape_budget(a, _Camp(300)) is True


def test_today_counter_counts():
    a = _Acct(300, date=_today())
    assert effective_scrape_lookups(a) == 300
    assert has_scrape_budget(a, _Camp(300)) is False


def test_bump_resets_stale_then_increments():
    """Primo bump di un contatore stale: azzera, aggiorna la data, poi +1."""
    a = _Acct(300, date=_yesterday())
    bump_scrape_lookup(a)
    assert a.scrape_lookups_today == 1
    assert a.scrape_lookups_date == _today()


def test_bump_increments_today():
    a = _Acct(5, date=_today())
    bump_scrape_lookup(a)
    assert a.scrape_lookups_today == 6
    assert a.scrape_lookups_date == _today()
