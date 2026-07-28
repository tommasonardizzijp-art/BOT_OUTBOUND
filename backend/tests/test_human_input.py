import asyncio
import pytest


class FakeKeyboard:
    def __init__(self):
        self.typed: list[str] = []
        self.pressed: list[str] = []

    async def type(self, text: str):
        self.typed.append(text)

    async def press(self, key: str):
        self.pressed.append(key)


class FakePage:
    def __init__(self):
        self.keyboard = FakeKeyboard()


class FakeElement:
    def __init__(self):
        self.clicked = False

    async def click(self):
        self.clicked = True


@pytest.mark.asyncio
async def test_instagram_human_type_ancora_digita_il_testo(monkeypatch):
    """Non-regressione IG: la digitazione resta corretta al netto dei typo."""
    from app.browser.instagram_page import InstagramPage

    # Le pause umane vanno azzerate, altrimenti il test dura minuti.
    # ATTENZIONE: il riferimento va catturato PRIMA del patch. Scrivere
    #   lambda *a, **k: asyncio.sleep(0)
    # sembra equivalente ma non lo e': dentro il corpo, `asyncio.sleep` viene
    # risolto a ogni chiamata e a quel punto e' gia' la lambda stessa. Il
    # risultato e' una ricorsione infinita che alloca senza fermarsi: misurato
    # 22 MB -> 1350 MB in 5 secondi, abbastanza da congelare la macchina.
    _sleep_reale = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda *_a, **_k: _sleep_reale(0))
    page, element = FakePage(), FakeElement()
    ig = InstagramPage.__new__(InstagramPage)
    ig._page = page
    ig._tm = 1.0

    await ig._human_type(element, "ciao come stai")

    assert element.clicked is True
    # I typo vengono corretti con Backspace: il numero di Backspace deve
    # corrispondere ai caratteri battuti in eccesso.
    battuti = "".join(page.keyboard.typed)
    backspace = page.keyboard.pressed.count("Backspace")
    assert len(battuti) - backspace == len("ciao come stai")
