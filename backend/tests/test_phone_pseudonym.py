import pytest

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
