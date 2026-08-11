"""Collaudo dal vivo della Fase A auto-discover, sul numero vero di Primero.

Perche' questo file sta QUI e non nel worktree: i path del `.env` e dei profili
Chromium sono relativi alla cartella backend, e lanciare il browser da un
worktree creerebbe un profilo VUOTO -- cioe' un dispositivo nuovo per WhatsApp,
con tutto quel che comporta. Si lavora dalla cartella vera (cwd), ma il CODICE
si importa dal worktree, che e' l'unico ad avere la Fase A: la dir principale e'
su un branch di un'altra sessione.

Cosa fa, in ordine:
  1. fotografa lo stato PRIMA (messaggi inviati, contatore giornaliero del
     numero, righe gia' in staging);
  2. esegue un giro di scansione con il browser IN FINESTRA (Tommaso guarda);
  3. rifotografa lo stato DOPO e verifica le invarianti che contano davvero --
     quelle delle categorie G e H della lista adversarial, le uniche che
     dimostrano che P12 regge e che la Fase A NON invia.

Non e' un test unitario: e' l'unica prova che il motore funzioni contro il DOM
vero. Tutti i 93 test della suite girano su pagine finte.
"""
import asyncio
import os
import sys

# Il codice della Fase A vive nel worktree; i dati (env, profili) qui.
WORKTREE = r"D:\BOT OUTBOUND\.worktrees\wa-fase4-autodiscover\backend"
sys.path.insert(0, WORKTREE)
sys.path.insert(1, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import func, select, text        # noqa: E402

NUMERO_PRIMERO = "8c578c08-6659-43fa-9840-f55e88e220fc"


async def _fotografia(db, number_id: str) -> dict:
    """Lo stato che la Fase A NON deve toccare, piu' quello che deve riempire."""
    from app.models.wa import WaDiscoveredChat, WaMessage, WaNumber

    numero = await db.scalar(select(WaNumber).where(WaNumber.id == number_id))
    return {
        "wa_messages_totali": await db.scalar(select(func.count(WaMessage.id))) or 0,
        "sent_today": numero.sent_today,
        "sent_date": numero.sent_date,
        "warmup_day": numero.warmup_day,
        "status_numero": numero.status.value,
        "scoperte": await db.scalar(
            select(func.count(WaDiscoveredChat.id)).where(
                WaDiscoveredChat.number_id == number_id)) or 0,
    }


async def _invarianti(db, number_id: str) -> list[tuple[str, bool, str]]:
    """Le verifiche della categoria G: si eseguono in SQL sui dati veri appena
    scritti, non su asserzioni unitarie. Ognuna ritorna (nome, passata, dettaglio).
    """
    from app.models.wa import WaDiscoveredChat

    esiti = []

    # G1 - nessun numero in chiaro in un campo testuale (P12). Si cerca una
    # sequenza di 8+ cifre: un nome vero non ne ha, un numero si'.
    righe = (await db.execute(
        select(WaDiscoveredChat.chat_title, WaDiscoveredChat.display_name).where(
            WaDiscoveredChat.number_id == number_id)
    )).all()
    import re
    otto_cifre = re.compile(r"(?:\D*\d){8}")
    sporche = [
        (t, d) for t, d in righe
        if (t and otto_cifre.match(t.replace("\u2022", ""))
            and "\u2022" not in t)
        or (d and otto_cifre.match(d.replace("\u2022", "")) and "\u2022" not in d)
    ]
    esiti.append(("G1 nessun numero in chiaro (P12)", not sporche,
                  f"{len(sporche)} righe sporche" if sporche else "0 righe sporche"))

    # G2 - numero_leggibile=True implica phone_hmac presente.
    incoerenti = await db.scalar(select(func.count(WaDiscoveredChat.id)).where(
        WaDiscoveredChat.number_id == number_id,
        WaDiscoveredChat.numero_leggibile.is_(True),
        WaDiscoveredChat.phone_hmac.is_(None)))
    esiti.append(("G2 leggibile senza hmac", not incoerenti, f"{incoerenti} righe"))

    # G3 - nessuna riga senza titolo E senza numero: non sarebbe deduplicabile
    # da nessuna delle due unique, ne' identificabile dall'operatore.
    fantasma = await db.scalar(select(func.count(WaDiscoveredChat.id)).where(
        WaDiscoveredChat.number_id == number_id,
        WaDiscoveredChat.chat_title.is_(None),
        WaDiscoveredChat.phone_hmac.is_(None)))
    esiti.append(("G3 righe senza identita'", not fantasma, f"{fantasma} righe"))

    # G4/G5 - nessun duplicato sulle due chiavi.
    dup_titolo = (await db.execute(text(
        "select count(*) from (select chat_title from wa_discovered_chats "
        "where number_id = :n and chat_title is not null "
        "group by chat_title having count(*) > 1) x"), {"n": number_id})).scalar()
    esiti.append(("G4 duplicati per titolo", not dup_titolo, f"{dup_titolo} gruppi"))
    dup_hmac = (await db.execute(text(
        "select count(*) from (select phone_hmac from wa_discovered_chats "
        "where number_id = :n and phone_hmac is not null "
        "group by phone_hmac having count(*) > 1) x"), {"n": number_id})).scalar()
    esiti.append(("G5 duplicati per numero", not dup_hmac, f"{dup_hmac} gruppi"))

    return esiti


async def main():
    from app.database import AsyncSessionLocal
    from app.services.wa_discover_run import esegui_discover_run

    async with AsyncSessionLocal() as db:
        prima = await _fotografia(db, NUMERO_PRIMERO)
    print("=== PRIMA ===")
    for k, v in prima.items():
        print(f"  {k}: {v}")

    print("\n=== SCAN (browser in finestra, chiudilo per fermare) ===", flush=True)
    esito = await esegui_discover_run(NUMERO_PRIMERO, headless=False)
    print("\n=== ESITO SCAN ===")
    for k, v in esito.items():
        print(f"  {k}: {v}")

    async with AsyncSessionLocal() as db:
        dopo = await _fotografia(db, NUMERO_PRIMERO)
        invarianti = await _invarianti(db, NUMERO_PRIMERO)

    print("\n=== DOPO ===")
    for k, v in dopo.items():
        print(f"  {k}: {v}")

    print("\n=== H — la Fase A non invia (invariante che la DEFINISCE) ===")
    controlli_h = [
        ("H1 wa_messages invariato",
         prima["wa_messages_totali"] == dopo["wa_messages_totali"],
         f"{prima['wa_messages_totali']} -> {dopo['wa_messages_totali']}"),
        ("H2 sent_today invariato",
         prima["sent_today"] == dopo["sent_today"],
         f"{prima['sent_today']} -> {dopo['sent_today']}"),
        ("H3 warmup_day invariato",
         prima["warmup_day"] == dopo["warmup_day"],
         f"{prima['warmup_day']} -> {dopo['warmup_day']}"),
        ("H4 stato numero invariato",
         prima["status_numero"] == dopo["status_numero"],
         f"{prima['status_numero']} -> {dopo['status_numero']}"),
    ]
    for nome, ok, dettaglio in controlli_h:
        print(f"  [{'OK' if ok else 'FAIL'}] {nome}: {dettaglio}")

    print("\n=== G — invarianti sui dati scritti ===")
    for nome, ok, dettaglio in invarianti:
        print(f"  [{'OK' if ok else 'FAIL'}] {nome}: {dettaglio}")

    # G7 - COPERTURA. Mancava nella prima versione di questo script, ed e' il
    # motivo per cui il collaudo dell'11/08 ha stampato "TUTTO VERDE" con UNA
    # chat raccolta su 291: le invarianti di sicurezza passavano tutte, ma il
    # motore non aveva funzionato. Un collaudo che verifica solo di non aver
    # fatto danni, e non di aver fatto il lavoro, e' lo stesso verde che questo
    # progetto ha gia' pagato una volta (90/90 con zero messaggi inviati).
    dichiarato = esito.get("dichiarato")
    raccolte = dopo["scoperte"]
    if dichiarato:
        copertura = raccolte / dichiarato
        ok_copertura = copertura >= 0.80
        dettaglio = (f"{raccolte}/{dichiarato} = {copertura:.0%} "
                     f"(soglia 80%; motivo scan: {esito.get('motivo')})")
    else:
        ok_copertura = False
        dettaglio = "totale dichiarato ignoto: impossibile dire se la raccolta e' completa"
    invarianti = list(invarianti) + [("G7 copertura della raccolta", ok_copertura, dettaglio)]
    print(f"  [{'OK' if ok_copertura else 'FAIL'}] G7 copertura della raccolta: {dettaglio}")

    falliti = [n for n, ok, _ in controlli_h + invarianti if not ok]
    print(f"\n=== ESITO COLLAUDO: {'TUTTO VERDE' if not falliti else 'FALLITE: ' + ', '.join(falliti)} ===")


for flusso in (sys.stdout, sys.stderr):
    try:
        flusso.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

asyncio.run(main())
