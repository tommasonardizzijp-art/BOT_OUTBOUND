"""
Leads API — browse and export the GlobalContact database.

GlobalContact is the deduplicated cross-campaign lead registry.
Every user that was successfully contacted is recorded here.

Endpoints:
  GET  /leads         — paginated list with filters + inline insights
  GET  /leads/export  — CSV download (same filters, no pagination)
"""
import csv
import io
import json
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select, func, case, exists, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.global_contact import GlobalContact
from app.models.follower import Follower, FollowerStatus
from app.models.campaign import Campaign
from app.schemas.lead import LeadResponse, LeadListResponse, LeadInsights

router = APIRouter(prefix="/leads", tags=["leads"])


def _stats_subquery():
    """
    Aggregate follower stats per ig_user_id across all campaigns.
    Returns subquery with: follower_count, following_count, external_url,
    profile_pic_url, is_verified, has_replied, scrape_sources (comma-sep).
    """
    return (
        select(
            Follower.ig_user_id,
            func.max(Follower.follower_count).label("follower_count"),
            func.max(Follower.following_count).label("following_count"),
            func.max(Follower.external_url).label("external_url"),
            func.max(Follower.profile_pic_url).label("profile_pic_url"),
            func.max(case((Follower.is_verified == True, 1), else_=0)).label("is_verified"),
            func.max(case((Follower.status == FollowerStatus.replied, 1), else_=0)).label("has_replied"),
        )
        .group_by(Follower.ig_user_id)
        .subquery("fs")
    )


def _sources_subquery():
    """Comma-separated list of distinct target_usernames that scraped each ig_user_id."""
    distinct_sources = (
        select(
            Follower.ig_user_id.label("ig_user_id"),
            Campaign.target_username.label("target_username"),
        )
        .join(Campaign, Campaign.id == Follower.campaign_id)
        .distinct()
        .subquery("distinct_src")
    )
    return (
        select(
            distinct_sources.c.ig_user_id,
            func.aggregate_strings(distinct_sources.c.target_username, ",").label("scrape_sources"),
        )
        .group_by(distinct_sources.c.ig_user_id)
        .subquery("src")
    )


def _build_conditions(stats_sq, search, campaign_id, has_replied,
                      verified_only, min_followers, date_from, date_to,
                      campaign_ids=None, scraping_account_ids=None,
                      has_phone=False, has_email=False):
    """Build WHERE conditions referencing the given stats_sq instance.
    Each query (list, count, insights) must pass its OWN stats_sq — sharing
    a single subquery instance across queries breaks SQLAlchemy FROM resolution.
    """
    conditions = []
    if search:
        s = f'%{search}%'
        conditions.append(or_(
            GlobalContact.username.ilike(s),
            GlobalContact.full_name.ilike(s),
            GlobalContact.biography.ilike(s),
        ))
    if campaign_id:
        conditions.append(
            exists(
                select(1).where(
                    Follower.ig_user_id == GlobalContact.ig_user_id,
                    Follower.campaign_id == campaign_id,
                )
            )
        )
    if campaign_ids:
        conditions.append(
            exists(select(1).where(
                Follower.ig_user_id == GlobalContact.ig_user_id,
                Follower.campaign_id.in_(campaign_ids),
            ))
        )
    if scraping_account_ids:
        # scrape_sources is a JSON array of objects containing scraping_account_id.
        conditions.append(or_(*[
            GlobalContact.scrape_sources.like(f'%"{aid}"%') for aid in scraping_account_ids
        ]))
    if has_phone:
        conditions.append(GlobalContact.phone.isnot(None))
    if has_email:
        conditions.append(GlobalContact.email.isnot(None))
    if has_replied is True:
        conditions.append(stats_sq.c.has_replied == 1)
    elif has_replied is False:
        conditions.append(or_(stats_sq.c.has_replied == 0, stats_sq.c.has_replied.is_(None)))
    if verified_only:
        conditions.append(stats_sq.c.is_verified == 1)
    if min_followers is not None:
        conditions.append(stats_sq.c.follower_count >= min_followers)
    # Temporal filter targets the SCRAPE date (first_seen_at), falling back to
    # created_at for pre-014 rows. This keeps scraped-but-never-contacted leads
    # (last_contacted_at=NULL) in range — the point of "estrai contatti scrapati".
    scraped_at = func.coalesce(GlobalContact.first_seen_at, GlobalContact.created_at)
    if date_from:
        try:
            conditions.append(scraped_at >= datetime.fromisoformat(date_from))
        except ValueError:
            pass
    if date_to:
        try:
            dt_to = datetime.fromisoformat(date_to)
            # Bare date (midnight) → make the whole day inclusive.
            if (dt_to.hour, dt_to.minute, dt_to.second) == (0, 0, 0):
                conditions.append(scraped_at < dt_to + timedelta(days=1))
            else:
                conditions.append(scraped_at <= dt_to)
        except ValueError:
            pass
    return conditions


def _filter_args(search, campaign_id, has_replied, verified_only, min_followers, date_from, date_to,
                 campaign_ids=None, scraping_account_ids=None, has_phone=False, has_email=False):
    """Bundle filter kwargs for repeated _build_conditions calls."""
    return dict(
        search=search, campaign_id=campaign_id, has_replied=has_replied,
        verified_only=verified_only, min_followers=min_followers,
        date_from=date_from, date_to=date_to,
        campaign_ids=campaign_ids, scraping_account_ids=scraping_account_ids,
        has_phone=has_phone, has_email=has_email,
    )


def _row_to_lead(row) -> LeadResponse:
    gc = row[0]
    try:
        history = json.loads(gc.contact_history) if gc.contact_history else []
    except Exception:
        history = []
    sources_str = row.scrape_sources or ""
    scrape_sources = [s.strip() for s in sources_str.split(",") if s.strip()] if sources_str else []
    try:
        bio_links = json.loads(gc.bio_links) if gc.bio_links else []
    except Exception:
        bio_links = []
    try:
        scrape_src_json = json.loads(gc.scrape_sources) if gc.scrape_sources else []
        scraping_accounts = sorted({
            e.get("scraping_account_username") for e in scrape_src_json
            if e.get("scraping_account_username")
        })
    except Exception:
        scraping_accounts = []
    return LeadResponse(
        ig_user_id=gc.ig_user_id,
        username=gc.username,
        full_name=gc.full_name,
        biography=gc.biography,
        follower_count=row.follower_count,
        following_count=row.following_count,
        is_verified=bool(row.is_verified),
        external_url=gc.external_url or row.external_url,
        profile_pic_url=row.profile_pic_url,
        phone=gc.phone,
        email=gc.email,
        whatsapp=gc.whatsapp,
        bio_links=bio_links,
        scraping_accounts=scraping_accounts,
        contact_history=history,
        contacts_count=len(history),
        scrape_sources=scrape_sources,
        has_replied=bool(row.has_replied),
        first_seen_at=gc.first_seen_at,
        last_contacted_at=gc.last_contacted_at,
        created_at=gc.created_at,
    )


@router.get("", response_model=LeadListResponse)
async def list_leads(
    search: str | None = Query(default=None, description="Search username, name or bio"),
    campaign_id: str | None = Query(default=None, description="Filter by campaign"),
    has_replied: bool | None = Query(default=None, description="Filter by reply status"),
    verified_only: bool = Query(default=False, description="Only verified accounts"),
    min_followers: int | None = Query(default=None, ge=0, description="Minimum follower count"),
    date_from: str | None = Query(default=None, description="ISO date — last_contacted_at >="),
    date_to: str | None = Query(default=None, description="ISO date — last_contacted_at <="),
    campaign_ids: list[str] | None = Query(default=None, description="Filter by multiple campaign ids"),
    scraping_account_ids: list[str] | None = Query(default=None, description="Filter by scraping account ids"),
    has_phone: bool = Query(default=False, description="Only leads with a phone"),
    has_email: bool = Query(default=False, description="Only leads with an email"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    fargs = _filter_args(search, campaign_id, has_replied, verified_only,
                         min_followers, date_from, date_to,
                         campaign_ids=campaign_ids, scraping_account_ids=scraping_account_ids,
                         has_phone=has_phone, has_email=has_email)

    # List query: dedicated stats_sq + sources_sq
    stats_sq = _stats_subquery()
    sources_sq = _sources_subquery()

    base = (
        select(
            GlobalContact,
            stats_sq.c.follower_count,
            stats_sq.c.following_count,
            stats_sq.c.external_url,
            stats_sq.c.profile_pic_url,
            stats_sq.c.is_verified,
            stats_sq.c.has_replied,
            sources_sq.c.scrape_sources,
        )
        .outerjoin(stats_sq, stats_sq.c.ig_user_id == GlobalContact.ig_user_id)
        .outerjoin(sources_sq, sources_sq.c.ig_user_id == GlobalContact.ig_user_id)
    )

    list_conditions = _build_conditions(stats_sq, **fargs)
    if list_conditions:
        base = base.where(and_(*list_conditions))

    # Total count for pagination
    count_sq = base.subquery()
    total = await db.scalar(select(func.count()).select_from(count_sq)) or 0

    # Paginated results
    stmt = (
        base
        .order_by(GlobalContact.last_contacted_at.desc().nullslast())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = (await db.execute(stmt)).all()
    items = [_row_to_lead(row) for row in rows]

    # Insights query: BUILDS ITS OWN stats_sq + conditions referencing it.
    # Sharing stats_sq with the list query produces SQL referencing a subquery
    # not present in this query's FROM → 500 error on filter (bug 2026-05-07).
    ins_sq2 = _stats_subquery()
    ins_conditions = _build_conditions(ins_sq2, **fargs)
    filtered_ins_base = (
        select(
            func.count(GlobalContact.id).label("total"),
            func.sum(case((GlobalContact.last_contacted_at.isnot(None), 1), else_=0)).label("contacted"),
            func.sum(case((ins_sq2.c.has_replied == 1, 1), else_=0)).label("total_replied"),
        )
        .outerjoin(ins_sq2, ins_sq2.c.ig_user_id == GlobalContact.ig_user_id)
    )
    if ins_conditions:
        filtered_ins_base = filtered_ins_base.where(and_(*ins_conditions))
    ins = (await db.execute(filtered_ins_base)).one()

    # total = lead con info acquisite (righe GlobalContact = bio/contatti estratti)
    # contacted = quelli con DM realmente inviato (last_contacted_at valorizzato)
    info_leads = ins.total or 0
    contacted_leads = int(ins.contacted or 0)
    total_replied = int(ins.total_replied or 0)

    # Scraped leads: unique ig_user_ids in Follower table (filter by campaign if set)
    scraped_q = select(func.count(Follower.ig_user_id.distinct()))
    if campaign_id:
        scraped_q = scraped_q.where(Follower.campaign_id == campaign_id)
    if date_from:
        try:
            scraped_q = scraped_q.where(Follower.created_at >= datetime.fromisoformat(date_from))
        except ValueError:
            pass
    if date_to:
        try:
            dt_to = datetime.fromisoformat(date_to)
            if (dt_to.hour, dt_to.minute, dt_to.second) == (0, 0, 0):
                scraped_q = scraped_q.where(Follower.created_at < dt_to + timedelta(days=1))
            else:
                scraped_q = scraped_q.where(Follower.created_at <= dt_to)
        except ValueError:
            pass
    scraped_leads = await db.scalar(scraped_q) or 0

    insights = LeadInsights(
        scraped_leads=scraped_leads,
        total_leads=info_leads,
        contacted_leads=contacted_leads,
        total_replied=total_replied,
        reply_rate=round((total_replied / contacted_leads) * 100, 1) if contacted_leads > 0 else 0.0,
    )

    return LeadListResponse(items=items, total=total, page=page, page_size=page_size, insights=insights)


@router.get("/export")
async def export_leads_csv(
    search: str | None = Query(default=None),
    campaign_id: str | None = Query(default=None),
    has_replied: bool | None = Query(default=None),
    verified_only: bool = Query(default=False),
    min_followers: int | None = Query(default=None),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    campaign_ids: list[str] | None = Query(default=None, description="Filter by multiple campaign ids"),
    scraping_account_ids: list[str] | None = Query(default=None, description="Filter by scraping account ids"),
    has_phone: bool = Query(default=False, description="Only leads with a phone"),
    has_email: bool = Query(default=False, description="Only leads with an email"),
    db: AsyncSession = Depends(get_db),
):
    """Export leads as CSV with the same filters as the list endpoint."""
    fargs = _filter_args(search, campaign_id, has_replied, verified_only,
                         min_followers, date_from, date_to,
                         campaign_ids=campaign_ids, scraping_account_ids=scraping_account_ids,
                         has_phone=has_phone, has_email=has_email)

    stats_sq = _stats_subquery()
    sources_sq = _sources_subquery()

    base = (
        select(
            GlobalContact,
            stats_sq.c.follower_count,
            stats_sq.c.following_count,
            stats_sq.c.external_url,
            stats_sq.c.profile_pic_url,
            stats_sq.c.is_verified,
            stats_sq.c.has_replied,
            sources_sq.c.scrape_sources,
        )
        .outerjoin(stats_sq, stats_sq.c.ig_user_id == GlobalContact.ig_user_id)
        .outerjoin(sources_sq, sources_sq.c.ig_user_id == GlobalContact.ig_user_id)
    )

    conditions = _build_conditions(stats_sq, **fargs)
    if conditions:
        base = base.where(and_(*conditions))

    stmt = base.order_by(GlobalContact.last_contacted_at.desc().nullslast())
    rows = (await db.execute(stmt)).all()

    output = io.StringIO()
    # delimiter ';' = separatore nativo di Excel italiano; il modulo csv quota i
    # campi che lo contengono (es. bio con ';'), evitando split di colonna sbagliati.
    writer = csv.DictWriter(output, delimiter=";", fieldnames=[
        "ig_user_id", "username", "full_name", "biography",
        "follower_count", "following_count", "is_verified",
        "phone", "email", "whatsapp", "external_url", "bio_links",
        "scrape_sources", "scraping_accounts", "contacts_count",
        "has_replied", "first_seen_at", "last_contacted_at", "created_at",
    ])
    writer.writeheader()

    for row in rows:
        lead = _row_to_lead(row)
        writer.writerow({
            "ig_user_id": lead.ig_user_id,
            "username": lead.username or "",
            "full_name": lead.full_name or "",
            "biography": (lead.biography or "").replace("\n", " ").replace("\r", " ").replace("\t", " "),
            "follower_count": lead.follower_count or "",
            "following_count": lead.following_count or "",
            "is_verified": "yes" if lead.is_verified else "no",
            "phone": lead.phone or "",
            "email": lead.email or "",
            "whatsapp": lead.whatsapp or "",
            "external_url": lead.external_url or "",
            "bio_links": " | ".join(l.get("url", "") for l in lead.bio_links),
            "scrape_sources": ",".join(lead.scrape_sources),
            "scraping_accounts": ",".join(lead.scraping_accounts),
            "contacts_count": lead.contacts_count,
            "has_replied": "yes" if lead.has_replied else "no",
            "first_seen_at": lead.first_seen_at.isoformat() if lead.first_seen_at else "",
            "last_contacted_at": lead.last_contacted_at.isoformat() if lead.last_contacted_at else "",
            "created_at": lead.created_at.isoformat(),
        })

    output.seek(0)
    filename = f"leads_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
    # BOM utf-8-sig: senza, Excel legge il CSV come cp1252 e accenti/emoji
    # diventano mojibake. Il BOM forza Excel a interpretarlo come UTF-8.
    return StreamingResponse(
        iter([output.getvalue().encode("utf-8-sig")]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
