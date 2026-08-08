"""Fase Bio via BROWSER (Patchright) — alternativa credibile all'API mobile.

Perche' esiste: instagrapi chiama gli endpoint privati mobile con una sessione
web-born su device sintetico -> firma da automazione (vedi memory
[[botoutbound-checkpoint-pattern-api]]). Aprire il profilo in un browser vero e'
molto piu' tollerato: la richiesta ai dati profilo viaggia dentro una sessione
Chromium legittima (cookie reali, header, TLS, referer della navigazione).

Come funziona: Instagram web e' una SPA React. Navigando su /<username>/ i dati
profilo arrivano comunque da chiamate interne che fa il JS di IG, non noi, e che
noi INTERCETTIAMO passivamente (nessuna richiesta aggiunta). Niente API instagrapi
-> NON consuma il cap scrape_daily_limit.

QUALE sorgente passiva (passo 4, misurato — la v1 aveva l'ordine sbagliato): la
sorgente e' la query GraphQL `PolarisProfilePageContentQuery`, disponibile 65/65 e
senza campi mancanti. `web_profile_info` NON e' mai stato colto passivamente
(0/65): attenderlo significava consumare l'attesa intera e poi fare una fetch
in-page, cioe' UNA RICHIESTA ATTRIBUIBILE PER PROFILO che la pagina non avrebbe
fatto. Ora la fetch esplicita e' solo il ripiego, ed e' contata
(`contatore_ripieghi`) perche' una regressione la rimetterebbe in mezzo in
silenzio. Vedi lo spec dei livelli di arricchimento §3 e §4.5.

Anti-divergenza: i campi del JSON web sono mappati su uno shim con gli STESSI nomi
attributo dell'oggetto instagrapi, poi passati allo stesso `extract_contacts` e
scritti sugli stessi campi Follower + `upsert_lead` di `fetch_and_store_bio`.
"""
import asyncio
import json as _json
import random
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import arq
from loguru import logger
from sqlalchemy import update

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.follower import FollowerStatus
from app.services import account_manager
from app.services.bot_state_service import is_halted
from app.services.scrape_bios import bio_should_continue, pick_session_cap
from app.services.work_enqueue import arq_redis_settings, ARQ_MAIN_QUEUE
from app.browser.context_manager import BrowserSession
from app.utils.exceptions import (
    AccountChallengeError, AccountBannedError, AccountSessionExpiredError,
)
from app.utils.ig_block_detect import detect_block_interstitial

# App-id pubblico del web di Instagram (usato dal suo stesso JS per web_profile_info).
WEB_APP_ID = "936619743392459"
_WEB_PROFILE_PATH = "/api/v1/users/web_profile_info/"

# Endpoint interno del client web IG: il browser lo chiama da solo navigando il
# profilo (query `PolarisProfilePageContentQuery`). Lo INTERCETTIAMO passivamente.
# Dal passo 4 e' la sorgente PRIMARIA, non piu' il ripiego: misurato disponibile
# 65/65 e senza campi mancanti, mentre web_profile_info non e' mai stato colto
# passivamente (0/65). VIETATO fare fetch attivo di questo endpoint (replicherebbe
# fb_dtsg/lsd/doc_id = pattern anomalo + fragile): solo lettura passiva. Vedi
# docs/audits/GRAPHQL_FALLBACK_BIO_BROWSER.md.
_GRAPHQL_PATH = "/api/graphql"

# Quante volte in questo processo si e' dovuto CHIEDERE web_profile_info invece di
# leggere una sorgente passiva. Dopo l'inversione deve restare a zero o quasi: se
# cresce, una regressione ha rimesso una richiesta attribuibile per profilo. Non
# persistito: e' un termometro di processo, non un dato di dominio.
_ripieghi_espliciti = 0


def contatore_ripieghi() -> int:
    """Numero di fetch esplicite di web_profile_info fatte da questo processo."""
    return _ripieghi_espliciti


def reset_contatore_ripieghi() -> None:
    """Azzera il contatore (usato dai test; in produzione non serve chiamarla)."""
    global _ripieghi_espliciti
    _ripieghi_espliciti = 0


def _conta_ripiego(username: str) -> None:
    global _ripieghi_espliciti
    _ripieghi_espliciti += 1
    logger.warning(
        f"[BioBrowser] @{username}: nessuna sorgente passiva entro l'attesa -> fetch "
        f"esplicita di web_profile_info (ripieghi in questo processo: {_ripieghi_espliciti})"
    )


# Chiamate a /info/ risparmiate dal gate professional. E' il numero che dice se il
# gate sta lavorando: se resta a zero su una lista lunga, il segnale non arriva.
_gate_risparmi = 0


def contatore_gate() -> int:
    """Chiamate /info/ evitate dal gate professional in questo processo."""
    return _gate_risparmi


def reset_contatore_gate() -> None:
    global _gate_risparmi
    _gate_risparmi = 0


def _conta_gate() -> None:
    global _gate_risparmi
    _gate_risparmi += 1

# Backstop iterazioni per mini-sessione: `cap` conta solo gli outcome 'done'. Un pool
# di pending prevalentemente private/not_found/error potrebbe non raggiungere mai
# `cap` pur richiedendo moltissime iterazioni (ognuna con un human_profile_pause di
# 5-10s+), rischiando di sforare job_timeout. Limite totale iterazioni (done+skip) =
# cap * questo moltiplicatore.
MAX_SESSION_ITERATIONS_MULTIPLIER = 5


def _to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def web_user_to_shim(user: dict) -> SimpleNamespace:
    """Mappa il dict `data.user` di web_profile_info su un oggetto con gli stessi
    nomi-attributo che `extract_contacts`/lo storage si aspettano dall'oggetto
    instagrapi. Pura e testabile: nessun IO. Robusta a chiavi mancanti."""
    user = user or {}
    followed_by = user.get("edge_followed_by") or {}
    follows = user.get("edge_follow") or {}

    # bio_links nel web arriva come lista di {url,title,link_type,lynx_url}. Lo
    # normalizziamo a {url,title} come si aspetta _bio_links_from (che gestisce dict).
    raw_bio_links = user.get("bio_links") or []
    bio_links = []
    for bl in raw_bio_links:
        if isinstance(bl, dict) and bl.get("url"):
            bio_links.append({"url": bl.get("url"), "title": bl.get("title") or bl.get("link_type")})

    return SimpleNamespace(
        pk=user.get("id"),
        username=user.get("username"),
        full_name=user.get("full_name"),
        biography=user.get("biography") or "",
        is_verified=bool(user.get("is_verified", False)),
        is_private=bool(user.get("is_private", False)),
        follower_count=_to_int(followed_by.get("count")),
        following_count=_to_int(follows.get("count")),
        external_url=user.get("external_url"),
        # Campi business: il web li espone come business_email/business_phone_number.
        public_email=user.get("business_email"),
        public_phone_number=user.get("business_phone_number"),
        contact_phone_number=user.get("business_phone_number"),
        public_phone_country_code=None,  # il web li da' gia' uniti nel numero business
        bio_links=bio_links,
        whatsapp_number=None,  # non esposto sul web; il regex sulla bio cattura wa.me
    )


def graphql_user_to_web_shape(gql_user: dict) -> dict:
    """Normalizza il dict `data.user` della query GraphQL interna di IG
    (`PolarisProfilePageContentQuery`) nella FORMA di `web_profile_info`, cosi'
    `web_user_to_shim` resta l'UNICO shim (anti-divergenza col path API).

    Delta di forma noti (GraphQL FLAT -> web_profile_info ANNIDATO):
      - pk              -> id
      - follower_count  -> edge_followed_by.count
      - following_count -> edge_follow.count
    Gli altri campi (username, full_name, biography, is_private, is_verified,
    external_url, bio_links) hanno gia' gli stessi nomi e passano invariati.
    Pura e testabile: nessun IO. Robusta a chiavi mancanti/None.
    """
    g = gql_user or {}
    shaped = dict(g)  # copia: preserva i campi col nome gia' coincidente
    shaped["id"] = g.get("pk") or g.get("id")
    shaped["edge_followed_by"] = {"count": g.get("follower_count")}
    shaped["edge_follow"] = {"count": g.get("following_count")}
    return shaped


async def _capture_web_profile_info(
    raw_page, username: str, timeout_s: float | None = None
) -> dict | None:
    """Naviga al profilo e ottiene i dati profilo nella forma di web_profile_info.

    ORDINE DELLE SORGENTI (passo 4 — invertito rispetto alla v1):
      1. **Sorgenti passive**, entrambe a costo zero, ascoltate in parallelo mentre
         la pagina carica: la GraphQL che il JS di IG spara da se' (misurata
         disponibile 65/65) e web_profile_info (misurata 0/65: praticamente mai).
         Si esce APPENA una delle due e' pronta — l'attesa e' guidata dall'arrivo,
         non dal timeout. A parita', web_profile_info vince: stesso costo ma piu'
         ricco, espone `is_professional_account` in modo affidabile mentre nel
         GraphQL e' sempre null (serve al gate professional).
      2. **Ripiego esplicito**: fetch IN-PAGE di web_profile_info (cookie reali,
         x-ig-app-id web) solo se nessuna sorgente passiva e' arrivata entro
         `timeout_s`. NON e' invisibile — dove la pagina fa una richiesta dati,
         questa ne fa due — quindi viene CONTATA (`contatore_ripieghi`).
      3. **Ultima spiaggia**: se la fetch fallisce con un errore NON-rate-limit
         (tipicamente il bug 400 "asset ...subvertical deleted" su certi account
         business) e nel frattempo e' arrivata una GraphQL del profilo giusto, la
         normalizziamo e la usiamo. NON facciamo MAI un fetch attivo di
         /api/graphql (solo lettura passiva).

    `timeout_s` None => `settings.bio_browser_source_wait_s`. Non abbassarlo a
    pochi secondi: la GraphQL arriva a mediana ~4s, con 2s le catture sono zero.

    Ritorna il dict `data.user` (forma web_profile_info, eventualmente da GraphQL
    gia' normalizzata), oppure {"__status": st} su fail rate-limit, oppure
    {"__blocked": nome_blocco} se il goto e' finito su un interstiziale IG
    (scraping_warning/challenge/checkpoint/suspended), oppure None.
    Non solleva su errori di parsing.
    """
    if timeout_s is None:
        timeout_s = settings.bio_browser_source_wait_s
    captured: dict = {}

    async def _on_response(resp):
        try:
            # Rate-limit/soft-block visto PASSIVAMENTE. Va catturato e ha priorita'
            # sui dati: prima dell'inversione il 429 lo scoprivamo solo perche' la
            # fetch esplicita lo riceveva in faccia, e non mascherarlo era
            # un'invariante dichiarata. Senza piu' fetch quel segnale sparirebbe.
            # Cosi' e' anche meglio di prima: vediamo il throttle che IG serve al
            # SUO stesso codice, che la vecchia strada non poteva vedere.
            if resp.status in (429, 401, 403) and (
                _WEB_PROFILE_PATH in resp.url or _GRAPHQL_PATH in resp.url
            ):
                captured.setdefault("blocco_passivo", resp.status)
                return
            if _WEB_PROFILE_PATH in resp.url and resp.status == 200:
                body = await resp.json()
                u = (((body or {}).get("data") or {}).get("user"))
                if u:
                    captured["user"] = u
            elif (_GRAPHQL_PATH in resp.url and resp.status == 200
                  and "graphql_user" not in captured):
                body = await resp.json()
                u = (((body or {}).get("data") or {}).get("user"))
                # Solo il profilo GIUSTO (username combacia) e con i campi bio:
                # /api/graphql serve molte query diverse; ci interessa solo quella
                # del profilo navigato.
                if (isinstance(u, dict) and u.get("username")
                        and str(u["username"]).lower() == username.lower()
                        and ("biography" in u or "follower_count" in u)):
                    captured["graphql_user"] = u
        except Exception:
            pass  # response non-JSON o gia' consumata: ignora

    def _graphql_fallback(motivo: str = "web_profile_info non utilizzabile"):
        """Ritorna il user GraphQL normalizzato se catturato, altrimenti None."""
        g = captured.get("graphql_user")
        if g:
            logger.info(f"[BioBrowser] @{username}: uso GraphQL passivo ({motivo})")
            return graphql_user_to_web_shape(g)
        return None

    raw_page.on("response", _on_response)
    try:
        url = f"https://www.instagram.com/{username}/"
        await raw_page.goto(url, wait_until="domcontentloaded", timeout=30000)

        # Blocco IG: uscire SUBITO. Insistere con la fetch esplicita da dietro
        # l'avviso aggiunge richieste attribuibili proprio nel momento peggiore,
        # e i profili tornerebbero vuoti facendoli marcare 'skipped' a vuoto.
        blocco = detect_block_interstitial(raw_page.url)
        if blocco:
            logger.error(f"[BioBrowser] @{username}: interstiziale IG '{blocco}' ({raw_page.url})")
            return {"__blocked": blocco}

        # Attesa guidata dall'ARRIVO, non dal timeout: si esce appena UNA delle due
        # sorgenti passive e' pronta. Prima dell'inversione si attendeva solo
        # web_profile_info, che sul campo non arriva mai (0/65): otto secondi buttati
        # e poi una fetch esplicita per ogni profilo. Passo 0,1s (non 0,4) per non
        # regalare fino a mezzo secondo dopo che il dato e' gia' in mano.
        waited = 0.0
        while waited < timeout_s and not (
            "user" in captured or "graphql_user" in captured or "blocco_passivo" in captured
        ):
            await asyncio.sleep(0.1)
            waited += 0.1

        # PRIORITA', decisa esplicitamente: i dati passivi, se ci sono, si usano.
        # Sono gia' in mano e non sono costati nessuna richiesta -- rifiutarli non
        # protegge da niente e perde il lead. Il 429 passivo serve a decidere di NON
        # chiedere (sotto), non a buttare dati gratuiti.
        if "user" in captured:
            return captured["user"]
        gql_passivo = _graphql_fallback("sorgente primaria, nessuna richiesta aggiunta")
        if gql_passivo is not None:
            return gql_passivo

        # Niente dati passivi. Se abbiamo visto un throttle, NON si chiede: insistere
        # da dentro un rate-limit e' esattamente il modo di trasformarlo in blocco.
        st_passivo = captured.get("blocco_passivo")
        if st_passivo:
            logger.warning(
                f"[BioBrowser] @{username}: HTTP {st_passivo} visto passivamente e nessun "
                f"dato -> soft-block, nessuna richiesta aggiunta"
            )
            return {"__status": st_passivo}

        # Nessuna sorgente passiva entro l'attesa: ripiego esplicito, contato.
        _conta_ripiego(username)
        try:
            result = await raw_page.evaluate(
                """async (args) => {
                    const [username, appId] = args;
                    const r = await fetch(
                        `/api/v1/users/web_profile_info/?username=${encodeURIComponent(username)}`,
                        { headers: { 'x-ig-app-id': appId }, credentials: 'include' }
                    );
                    if (!r.ok) return { __status: r.status };
                    return await r.json();
                }""",
                [username, WEB_APP_ID],
            )
            if isinstance(result, dict):
                if result.get("__status"):
                    st = result["__status"]
                    # 429/401/403 = rate-limit reale: propaga come soft_block, NON
                    # mascherare con GraphQL (altrimenti si martella cieco).
                    if st not in (429, 401, 403):
                        gql = _graphql_fallback()
                        if gql is not None:
                            return gql
                    logger.warning(f"[BioBrowser] @{username}: web_profile_info fetch HTTP {st}")
                    return {"__status": st}
                u = (((result or {}).get("data") or {}).get("user"))
                if u:
                    return u
        except Exception as e:
            logger.warning(f"[BioBrowser] @{username}: fetch in-page fallito ({type(e).__name__}: {e})")

        # Ne' passivo ne' fetch in-page hanno dato dati usabili: ultima spiaggia GraphQL.
        return _graphql_fallback()
    finally:
        try:
            raw_page.remove_listener("response", _on_response)
        except Exception:
            pass


async def _fetch_public_contact_inpage(raw_page, pk) -> dict | None:
    """Recupera i contatti pubblici (public_email/public_phone) via `/api/v1/users/{pk}/info/`
    con un in-page fetch DENTRO la sessione browser (cookie web, Chrome reale, x-ig-app-id web).

    Perche' serve: `web_profile_info` (quello che il JS di IG spara sul profilo) NON espone
    l'email/telefono business — torna `business_email=null`. Il dato di contatto vive invece
    su `/info/`, nei campi mobile-native `public_email`/`public_phone_number`/`contact_phone_number`
    (verificato 2026-07-08: web_profile_info=null, /info/ con app-id web=200 con l'email giusta).

    Anti-detection: NON e' il "pattern API nudo" mobile (device sintetico) che causa i checkpoint —
    e' il browser reale (UA Chrome, TLS, cookie web) che chiama un endpoint web-autenticato, come
    gia' fa con web_profile_info. Con app-id web risponde 200; senza da 400 "useragent mismatch"
    (IG valida la coerenza app-id/UA, e la nostra e' coerente). Kill-switch: bio_browser_contact_info_enabled.

    Difensivo: non solleva mai. Ritorna:
      - dict coi campi contatto in caso di successo;
      - {"__rate_limited": status} se /info/ risponde 429/401/403 (il chiamante
        lo tratta come soft_block: NON ingoiare il rate-limit di questo endpoint
        mobile-shape, altrimenti si martella cieco a volume);
      - None per qualsiasi altro miss benigno (no pk, no user, altri status, errore JS)."""
    if not pk:
        return None
    try:
        result = await raw_page.evaluate(
            """async (args) => {
                const [pk, appId] = args;
                try {
                    const r = await fetch(`/api/v1/users/${pk}/info/`,
                        { headers: { 'x-ig-app-id': appId }, credentials: 'include' });
                    if (!r.ok) return { __status: r.status };
                    const b = await r.json();
                    const u = b && b.user;
                    if (!u) return null;
                    return {
                        public_email: u.public_email ?? null,
                        public_phone_number: u.public_phone_number ?? null,
                        contact_phone_number: u.contact_phone_number ?? null,
                        public_phone_country_code: u.public_phone_country_code ?? null,
                    };
                } catch (e) { return { __err: String(e) }; }
            }""",
            [str(pk), WEB_APP_ID],
        )
        if isinstance(result, dict) and not result.get("__status") and not result.get("__err"):
            return result
        if isinstance(result, dict) and result.get("__status"):
            st = result["__status"]
            if st in (429, 401, 403):
                logger.warning(f"[BioBrowser] /info/ HTTP {st} per pk={pk} — rate-limit, propago soft_block")
                return {"__rate_limited": st}
            logger.debug(f"[BioBrowser] /info/ HTTP {st} per pk={pk}")
        return None
    except Exception as e:
        logger.debug(f"[BioBrowser] /info/ in-page fallito pk={pk} ({type(e).__name__}: {e})")
        return None


def profilo_professional(profilo) -> bool | None:
    """True / False / None (nessun segnale leggibile) dal payload GIA' scaricato.

    Zero richieste in piu' per decidere: e' la ragione per cui il gate non e'
    circolare. Regola misurata su tre probe (44 casi, 07/08): ogni profilo che ha
    reso un contatto reale era marcato professional.

    Si guardano TRE campi e non solo `is_professional_account` perche' quel flag e'
    affidabile in `web_profile_info` ma e' SEMPRE null nella GraphQL, che dal passo 4
    e' la sorgente primaria: la' il segnale sta su `account_type` / `is_business`.

    I positivi vengono valutati prima dei negativi (stesso ordine del probe), e
    qualunque forma inattesa cade su None -- che il chiamante tratta come "procedi".
    Un tipo strano (`account_type` stringa) quindi NON chiude il gate: fallire da
    questo lato costa footprint, dall'altro costa un contatto che non si recupera."""
    if not isinstance(profilo, dict):
        return None
    if profilo.get("is_professional_account") is True:
        return True
    if profilo.get("account_type") in (2, 3):
        return True
    if profilo.get("is_business") is True:
        return True
    if (profilo.get("is_professional_account") is False
            or profilo.get("account_type") == 1
            or profilo.get("is_business") is False):
        return False
    return None


def contatti_richiesti(campaign, profilo=None) -> bool:
    """True se `/info/` puo' partire per QUESTO profilo di QUESTA campagna.

    Tre condizioni in AND, e sono TUTTE necessarie solo qui (canale browser),
    perche' `/info/` e' una richiesta reale che nessuna pagina web produce mai:
      - l'interruttore globale e' acceso (kill-switch operativo, vince su tutto) —
        e' un interruttore del canale browser, non tocca il ramo API;
      - il livello della campagna e' 'contacts' (`contatti_richiesti_dal_livello`,
        condivisa col ramo API — vedi il suo docstring per la differenza);
      - il profilo non e' esplicitamente non-professional (gate, §4.3 dello spec) —
        esiste solo per evitare QUESTA richiesta, quindi non si applica al ramo API
        dove i contatti arrivano gia' col payload della bio.

    Difensivo sulla retrocompatibilita': una campagna senza il campo si comporta
    come prima dell'introduzione dei livelli (chiama /info/), altrimenti la
    migrazione spegnerebbe in silenzio la raccolta contatti su campagne che la
    volevano. `profilo` None => gate non applicabile => si procede."""
    from app.models.campaign import contatti_richiesti_dal_livello
    if not settings.bio_browser_contact_info_enabled:
        return False
    if not contatti_richiesti_dal_livello(campaign):
        return False
    if not settings.bio_browser_professional_gate_enabled:
        return True
    if profilo_professional(profilo) is False:
        _conta_gate()
        return False
    return True


async def fetch_and_store_bio_browser(follower, campaign, db, browser_session) -> tuple[str, Exception | None]:
    """Come `fetch_and_store_bio` ma via browser. Scrive gli STESSI campi Follower +
    upsert_lead. NON consuma il cap API (nessun user_info_v1).

    Ritorna (outcome, err):
      'done' | 'private' | 'not_found' | 'soft_block' | 'network' | 'error' | 'blocked'
    """
    from app.utils.contact_extract import extract_contacts
    from app.services.scraper import upsert_lead

    username = follower.username
    try:
        raw_page = await browser_session.page._get_page()
    except Exception as e:
        return "network", e

    try:
        user = await _capture_web_profile_info(raw_page, username)
    except Exception as e:
        es = str(e).lower()
        if any(k in es for k in ("timeout", "net::", "connection", "proxy", "closed")):
            return "network", e
        return "error", e

    if user is None:
        # Nessun dato: profilo inesistente o parsing a vuoto. Skip non fatale.
        return "not_found", None
    if isinstance(user, dict) and user.get("__blocked"):
        return "blocked", Exception(f"interstiziale IG: {user['__blocked']}")
    if isinstance(user, dict) and user.get("__status"):
        st = user["__status"]
        # 429/401/403 dal web = soft-block/rate: il chiamante rallenta o pausa.
        if st in (429, 401, 403):
            return "soft_block", Exception(f"web_profile_info HTTP {st}")
        return "error", Exception(f"web_profile_info HTTP {st}")

    shim = web_user_to_shim(user)

    # Arricchimento contatti: web_profile_info NON espone email/telefono business
    # (business_email=null). Li prendiamo da /api/v1/users/{pk}/info/ (public_email/
    # public_phone_number) con un in-page fetch web-autenticato. Senza questo, il
    # motore browser perde ~95% delle email (verificato sul campo il 08/07).
    # `user` e' il payload GIA' scaricato: serve al gate professional, che decide
    # senza aggiungere nessuna richiesta.
    if contatti_richiesti(campaign, user):
        info = await _fetch_public_contact_inpage(raw_page, shim.pk)
        if isinstance(info, dict) and info.get("__rate_limited"):
            # /info/ rate-limitato: NON ingoiare (era il bug INFO-1/PR-01). Propaga
            # come soft_block cosi' il chiamante rilascia il claim e fa backoff/escalation.
            st = info["__rate_limited"]
            return "soft_block", Exception(f"/info/ HTTP {st}")
        if info:
            shim.public_email = info.get("public_email") or shim.public_email
            shim.public_phone_number = info.get("public_phone_number") or shim.public_phone_number
            shim.contact_phone_number = info.get("contact_phone_number") or shim.contact_phone_number
            shim.public_phone_country_code = (
                info.get("public_phone_country_code") or shim.public_phone_country_code
            )

    contacts = extract_contacts(shim)

    # Aggiorna full_name se il web lo espone e in DB manca (la Fase Lista a volte no).
    if shim.full_name and not follower.full_name:
        follower.full_name = shim.full_name
    follower.biography = shim.biography or None
    follower.is_verified = bool(shim.is_verified)
    follower.is_private = bool(shim.is_private)
    follower.follower_count = shim.follower_count
    follower.following_count = shim.following_count
    ext = shim.external_url
    follower.external_url = contacts.external_url or (str(ext) if ext else None)
    follower.phone = contacts.phone
    follower.email = contacts.email
    follower.whatsapp = contacts.whatsapp
    follower.bio_links = _json.dumps(contacts.bio_links) if contacts.bio_links else None
    follower.contact_source = _json.dumps(contacts.sources) if contacts.sources else None
    follower.status = FollowerStatus.bio_scraped
    follower.locked_by_account_id = None   # C2: libera il claim atomico (Task 4)
    follower.locked_at = None
    await db.commit()

    await upsert_lead(
        db,
        ig_user_id=follower.ig_user_id,
        username=follower.username,
        full_name=follower.full_name,
        biography=follower.biography,
        contacts=contacts,
        campaign=campaign,
        account=None,  # via browser: nessun account API attribuibile alla lookup
    )

    logger.info(f"[BioBrowser] @{username} bio via browser (no cap API)")
    return "done", None


# ── Costanti condivise dei ritardi hardcoded (Fase Bio via browser) ──
# Usate SIA dal codice che dorme davvero (human_profile_pause, maybe_micro_scroll)
# SIA dal budget CALCOLATO qui sotto (expected_delay_budget_s, worst_case_delay_budget_s):
# un'unica fonte, cosi' chi taglia una pausa per "recuperare i secondi liberati
# dall'inversione A.1" la taglia anche nella guardia, invece di lasciarla verde
# per una costante duplicata e dimenticata in due punti del file.
HUMAN_PAUSE_MIN_S = 5.0
HUMAN_PAUSE_MAX_S = 10.0
PUBLIC_SCROLL_MIN_S = 6.0   # ramo pubblico di maybe_micro_scroll -- non e' un settings
PUBLIC_SCROLL_MAX_S = 10.0
OPEN_POST_MIN_S = 3.0       # apertura di un post pubblico dentro maybe_micro_scroll
OPEN_POST_MAX_S = 9.0

# Baseline dello spec (Passo 4, S4.6): tempo TOTALE per profilo misurato PRIMA
# dell'inversione sorgenti (A.1). Dentro quel numero c'era anche l'attesa della
# sorgente dati: prima dell'inversione web_profile_info non arrivava MAI in
# modo passivo (0/65 misurato), quindi si consumava sempre l'intero tetto
# bio_browser_source_wait_s (~8s); il comportamento (pause/scroll/reel) valeva
# il resto, ~7.5s. 8 + 7.5 = 15.5.
BIO_PROFILE_TOTAL_BASELINE_S = 15.5
# Dopo l'inversione A.1 la sorgente giusta (GraphQL passivo) arriva quasi
# subito: mediana di attesa residua ~4s (misurata). Il totale per profilo non
# deve calare -- e' il vincolo dello spec -- quindi il comportamento deve
# SALIRE per compensare i secondi liberati dall'attesa: 15.5 - 4.0 = 11.5.
# Confrontare un budget di solo comportamento contro il baseline TOTALE (15.5)
# sarebbe scorretto: chiederebbe al comportamento di crescere di 8s invece dei
# ~4s che l'inversione ha davvero liberato.
BIO_SOURCE_WAIT_AFTER_INVERSION_S = 4.0
BIO_BEHAVIOUR_FLOOR_S = BIO_PROFILE_TOTAL_BASELINE_S - BIO_SOURCE_WAIT_AFTER_INVERSION_S  # 11.5


def expected_delay_budget_s(cfg=None) -> float:
    """Valore atteso del ritardo comportamentale per profilo nella Fase Bio
    via browser, sommando i contributi configurati in app.config.Settings.
    Pura: nessun I/O, nessun sleep, nessun random -- serve a verificare CHE il
    budget di ritardo configurato non scenda sotto BIO_BEHAVIOUR_FLOOR_S,
    non a misurare un tempo reale di sessione (per quello serve un log su prod).

    ATTENZIONE alla struttura reale del ciclo (righe ~1020-1048): la pausa reel
    e human_profile_pause() sono in un if/else, cioe' ALTERNATIVE, non additive
    -- sul profilo in cui scatta la pausa reel, la pausa umana NON viene
    eseguita. Il micro-scroll invece e' FUORI dall'if/else e si somma sempre.
    Stesso identico blocco if/else in browser_import.py (righe ~502-515): questa
    funzione legge solo i settings condivisi, quindi la stima vale per entrambi
    i motori anche se qui e' usata solo per la Fase Bio.

    NON include bio_browser_source_wait_s: quello e' un TETTO (si esce appena
    arriva la sorgente GraphQL passiva, mediana ~4s dopo l'inversione A.1), non
    un'attesa garantita -- contarlo gonfierebbe il budget con tempo che nella
    pratica non viene speso.

    QUESTA E' UNA SOGLIA DI GUARDIA, NON UNA STIMA DI CAPACITY PLANNING: il
    tempo reale e' SISTEMATICAMENTE MAGGIORE di quello modellato qui, mai
    minore. Il ramo reel in particolare sottostima apposta (sottostima =
    conservativo per una guardia-minimo): questa funzione conta solo
    avg_n_reels * avg_dwell_s, ma InstagramPage.browse_reels() reale spende
    anche ~1.5-3.0s (click sul link nav) o ~1.8-3.2s (goto diretto) di
    navigazione verso /reels/ UNA VOLTA per pausa (instagram_page.py:1302 e
    :1308), piu' ~0.2-0.6s di assestamento scroll per OGNI reel guardato
    (instagram_page.py:1345). Chi riusa questo numero per stimare quanto dura
    una sessione (non per la guardia sul budget minimo) deve sommarci questi
    contributi a parte, o sottostimera' la durata reale.

    cfg: oggetto settings da usare al posto del default globale (per iniettare
    valori finti nei test). Deve esporre gli stessi attributi di app.config
    usati qui sotto.
    """
    s = cfg if cfg is not None else settings

    def _mid(lo: float, hi: float) -> float:
        return (lo + hi) / 2.0

    # 1) Pausa umana tra profili quando NON scatta la pausa reel: uniform(5,10)s.
    pause_s = _mid(HUMAN_PAUSE_MIN_S, HUMAN_PAUSE_MAX_S)

    # 2) Micro-scroll, su ~scroll_ratio dei profili -- fuori dall'if/else pausa
    #    umana/reel, quindi additivo indipendentemente da quale delle due scatta.
    #    Il ramo privato (solo header, ~4-5s) e il ramo pubblico (griglia post,
    #    ~6-10s) hanno durate diverse: prendiamo il ramo PIU' BASSO come
    #    garanzia inferiore. Il budget deve essere una soglia che il
    #    comportamento reale supera sempre, non una media pesata sul mix
    #    privato/pubblico (che qui non e' nota a priori).
    private_scroll_s = _mid(s.bio_browser_scroll_min_s, s.bio_browser_scroll_max_s)
    public_scroll_s = _mid(PUBLIC_SCROLL_MIN_S, PUBLIC_SCROLL_MAX_S)
    conservative_scroll_s = min(private_scroll_s, public_scroll_s)
    scroll_contrib_s = s.bio_browser_scroll_ratio * conservative_scroll_s

    # 3) Apertura di un post pubblico: capita solo dentro il ramo scroll pubblico,
    #    quindi la probabilita' e' composta (scroll_ratio * open_post_ratio).
    open_post_s = _mid(OPEN_POST_MIN_S, OPEN_POST_MAX_S)
    open_post_contrib_s = s.bio_browser_scroll_ratio * s.bio_browser_open_post_ratio * open_post_s

    # 4) Pausa reel VS pausa umana: sono ALTERNATIVE (if/else), non additive.
    #    Il profilo su cui scatta la pausa reel non fa human_profile_pause().
    #    P(reel) ~= 1/E[every]: piu' la cadenza e' rada, meno spesso la pausa
    #    reel sostituisce quella umana. every_min=0 di default: la cadenza
    #    campionata puo' uscire 0 o 1, cioe' la pausa reel puo' scattare A OGNI
    #    profilo (worst case, non un'ipotesi remota). Con E[every] <= 1 il
    #    complemento (E[every]-1)/E[every] diventerebbe <= 0 (o la formula
    #    esploderebbe a every=0): trattiamo esplicitamente E[every] <= 1 come
    #    "reel sempre" (P(reel)=1, P(pausa umana)=0) invece di nasconderlo
    #    dietro una media. E con count_min=0/dwell_min_s=0.0 la pausa reel puo'
    #    valere PROPRIO 0 secondi: il budget deve renderlo visibile (contributo
    #    -> 0), non compensarlo con la pausa umana che in quel caso non scatta.
    avg_n_reels = _mid(s.bio_browser_reels_count_min, s.bio_browser_reels_count_max)
    avg_dwell_s = _mid(s.bio_browser_reels_dwell_min_s, s.bio_browser_reels_dwell_max_s)
    avg_every = _mid(s.bio_browser_reels_every_min, s.bio_browser_reels_every_max)
    if avg_every <= 1.0:
        p_reel = 1.0
    else:
        p_reel = 1.0 / avg_every
    p_human = 1.0 - p_reel
    reel_or_pause_contrib_s = p_human * pause_s + p_reel * (avg_n_reels * avg_dwell_s)

    return reel_or_pause_contrib_s + scroll_contrib_s + open_post_contrib_s


def worst_case_delay_budget_s(cfg=None) -> float:
    """Caso peggiore (NON un valore atteso): ogni sorteggio esce al suo
    estremo piu' sfavorevole -- pausa umana al minimo, cadenza/conteggio/dwell
    reel ai minimi configurati, micro-scroll che non scatta mai (possibile con
    qualunque scroll_ratio < 1, quindi lo escludiamo a prescindere dal suo
    valore). Non entra in nessuna asserzione di budget: serve a documentare
    quanto puo' scendere il ritardo reale con i minimi odierni, come argomento
    per il task B.1. Pura, stessa firma/uso di expected_delay_budget_s.
    """
    s = cfg if cfg is not None else settings

    n_reels_min = s.bio_browser_reels_count_min
    dwell_min_s = s.bio_browser_reels_dwell_min_s
    every_min = s.bio_browser_reels_every_min
    reel_block_s = n_reels_min * dwell_min_s

    if every_min <= 1:
        # Cadenza al minimo puo' essere 0 o 1: la pausa reel scatta ad OGNI
        # profilo, la pausa umana non viene mai eseguita in questo scenario
        # deterministico (stesso caso limite di expected_delay_budget_s).
        return reel_block_s

    pause_block_s = (every_min - 1) * HUMAN_PAUSE_MIN_S
    return (pause_block_s + reel_block_s) / every_min


async def human_profile_pause() -> None:
    """Pausa tra un profilo e l'altro: 5-10s. La vecchia sosta stazionaria
    occasionale (12% di probabilita', 15-45s "distrazione") e' stata rimossa:
    stare fermi ripetutamente e' proprio il pattern da evitare. Al suo posto,
    dopo un numero random di profili (0-10), `scrape_bios_browser_session`
    intercala una pausa ATTIVA sui reel (`InstagramPage.browse_reels`) — qualcosa
    che un account vero farebbe davvero, non uno stallo."""
    await asyncio.sleep(random.uniform(HUMAN_PAUSE_MIN_S, HUMAN_PAUSE_MAX_S))


async def maybe_micro_scroll(session, *, is_private: bool = False, rng=None) -> bool:
    """Scroll sul profilo aperto, ~bio_browser_scroll_ratio dei profili.

    PRIVATO: scroll breve (~4-5s, solo l'header) — non c'e' una griglia di post
    da guardare. PUBBLICO: scroll piu' lungo e deciso (~6-10s) e, con
    probabilita' `bio_browser_open_post_ratio`, apre UN post (mai un reel o una
    storia qui) e lo guarda un attimo prima di tornare indietro.

    Difensivo: non solleva. Ritorna True se ha scrollato."""
    r = rng or random
    if r.random() >= settings.bio_browser_scroll_ratio:
        return False
    try:
        raw_page = await session.page._get_page()

        if is_private:
            dur = r.uniform(settings.bio_browser_scroll_min_s, settings.bio_browser_scroll_max_s)
            steps = max(1, int(dur))
            for _ in range(steps):
                await raw_page.evaluate("window.scrollBy({top: 300, behavior: 'smooth'})")
                await asyncio.sleep(1.0)
            return True

        # Pubblico: scroll piu' lungo/deciso (c'e' davvero una griglia di post).
        dur = r.uniform(PUBLIC_SCROLL_MIN_S, PUBLIC_SCROLL_MAX_S)
        steps = max(1, int(dur / 1.5))
        for _ in range(steps):
            await raw_page.evaluate("window.scrollBy({top: 400, behavior: 'smooth'})")
            await asyncio.sleep(1.5)

        if r.random() < settings.bio_browser_open_post_ratio:
            try:
                post_link = raw_page.locator('article a[href*="/p/"]').first
                if await post_link.count() > 0:
                    await post_link.click(timeout=3000)
                    await asyncio.sleep(r.uniform(OPEN_POST_MIN_S, OPEN_POST_MAX_S))
                    try:
                        await raw_page.go_back()
                    except Exception:
                        await raw_page.keyboard.press("Escape")
            except Exception as e:
                logger.debug(f"[BioBrowser] apertura post pubblico saltata ({type(e).__name__}: {e})")

        return True
    except Exception as e:
        logger.debug(f"[BioBrowser] micro-scroll saltato ({type(e).__name__}: {e})")
        return False


async def claim_next_pending(db, campaign_id: str, account_id: str):
    """Claima atomicamente un Follower pending non lockato per questo account.
    Rilascia prima gli stale lock della campagna (sessioni morte). Ritorna il
    Follower claimato o None. Optimistic lock: safe con più account paralleli
    (SQLite WAL / Postgres). Stesso schema del claim DM in campaign_orchestrator.
    """
    from sqlalchemy import select
    from app.models.follower import Follower, FollowerStatus
    from app.services.campaign_orchestrator import LOCK_TIMEOUT_MINUTES

    stale_cutoff = datetime.utcnow() - timedelta(minutes=LOCK_TIMEOUT_MINUTES)
    await db.execute(
        update(Follower).where(
            Follower.campaign_id == campaign_id,
            Follower.status == FollowerStatus.pending,
            Follower.locked_by_account_id.isnot(None),
            Follower.locked_at < stale_cutoff,
        ).values(locked_by_account_id=None, locked_at=None)
    )
    await db.commit()

    for _ in range(25):  # ritenta se un altro account claima tra SELECT e UPDATE
        follower = (await db.execute(
            select(Follower).where(
                Follower.campaign_id == campaign_id,
                Follower.status == FollowerStatus.pending,
                Follower.locked_by_account_id.is_(None),
            ).limit(1)
        )).scalar_one_or_none()
        if follower is None:
            return None
        claim = await db.execute(
            update(Follower).where(
                Follower.id == follower.id,
                Follower.locked_by_account_id.is_(None),
            ).values(locked_by_account_id=account_id, locked_at=datetime.utcnow())
        )
        await db.commit()
        if claim.rowcount == 1:
            await db.refresh(follower)
            return follower
    return None


async def _maybe_complete_browser_bio(campaign_id: str) -> bool:
    """Specchio del completamento lato API (`scrape_bios.py:326-329`): quando il pool
    globale di pending e' esaurito OPPURE il target e' raggiunto, porta la campagna
    a 'ready' ed emette 'scrape_complete'. Senza questo il path browser non ha MAI
    un modo di uscire da 'scraping' (era il bug: la fase restava bloccata per sempre).

    Conta i pending sull'INTERA campagna (non per-account): con piu' account paralleli
    e' l'ultimo che svuota il pool globale (o fa scattare il target) a completare;
    finche' un altro account tiene ancora righe pending (anche locked) la campagna
    resta aperta, e completera' quando quell'account a sua volta esaurira' il pool.

    L'UPDATE e' atomico e condizionato (`status IN (scraping, scraping_break)`) con
    controllo su `rowcount`: se piu' account/task arrivano qui in corsa, SOLO uno
    vince la transizione e quindi SOLO uno emette l'evento (no doppio scrape_complete).

    Non tocca `scrape_completed_at`/`scrape_outcome` (di competenza della Fase Lista,
    coerente col path API che a sua volta non li tocca in questo punto).
    """
    from sqlalchemy import select, func
    from app.models.campaign import Campaign, CampaignStatus, SCRAPING_ACTIVE_STATES, bio_done_status
    from app.models.follower import Follower, FollowerStatus
    from app.utils.events import emit as emit_event

    async with AsyncSessionLocal() as db:
        campaign = (await db.execute(
            select(Campaign).where(Campaign.id == campaign_id)
        )).scalar_one_or_none()
        if campaign is None:
            return False

        pending_count = await db.scalar(
            select(func.count()).select_from(Follower).where(
                Follower.campaign_id == campaign_id,
                Follower.status == FollowerStatus.pending,
            )
        ) or 0
        bio_done = await db.scalar(
            select(func.count()).select_from(Follower).where(
                Follower.campaign_id == campaign_id,
                Follower.status == FollowerStatus.bio_scraped,
            )
        ) or 0

        target_reached = campaign.bio_target is not None and bio_done >= campaign.bio_target
        if pending_count > 0 and not target_reached:
            return False  # altro account sta ancora smaltendo il pool: non e' finita

        # Se il DM gira in parallelo -> 'running' (tiene vivi i worker DM), altrimenti
        # 'ready'. Target calcolato dallo stato appena letto in questa txn.
        target_status = bio_done_status(campaign.status)
        result = await db.execute(
            update(Campaign).where(
                Campaign.id == campaign_id,
                Campaign.status.in_(SCRAPING_ACTIVE_STATES),
            ).values(status=target_status, updated_at=datetime.utcnow())
        )
        await db.commit()
        if result.rowcount == 1:
            emit_event(campaign_id, "scrape_complete", f"Fase Bio (browser) completata: {bio_done} bio")
            return True
        return False  # gia' transitata da un'altra race (o non era in uno stato scraping attivo)


async def _resilient_release(db, follower_id, *, status=None, skip_reason=None):
    """Rilascia il lock (ed eventualmente marca lo status) resiliente a una sessione
    avvelenata da un commit fallito a monte: rollback preventivo + UPDATE per id
    (non dipende dallo stato dell'oggetto ORM, che dopo un flush fallito e' inservibile).

    Perche' esiste: `fetch_and_store_bio_browser` fa il proprio `db.commit()` su una
    riga gia' modificata (bio scritta). Se quel commit fallisce nel flush (errore
    transitorio Postgres/Supabase: deadlock, statement timeout, connessione caduta),
    la AsyncSession resta in stato PendingRollback: qualunque `db.commit()`
    successivo sulla STESSA sessione fallisce a sua volta, propagando all'except
    esterno (`return 300`) e lasciando il follower pending+LOCKED (il lock era gia'
    stato committato dal claim) fino al cron di stale-lock (20 min). Un rollback
    preventivo + UPDATE per id (non tocca l'oggetto ORM, expired dopo il rollback)
    rende il rilascio del lock indipendente dall'esito del commit di `fetch`.
    """
    from app.models.follower import Follower
    try:
        await db.rollback()
    except Exception:
        pass
    vals = {"locked_by_account_id": None, "locked_at": None, "updated_at": datetime.utcnow()}
    if status is not None:
        vals["status"] = status
    if skip_reason is not None:
        vals["skip_reason"] = skip_reason
    await db.execute(update(Follower).where(Follower.id == follower_id).values(**vals))
    await db.commit()


def _soft_block_redis_key(campaign_id: str, account_id: str) -> str:
    return f"biobrowser:softblock:{campaign_id}:{account_id}"


async def _soft_block_incr(campaign_id: str, account_id: str) -> int:
    """Incrementa il contatore soft-block consecutivi per (campagna, account) in Redis
    (TTL 2h: se l'account resta buono il conteggio decade). Difensivo: su Redis giu'
    ritorna 1 (non falsa l'escalation ne' blocca)."""
    try:
        redis = await arq.create_pool(arq_redis_settings())
        try:
            key = _soft_block_redis_key(campaign_id, account_id)
            n = await redis.incr(key)
            await redis.expire(key, 7200)
            return int(n)
        finally:
            await redis.aclose()
    except Exception as e:
        logger.debug(f"[BioBrowser] soft-block incr fallito ({type(e).__name__}) — assumo 1")
        return 1


async def _soft_block_reset(campaign_id: str, account_id: str) -> None:
    """Azzera il contatore: l'account e' tornato a scrapare. Difensivo, non solleva."""
    try:
        redis = await arq.create_pool(arq_redis_settings())
        try:
            await redis.delete(_soft_block_redis_key(campaign_id, account_id))
        finally:
            await redis.aclose()
    except Exception:
        pass


async def _pause_campaign_soft_block(campaign_id: str, account_id: str, n: int) -> None:
    """Dopo N soft-block CONSECUTIVI di un account (mirror del guard del path API),
    ferma la Fase Bio e avvisa: stop al `429 -> defer -> 429` infinito.

    Ferma SOLO la bio: se i DM girano in parallelo (scraping_and_running) la
    campagna scende a 'running', cosi' i worker DM continuano. Il 429 e' un
    limite dell'endpoint web_profile_info per IP e riguarda l'account che
    scrapa; l'invio DM gira su un altro account ed e' sano — non c'e' motivo di
    spegnerlo. (Caso reale 14/07: 5x 429 della bio mettevano tutto in 'paused' e
    il worker DM si fermava al giro dopo, a batch non finito.)"""
    from sqlalchemy import select
    from app.models.campaign import Campaign, CampaignStatus, SCRAPING_ACTIVE_STATES
    from app.models.activity_log import ActivityLog
    from app.utils.events import emit as emit_event
    dm_kept = False
    async with AsyncSessionLocal() as db:
        campaign = (await db.execute(
            select(Campaign).where(Campaign.id == campaign_id)
        )).scalar_one_or_none()
        if campaign is not None and campaign.status in SCRAPING_ACTIVE_STATES:
            dm_kept = campaign.status == CampaignStatus.scraping_and_running
            campaign.status = (
                CampaignStatus.running if dm_kept else CampaignStatus.paused
            )
            campaign.updated_at = datetime.utcnow()
            db.add(ActivityLog(
                campaign_id=campaign_id, action="bios_paused_soft_block",
                details=_json.dumps({
                    "account_id": account_id, "consecutive": n,
                    "dm_kept_running": dm_kept,
                }),
            ))
            await db.commit()
    msg = (f"Fase Bio browser in PAUSA: {n} soft-block (429) consecutivi sull'account "
           f"{account_id[:8]} — serve riposo/verifica account."
           + (" I DM continuano." if dm_kept else ""))
    logger.error(f"[BioBrowser] {msg}")
    emit_event(campaign_id, "scrape_stopped", msg, level="error")
    try:
        from app.services import notifier
        asyncio.create_task(notifier.send_telegram(f"[BOT OUTBOUND] {msg}", level="error"))
    except Exception:
        pass


async def _isolate_account_and_pause(campaign_id: str, account_id: str, exc: Exception) -> None:
    """Isola l'account (challenge/ban/sessione scaduta) e mette la campagna in pausa —
    mirror di isolate_challenged_account per il canale browser: un account fatale NON
    deve restare 'active' (ogni retry ri-fallirebbe e martellerebbe IG)."""
    from sqlalchemy import select
    from app.models.campaign import Campaign, CampaignStatus, SCRAPING_ACTIVE_STATES
    from app.models.account import InstagramAccount, AccountStatus
    from app.models.activity_log import ActivityLog
    from app.utils.events import emit as emit_event
    exc_name = type(exc).__name__
    async with AsyncSessionLocal() as db:
        acc = (await db.execute(
            select(InstagramAccount).where(InstagramAccount.id == account_id)
        )).scalar_one_or_none()
        label = acc.username if acc else account_id[:8]
        if acc is not None:
            acc.status = AccountStatus.challenge_required  # non-active: pulled out
        campaign = (await db.execute(
            select(Campaign).where(Campaign.id == campaign_id)
        )).scalar_one_or_none()
        if campaign is not None and campaign.status in SCRAPING_ACTIVE_STATES:
            campaign.status = CampaignStatus.paused
            campaign.updated_at = datetime.utcnow()
        db.add(ActivityLog(
            campaign_id=campaign_id, action="bios_account_isolated",
            details=_json.dumps({"account": label, "exc": exc_name}),
        ))
        await db.commit()
    msg = (f"Fase Bio browser: account @{label} isolato ({exc_name}) — campagna in pausa. "
           f"Login manuale/verifica prima di riprendere.")
    logger.error(f"[BioBrowser] {msg}")
    emit_event(campaign_id, "scrape_stopped", msg, level="error")
    try:
        from app.services import notifier
        asyncio.create_task(notifier.send_telegram(f"[BOT OUTBOUND] {msg}", level="error"))
    except Exception:
        pass


async def scrape_bios_browser_session(campaign_id: str, account_id: str) -> int | None:
    """Una mini-sessione browser per UN account: apre, scrapa fino a un cap di
    profili claimati (pool disgiunto via claim_next_pending), chiude. Ritorna i
    secondi di defer per la pausa lunga anti-block, o None se non c'è più lavoro.
    Job corto: mai oltre job_timeout. Difensiva sui singoli profili."""
    from sqlalchemy import select, func
    from app.models.campaign import Campaign, CampaignStatus, SCRAPING_ACTIVE_STATES
    from app.models.follower import Follower, FollowerStatus
    from app.utils.events import emit as emit_event

    async with AsyncSessionLocal() as db:
        campaign = (await db.execute(
            select(Campaign).where(Campaign.id == campaign_id)
        )).scalar_one_or_none()
        if campaign is None or campaign.status not in SCRAPING_ACTIVE_STATES:
            return None
        if await is_halted(db):
            return None

        done = await db.scalar(
            select(func.count()).select_from(Follower).where(
                Follower.campaign_id == campaign_id,
                Follower.status == FollowerStatus.bio_scraped))
        if not bio_should_continue(campaign.bio_target, done or 0):
            await _maybe_complete_browser_bio(campaign_id)
            return None

    cap = pick_session_cap(settings.bio_browser_session_cap_min, settings.bio_browser_session_cap_max)
    max_iterations = cap * MAX_SESSION_ITERATIONS_MULTIPLIER
    done_count = 0
    iterations = 0
    target_reached = False
    session = None
    # Cadenza pausa attiva sui reel: dopo un numero random di profili (default 0-10),
    # rimpiazza lo standing-still rimosso da `human_profile_pause`. Ripescata dopo
    # ogni pausa reel cosi' la cadenza non e' sempre identica tra una pausa e l'altra.
    reels_cadence_target = random.randint(
        settings.bio_browser_reels_every_min, settings.bio_browser_reels_every_max
    )
    profiles_since_reels_break = 0
    try:
        session = BrowserSession(account_id, headless=settings.bio_browser_headless)
        await session.open()
        # allow_login=False: lo scraping NON fa MAI login automatico (ban risk). Se la
        # sessione e' scaduta -> AccountSessionExpiredError -> isolamento (except sotto).
        await session.page.ensure_logged_in(account_id, allow_login=False)

        while done_count < cap and iterations < max_iterations:
            follower_is_private = False
            async with AsyncSessionLocal() as db:
                if await is_halted(db):
                    return None
                campaign = (await db.execute(
                    select(Campaign).where(Campaign.id == campaign_id)
                )).scalar_one_or_none()
                if campaign is None or campaign.status not in SCRAPING_ACTIVE_STATES:
                    return None

                # Re-check del target globale ad ogni iterazione: con piu' sessioni
                # per-account in parallelo il pre-check fatto prima del loop puo'
                # essere superato nel frattempo (evita overshoot su bio_target).
                bio_done_total = await db.scalar(
                    select(func.count()).select_from(Follower).where(
                        Follower.campaign_id == campaign_id,
                        Follower.status == FollowerStatus.bio_scraped))
                if not bio_should_continue(campaign.bio_target, bio_done_total or 0):
                    target_reached = True
                    break

                follower = await claim_next_pending(db, campaign_id, account_id)
                if follower is None:
                    await _maybe_complete_browser_bio(campaign_id)
                    return None  # pool globale esaurito

                # Cattura username/id SUBITO dopo il claim (l'oggetto e' appena stato
                # refresh-ato: accesso sicuro, zero query). Se il commit di
                # `fetch_and_store_bio_browser` fallisce su un errore transitorio a
                # monte, la sessione entra in stato PendingRollback e QUALSIASI accesso
                # successivo agli attributi dell'oggetto `follower` (persino un
                # attributo gia' caricato come `.id`) rilancia PendingRollbackError —
                # non solo dopo un rollback esplicito, ma dal momento stesso del flush
                # fallito. Usare da qui in poi solo le variabili locali `uname`/`fid`,
                # mai piu' `follower.<attr>`, evita di ri-toccare l'oggetto avvelenato.
                uname, fid = follower.username, follower.id
                try:
                    outcome, err = await fetch_and_store_bio_browser(follower, campaign, db, session)
                except Exception as e:
                    logger.warning(f"[BioBrowser] @{uname} errore inatteso ({e}) — skip")
                    outcome, err = "error", e

                iterations += 1
                if outcome == "done":
                    done_count += 1
                    if done_count == 1:
                        # L'account scrapa: azzera la streak di soft-block consecutivi (Fix C).
                        await _soft_block_reset(campaign_id, account_id)
                    # Sicuro leggere l'attributo qui (a differenza del resto di questo
                    # blocco, che usa solo uname/fid): si arriva a outcome == 'done'
                    # SOLO se il db.commit() dentro fetch_and_store_bio_browser e'
                    # andato a buon fine (altrimenti l'eccezione sarebbe stata
                    # catturata sopra come 'error') — quindi la sessione non e'
                    # avvelenata e l'oggetto non e' expired (expire_on_commit=False).
                    # Serve a decidere lo scroll pubblico/privato piu' sotto.
                    follower_is_private = bool(follower.is_private)
                    emit_event(campaign_id, "scrape_progress", f"@{uname} bio via browser")
                elif outcome == "blocked":
                    # NON marcare il follower: resta 'pending' e verra' ripreso quando
                    # l'account sara' di nuovo pulito. Isola l'account e ferma tutto.
                    await _resilient_release(db, fid)
                    await _isolate_account_and_pause(campaign_id, account_id, err)
                    return None
                elif outcome in ("not_found", "private", "error"):
                    await _resilient_release(
                        db, fid, status=FollowerStatus.skipped, skip_reason=f"browser_{outcome}"
                    )
                elif outcome == "soft_block":
                    # Non bruciare i pending: rilascia il claim e fai backoff invece di
                    # `return None`. Fix C: contatore soft-block CONSECUTIVI per account
                    # (Redis, azzerato appena l'account torna a scrapare — vedi done sopra).
                    # Backoff CRESCENTE con la streak; dopo N consecutivi -> pausa campagna
                    # (mirror del guard del path API), stop al `429 -> defer -> 429` infinito.
                    await _resilient_release(db, fid)
                    logger.warning(f"[BioBrowser] soft-block su @{uname}: {err} — backoff")
                    emit_event(
                        campaign_id, "scrape_progress",
                        f"@{uname}: soft-block (429) — backoff", level="warn",
                    )
                    n_sb = await _soft_block_incr(campaign_id, account_id)
                    if n_sb >= settings.bio_browser_soft_block_pause_threshold:
                        await _pause_campaign_soft_block(campaign_id, account_id, n_sb)
                        return None  # campagna in pausa: stop, niente altro retry
                    base = random.randint(900, 1800)          # 15-30 min alla 1a
                    return min(3600, base * n_sb)             # cresce con la streak, cap 60 min
                elif outcome == "network":
                    # Stessa logica del soft_block: rilascia il claim, retry breve
                    # invece di sideline silenzioso.
                    await _resilient_release(db, fid)
                    logger.warning(f"[BioBrowser] errore rete su @{uname}: {err} — retry breve")
                    return 180  # 3 min
                else:
                    # Outcome inatteso (fetch_and_store_bio_browser cambiato senza
                    # aggiornare qui): non stranare il follower (pending+locked per
                    # sempre). Skip difensivo, libera il lock, conta come iterazione.
                    await _resilient_release(
                        db, fid, status=FollowerStatus.skipped, skip_reason="browser_unknown"
                    )
                    logger.warning(f"[BioBrowser] @{uname} outcome inatteso '{outcome}' — skip difensivo")

            # Nota: soft_block/network hanno gia' fatto `return` sopra — questo punto
            # e' raggiunto solo dagli outcome che continuano la mini-sessione
            # (done/not_found/private/error/unknown), quindi la pausa reel non gira
            # mai su quei due path di stop.
            await maybe_micro_scroll(session, is_private=follower_is_private)

            profiles_since_reels_break += 1
            if profiles_since_reels_break >= reels_cadence_target:
                try:
                    n_reels = random.randint(
                        settings.bio_browser_reels_count_min,
                        settings.bio_browser_reels_count_max,
                    )
                    logger.info(
                        f"[BioBrowser] pausa attiva sui reel: {n_reels} reel "
                        f"(dopo {reels_cadence_target} profili)"
                    )
                    await session.page.browse_reels(
                        n_reels,
                        dwell_min_s=settings.bio_browser_reels_dwell_min_s,
                        dwell_max_s=settings.bio_browser_reels_dwell_max_s,
                    )
                except Exception as e:
                    logger.warning(
                        f"[BioBrowser] pausa attiva sui reel fallita "
                        f"({type(e).__name__}: {e}) — ignorata"
                    )
                profiles_since_reels_break = 0
                reels_cadence_target = random.randint(
                    settings.bio_browser_reels_every_min, settings.bio_browser_reels_every_max
                )
            else:
                await human_profile_pause()

        if target_reached:
            await _maybe_complete_browser_bio(campaign_id)
            return None

        if done_count >= cap:
            # Prima di pausare, prova a completare: se un solo account ha appena
            # drenato l'ULTIMO pending esattamente al cap, il `while` esce da qui
            # (non da `claim -> None`) e senza questo check la campagna resterebbe
            # 'scraping' con pool vuoto per l'intera pausa anti-block (30-45 min) a
            # vuoto, invece di andare subito 'ready'.
            if await _maybe_complete_browser_bio(campaign_id):
                return None  # pool drenato esattamente al cap -> completa subito, niente pausa inutile
            # cap raggiunto → pausa lunga anti-block via defer
            minutes = random.uniform(
                getattr(campaign, "scrape_break_minutes_min", 30) or 30,
                getattr(campaign, "scrape_break_minutes_max", 45) or 45,
            )
            emit_event(campaign_id, "scrape_break", f"Pausa bio browser {int(minutes)} min")
            return max(60, int(minutes * 60))

        # Backstop iterazioni raggiunto senza completare il cap di bio reali: pool
        # skip-heavy (private/not_found/error). Defer BREVE (non la pausa lunga
        # anti-block) cosi' la prossima mini-sessione riprende subito a smaltire
        # il pool invece di aspettare 30-45 min senza motivo.
        logger.info(
            f"[BioBrowser] backstop iterazioni ({iterations}/{max_iterations}) "
            f"raggiunto con solo {done_count}/{cap} bio reali — pool skip-heavy, defer breve"
        )
        return 60
    except (AccountChallengeError, AccountBannedError, AccountSessionExpiredError) as e:
        # Fix A: challenge/ban/sessione scaduta = FATALE per l'account. NON ritentare
        # (era `return 300` = retry ogni 5 min all'infinito): isola l'account e pausa
        # la campagna. Il login manuale/verifica lo fa l'operatore.
        logger.error(f"[BioBrowser] account fatale @{account_id[:8]}: {type(e).__name__} — isolo + pauso")
        await _isolate_account_and_pause(campaign_id, account_id, e)
        return None
    except Exception as e:
        logger.warning(f"[BioBrowser] mini-sessione @{account_id[:8]} fallita ({type(e).__name__}: {e})")
        # errore d'apertura/login: breve retry via defer, non perde i pending
        return 300
    finally:
        if session is not None:
            try:
                await session.close()
            except Exception:
                pass


async def _scrape_batch(campaign, db, browser_session, count: int, account_id: str) -> int:
    """Scrapa fino a `count` follower pending via la sessione browser gia' aperta,
    a ritmo umano. Ritorna quante bio estratte. Difensivo: non solleva.

    `account_id` serve SOLO al ramo 'blocked': se il batch gira durante la pausa
    tra sessioni e l'account e' dietro un interstiziale IG, isola l'account cosi'
    il ciclo successivo non lo ripesca (altrimenti continuerebbe a generare
    traffico da dietro il blocco — mirror di scrape_bios_browser_session)."""
    from sqlalchemy import select
    from app.models.follower import Follower

    done = 0
    for _ in range(count):
        follower = (await db.execute(
            select(Follower).where(
                Follower.campaign_id == campaign.id,
                Follower.status == FollowerStatus.pending,
            ).limit(1)
        )).scalar_one_or_none()
        if follower is None:
            break  # niente piu' pending

        try:
            outcome, err = await fetch_and_store_bio_browser(follower, campaign, db, browser_session)
        except Exception as e:
            logger.warning(f"[BioBrowser] batch: errore inatteso su @{follower.username} ({e}) — stop batch")
            break

        if outcome == "done":
            done += 1
        elif outcome == "blocked":
            # Come sopra: nessuna marcatura. In piu' (a differenza del vecchio
            # solo-break): isola l'account e pausa la campagna, stesso mirror di
            # scrape_bios_browser_session. Senza isolamento il ciclo di pausa
            # successivo ripesca lo stesso account e continua a generare traffico
            # da dietro l'interstiziale — esattamente il fail-mode da chiudere.
            logger.error(f"[BioBrowser] batch fermo: interstiziale IG su @{follower.username}")
            await _isolate_account_and_pause(campaign.id, account_id, err)
            break
        elif outcome in ("not_found", "private", "error"):
            # Skip benigno: marca skipped cosi' non ri-seleziona lo stesso pending
            # (limit(1) senza ORDER BY ritornerebbe lo stesso -> loop).
            follower.status = FollowerStatus.skipped
            follower.skip_reason = f"browser_{outcome}"
            follower.updated_at = datetime.utcnow()
            await db.commit()
        elif outcome in ("soft_block", "network"):
            # 429/soft-block o rete giu': fermati, NON bruciare i pending buoni
            # (restano pending per l'API alla ripresa).
            logger.warning(f"[BioBrowser] batch stop ({outcome}) su @{follower.username}: {err}")
            break

        await human_profile_pause()

    logger.info(f"[BioBrowser] batch pausa: {done} bio estratte")
    return done


async def _scraping_accounts_of_campaign(campaign_id: str):
    """(account_id, username) degli account scraping/both attivi della campagna."""
    from app.database import AsyncSessionLocal
    from sqlalchemy import select
    from app.models.campaign_account import CampaignAccount
    from app.models.account import InstagramAccount, AccountStatus
    from app.utils.roles import SCRAPE_ROLES
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(InstagramAccount.id, InstagramAccount.username)
            .join(CampaignAccount, CampaignAccount.account_id == InstagramAccount.id)
            .where(
                CampaignAccount.campaign_id == campaign_id,
                CampaignAccount.is_active == True,  # noqa: E712
                CampaignAccount.role.in_(SCRAPE_ROLES),
                InstagramAccount.status == AccountStatus.active,
            )
        )).all()
    return [(r[0], r[1]) for r in rows]


def browser_bio_job_id(campaign_id: str, account_id: str) -> str:
    return f"biobrowser:{campaign_id}:{account_id}"


def browser_bio_redis_keys(campaign_id: str, account_id: str) -> tuple[str, str, str]:
    """Mirror di `dm_worker_redis_keys` (work_enqueue.py) per i job Bio browser."""
    job_id = browser_bio_job_id(campaign_id, account_id)
    return (
        f"arq:job:{job_id}",
        f"arq:retry:{job_id}",
        f"arq:in-progress:{job_id}",
    )


async def enqueue_browser_bio_workers(campaign_id: str) -> int:
    """Fan-out: un task ARQ per account scraping, con stagger crescente via
    _defer_by (partenze differite) e _job_id deterministico (dedup, come i DM).

    Stessa disciplina di `_reenqueue_phase` in work_enqueue.py: un job
    parcheggiato in `Retry(defer=...)` (pausa lunga anti-block) resta dedup-ato
    da ARQ sul suo `_job_id` finche' le chiavi `arq:job`/`arq:retry` non
    vengono cancellate. Senza questo, uno stop→resume durante la pausa lunga
    sarebbe un no-op silenzioso (il job non si risveglia mai). Se il job e'
    gia' 'in-progress' (worker vivo in questo momento) NON lo cancelliamo ne'
    lo ri-accodiamo, per non far partire un secondo worker concorrente sullo
    stesso account.

    Ritorna il numero di account ORA schedulati: sia quelli appena accodati
    sia quelli gia' in esecuzione (in-progress), cosi' l'evento "N account in
    parallelo" del chiamante resta veritiero."""
    accounts = await _scraping_accounts_of_campaign(campaign_id)
    if not accounts:
        return 0
    redis = await arq.create_pool(arq_redis_settings())
    lo = min(settings.bio_browser_stagger_min_s, settings.bio_browser_stagger_max_s)
    hi = max(settings.bio_browser_stagger_min_s, settings.bio_browser_stagger_max_s)
    n = 0
    try:
        for idx, (account_id, _username) in enumerate(accounts):
            job_id = browser_bio_job_id(campaign_id, account_id)
            job_key, retry_key, in_progress_key = browser_bio_redis_keys(campaign_id, account_id)
            if await redis.exists(in_progress_key):
                logger.info(f"[BioBrowser] {job_id} gia' in esecuzione — skip enqueue duplicato")
                n += 1
                continue
            await redis.delete(job_key, retry_key)
            defer = 0 if idx == 0 else int(random.uniform(lo, hi) * idx)
            await redis.enqueue_job(
                "browser_bio_account_task",
                campaign_id,
                account_id,
                _job_id=job_id,
                _defer_by=defer,
                _queue_name=ARQ_MAIN_QUEUE,
            )
            n += 1
    finally:
        await redis.aclose()  # non lasciare la connessione ARQ aperta (come le helper in work_enqueue)
    return n


async def run_pause_browser_activity(campaign_id: str, account_id: str, username: str | None = None) -> int:
    """Durante la pausa lunga della Fase Bio: UNA sessione browser coerente su UN account
    = scroll organico (warm-up) + eventuale BLOCCO di profili scrapati. Ritorna i secondi
    spesi (0 se disabilitato/fallito). Difensivo: non solleva mai.

    Apre la PROPRIA db session (via AsyncSessionLocal) SOLO per il batch, cosi' e'
    sicura chiamata in parallelo su piu' account (le sessioni SQLAlchemy async non sono
    concorrenti-safe). Coerenza IP: BrowserSession esce dallo STESSO account.proxy dell'API.
    Un solo login/apertura sessione per account (non per profilo) = comportamento umano.
    """
    do_scroll = settings.warmup_browse_enabled
    do_batch = settings.bio_browser_batch_enabled
    if not do_scroll and not do_batch:
        return 0

    tag = f"@{username}" if username else account_id[:8] + "…"
    start = time.monotonic()
    session = None
    try:
        from app.browser.context_manager import BrowserSession
        session = BrowserSession(account_id, headless=settings.warmup_browse_headless)
        await session.open()
        await session.page.ensure_logged_in(account_id)

        # 1) Scroll organico (warm-up): feed scroll, post, like ~35%.
        if do_scroll:
            scroll_s = int(random.uniform(
                settings.warmup_browse_min_minutes, settings.warmup_browse_max_minutes
            ) * 60)
            logger.info(f"[BioBrowser] {tag}: scroll organico ~{scroll_s}s")

            # like_gate: tetto giornaliero PERSISTITO (scrittura, vettore di
            # blocco proprio). Nessuna db session aperta qui ancora (quella del
            # batch sotto arriva dopo) -- sessione breve per ogni prenotazione,
            # come warmup_browse.run_warmup.
            async def _like_gate() -> bool:
                async with AsyncSessionLocal() as like_db:
                    return await account_manager.reserve_daily_like(like_db, account_id)

            await session.page.browse_feed(scroll_s, like_gate=_like_gate)

        # 2) Blocco di profili scrapati nella STESSA sessione (piu' umano di 1 sporadico).
        if do_batch:
            lo = min(settings.bio_browser_batch_min, settings.bio_browser_batch_max)
            hi = max(settings.bio_browser_batch_min, settings.bio_browser_batch_max)
            n = random.randint(lo, hi)
            logger.info(f"[BioBrowser] {tag}: batch di {n} profili via browser")
            from app.database import AsyncSessionLocal
            from app.models.campaign import Campaign
            from sqlalchemy import select
            async with AsyncSessionLocal() as db:  # db propria = parallel-safe
                campaign = (await db.execute(
                    select(Campaign).where(Campaign.id == campaign_id)
                )).scalar_one_or_none()
                if campaign is not None:
                    await _scrape_batch(campaign, db, session, n, account_id)

        return int(time.monotonic() - start)
    except Exception as e:
        logger.warning(f"[BioBrowser] {tag}: attivita' in pausa fallita ({type(e).__name__}: {e}) — ingoiato")
        return int(time.monotonic() - start)
    finally:
        if session is not None:
            try:
                await session.close()
            except Exception as e:  # pragma: no cover
                logger.debug(f"[BioBrowser] {tag}: close fallita ({e})")


async def run_pause_browser_all_accounts(campaign_id: str) -> int:
    """Ogni account scraping della campagna fa la sua sessione scroll+batch in pausa.
    Parallelo con partenze SCAGLIONATE (offset random 1-3 min tra account, mai tutti
    nello stesso istante) e cap sui browser concorrenti (max_concurrent_browsers).
    Ritorna i secondi totali spesi (0 se disabilitato / nessun account). Difensivo."""
    if not (settings.warmup_browse_enabled or settings.bio_browser_batch_enabled):
        return 0
    accounts = await _scraping_accounts_of_campaign(campaign_id)
    if not accounts:
        return 0

    start = time.monotonic()
    sem = asyncio.Semaphore(max(1, settings.max_concurrent_browsers))

    async def _one(account_id, username, idx):
        # Stagger: parte dopo idx * (1-3 min), cosi' non tutti nello stesso istante.
        if idx:
            await asyncio.sleep(random.uniform(60.0, 180.0) * idx)
        async with sem:
            await run_pause_browser_activity(campaign_id, account_id, username)

    await asyncio.gather(
        *[_one(a, u, i) for i, (a, u) in enumerate(accounts)],
        return_exceptions=True,  # un account che fallisce non blocca gli altri
    )
    logger.info(f"[BioBrowser] pausa: sessioni browser completate su {len(accounts)} account")
    return int(time.monotonic() - start)
