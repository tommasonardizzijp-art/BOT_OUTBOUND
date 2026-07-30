"""Seed di una campagna WhatsApp senza passare dalla UI (contratto M2/M3
§7.4). Scritto da M2 in PR-0, usato da M3 per generarsi dati: e' la ragione
tecnica per cui i due cantieri possono partire lo stesso giorno.

Idempotente: rilanciarlo con gli stessi argomenti non duplica nulla
(get-or-create su (tenant, phone_hmac) e su (tenant, campaign_name)).

Esempio:
    python -m scripts.wa_seed_campaign \\
        --tenant-label "Tenant Test M3" \\
        --number-label "Numero test M3" \\
        --number-phone "+39XXXXXXXXXX" \\
        --browser-profile "data/browser_profiles/wa_test_m3" \\
        --contact "+39XXXXXXXXXX" --contact "+39YYYYYYYYYY" \\
        --campaign-name "M3 smoke" \\
        --campaign-type followup \\
        --template "Ciao {nome}, {questo|quello} e' un test." \\
        --daily-cap 3 \\
        [--start] [--dry-run] [--force-number-active]
"""
import argparse
import asyncio
from datetime import datetime

from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.tenant import Tenant
from app.models.wa import (WaCampaign, WaCampaignContact, WaCampaignStatus,
                           WaCampaignType, WaContact, WaContactStatus, WaNumber,
                           WaNumberStatus, WaSendCondition, WaSequenceStep)
from app.utils.crypto import encrypt
from app.utils.phone_pseudonym import hmac_phone, mask_phone


def _assert_db_di_test(url: str, forzato: bool) -> None:
    """L'08/07 i test hanno creato 110 campagne fantasma su Supabase
    PRODUZIONE. Uno script di seed e' la stessa arma con la sicura tolta."""
    if forzato:
        return
    if not (url.startswith("sqlite") or "test" in url.lower()):
        raise SystemExit(
            f"DATABASE_URL non sembra un database di test ({url[:40]}...). "
            "Se sai cosa stai facendo, ripeti con --i-know-what-im-doing."
        )


def _assert_profilo_non_poc1(path: str | None) -> None:
    if path and "wa-poc" in path.replace("/", "\\").lower():
        raise SystemExit(
            "Questo e' il profilo di PoC-1, in corso fino al 10/08: aprirlo "
            "rischia un re-scan del QR e azzera 14 giorni di misura."
        )


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--tenant-label", required=True)
    p.add_argument("--number-label", required=True)
    p.add_argument("--number-phone", required=True)
    p.add_argument("--browser-profile", default=None)
    p.add_argument("--contact", action="append", dest="contacts", required=True)
    p.add_argument("--campaign-name", required=True)
    p.add_argument("--campaign-type", choices=["marketing", "followup"], required=True)
    p.add_argument("--template", required=True)
    p.add_argument("--daily-cap", type=int, default=20)
    p.add_argument("--start", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force-number-active", action="store_true")
    p.add_argument("--i-know-what-im-doing", action="store_true")
    return p.parse_args()


async def _get_or_create_tenant(db, label: str) -> Tenant:
    tenant = await db.scalar(select(Tenant).where(Tenant.name == label))
    if tenant is None:
        tenant = Tenant(name=label, status="active")
        db.add(tenant)
        await db.flush()
    return tenant


async def _get_or_create_number(db, tenant: Tenant, *, label: str, e164: str,
                                browser_profile: str | None, daily_cap: int,
                                forza_attivo: bool) -> WaNumber:
    pseudo = hmac_phone(e164)
    numero = await db.scalar(select(WaNumber).where(WaNumber.phone_hmac == pseudo))
    if numero is None:
        numero = WaNumber(
            tenant_id=tenant.id, label=label, phone_hmac=pseudo,
            encrypted_phone=encrypt(e164),
            status=WaNumberStatus.active if forza_attivo else WaNumberStatus.pending_qr,
            browser_profile=browser_profile, daily_cap=daily_cap, warmup_day=1,
        )
        db.add(numero)
        await db.flush()
    return numero


async def _get_or_create_contact(db, tenant: Tenant, e164: str) -> WaContact:
    pseudo = hmac_phone(e164)
    contatto = await db.scalar(
        select(WaContact).where(WaContact.tenant_id == tenant.id,
                                WaContact.phone_hmac == pseudo))
    if contatto is None:
        contatto = WaContact(tenant_id=tenant.id, phone_hmac=pseudo,
                             encrypted_phone=encrypt(e164), display_name=None)
        db.add(contatto)
        await db.flush()
    return contatto


async def _get_or_create_campaign(db, tenant: Tenant, numero: WaNumber, *, name: str,
                                  tipo: str, template: str, avvia: bool) -> WaCampaign:
    camp = await db.scalar(
        select(WaCampaign).where(WaCampaign.tenant_id == tenant.id,
                                 WaCampaign.name == name))
    if camp is not None:
        return camp

    tipo_enum = WaCampaignType(tipo)
    # optout_enabled: True se marketing (contratto §2.1). wa_campaign_service
    # non esiste ancora in PR-0 (arriva col Task 6 di M2): regola inline qui,
    # stessa logica, un solo posto la scrive comunque una volta che esiste.
    optout = tipo_enum == WaCampaignType.marketing
    camp = WaCampaign(
        tenant_id=tenant.id, wa_number_id=numero.id, name=name,
        campaign_type=tipo_enum,
        status=WaCampaignStatus.running if avvia else WaCampaignStatus.draft,
        optout_enabled=optout,
        optout_cta=("Scrivi STOP per non ricevere piu' messaggi." if optout else None),
        started_at=datetime.utcnow() if avvia else None,
    )
    db.add(camp)
    await db.flush()
    db.add(WaSequenceStep(campaign_id=camp.id, step_index=0, template_a=template,
                          send_condition=WaSendCondition.always, wait_days=0))
    await db.flush()
    return camp


async def _get_or_create_campaign_contact(db, camp: WaCampaign, contatto: WaContact) -> None:
    esistente = await db.scalar(
        select(WaCampaignContact).where(WaCampaignContact.campaign_id == camp.id,
                                        WaCampaignContact.contact_id == contatto.id))
    if esistente is None:
        db.add(WaCampaignContact(
            campaign_id=camp.id, contact_id=contatto.id,
            status=WaContactStatus.queued, current_step=-1,
            next_action_at=datetime.utcnow(), failure_count=0,
        ))
        await db.flush()


async def main() -> None:
    args = _parse_args()
    _assert_db_di_test(settings.database_url, args.i_know_what_im_doing)
    _assert_profilo_non_poc1(args.browser_profile)

    if args.force_number_active:
        print("ATTENZIONE: --force-number-active mette il numero ad 'active' "
              "SENZA sessione QR. Non usarlo per una prova d'invio vera: "
              "l'apertura chat fallirebbe e sporcherebbe le misure.")

    if args.dry_run:
        print("[dry-run] nessuna scrittura. Riepilogo di cio' che verrebbe creato:")
        print(f"  tenant={args.tenant_label!r} numero={mask_phone(args.number_phone)} "
              f"contatti={len(args.contacts)} campagna={args.campaign_name!r} "
              f"tipo={args.campaign_type} start={args.start}")
        return

    async with AsyncSessionLocal() as db:
        tenant = await _get_or_create_tenant(db, args.tenant_label)
        numero = await _get_or_create_number(
            db, tenant, label=args.number_label, e164=args.number_phone,
            browser_profile=args.browser_profile, daily_cap=args.daily_cap,
            forza_attivo=args.force_number_active)
        contatti = [await _get_or_create_contact(db, tenant, e164) for e164 in args.contacts]
        campagna = await _get_or_create_campaign(
            db, tenant, numero, name=args.campaign_name, tipo=args.campaign_type,
            template=args.template, avvia=args.start)
        for contatto in contatti:
            await _get_or_create_campaign_contact(db, campagna, contatto)
        campagna.total_contacts = len(contatti)
        await db.commit()

        print("Seed completato:")
        print(f"  tenant_id={tenant.id}")
        print(f"  numero_id={numero.id} ({mask_phone(args.number_phone)}, "
              f"status={numero.status.value})")
        print(f"  campagna_id={campagna.id} (status={campagna.status.value})")
        print(f"  contatti={len(contatti)}")


if __name__ == "__main__":
    asyncio.run(main())
