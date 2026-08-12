"""Timing del canale WhatsApp (invio, sessioni, break). Stesso stile di
utils/timing.py (lognormale, mai delay uniformi) MA parametrizzato sui
campi per-campagna di wa_campaigns (fallback ai default globali WA_*) --
riuso as-is di timing.py (SDD 6.1), non lo si modifica: quel file resta
di IG, questo e' il suo equivalente WA, non un secondo branch dello stesso
modulo condiviso.
"""
import math
import random

from app.config import settings


def wa_send_delay_seconds() -> float:
    """Delay tra due invii consecutivi dello stesso numero. Lognormale
    centrata su WA_SEND_DELAY_MEDIAN_S (default 15s, tipico 5-30s -- deciso
    con Tommaso 08/08, sostituisce la proposta iniziale di SDD 10.3, mediana
    90s), sigma WA_SEND_DELAY_SIGMA (default 0.7, stesso principio
    anti-firma-piatta di utils.timing.random_delay_seconds)."""
    median = float(settings.wa_send_delay_median_s)
    sigma = float(settings.wa_send_delay_sigma)
    mu = math.log(max(1.0, median))
    delay = random.lognormvariate(mu, sigma)
    return max(1.0, min(median * 8.0, delay))


def _effective_int_pair(campaign_lo, campaign_hi, settings_lo: int, settings_hi: int) -> tuple[int, int]:
    """Campo per-campagna (nullable) vince se ENTRAMBI lo/hi sono valorizzati;
    altrimenti fallback ai default globali WA_*. Un solo campo valorizzato
    e l'altro nullo verrebbe letto come range invertito/degenere: si tratta
    come 'non configurato' e si cade sul default intero."""
    if campaign_lo is not None and campaign_hi is not None:
        return int(campaign_lo), int(campaign_hi)
    return int(settings_lo), int(settings_hi)


def wa_session_message_count(campaign) -> int:
    """Quanti messaggi in una mini-sessione prima del break anti-ban.
    Campo per-campagna (session_min/max_messages) se presente, altrimenti
    WA_SESSION_MIN_MSG/MAX_MSG (default 8/15, SDD 10.3)."""
    lo, hi = _effective_int_pair(
        getattr(campaign, "session_min_messages", None),
        getattr(campaign, "session_max_messages", None),
        settings.wa_session_min_msg, settings.wa_session_max_msg,
    )
    if hi < lo:
        lo, hi = hi, lo
    return random.randint(lo, hi)


def wa_session_break_seconds(campaign) -> float:
    """Pausa lunga anti-ban tra una mini-sessione e la successiva sullo
    stesso numero. Lognormale (sigma 0.6) TRONCATA per riestrazione, non
    clampata.

    Il clamp (`max(lo, min(hi, val))`) non scarta l'estrazione fuori range: la
    schiaccia sul bound. Coi default WA (20-40 min) mandava il **56%** delle
    pause esattamente su 1200s o 2400s -- una firma peggiore di un ritardo
    costante, cioe' lo stesso difetto misurato al 45% sul pacing inbox di
    Instagram e corretto con la PR #55. La mediana sta sulla media
    GEOMETRICA sqrt(lo*hi): in scala logaritmica e' il centro naturale, il
    troncamento taglia code simmetriche e accetta ~2 volte su 3 (stesso
    ragionamento di inbox_browser/ritmo.py::_troncata, non importato da qui
    apposta: quel modulo e' di Instagram, questo file resta l'equivalente WA
    e non un branch condiviso).
    """
    lo_min, hi_min = _effective_int_pair(
        getattr(campaign, "break_min_minutes", None),
        getattr(campaign, "break_max_minutes", None),
        settings.wa_break_min_min, settings.wa_break_max_min,
    )
    lo_s, hi_s = lo_min * 60, hi_min * 60
    if hi_s < lo_s:
        lo_s, hi_s = hi_s, lo_s
    lo_f, hi_f = float(lo_s), float(hi_s)
    if hi_f <= lo_f:
        return lo_f
    mediana = math.sqrt(max(1.0, lo_f) * hi_f)
    for _ in range(20):
        val = random.lognormvariate(math.log(mediana), 0.6)
        if lo_f <= val <= hi_f:
            return val
    return mediana


def effective_wa_active_hours(campaign) -> tuple[int, int]:
    """(ora_inizio, ora_fine) in ora locale del tenant. Campo per-campagna
    (stringhe 'HH:MM') se presente, altrimenti WA_ACTIVE_HOURS globale
    (default '09:30-19:30', Europe/Rome, SDD 10.3). Si tronca ai minuti:
    la granularita' oraria basta al gate finestra (SDD Q68 propone
    lognormale semplice dentro l'ora, non picchi orari)."""
    start_s = getattr(campaign, "active_hours_start", None)
    end_s = getattr(campaign, "active_hours_end", None)
    if start_s and end_s:
        return int(start_s.split(":")[0]), int(end_s.split(":")[0])
    lo_s, hi_s = settings.wa_active_hours.split("-")
    return int(lo_s.split(":")[0]), int(hi_s.split(":")[0])
