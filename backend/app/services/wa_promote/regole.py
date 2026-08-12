"""Fase B, Task 1: la decisione pura "promuovibile o no, e perche'".

Nessun I/O, nessun accesso DB: prende una riga di `wa_discovered_chats` gia'
caricata e risponde. Stesso principio di `wa_discover/classifica.py` -- qui
vive il 'decide', non il 'raccoglie' ne' lo 'scrive'.

Un solo motivo per riga (il primo controllo che fallisce vince), cosi' chi
chiama (Task 2, e il serializzatore GET del Task 4) non deve mai scegliere
fra due motivi contemporaneamente veri.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.models.wa import WaDiscoveredChat
from app.services.wa_discover.classifica import TIPO_GRUPPO

MOTIVO_GRUPPO = "gruppo"
MOTIVO_SENZA_NUMERO = "senza_numero"
MOTIVO_GIA_PROMOSSO = "gia_promosso"


@dataclass
class DecisionePromozione:
    ok: bool
    motivo: str | None


def promuovibile(riga: WaDiscoveredChat) -> DecisionePromozione:
    """Puo' `riga` diventare un `WaContact`?

    Ordine dei controlli, il primo che fallisce vince:
    1. `status != 'nuovo'` -- non si ripromuove ('promosso' -> motivo
       'gia_promosso') e non si promuove chi e' stato scartato dall'operatore
       ('scartato' -> motivo 'scartato', lo status stesso).
    2. **Mai un gruppo**, per costruzione (vincolo globale del piano Fase B):
       anche se un id di gruppo arriva alla funzione con un numero valletto
       (bug di selezione, bulk-select, richiesta scritta a mano), si scarta.
    3. Senza numero (manca `phone_hmac` o `encrypted_phone`, le due colonne
       che `salvataggio.py` scrive sempre insieme) non c'e' niente da
       promuovere: un `WaContact` le vuole entrambe NOT NULL.
    4. Altrimenti si puo' promuovere -- **incluso 'ignoto'**: il tri-stato
       dice "non lo so", non "e' un gruppo", e il backend non blocca il
       dubbio, lo lascia decidere all'operatore in UI.
    """
    if riga.status != "nuovo":
        motivo = MOTIVO_GIA_PROMOSSO if riga.status == "promosso" else riga.status
        return DecisionePromozione(ok=False, motivo=motivo)

    if riga.tipo_chat == TIPO_GRUPPO:
        return DecisionePromozione(ok=False, motivo=MOTIVO_GRUPPO)

    if riga.phone_hmac is None or riga.encrypted_phone is None:
        return DecisionePromozione(ok=False, motivo=MOTIVO_SENZA_NUMERO)

    return DecisionePromozione(ok=True, motivo=None)
