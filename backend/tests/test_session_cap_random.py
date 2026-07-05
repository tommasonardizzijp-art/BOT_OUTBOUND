from app.services.scrape_bios import pick_session_cap


def test_cap_in_range():
    for _ in range(500):
        c = pick_session_cap(150, 300)
        assert 150 <= c <= 300


def test_cap_varies():
    caps = {pick_session_cap(150, 300) for _ in range(500)}
    assert len(caps) > 20  # random, non un valore fisso


def test_cap_handles_inverted():
    c = pick_session_cap(300, 150)
    assert 150 <= c <= 300
