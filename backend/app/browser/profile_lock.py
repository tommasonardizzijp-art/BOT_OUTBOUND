"""Lucchetto Redis CROSS-PROCESSO per il profilo Chromium di un account IG.

Perche' serve (C.2, passo 4): un account ha UNA cartella di profilo browser.
Il mutex `_get_account_lock` in `context_manager.py` e' un `asyncio.Lock`
in-process — vive nella memoria di UN processo Python. Il backend FastAPI
(pulsante manuale, login manuale) e il worker ARQ (campagne DM, Fase Bio,
import, warm-up) sono DUE processi OS separati: quel mutex non puo'
strutturalmente vedere cosa fa l'altro processo. Senza un lock esterno,
premere "browse manuale" mentre una campagna sta mandando DM sullo stesso
account apre un secondo Chromium sulla STESSA cartella profilo -- non un
errore recuperabile, e' cosi' che un profilo si corrompe e si perde una
sessione loggata (nuovo login = nuovo dispositivo per Instagram = rischio
challenge). Vedi memory [[botoutbound-wa-profile-lock-orfano]] per il
precedente WhatsApp che ha causato lo stesso danno.

Pattern ricalcato da `app/services/wa_profile_lock.py` (gia' collaudato in
produzione sul canale WhatsApp), con due differenze deliberate:

1. TTL CORTO + rinnovo automatico, non lungo per prudenza. Il lock WA e' a
   90 minuti SENZA rinnovo automatico ad ogni sessione, e ha gia' prodotto
   un lock orfano che ha richiesto una DELETE manuale (memory
   [[botoutbound-wa-profile-lock-orfano]]). Qui il rinnovo e' incorporato
   nel chokepoint stesso (`context_manager.get_browser_context` /
   `BrowserSession`) tramite `held_with_renew`, quindi il TTL puo' restare
   corto: un crash a meta' sessione si autolibera in pochi minuti invece di
   bloccare l'account per un'ora e mezza.

2. Fail-CLOSED se Redis non risponde: acquisire il lock e non riuscire a
   parlare con Redis NON apre il browser. Motivazione: senza Redis il
   worker ARQ non gira affatto (i job arrivano da li'), quindi non c'e'
   nulla di legittimo gia' in corso da proteggere se Redis e' giu' -- e un
   rifiuto e' un fastidio recuperabile (il chiamante riprova), un profilo
   corrotto no. La versione WA e' invece fail-open sul rinnovo (vedi
   `wa_profile_lock.renew`) perche' li' il rischio e' diverso: qui invece
   l'ACQUISIZIONE (non il rinnovo) e' il punto in cui fail-open aprirebbe
   un secondo Chromium alla cieca -- quindi qui fail-closed.
"""
import asyncio
import enum
import time
import uuid
from contextlib import asynccontextmanager

import arq
from loguru import logger

from app.config import settings
from app.services.work_enqueue import arq_redis_settings

# Retry breve quando un consumatore automatico (campagna DM, Fase Bio,
# import) trova l'account occupato da un'altra sessione browser: non e' la
# fine-sessione (session_break_seconds, minuti-decine), e' "riprova fra un
# attimo" -- stesso valore e stesso ragionamento di `wa_lock_busy_retry_s`
# (config.py), qui replicato invece di importato perche' il lock WA e
# quello IG sono moduli indipendenti per namespace diverso.
BUSY_RETRY_S = 90


class AccountBrowserBusy(Exception):
    """Il profilo browser dell'account e' gia' in uso da un'altra sessione
    (altro processo, altro job) — oppure Redis non ha risposto e si e'
    rifiutata l'apertura per sicurezza (fail-closed, vedi docstring modulo)."""


def _lock_key(account_id: str) -> str:
    return f"ig:profile-lock:{account_id}"


def _valore(token: str) -> str:
    """Token + heartbeat (epoch ms) nello stesso valore, stesso schema di
    wa_profile_lock: un secondo campo, non una seconda chiave, resta atomico
    con SET/GET. Il token non contiene ':' (uuid4().hex), split univoco."""
    return f"{token}:{int(time.time() * 1000)}"


def _token_di(valore: bytes) -> str:
    return valore.decode().split(":", 1)[0]


@asynccontextmanager
async def held(account_id: str, *, ttl_s: int | None = None):
    """Prova UNA VOLTA ad acquisire il lock del profilo `account_id`. Mai
    un'attesa: nessun consumatore (job ARQ o richiesta HTTP) deve bloccarsi
    qui dentro. Solleva `AccountBrowserBusy` se occupato O se Redis non
    risponde (fail-closed). Rilascia in `finally`, solo se il valore a
    Redis e' ancora il TOKEN di questa acquisizione (mai una DELETE
    incondizionata: rilascerebbe il lock di un altro possessore se il
    nostro TTL e' scaduto nel frattempo).

    Non rinnova da solo — per qualunque sessione che puo' durare piu' del
    TTL (qualunque cosa apra un browser: DM, bio, import, warm-up, browse
    manuale, login manuale) usare `held_with_renew`, non questo nudo."""
    ttl = ttl_s if ttl_s is not None else settings.browser_profile_lock_ttl_s
    token = uuid.uuid4().hex
    key = _lock_key(account_id)

    try:
        redis = await arq.create_pool(arq_redis_settings())
    except Exception as exc:
        logger.error(
            f"[IG] lock profilo {account_id}: Redis irraggiungibile in fase di "
            f"connessione, rifiuto apertura browser (fail-closed): {exc}"
        )
        raise AccountBrowserBusy(
            f"account {account_id}: impossibile contattare Redis per il lock del "
            "profilo — apertura browser rifiutata per sicurezza"
        ) from exc

    try:
        try:
            acquired = await redis.set(key, _valore(token), nx=True, ex=ttl)
        except Exception as exc:
            logger.error(
                f"[IG] lock profilo {account_id}: Redis irraggiungibile durante "
                f"l'acquisizione, rifiuto apertura browser (fail-closed): {exc}"
            )
            raise AccountBrowserBusy(
                f"account {account_id}: impossibile verificare il lock del profilo "
                "(Redis non risponde) — apertura browser rifiutata per sicurezza"
            ) from exc

        if not acquired:
            raise AccountBrowserBusy(
                f"account {account_id} gia' in uso da un'altra sessione browser"
            )

        try:
            yield token
        finally:
            # Rilascio best-effort: un blip Redis qui non deve trasformare una
            # sessione riuscita in un errore per il chiamante. Il TTL resta il
            # backstop se questa DELETE non arriva a segno.
            try:
                current = await redis.get(key)
                if current is not None and _token_di(current) == token:
                    await redis.delete(key)
                elif current is not None:
                    logger.warning(
                        f"[IG] lock profilo {account_id}: token cambiato durante "
                        "l'uso (TTL scaduto + nuovo possessore) — non rilascio un "
                        "lock che non e' piu' mio"
                    )
            except Exception as exc:
                logger.warning(
                    f"[IG] lock profilo {account_id}: rilascio fallito (Redis?): "
                    f"{exc} — il TTL scadra' da solo"
                )
    finally:
        try:
            await redis.aclose()
        except Exception:
            pass


class _RenewOutcome(enum.Enum):
    """Distingue i due modi in cui un rinnovo puo' fallire — sono trattati
    diversamente da `held_with_renew` (vedi sotto), quindi `renew()` da solo
    (bool) non basta piu' internamente: serve sapere QUALE dei due."""
    ok = "ok"
    lost = "lost"      # il token non e' piu' il nostro: un altro processo ha il lock
    error = "error"    # blip Redis (connessione o comando) — nessuno sa ancora chi ha il lock


async def _renew_once(account_id: str, token: str, ttl_s: int) -> _RenewOutcome:
    """Un tentativo di rinnovo, con l'esito distinto per `held_with_renew`."""
    key = _lock_key(account_id)
    try:
        redis = await arq.create_pool(arq_redis_settings())
    except Exception as exc:
        logger.warning(
            f"[IG] lock profilo {account_id}: rinnovo fallito, Redis irraggiungibile "
            f"in connessione ({type(exc).__name__}: {exc})"
        )
        return _RenewOutcome.error
    try:
        current = await redis.get(key)
        if current is None or _token_di(current) != token:
            logger.warning(
                f"[IG] lock profilo {account_id}: rinnovo saltato, il lock non e' "
                "piu' nostro (un altro processo lo ha ripreso)"
            )
            return _RenewOutcome.lost
        await redis.set(key, _valore(token), ex=ttl_s)
        return _RenewOutcome.ok
    except Exception as exc:
        logger.warning(f"[IG] lock profilo {account_id}: rinnovo TTL fallito ({type(exc).__name__}: {exc})")
        return _RenewOutcome.error
    finally:
        try:
            await redis.aclose()
        except Exception:
            pass


async def renew(account_id: str, token: str, *, ttl_s: int | None = None) -> bool:
    """Rimette il TTL pieno sul lock, SOLO se e' ancora nostro. Non solleva:
    e' l'API pubblica a basso livello (usata anche fuori da held_with_renew),
    True/False e' il contratto che i chiamanti esistenti si aspettano.
    `held_with_renew` sotto usa `_renew_once` direttamente perche' le due
    ragioni di un False (lock perso vs. Redis irraggiungibile) richiedono
    reazioni diverse — qui sono equivalenti."""
    ttl = ttl_s if ttl_s is not None else settings.browser_profile_lock_ttl_s
    return await _renew_once(account_id, token, ttl) is _RenewOutcome.ok


@asynccontextmanager
async def held_with_renew(
    account_id: str, *, ttl_s: int | None = None, renew_every_s: int | None = None,
    max_consecutive_renew_errors: int | None = None,
):
    """`held()` piu' un heartbeat automatico in background per tutta la
    durata del blocco `async with`. Usare SEMPRE questo (non `held()` nudo)
    per qualunque sessione che apre un browser: la durata (DM batch, Fase
    Bio, browse manuale fino a 60 min, login manuale a tempo dell'utente)
    non e' nota in anticipo e puo' superare il TTL corto del lock. Senza
    rinnovo un TTL corto scadrebbe a meta' sessione e un altro processo
    entrerebbe — il danno esatto che il lock deve evitare.

    Due modi distinti in cui il rinnovo puo' smettere di funzionare, due
    reazioni diverse (rilievo review C.1-C.3, il precedente si limitava a
    loggare ed era un fail-open silenzioso su ENTRAMBI i casi):

    - Blip di connessione Redis: fail-open per un numero limitato di
      tentativi consecutivi (`max_consecutive_renew_errors`, default da
      settings) — un singolo hiccup non deve abbattere una sessione viva.
      Ma oltre quel numero il lock e' verosimilmente scaduto (nessuno lo sa
      con certezza senza poter parlare con Redis): si passa a fail-closed,
      stesso esito del caso sotto.
    - Il token in Redis non e' piu' il nostro: un altro processo ha
      LEGITTIMAMENTE ripreso il lock (dal suo punto di vista era libero).
      Da questo momento possono esistere due Chromium sulla stessa cartella
      profilo — fail-closed immediato, nessun margine di tolleranza.

    In entrambi i casi il chiamante lo scopre: SOLO se il suo blocco
    `async with` finisce senza sollevare nulla di suo, questa funzione
    solleva `AccountBrowserBusy` a chiusura del blocco (dal `finally`, dopo
    aver comunque fatto girare la pulizia di `held()` sotto). Se il blocco
    del chiamante ha gia' sollevato una sua eccezione, quella ha la
    precedenza — non la si maschera con questa — ma il lock perso resta
    comunque loggato a livello ERROR."""
    async with held(account_id, ttl_s=ttl_s) as token:
        ttl = ttl_s if ttl_s is not None else settings.browser_profile_lock_ttl_s
        every = renew_every_s if renew_every_s is not None else settings.browser_profile_lock_renew_s
        max_errors = (
            max_consecutive_renew_errors if max_consecutive_renew_errors is not None
            else settings.browser_profile_lock_max_renew_errors
        )
        stop = asyncio.Event()
        lost = asyncio.Event()

        async def _renew_loop() -> None:
            consecutive_errors = 0
            while True:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=every)
                    return  # stop richiesto — il chiamante ha chiuso il blocco
                except asyncio.TimeoutError:
                    pass
                outcome = await _renew_once(account_id, token, ttl)
                if outcome is _RenewOutcome.ok:
                    consecutive_errors = 0
                    continue
                if outcome is _RenewOutcome.lost:
                    logger.error(
                        f"[IG] lock profilo {account_id}: possesso perso a sessione "
                        "viva — un altro processo ha ripreso il profilo"
                    )
                    lost.set()
                    return
                consecutive_errors += 1
                if consecutive_errors >= max_errors:
                    logger.error(
                        f"[IG] lock profilo {account_id}: {consecutive_errors} rinnovi "
                        "falliti di fila (Redis irraggiungibile) — il lock e' "
                        "verosimilmente scaduto, tratto come perso"
                    )
                    lost.set()
                    return

        task = asyncio.create_task(_renew_loop())
        caller_exc: BaseException | None = None
        try:
            yield token
        except BaseException as exc:
            caller_exc = exc
            raise
        finally:
            stop.set()
            try:
                await task
            except Exception as exc:  # pragma: no cover — difensivo
                logger.debug(f"[IG] lock profilo {account_id}: renew loop terminato con errore: {exc}")
            if lost.is_set():
                if caller_exc is None:
                    raise AccountBrowserBusy(
                        f"account {account_id}: possesso del lock perso durante la "
                        "sessione (rinnovo fallito ripetutamente, o un altro processo "
                        "ha ripreso il profilo) — sessione interrotta"
                    )
                logger.error(
                    f"[IG] lock profilo {account_id}: possesso perso E il chiamante "
                    f"aveva gia' un'eccezione propria ({type(caller_exc).__name__}) "
                    "— propago quella, non la maschero"
                )
