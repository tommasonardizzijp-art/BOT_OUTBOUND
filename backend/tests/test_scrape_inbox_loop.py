"""Inbox Fase Lista: dedup-frontier puro (correttezza riavvio/switch)."""
from app.services.scrape_inbox import inbox_collect


def test_collect_filters_already_saved():
    existing = {123, 456}
    page = [(123, "mario"), (789, "gino"), (456, "lucia"), (789, "gino")]
    # 123/456 gia' salvati -> scartati; 789 nuovo, dedup interno pagina -> una volta
    assert inbox_collect(page, existing) == [(789, "gino")]


def test_collect_all_new():
    assert inbox_collect([(1, "a"), (2, "b")], set()) == [(1, "a"), (2, "b")]


def test_collect_all_known_returns_empty():
    assert inbox_collect([(1, "a"), (2, "b")], {1, 2}) == []
