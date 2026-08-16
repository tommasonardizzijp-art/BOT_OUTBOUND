"""Fase B, Task 2: staging (`wa_discovered_chats`) -> `WaContact`.

Stesso schema di persistenza di `wa_ingest.ingerisci_csv` (lookup ESPLICITA,
mai un INSERT lasciato parlare al vincolo; `db.begin_nested()`/`IntegrityError`
come ripiego sulla concorrenza; gap-fill che integra e non cancella) -- la
differenza di fondo e' che qui `phone_hmac`/`encrypted_phone` si RIUSANO cosi'
come sono (vincolo globale del piano Fase B): la riga scoperta li porta gia'
nello stesso formato di `WaContact` (`salvataggio.py` li scrive con
`hmac_e164(riga.numero)`/`encrypt(riga.numero)`, forma canonica CON '+' --
vero solo dopo la migrazione dell'AVVIO 12/08 §1: prima la Fase A scriveva
la forma nuda, e questa docstring lo affermava senza che nessun test lo
verificasse),
quindi decifrare e ri-cifrare sarebbe lavoro sprecato e un'occasione in piu'
per un numero in chiaro in un log.

Un solo commit a fine batch (non per riga): un batch di N id e' una
transazione sola, coerente con "innocua ri-promozione" -- se fallisce a meta',
niente scritto a meta'. Il SAVEPOINT (`db.begin_nested()`) dentro il loop
protegge la singola INSERT concorrente fra due chiamate `promuovi()` diverse
(due sessioni), non e' in conflitto col commit unico: e' lo stesso schema di
`wa_ingest.ingerisci_csv`, che fa la savepoint per riga ma un solo
`db.commit()` alla fine della funzione.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.wa import WaContact, WaDiscoveredChat
from app.services.wa_discover.classifica import e_etichetta_mascherata
from app.services.wa_promote.regole import promuovibile
from app.utils.ids import uuid_valido


def _gap_fill(contatto: WaContact, riga: WaDiscoveredChat) -> None:
    """Aggiorna `display_name`/`chat_title` con cio' che la riga scoperta
    porta, senza mai cancellare cio' che c'era e senza mai far passare una
    maschera (P12) per un nome vero. Un solo punto per i due rami che
    riusano un `WaContact` esistente (trovato subito, o dopo l'IntegrityError
    di corsa): erano rimasti divergenti -- il primo aggiornava solo
    `display_name`, mai `chat_title`, mentre la creazione ex-novo valorizza
    entrambi. Trovato in review finale di branch."""
    if riga.display_name and not e_etichetta_mascherata(riga.display_name):
        contatto.display_name = riga.display_name
    if riga.chat_title and not e_etichetta_mascherata(riga.chat_title):
        contatto.chat_title = riga.chat_title


@dataclass
class Scarto:
    id: str
    motivo: str


@dataclass
class ReportPromozione:
    promossi: int = 0
    contatti_creati: int = 0
    contatti_riusati: int = 0
    gia_dnc: int = 0
    scarti: list[Scarto] = field(default_factory=list)
    contatti_promossi_ids: list[str] = field(default_factory=list)


async def promuovi(db, *, tenant_id: str, ids: list[str]) -> ReportPromozione:
    """Promuove ogni id di `wa_discovered_chats` a `WaContact`, se `regole.
    promuovibile` lo consente. Idempotente: una riga gia' 'promosso' si
    riscarta con quello stesso motivo, non si ripromuove una seconda volta.

    `tenant_id` e' obbligatorio ed e' un confine di sicurezza, non solo un
    filtro: se l'id esiste ma appartiene a un `wa_discovered_chats` di un
    ALTRO tenant, si scarta con lo STESSO motivo di un id inesistente
    ('non_trovato') -- mai un motivo diverso tipo 'altro_tenant', altrimenti
    chi chiama con un id indovinato distinguerebbe "non esiste" da "esiste ma
    non e' tuo" (nessuna informazione persa all'esterno). Stesso principio
    "per costruzione" del vincolo globale sui gruppi (Task 1): anche se in
    futuro un bug lato API (Task 4) passasse qui un id di un tenant diverso
    da quello della richiesta autenticata, questa funzione non lo promuove.

    L'opt-out NON blocca questo passo (vince solo sull'arruolamento, Task 3):
    un contatto gia' opted_out/do_not_contact ritrovato qui diventa comunque
    (o resta) un `WaContact` regolare, la riga passa comunque a 'promosso' --
    solo si conta anche in `gia_dnc`, informativo, e non entra in
    `contatti_promossi_ids` (a valle nessuno lo proporrebbe per l'arruolamento,
    Task 3 lo respingerebbe comunque).
    """
    report = ReportPromozione()
    adesso = datetime.utcnow()

    for id_ in ids:
        if not uuid_valido(id_):
            # Si scarta PRIMA della query, stesso motivo "non_trovato" di un
            # id valido ma inesistente -- nessuna differenza osservabile fra
            # le due cause, stesso principio del confine di sicurezza sul
            # tenant sopra.
            report.scarti.append(Scarto(id_, "non_trovato"))
            continue

        riga = await db.get(WaDiscoveredChat, id_)
        if riga is None or riga.tenant_id != tenant_id:
            report.scarti.append(Scarto(id_, "non_trovato"))
            continue

        esito = promuovibile(riga)
        if not esito.ok:
            report.scarti.append(Scarto(id_, esito.motivo))
            continue

        contatto = await db.scalar(
            select(WaContact).where(WaContact.tenant_id == riga.tenant_id,
                                    WaContact.phone_hmac == riga.phone_hmac))

        if contatto is None:
            # P12: mai un'etichetta mascherata (numero mascherato) dentro
            # WaContact.chat_title NE' display_name -- se il titolo scoperto
            # e' una maschera (o manca del tutto), il contatto nasce senza
            # chat_title/display_name, non con una maschera che sembrerebbe
            # un nome. e_etichetta_mascherata(None) e' False, quindi
            # l'espressione copre anche quel caso da sola. `chat_title` e
            # `display_name` di `WaDiscoveredChat` arrivano SEMPRE dalla
            # stessa sorgente (`etichetta_visibile` in salvataggio.py), quindi
            # la stessa maschera puo' comparire in entrambe le colonne -- il
            # guard va applicato a entrambe, non solo a chat_title.
            chat_title = (riga.chat_title
                         if not e_etichetta_mascherata(riga.chat_title) else None)
            display_name = (riga.display_name
                            if not e_etichetta_mascherata(riga.display_name) else None)
            contatto = WaContact(
                tenant_id=riga.tenant_id, phone_hmac=riga.phone_hmac,
                encrypted_phone=riga.encrypted_phone, display_name=display_name,
                chat_title=chat_title, first_seen_at=adesso,
            )
            try:
                # SAVEPOINT: due promozioni concorrenti sullo stesso
                # phone_hmac (due righe scoperte diverse, stesso numero
                # trovato da due wa_numbers dello stesso tenant, o un doppio
                # click sullo stesso batch) possono passare ENTRAMBE la
                # SELECT sopra prima che l'altra faccia INSERT -- stesso
                # bug di corsa gia' risolto in wa_ingest.ingerisci_csv.
                async with db.begin_nested():
                    db.add(contatto)
                    await db.flush()
                report.contatti_creati += 1
            except IntegrityError:
                # L'altra chiamata ha vinto la corsa: si rilegge e si
                # procede come nel ramo "trovato" sotto. Guard esplicito su
                # None (a differenza di ogni altro lookup in questo file):
                # sotto READ COMMITTED la riga vincente deve esserci sempre,
                # ma se quell'assunzione si rompesse un AttributeError grezzo
                # sarebbe peggio di un errore chiaro. Trovato in review
                # finale di branch.
                contatto = await db.scalar(
                    select(WaContact).where(WaContact.tenant_id == riga.tenant_id,
                                            WaContact.phone_hmac == riga.phone_hmac))
                if contatto is None:
                    raise
                _gap_fill(contatto, riga)
                report.contatti_riusati += 1
        else:
            # Gap-fill (stesso principio di wa_ingest Q16): si aggiorna cio'
            # che la riga scoperta porta, non si cancella cio' che c'era --
            # e una maschera non e' "cio' che la riga scoperta porta" nel
            # senso utile: e' un segnaposto, non un nome, e sovrascrivere un
            # display_name vero gia' salvato con una maschera sarebbe
            # l'opposto di "integra, non cancella". Trovato con un test
            # dedicato: senza il guard, un contatto con display_name="Mario
            # Rossi" veniva sovrascritto da una ri-scoperta con pannello non
            # apribile ("+39•••••077"). Stesso guard vale per chat_title
            # (review finale di branch: era rimasto scoperto).
            _gap_fill(contatto, riga)
            report.contatti_riusati += 1

        # status non torna mai indietro (vincolo globale): da qui in poi la
        # riga e' sempre 'promosso', qualunque sia l'esito DNC sotto.
        riga.status = "promosso"
        riga.updated_at = adesso

        report.promossi += 1
        if contatto.opted_out or contatto.do_not_contact:
            # L'opt-out non impedisce "diventare WaContact" -- impedisce solo
            # l'arruolamento in campagna (Task 3), non questo passo.
            report.gia_dnc += 1
        else:
            report.contatti_promossi_ids.append(contatto.id)

    await db.flush()
    await db.commit()

    logger.info(f"[WA promote] promossi={report.promossi} "
               f"creati={report.contatti_creati} riusati={report.contatti_riusati} "
               f"dnc={report.gia_dnc} scarti={len(report.scarti)}")
    return report
