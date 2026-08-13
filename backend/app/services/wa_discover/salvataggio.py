"""Salvataggio idempotente delle chat scoperte dalla Fase A (staging).

Stessa forma di `app/services/inbox_browser/salvataggio.py`: lookup ESPLICITA
(mai un INSERT lasciato parlare al vincolo), fusione che integra e non
sovrascrive, `IntegrityError` gestito come ripiego sulla fusione (due
scansioni concorrenti sulla stessa chat).

La differenza di fondo (spec 5.4, P12): qui la lookup non ha UNA chiave sola,
ne ha DUE che si coprono a vicenda. `wa_discovered_chats` porta
`UniqueConstraint(number_id, chat_title)` e `UniqueConstraint(number_id,
phone_hmac)` perche' in SQL NULL != NULL, e `chat_title` e' NULL proprio nel
caso piu' frequente (39% delle chat di Primero, PoC-5: il titolo E' il numero,
e per P12 non si scrive in chiaro). Cercare solo per titolo o solo per hmac
lascerebbe fuori meta' delle ri-scansioni: una chat scoperta la prima volta
senza titolo (solo hmac, perche' il pannello si e' aperto) e ritrovata la
seconda volta con un titolo vero (rubrica sincronizzata nel frattempo) va
cercata per hmac, non per titolo -- il titolo di prima non esiste ancora.
"""
from __future__ import annotations

from datetime import datetime

from loguru import logger
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from app.models.wa import WaDiscoveredChat
from app.services.wa_discover.classifica import (
    RigaScoperta,
    TIPO_GRUPPO,
    TIPO_IGNOTO,
    TIPO_INDIVIDUALE,
    e_etichetta_mascherata,
    etichetta_visibile,
)
from app.utils.crypto import encrypt
from app.utils.phone_pseudonym import hmac_e164

# Rango del tipo chat in una fusione: 'ignoto' e' l'unico stato che puo'
# avanzare. Tra individuale e gruppo non c'e' un ordine di merito -- se un
# giro successivo porta un segnale diverso da quello gia' salvato, la riga
# non deve oscillare: si tiene la prima classificazione nota. Stessa idea di
# `stato_vincente` nel gemello Instagram: la fusione integra, non ribalta.
_RANGO_TIPO = {TIPO_IGNOTO: 0, TIPO_INDIVIDUALE: 1, TIPO_GRUPPO: 1}


def tipo_vincente(a: str, b: str) -> str:
    """In una fusione un tipo gia' noto non si ribalta; solo 'ignoto' avanza."""
    return a if _RANGO_TIPO.get(a, 0) >= _RANGO_TIPO.get(b, 0) else b


async def _trova_esistente(db, number_id: str, chat_title: str | None,
                            phone_hmac: str | None) -> WaDiscoveredChat | None:
    """La stessa chat puo' essere gia' a DB sotto titolo O sotto hmac (o
    entrambi, dopo una prima fusione): le due unique si coprono a vicenda
    (vedi docstring del modulo), quindi la lookup prova entrambe, non una sola.
    """
    filtri = []
    if chat_title is not None:
        filtri.append(WaDiscoveredChat.chat_title == chat_title)
    if phone_hmac is not None:
        filtri.append(WaDiscoveredChat.phone_hmac == phone_hmac)
    if not filtri:
        # Ne' titolo ne' numero: niente con cui riconoscere una riga gia'
        # salvata. Succede solo quando etichetta_visibile non ha trovato
        # nulla da mostrare (titolo vuoto E numero non letto) -- una riga
        # senza alcuna identita', che quindi non puo' fondersi con nulla.
        return None

    righe = (await db.execute(
        select(WaDiscoveredChat).where(
            WaDiscoveredChat.number_id == number_id, or_(*filtri),
        )
    )).scalars().all()

    if not righe:
        return None
    if len(righe) == 1:
        return righe[0]

    # Caso raro: l'OR combacia con piu' di una riga (es. una scoperta senza
    # titolo con l'hmac X e -- coincidenza -- una scoperta senza numero il cui
    # titolo e' oggi identico all'etichetta mascherata di X). L'hmac e'
    # l'identificatore piu' forte perche' non e' derivato da un titolo: si
    # preferisce, e si logga perche' e' un caso che merita occhio.
    per_hmac = [r for r in righe if phone_hmac is not None and r.phone_hmac == phone_hmac]
    scelta = per_hmac[0] if per_hmac else righe[0]
    logger.warning(
        f"[WaDiscover] number_id={number_id}: la lookup ha trovato {len(righe)} "
        f"righe per (titolo={chat_title!r}, hmac={phone_hmac!r}); scelta id={scelta.id}"
    )
    return scelta


def _fondi(esistente: WaDiscoveredChat, *, etichetta: str | None,
           encrypted_phone: str | None, phone_hmac: str | None,
           riga: RigaScoperta) -> None:
    """Fusione: si integra, non si sovrascrive. Non committa (il chiamante lo fa).

    Un campo gia' valorizzato non si tocca mai, anche se il nuovo giro porta
    un valore diverso (o nessun valore): e' cosi' che un giro in cui il
    pannello non si apre non fa perdere un numero gia' salvato al giro prima
    -- il contrario dell'"integra" richiesto dalla spec 5.3.
    """
    # Il titolo si scrive quando manca, MA anche quando quello salvato e' solo
    # una maschera e il nuovo giro porta un nome vero. E' il caso piu' frequente
    # per un negozio: un contatto non in rubrica ha il numero come titolo (39%
    # delle chat di Primero) e si salva come '+39.....077'; quando il cliente lo
    # salva in rubrica, la ri-scansione porta 'Mario Rossi'. Con la sola regola
    # "non sovrascrivere" quel contatto resterebbe una maschera per sempre, e in
    # Fase B l'operatore approverebbe una lista di maschere invece di nomi.
    # Il verso opposto non accade mai: un nome vero non si degrada a maschera.
    nuovo_e_nome_vero = etichetta is not None and not e_etichetta_mascherata(etichetta)
    if etichetta is not None and (
        esistente.chat_title is None
        or (nuovo_e_nome_vero and e_etichetta_mascherata(esistente.chat_title))
    ):
        esistente.chat_title = etichetta
        esistente.display_name = etichetta
    if esistente.phone_hmac is None and phone_hmac is not None:
        esistente.encrypted_phone = encrypted_phone
        esistente.phone_hmac = phone_hmac
    # Un True non torna mai False: stessa idea di stato_vincente, un progresso
    # non regredisce.
    esistente.numero_leggibile = esistente.numero_leggibile or riga.numero_leggibile
    esistente.tipo_chat = tipo_vincente(esistente.tipo_chat, riga.tipo)
    esistente.updated_at = datetime.utcnow()
    # status NON si tocca qui, apposta: lo muove solo la Fase B (promozione a
    # WaContact / scarto). RigaScoperta non porta uno status -- ogni scoperta
    # della Fase A e' per definizione 'nuovo', e se la fusione lo riscrivesse
    # esplicitamente una riga gia' promossa tornerebbe 'nuovo' e verrebbe
    # promossa una seconda volta.


async def salva_scoperta(db, tenant_id: str, number_id: str, riga: RigaScoperta) -> str:
    """Crea o aggiorna la riga di staging per questa chat. 'creata' | 'aggiornata'.

    `chat_title`/`display_name` passano SEMPRE da `etichetta_visibile`, mai dal
    titolo grezzo (P12): e' la stessa funzione che sa mascherare i titoli che
    superano `titolo_e_numero` ma da cui `numero_dal_titolo` non estrae nulla
    (es. '123456789012345678', troppe cifre per E.164) -- senza, quella riga
    finirebbe con chat_title=None E phone_hmac=None, cioe' senza identita' per
    nessuna delle due unique, e si duplicherebbe a ogni ri-scansione.
    """
    etichetta = etichetta_visibile(riga.titolo, riga.numero)
    encrypted_phone = encrypt(riga.numero) if riga.numero else None
    # hmac_e164, non hmac_phone: riga.numero e' nudo (senza '+'), ed e'
    # esattamente il punto in cui il '+' andava ricomposto e non lo era mai
    # stato (AVVIO 12/08 §1) -- 246 contatti su 258 sono nati da qui nella
    # forma sbagliata, invisibili al reply-watcher finche' non e' arrivato
    # il cerotto (PR #75) e poi la migrazione (probe_hmac_duplicati.py +
    # migra_hmac_forma_canonica.py, 13/08).
    phone_hmac = hmac_e164(riga.numero) if riga.numero else None

    esistente = await _trova_esistente(db, number_id, etichetta, phone_hmac)

    if esistente is not None:
        _fondi(esistente, etichetta=etichetta, encrypted_phone=encrypted_phone,
               phone_hmac=phone_hmac, riga=riga)
        await db.commit()
        return "aggiornata"

    db.add(WaDiscoveredChat(
        tenant_id=tenant_id, number_id=number_id,
        chat_title=etichetta, display_name=etichetta,
        encrypted_phone=encrypted_phone, phone_hmac=phone_hmac,
        numero_leggibile=riga.numero_leggibile, tipo_chat=riga.tipo,
        status="nuovo",
    ))
    try:
        await db.commit()
    except IntegrityError:
        # Due scansioni concorrenti sulla stessa chat: la SELECT sopra puo'
        # non vedere ancora la riga dell'altra, e le due INSERT collidono su
        # una delle due unique. Non e' un errore del chiamante: si ripiega
        # sulla fusione contro la riga che ha vinto la corsa.
        await db.rollback()
        esistente = await _trova_esistente(db, number_id, etichetta, phone_hmac)
        if esistente is None:
            raise
        _fondi(esistente, etichetta=etichetta, encrypted_phone=encrypted_phone,
               phone_hmac=phone_hmac, riga=riga)
        await db.commit()
        return "aggiornata"
    return "creata"
