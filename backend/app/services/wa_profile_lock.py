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
        acquired = await redis.set(key, token, nx=True, ex=ttl_s)
        if not acquired:
            raise WaProfileBusy(f"profilo {number_id} gia' in uso")
        try:
            yield token
        finally:
            current = await redis.get(key)
            if current is not None and current.decode() == token:
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
            if current is None or current.decode() != token:
                logger.warning(f"[WA] lock profilo {number_id}: rinnovo saltato, "
                               "il lock non e' piu' nostro")
                return False
            await redis.expire(key, ttl_s)
            return True
        finally:
            await redis.aclose()
    except Exception as exc:
        logger.warning(f"[WA] lock profilo {number_id}: rinnovo TTL fallito "
                       f"({type(exc).__name__})")
        return False
