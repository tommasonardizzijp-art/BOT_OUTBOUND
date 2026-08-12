"""Comportamento umano di input, condiviso tra i canali browser.

Estratto da InstagramPage il 27/07 durante M1. Prima di questa estrazione lo
stesso codice esisteva in TRE copie: InstagramPage._human_type, la copia negli
script PoC di M0 (che nel proprio docstring dichiarava di essere una copia), e
quella che sarebbe finita nel POM WhatsApp. Tre copie significano che una
taratura anti-detect corretta in un posto resta sbagliata negli altri due.

NON cambiare le costanti numeriche senza una misura. Sono tarate su un utente
"digitale" (~100 WPM di picco) e la loro varianza E' la mitigazione: un ritardo
fisso e' varianza zero, cioe' la firma robotica piu' banale da misurare.
"""
import asyncio
import math
import random

from app.config import settings

# Tasti adiacenti su QWERTY, per generare typo plausibili.
QWERTY_ADJACENT: dict[str, str] = {
    'q': 'wa',   'w': 'qes',  'e': 'wrd',  'r': 'etf',  't': 'ryg',
    'y': 'tuh',  'u': 'yij',  'i': 'uok',  'o': 'ipl',  'p': 'ol',
    'a': 'qsz',  's': 'awdz', 'd': 'sefc', 'f': 'drgv', 'g': 'fthb',
    'h': 'gyun', 'j': 'huim', 'k': 'jiol', 'l': 'kop',
    'z': 'asx',  'x': 'zdc',  'c': 'xfv',  'v': 'cgb',  'b': 'vhn',
    'n': 'bhm',  'm': 'nj',
    # Fila dei numeri. Mancava, e le cifre erano l'unica classe di caratteri
    # immune ai typo: `typo_char('3')` tornava None, quindi nella ricerca di
    # WhatsApp Web -- dove si digita un E.164 di 13 caratteri -- il ramo del
    # typo veniva estratto regolarmente e poi non produceva niente. Zero errori
    # sui numeri, sempre, nella fase in cui un umano sbaglia di piu'.
    # I vicini restano SULLA FILA: un numero di telefono si batte sulla riga in
    # alto, e un typo che producesse una lettera sarebbe l'errore di un'altra
    # tastiera.
    '1': '2',    '2': '13',   '3': '24',   '4': '35',   '5': '46',
    '6': '57',   '7': '68',   '8': '79',   '9': '80',   '0': '9',
}


def typo_char(char: str) -> str | None:
    """Un tasto adiacente plausibile per char (conserva il maiuscolo), o None."""
    adjacent = QWERTY_ADJACENT.get(char.lower())
    if not adjacent:
        return None
    wrong = random.choice(adjacent)
    return wrong.upper() if char.isupper() else wrong


async def human_type(page, element, text: str, *, timing_multiplier: float = 1.0,
                     newline_key: str = "Shift+Enter") -> None:
    """Digita con velocita' variabile, pause tra le parole e typo corretti.

    Clicca l'elemento per dargli il focus, poi usa page.keyboard per tutto il
    resto: cosi' non si ri-localizza l'elemento a ogni carattere, cosa che
    fallisce se il DOM React del sito si ri-renderizza durante la digitazione.

    newline_key e' un parametro e non una costante perche' e' una regola DEL
    SITO, non del nostro codice: su IG e su WhatsApp Web un Enter nudo INVIA il
    messaggio, quindi un a-capo battuto come Enter spedisce meta' testo.
    """
    await element.click()
    await asyncio.sleep(random.uniform(0.2, 0.5))

    base_ms = random.uniform(40, 95) * timing_multiplier

    for line_idx, line in enumerate(text.split('\n')):
        if line_idx > 0:
            await page.keyboard.press(newline_key)
            await asyncio.sleep(random.uniform(0.15, 0.5))

        words = line.split(' ')
        for i, word in enumerate(words):
            # Pausa di pensiero occasionale prima di una parola.
            if i > 0 and random.random() < 0.07:
                await asyncio.sleep(random.uniform(0.25, 1.0))

            for char_idx, char in enumerate(word):
                # Typo su un carattere INTERNO di una parola/blocco piu' lungo di
                # 3: mai sul primo ne' sull'ultimo, che sono quelli che si
                # sbagliano di meno. La probabilita' e' configurabile
                # (HUMAN_TYPO_PROBABILITY, default 0.10, era 0.08 cablato):
                # tararla e' una decisione di anti-detection, non una modifica
                # al sorgente.
                if (len(word) > 3 and 0 < char_idx < len(word) - 1
                        and random.random() < settings.human_typo_probability):
                    wrong = typo_char(char)
                    if wrong:
                        err_delay = random.lognormvariate(math.log(base_ms), 0.45)
                        await page.keyboard.type(wrong)
                        await asyncio.sleep(max(30, min(480, err_delay)) / 1000)
                        await asyncio.sleep(random.uniform(0.12, 0.40))   # se ne accorge
                        await page.keyboard.press("Backspace")
                        await asyncio.sleep(random.uniform(0.06, 0.20))   # prima di ribattere

                delay_ms = max(30, min(480, random.lognormvariate(math.log(base_ms), 0.45)))
                await page.keyboard.type(char)
                await asyncio.sleep(delay_ms / 1000)
                # Micro-pausa rara dentro una parola (rilettura, esitazione).
                if random.random() < 0.015:
                    await asyncio.sleep(random.uniform(0.2, 0.7))

            if i < len(words) - 1:
                await page.keyboard.type(' ')
                await asyncio.sleep(random.uniform(25, 80) / 1000)


async def human_click(page, element) -> None:
    """Clicca in un punto casuale dentro il bounding box dell'elemento.

    Il box si calcola subito prima del click: un'attesa lunga tra calcolo e
    click lascia che il layout si sposti e le coordinate diventino stale.
    """
    try:
        await element.scroll_into_view_if_needed(timeout=1500)
    except Exception:
        pass
    box = await element.bounding_box()
    if not box:
        await element.click()
        return
    x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
    y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
    await page.mouse.move(x, y, steps=random.randint(5, 15))
    await asyncio.sleep(random.uniform(0.05, 0.15))
    await page.mouse.click(x, y)
