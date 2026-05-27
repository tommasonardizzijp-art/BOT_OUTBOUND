"""Anomalies API — list and acknowledge events recorded by the anomaly detector."""
import json
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.database import get_db
from app.models.anomaly import Anomaly

router = APIRouter(prefix="/anomalies", tags=["anomalies"])


# TODO(2.A): add Depends(require_admin) to mutating + listing routes once auth is merged.


@router.get("")
async def list_anomalies(
    since_hours: int = Query(default=24, ge=1, le=24 * 30),
    campaign_id: str | None = None,
    account_id: str | None = None,
    kind: str | None = None,
    only_unacknowledged: bool = False,
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    cutoff = datetime.utcnow() - timedelta(hours=since_hours)
    stmt = select(Anomaly).where(Anomaly.created_at >= cutoff)
    if campaign_id:
        stmt = stmt.where(Anomaly.campaign_id == campaign_id)
    if account_id:
        stmt = stmt.where(Anomaly.account_id == account_id)
    if kind:
        stmt = stmt.where(Anomaly.kind == kind)
    if only_unacknowledged:
        stmt = stmt.where(Anomaly.acknowledged_at.is_(None))

    total = await db.scalar(
        select(func.count()).select_from(stmt.order_by(None).subquery())
    ) or 0

    rows = (
        await db.execute(
            stmt.order_by(Anomaly.created_at.desc()).limit(limit)
        )
    ).scalars().all()

    items = []
    for a in rows:
        try:
            details = json.loads(a.details or "{}")
        except Exception:
            details = {"_raw": a.details}
        items.append({
            "id": a.id,
            "kind": a.kind,
            "severity": a.severity,
            "campaign_id": a.campaign_id,
            "account_id": a.account_id,
            "details": details,
            "created_at": a.created_at,
            "acknowledged_at": a.acknowledged_at,
        })
    return {"items": items, "total": total}


@router.post("/{anomaly_id}/ack")
async def acknowledge_anomaly(anomaly_id: str, db: AsyncSession = Depends(get_db)):
    a = await db.scalar(select(Anomaly).where(Anomaly.id == anomaly_id))
    if not a:
        raise HTTPException(status_code=404, detail="Anomaly not found")
    if a.acknowledged_at is None:
        a.acknowledged_at = datetime.utcnow()
        await db.commit()
    return {"ok": True, "id": a.id, "acknowledged_at": a.acknowledged_at}


@router.get("/summary")
async def anomalies_summary(
    since_hours: int = Query(default=24, ge=1, le=24 * 30),
    db: AsyncSession = Depends(get_db),
):
    """Counts by kind + by severity. Used by dashboard badge."""
    cutoff = datetime.utcnow() - timedelta(hours=since_hours)

    by_kind = (await db.execute(
        select(Anomaly.kind, func.count(Anomaly.id))
        .where(Anomaly.created_at >= cutoff)
        .group_by(Anomaly.kind)
    )).all()

    by_severity = (await db.execute(
        select(Anomaly.severity, func.count(Anomaly.id))
        .where(Anomaly.created_at >= cutoff)
        .group_by(Anomaly.severity)
    )).all()

    unack = await db.scalar(
        select(func.count(Anomaly.id))
        .where(Anomaly.created_at >= cutoff, Anomaly.acknowledged_at.is_(None))
    ) or 0

    return {
        "since_hours": since_hours,
        "by_kind": {k: c for k, c in by_kind},
        "by_severity": {s: c for s, c in by_severity},
        "unacknowledged": unack,
    }
