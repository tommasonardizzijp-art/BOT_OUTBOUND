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


@pytest.mark.asyncio
async def test_human_type_batte_il_testo_e_corregge_i_typo(monkeypatch):
    from app.browser import human_input

    _sleep_reale = asyncio.sleep   # catturare PRIMA del patch, o e' ricorsione infinita
    monkeypatch.setattr(asyncio, "sleep", lambda *_a, **_k: _sleep_reale(0))
    page, element = FakePage(), FakeElement()

    await human_input.human_type(page, element, "ciao mondo")

    battuti = "".join(page.keyboard.typed)
    backspace = page.keyboard.pressed.count("Backspace")
    assert len(battuti) - backspace == len("ciao mondo")
    assert element.clicked is True


@pytest.mark.asyncio
async def test_human_type_usa_shift_enter_per_gli_a_capo(monkeypatch):
    """Un a-capo battuto come Enter INVIA il messaggio a meta'. Su entrambi i siti."""
    from app.browser import human_input

    _sleep_reale = asyncio.sleep   # catturare PRIMA del patch, o e' ricorsione infinita
    monkeypatch.setattr(asyncio, "sleep", lambda *_a, **_k: _sleep_reale(0))
    page, element = FakePage(), FakeElement()

    await human_input.human_type(page, element, "riga uno\nriga due")

    assert page.keyboard.pressed.count("Shift+Enter") == 1
    assert "Enter" not in page.keyboard.pressed


def test_typo_char_resta_sulla_tastiera_e_conserva_il_maiuscolo():
    from app.browser.human_input import QWERTY_ADJACENT, typo_char

    for _ in range(50):
        wrong = typo_char("S")
        assert wrong is not None
        assert wrong.isupper()
        assert wrong.lower() in QWERTY_ADJACENT["s"]


def test_typo_char_sbaglia_anche_le_cifre_e_resta_sulla_fila_dei_numeri():
    """Le cifre erano l'unica classe di caratteri immune ai typo.

    `QWERTY_ADJACENT` conteneva solo lettere, quindi `typo_char('3')` tornava
    None: nella ricerca di WhatsApp Web, dove si digita un numero E.164 di 13
    caratteri, il ramo del typo veniva estratto regolarmente e poi non
    produceva nulla. Zero errori sui numeri, per costruzione, nella fase in cui
    un umano sbaglia di piu' (feedback di Tommaso dopo aver guardato la
    campagna del 12/08 dal browser). Il vicino deve stare sulla FILA DEI
    NUMERI: un numero di telefono si batte sulla riga in alto, e un typo che
    producesse una lettera non sarebbe l'errore di quella fila.
    """
    from app.browser.human_input import typo_char

    for cifra in "0123456789":
        visti = {typo_char(cifra) for _ in range(60)}
        assert None not in visti, f"nessun typo possibile sulla cifra {cifra}"
        assert all(v.isdigit() for v in visti), (
            f"typo fuori dalla fila dei numeri per {cifra}: {visti}")
        assert cifra not in visti, f"il typo di {cifra} non puo' essere {cifra}"


@pytest.mark.asyncio
async def test_human_type_sbaglia_anche_digitando_un_numero_di_telefono(monkeypatch):
    """Il testo finale resta corretto, ma qualche Backspace deve comparire.

    Prova che i typo arrivino davvero alla fase numerica: prima di questo fix
    il ciclo girava e il testo usciva pulito perche' `typo_char` diceva sempre
    None, non perche' la sorte fosse clemente.
    """
    from app.browser import human_input

    _sleep_reale = asyncio.sleep   # catturare PRIMA del patch, o e' ricorsione infinita
    monkeypatch.setattr(asyncio, "sleep", lambda *_a, **_k: _sleep_reale(0))

    e164 = "+393421460077"
    backspace_totali = 0
    for _ in range(25):
        page, element = FakePage(), FakeElement()
        await human_input.human_type(page, element, e164)
        battuti = "".join(page.keyboard.typed)
        backspace = page.keyboard.pressed.count("Backspace")
        # invariante: quello che resta a schermo e' il numero giusto
        assert len(battuti) - backspace == len(e164)
        backspace_totali += backspace

    assert backspace_totali > 0, (
        "25 digitazioni di un numero di 13 cifre senza un solo errore: "
        "i typo non arrivano alla fase numerica")


def test_probabilita_typo_e_configurabile():
    """Tarare il tasso di errore non deve richiedere una modifica al sorgente."""
    from app.config import settings

    assert 0.0 <= settings.human_typo_probability <= 0.5
    assert settings.human_typo_probability == 0.10
