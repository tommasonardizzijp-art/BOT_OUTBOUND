"""Rendering dei template del canale WhatsApp.

Esiste perche' template_renderer NON e' riusabile qui (contratto §2.4):
pick_template legge i campi Instagram (base_message_template) e su uno
WaSequenceStep prende stringa vuota senza sollevare; render_template
conosce solo {nome} e SOLLEVA su {ultimo_ordine}, cioe' esattamente i
placeholder che l'ingest raccoglie dalle colonne libere del CSV.

Lo spintax si RIUSA da template_renderer.resolve_spintax: una seconda
implementazione dello stesso parser e' una seconda occasione di divergere.
"""
import random
import re

from app.services.template_renderer import (RESIDUAL_PLACEHOLDER_RE,
                                            TemplateRenderError, resolve_spintax)

_NOME_RE = re.compile(r"\{nome\}|\[nome\]|\{name\}|\[name\]", re.IGNORECASE)
_ATTR_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]{0,60})\}")


def pick_wa_template(step, rng: random.Random | None = None) -> tuple[str, str]:
    """(testo, variante) fra i template compilati dello step, pesi uguali.
    Stessa semantica di template_renderer.pick_template, sui campi WA."""
    r = rng or random
    candidati = [(step.template_a or "", "a")]
    for campo, lettera in (("template_b", "b"), ("template_c", "c"), ("template_d", "d")):
        valore = getattr(step, campo, None)
        if (valore or "").strip():
            candidati.append((valore, lettera))
    return r.choice(candidati)


def render_wa_template(template: str, *, display_name: str | None,
                       attributes: dict | None, rng: random.Random | None = None) -> str:
    """spintax -> {nome} -> attributi -> normalizzazione.

    Solleva TemplateRenderError se resta un placeholder sconosciuto o se un
    attributo atteso e' vuoto PER QUESTO contatto: meglio non mandare UN
    messaggio che mandarne uno con un buco dentro.
    """
    out = resolve_spintax(template, rng=rng)
    out = _NOME_RE.sub((display_name or "").strip(), out)

    attrs = attributes or {}

    def _sostituisci(m: re.Match) -> str:
        chiave = m.group(1)
        if chiave not in attrs:
            raise TemplateRenderError(f"Placeholder sconosciuto: {{{chiave}}}")
        valore = str(attrs[chiave] or "").strip()
        if not valore:
            raise TemplateRenderError(
                f"Attributo {{{chiave}}} vuoto per questo contatto: non si manda "
                "un messaggio con un buco dentro")
        return valore

    out = _ATTR_RE.sub(_sostituisci, out)

    residuo = RESIDUAL_PLACEHOLDER_RE.search(out)
    if residuo:
        raise TemplateRenderError(f"Placeholder non risolto: {residuo.group(0)!r}")
    out = re.sub(r"[ \t]{2,}", " ", out.replace("\r\n", "\n"))
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    # {nome} vuoto lascia una virgola orfana ("Ciao , promo."): il caso
    # tipico e' proprio "Ciao {nome}, ..." (contratto §2.4, esempi). Non e'
    # un segnaposto letterale come '@username' su IG, ma la punteggiatura
    # va comunque pulita, non solo il nome.
    out = re.sub(r"\s+,", ",", out)
    out = re.sub(r"^,\s*", "", out)
    if not out:
        raise TemplateRenderError("Template vuoto dopo il rendering")
    return out


def validate_wa_template(template: str, *, known_attributes: set[str]) -> list[str]:
    """Placeholder NON risolvibili con le colonne note. Lista vuota =
    template valido. E' il gate al salvataggio di uno step (M2 Task 6): un
    template con placeholder ignoti non si salva."""
    testo = resolve_spintax(template, rng=random.Random(0))
    testo = _NOME_RE.sub("x", testo)
    return [m.group(1) for m in _ATTR_RE.finditer(testo)
            if m.group(1) not in known_attributes]
