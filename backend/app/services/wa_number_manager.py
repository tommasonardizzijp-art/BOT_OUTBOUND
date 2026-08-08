"""Cap/warmup/cooldown per i numeri WA. Pattern copiato da
account_manager.py (SDD 6.2: "il concetto si riusa, l'implementazione e'
cablata su InstagramAccount -> servizio wa_number_manager.py che replica
il pattern su wa_numbers, non si generalizza l'esistente in MVP" -- BT3).

wa_numbers NON ha cooldown_until (schema congelato): il timer di cooldown
vive in Redis (TTL), non a DB. Stesso stile del contatore soft-block di
browser_bio.py.
"""
from datetime import datetime

import arq
from loguru import logger

from app.config import settings
from app.services.work_enqueue import arq_redis_settings


def _utc_today_str() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")


def _parse_wa_warmup_steps(spec: str) -> list[int]:
    """"20,20,30,40,60,80,100" -> [20,20,30,40,60,80,100]. Lista ordinale
    (non range come account_manager.WARMUP_LIMITS): warmup_day 1-based
    indicizza direttamente, oltre la fine si resta sull'ultimo valore
    (regime raggiunto, SDD 10.3).

    Una voce non numerica viene SCARTATA con un warning invece di sollevare:
    questa funzione gira nel lifespan del boot e dentro il calcolo del cap di
    ogni invio, quindi un refuso in WA_WARMUP_STEPS faceva morire l'avvio
    dell'applicazione con un ValueError grezzo. Scartare la voce sporca
    degrada in modo prevedibile (la rampa resta sui gradini validi) e lascia
    una traccia leggibile; se NESSUNA voce e' valida, chi chiama ricade sul
    default di config (get_wa_warmup_cap) -- mai su "nessun tetto".
    Trovato nel collaudo M5 con WA_WARMUP_STEPS="abc".
    """
    valori = []
    for voce in spec.split(","):
        voce = voce.strip()
        if not voce:
            continue
        try:
            valori.append(int(voce))
        except ValueError:
            logger.warning(
                f"[WaWarmup] WA_WARMUP_STEPS contiene una voce non numerica "
                f"({voce!r}): ignorata. Gradini validi: {valori or 'nessuno'}")
    return valori


def get_wa_warmup_cap(warmup_day: int) -> int:
    """Cap del giorno di warmup. warmup_day<=0 = fuori warmup: nessun tetto
    da qui (la composizione in effective_wa_daily_cap lo ignora)."""
    steps = _parse_wa_warmup_steps(settings.wa_warmup_steps)
    if not steps:
        return settings.wa_daily_cap_default
    idx = min(warmup_day, len(steps)) - 1
    return steps[max(0, idx)]


async def advance_wa_warmup_if_needed() -> None:
    """Avanza warmup_day per i numeri WA 'active' che non sono ancora stati
    avanzati oggi. Stesso pattern idempotente di
    account_manager.advance_warmup_if_needed (warmup_advanced_date come
    guardia, sicuro sia chiamato al boot che dal cron giornaliero senza
    avanzare due volte lo stesso giorno), con due differenze deliberate
    (decisione prodotto M5, non c'e' uno stato 'warming_up' dedicato per WA):

    - avanza SOLO i numeri 'active' (un WaNumber resta 'active' per tutta
      la rampa, non esiste un secondo stato da cui "uscire");
    - si ferma (no-op) quando warmup_day >= len(steps) invece di azzerare:
      il warmup 'plateau-a' sull'ultimo gradino, non termina.

    G4 (flag, 08/08): se `settings.wa_warmup_enabled` e' False, la funzione
    e' un no-op TOTALE -- nessun numero avanza, nessun warmup_advanced_date
    viene toccato, QUALUNQUE sia warmup_day sulla riga. Stesso principio del
    guard esistente sotto su `passo <= 0`, ma sul flag di prodotto invece
    che sulla configurazione del passo: un flag spento durante un boot o un
    giro di cron non deve avanzare la rampa "di nascosto" mentre e'
    disattivata."""
    from sqlalchemy import select
    from app.database import AsyncSessionLocal
    from app.models.wa import WaNumber, WaNumberStatus

    if not settings.wa_warmup_enabled:
        logger.info("[WaWarmup] wa_warmup_enabled=False: rampa disattivata, "
                    "nessun numero avanzato.")
        return

    steps = _parse_wa_warmup_steps(settings.wa_warmup_steps)
    passo = settings.wa_warmup_advance_steps_per_day

    # Il passo ha UNA direzione sola. Un valore <= 0 in .env non deve poter
    # cambiare il verso della rampa: con un negativo warmup_day scendeva sotto
    # lo zero, e sotto lo zero il gradino esce dal min() di
    # effective_wa_daily_cap -- cioe' una configurazione sbagliata RIMUOVEVA il
    # tetto anti-ban invece di rallentare la rampa, lasciando per giunta il
    # numero fuori dallo sweep per sempre (collaudo M5, passo -5).
    #
    # Zero e' un caso a parte: NON lo si forza a 1. Chi scrive 0 sta chiedendo
    # di congelare la rampa, ed e' una richiesta legittima -- ma va onorata
    # fermandosi qui, non lasciando che la rampa salga lo stesso mentre
    # warmup_advanced_date continua ad aggiornarsi (sembrerebbe funzionare e
    # non farebbe nulla di quello che si e' chiesto). Il warning esiste perche'
    # una rampa ferma e' uno stato che si nota solo se qualcuno lo dice.
    if passo <= 0:
        if passo < 0:
            logger.warning(
                f"[WaWarmup] WA_WARMUP_ADVANCE_STEPS_PER_DAY={passo} e' negativo: "
                "il passo della rampa non puo' essere negativo, avanzamento "
                "sospeso. Correggi la configurazione (1 = un gradino al giorno).")
        else:
            logger.warning(
                "[WaWarmup] WA_WARMUP_ADVANCE_STEPS_PER_DAY=0: la rampa NON "
                "avanzera' da sola. I numeri restano al gradino attuale finche' "
                "non lo si rimette a 1.")
        return

    today = _utc_today_str()
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(WaNumber).where(
                WaNumber.status == WaNumberStatus.active,
                WaNumber.warmup_day > 0,
                WaNumber.warmup_day < len(steps),
            )
        )
        advanced = 0
        for number in result.scalars().all():
            if number.warmup_advanced_date == today:
                continue
            number.warmup_day = min(number.warmup_day + passo, len(steps))
            number.warmup_advanced_date = today
            logger.info(f"[WaWarmup] numero {number.id[:8]} warmup_day -> {number.warmup_day}")
            advanced += 1
        if advanced:
            await db.commit()
            logger.info(f"[WaWarmup] Avanzati {advanced} numero/i")


def effective_wa_daily_cap(number, campaign) -> int:
    """Minimo tra: daily_cap del numero (override admin), daily_limit della
    campagna (se impostato), e il gradino di warmup (se warmup_day>0 E la
    rampa e' abilitata). Nessuno di questi e' opzionale da solo -- e' la
    composizione ad AND che conta (SDD 10.3).

    G4 (flag, 08/08): `settings.wa_warmup_enabled=False` esclude il gradino
    dal min() QUALUNQUE sia warmup_day sulla riga -- non solo quando vale 0.
    Serve perche' warmup_day da solo e' ambiguo (0 = "mai partita" o "spenta
    apposta"?) e riattiva() (wa_numbers.py) scrive warmup_day=1 ad ogni
    riattivazione a prescindere: senza questo flag separato, riattivare un
    numero con la rampa spenta la riaccendeva in silenzio."""
    candidates = [number.daily_cap]
    if getattr(campaign, "daily_limit", None) is not None:
        candidates.append(campaign.daily_limit)
    if settings.wa_warmup_enabled and (number.warmup_day or 0) > 0:
        candidates.append(get_wa_warmup_cap(number.warmup_day))
    return max(0, min(candidates))


def wa_sent_today(number) -> int:
    """Contatore di OGGI con reset lazy (stesso pattern di
    account_manager.effective_scrape_lookups / migrazione 018): se
    sent_date != oggi (UTC), il contatore e' di un giorno passato e vale 0
    senza dipendere da un cron di reset."""
    if getattr(number, "sent_date", None) != _utc_today_str():
        return 0
    return getattr(number, "sent_today", 0) or 0


async def has_wa_send_budget(db, number, campaign) -> bool:
    """Budget del NUMERO (cap effettivo) E del cap GLOBALE di macchina
    (WA_GLOBAL_DAILY_CAP, SDD Q70 -- safety valve su tutti i tenant)."""
    from sqlalchemy import select, func
    from app.models.wa import WaNumber

    if wa_sent_today(number) >= effective_wa_daily_cap(number, campaign):
        return False

    today = _utc_today_str()
    global_sent = await db.scalar(
        select(func.coalesce(func.sum(WaNumber.sent_today), 0)).where(
            WaNumber.sent_date == today,
        )
    ) or 0
    return int(global_sent) < settings.wa_global_daily_cap


async def record_wa_sent(db, number_id: str) -> None:
    """+1 atomico su sent_today, con rollover date-aware nella STESSA
    UPDATE (sezione 4.2: mai read-modify-write, mai due statement separati per
    incremento e confronto data -- pattern scrape_lookups_date, mig. 018)."""
    from sqlalchemy import update, case
    from app.models.wa import WaNumber

    today = _utc_today_str()
    await db.execute(
        update(WaNumber).where(WaNumber.id == number_id).values(
            sent_today=case(
                (WaNumber.sent_date == today, WaNumber.sent_today + 1),
                else_=1,
            ),
            sent_date=today,
        )
    )
    await db.commit()


def _wa_cooldown_redis_key(number_id: str) -> str:
    return f"wa:cooldown:{number_id}"


async def apply_wa_cooldown(number_id: str, *, minutes: int) -> None:
    """Segnale di rischio (FM8-adiacente, SDD 8.3) -> status='cooldown' a
    DB (chiamante) + timer in Redis con TTL. Nessuna scrittura qui su
    WaNumber.status: e' compito del chiamante (wa_sender/wa_worker), che
    conosce il motivo da loggare nell'evento."""
    redis = await arq.create_pool(arq_redis_settings())
    try:
        await redis.set(_wa_cooldown_redis_key(number_id), "1", ex=minutes * 60)
    finally:
        await redis.aclose()


async def is_wa_cooldown_active(number_id: str) -> bool:
    redis = await arq.create_pool(arq_redis_settings())
    try:
        return bool(await redis.exists(_wa_cooldown_redis_key(number_id)))
    finally:
        await redis.aclose()


async def release_expired_wa_cooldowns() -> list[str]:
    """Per ogni WaNumber in status='cooldown', se la chiave Redis e'
    scaduta (TTL passato) lo riporta 'active'. Ritorna gli id rilasciati.
    Chiamato dal cron wa_session_healthcheck (Task 13), non da un timer
    a DB (non esiste una colonna cooldown_until)."""
    from sqlalchemy import select, update
    from app.database import AsyncSessionLocal
    from app.models.wa import WaNumber, WaNumberStatus

    released: list[str] = []
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(WaNumber.id).where(WaNumber.status == WaNumberStatus.cooldown)
        )
        ids = [r[0] for r in result.all()]
        for number_id in ids:
            if not await is_wa_cooldown_active(number_id):
                await db.execute(
                    update(WaNumber).where(WaNumber.id == number_id)
                    .values(status=WaNumberStatus.active)
                )
                released.append(number_id)
        if released:
            await db.commit()
            logger.info(f"[WaNumberManager] cooldown rilasciato per {len(released)} numero/i")
    return released
