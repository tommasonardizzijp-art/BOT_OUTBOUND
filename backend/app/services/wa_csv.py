"""Parsing difensivo del CSV di ingest. Puro: nessun DB, nessuna rete,
nessun log del contenuto (le righe contengono numeri di telefono).

Stile ereditato da import_resolver.py: il file non fallisce mai in blocco
per colpa di una riga storta -- una riga sbagliata e' UNA riga (SDD Q21).
Falliscono in blocco solo i problemi di STRUTTURA (header assente, colonna
numero mancante, intestazioni duplicate, file oltre il limite), perche' li'
non c'e' niente da salvare e proseguire produrrebbe solo rumore.
"""
import csv
import io
from dataclasses import dataclass

from app.config import settings

COLONNA_NUMERO = "numero"
COLONNA_NOME = "nome"


class CsvParseError(ValueError):
    """Problema di STRUTTURA del file. Non contiene mai dati di riga."""


@dataclass
class RigaCsv:
    numero_riga: int          # 1-based, header escluso: e' quello che l'admin vede
    valori: dict[str, str]


def _decodifica(contenuto: bytes) -> str:
    """UTF-8 con BOM gestito. Un file latin-1 non deve dare
    UnicodeDecodeError: si sostituiscono i caratteri illeggibili e si va
    avanti -- un accento storto in un nome non giustifica il rifiuto di
    5.000 contatti."""
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return contenuto.decode(encoding)
        except UnicodeDecodeError:
            continue
    return contenuto.decode("latin-1", errors="replace")


def _dialetto(prima_riga: str) -> str:
    """';' se ce ne sono piu' che virgole: e' l'export dell'Excel
    italiano, ed e' il caso piu' probabile dei file veri."""
    return ";" if prima_riga.count(";") > prima_riga.count(",") else ","


def _maschera_se_numerico(valore: str) -> str:
    """Un file SENZA intestazione fa leggere la prima riga di DATI come
    header: se quella riga sono numeri di telefono, il messaggio 'colonna
    numero assente' non deve stamparli in chiaro (P12, contratto §2.3). Qui
    non sappiamo ancora se e' davvero un numero: un token con 6+ cifre lo
    trattiamo come tale per sicurezza, un vero nome di colonna (es.
    'telefono', 'id_cliente') non ne ha abbastanza e passa intatto."""
    if sum(c.isdigit() for c in valore) < 6:
        return valore
    v = valore.strip()
    if len(v) <= 5:
        return "•" * len(v)
    return f"{v[:3]}{'•' * max(3, len(v) - 5)}{v[-2:]}"


def parse_wa_csv(contenuto: bytes) -> tuple[list[RigaCsv], list[str]]:
    testo = _decodifica(contenuto).strip()
    if not testo:
        raise CsvParseError("File vuoto.")

    prima = testo.splitlines()[0]
    reader = csv.reader(io.StringIO(testo), delimiter=_dialetto(prima))
    header = [h.strip().lstrip("\ufeff").lower() for h in next(reader, [])]
    if not header:
        raise CsvParseError("File senza intestazione.")
    if len(set(header)) != len(header):
        raise CsvParseError("Intestazioni duplicate: ogni colonna deve avere un nome unico.")
    if COLONNA_NUMERO not in header:
        colonne_sicure = [_maschera_se_numerico(h) for h in header]
        raise CsvParseError(
            f"Colonna '{COLONNA_NUMERO}' obbligatoria e assente. "
            f"Colonne trovate: {', '.join(colonne_sicure)}."
        )

    righe: list[RigaCsv] = []
    for i, valori in enumerate(reader, start=1):
        if not any((v or "").strip() for v in valori):
            continue        # riga vuota: non e' uno scarto, e' niente
        if len(righe) >= settings.wa_ingest_max_rows:
            raise CsvParseError(
                f"File oltre il limite di {settings.wa_ingest_max_rows} righe. "
                "Con un cap di 100-200 messaggi al giorno una lista cosi' lunga "
                "sono mesi di campagna: va spezzata."
            )
        # zip_longest a mano: una riga corta riempie di stringhe vuote, una
        # riga lunga scarta la coda. In entrambi i casi la riga SOPRAVVIVE:
        # sara' la validazione del numero a scartarla, con un motivo vero.
        valori = list(valori) + [""] * (len(header) - len(valori))
        righe.append(RigaCsv(numero_riga=i,
                             valori={h: (v or "").strip()
                                     for h, v in zip(header, valori)}))

    if not righe:
        raise CsvParseError("Il file ha l'intestazione ma nessuna riga di dati.")

    attributi = sorted(h for h in header if h not in (COLONNA_NUMERO, COLONNA_NOME))
    return righe, attributi
