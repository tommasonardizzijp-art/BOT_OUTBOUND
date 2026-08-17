"""Salvataggio idempotente delle chat scoperte dalla Fase A: fusione, P12,
lookup a doppia chiave, concorrenza.

La fusione non e' un caso limite qui: e' l'esito ATTESO di ogni ri-scansione
(spec 5.3), ed e' proprio il percorso che deve rispettare P12 -- il numero non
finisce mai in chiaro in chat_title/display_name, nemmeno passando per la
fusione.
"""
import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.database import AsyncSessionLocal
from app.models.wa import WaDiscoveredChat
from app.services.wa_discover.classifica import (
    TIPO_GRUPPO, TIPO_IGNOTO, TIPO_INDIVIDUALE, RigaScoperta,
)
from app.services.wa_discover.salvataggio import (
    RigaSenzaIdentita, salva_scoperta, tipo_vincente,
)
from app.utils.phone_pseudonym import hmac_e164

from tests.test_wa_discover_modello import _scoperte_di, numero_wa  # noqa: F401


@pytest.mark.asyncio
async def test_il_numero_non_finisce_mai_in_chiaro(db_session, numero_wa):
    """P12. Il 39% delle chat di Primero ha il numero COME TITOLO: se finisse
    in chat_title o display_name, avremmo una tabella piena di numeri in
    chiaro."""
    riga = RigaScoperta(titolo=None, numero="393421460077",
                        numero_leggibile=True, tipo=TIPO_INDIVIDUALE)
    esito = await salva_scoperta(db_session, numero_wa.tenant_id, numero_wa.id, riga)
    assert esito == "creata"

    trovate = await _scoperte_di(db_session, numero_wa.id)
    assert len(trovate) == 1
    salvata = trovate[0]
    assert salvata.chat_title is None
    assert salvata.phone_hmac == hmac_e164("393421460077")
    assert "3421460077" not in (salvata.chat_title or "")
    assert "3421460077" not in (salvata.display_name or "")


@pytest.mark.asyncio
async def test_riscansione_aggiorna_e_non_duplica(db_session, numero_wa):
    """Spec 5.3: ri-scansionare la stessa lista deve essere innocuo."""
    riga = RigaScoperta(titolo="Fulvio", numero=None, numero_leggibile=False,
                        tipo=TIPO_IGNOTO)
    esito = await salva_scoperta(db_session, numero_wa.tenant_id, numero_wa.id, riga)
    assert esito == "creata"

    migliore = RigaScoperta(titolo="Fulvio", numero="393421460077",
                            numero_leggibile=True, tipo=TIPO_INDIVIDUALE)
    esito = await salva_scoperta(db_session, numero_wa.tenant_id, numero_wa.id, migliore)
    assert esito == "aggiornata"

    tutte = await _scoperte_di(db_session, numero_wa.id)
    assert len(tutte) == 1
    assert tutte[0].numero_leggibile is True         # il dato migliore vince
    assert tutte[0].phone_hmac == hmac_e164("393421460077")
    assert tutte[0].tipo_chat == TIPO_INDIVIDUALE
    assert tutte[0].chat_title == "Fulvio"


@pytest.mark.asyncio
async def test_fusione_non_perde_il_numero_gia_salvato(db_session, numero_wa):
    """Il contrario dell'"integra" della spec 5.3: una riga che AVEVA il
    numero non deve mai perderlo perche' un giro successivo non e' riuscito
    ad aprire il pannello (o non ci ha nemmeno provato)."""
    prima = RigaScoperta(titolo="Fulvio", numero="393421460077",
                         numero_leggibile=True, tipo=TIPO_INDIVIDUALE)
    await salva_scoperta(db_session, numero_wa.tenant_id, numero_wa.id, prima)

    pannello_non_aperto = RigaScoperta(titolo="Fulvio", numero=None,
                                       numero_leggibile=False, tipo=TIPO_IGNOTO)
    esito = await salva_scoperta(db_session, numero_wa.tenant_id, numero_wa.id,
                                 pannello_non_aperto)
    assert esito == "aggiornata"

    tutte = await _scoperte_di(db_session, numero_wa.id)
    assert len(tutte) == 1
    assert tutte[0].phone_hmac == hmac_e164("393421460077"), \
        "il numero gia' salvato non deve sparire"
    assert tutte[0].numero_leggibile is True
    assert tutte[0].tipo_chat == TIPO_INDIVIDUALE, \
        "'ignoto' non deve far retrocedere un tipo gia' noto"


@pytest.mark.asyncio
async def test_lookup_trova_per_hmac_quando_il_titolo_arriva_dopo(db_session, numero_wa):
    """Prima scoperta: solo hmac (il titolo E' il numero, P12 lo azzera).
    Seconda scoperta della STESSA chat: ora il titolo e' un nome vero (es. il
    contatto e' stato salvato in rubrica nel frattempo). La lookup deve
    trovare la riga via hmac, non via titolo -- il titolo di prima non
    esisteva."""
    solo_numero = RigaScoperta(titolo=None, numero="393421460077",
                               numero_leggibile=True, tipo=TIPO_INDIVIDUALE)
    esito = await salva_scoperta(db_session, numero_wa.tenant_id, numero_wa.id, solo_numero)
    assert esito == "creata"

    ora_con_nome = RigaScoperta(titolo="Fulvio CBD", numero="393421460077",
                                numero_leggibile=True, tipo=TIPO_INDIVIDUALE)
    esito = await salva_scoperta(db_session, numero_wa.tenant_id, numero_wa.id, ora_con_nome)
    assert esito == "aggiornata"

    tutte = await _scoperte_di(db_session, numero_wa.id)
    assert len(tutte) == 1, "doveva fondersi nella riga trovata via hmac, non duplicare"
    assert tutte[0].chat_title == "Fulvio CBD"
    assert tutte[0].display_name == "Fulvio CBD"


@pytest.mark.asyncio
async def test_titolo_numerico_non_estraibile_si_salva_mascherato(db_session, numero_wa):
    """Il caso descritto nel piano: un titolo che supera `titolo_e_numero` ma
    da cui `numero_dal_titolo` non estrae nulla (troppe cifre per E.164).
    Senza passare da `etichetta_visibile` la riga finirebbe con
    chat_title=None E phone_hmac=None -- nessuna identita', duplicata a ogni
    ri-scansione. Qui invece deve restare stabile e mascherata."""
    titolo_ostile = "+39 342 146 0077 99 88 77"  # troppe cifre, misurato nel piano
    riga = RigaScoperta(titolo=titolo_ostile, numero=None,
                        numero_leggibile=False, tipo=TIPO_IGNOTO)

    esito = await salva_scoperta(db_session, numero_wa.tenant_id, numero_wa.id, riga)
    assert esito == "creata"

    esito = await salva_scoperta(db_session, numero_wa.tenant_id, numero_wa.id, riga)
    assert esito == "aggiornata", "la stessa riga rivista deve trovare l'identita' mascherata"

    tutte = await _scoperte_di(db_session, numero_wa.id)
    assert len(tutte) == 1
    assert tutte[0].chat_title is not None
    assert tutte[0].phone_hmac is None
    numeri_in_chiaro = "3421460077998877"
    assert numeri_in_chiaro not in (tutte[0].chat_title or "")


@pytest.mark.asyncio
async def test_riga_senza_identita_non_si_scrive(db_session, numero_wa):
    """Trovato in review (adversarial I, 15/08): titolo vuoto E numero non
    letto -> ne' chat_title ne' phone_hmac, nessuna delle due
    UniqueConstraint di wa_discovered_chats potrebbe riconoscere la riga in
    futuro (NULL != NULL in SQL). L'unico chiamante di produzione
    (wa_discover_run.py) gia' filtra i titoli vuoti PRIMA di arrivare qui --
    ma quel filtro vive nel CHIAMANTE, non in salva_scoperta: un secondo
    chiamante (es. uno script di recupero mirato che chiama salva_scoperta
    direttamente -- ne esiste gia' uno, scritto per recuperare a mano le
    chat senza numero) che non lo replicasse scriverebbe la riga orfana che
    il docstring del modulo dice di voler escludere strutturalmente. La
    guardia deve stare QUI, e deve sollevare
    (non un valore di ritorno stringa come 'creata'/'aggiornata' che un
    chiamante distratto ignorerebbe)."""
    riga = RigaScoperta(titolo=None, numero=None, numero_leggibile=False,
                        tipo=TIPO_IGNOTO)
    with pytest.raises(RigaSenzaIdentita):
        await salva_scoperta(db_session, numero_wa.tenant_id, numero_wa.id, riga)

    trovate = await _scoperte_di(db_session, numero_wa.id)
    assert trovate == [], "nessuna riga deve essere stata scritta"


@pytest.mark.asyncio
async def test_una_riga_promossa_non_torna_indietro(db_session, numero_wa):
    """Stessa logica di stato_vincente nel gemello Instagram: uno stato piu'
    avanzato non retrocede. Una chat gia' promossa a WaContact che tornasse
    'nuovo' verrebbe promossa una seconda volta."""
    riga = RigaScoperta(titolo="Fulvio", numero=None, numero_leggibile=False,
                        tipo=TIPO_IGNOTO)
    await salva_scoperta(db_session, numero_wa.tenant_id, numero_wa.id, riga)

    salvata = (await _scoperte_di(db_session, numero_wa.id))[0]
    salvata.status = "promosso"
    await db_session.commit()

    riscansione = RigaScoperta(titolo="Fulvio", numero="393421460077",
                               numero_leggibile=True, tipo=TIPO_INDIVIDUALE)
    esito = await salva_scoperta(db_session, numero_wa.tenant_id, numero_wa.id, riscansione)
    assert esito == "aggiornata"

    tutte = await _scoperte_di(db_session, numero_wa.id)
    assert len(tutte) == 1, "la ri-scansione di una riga promossa non deve duplicarla"
    assert tutte[0].status == "promosso", \
        "una riga gia' promossa non deve tornare 'nuovo'"
    assert tutte[0].phone_hmac == hmac_e164("393421460077"), \
        "la fusione continua a integrare anche su una riga promossa"


def test_precedenza_di_tipo():
    assert tipo_vincente(TIPO_IGNOTO, TIPO_INDIVIDUALE) == TIPO_INDIVIDUALE
    assert tipo_vincente(TIPO_INDIVIDUALE, TIPO_IGNOTO) == TIPO_INDIVIDUALE
    assert tipo_vincente(TIPO_IGNOTO, TIPO_GRUPPO) == TIPO_GRUPPO
    # Nessun ordine tra individuale e gruppo: chi c'era prima resta, un
    # giro non deve far oscillare la classificazione.
    assert tipo_vincente(TIPO_INDIVIDUALE, TIPO_GRUPPO) == TIPO_INDIVIDUALE
    assert tipo_vincente(TIPO_GRUPPO, TIPO_INDIVIDUALE) == TIPO_GRUPPO


@pytest.mark.asyncio
async def test_due_scansioni_concorrenti_stessa_chat_una_riga_sola(numero_wa):
    """ADVERSARIAL: due `salva_scoperta` concorrenti sulla STESSA chat, via
    `asyncio.gather` reale su due sessioni DB indipendenti -- non
    sequenziale. Identificata solo dall'hmac (titolo None per P12): entrambe
    le INSERT puntano allo stesso vincolo UNIQUE(number_id, phone_hmac), e la
    finestra fra la SELECT esplicita e il COMMIT puo' far vedere "nessuna
    riga" a entrambe, facendole collidere in fase di INSERT.
    """
    tenant_id, number_id = numero_wa.tenant_id, numero_wa.id
    riga = RigaScoperta(titolo=None, numero="393421460077",
                        numero_leggibile=True, tipo=TIPO_INDIVIDUALE)

    async def _salva():
        async with AsyncSessionLocal() as db:
            return await salva_scoperta(db, tenant_id, number_id, riga)

    risultati = await asyncio.gather(_salva(), _salva(), return_exceptions=True)

    errori = [r for r in risultati if isinstance(r, BaseException)]
    assert not errori, f"IntegrityError non gestita: {errori}"
    assert set(risultati) <= {"creata", "aggiornata"}
    assert "aggiornata" in risultati, "una delle due deve essere ripiegata sulla fusione"

    async with AsyncSessionLocal() as db:
        righe = (await db.execute(
            select(WaDiscoveredChat).where(WaDiscoveredChat.number_id == number_id)
        )).scalars().all()
    assert len(righe) == 1, "due scoperte concorrenti sulla stessa chat devono produrre una riga sola"
    assert righe[0].phone_hmac == hmac_e164("393421460077")


@pytest.mark.asyncio
async def test_scoperte_su_due_numeri_restano_distinte(db_session, numero_wa):
    """La lookup e' sempre filtrata per number_id: la stessa persona in
    rubriche di due numeri diversi e' due scoperte, non una fusione."""
    from app.models.wa import WaNumber
    import uuid

    secondo = WaNumber(
        id=str(uuid.uuid4()), tenant_id=numero_wa.tenant_id, label="secondo",
        phone_hmac=f"h-{uuid.uuid4().hex[:8]}", encrypted_phone="e2",
        daily_cap=100, warmup_day=0, sent_today=0, sent_date=None,
    )
    db_session.add(secondo)
    await db_session.flush()

    riga = RigaScoperta(titolo="Fulvio", numero=None, numero_leggibile=False,
                        tipo=TIPO_IGNOTO)
    await salva_scoperta(db_session, numero_wa.tenant_id, numero_wa.id, riga)
    await salva_scoperta(db_session, numero_wa.tenant_id, secondo.id, riga)

    assert len(await _scoperte_di(db_session, numero_wa.id)) == 1
    assert len(await _scoperte_di(db_session, secondo.id)) == 1


@pytest.mark.asyncio
async def test_il_nome_vero_sostituisce_l_etichetta_mascherata(db_session, numero_wa):
    """Il caso piu' frequente di tutti per un negozio, e quello che la regola
    "integra, non sovrascrive" da sola sbaglia.

    Un contatto non ancora in rubrica ha il NUMERO come titolo (39% delle chat
    di Primero): si salva mascherato, perche' P12 vieta il chiaro. Poi il
    cliente salva quel contatto in rubrica, e alla ri-scansione WhatsApp mostra
    'Mario Rossi'. La riga si ritrova per hmac -- ma se chat_title non viene
    aggiornato perche' "era gia' valorizzato", quel contatto resta
    '+39.....077' per sempre, e in Fase B l'operatore deve approvare una lista
    di maschere invece di nomi.

    Un'etichetta mascherata non e' un dato: e' un segnaposto in attesa del nome.
    """
    from app.services.wa_discover.classifica import RigaScoperta
    from app.services.wa_discover.salvataggio import salva_scoperta

    number_id, tenant_id = numero_wa.id, numero_wa.tenant_id

    # Primo giro: il titolo E' il numero -> etichetta mascherata.
    assert await salva_scoperta(db_session, tenant_id, number_id, RigaScoperta(
        titolo="+39 342 146 0077", numero="393421460077",
        numero_leggibile=True, tipo="individuale")) == "creata"

    prima = (await _scoperte_di(db_session, number_id))[0]
    assert "\u2022" in (prima.chat_title or ""), "il primo giro deve mascherare"

    # Secondo giro: il contatto e' finito in rubrica, WhatsApp mostra il nome.
    assert await salva_scoperta(db_session, tenant_id, number_id, RigaScoperta(
        titolo="Mario Rossi", numero="393421460077",
        numero_leggibile=True, tipo="individuale")) == "aggiornata"

    righe = await _scoperte_di(db_session, number_id)
    assert len(righe) == 1, "stessa persona: deve restare una riga sola"
    assert righe[0].chat_title == "Mario Rossi"
    assert righe[0].display_name == "Mario Rossi"


@pytest.mark.asyncio
async def test_una_maschera_non_sostituisce_un_nome_vero(db_session, numero_wa):
    """Il verso opposto NON deve accadere: se un giro successivo non riesce a
    leggere il nome e ripiega sulla maschera, un nome gia' noto non si perde."""
    from app.services.wa_discover.classifica import RigaScoperta
    from app.services.wa_discover.salvataggio import salva_scoperta

    number_id, tenant_id = numero_wa.id, numero_wa.tenant_id

    await salva_scoperta(db_session, tenant_id, number_id, RigaScoperta(
        titolo="Mario Rossi", numero="393421460077",
        numero_leggibile=True, tipo="individuale"))
    await salva_scoperta(db_session, tenant_id, number_id, RigaScoperta(
        titolo="+39 342 146 0077", numero="393421460077",
        numero_leggibile=True, tipo="individuale"))

    righe = await _scoperte_di(db_session, number_id)
    assert len(righe) == 1
    assert righe[0].chat_title == "Mario Rossi", "un nome vero non si degrada a maschera"
