"""Probe di SOLA LETTURA per la fusione dei 9 duplicati phone_hmac (AVVIO
12/08, §1). Nessuna scrittura: legge wa_contacts, decifra encrypted_phone,
raggruppa per (tenant_id, numero canonico), e per ogni gruppo con piu' di un
WaContact stampa PRIMA + piano di fusione proposto (chi si tiene, chi si
cancella, quante righe si riassegnano).

Uso: dalla root backend, con venv attivo:
    python -m scripts.probe_hmac_duplicati
"""
import asyncio
from collections import defaultdict

from sqlalchemy import func, select

from app.database import AsyncSessionLocal
from app.models.wa import WaCampaignContact, WaContact, WaInboundEvent, WaMessage
from app.utils.crypto import decrypt
from app.utils.phone_pseudonym import normalize_e164


async def main():
    async with AsyncSessionLocal() as db:
        contatti = (await db.execute(select(WaContact))).scalars().all()
        print(f"wa_contacts totali: {len(contatti)}")

        gruppi = defaultdict(list)
        falliti = []
        for c in contatti:
            try:
                piano = decrypt(c.encrypted_phone)
                canonico = normalize_e164(piano)
            except Exception as e:
                falliti.append((c.id, str(e)))
                continue
            gruppi[(c.tenant_id, canonico)].append(c)

        if falliti:
            print(f"\n!! {len(falliti)} righe non decifrabili/normalizzabili (BLOCCANTE, fermarsi):")
            for cid, err in falliti:
                print(f"   {cid}: {err}")

        duplicati = {k: v for k, v in gruppi.items() if len(v) > 1}
        print(f"\nGruppi con piu' di un WaContact per lo stesso numero: {len(duplicati)}")

        if not duplicati:
            print("Nessun duplicato trovato. STOP: non procedere con la fusione, non c'e' niente da fondere.")
            return

        for (tenant_id, canonico), righe in duplicati.items():
            print(f"\n{'='*70}")
            print(f"tenant={tenant_id}  numero_canonico={canonico}  righe={len(righe)}")
            if len(righe) != 2:
                print(f"   !! ATTENZIONE: {len(righe)} righe, non 2 -- caso fuori dai 9 attesi, guardare a mano prima di fondere")

            schede = []
            for c in righe:
                n_camp = await db.scalar(
                    select(func.count()).select_from(WaCampaignContact)
                    .where(WaCampaignContact.contact_id == c.id))
                n_msg = await db.scalar(
                    select(func.count()).select_from(WaMessage)
                    .where(WaMessage.contact_id == c.id))
                n_evt = await db.scalar(
                    select(func.count()).select_from(WaInboundEvent)
                    .where(WaInboundEvent.contact_id == c.id))
                schede.append({
                    "contatto": c, "n_campaign_contacts": n_camp,
                    "n_messages": n_msg, "n_inbound_events": n_evt,
                })
                print(f"  - id={c.id}")
                print(f"      phone_hmac={c.phone_hmac}")
                print(f"      first_seen_at={c.first_seen_at}  chat_title={c.chat_title!r}  display_name={c.display_name!r}")
                print(f"      opted_out={c.opted_out}  do_not_contact={c.do_not_contact}")
                print(f"      wa_campaign_contacts={n_camp}  wa_messages={n_msg}  wa_inbound_events={n_evt}")

            # Punteggio: arruolamento in campagna pesa piu' di tutto (e' la
            # storia che conta davvero: un contatto arruolato ha next_action_at,
            # stato di sequenza, eventuale opt-out legato a una campagna VERA).
            # A parita', vince chi ha piu' messaggi+eventi. A parita' totale,
            # vince il piu' vecchio (first_seen_at piu' basso).
            def punteggio(s):
                return (s["n_campaign_contacts"] > 0,
                        s["n_messages"] + s["n_inbound_events"],
                        -s["contatto"].first_seen_at.timestamp())

            schede.sort(key=punteggio, reverse=True)
            tieni, scarta = schede[0], schede[1]

            print(f"\n  PROPOSTA: tenere id={tieni['contatto'].id} (phone_hmac={tieni['contatto'].phone_hmac})")
            print(f"            cancellare id={scarta['contatto'].id} (phone_hmac={scarta['contatto'].phone_hmac})")
            if scarta["n_campaign_contacts"] or scarta["n_messages"] or scarta["n_inbound_events"]:
                print(f"  DA RIASSEGNARE dal cancellato al tenuto prima del delete:")
                print(f"     wa_campaign_contacts: {scarta['n_campaign_contacts']} righe")
                print(f"     wa_messages:          {scarta['n_messages']} righe")
                print(f"     wa_inbound_events:    {scarta['n_inbound_events']} righe")
            else:
                print(f"  Il contatto da cancellare non ha nessuna riga collegata: nessuna riassegnazione necessaria, solo un DELETE.")

            # Guardia critica: se ENTRAMBI i gemelli sono arruolati (magari in
            # campagne diverse), riassegnare wa_campaign_contacts puo' violare
            # la UniqueConstraint(campaign_id, contact_id) se la stessa
            # campagna li ha entrambi -- caso da fermarsi e guardare a mano,
            # mai risolvere con un ON CONFLICT silenzioso.
            camp_tieni = set((await db.execute(
                select(WaCampaignContact.campaign_id)
                .where(WaCampaignContact.contact_id == tieni["contatto"].id))).scalars().all())
            camp_scarta = set((await db.execute(
                select(WaCampaignContact.campaign_id)
                .where(WaCampaignContact.contact_id == scarta["contatto"].id))).scalars().all())
            sovrapposte = camp_tieni & camp_scarta
            if sovrapposte:
                print(f"  !! CONFLITTO: entrambi i contatti sono arruolati nella/e stessa/e campagna/e {sovrapposte} "
                      f"-- riassegnare wa_campaign_contacts violerebbe la UNIQUE(campaign_id, contact_id). "
                      f"Questa coppia va risolta A MANO, non dallo script di fusione automatico.")

        print(f"\n{'='*70}")
        print(f"Riepilogo: {len(duplicati)} gruppi duplicati, "
              f"{sum(1 for _ in duplicati.values())} fusioni proposte.")
        print("Nessuna scrittura eseguita da questo script.")


if __name__ == "__main__":
    asyncio.run(main())
