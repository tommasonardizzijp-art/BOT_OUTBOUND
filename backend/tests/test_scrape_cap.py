from app.services.account_manager import scrape_daily_limit_for, has_scrape_budget


class _Acct:
    def __init__(self, lookups):
        self.scrape_lookups_today = lookups


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
