import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import inspect, select

from app.config import settings
from app.models.tenant import Tenant
from app.models.wa import WaContact
from app.utils.crypto import decrypt, encrypt
from app.utils.phone_pseudonym import (PhoneNormalizationError, hmac_phone,
                                       mask_phone, normalize_e164)

# I caratteri non ASCII di questo file sono scritti come escape e MAI come
# glifi: un marcatore bidi o uno spazio no-break incollato nel sorgente e'
# invisibile in un diff, e un test che fallisce per un carattere che nessuno
# vede costa piu' del test stesso.
BIDI_L, BIDI_R = "‪", "‬"          # marcatori dai title di WhatsApp
CIFRE_ARABE = "٣٩٣٤٢١٤٦٠٠٧٧"
SETTE_ARABO = "٧"                        # cifra 7 arabo-indiana
NBSP = " "                               # spazio no-break
PALLINO = "•"


@pytest.mark.parametrize("raw,atteso", [
    ("+39 342 146 0077", "393421460077"),
    ("3421460077", "393421460077"),        # nazionale italiano, prefisso implicito
    ("0039 342 146 0077", "393421460077"),
    ("+39-342-146-0077", "393421460077"),
    ("39-342-146-0077", "393421460077"),
    ("(39) 342.146.0077", "393421460077"),
    ("+39/342/146/0077", "393421460077"),
    (BIDI_L + "+393421460077" + BIDI_R, "393421460077"),   # title di WhatsApp
])
def test_normalize_e164_accetta_le_forme_reali(raw, atteso):
    assert normalize_e164(raw) == atteso


@pytest.mark.parametrize("raw", ["", "   ", "abc", "+39", "12", None])
def test_normalize_e164_rifiuta_invece_di_indovinare(raw):
    """Un numero non normalizzabile e' uno SCARTO dell'ingest, non un numero
    'quasi giusto': indovinare significa scrivere a uno sconosciuto."""
    with pytest.raises(PhoneNormalizationError):
        normalize_e164(raw)


@pytest.mark.parametrize("raw", [
    "+39 342 146 0077 ext. 12",       # le cifre dell'interno si saldano al numero
    "+39 342 146 0077 int.4",
    "+39 342 146 0077 (ufficio)",     # nessuna cifra, ma resta testo: si rifiuta lo stesso
    "+" + CIFRE_ARABE,                # tutte cifre arabo-indiane
    "+39342146007" + SETTE_ARABO,     # UNA sola cifra non ASCII, in coda
    "+39" + NBSP + "342" + NBSP + "146" + NBSP + "0077",   # spazi no-break, non ammessi
])
def test_normalize_e164_rifiuta_input_sporco_invece_di_ripulirlo(raw):
    """Regressione dei due bug trovati in review il 28/07.

    Il modulo RIPULIVA (cancellava tutto cio' che non fosse cifra) invece di
    VALIDARE. Due conseguenze, entrambe misurate sul codice vero:

        '+39 342 146 0077 ext. 12'   ->  '39342146007712'   accettato
        '+<cifre arabo-indiane>'     ->  restituito tale e quale

    Il primo e' il piu' grave. Le cifre dell'annotazione sopravvivono alla
    pulizia e si saldano al numero, che esce lungo 14 e quindi dentro il range
    E.164 ammesso. Non produce un errore: produce un numero DIVERSO da quello
    scritto, accettato in silenzio. Su questo canale significa recapitare un
    messaggio commerciale a uno sconosciuto.

    Il secondo passava perche' in Python `\\d` e `str.isdigit()` accettano le
    cifre di qualunque alfabeto. Il risultato non e' componibile e finirebbe a
    DB come phone_hmac di un numero che non esiste.

    Nessuno dei 16 test precedenti li intercettava: i casi negativi erano tutti
    ovviamente invalidi ('', 'abc', '+39'), e nessuno SEMBRAVA un numero. Da
    qui la regola: i casi negativi utili sono quelli plausibili.
    """
    with pytest.raises(PhoneNormalizationError):
        normalize_e164(raw)


def test_normalize_e164_accetta_i_separatori_ammessi():
    """Il rifiuto non deve essere cosi' stretto da respingere le forme reali:
    la lista dei separatori e' chiusa, ma contiene quelli che un umano usa."""
    assert normalize_e164("+39 (342) 146.0077") == "393421460077"
    assert normalize_e164("+39/342/146/0077") == "393421460077"


def test_hmac_e_deterministico_e_lungo_64():
    a = hmac_phone("393421460077")
    assert a == hmac_phone("393421460077")
    assert len(a) == 64 and all(c in "0123456789abcdef" for c in a)


def test_hmac_distingue_numeri_diversi():
    assert hmac_phone("393421460077") != hmac_phone("393421460078")


def test_hmac_non_contiene_il_numero():
    assert "3421460077" not in hmac_phone("393421460077")


def test_mask_mostra_solo_prefisso_e_ultime_tre():
    assert mask_phone("393421460077") == "+39" + PALLINO * 5 + "077"


def test_mask_non_esplode_su_numero_corto():
    """mask_phone finisce nei log degli errori: se solleva li' dentro, nasconde
    l'errore vero che stava per essere loggato."""
    assert mask_phone("39") == "+39" + PALLINO * 5
    assert mask_phone("") == ""


# ---------------------------------------------------------------------------
# Funzionale 5 -- HMAC deterministico anche fra processi diversi (Batch 3).
# ---------------------------------------------------------------------------

def test_hmac_deterministico_tra_processi():
    """Se l'HMAC dipendesse da stato di processo (es. un salt casuale creato
    all'avvio), lo stesso numero produrrebbe digest diversi a ogni riavvio
    del backend: un contatto gia' salvato smetterebbe di corrispondere a se
    stesso. Verificato ricalcolando in un subprocess separato, con la stessa
    PHONE_HMAC_KEY ereditata dall'ambiente."""
    numero = "393421460077"
    atteso = hmac_phone(numero)

    backend_dir = Path(__file__).resolve().parents[1]
    script = (
        "from app.utils.phone_pseudonym import hmac_phone; "
        f"print(hmac_phone({numero!r}))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(backend_dir),
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"subprocess fallito: {proc.stderr}"
    assert proc.stdout.strip() == atteso


# ---------------------------------------------------------------------------
# Funzionale 6 -- l'HMAC dipende dalla chiave (Batch 3).
# ---------------------------------------------------------------------------

def test_hmac_dipende_dalla_chiave(monkeypatch):
    """Stessa cifra, chiave diversa -> digest diverso: la prova che non e'
    uno SHA-256 nudo travestito da HMAC (che ignorerebbe la chiave)."""
    numero = "393421460077"
    monkeypatch.setattr(settings, "phone_hmac_key", "chiave-uno-di-test")
    digest_uno = hmac_phone(numero)
    monkeypatch.setattr(settings, "phone_hmac_key", "chiave-due-di-test")
    digest_due = hmac_phone(numero)
    assert digest_uno != digest_due


# ---------------------------------------------------------------------------
# Funzionale 8 -- round-trip d'uso: dopo il salvataggio il numero in chiaro
# non compare in nessuna colonna tranne encrypted_phone (Batch 3).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_round_trip_numero_in_chiaro_solo_in_encrypted_phone(db_session):
    """normalize_e164 -> hmac_phone -> chiave di WaContact. Dopo il commit,
    nessuna colonna diversa da encrypted_phone deve contenere le cifre del
    numero: encrypted_phone e' Fernet (reversibile solo con SECRET_KEY),
    phone_hmac e' l'HMAC (irreversibile senza PHONE_HMAC_KEY)."""
    raw = "+39 399 0000 101"           # prefisso di test 3990000xxx, non un numero reale
    e164 = normalize_e164(raw)
    h = hmac_phone(e164)
    cifrato = encrypt(e164)

    tenant = Tenant(id=str(uuid.uuid4()), name="QA phone_pseudonym round-trip")
    db_session.add(tenant)
    await db_session.flush()

    contatto = WaContact(
        id=str(uuid.uuid4()), tenant_id=tenant.id, phone_hmac=h,
        encrypted_phone=cifrato, display_name="QA round-trip",
    )
    db_session.add(contatto)
    await db_session.commit()

    ricaricato = (
        await db_session.execute(select(WaContact).where(WaContact.id == contatto.id))
    ).scalar_one()

    # L'unico posto da cui il numero deve tornare fuori.
    assert decrypt(ricaricato.encrypted_phone) == e164

    frammento = e164[-6:]  # ultime 6 cifre: improbabile falso positivo su hash/uuid
    for colonna in inspect(WaContact).columns:
        if colonna.name == "encrypted_phone":
            continue
        valore = getattr(ricaricato, colonna.name)
        if valore is None:
            continue
        assert frammento not in str(valore), (
            f"numero in chiaro trovato nella colonna {colonna.name!r}: {valore!r}"
        )


# ---------------------------------------------------------------------------
# Adversarial A -- numeri ostili non ancora coperti (2, 3, 7, 8).
# ---------------------------------------------------------------------------

def test_adv2_dieci_zeri_non_diventa_numero_italiano():
    """10 cifre che non iniziano per 3: nessun prefisso implicito da
    aggiungere (il ramo 'nazionale italiano' scatta solo se inizia per 3), e
    la lunghezza resta fuori range. Non deve diventare 390000000000."""
    with pytest.raises(PhoneNormalizationError):
        normalize_e164("0000000000")


def test_adv3_trenta_cifre_rifiutato_non_troncato():
    """Rifiutato per lunghezza: non deve essere silenziosamente troncato a un
    prefisso che sembra valido."""
    trenta_cifre = "1" * 30
    with pytest.raises(PhoneNormalizationError):
        normalize_e164(trenta_cifre)


def test_adv4_marcatori_bidi_stesso_hmac_del_numero_pulito():
    """WhatsApp infila i marcatori di direzione negli attributi `title`: un
    contatto non deve esistere due volte perche' un carattere invisibile
    cambia la stringa. Normalizza identico E produce lo stesso HMAC."""
    sporco = BIDI_L + "+393421460077" + BIDI_R
    pulito = "+393421460077"
    assert normalize_e164(sporco) == normalize_e164(pulito)
    assert hmac_phone(normalize_e164(sporco)) == hmac_phone(normalize_e164(pulito))


@pytest.mark.parametrize("raw", [None, 0, b"393421460077", ["393421460077"], {}])
def test_adv7_input_non_testuali_si_difendono_senza_eccezione_grezza(raw):
    """None, 0, bytes, lista, dict: mai un TypeError/AttributeError grezzo
    che risale al chiamante -- sempre PhoneNormalizationError leggibile.
    Nota sul codice: `if not raw or not isinstance(raw, str)` -- 0, '' e {}
    sono falsy e si fermano gia' sul primo `not raw`; bytes e list sono
    truthy quando non vuoti e si fermano sul secondo controllo."""
    with pytest.raises(PhoneNormalizationError):
        normalize_e164(raw)


def test_adv8_quattro_forme_dello_stesso_numero_stesso_hmac():
    """Nessun contatto doppio a seconda di come e' stato scritto il numero
    (con zeri internazionali, con +, senza nulla, in forma nazionale)."""
    forme = ["00393421460077", "+393421460077", "393421460077", "3421460077"]
    digest = {hmac_phone(normalize_e164(f)) for f in forme}
    assert len(digest) == 1


# ---------------------------------------------------------------------------
# Adversarial B -- la chiave segreta (9, 10, 11).
# ---------------------------------------------------------------------------

def test_adv9_chiave_assente_solleva_runtimeerror_leggibile(monkeypatch):
    """PHONE_HMAC_KEY assente (simulata come None: e' cosi' che si presenta
    un attributo mai valorizzato) -> RuntimeError che spiega come generarla.
    Mai un ripiego silenzioso su hash senza chiave."""
    monkeypatch.setattr(settings, "phone_hmac_key", None)
    with pytest.raises(RuntimeError, match="PHONE_HMAC_KEY"):
        hmac_phone("393421460077")


def test_adv10_chiave_stringa_vuota_stesso_trattamento(monkeypatch):
    """Una chiave vuota non e' una chiave: stesso RuntimeError del caso
    assente, non un caso a parte trattato con leggerezza."""
    monkeypatch.setattr(settings, "phone_hmac_key", "")
    with pytest.raises(RuntimeError, match="PHONE_HMAC_KEY"):
        hmac_phone("393421460077")


@pytest.mark.parametrize("chiave_ostile", [None, ""])
def test_adv11_messaggio_errore_non_contiene_numero_ne_chiave(monkeypatch, chiave_ostile):
    """Il messaggio d'errore e' una guida operativa (come generare la
    chiave): non deve mai interpolare il numero che si stava normalizzando
    ne' il valore della chiave. Guardia di regressione: se in futuro
    qualcuno aggiungesse quell'interpolazione al messaggio, questo test la
    intercetta."""
    monkeypatch.setattr(settings, "phone_hmac_key", chiave_ostile)
    numero = "393421460077"
    with pytest.raises(RuntimeError) as exc_info:
        hmac_phone(numero)
    messaggio = str(exc_info.value)
    assert numero not in messaggio
    assert repr(chiave_ostile) not in messaggio
