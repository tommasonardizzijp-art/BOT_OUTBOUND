"""Re-process the LeadQualification rows stuck in status=error for a run.

These are weak-signal leads that went to AI review and failed (Groq free-tier
rate-limit exhaustion). We re-score them and re-run the AI classification,
updating the existing rows in place. Paced + backoff to survive the free-tier.
"""
import asyncio
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.global_contact import GlobalContact
from app.models.lead_qualification import (
    LeadQualification,
    LeadQualificationRun,
    LeadQualificationStatus,
)
from app.services.lead_qualification import (
    classify_ambiguous_lead,
    run_profile_snapshot,
    score_lead,
)
from app.services.lead_qualification_rules import safe_json_dumps, safe_json_loads

RUN_ID = "2a37c3d3-e328-4f08-a9b5-01d4520bfdfb"
PACE = 8.0


async def _ai_with_retry(profile, contact, det, max_attempts=6):
    for attempt in range(max_attempts):
        try:
            return await classify_ambiguous_lead(profile, contact, det)
        except Exception as exc:
            msg = str(exc).lower()
            rate = "429" in msg or "rate limit" in msg or "too many" in msg
            if not rate or attempt == max_attempts - 1:
                raise
            delay = min(60, 5 * (2 ** attempt))
            print(f"  rate-limit {attempt+1}/{max_attempts}, wait {delay}s")
            await asyncio.sleep(delay)


async def main():
    async with AsyncSessionLocal() as db:
        run = await db.get(LeadQualificationRun, RUN_ID)
        profile = run_profile_snapshot(run)
        rules = safe_json_loads(profile.compiled_rules, {})
        match_on_contact = bool(safe_json_loads(run.filters, {}).get("match_on_contact"))

        rows = (await db.execute(
            select(LeadQualification).where(
                LeadQualification.run_id == RUN_ID,
                LeadQualification.status == LeadQualificationStatus.error,
            )
        )).scalars().all()
        print(f"errored rows: {len(rows)}")

        fixed = match = no_match = ambiguous = still_err = 0
        for q in rows:
            contact = await db.get(GlobalContact, q.global_contact_id)
            det = score_lead(contact, rules, pass_threshold=profile.pass_threshold,
                             reject_threshold=profile.reject_threshold, match_on_contact=match_on_contact)
            if det.status != LeadQualificationStatus.ambiguous.value:
                # deterministic now decides it (e.g. match_on_contact) -> no AI needed
                q.status = LeadQualificationStatus(det.status)
                q.final_score = det.score
                q.deterministic_score = det.score
                q.matched_signals = safe_json_dumps(det.matched_signals)
                q.reason = None
                fixed += 1
                print(f"  @{contact.username}: deterministic -> {det.status} ({det.score})")
            else:
                await asyncio.sleep(PACE)
                ai = await _ai_with_retry(profile, contact, det)
                q.ai_used = True
                q.ai_score = ai["ai_score"]
                q.final_score = ai["ai_score"]
                q.status = LeadQualificationStatus(ai["status"])
                q.ai_label = ai["label"]
                q.reason = ai["reason"]
                q.model_used = ai["model_used"]
                fixed += 1
                print(f"  @{contact.username}: AI -> {ai['status']} ({ai['ai_score']}) {ai.get('reason') or ''}")
            await db.commit()

        # refresh run counters from the actual rows
        all_rows = (await db.execute(select(LeadQualification.status).where(LeadQualification.run_id == RUN_ID))).scalars().all()
        from collections import Counter
        c = Counter(s.value for s in all_rows)
        run.matched_count = c.get("match", 0)
        run.no_match_count = c.get("no_match", 0)
        run.ambiguous_count = c.get("ambiguous", 0)
        run.error_count = c.get("error", 0)
        await db.commit()
        print(f"\nfixed {fixed} rows. run now: match={run.matched_count} "
              f"no_match={run.no_match_count} ambiguous={run.ambiguous_count} errors={run.error_count}")


if __name__ == "__main__":
    asyncio.run(main())
