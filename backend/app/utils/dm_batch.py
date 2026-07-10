"""Cadenza del feed browse nell'invio DM.

Regola: NON si scrolla il feed dopo ogni DM, ma dopo un BATCH di batch_min..batch_max
(random) DM RIUSCITI consecutivi. Dentro il batch nessuna attesa aggiunta: il browse
del profilo target dentro send_dm fa gia' da gap umano. Il feed browse resta il
riposo anti-ban, solo meno frequente.

Uso nel worker (run_campaign_worker):
    pacer = DmBatchPacer(settings.dm_batch_min, settings.dm_batch_max, _random)
    ...loop per DM...
        if browser_open and pacer.should_browse():
            ...browse_feed...           # riposo/scroll di fine batch
            pacer.record_browse()       # chiude il batch, ne apre uno nuovo
        ...invio DM...
        pacer.record_sent()             # SOLO su invio riuscito
    # a inizio nuova sessione dopo un session break:
        pacer.reset()

Logica isolata qui apposta: il loop worker non e' testabile end-to-end senza un
harness enorme, questo componente lo e' in modo deterministico (rng iniettabile).
"""
import random as _random_mod


class DmBatchPacer:
    def __init__(self, batch_min: int, batch_max: int, rng=None):
        # Difensivo: bound sensati anche con config sballata (batch_min>=1, max>=min).
        batch_min = max(1, int(batch_min))
        batch_max = max(batch_min, int(batch_max))
        self._min = batch_min
        self._max = batch_max
        self._rng = rng or _random_mod
        self._sent_in_batch = 0
        self._target = self._rng.randint(self._min, self._max)

    @property
    def target(self) -> int:
        return self._target

    @property
    def sent_in_batch(self) -> int:
        return self._sent_in_batch

    def should_browse(self) -> bool:
        """True quando il batch corrente e' completo -> ora si scrolla/riposa."""
        return self._sent_in_batch >= self._target

    def record_sent(self) -> None:
        """Un DM RIUSCITO e' entrato nel batch corrente."""
        self._sent_in_batch += 1

    def record_browse(self) -> None:
        """Fatto lo scroll: chiude il batch e ne apre uno nuovo (target random)."""
        self._sent_in_batch = 0
        self._target = self._rng.randint(self._min, self._max)

    def reset(self) -> None:
        """Nuova sessione (dopo un session break): batch da capo."""
        self._sent_in_batch = 0
        self._target = self._rng.randint(self._min, self._max)
