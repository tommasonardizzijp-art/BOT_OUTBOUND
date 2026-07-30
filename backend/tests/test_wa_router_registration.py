"""Non-regressione: il canale IG e' in produzione. Si scrive PRIMA di
toccare main.py (contratto M2/M3 §8.4, piano PR-0 Task 1 Step 2)."""


def test_i_router_instagram_restano_registrati():
    """followers.router e' annidato sotto /campaigns/{campaign_id}/followers,
    non sotto /followers: verificato sul codice reale (app/api/followers.py),
    il piano assumeva un prefisso piatto che non esiste."""
    from app.main import app
    paths = {r.path for r in app.routes}
    for atteso in ("/api/campaigns", "/api/accounts", "/api/health"):
        assert any(p.startswith(atteso) for p in paths), f"{atteso} sparito"
    assert any("/followers" in p for p in paths), "router followers sparito"


def test_i_cinque_router_wa_hanno_il_prefisso_giusto():
    from app.api import tenants, wa_campaigns, wa_contacts, wa_numbers, wa_ops
    assert tenants.router.prefix == "/tenants"
    assert wa_numbers.router.prefix == "/wa/numbers"
    assert wa_campaigns.router.prefix == "/wa/campaigns"
    assert wa_contacts.router.prefix == "/wa/contacts"
    assert wa_ops.router.prefix == "/wa/ops"


def test_i_cinque_router_wa_sono_registrati_in_main():
    """Un APIRouter senza endpoint non compare in app.routes (verificato:
    FastAPI non tiene un oggetto Mount per l'inclusione, si limita a
    fondere le route del sotto-router in quelle del padre) -- con zero
    endpoint zero tracce. Si verifica quindi sulla sorgente di main.py,
    che resta vera anche quando gli endpoint arriveranno (Task 4-6)."""
    import inspect

    from app import main
    sorgente = inspect.getsource(main)
    for nome in ("tenants.router", "wa_numbers.router", "wa_campaigns.router",
                 "wa_contacts.router", "wa_ops.router"):
        assert f"include_router({nome}" in sorgente, f"{nome} non registrato in main.py"
