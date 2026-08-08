"""Budget di ritardo CALCOLATO della Fase Bio via browser (Passo 4, S4.6).

Il vincolo dello spec: il tempo TOTALE medio/profilo non deve scendere sotto
la baseline pre-inversione (BIO_PROFILE_TOTAL_BASELINE_S = 15.5s). Ma quel
numero include l'attesa della sorgente dati (~8s prima dell'inversione A.1,
perche' web_profile_info non arrivava mai passivamente): dopo l'inversione
l'attesa scende a ~4s (BIO_SOURCE_WAIT_AFTER_INVERSION_S) e il COMPORTAMENTO
configurato deve salire per compensare, fino a BIO_BEHAVIOUR_FLOOR_S = 11.5s.
expected_delay_budget_s() calcola il solo comportamento (niente attesa di
rete) -- niente sleep, niente cronometro: somma i contributi additivi
(micro-scroll, apertura post) e la MEDIA PESATA tra pausa umana e pausa reel,
che nel codice reale (browser_bio.py righe ~1020-1048) sono ALTERNATIVE
(if/else), non additive. worst_case_delay_budget_s() e' un secondo numero,
NON usato in nessuna asserzione di budget: il caso deterministico peggiore
(tutti i sorteggi al minimo), per documentare quanto puo' scendere il ritardo
reale con i minimi reel odierni. Vedi i docstring in
app/services/browser_bio.py per il perche' di ogni termine.
"""
from types import SimpleNamespace

import pytest

from app.services.browser_bio import (
    BIO_BEHAVIOUR_FLOOR_S,
    BIO_PROFILE_TOTAL_BASELINE_S,
    BIO_SOURCE_WAIT_AFTER_INVERSION_S,
    expected_delay_budget_s,
    worst_case_delay_budget_s,
)


def _fake_settings(
    *,
    scroll_min_s=4.0,
    scroll_max_s=5.0,
    scroll_ratio=0.35,
    open_post_ratio=0.25,
    reels_count_min=0,
    reels_count_max=10,
    reels_dwell_min_s=0.0,
    reels_dwell_max_s=10.0,
    reels_every_min=0,
    reels_every_max=10,
):
    """SimpleNamespace con solo gli attributi che le funzioni di budget leggono."""
    return SimpleNamespace(
        bio_browser_scroll_min_s=scroll_min_s,
        bio_browser_scroll_max_s=scroll_max_s,
        bio_browser_scroll_ratio=scroll_ratio,
        bio_browser_open_post_ratio=open_post_ratio,
        bio_browser_reels_count_min=reels_count_min,
        bio_browser_reels_count_max=reels_count_max,
        bio_browser_reels_dwell_min_s=reels_dwell_min_s,
        bio_browser_reels_dwell_max_s=reels_dwell_max_s,
        bio_browser_reels_every_min=reels_every_min,
        bio_browser_reels_every_max=reels_every_max,
    )


def test_bio_behaviour_floor_e_derivato_dal_baseline_totale_meno_l_attesa():
    # 15.5 (totale pre-inversione, spec S4.6) - 4.0 (attesa residua post A.1) = 11.5.
    assert BIO_PROFILE_TOTAL_BASELINE_S == pytest.approx(15.5)
    assert BIO_SOURCE_WAIT_AFTER_INVERSION_S == pytest.approx(4.0)
    assert BIO_BEHAVIOUR_FLOOR_S == pytest.approx(11.5)


def test_expected_delay_budget_matches_hand_computation_su_default_odierni():
    # Stessi default di app/config.py. avg_every=5 -> p_reel=1/5=0.2,
    # p_human=0.8. reel_or_pause = 0.8*7.5 + 0.2*(5*5) = 6.0 + 5.0 = 11.0.
    # scroll = 0.35*4.5 = 1.575. post = 0.35*0.25*6.0 = 0.525.
    # Totale = 11.0 + 1.575 + 0.525 = 13.1s.
    cfg = _fake_settings()
    assert expected_delay_budget_s(cfg) == pytest.approx(13.1, abs=1e-9)


def test_expected_delay_budget_oggi_supera_il_floor_comportamentale():
    # Col floor corretto (11.5, non il baseline totale 15.5) il comportamento
    # configurato oggi COPRE il vincolo: 13.1 >= 11.5. A.4 e' quindi una
    # guardia contro futuri tagli alle pause, non un blocco che impone B.1
    # subito -- vedi test_prova_del_nove sotto per la regressione che la fa
    # scattare.
    cfg = _fake_settings()
    assert expected_delay_budget_s(cfg) >= BIO_BEHAVIOUR_FLOOR_S


def test_expected_delay_budget_scroll_ratio_zero_azzera_scroll_e_post():
    cfg = _fake_settings(scroll_ratio=0.0)
    budget = expected_delay_budget_s(cfg)
    # Con scroll_ratio=0 restano solo pausa/reel: 11.0 (scroll e post sono
    # additivi e indipendenti dalla media pesata pausa/reel).
    assert budget == pytest.approx(11.0, abs=1e-9)


def test_expected_delay_budget_cadenza_degenere_reel_sempre_non_esplode():
    # Caso degenere esplicito: every_min=every_max=0 -> cadenza media 0.
    # Niente ZeroDivisionError e niente probabilita' negativa: sotto la
    # soglia avg_every<=1 trattiamo il caso come "la pausa reel scatta A OGNI
    # profilo" (p_reel=1, p_human=0), il worst-case reale (con every_min=0 la
    # cadenza campionata puo' uscire 0 o 1). Qui i minimi reel restano ai
    # default (0..10, non azzerati): il contributo e' quindi grande (25s),
    # non piccolo -- la media pesata non nasconde il caso, lo rende visibile
    # in entrambe le direzioni.
    cfg = _fake_settings(reels_every_min=0, reels_every_max=0)
    budget = expected_delay_budget_s(cfg)
    assert budget == pytest.approx(25.0 + 1.575 + 0.525, abs=1e-9)


def test_expected_delay_budget_worst_case_reel_sempre_e_zero_secondi():
    # Cadenza degenere E count_min=0/dwell_min_s=0.0: la pausa reel che
    # sostituisce SEMPRE quella umana puo' valere PROPRIO 0 secondi. Deve
    # essere visibile nel budget (crolla a 0 sul termine pausa/reel), non
    # compensato da una pausa umana che in questo scenario non scatta mai.
    cfg = _fake_settings(
        reels_every_min=0,
        reels_every_max=0,
        reels_count_min=0,
        reels_count_max=0,
        reels_dwell_min_s=0.0,
        reels_dwell_max_s=0.0,
    )
    budget = expected_delay_budget_s(cfg)
    # Resta solo il contributo additivo di scroll/post (default ratio 0.35/0.25).
    assert budget == pytest.approx(1.575 + 0.525, abs=1e-9)


def test_expected_delay_budget_tutti_i_minimi_a_zero():
    cfg = _fake_settings(
        scroll_ratio=0.0,
        reels_count_min=0,
        reels_count_max=0,
        reels_dwell_min_s=0.0,
        reels_dwell_max_s=0.0,
        reels_every_min=0,
        reels_every_max=0,
    )
    # Tutto azzerato: scroll/post spenti (ratio=0), pausa/reel a p_reel=1 con
    # reel di durata 0 -> budget 0. E' il pavimento assoluto della funzione.
    assert expected_delay_budget_s(cfg) == pytest.approx(0.0, abs=1e-9)


def test_expected_delay_budget_su_settings_globale_reale():
    # Acceptance test dello spec: gira sui settings VERI (app.config.settings),
    # non su un fake. Confronta contro il FLOOR comportamentale (11.5), non
    # contro il baseline totale (15.5) -- quello includeva l'attesa di rete,
    # che qui non e' contata. Dopo B.1 (minimi reel alzati) il budget e'
    # SALITO da 13.1 a ~14.446 (avg_every=6.5, avg_n_reels=6, avg_dwell_s=6.5):
    # ancora VERDE (14.446 >= 11.5), con margine maggiore di prima.
    assert expected_delay_budget_s() >= BIO_BEHAVIOUR_FLOOR_S


def test_worst_case_delay_budget_matches_hand_computation():
    # Caso non degenere: every_min=3, count_min=2, dwell_min_s=1.0.
    # 2 profili su 3 fanno la pausa umana al minimo (5.0s ciascuno), 1 fa il
    # reel al minimo (2*1.0=2.0s): (2*5.0 + 2.0) / 3 = 12.0/3 = 4.0s.
    cfg = _fake_settings(reels_every_min=3, reels_count_min=2, reels_dwell_min_s=1.0)
    assert worst_case_delay_budget_s(cfg) == pytest.approx(4.0, abs=1e-9)


def test_worst_case_delay_budget_su_default_odierni():
    # B.1 ha alzato i minimi (every_min 0->3, count_min 0->2, dwell_min_s
    # 0.0->3.0) proprio perche' con i vecchi il caso peggiore era un profilo
    # scrapato SENZA NESSUNA pausa comportamentale -- 0.0s. Con i minimi
    # nuovi il caso peggiore deterministico e': 2 profili su 3 fanno la pausa
    # umana al minimo (5.0s ciascuno), 1 fa il reel al minimo (2*3.0=6.0s):
    # (2*5.0 + 6.0) / 3 = 16.0/3 ~= 5.333s. Non e' piu' 0: e' questo che
    # dimostra che B.1 ha chiuso il buco. Pinnato di nuovo: se qualcuno
    # riabbassa i minimi, questo assert salta e va rivisto consapevolmente.
    assert worst_case_delay_budget_s() == pytest.approx(16.0 / 3.0, abs=1e-9)


def test_prova_del_nove_dimezzare_la_pausa_umana_fa_scendere_sotto_il_floor():
    # Dimostra che il test intercetta la regressione vietata dallo spec:
    # "recuperare i secondi liberati dall'inversione A.1" tagliando le pause
    # vere. HUMAN_PAUSE_MIN_S/MAX_S sono condivise da human_profile_pause()
    # (il vero asyncio.sleep) e da expected_delay_budget_s(): dimezzarle
    # simula chi taglia la pausa reale, ed essendo la STESSA costante letta
    # da entrambi i posti, la guardia non puo' restare verde per una
    # duplicazione dimenticata.
    import app.services.browser_bio as browser_bio

    original_min, original_max = browser_bio.HUMAN_PAUSE_MIN_S, browser_bio.HUMAN_PAUSE_MAX_S
    try:
        browser_bio.HUMAN_PAUSE_MIN_S = original_min / 2   # 2.5
        browser_bio.HUMAN_PAUSE_MAX_S = original_max / 2   # 5.0
        # Dopo B.1 avg_every=6.5 (p_reel=2/13, p_human=11/13), avg_n_reels=6,
        # avg_dwell_s=6.5 -> reel contrib = 6*6.5=39 (invariato, non dipende
        # da HUMAN_PAUSE). pause_s = mid(2.5,5.0) = 3.75.
        # reel_or_pause = (11/13)*3.75 + (2/13)*39 = 9.173076923...
        # totale = 9.173076923... + 1.575 + 0.525 = 11.273076923... < 11.5.
        # Anche con i minimi reel alzati da B.1, dimezzare la pausa umana vera
        # fa ancora scendere sotto il floor: la guardia non dipende SOLO dal
        # margine che B.1 ha aggiunto.
        budget = expected_delay_budget_s()
        assert budget == pytest.approx(11.273076923076923, abs=1e-9)
        assert budget < BIO_BEHAVIOUR_FLOOR_S, (
            "il test NON ha rilevato il dimezzamento della pausa umana: non vale niente, va rifatto"
        )
    finally:
        browser_bio.HUMAN_PAUSE_MIN_S = original_min
        browser_bio.HUMAN_PAUSE_MAX_S = original_max
