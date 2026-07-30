"""Kill-switch WA, stato invii, start/stop worker. Scheletro creato in
PR-0 e riempito da M3: M2 lo crea e non lo riapre mai piu' (contratto §5,
proprieta' dei file §5.3)."""
from fastapi import APIRouter

router = APIRouter(prefix="/wa/ops", tags=["wa-ops"])
