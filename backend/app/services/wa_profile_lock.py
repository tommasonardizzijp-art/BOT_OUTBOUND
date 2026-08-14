"""Lucchetto Redis per profilo Chromium WA: tre consumatori (invio M3,
health-check, reply-scan M4) possono voler aprire lo STESSO profilo
Chromium nello stesso momento. Chromium impedirebbe da solo un secondo
avvio concorrente (SingletonLock), ma `_open_wa_browser` (M1, frozen)
cancella quel file ad ogni avvio come pulizia da crash precedenti --
quindi il guardiano OS sparisce e serve un lock applicativo esplicito.

Un TENTATIVO SOLO, mai un'attesa: nessuno dei tre consumatori deve
bloccarsi dentro un job ARQ (lezione "mai sleep lunghi in job",
browser_bio/wa_worker). Se occupato, il chiamante decide (skip per un
cron, Retry breve per un job).

Token, non DELETE incondizionato: se il TTL scade mentre il possessore
originale e' ancora vivo (sessione piu' lunga del previsto) e un secondo
processo acquisisce nel frattempo, il rilascio del primo NON deve
cancellare il lock del secondo. Rischio residuo accettato (nessun Lua
script in questo repo, nessun altro lock ce l'ha): la finestra fra
GET e DELETE e' minuscola e il caso -- TTL scaduto E secondo acquirente
nella stessa manciata di millisecondi -- non e' mai stato osservato per
i lock TTL gia' in uso (wa_number_manager.apply_wa_cooldown)."""
import time
import uuid
from contextlib import asynccontextmanager

import arq
from loguru import logger

from app.config import settings
from app.services.work_enqueue import arq_redis_settings


class WaProfileBusy(Exception):
    """Il profilo e' gia' in uso da un altro consumatore (invio/health-check/scan)."""


def _lock_key(number_id: str) -> str:
    return f"wa:profile-lock:{number_id}"


def _valore(token: str) -> str:
    """Token + heartbeat (epoch ms) nello stesso valore: un secondo campo,
    non una seconda chiave, cosi' resta atomico con SET/GET esistenti. Il
    token non contiene ':' (uuid4().hex), quindi lo split e' univoco."""
    return f"{token}:{int(time.time() * 1000)}"


def _token_di(valore: bytes) -> str:
    return valore.decode().split(":", 1)[0]


@asynccontextmanager
async def held(number_id: str, *, ttl_min: int | None = None):
    """Prova UNA VOLTA ad acquisire il lock del profilo `number_id`.
    Solleva WaProfileBusy se occupato. Rilascia in `finally`, solo se il
    valore a Redis e' ancora il TOKEN di questa acquisizione."""
    ttl_s = (ttl_min if ttl_min is not None else settings.wa_profile_lock_ttl_min) * 60
    token = uuid.uuid4().hex
    key = _lock_key(number_id)

    redis = await arq.create_pool(arq_redis_settings())
    try:
        acquired = await redis.set(key, _valore(token), nx=True, ex=ttl_s)
        if not acquired:
            raise WaProfileBusy(f"profilo {number_id} gia' in uso")
        try:
            yield token
        finally:
            current = await redis.get(key)
            if current is not None and _token_di(current) == token:
                await redis.delete(key)
            elif current is not None:
                logger.warning(f"[WA] lock profilo {number_id}: token cambiato "
                               "durante l'uso (TTL scaduto + nuovo possessore) -- "
                               "non rilascio un lock che non e' piu' mio")
    finally:
        await redis.aclose()


async def renew(number_id: str, token: str, *, ttl_min: int | None = None) -> bool:
    """Rimette il TTL pieno sul lock, SOLO se e' ancora nostro (stesso
    principio del rilascio: mai toccare un lock che non e' piu' nostro).
    Ritorna True se rinnovato.

    Serve perche' il TTL da solo non basta: una mini-sessione di invio dura
    decine di minuti e la sua durata ha una coda destra lunga (delay
    lognormale fra i messaggi). Se il TTL scadesse a sessione viva, un altro
    consumatore acquisirebbe legittimamente il lock e aprirebbe un secondo
    Chromium sullo STESSO profilo -- il danno esatto che il lock previene.
    Un EXPIRE dopo ogni messaggio costa nulla e rende la scadenza funzione
    dell'ultimo segno di vita, non della durata totale prevista.

    Non solleva: un blip Redis non deve abbattere una sessione di invio in
    corso, e il chiamante non ha nulla di sensato da fare con l'errore --
    resta il TTL gia' impostato piu' il cap wall-clock del chiamante."""
    ttl_s = (ttl_min if ttl_min is not None else settings.wa_profile_lock_ttl_min) * 60
    key = _lock_key(number_id)
    try:
        redis = await arq.create_pool(arq_redis_settings())
        try:
            current = await redis.get(key)
            if current is None or _token_di(current) != token:
                logger.warning(f"[WA] lock profilo {number_id}: rinnovo saltato, "
                               "il lock non e' piu' nostro")
                return False
            # SET (non EXPIRE) perche' rinfresca anche l'heartbeat embedded
            # nel valore, che release_stale() usa per capire se il possessore
            # e' ancora vivo -- indipendentemente dal TTL residuo.
            await redis.set(key, _valore(token), ex=ttl_s)
            return True
        finally:
            await redis.aclose()
    except Exception as exc:
        logger.warning(f"[WA] lock profilo {number_id}: rinnovo TTL fallito "
                       f"({type(exc).__name__})")
        return False


async def release_stale(*, stale_after_min: int | None = None) -> int:
    """Pulizia proattiva dei lock `wa:profile-lock:*` orfani, a fianco (non
    al posto) del TTL. INDIPENDENTE da wa_profile_lock_ttl_min: quel valore
    e' anche il cap wall-clock della mini-sessione (vedi commento in
    config.py e _limite_sessione_s in wa_worker.py), abbassarlo per
    accelerare la pulizia sposterebbe anche quel cap. Qui invece si guarda
    l'heartbeat che renew() aggiorna a ogni segno di vita: il gap peggiore
    fra due renew legittimi e' un delay fra messaggi (default fino a
    ~12 min, WA_SEND_DELAY_MEDIAN_S*8) piu' il costo di invio di un
    messaggio -- stale_after_min di default (25) lascia margine ampio senza
    aspettare i 90 min pieni del TTL. Va chiamato da un cron a tutte le ore
    (cron_worker.release_stale_wa_profile_locks): un worker puo' morire
    fuori dalla finestra attiva del canale.

    Non confonde un lock scritto PRIMA di questa modifica (valore senza
    heartbeat, solo token) per orfano: lo ignora, non lo cancella -- fail
    safe verso il lock, non verso la pulizia."""
    threshold_min = (stale_after_min if stale_after_min is not None
                     else settings.wa_profile_lock_stale_min)
    cutoff_ms = int(time.time() * 1000) - threshold_min * 60_000
    redis = await arq.create_pool(arq_redis_settings())
    rilasciati = 0
    try:
        async for key in redis.scan_iter(match="wa:profile-lock:*"):
            valore = await redis.get(key)
            if valore is None:
                continue
            parti = valore.decode().split(":", 1)
            if len(parti) != 2 or not parti[1].isdigit():
                continue
            heartbeat_ms = int(parti[1])
            if heartbeat_ms < cutoff_ms:
                await redis.delete(key)
                rilasciati += 1
                eta_min = round((int(time.time() * 1000) - heartbeat_ms) / 60_000)
                logger.warning(
                    f"[WA] lock profilo {key.decode()}: rilasciato da cron, "
                    f"nessun heartbeat da {eta_min} min (worker morto a meta' "
                    "sessione, non un rilascio pulito)")
    finally:
        await redis.aclose()
    return rilasciati


async def profilo_occupato_da() -> str | None:
    """Il number_id di UN profilo con il lucchetto preso, o None se nessuno.

    Serve al gate globale del discover: i lock sono per-numero e non si
    escludono fra loro, ma i browser condividono la RAM della macchina. Su
    questo PC (7,5 GB, 1,2 GB per profilo) due sessioni insieme sono la
    condizione in cui il 14/08 sender e scan hanno girato sovrapposti.

    Sola lettura: non prende, non rilascia, non rinnova nulla.
    """
    redis = await arq.create_pool(arq_redis_settings())
    try:
        async for key in redis.scan_iter(match="wa:profile-lock:*"):
            nome = key.decode() if isinstance(key, bytes) else key
            return nome.split("wa:profile-lock:", 1)[1]
        return None
    finally:
        await redis.aclose()
