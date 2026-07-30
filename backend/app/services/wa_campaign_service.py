"""Regole di campagna del canale WhatsApp.

Sta in un servizio e non nell'endpoint perche' queste regole valgono anche
per lo script di seed e per i test: una regola che vive dentro un handler
HTTP e' una regola che il resto del sistema puo' aggirare.
"""
from datetime import datetime

from sqlalchemy import select

from app.models.wa import (WaCampaign, WaCampaignStatus, WaCampaignType, WaNumber,
                           WaSendCondition, WaSequenceStep)
from app.services.wa_template import validate_wa_template


def calcola_optout_enabled(tipo: WaCampaignType) -> bool:
    """V10: marketing -> CTA "scrivi STOP" obbligatoria; follow-up -> no.
    Il server_default=true della migrazione 025 e' la rete di sicurezza; la
    regola vera e' condizionale, e quindi va scritta qui (contratto §2.1)."""
    return tipo == WaCampaignType.marketing


def valida_step(template: str, *, colonne_note: set[str]) -> None:
    """Un template con placeholder che il CSV non copre non si salva: se
    passasse, fallirebbe a tempo di invio, un contatto alla volta, in una
    campagna gia' partita."""
    if not (template or "").strip():
        raise ValueError("Il testo del messaggio non puo' essere vuoto.")
    ignoti = validate_wa_template(template, known_attributes=colonne_note)
    if ignoti:
        raise ValueError(
            "Placeholder non disponibili nella lista contatti: "
            + ", ".join(f"{{{x}}}" for x in ignoti)
            + ". Aggiungi la colonna al CSV oppure togli il segnaposto."
        )


async def crea_campagna(db, dati: dict) -> WaCampaign:
    """Crea la campagna in draft + il suo step 0.

    optout_enabled e' assegnato ESPLICITAMENTE (mai lasciato al
    server_default=true della migrazione 025, contratto §2.1): True se e
    solo se il chiamante non forza un valore diverso e il tipo e' marketing.
    """
    tipo = dati["campaign_type"]
    numero = await db.scalar(select(WaNumber).where(WaNumber.id == dati["wa_number_id"]))
    if numero is None:
        raise ValueError("Numero inesistente.")
    if numero.tenant_id != dati["tenant_id"]:
        raise ValueError("Il numero appartiene a un altro tenant.")

    optout = dati.get("optout_enabled")
    if optout is None:
        optout = calcola_optout_enabled(tipo)      # esplicito, mai il default a DB
    cta = (dati.get("optout_cta") or "").strip() or None
    if optout and not cta:
        raise ValueError(
            "Una campagna con opt-out attivo deve avere una CTA: non si manda "
            "marketing senza via d'uscita."
        )

    valida_step(dati["template_a"], colonne_note=set(dati.get("colonne_note") or []))

    campagna = WaCampaign(
        tenant_id=dati["tenant_id"], wa_number_id=numero.id, name=dati["name"],
        campaign_type=tipo, status=WaCampaignStatus.draft,
        optout_enabled=bool(optout), optout_cta=cta,
        daily_limit=dati.get("daily_limit"),
        active_hours_start=dati.get("active_hours_start"),
        active_hours_end=dati.get("active_hours_end"),
        created_at=datetime.utcnow(),
    )
    db.add(campagna)
    await db.flush()
    db.add(WaSequenceStep(
        campaign_id=campagna.id, step_index=0, template_a=dati["template_a"],
        template_b=dati.get("template_b"), template_c=dati.get("template_c"),
        template_d=dati.get("template_d"),
        # MVP: un solo step, condizione fissa. Lo SCHEMA e' completo, il
        # motore multi-step si accende post-MVP senza migrazione (SDD Q29).
        send_condition=WaSendCondition.always, wait_days=0,
    ))
    await db.commit()
    await db.refresh(campagna)
    return campagna
