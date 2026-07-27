"""Stato persistente dei PoC WhatsApp (M0): opt-out + memoria degli invii.

Nessun import di app.*, logica pura + JSON su file: e' testabile in isolamento,
come wa_lib.py.

Perche' esiste (SDD wave-a-spec.md, sez. A1): la guardia anti-opt-out di
poc2_send.py legge il DOM partendo dal fondo e si ferma al primo messaggio
nostro. Se qualcuno risponde dopo lo STOP del cliente ("ok ci mancherebbe"),
lo STOP diventa invisibile per sempre — a meno che non venga scritto da
qualche parte. Questo modulo e' quella memoria.

In M0 lo stato sta su file (e' un PoC). In produzione l'opt-out dovra' stare
nel DB insieme al contatto (requisito vincolante per M1/M3) — la logica qui
sotto (persistenza append-only, un opt-out vale per sempre, mai testo in
chiaro) e' quella che verra' riportata a DB, non un dettaglio usa-e-getta.

Contratto implicito sulle chiavi: `e164` deve essere gia' normalizzato con
`wa_lib.normalize_e164` prima di chiamare questi metodi. Ne' OptOutStore ne'
SentLog normalizzano (come AllowList): passare un numero non normalizzato
significa scrivere un opt-out/un invio su una chiave che poi nessuno
ricerchera' nella forma giusta.
"""
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# Stesso default di _common.POC_ROOT, ripetuto invece che importato: questo
# modulo resta puro (niente _common, niente psutil) per poter girare nei test
# senza browser. Se cambia il default, vanno cambiati entrambi.
STATE_DIR = Path(os.environ.get("POC_WA_STATE_DIR") or os.environ.get("POC_WA_ROOT") or r"D:\dev\wa-poc")
OPTOUT_PATH = Path(os.environ.get("POC_WA_OPTOUT_PATH", str(STATE_DIR / "optout.json")))
SENTLOG_PATH = Path(os.environ.get("POC_WA_SENTLOG_PATH", str(STATE_DIR / "sent_log.json")))


class PocStateCorrupted(Exception):
    """Il file di stato esiste ma non e' un JSON valido (o non ha la forma attesa).

    Deliberatamente NON trattato come "insieme vuoto": lo stato riguarda opt-out
    reali, e confondere "corrotto" con "vuoto" significherebbe scrivere di nuovo
    a qualcuno che aveva chiesto di smettere.
    """


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash_text(testo: str) -> str:
    """sha256 esadecimale del testo. Mai il testo in chiaro sul disco: questi
    file restano su disco per settimane e riguardano messaggi veri."""
    return hashlib.sha256((testo or "").encode("utf-8")).hexdigest()


def _load_json_object(path: Path) -> dict:
    """File assente = insieme vuoto (primo run, normale). File presente ma
    illeggibile/non-oggetto = PocStateCorrupted (rumoroso di proposito)."""
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise PocStateCorrupted(
            f"Stato PoC corrotto: {path} non e' un JSON valido ({exc}). "
            f"NON cancellare: potrebbe contenere opt-out registrati. "
            f"Ripristinare da backup o correggere a mano prima di rilanciare."
        ) from exc
    if not isinstance(data, dict):
        raise PocStateCorrupted(
            f"Stato PoC corrotto: {path} non contiene un oggetto JSON "
            f"(trovato {type(data).__name__})."
        )
    return data


def _atomic_write_json(path: Path, data: dict) -> None:
    """Scrive `data` come JSON in `path` senza mai lasciare il file a meta':
    file temporaneo nella stessa directory + os.replace (atomico sullo stesso
    filesystem). Un'interruzione a meta' scrittura farebbe perdere TUTTI gli
    opt-out registrati al run successivo."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


class OptOutStore:
    """Chi ha chiesto di smettere. Un opt-out registrato vale per sempre: non
    esiste rimozione in questo modulo (in produzione la revoca sara' una
    decisione di business sul DB, non un dettaglio di script)."""

    def __init__(self, path: Path, entries: dict):
        self._path = path
        self._entries = entries

    @classmethod
    def load(cls, path: str | Path | None = None) -> "OptOutStore":
        p = Path(path) if path is not None else OPTOUT_PATH
        return cls(p, _load_json_object(p))

    def is_opted_out(self, e164: str) -> bool:
        return e164 in self._entries

    def add(self, e164: str, motivo: str) -> None:
        """Rilegge lo stato da disco e si fonde con esso prima di scrivere: due
        OptOutStore caricati entrambi prima di qualunque write non devono farsi
        last-write-wins a vicenda, altrimenti il primo opt-out registrato
        sparirebbe sotto il secondo. Nessuna scrittura parallela e' garantita
        oggi (il lock sul profilo Chromium ammette un solo sender), ma il
        fallimento di questo modulo non e' un crash: e' scrivere di nuovo a
        qualcuno che aveva chiesto di smettere, quindi non ci si affida a una
        garanzia che vive altrove."""
        self._entries = _load_json_object(self._path)
        self._entries[e164] = {"motivo": motivo, "ts": _now()}
        _atomic_write_json(self._path, self._entries)


class SentLog:
    """Cosa e' gia' stato mandato a chi, per hash del testo (mai in chiaro)."""

    def __init__(self, path: Path, entries: dict):
        self._path = path
        self._entries = entries

    @classmethod
    def load(cls, path: str | Path | None = None) -> "SentLog":
        p = Path(path) if path is not None else SENTLOG_PATH
        return cls(p, _load_json_object(p))

    def already_sent(self, e164: str, testo: str) -> bool:
        return _hash_text(testo) in self._entries.get(e164, [])

    def record(self, e164: str, testo: str) -> None:
        """Stesso motivo di OptOutStore.add(): rilegge da disco e si fonde
        prima di scrivere, non serializza la copia in memoria caricata da
        load()."""
        self._entries = _load_json_object(self._path)
        h = _hash_text(testo)
        hashes = self._entries.setdefault(e164, [])
        if h not in hashes:
            hashes.append(h)
        _atomic_write_json(self._path, self._entries)
