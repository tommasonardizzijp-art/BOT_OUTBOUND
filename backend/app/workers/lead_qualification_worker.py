from datetime import datetime

from loguru import logger
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.global_contact import GlobalContact
from app.models.lead_qualification import LeadQualificationRunStatus, LeadTargetProfile
from app.models.lead_qualification import LeadQualificationRun as LeadQualificationRunModel
from app.schemas.lead_qualification import LeadQualificationFilters
from app.services.lead_qualification import candidate_select_for_processing, classify_batch, run_profile_snapshot
from app.services.lead_qualification_rules import safe_json_loads
from app.services.notifier import send_telegram


BATCH_SIZE = 100


async def qualify_leads_task(ctx: dict, run_id: str) -> None:
    logger.info(f"[LeadQualification] Starting run {run_id}")
    async with AsyncSessionLocal() as db:
        run = await db.get(LeadQualificationRunModel, run_id)
        if not run:
            logger.warning(f"[LeadQualification] Run {run_id} not found")
            return
        profile = await db.get(LeadTargetProfile, run.target_profile_id)
        if not profile:
            run.status = LeadQualificationRunStatus.failed
            run.completed_at = datetime.utcnow()
            await db.commit()
            return
        profile_snapshot = run_profile_snapshot(run)

        filters = LeadQualificationFilters(**safe_json_loads(run.filters, {}))

        # Honour cancellation before we even start
        if run.status == LeadQualificationRunStatus.cancelled:
            logger.info(f"[LeadQualification] Run {run_id} was cancelled before starting")
            return

        run.status = LeadQualificationRunStatus.running
        run.started_at = datetime.utcnow()
        run.updated_at = datetime.utcnow()
        await db.commit()

        try:
            # Load only IDs first (UUIDs are lightweight; avoids loading all GlobalContact in RAM)
            contact_ids = list((await db.execute(
                candidate_select_for_processing(
                    filters,
                    target_profile_id=run.target_profile_id,
                    rules_hash=run.rules_hash,
                    run_id=run.id,
                    ids_only=True,
                )
            )).scalars().all())

            total_to_process = len(contact_ids)
            processed_total = 0

            for start in range(0, total_to_process, BATCH_SIZE):
                # Check for cancellation at each batch boundary
                current_status = await db.scalar(
                    select(LeadQualificationRunModel.status).where(LeadQualificationRunModel.id == run.id)
                )
                if current_status == LeadQualificationRunStatus.cancelled:
                    logger.info(f"[LeadQualification] Run {run_id} cancelled mid-run after {processed_total} contacts")
                    return

                batch_ids = contact_ids[start : start + BATCH_SIZE]
                contacts = (await db.execute(
                    select(GlobalContact).where(GlobalContact.id.in_(batch_ids))
                )).scalars().all()

                results, counts = await classify_batch(profile=profile_snapshot, run=run, contacts=list(contacts))
                for result in results:
                    db.add(result)

                run.processed_count += counts["processed"]
                run.matched_count += counts["match"]
                run.no_match_count += counts["no_match"]
                run.ambiguous_count += counts["ambiguous"]
                run.ai_reviewed_count += counts["ai_reviewed"]
                run.error_count += counts["errors"]
                run.updated_at = datetime.utcnow()
                processed_total += counts["processed"]
                await db.commit()
                logger.info(
                    f"[LeadQualification] run={run.id} processed={processed_total}/{total_to_process} "
                    f"match={run.matched_count} no_match={run.no_match_count} ambiguous={run.ambiguous_count}"
                )

            run.status = LeadQualificationRunStatus.completed
            run.completed_at = datetime.utcnow()
            run.updated_at = datetime.utcnow()
            await db.commit()
            await _notify_completed(profile_snapshot, run)
            logger.info(f"[LeadQualification] Completed run {run_id}")
        except Exception as exc:
            logger.exception(f"[LeadQualification] Run {run_id} failed: {exc}")
            await db.rollback()
            run = await db.get(LeadQualificationRunModel, run_id)
            if run and run.status not in (LeadQualificationRunStatus.cancelled,):
                run.status = LeadQualificationRunStatus.failed
                run.completed_at = datetime.utcnow()
                run.updated_at = datetime.utcnow()
                await db.commit()
            await send_telegram(
                f"*Qualifica lead fallita*\nRun: `{run_id}`\nErrore: `{str(exc)[:300]}`",
                level="error",
            )
            raise


async def _notify_completed(profile: LeadTargetProfile, run: LeadQualificationRunModel) -> None:
    await send_telegram(
        "\n".join([
            "*Qualifica lead completata*",
            f"Target: *{profile.name}*",
            f"Processati: `{run.processed_count}`",
            f"Match: `{run.matched_count}`",
            f"No match: `{run.no_match_count}`",
            f"Ambigui: `{run.ambiguous_count}`",
            f"AI reviewed: `{run.ai_reviewed_count}`",
            f"Errori: `{run.error_count}`",
        ]),
        level="info",
    )
