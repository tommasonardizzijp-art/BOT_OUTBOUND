"""Modalita' segnalibro: saltare la parte alta gia' lavorata.

Requisiti posti da Tommaso l'11/08, che vincolano il disegno:

- e' una modalita' con TOGGLE, scelta a ogni avvio della Fase Lista, non una
  configurazione permanente: una sessione va in profondita' saltando, quella
  dopo gira con la modalita' spenta e recupera chi ha risposto ed e' risalito;
- la soglia e' una DATA, mai il puntatore a una chat: se si memorizzasse
  "l'ultima chat vista" e proprio quella ricevesse una risposta, risalirebbe in
  cima e il riferimento sarebbe perso;
- la data si legge dalla riga di lista, mai aprendo il thread;
- il rischio di perdere chi e' risalito e' accettato consapevolmente.
"""
from datetime import datetime, timedelta

from app.services.inbox_browser.segnalibro import (
    nuovo_cursore, riga_da_saltare, soglia_in_ore,
)

ADESSO = datetime(2026, 8, 11, 12, 0, 0)


# ── quando si salta ────────────────────────────────────────────────────────
def test_a_modalita_spenta_non_si_salta_mai():
    """La sessione a modalita' spenta e' quella che recupera chi e' risalito:
    se saltasse qualcosa non servirebbe a niente."""
    assert riga_da_saltare("20 h", soglia_ore=120.0, attiva=False) is False


def test_una_riga_piu_recente_della_soglia_si_salta():
    """20 ore fa e' sopra il punto dove eravamo arrivati (5 giorni): quella
    zona e' gia' stata lavorata."""
    assert riga_da_saltare("20 h", soglia_ore=120.0, attiva=True) is True


def test_una_riga_piu_vecchia_della_soglia_si_lavora():
    assert riga_da_saltare("7 g", soglia_ore=120.0, attiva=True) is False


def test_sul_confine_si_lavora():
    """Sulla soglia esatta si legge: e' il punto da cui si riprende, e rileggere
    qualche riga costa meno che perderla."""
    assert riga_da_saltare("5 g", soglia_ore=120.0, attiva=True) is False


def test_senza_soglia_non_si_salta():
    """Prima sessione in assoluto: non esiste un punto a cui tornare."""
    assert riga_da_saltare("20 h", soglia_ore=None, attiva=True) is False


def test_una_data_illeggibile_non_si_salta():
    """'Unread' e qualunque formato nuovo: nel dubbio si guarda. Saltare su
    un'eta' sconosciuta perderebbe contatti in silenzio, che e' il fallimento
    che questo modulo deve evitare piu' di ogni altro."""
    assert riga_da_saltare("Unread", soglia_ore=120.0, attiva=True) is False
    assert riga_da_saltare(None, soglia_ore=120.0, attiva=True) is False


# ── come si aggiorna il cursore ────────────────────────────────────────────
def test_il_primo_cursore_e_l_eta_della_riga_lavorata():
    atteso = ADESSO - timedelta(hours=120)
    assert nuovo_cursore(None, eta_ore=120.0, adesso=ADESSO) == atteso


def test_il_cursore_scende_solo_verso_il_passato():
    """Il cursore segna QUANTO IN BASSO si e' arrivati. Una riga piu' recente
    incontrata dopo — perche' e' risalita, o dopo un reset della lista — non
    deve farlo tornare indietro, altrimenti la sessione successiva ripartirebbe
    da piu' in alto e il segnalibro perderebbe senso."""
    vecchio = ADESSO - timedelta(hours=120)
    assert nuovo_cursore(vecchio, eta_ore=20.0, adesso=ADESSO) == vecchio


def test_una_riga_piu_vecchia_sposta_il_cursore_piu_indietro():
    vecchio = ADESSO - timedelta(hours=120)
    atteso = ADESSO - timedelta(hours=168)
    assert nuovo_cursore(vecchio, eta_ore=168.0, adesso=ADESSO) == atteso


def test_un_eta_illeggibile_lascia_il_cursore_dov_e():
    vecchio = ADESSO - timedelta(hours=120)
    assert nuovo_cursore(vecchio, eta_ore=None, adesso=ADESSO) == vecchio


def test_un_eta_assurda_non_fa_esplodere_timedelta():
    """Trovato in review: senza tetto, un'eta' abnorme (bug futuro
    dell'interfaccia IG, non impedito dalla regex di eta_riga_in_ore) faceva
    sollevare OverflowError su timedelta invece di degradare in sicurezza
    come un'eta' illeggibile qualsiasi."""
    vecchio = ADESSO - timedelta(hours=120)
    assert nuovo_cursore(vecchio, eta_ore=999999999999.0, adesso=ADESSO) == vecchio


# ── dalla data alla soglia ─────────────────────────────────────────────────
def test_la_soglia_e_la_distanza_fra_cursore_e_adesso():
    cursore = ADESSO - timedelta(hours=120)
    assert soglia_in_ore(cursore, ADESSO) == 120.0


def test_senza_cursore_non_c_e_soglia():
    assert soglia_in_ore(None, ADESSO) is None


def test_un_cursore_nel_futuro_non_produce_una_soglia_negativa():
    """Orologi sfasati o dati sporchi: una soglia negativa farebbe saltare
    l'intera lista."""
    assert soglia_in_ore(ADESSO + timedelta(hours=5), ADESSO) is None


# ── il cursore deve avanzare anche sulle chat vecchie ──────────────────────
# Misurato il 12/08 sul campo: 184 aperture, cursore fermo al giorno di prima.
# L'eta' relativa della riga tornava illeggibile su tutte (le chat vecchie non
# dicono piu' '5 sett'), mentre la data assoluta del thread aperto era corretta
# 146 volte su 146. Il segnalibro si segnava di aver lavorato ma non fin dove.
def test_il_cursore_scende_con_la_data_assoluta_del_thread():
    from datetime import datetime

    from app.services.inbox_browser.segnalibro import nuovo_cursore_da_data

    cursore = datetime(2026, 7, 28, 18, 41)
    piu_vecchia = datetime(2026, 3, 2, 9, 0)
    assert nuovo_cursore_da_data(cursore, piu_vecchia) == piu_vecchia


def test_una_chat_recente_non_riporta_su_il_cursore():
    """Il cursore scende soltanto: e' la stessa regola di `nuovo_cursore`, e
    serve perche' dopo un reset della lista la sessione dopo ripartirebbe da
    piu' in alto."""
    from datetime import datetime

    from app.services.inbox_browser.segnalibro import nuovo_cursore_da_data

    cursore = datetime(2026, 3, 2, 9, 0)
    assert nuovo_cursore_da_data(cursore, datetime(2026, 8, 11, 22, 0)) == cursore


def test_senza_data_del_thread_il_cursore_resta_dov_e():
    """Nel dubbio non si sposta niente: un cursore spostato per errore fa
    saltare in silenzio le chat che stanno in mezzo."""
    from datetime import datetime

    from app.services.inbox_browser.segnalibro import nuovo_cursore_da_data

    cursore = datetime(2026, 7, 28, 18, 41)
    assert nuovo_cursore_da_data(cursore, None) == cursore
    assert nuovo_cursore_da_data(None, None) is None
