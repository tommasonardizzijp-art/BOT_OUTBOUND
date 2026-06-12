import asyncio, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models.lead_qualification import LeadQualificationRun

async def main():
    async with AsyncSessionLocal() as db:
        r = (await db.execute(select(LeadQualificationRun).order_by(LeadQualificationRun.created_at.desc()))).scalars().first()
        tot = r.total_candidates or 0
        pct = (100*r.matched_count/r.processed_count) if r.processed_count else 0
        print(f"run={r.id} status={r.status.value}")
        print(f"processed={r.processed_count}/{tot} match={r.matched_count} ({pct:.1f}%) "
              f"no_match={r.no_match_count} ambiguous={r.ambiguous_count} "
              f"ai_reviewed={r.ai_reviewed_count} errors={r.error_count}")
asyncio.run(main())
