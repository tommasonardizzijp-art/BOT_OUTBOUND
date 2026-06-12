import csv
import io
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.global_contact import GlobalContact
from app.models.lead_qualification import (
    LeadQualification,
    LeadQualificationRun,
    LeadQualificationRunStatus,
    LeadQualificationStatus,
    LeadTargetProfile,
)
from app.schemas.lead_qualification import (
    CompileProfileRequest,
    CompileProfileResponse,
    LeadQualificationEstimateRequest,
    LeadQualificationEstimateResponse,
    LeadQualificationResultListResponse,
    LeadQualificationResultResponse,
    LeadQualificationRunCreate,
    LeadQualificationRunResponse,
    LeadTargetProfileCreate,
    LeadTargetProfileResponse,
    LeadTargetProfileUpdate,
)
from app.services.lead_qualification import compile_target_description, estimate_run
from app.services.lead_qualification_rules import (
    rules_hash,
    safe_json_dumps,
    safe_json_loads,
    validate_compiled_rules,
)
from app.services.work_enqueue import enqueue_lead_qualification

router = APIRouter(prefix="/lead-qualification", tags=["lead-qualification"])


def _profile_to_response(profile: LeadTargetProfile) -> LeadTargetProfileResponse:
    return LeadTargetProfileResponse(
        id=profile.id,
        name=profile.name,
        description=profile.description,
        compiled_rules=safe_json_loads(profile.compiled_rules, {}),
        rules_hash=profile.rules_hash,
        pass_threshold=profile.pass_threshold,
        reject_threshold=profile.reject_threshold,
        ai_review_min_score=profile.ai_review_min_score,
        ai_review_max_score=profile.ai_review_max_score,
        max_run_size=profile.max_run_size,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )


def _run_to_response(run: LeadQualificationRun, profile_name: str | None = None) -> LeadQualificationRunResponse:
    return LeadQualificationRunResponse(
        id=run.id,
        target_profile_id=run.target_profile_id,
        target_profile_name=profile_name or getattr(run, "target_name", None),
        target_description=getattr(run, "target_description", None),
        filters=safe_json_loads(run.filters, {}),
        rules_hash=run.rules_hash,
        status=run.status.value if hasattr(run.status, "value") else str(run.status),
        total_candidates=run.total_candidates,
        skipped_existing=run.skipped_existing,
        processed_count=run.processed_count,
        matched_count=run.matched_count,
        no_match_count=run.no_match_count,
        ambiguous_count=run.ambiguous_count,
        ai_reviewed_count=run.ai_reviewed_count,
        error_count=run.error_count,
        started_at=run.started_at,
        completed_at=run.completed_at,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


async def _get_profile(db: AsyncSession, profile_id: str) -> LeadTargetProfile:
    profile = await db.get(LeadTargetProfile, profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Target profile non trovato")
    return profile


@router.post("/profiles/compile", response_model=CompileProfileResponse)
async def compile_profile(data: CompileProfileRequest):
    try:
        compiled = await compile_target_description(data.description)
        return CompileProfileResponse(**compiled)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Generazione criteri AI fallita: {str(exc)[:240]}")


@router.get("/profiles", response_model=list[LeadTargetProfileResponse])
async def list_profiles(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(LeadTargetProfile).order_by(LeadTargetProfile.created_at.desc()))).scalars().all()
    return [_profile_to_response(row) for row in rows]


@router.post("/profiles", response_model=LeadTargetProfileResponse, status_code=status.HTTP_201_CREATED)
async def create_profile(data: LeadTargetProfileCreate, db: AsyncSession = Depends(get_db)):
    compiled_rules = validate_compiled_rules(data.compiled_rules)
    profile = LeadTargetProfile(
        name=data.name,
        description=data.description,
        compiled_rules=safe_json_dumps(compiled_rules),
        rules_hash=rules_hash(compiled_rules),
        pass_threshold=data.pass_threshold,
        reject_threshold=data.reject_threshold,
        ai_review_min_score=data.ai_review_min_score,
        ai_review_max_score=data.ai_review_max_score,
        max_run_size=data.max_run_size,
    )
    db.add(profile)
    await db.commit()
    await db.refresh(profile)
    return _profile_to_response(profile)


@router.get("/profiles/{profile_id}", response_model=LeadTargetProfileResponse)
async def get_profile(profile_id: str, db: AsyncSession = Depends(get_db)):
    return _profile_to_response(await _get_profile(db, profile_id))


@router.put("/profiles/{profile_id}", response_model=LeadTargetProfileResponse)
async def update_profile(profile_id: str, data: LeadTargetProfileUpdate, db: AsyncSession = Depends(get_db)):
    profile = await _get_profile(db, profile_id)
    if data.name is not None:
        profile.name = data.name
    if data.description is not None:
        profile.description = data.description
    if data.compiled_rules is not None:
        compiled_rules = validate_compiled_rules(data.compiled_rules)
        profile.compiled_rules = safe_json_dumps(compiled_rules)
        profile.rules_hash = rules_hash(compiled_rules)
    for field in ("pass_threshold", "reject_threshold", "ai_review_min_score", "ai_review_max_score", "max_run_size"):
        value = getattr(data, field)
        if value is not None:
            setattr(profile, field, value)
    if profile.reject_threshold >= profile.pass_threshold:
        raise HTTPException(status_code=422, detail="reject_threshold deve essere minore di pass_threshold")
    if profile.ai_review_min_score > profile.ai_review_max_score:
        raise HTTPException(status_code=422, detail="ai_review_min_score deve essere <= ai_review_max_score")
    profile.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(profile)
    return _profile_to_response(profile)


@router.delete("/profiles/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_profile(profile_id: str, db: AsyncSession = Depends(get_db)):
    profile = await _get_profile(db, profile_id)
    # Block only on runs with meaningful results; failed/cancelled runs are safe to discard
    blocking_runs = await db.scalar(
        select(func.count(LeadQualificationRun.id)).where(
            LeadQualificationRun.target_profile_id == profile.id,
            LeadQualificationRun.status.in_([
                LeadQualificationRunStatus.queued,
                LeadQualificationRunStatus.running,
                LeadQualificationRunStatus.completed,
            ])
        )
    )
    if blocking_runs:
        raise HTTPException(
            status_code=409,
            detail=f"Target profile con {blocking_runs} run completate o attive: non eliminabile nel MVP",
        )
    await db.delete(profile)
    await db.commit()
    return None


@router.post("/runs/estimate", response_model=LeadQualificationEstimateResponse)
async def estimate_qualification_run(data: LeadQualificationEstimateRequest, db: AsyncSession = Depends(get_db)):
    profile = await _get_profile(db, data.target_profile_id)
    estimate = await estimate_run(db, profile, data.filters)
    return LeadQualificationEstimateResponse(**estimate)


@router.post("/runs", response_model=LeadQualificationRunResponse, status_code=status.HTTP_201_CREATED)
async def create_run(data: LeadQualificationRunCreate, db: AsyncSession = Depends(get_db)):
    profile = await _get_profile(db, data.target_profile_id)
    estimate = await estimate_run(db, profile, data.filters)
    if estimate["will_process"] <= 0:
        raise HTTPException(status_code=400, detail="Nessun lead da qualificare con questi filtri")
    if estimate["over_limit"]:
        raise HTTPException(
            status_code=400,
            detail=f"Run troppo grande ({estimate['will_process']} lead). Riduci i filtri o max_leads.",
        )

    run = LeadQualificationRun(
        target_profile_id=profile.id,
        target_name=profile.name,
        target_description=profile.description,
        compiled_rules=profile.compiled_rules,
        filters=safe_json_dumps(data.filters.model_dump()),
        rules_hash=profile.rules_hash,
        pass_threshold=profile.pass_threshold,
        reject_threshold=profile.reject_threshold,
        ai_review_min_score=profile.ai_review_min_score,
        ai_review_max_score=profile.ai_review_max_score,
        status=LeadQualificationRunStatus.queued,
        total_candidates=estimate["candidate_count"],
        skipped_existing=estimate["already_qualified_same_rules"],
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    try:
        await enqueue_lead_qualification(run.id)
    except Exception as exc:
        run.status = LeadQualificationRunStatus.failed
        run.completed_at = datetime.utcnow()
        await db.commit()
        raise HTTPException(status_code=503, detail=f"Impossibile accodare la run: {str(exc)[:240]}")
    return _run_to_response(run, profile.name)


@router.get("/runs", response_model=list[LeadQualificationRunResponse])
async def list_runs(
    target_profile_id: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(LeadQualificationRun, LeadTargetProfile.name)
        .join(LeadTargetProfile, LeadTargetProfile.id == LeadQualificationRun.target_profile_id)
        .order_by(LeadQualificationRun.created_at.desc())
    )
    if target_profile_id:
        stmt = stmt.where(LeadQualificationRun.target_profile_id == target_profile_id)
    rows = (await db.execute(stmt.limit(100))).all()
    return [_run_to_response(run, name) for run, name in rows]


@router.get("/runs/{run_id}", response_model=LeadQualificationRunResponse)
async def get_run(run_id: str, db: AsyncSession = Depends(get_db)):
    row = (await db.execute(
        select(LeadQualificationRun, LeadTargetProfile.name)
        .join(LeadTargetProfile, LeadTargetProfile.id == LeadQualificationRun.target_profile_id)
        .where(LeadQualificationRun.id == run_id)
    )).one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Run non trovata")
    return _run_to_response(row[0], row[1])


@router.post("/runs/{run_id}/cancel", response_model=LeadQualificationRunResponse)
async def cancel_run(run_id: str, db: AsyncSession = Depends(get_db)):
    row = (await db.execute(
        select(LeadQualificationRun, LeadTargetProfile.name)
        .join(LeadTargetProfile, LeadTargetProfile.id == LeadQualificationRun.target_profile_id)
        .where(LeadQualificationRun.id == run_id)
    )).one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Run non trovata")
    run, profile_name = row
    if run.status not in (LeadQualificationRunStatus.queued, LeadQualificationRunStatus.running):
        raise HTTPException(
            status_code=400,
            detail=f"Run non cancellabile in stato '{run.status.value if hasattr(run.status, 'value') else run.status}'",
        )
    run.status = LeadQualificationRunStatus.cancelled
    run.completed_at = datetime.utcnow()
    run.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(run)
    return _run_to_response(run, profile_name)


def _latest_results_subquery(target_profile_id: str | None):
    stmt = select(
        LeadQualification.target_profile_id.label("target_profile_id"),
        LeadQualification.global_contact_id.label("global_contact_id"),
        func.max(LeadQualification.created_at).label("created_at"),
    )
    if target_profile_id:
        stmt = stmt.where(LeadQualification.target_profile_id == target_profile_id)
    return stmt.group_by(LeadQualification.target_profile_id, LeadQualification.global_contact_id).subquery("latest_lq")


def _parse_statuses(status_value: str | None) -> list[str]:
    """status puo' essere singolo o comma-separated ('match,ambiguous'). Vuoto = tutti."""
    if not status_value:
        return []
    valid = {item.value for item in LeadQualificationStatus}
    out = []
    for raw in status_value.split(","):
        s = raw.strip()
        if not s:
            continue
        if s not in valid:
            raise HTTPException(status_code=422, detail=f"status non valido: {s}")
        out.append(s)
    return out


def _results_base(
    *,
    target_profile_id: str | None,
    run_id: str | None,
    status_value: str | None,
    min_score: int | None,
):
    statuses = _parse_statuses(status_value)
    if run_id:
        stmt = (
            select(LeadQualification, GlobalContact, LeadTargetProfile)
            .join(GlobalContact, GlobalContact.id == LeadQualification.global_contact_id)
            .join(LeadTargetProfile, LeadTargetProfile.id == LeadQualification.target_profile_id)
            .where(LeadQualification.run_id == run_id)
        )
    else:
        latest = _latest_results_subquery(target_profile_id)
        stmt = (
            select(LeadQualification, GlobalContact, LeadTargetProfile)
            .join(GlobalContact, GlobalContact.id == LeadQualification.global_contact_id)
            .join(LeadTargetProfile, LeadTargetProfile.id == LeadQualification.target_profile_id)
            .join(
                latest,
                and_(
                    latest.c.target_profile_id == LeadQualification.target_profile_id,
                    latest.c.global_contact_id == LeadQualification.global_contact_id,
                    latest.c.created_at == LeadQualification.created_at,
                ),
            )
        )
    if target_profile_id:
        stmt = stmt.where(LeadQualification.target_profile_id == target_profile_id)
    if statuses:
        stmt = stmt.where(LeadQualification.status.in_([LeadQualificationStatus(s) for s in statuses]))
    if min_score is not None:
        stmt = stmt.where(LeadQualification.final_score >= min_score)
    return stmt


def _result_to_response(row) -> LeadQualificationResultResponse:
    q, gc, profile = row
    return LeadQualificationResultResponse(
        id=q.id,
        target_profile_id=profile.id,
        target_profile_name=profile.name,
        run_id=q.run_id,
        ig_user_id=gc.ig_user_id,
        username=gc.username,
        full_name=gc.full_name,
        biography=gc.biography,
        phone=gc.phone,
        email=gc.email,
        whatsapp=gc.whatsapp,
        external_url=gc.external_url,
        bio_links=safe_json_loads(gc.bio_links, []),
        status=q.status.value if hasattr(q.status, "value") else str(q.status),
        confidence_score=q.final_score,
        deterministic_score=q.deterministic_score,
        ai_score=q.ai_score,
        ai_used=q.ai_used,
        matched_signals=safe_json_loads(q.matched_signals, []),
        negative_signals=safe_json_loads(q.negative_signals, []),
        reason=q.reason,
        first_seen_at=gc.first_seen_at,
        created_at=q.created_at,
    )


@router.get("/results", response_model=LeadQualificationResultListResponse)
async def list_results(
    target_profile_id: str | None = Query(default=None),
    run_id: str | None = Query(default=None),
    status_value: str | None = Query(default=None, alias="status"),
    min_score: int | None = Query(default=None, ge=0, le=100),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    stmt = _results_base(
        target_profile_id=target_profile_id,
        run_id=run_id,
        status_value=status_value,
        min_score=min_score,
    )
    count_sq = stmt.subquery()
    total = await db.scalar(select(func.count()).select_from(count_sq)) or 0
    rows = (await db.execute(
        stmt.order_by(LeadQualification.final_score.desc(), LeadQualification.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )).all()
    return LeadQualificationResultListResponse(
        items=[_result_to_response(row) for row in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/results/export")
async def export_results_csv(
    target_profile_id: str = Query(...),
    run_id: str | None = Query(default=None),
    # status: singolo o comma-separated ('match,ambiguous'); vuoto/assente = tutti.
    # Niente piu' default 'match'/80 silenzioso: l'export rispetta esattamente la selezione.
    status_value: str | None = Query(default=None, alias="status"),
    min_score: int = Query(default=0, ge=0, le=100),
    db: AsyncSession = Depends(get_db),
):
    stmt = _results_base(
        target_profile_id=target_profile_id,
        run_id=run_id,
        status_value=status_value,
        min_score=min_score,
    ).order_by(LeadQualification.final_score.desc(), LeadQualification.created_at.desc())
    rows = (await db.execute(stmt)).all()

    output = io.StringIO()
    # delimiter ';' = separatore nativo di Excel italiano. Il modulo csv quota
    # automaticamente i campi che contengono ';' (es. bio "...sleale; Agente..."),
    # cosi' non si spaccano in colonne sbagliate. Con ',' restavano nudi.
    writer = csv.DictWriter(output, delimiter=";", fieldnames=[
        "ig_user_id", "username", "full_name", "biography",
        "phone", "email", "whatsapp", "external_url", "bio_links",
        "target_profile", "qualification_status", "confidence_score",
        "deterministic_score", "ai_score", "ai_used",
        "matched_signals", "negative_signals", "first_seen_at",
        "scrape_sources", "scraping_accounts",
    ])
    writer.writeheader()
    for q, gc, profile in rows:
        scrape_sources = safe_json_loads(gc.scrape_sources, [])
        scraping_accounts = sorted({
            e.get("scraping_account_username") for e in scrape_sources
            if isinstance(e, dict) and e.get("scraping_account_username")
        })
        writer.writerow({
            "ig_user_id": gc.ig_user_id,
            "username": gc.username or "",
            "full_name": gc.full_name or "",
            "biography": (gc.biography or "").replace("\n", " ").replace("\r", " ").replace("\t", " "),
            "phone": gc.phone or "",
            "email": gc.email or "",
            "whatsapp": gc.whatsapp or "",
            "external_url": gc.external_url or "",
            "bio_links": " | ".join(
                str(link.get("url") or "") for link in safe_json_loads(gc.bio_links, [])
                if isinstance(link, dict)
            ),
            "target_profile": profile.name,
            "qualification_status": q.status.value if hasattr(q.status, "value") else str(q.status),
            "confidence_score": q.final_score,
            "deterministic_score": q.deterministic_score,
            "ai_score": q.ai_score if q.ai_score is not None else "",
            "ai_used": "yes" if q.ai_used else "no",
            "matched_signals": safe_json_dumps(safe_json_loads(q.matched_signals, [])),
            "negative_signals": safe_json_dumps(safe_json_loads(q.negative_signals, [])),
            "first_seen_at": gc.first_seen_at.isoformat() if gc.first_seen_at else "",
            "scrape_sources": safe_json_dumps(scrape_sources),
            "scraping_accounts": ",".join(scraping_accounts),
        })

    output.seek(0)
    filename = f"lead_qualification_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
    # BOM utf-8-sig: senza, Excel legge il CSV come cp1252 e accenti/emoji
    # diventano mojibake. Il BOM forza Excel a interpretarlo come UTF-8.
    return StreamingResponse(
        iter([output.getvalue().encode("utf-8-sig")]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
