"""Passo 4 / A.3 — gate professional dentro il livello `contacts`.

`/info/` e' l'unica richiesta del canale browser che NESSUNA pagina web produce in
nessuna condizione: non si puo' rendere invisibile, si puo' solo fare meno spesso.
Il gate la fa partire solo sui profili professional, letti dal payload GIA'
scaricato: zero richieste in piu' per decidere.

Prove misurate il 07/08 (tre probe, 44 casi): ogni profilo che ha reso un contatto
reale era marcato professional, zero contatti persi. Dei profili SENZA contatto il
~28% e' comunque professional, quindi il gate risparmia ~34% delle chiamate, non
tutte quelle inutili.

Perche' si guardano TRE campi e non solo `is_professional_account`: quel flag e'
affidabile in `web_profile_info` ma e' SEMPRE null nella GraphQL, che dal passo 4 e'
la sorgente primaria. La' il segnale arriva da `account_type` / `is_business`.
"""
import pytest

from app.services import browser_bio
from app.services.browser_bio import contatti_richiesti
from app.models.campaign import ENRICHMENT_BIO, ENRICHMENT_CONTACTS, ENRICHMENT_NONE


class Camp:
    def __init__(self, livello=ENRICHMENT_CONTACTS):
        self.enrichment_level = livello


class CampSenzaCampo:
    """Campagna anteriore all'introduzione dei livelli: deve comportarsi come prima."""


@pytest.fixture(autouse=True)
def _gate_e_killswitch_accesi(monkeypatch):
    monkeypatch.setattr(browser_bio.settings, "bio_browser_contact_info_enabled", True)
    monkeypatch.setattr(browser_bio.settings, "bio_browser_professional_gate_enabled", True)
    browser_bio.reset_contatore_gate()
    yield


# ── Segnali POSITIVI: il gate resta aperto ────────────────────────────────────

@pytest.mark.parametrize("profilo", [
    {"is_professional_account": True},
    {"account_type": 2},
    {"account_type": 3},
    {"is_business": True},
    # Positivo che vince su un negativo di un altro campo (ordine del probe).
    {"is_professional_account": False, "account_type": 2},
])
def test_profilo_professional_apre_il_gate(profilo):
    assert contatti_richiesti(Camp(), profilo) is True
    assert browser_bio.contatore_gate() == 0


# ── Segnali NEGATIVI: il gate chiude ─────────────────────────────────────────

@pytest.mark.parametrize("profilo", [
    {"is_professional_account": False},
    {"account_type": 1},
    {"is_business": False},
])
def test_profilo_personale_chiude_il_gate(profilo):
    assert contatti_richiesti(Camp(), profilo) is False
    assert browser_bio.contatore_gate() == 1, "la chiamata risparmiata va contata"


# ── Nessun segnale: si procede (escape di sicurezza) ─────────────────────────

@pytest.mark.parametrize("profilo", [
    None,            # payload non passato dal chiamante
    {},              # payload vuoto
    {"username": "x"},                       # nessuno dei tre campi
    {"is_professional_account": None},       # presente ma nullo (caso GraphQL)
    {"account_type": None},
    "non-un-dict",                           # forma inattesa
    {"account_type": "2"},                   # tipo inatteso: NON deve chiudere
])
def test_segnale_assente_o_illeggibile_lascia_passare(profilo):
    """Meglio una chiamata in piu' che un contatto perso: un contatto perso non si
    recupera, una chiamata in piu' costa un po' di footprint. E qualunque sorpresa
    di formato deve cadere da questo lato."""
    assert contatti_richiesti(Camp(), profilo) is True


# ── Il gate non scavalca cio' che veniva prima ───────────────────────────────

def test_kill_switch_globale_vince_su_tutto(monkeypatch):
    monkeypatch.setattr(browser_bio.settings, "bio_browser_contact_info_enabled", False)
    assert contatti_richiesti(Camp(), {"is_professional_account": True}) is False


@pytest.mark.parametrize("livello", [ENRICHMENT_BIO, ENRICHMENT_NONE])
def test_livello_sotto_contacts_non_chiede_contatti_nemmeno_ai_professional(livello):
    assert contatti_richiesti(Camp(livello), {"is_professional_account": True}) is False


def test_campagna_senza_il_campo_livello_resta_come_prima():
    """Retrocompatibilita' dichiarata: senza il campo si comporta come prima dei
    livelli, cioe' chiede i contatti -- ma il gate professional si applica comunque."""
    assert contatti_richiesti(CampSenzaCampo(), {"is_professional_account": True}) is True
    assert contatti_richiesti(CampSenzaCampo(), {"is_professional_account": False}) is False


def test_gate_disattivabile_senza_toccare_il_resto(monkeypatch):
    """Interruttore di sicurezza: se sul campo il gate perdesse contatti, si spegne
    lui senza spegnere la raccolta contatti."""
    monkeypatch.setattr(browser_bio.settings, "bio_browser_professional_gate_enabled", False)
    assert contatti_richiesti(Camp(), {"is_professional_account": False}) is True
    assert browser_bio.contatore_gate() == 0


def test_gate_regge_su_payload_normalizzato_dal_graphql():
    """Il caso reale del passo 4: il payload arriva dalla GraphQL, quindi passa dal
    traduttore. `is_professional_account` la' e' sempre null e il segnale deve
    sopravvivere alla normalizzazione su `account_type`."""
    gql = {"pk": "1", "username": "shop", "follower_count": 10, "following_count": 2,
           "is_professional_account": None, "account_type": 2}
    shaped = browser_bio.graphql_user_to_web_shape(gql)
    assert contatti_richiesti(Camp(), shaped) is True

    gql_personale = dict(gql, account_type=1)
    shaped_personale = browser_bio.graphql_user_to_web_shape(gql_personale)
    assert contatti_richiesti(Camp(), shaped_personale) is False
