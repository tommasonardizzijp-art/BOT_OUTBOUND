"""Ingest e gestione contatti WhatsApp. Scheletro creato in PR-0 e
riempito da M2: la registrazione in main.py avviene UNA volta sola, cosi'
i due cantieri paralleli non toccano mai lo stesso file (contratto §5)."""
from fastapi import APIRouter

router = APIRouter(prefix="/wa/contacts", tags=["wa-contacts"])
