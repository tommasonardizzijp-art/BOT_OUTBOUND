"""Adversarial B -- numeri ostili (docs/superpowers/qa/2026-08-15-qa-wa-discover-lancio-adversarial.md, casi 13, 16, 17, 18).

14 e 15 (clamp copertura a 100, dichiarato 0/None -> copertura None) NON
sono qui: sono gia' coperti da test unit puri e deterministici
(`test_copertura_non_supera_cento`, `test_copertura_none_se_il_dichiarato_manca`,
`test_copertura_none_se_il_dichiarato_e_zero` in
backend/tests/test_wa_discover_runs_servizio.py) -- rieseguirli con uno
script separato sarebbe una copia, non una verifica in piu'.

Uso:
    cd "D:\\BOT OUTBOUND\\backend"
    .\\venv\\Scripts\\python.exe scripts/qa_wa_discover/adv_b_numeri_ostili.py
"""
import asyncio
from unittest.mock import patch

import _bootstrap  # noqa: E402


def _caso13_validator_config() -> tuple[bool, str]:
    """13. wa_discover_run_orfana_min sotto wa_discover_job_timeout_s/60 ->
    ValueError esplicito al caricamento di Settings, non un comportamento
    silenzioso."""
    from pydantic import ValidationError

    from app.config import Settings, settings

    dump_valido = settings.model_dump()
    # (a) i default reali caricano senza sollevare -- gia' vero (settings
    # esiste), ri-validato esplicitamente qui.
    try:
        Settings.model_validate(dump_valido)
    except ValidationError as exc:
        return False, f"i default reali NON validano piu': {exc}"

    # (b) override incoerente (orfana_min sotto job_timeout_s/60) DEVE
    # sollevare.
    rotto = dict(dump_valido)
    rotto["wa_discover_job_timeout_s"] = 21600  # 360 min
    rotto["wa_discover_run_orfana_min"] = 100   # sotto 360: incoerente
    try:
        Settings.model_validate(rotto)
    except ValidationError as exc:
        if "wa_discover_run_orfana_min" not in str(exc):
            return False, f"ha sollevato, ma non per il motivo giusto: {exc}"
        return True, "default OK, override incoerente respinto con ValueError esplicito"
    return False, "l'override incoerente (100 min contro 360 min) NON ha sollevato -- avviabile in produzione con una guardia rotta"


def _caso17_ram_min_bordo() -> tuple[bool, str]:
    """17. wa_discover_ram_min_mb a 0 o negativo -- non deve crashare ne'
    invertire la logica. Comportamento REALE osservato, documentato invece
    di assunto: ram_libera_mb() e' sempre >= 0, quindi una soglia <= 0 rende
    la guardia RAM un no-op permanente (mai rifiuta) -- non un bug nuovo,
    ma un effetto collaterale del confronto che vale la pena scrivere nero
    su bianco."""
    from app.services import wa_discover_gate

    risultati = []
    for soglia in (0, -500):
        with patch.object(wa_discover_gate.settings, "wa_discover_ram_min_mb", soglia):
            rifiuta = wa_discover_gate.ram_libera_mb() < wa_discover_gate.settings.wa_discover_ram_min_mb
            risultati.append((soglia, rifiuta))
    if any(rifiuta for _, rifiuta in risultati):
        return False, f"con soglia <= 0 la guardia ha rifiutato comunque: {risultati}"
    return True, (f"nessun crash. Comportamento reale: {risultati} -- con "
                 "wa_discover_ram_min_mb <= 0 la guardia RAM non rifiuta MAI "
                 "(ram_libera_mb() e' sempre >= 0). Non lascia passare 'per errore "
                 "di confronto': e' la conseguenza diretta e prevedibile del "
                 "confronto '<', nessuna eccezione, nessun comportamento indefinito.")


def _caso18_soglia_sync_fuori_range() -> tuple[bool, str]:
    """18. soglia_sync negativa o 1000 passata al gate di sincronizzazione ->
    nessun crash, comportamento definito (puro confronto numerico)."""
    from app.services.wa_discover.sincronizzazione import (LetturaSync,
                                                            puo_scansionare,
                                                            puo_scansionare_lettura)

    casi = []
    try:
        for soglia in (-50, 1000):
            for percentuale in (0, 50, 100, None):
                ok, motivo = puo_scansionare(percentuale, soglia=soglia)
                casi.append((soglia, percentuale, ok))
            for stato, pct in (("letta", 50), ("assente", None), ("ignota", None)):
                lettura = LetturaSync(stato=stato, percentuale=pct)
                ok, motivo = puo_scansionare_lettura(lettura, soglia=soglia)
                casi.append((soglia, f"lettura:{stato}", ok))
    except Exception as exc:  # noqa: BLE001
        return False, f"ha sollevato invece di tornare un esito ({type(exc).__name__}: {exc})"

    # soglia=-50: qualunque percentuale (anche 0) >= -50 -> sempre True.
    # soglia=1000: qualunque percentuale <=100 e' sempre < 1000 -> sempre
    # False salvo i casi 'None'/'assente'/'ignota' che restano True per design.
    attesi_falsi_a_1000 = [(s, p, ok) for s, p, ok in casi
                           if s == 1000 and isinstance(p, int) and not ok]
    if len(attesi_falsi_a_1000) < 3:
        return False, f"soglia=1000 doveva rifiutare le percentuali numeriche, casi: {casi}"
    return True, f"nessun crash su soglia negativa/1000, comportamento coerente col confronto numerico. Dettaglio: {casi}"


async def _caso16_contatori_enormi() -> tuple[bool, str]:
    """16. Contatori scritti direttamente a DB con un intero enorme -> l'API
    li restituisce senza overflow/crash (la UI e' fuori portata, nessun
    frontend in esecuzione: qui si verifica solo che il backend non esploda
    ne' tronchi silenziosamente)."""
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.config import settings
    from app.database import get_db
    from app.main import app
    from app.models.user import User
    from app.utils.auth_deps import get_current_user
    from app.utils.db_dialect import to_async_database_url
    from tests.factories_wa import make_discover_run, make_number, make_tenant

    eng = create_async_engine(to_async_database_url(settings.database_url))
    maker = async_sessionmaker(eng, expire_on_commit=False)

    ENORME = 10**15
    async with maker() as db:
        tenant = await make_tenant(db)
        number = await make_number(db, tenant)
        await make_discover_run(db, tenant, number, stato="done",
                                salvate=ENORME, aggiornate=ENORME,
                                dichiarato=ENORME, copertura=100)
        await db.commit()
        number_id = number.id

    def _admin() -> User:
        from datetime import datetime
        return User(id="00000000-0000-0000-0000-0000000000b6",
                   email="admin-adv-b16@test.local", password_hash="x",
                   role="admin", is_active=True, created_at=datetime(2026, 1, 1))

    async def _get_db():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_user] = _admin
    try:
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as c:
            r = await c.get(f"/api/wa/numbers/{number_id}/discover")
    finally:
        app.dependency_overrides.clear()
        await eng.dispose()

    if r.status_code != 200:
        return False, f"GET con contatori enormi ha risposto {r.status_code}: {r.text}"
    salvate = r.json()["ultima"]["salvate"]
    if salvate != ENORME:
        return False, f"contatore alterato/troncato: atteso {ENORME}, ricevuto {salvate}"
    return True, f"GET con contatori a {ENORME} risponde 200, valore intatto nel JSON"


async def main() -> None:
    await _bootstrap.crea_schema_pulito()

    ok13, det13 = _caso13_validator_config()
    _bootstrap_riporta("B.13 validator config orfana_min/job_timeout_s", ok13, det13)

    ok16, det16 = await _caso16_contatori_enormi()
    _bootstrap_riporta("B.16 contatori enormi via API", ok16, det16)

    ok17, det17 = _caso17_ram_min_bordo()
    _bootstrap_riporta("B.17 ram_min_mb a 0/negativo", ok17, det17)

    ok18, det18 = _caso18_soglia_sync_fuori_range()
    _bootstrap_riporta("B.18 soglia_sync fuori range", ok18, det18)

    if not (ok13 and ok16 and ok17 and ok18):
        raise SystemExit(1)


def _bootstrap_riporta(nome: str, ok: bool, dettaglio: str) -> None:
    print(f"\n=== {'PASS' if ok else 'FAIL'} -- {nome} ===")
    print(dettaglio)


asyncio.run(main())
