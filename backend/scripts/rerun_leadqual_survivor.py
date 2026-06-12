"""Re-run lead qualification for the 'Shop survivor' campaign with the fixed
calibration. Updates the target profile thresholds, creates a fresh run and
processes ALL candidate leads through the real deterministic+AI pipeline.

Usage:
  python -m scripts.rerun_leadqual_survivor --dry   # scoring only, no AI calls
  python -m scripts.rerun_leadqual_survivor         # full run (real AI)
"""
import asyncio
import json
import sys
from collections import Counter
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.campaign import Campaign
from app.models.global_contact import GlobalContact
from app.models.lead_qualification import (
    LeadQualificationRun,
    LeadQualificationRunStatus,
    LeadTargetProfile,
)
from app.schemas.lead_qualification import LeadQualificationFilters
from app.services.lead_qualification import (
    candidate_select_for_processing,
    classify_batch,
    run_profile_snapshot,
    score_lead,
)
from app.services.lead_qualification_rules import safe_json_loads, safe_json_dumps, rules_hash as compute_rules_hash

# Nuovo design recall-first: 1 keyword specifica = match diretto, niente negativi.
PASS_TH, REJECT_TH, AI_MIN, AI_MAX = 10, 0, 1, 9
BATCH_SIZE = 100

# MATCH DIRETTO (positive_terms): keyword di nicchia specifiche + indicatori retail
# che l'utente considera ottimi (negozio/shop/store/brand/style). Una basta.
POSITIVE_TERMS = [
    "abbigliamento", "moda", "fashion", "clothing", "clothes", "apparel",
    "boutique", "vestiti", "abiti", "capispalla", "cappotti", "giacche",
    "felpe", "magliette", "maglie", "maglieria", "jeans", "intimo", "lingerie",
    "calzature", "scarpe", "pelletteria", "borse", "premaman",
    "sartoria", "sarta", "sarto", "sartoriale", "stilista", "modista",
    "couture", "atelier", "streetwear", "sportswear", "vintage", "outlet",
    "showroom", "grossista", "wholesale", "rivenditore", "retail", "e-commerce",
    # indicatori retail "ottimi" (scelta utente)
    "negozio", "negozi", "shop", "store", "brand", "style", "bottega",
]

# FASCIA AI (positive_concepts): generiche ma possibili. Da sole NON fanno match,
# vanno all'AI per conferma. "designer" c'entra poco -> qui, non a match.
POSITIVE_CONCEPTS = [
    "uomo", "donna", "designer", "kids", "bimbi", "bimbo", "bambini", "baby",
    "accessori", "total look", "collezione",
]

# STRONG (match netto): frasi inequivocabili di nicchia.
STRONG_TERMS = [
    "abbigliamento donna", "abbigliamento uomo", "abbigliamento bimbi",
    "fashion brand", "clothing store", "boutique fashion", "moda italiana",
    "stilista di moda", "sarto professionista", "negozio di abbigliamento",
]


async def main(dry: bool):
    async with AsyncSessionLocal() as db:
        camp = (await db.execute(
            select(Campaign).where(Campaign.name.ilike("%survivor%"))
        )).scalars().first()
        print(f"Campaign: {camp.name} ({camp.id})")

        # Find the profile used by the latest survivor run.
        runs = (await db.execute(
            select(LeadQualificationRun).order_by(LeadQualificationRun.created_at.desc())
        )).scalars().all()
        src_run = next(
            (r for r in runs if camp.id in (safe_json_loads(r.filters, {}).get("campaign_ids") or [])),
            None,
        )
        profile = await db.get(LeadTargetProfile, src_run.target_profile_id)
        print(f"Target profile: {profile.name} ({profile.id})")
        print(f"  old thresholds: pass={profile.pass_threshold} reject={profile.reject_threshold} "
              f"ai=[{profile.ai_review_min_score},{profile.ai_review_max_score}]")

        # 0. Rebuild the rules with the new recall-first tiering + recompute hash.
        rules_obj = safe_json_loads(profile.compiled_rules, {})
        rules_obj["positive_terms"] = sorted({t.lower() for t in POSITIVE_TERMS})
        rules_obj["strong_terms"] = sorted({t.lower() for t in STRONG_TERMS})
        rules_obj["positive_concepts"] = sorted({t.lower() for t in POSITIVE_CONCEPTS})
        rules_obj["negative_terms"] = []          # niente negativi: zero falsi negativi
        rules_obj["negative_concepts"] = []
        rules_obj.setdefault("score_rules", {})["positive_term_bonus"] = 10
        profile.compiled_rules = safe_json_dumps(rules_obj)
        profile.rules_hash = compute_rules_hash(rules_obj)
        print(f"  positive_terms={len(rules_obj['positive_terms'])} "
              f"strong={len(rules_obj['strong_terms'])} concepts(AI)={len(rules_obj['positive_concepts'])} "
              f"negatives=0")

        # 1. Update profile thresholds (fixes UI + future runs too).
        profile.pass_threshold = PASS_TH
        profile.reject_threshold = REJECT_TH
        profile.ai_review_min_score = AI_MIN
        profile.ai_review_max_score = AI_MAX
        await db.commit()
        print(f"  new thresholds: pass={PASS_TH} reject={REJECT_TH} ai=[{AI_MIN},{AI_MAX}]")

        # Clean up any stale running/queued runs for this profile (e.g. a
        # previous killed run) so the UI doesn't show a stuck spinner.
        stale = (await db.execute(
            select(LeadQualificationRun).where(
                LeadQualificationRun.target_profile_id == profile.id,
                LeadQualificationRun.status.in_([
                    LeadQualificationRunStatus.running,
                    LeadQualificationRunStatus.queued,
                ]),
            )
        )).scalars().all()
        for s in stale:
            s.status = LeadQualificationRunStatus.failed
            s.completed_at = datetime.utcnow()
        if stale:
            await db.commit()
            print(f"  cleaned {len(stale)} stale run(s)")

        filters = LeadQualificationFilters(
            campaign_ids=[camp.id],
            date_to=safe_json_loads(src_run.filters, {}).get("date_to"),
            skip_existing_same_rules=False,
            max_leads=5000,
            match_on_contact=MATCH_ON_CONTACT,
        )
        print(f"  match_on_contact={MATCH_ON_CONTACT}")

        # Dry run: just show how many now reach the AI window.
        rules = safe_json_loads(profile.compiled_rules, {})
        all_ids = list((await db.execute(
            candidate_select_for_processing(
                filters, target_profile_id=profile.id, rules_hash=profile.rules_hash, ids_only=True,
            )
        )).scalars().all())
        print(f"\nCandidates: {len(all_ids)}")

        if dry:
            contacts = (await db.execute(
                select(GlobalContact).where(GlobalContact.id.in_(all_ids))
            )).scalars().all()
            st = Counter()
            eligible = 0
            for c in contacts:
                det = score_lead(c, rules, pass_threshold=PASS_TH, reject_threshold=REJECT_TH,
                                 match_on_contact=MATCH_ON_CONTACT)
                st[det.status] += 1
                if det.status == "ambiguous" and AI_MIN <= det.score <= AI_MAX:
                    eligible += 1
            print("Deterministic status:", dict(st))
            print(f"AI-eligible (would call AI): {eligible}")
            return

        # 2. Create a fresh run with the updated snapshot.
        snap = run_profile_snapshot  # noqa
        run = LeadQualificationRun(
            target_profile_id=profile.id,
            target_name=profile.name,
            target_description=profile.description,
            compiled_rules=profile.compiled_rules,
            filters=json.dumps(filters.model_dump()),
            rules_hash=profile.rules_hash,
            pass_threshold=profile.pass_threshold,
            reject_threshold=profile.reject_threshold,
            ai_review_min_score=profile.ai_review_min_score,
            ai_review_max_score=profile.ai_review_max_score,
            status=LeadQualificationRunStatus.running,
            total_candidates=len(all_ids),
            started_at=datetime.utcnow(),
        )
        db.add(run)
        await db.commit()
        profile_snapshot = run_profile_snapshot(run)
        print(f"Run created: {run.id}\n")

        processed = 0
        for start in range(0, len(all_ids), BATCH_SIZE):
            batch_ids = all_ids[start : start + BATCH_SIZE]
            contacts = (await db.execute(
                select(GlobalContact).where(GlobalContact.id.in_(batch_ids))
            )).scalars().all()
            results, counts = await classify_batch(
                profile=profile_snapshot, run=run, contacts=list(contacts)
            )
            for r in results:
                db.add(r)
            run.processed_count += counts["processed"]
            run.matched_count += counts["match"]
            run.no_match_count += counts["no_match"]
            run.ambiguous_count += counts["ambiguous"]
            run.ai_reviewed_count += counts["ai_reviewed"]
            run.error_count += counts["errors"]
            run.updated_at = datetime.utcnow()
            processed += counts["processed"]
            await db.commit()
            print(f"  {processed}/{len(all_ids)}  match={run.matched_count} "
                  f"no_match={run.no_match_count} ambiguous={run.ambiguous_count} "
                  f"ai_reviewed={run.ai_reviewed_count} errors={run.error_count}")

        run.status = LeadQualificationRunStatus.completed
        run.completed_at = datetime.utcnow()
        await db.commit()

        total = run.processed_count or 1
        pct = 100 * run.matched_count / total
        print("\n=== DONE ===")
        print(f"Processed: {run.processed_count}")
        print(f"Match: {run.matched_count} ({pct:.1f}%)")
        print(f"No match: {run.no_match_count}")
        print(f"Ambiguous (unresolved): {run.ambiguous_count}")
        print(f"AI reviewed: {run.ai_reviewed_count}  errors: {run.error_count}")


MATCH_ON_CONTACT = "--contact" in sys.argv

if __name__ == "__main__":
    asyncio.run(main("--dry" in sys.argv))
