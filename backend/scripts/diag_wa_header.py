"""Diagnostica una tantum (21/08): read_open_chat_title() torna sempre None
nel pilota del backfill, anche se il selettore HEADER era verificato dal
vivo il 09-10/08 (poc4_info_panel.py, 20/20). Prima di ipotizzare, si
verifica: quale candidato aggancia (se aggancia), cosa contiene davvero.
SOLA LETTURA, nessuna scrittura DB, un solo contatto."""
import asyncio
import sys

from loguru import logger
from sqlalchemy import select

from app.browser import whatsapp_selectors as sel
from app.browser.whatsapp_page import WhatsAppWebPage
from app.database import AsyncSessionLocal
from app.models.wa import WaContact
from app.services import wa_profile_lock
from app.services.wa_session import WHATSAPP_WEB_URL, _open_wa_browser, _wa_number_or_raise
from app.utils.crypto import decrypt

NUMERO_ID = "e4b020cc-f906-4dbe-981a-27a4973c253f"


async def main():
    contact_id = sys.argv[1] if len(sys.argv) > 1 else None
    async with AsyncSessionLocal() as db:
        if contact_id:
            contact = await db.get(WaContact, contact_id)
        else:
            contact = await db.scalar(
                select(WaContact).where(WaContact.chat_title == "SPEDIZIONI")
                .order_by(WaContact.first_seen_at))
        e164 = decrypt(contact.encrypted_phone)
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

            aperto = await pom.open_chat(e164)
            print(f"open_chat: ok={aperto.ok} signal={aperto.signal} "
                 f"era_non_letto={aperto.era_non_letto}")

            for candidato in sel.HEADER:
                try:
                    loc = page.locator(candidato).first
                    await loc.wait_for(state="visible", timeout=3000)
                    testo = await loc.inner_text()
                    html = await loc.evaluate("el => el.outerHTML")
                    print(f"\n--- candidato '{candidato}': TROVATO ---")
                    print(f"inner_text: {testo!r}")
                    print(f"outerHTML (primi 1500 char): {html[:1500]!r}")
                except Exception as exc:
                    print(f"\n--- candidato '{candidato}': FALLITO ({type(exc).__name__}) ---")

            # read_open_chat_title() reale, per confronto diretto
            titolo = await pom.read_open_chat_title()
            print(f"\nread_open_chat_title() -> {titolo!r}")

            await page.screenshot(path="scripts/poc_wa/artifacts/diag_header_21_08.png")
            print("\nscreenshot: scripts/poc_wa/artifacts/diag_header_21_08.png")


asyncio.run(main())
