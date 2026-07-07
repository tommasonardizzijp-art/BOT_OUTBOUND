"""human_profile_pause non deve piu' fare la sosta stazionaria lunga (15-45s,
12% di probabilita'): quella distrazione e' stata sostituita dalla pausa ATTIVA
sui reel intercalata in `scrape_bios_browser_session` (vedi test_browse_reels
e test_scrape_bios_browser_session::test_reels_break_every_n_profiles).
Qui verifichiamo che il sonno resti sempre nel range base 5-10s e che ogni
chiamata dorma esattamente una volta (niente piu' sleep concatenato)."""
import pytest

from app.services import browser_bio


@pytest.mark.asyncio
async def test_human_pause_never_sleeps_more_than_base_range(monkeypatch):
    durations = []

    async def fake_sleep(seconds):
        durations.append(seconds)

    monkeypatch.setattr(browser_bio.asyncio, "sleep", fake_sleep)

    # Ripetuto molte volte: la vecchia probabilita' del 12% sulla sosta lunga
    # sarebbe emersa quasi certamente su 200 run se non fosse stata rimossa.
    for _ in range(200):
        await browser_bio.human_profile_pause()

    assert len(durations) == 200, "human_profile_pause deve dormire esattamente una volta a chiamata"
    out_of_range = [d for d in durations if not (5.0 <= d <= 10.0)]
    assert not out_of_range, f"pausa fuori dal range base 5-10s: {out_of_range}"
