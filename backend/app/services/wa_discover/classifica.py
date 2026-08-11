"""Le decisioni della Fase A, tutte pure e testabili senza browser.

REGOLA DI DISEGNO ereditata dal motore inbox Instagram: il JS raccoglie, Python
decide. Qui vive il 'decide'. Cablare queste regole dentro una query JS le
renderebbe verificabili solo con un browser vivo e una sessione WhatsApp reale --
cioe' mai, in pratica.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.utils.phone_pseudonym import (
    PhoneNormalizationError, mask_phone, normalize_e164,
)

# Un titolo che e' un numero: solo cifre e separatori tipografici, almeno 8
# cifre. Il confine stretto e' voluto -- '4 cessi' e 'Cuba 2 e bachata 2' sono
# titoli veri, e un match largo li cifrerebbe come numeri di telefono.
# I marcatori bidi (WhatsApp li infila nei titoli che contengono numeri) si
# scrivono con chr(), MAI come caratteri letterali nel sorgente: sono invisibili
# in una review, un find-and-replace testuale li perde e alcuni editor li
# normalizzano via, cambiando il regex senza che nessuno se ne accorga. Stessa
# forma gia' usata da whatsapp_page.py:_BIDI_MARKERS.
_MARCATORI_BIDI = f"{chr(0x202a)}-{chr(0x202e)}{chr(0x2066)}-{chr(0x2069)}"
_SOLO_NUMERO = re.compile(r"^[+\d][\d\s\-.()/" + _MARCATORI_BIDI + r"]*$")
_ALMENO_8_CIFRE = re.compile(r"(?:\D*\d){8}")
# Il segnale ad alta precisione per i gruppi (recall bassa: 1/6, PoC-4).
_PARTECIPANTI = re.compile(r"\d+\s*(partecipant|membr|iscritt)", re.IGNORECASE)

TIPO_INDIVIDUALE = "individuale"
TIPO_GRUPPO = "gruppo"
TIPO_IGNOTO = "ignoto"


@dataclass
class RigaScoperta:
    """Una chat scoperta, prima che il salvataggio decida come scriverla.

    `titolo` e' il titolo GREZZO letto dalla sidebar, anche quando e' un numero
    di telefono: NON va azzerato qui per rispettare P12. La mascheratura e'
    responsabilita' di `etichetta_visibile`, che il salvataggio applica sempre.
    Azzerarlo a monte butterebbe via l'informazione prima che qualcuno possa
    mascherarla, e la riga finirebbe a DB con chat_title NULL e phone_hmac NULL
    -- senza identita' per nessuna delle due unique, quindi duplicata a ogni
    ri-scansione (vedi wa_discover/salvataggio.py). P12 si rispetta scrivendo
    la maschera, non perdendo il dato.
    """
    titolo: str | None          # titolo grezzo dalla sidebar; maschera a valle
    numero: str | None          # E.164, gia' normalizzato
    numero_leggibile: bool
    tipo: str                   # TIPO_*


def titolo_e_numero(titolo: str | None) -> bool:
    """Il titolo della chat e' il numero stesso (contatto non in rubrica).

    Misurato: 39% delle chat di Primero, 14% del numero personale (PoC-5). Non
    e' un caso di bordo, e' un caso frequente -- e sono proprio le chat per cui
    NON serve aprire il pannello info, cioe' quelle che fanno risparmiare i 5,3s.
    """
    t = (titolo or "").strip()
    if not t or not _SOLO_NUMERO.match(t):
        return False
    return bool(_ALMENO_8_CIFRE.match(t))


def numero_dal_titolo(titolo: str | None) -> str | None:
    """Il numero E.164 se il titolo e' un numero, altrimenti None.

    Non solleva: un titolo che sembra un numero ma non normalizza (prefisso
    esotico, lunghezza fuori range) non e' un guasto -- e' una riga che verra'
    trattata come 'numero non letto', esattamente come un pannello illeggibile.
    """
    if not titolo_e_numero(titolo):
        return None
    try:
        return normalize_e164(titolo)
    except PhoneNormalizationError:
        return None


def etichetta_visibile(titolo: str | None, numero: str | None) -> str | None:
    """Cosa si salva come nome della chat, senza mai scrivere un numero in chiaro.

    Serve a chiudere un caso che cade fra `titolo_e_numero` e `numero_dal_titolo`:
    esistono titoli che superano il primo filtro ma da cui il secondo non ricava
    nulla -- misurati '+39 342 146 0077 99 88 77', '123456789012345678',
    '00 12 34 56 78 90 12 34 56 78' (troppe cifre per E.164). Senza questa
    funzione quella riga finirebbe a DB con chat_title=None (P12 vieta il numero
    in chiaro) E phone_hmac=None (non si e' ricavato il numero): entrambe le
    unique di wa_discovered_chats sono cieche sui NULL, quindi la riga si
    duplicherebbe a ogni ri-scansione e non avrebbe nessuna identita'.

    La via d'uscita e' un'etichetta MASCHERATA (mask_phone: '+39.....077'): non
    e' il numero, quindi P12 e' rispettato; e' deterministica, quindi la unique
    sul titolo torna a mordere; ed e' leggibile, quindi in Fase B l'operatore
    vede una riga riconoscibile invece di una vuota.

    Ritorna None solo se non c'e' proprio niente da mostrare.
    """
    t = (titolo or "").strip()
    if not t:
        return None
    if not titolo_e_numero(t):
        return t          # un nome vero: si salva com'e'
    # Il titolo E' un numero: mai in chiaro. Se il numero e' stato normalizzato
    # si maschera quello (forma canonica, stabile fra scansioni); altrimenti si
    # maschera il titolo grezzo, che e' comunque meglio di una riga anonima.
    return mask_phone(numero or t)


def e_etichetta_mascherata(etichetta: str | None) -> bool:
    """L'etichetta e' un segnaposto (numero mascherato), non un nome vero.

    Serve alla fusione: un contatto non ancora in rubrica ha il numero come
    titolo e viene salvato come '+39.....077'; quando il cliente lo salva in
    rubrica, la ri-scansione porta il nome vero. Senza distinguere le due cose,
    la regola "integra, non sovrascrive" terrebbe la maschera per sempre e in
    Fase B l'operatore approverebbe una lista di maschere invece di nomi.

    Il riconoscimento e' sulla forma prodotta da mask_phone (prefisso, puntini,
    ultime cifre): non si tenta di indovinare altro.
    """
    testo = (etichetta or "").strip()
    return bool(testo) and "•" in testo


def tipo_da_segnali(*, numero_leggibile: bool, testo_pannello: str | None,
                    titolo: str | None) -> str:
    """'individuale' | 'gruppo' | 'ignoto'.

    Tri-stato e non booleano, per una ragione misurata: l'euristica testuale sui
    gruppi ha recall 1/6 (PoC-4), e il pannello info fallisce nel 5% dei casi
    anche su chat 1:1 vere. Con un booleano quel 5% diventerebbe 'gruppo' --
    persone contattabili scartate senza lasciare traccia del dubbio. 'ignoto' e'
    una risposta onesta e resta filtrabile nella UI della Fase B.
    """
    if _PARTECIPANTI.search(testo_pannello or ""):
        return TIPO_GRUPPO
    if numero_leggibile:
        return TIPO_INDIVIDUALE
    return TIPO_IGNOTO
