"""Rendering locale dei template DM (modalità no-AI).

Pipeline: spintax -> placeholder nome -> normalizzazione whitespace.
Nessuna dipendenza da ai_personalizer (è ai_personalizer che importa da qui).
"""
import random
import re

from loguru import logger

# Gruppo spintax = graffe con almeno un '|' dentro: {Ciao|Hey|Salve}.
# {nome} non ha pipe -> non matcha -> resta per il fill del nome.
SPINTAX_RE = re.compile(r"\{([^{}|]*(?:\|[^{}|]*)+)\}")

# Placeholder nome accettati (stessa semantica storica di _fallback_message).
NAME_PLACEHOLDER_RE = re.compile(
    r"\{nome\}|\[nome\]|\{name\}|\[name\]", re.IGNORECASE
)

# Residuo sospetto: qualunque {x}/[x] corto rimasto dopo spintax+nome.
RESIDUAL_PLACEHOLDER_RE = re.compile(r"[{\[][^{}\[\]]{0,40}[}\]]")


class TemplateRenderError(Exception):
    """Template non renderizzabile in sicurezza (placeholder sconosciuti)."""


def resolve_spintax(text: str, rng: random.Random | None = None) -> str:
    """Espande ogni gruppo {a|b|c} scegliendo una variante a caso.
    Un solo livello (niente gruppi annidati). Graffe malformate = letterali."""
    r = rng or random
    def _pick(m: re.Match) -> str:
        return r.choice(m.group(1).split("|"))
    return SPINTAX_RE.sub(_pick, text)


def _fill_name(text: str, full_name: str | None, username: str) -> str:
    name = (full_name or "").strip() or f"@{username}"
    return NAME_PLACEHOLDER_RE.sub(name, text)


def _normalize(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(ln.rstrip() for ln in text.split("\n"))
    text = re.sub(r"[ \t]{2,}", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def render_template(
    template: str,
    full_name: str | None,
    username: str,
    rng: random.Random | None = None,
) -> str:
    """Spintax -> nome -> normalizzazione. Solleva TemplateRenderError se
    restano placeholder sconosciuti (es. {azienda}) o se il risultato è
    vuoto: meglio fallire UN messaggio che mandare un DM col placeholder
    letterale (o un DM vuoto)."""
    out = resolve_spintax(template, rng=rng)
    out = _fill_name(out, full_name, username)
    residual = RESIDUAL_PLACEHOLDER_RE.search(out)
    if residual:
        raise TemplateRenderError(
            f"Placeholder sconosciuto nel template: {residual.group(0)!r}"
        )
    if "{" in out or "[" in out:
        # Parentesi orfana/non chiusa che RESIDUAL_PLACEHOLDER_RE non ha
        # intercettato (es. spintax con gruppo mai chiuso): resta letterale nel
        # messaggio per scelta di design (vedi docstring) — solo un warning per
        # non spedire DM rotti in silenzio.
        logger.warning(f"Template con parentesi non chiuse/spintax malformato: {out[:80]!r}")
    if not out.strip():
        raise TemplateRenderError("Template vuoto dopo il rendering")
    return _normalize(out)


def pick_template(campaign, rng: random.Random | None = None) -> tuple[str, str]:
    """Sceglie a caso (pesi uguali) tra i template compilati della campagna.
    Ritorna (testo, variante) con variante in 'a'|'b'|'c'|'d'.
    Unifica i vecchi meccanismi (50/50 random e alternanza generated%2)."""
    r = rng or random
    candidates: list[tuple[str, str]] = [(campaign.base_message_template or "", "a")]
    if (campaign.message_template_b or "").strip():
        candidates.append((campaign.message_template_b, "b"))
    if (campaign.message_template_c or "").strip():
        candidates.append((campaign.message_template_c, "c"))
    if (getattr(campaign, "message_template_d", None) or "").strip():
        candidates.append((campaign.message_template_d, "d"))
    return r.choice(candidates)
