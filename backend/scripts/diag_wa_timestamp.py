"""Diagnostica una tantum (21/08): il pilota del backfill ha marcato 4/5
contatti come 'replied' senza alcun controllo temporale (read_inbound_tail
non porta timestamp) -- sospetto di Tommaso, motivato: tasso di risposta
medio ~10%, 4/5 nel pilota e' un'anomalia. Verifica dal vivo, per ogni
contatto passato: cosa c'e' DAVVERO in quella chat, con timestamp reale
(data-pre-plain-text, pattern WhatsApp Web -- verificato qui per la prima
volta) confrontato contro il sent_at del nostro invio campagna. SOLA
LETTURA."""
import asyncio
import re
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.browser.whatsapp_page import WhatsAppWebPage
from app.database import AsyncSessionLocal
from app.models.wa import WaContact, WaMessage
from app.services import wa_profile_lock
from app.services.wa_session import WHATSAPP_WEB_URL, _open_wa_browser, _wa_number_or_raise
from app.utils.crypto import decrypt

NUMERO_ID = "e4b020cc-f906-4dbe-981a-27a4973c253f"
FUSO_ROMA_ESTATE = timezone(timedelta(hours=2))  # CEST, approssimato: nessuna libreria tz disponibile

_JS_DUMP = """
() => {
  const rows = Array.from(document.querySelectorAll("#main [data-id], #main [data-testid^='conv-msg-']"));
  return rows.map((el) => {
    const copy = el.querySelector('.copyable-text');
    return {
      data_pre_plain_text: copy ? copy.getAttribute('data-pre-plain-text') : null,
      text: (el.innerText || '').slice(0, 120),
      aria_tu: !!el.querySelector("span[aria-label='Tu:']"),
    };
  });
}
"""

_RE_TS = re.compile(r"\[(\d{2}):(\d{2}), (\d{2})/(\d{2})/(\d{4})\]")


def _parse_ts(pre_plain_text: str):
    m = _RE_TS.search(pre_plain_text or "")
    if not m:
        return None
    hh, mm, dd, mo, yyyy = (int(x) for x in m.groups())
    return datetime(yyyy, mo, dd, hh, mm, tzinfo=FUSO_ROMA_ESTATE)


async def controlla_un_contatto(pom, page, contact_id: str):
    async with AsyncSessionLocal() as db:
        contact = await db.get(WaContact, contact_id)
        e164 = decrypt(contact.encrypted_phone)
        msg = await db.scalar(select(WaMessage).where(WaMessage.contact_id == contact_id))
        sent_at_utc = msg.sent_at if msg else None

    aperto = await pom.open_chat(e164)
    if not aperto.ok:
        print(f"[{contact_id[:8]}] open_chat fallita: {aperto.signal}")
        return
    await pom.load_history(minimo=300)
    righe = await page.evaluate(_JS_DUMP)

    print(f"\n=== {contact_id[:8]} === sent_at (UTC)={sent_at_utc} messaggi_dom={len(righe)}")
    trovata_risposta_48h = False
    for r in righe:
        ts = _parse_ts(r["data_pre_plain_text"])
        if ts is None or r["aria_tu"]:
            continue  # solo inbound con timestamp leggibile
        ts_utc = ts.astimezone(timezone.utc)
        entro_48h = sent_at_utc is not None and sent_at_utc <= ts_utc <= sent_at_utc + timedelta(hours=48)
        testo = (r["text"] or "")[:80].encode("ascii", "replace").decode("ascii")
        flag = "  <-- ENTRO 48H DAL NOSTRO INVIO" if entro_48h else ""
        print(f"  {ts_utc.isoformat()}  {testo!r}{flag}")
        if entro_48h:
            trovata_risposta_48h = True
    print(f"  => risposta genuina entro 48h: {trovata_risposta_48h}")


async def main():
    contatti = sys.argv[1:] or [
        "3291d8e0-5a25-4649-ba76-885c5ba4cc33",
        "337d30b6-e001-4f9f-ad93-435b686baebf",
        "3b04bbe6-bc47-4ce8-80c3-b0ea89dd5437",
    ]
    async with AsyncSessionLocal() as db:
        numero = await _wa_number_or_raise(db, NUMERO_ID)
        proxy_url = numero.proxy_url

    async with wa_profile_lock.held(NUMERO_ID):
        async with _open_wa_browser(NUMERO_ID, headless=True, proxy_url=proxy_url) as context:
            page = await context.new_page()
            await page.goto(WHATSAPP_WEB_URL, wait_until="domcontentloaded")
            pom = WhatsAppWebPage(page)
            stato = await pom.session_state()
            print(f"session_state={stato}")
            if stato != "logged_in":
                return
            for cid in contatti:
                await controlla_un_contatto(pom, page, cid)


asyncio.run(main())
