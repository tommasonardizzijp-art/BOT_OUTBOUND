"""Fusione VERA dei 9 duplicati phone_hmac (AVVIO 12/08, §1, passo 1).

Decisione per ogni coppia (manuale, dopo aver letto lo storico completo di
campagne/messaggi/eventi -- NON il punteggio automatico di
probe_hmac_duplicati.py, che per 6 coppie su 9 avrebbe tenuto il contatto
sbagliato): si tiene sempre il contatto collegato alla campagna REALE in
corso ('Campagna test PRIMERO FULVIO', running), anche se non ancora
inviato -- e' quello la cui identita' conta per il gate opt-out andando
avanti. Si scarta il gemello nato nei collaudi 07-09/08. L'unica coppia dove
nessuno dei due e' nella campagna reale (la prima) tiene quello con storia.

Ogni coppia: riassegna wa_campaign_contacts + wa_messages + wa_inbound_events
dallo scartato al tenuto, poi cancella lo scartato. Una SAVEPOINT per
coppia dentro un'unica sessione/transazione: se una coppia fallisce si
ferma li', le coppie gia' fuse restano (ognuna e' verificata prima di
passare alla successiva). Nessun ON CONFLICT: se una riassegnazione
violerebbe la UniqueConstraint(campaign_id, contact_id) lo script salta
quella coppia PRIMA di scrivere (verificato in anticipo: nessuna delle 9 ce
l'ha, il controllo resta per sicurezza).

Uso:
    HMAC_MERGE_DRY_RUN=1 python -m scripts.fondi_hmac_duplicati   # simula, rollback finale
    python -m scripts.fondi_hmac_duplicati                        # scrive davvero
"""
import asyncio
import os

from sqlalchemy import func, select, update

from app.database import AsyncSessionLocal
from app.models.wa import (WaCampaignContact, WaContact, WaInboundEvent,
                           WaMessage)

DRY_RUN = os.environ.get("HMAC_MERGE_DRY_RUN") == "1"

# (tieni, scarta, numero_canonico per il log)
COPPIE = [
    ("8fd4994f-c1d4-4b5b-9b2d-f5109213846e", "a5b39b76-042a-4416-8b9d-e4fb30516221", "393661376721"),
    ("3dadf6e1-78f4-4134-8ea3-bf3410c86206", "52e6d351-0447-464b-a7ad-1718034ecd52", "393421460077"),
    ("3dd50166-5ffe-46a3-bdf5-dd4ef0f4d219", "12984ca4-27bd-439f-be29-1e218193c51b", "393248373460"),
    ("d318577f-ac03-4127-a8ed-10fde8339d1e", "d732c0ea-89fa-4ccb-98a0-2eb8464ed1cc", "393739039859"),
    ("dc504015-3d65-4eea-af1f-3db51f83732c", "b4805a8a-4af4-42b8-af10-db48d629f7fe", "393464200572"),
    ("900033cf-895e-427d-934b-00cf76595516", "801f390d-1463-481e-bb0b-2a7da2058f8d", "393756208534"),
    ("57f64609-b848-40d5-8e1a-5dc49e2ab8dc", "5cb7c48a-3fd5-426d-a06d-0f82d086a780", "393737456614"),
    ("c952ee77-7f89-4112-af18-6d4ce9adf5cc", "15c48224-2bdb-486b-a110-2a4e70956350", "393925957527"),
    ("f09b850e-c3be-43c7-b878-1e53ee4851e1", "58f0f0d0-34c2-4363-ad56-fcb351db6b2c", "393758553776"),
]


async def conta(db, model, contact_id):
    return await db.scalar(select(func.count()).select_from(model).where(model.contact_id == contact_id))


async def fondi_coppia(db, tieni_id, scarta_id, numero):
    print(f"\n{'='*70}\nnumero={numero}  tieni={tieni_id}  scarta={scarta_id}")

    tieni = await db.get(WaContact, tieni_id)
    scarta = await db.get(WaContact, scarta_id)
    if tieni is None or scarta is None:
        print("  !! uno dei due contatti non esiste piu' (gia' fuso?), salto")
        return False

    # Guardia anti ON-CONFLICT-silenzioso: se le due liste di campaign_id si
    # sovrappongono, riassegnare wa_campaign_contacts violerebbe la
    # UniqueConstraint(campaign_id, contact_id) -- ci si ferma, non si scrive.
    camp_tieni = set((await db.execute(
        select(WaCampaignContact.campaign_id).where(WaCampaignContact.contact_id == tieni_id))).scalars().all())
    camp_scarta = set((await db.execute(
        select(WaCampaignContact.campaign_id).where(WaCampaignContact.contact_id == scarta_id))).scalars().all())
    sovrapposte = camp_tieni & camp_scarta
    if sovrapposte:
        print(f"  !! CONFLITTO campagne sovrapposte {sovrapposte}: coppia saltata, va risolta a mano")
        return False

    prima_camp_t, prima_camp_s = await conta(db, WaCampaignContact, tieni_id), await conta(db, WaCampaignContact, scarta_id)
    prima_msg_t, prima_msg_s = await conta(db, WaMessage, tieni_id), await conta(db, WaMessage, scarta_id)
    prima_evt_t, prima_evt_s = await conta(db, WaInboundEvent, tieni_id), await conta(db, WaInboundEvent, scarta_id)

    print(f"  PRIMA  tieni:  campaign_contacts={prima_camp_t} messages={prima_msg_t} inbound_events={prima_evt_t}")
    print(f"  PRIMA  scarta: campaign_contacts={prima_camp_s} messages={prima_msg_s} inbound_events={prima_evt_s}")

    esito = {}

    class _AnnullaSimulazione(Exception):
        pass

    try:
        async with db.begin_nested():
            r1 = await db.execute(update(WaCampaignContact).where(WaCampaignContact.contact_id == scarta_id)
                                  .values(contact_id=tieni_id))
            r2 = await db.execute(update(WaMessage).where(WaMessage.contact_id == scarta_id)
                                  .values(contact_id=tieni_id))
            r3 = await db.execute(update(WaInboundEvent).where(WaInboundEvent.contact_id == scarta_id)
                                  .values(contact_id=tieni_id))

            # Gap-fill: se il tenuto non ha chat_title/display_name ma lo
            # scartato si', si integra (stesso principio di _fondi in
            # salvataggio.py) -- non capita in questo lotto (verificato dal
            # probe) ma il guard resta per sicurezza.
            if tieni.chat_title is None and scarta.chat_title is not None:
                tieni.chat_title = scarta.chat_title
            if tieni.display_name is None and scarta.display_name is not None:
                tieni.display_name = scarta.display_name

            await db.delete(scarta)
            await db.flush()

            # Letti DENTRO il savepoint: la stessa sessione vede gia' le
            # scritture non ancora committate, cosi' il DOPO e' verificabile
            # anche in DRY_RUN, prima di decidere se tenerle o buttarle.
            esito["dopo_camp_t"] = await conta(db, WaCampaignContact, tieni_id)
            esito["dopo_msg_t"] = await conta(db, WaMessage, tieni_id)
            esito["dopo_evt_t"] = await conta(db, WaInboundEvent, tieni_id)
            esito["scarta_presente"] = (await db.get(WaContact, scarta_id)) is not None
            esito["r1"], esito["r2"], esito["r3"] = r1.rowcount, r2.rowcount, r3.rowcount

            if DRY_RUN:
                raise _AnnullaSimulazione()
    except _AnnullaSimulazione:
        pass

    if not DRY_RUN:
        await db.commit()

    print(f"  {'[SIMULAZIONE, rollback fatto] ' if DRY_RUN else ''}DOPO   tieni:  "
         f"campaign_contacts={esito['dopo_camp_t']} messages={esito['dopo_msg_t']} inbound_events={esito['dopo_evt_t']}")
    print(f"  righe riassegnate: campaign_contacts={esito['r1']} messages={esito['r2']} inbound_events={esito['r3']}")
    print(f"  scartato ancora presente (nella transazione): {esito['scarta_presente']} (deve essere False)")

    ok = (esito["dopo_camp_t"] == prima_camp_t + prima_camp_s
         and esito["dopo_msg_t"] == prima_msg_t + prima_msg_s
         and esito["dopo_evt_t"] == prima_evt_t + prima_evt_s
         and not esito["scarta_presente"])
    print(f"  VERIFICA: {'OK' if ok else '!! MISMATCH, guardare a mano'}")
    return ok


async def main():
    print(f"MODALITA': {'DRY RUN (nessuna scrittura sopravvive)' if DRY_RUN else 'SCRITTURA REALE'}")
    async with AsyncSessionLocal() as db:
        risultati = []
        for tieni_id, scarta_id, numero in COPPIE:
            ok = await fondi_coppia(db, tieni_id, scarta_id, numero)
            risultati.append((numero, ok))

        print(f"\n{'='*70}\nRiepilogo:")
        for numero, ok in risultati:
            print(f"  {numero}: {'OK' if ok else 'DA CONTROLLARE'}")

        if DRY_RUN:
            await db.rollback()
            print("\nDRY RUN: rollback finale eseguito, nessuna scrittura persistita.")
        else:
            totale = await db.scalar(select(func.count()).select_from(WaContact))
            print(f"\nwa_contacts totali dopo la fusione: {totale} (era 258, atteso 258-9=249)")


if __name__ == "__main__":
    asyncio.run(main())
