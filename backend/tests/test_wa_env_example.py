"""`.env.example` non deve rimettere in piedi bug gia' chiusi.

Il fix G6 (`7f594f0`) ha tolto `basta` dalle stop-word automatiche e l'ha
spostata fra le ambigue: da sola puo' voler dire "smettila di scrivermi" ma
dentro una frase lunga quasi mai, quindi non deve piu' produrre un opt-out
automatico — manda un avviso a un umano che legge e decide.

Il fix pero' vive nei default di `config.py`, mentre `.env.example` continuava
a elencare `basta` fra le `WA_STOP_WORDS`. Chi rigenera un `.env` dal template
— cioe' chiunque installi il progetto da capo, o un cliente nuovo — si riporta
in casa il comportamento vecchio, in silenzio e senza che un test se ne
accorga. Un template in deriva e' un bug che si ripresenta da solo.
"""
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[2]
ENV_EXAMPLE = REPO / ".env.example"


def _valore(chiave: str) -> str | None:
    testo = ENV_EXAMPLE.read_text(encoding="utf-8", errors="replace")
    trovato = re.search(rf"^{re.escape(chiave)}=(.*)$", testo, re.M)
    return trovato.group(1).strip() if trovato else None


def _insieme(valore: str) -> set[str]:
    return {p.strip().lower() for p in valore.split(",") if p.strip()}


def test_env_example_non_rimette_le_parole_ambigue_fra_gli_stop_automatici():
    from app.config import settings

    esempio = _valore("WA_STOP_WORDS")
    assert esempio is not None, ".env.example non documenta piu' WA_STOP_WORDS"

    ambigue = _insieme(settings.wa_stop_words_ambigue)
    sovrapposte = _insieme(esempio) & ambigue
    assert not sovrapposte, (
        f"{sorted(sovrapposte)} sono nelle WA_STOP_WORDS del template e insieme "
        "fra le ambigue: chi rigenera un .env da qui riattiva l'opt-out "
        "automatico che il fix G6 (7f594f0) aveva tolto")


def test_env_example_non_propone_una_finestra_oraria_diversa_dal_default():
    """La finestra di invio nel template era `09:30-19:30`, quella in codice
    e' passata a `09:00-20:00` (`23da60b`). Non e' grave come le stop-word,
    ma e' la stessa deriva: il template promette un comportamento che il
    codice non ha piu'."""
    from app.config import settings

    assert _valore("WA_ACTIVE_HOURS") == settings.wa_active_hours
