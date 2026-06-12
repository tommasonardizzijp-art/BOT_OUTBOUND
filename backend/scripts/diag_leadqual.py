"""Diagnostic: why did 'shop survivor' qualification match so few leads?

Read-only. Finds the campaign, its latest qualification run, replays the
deterministic scorer over the real candidate leads, and prints the score
distribution + sample bios so we can see WHERE leads are being dropped.
"""
import asyncio
import json
import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import select, func

from app.database import AsyncSessionLocal
from app.models.campaign import Campaign
from app.models.global_contact import GlobalContact
from app.models.lead_qualification import (
    LeadQualificationRun,
    LeadTargetProfile,
)
from app.schemas.lead_qualification import LeadQualificationFilters
from app.services.lead_qualification import (
    candidate_select,
    score_lead,
    _lead_fields,
)
from app.services.lead_qualification_rules import safe_json_loads


async def main():
    async with AsyncSessionLocal() as db:
        # 1. campaign
        camp = (await db.execute(
            select(Campaign).where(Campaign.name.ilike("%survivor%"))
        )).scalars().first()
        if not camp:
            print("NO campaign matching 'survivor'")
            camps = (await db.execute(select(Campaign.id, Campaign.name))).all()
            for c in camps:
                print("  campaign:", c.name)
            return
        print(f"CAMPAIGN: {camp.name} id={camp.id}")

        # 2. latest run touching this campaign (match by filters containing campaign id)
        runs = (await db.execute(
            select(LeadQualificationRun).order_by(LeadQualificationRun.created_at.desc())
        )).scalars().all()
        run = None
        for r in runs:
            f = safe_json_loads(r.filters, {})
            if camp.id in (f.get("campaign_ids") or []):
                run = r
                break
        if run is None:
            print("No run found whose filters include this campaign. Latest runs:")
            for r in runs[:10]:
                print(f"  run={r.id} target={r.target_name} filters={r.filters[:120]}")
            # fall back to most recent run
            run = runs[0] if runs else None
        if run is None:
            print("NO runs at all.")
            return

        print(f"\nRUN: {run.id} target={run.target_name}")
        print(f"  status={run.status} processed={run.processed_count} "
              f"match={run.matched_count} no_match={run.no_match_count} "
              f"ambiguous={run.ambiguous_count} ai_reviewed={run.ai_reviewed_count} "
              f"errors={run.error_count}")
        print(f"  thresholds: pass={run.pass_threshold} reject={run.reject_threshold} "
              f"ai_review=[{run.ai_review_min_score},{run.ai_review_max_score}]")
        print(f"  filters={run.filters}")
        rules = safe_json_loads(run.compiled_rules, {})
        print("\nCOMPILED RULES:")
        print(json.dumps(rules, ensure_ascii=False, indent=2))

        # 3. candidate leads (use run filters)
        filters = LeadQualificationFilters(**safe_json_loads(run.filters, {}))
        contacts = (await db.execute(candidate_select(filters))).scalars().all()
        print(f"\nCANDIDATE LEADS: {len(contacts)}")

        # 4. replay scorer
        dist = Counter()
        status_count = Counter()
        buckets = Counter()
        ai_eligible = 0
        samples = []
        for c in contacts:
            det = score_lead(
                c, rules,
                pass_threshold=run.pass_threshold,
                reject_threshold=run.reject_threshold,
            )
            status_count[det.status] += 1
            dist[det.score] += 1
            b = (det.score // 10) * 10
            buckets[b] += 1
            if run.ai_review_min_score <= det.score <= run.ai_review_max_score:
                ai_eligible += 1
            if len(samples) < 25:
                samples.append((det.score, det.status, c.username, c.biography, det.matched_signals))

        print("\nSTATUS (deterministic only):")
        for s, n in status_count.most_common():
            print(f"  {s}: {n}")
        print(f"\nAI-eligible (score in [{run.ai_review_min_score},{run.ai_review_max_score}]): {ai_eligible}")
        print("\nSCORE BUCKETS:")
        for b in sorted(buckets):
            print(f"  {b:3d}-{b+9:3d}: {buckets[b]}")

        # how many leads scored 0 (no signal at all)
        print(f"\nLeads with score 0 (zero signals matched): {dist[0]}")

        print("\nSAMPLE LEADS (score, status, username, bio[:80], matched):")
        for sc, st, un, bio, ms in samples:
            terms = [m['term'] for m in ms]
            print(f"  [{sc:3d}] {st:9s} @{un}: {repr((bio or '')[:80])} matched={terms}")


if __name__ == "__main__":
    asyncio.run(main())
