"""Riconoscimento per nome visualizzato e contatore di zona.

REGOLA FONDANTE: il riconoscimento decide SOLO il ritmo. Una riga non
riconosciuta si apre SEMPRE, in qualunque zona.

Il disegno precedente aveva una modalita' che non apriva niente, con rientro a 3
sconosciuti su 10. Due revisori indipendenti hanno dimostrato che raccoglieva
ZERO a regime: la lista e' ordinata per messaggio piu' recente, in cima ci sono i
~100 DM appena inviati (tutti noti), il contatore arrivava a 10 entro le prime
dieci righe, e da li' 1-2 sconosciuti ogni 10 non superavano mai la soglia.
"""
from __future__ import annotations

from collections import Counter, deque

from app.services.inbox_browser.testo import e_segnaposto, normalizza_nome

NOTI_PER_ZONA_RAPIDA = 10
FINESTRA = 10
SCONOSCIUTI_PER_ZONA_PIENA = 3


class ArchivioNomi:
    """I nomi gia' in archivio, in forma normalizzata.

    Un nome vale come riconoscimento solo se e' UNICO: i nomi visualizzati di
    Instagram non sono univoci, e riconoscere per un nome ripetuto significa
    saltare una persona diversa credendola gia' presa.
    """

    def __init__(self, nomi: list[str | None]):
        self._conteggio = Counter(n for n in (normalizza_nome(x) for x in nomi or []) if n)

    def e_riconosciuto(self, nome: str | None) -> bool:
        normale = normalizza_nome(nome)
        if not normale or e_segnaposto(nome):
            return False
        return self._conteggio.get(normale) == 1

    def aggiungi(self, nome: str | None) -> None:
        """Un nome appena raccolto entra nell'archivio.

        Se il nome era gia' presente, il conteggio sale a 2+ e smette di
        valere come riconoscimento: e' lo stesso caso ambiguo del costruttore,
        solo scoperto a runtime invece che al caricamento iniziale.
        """
        normale = normalizza_nome(nome)
        if normale and not e_segnaposto(nome):
            self._conteggio[normale] += 1


class ContatoreZona:
    """Governa SOLO il ritmo: 'piena' (si aprono chat nuove) o 'rapida'
    (si attraversa una zona gia' lavorata).

    Deliberatamente NON espone nessun metodo che dica se aprire una riga.
    """

    def __init__(self) -> None:
        self.zona = "piena"
        self._noti_di_fila = 0
        self._finestra: deque[bool] = deque(maxlen=FINESTRA)

    def registra(self, riconosciuto: bool) -> str:
        self._finestra.append(riconosciuto)

        if self.zona == "piena":
            self._noti_di_fila = self._noti_di_fila + 1 if riconosciuto else 0
            if self._noti_di_fila >= NOTI_PER_ZONA_RAPIDA:
                self.zona = "rapida"
                self._finestra.clear()
        else:
            sconosciuti = sum(1 for r in self._finestra if not r)
            if sconosciuti >= SCONOSCIUTI_PER_ZONA_PIENA:
                self.zona = "piena"
                self._noti_di_fila = 0
                self._finestra.clear()

        return self.zona
