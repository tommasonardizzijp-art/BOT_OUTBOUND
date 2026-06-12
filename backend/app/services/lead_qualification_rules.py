from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any


DEFAULT_FIELD_WEIGHTS = {
    "username": 8,
    "full_name": 12,
    "biography": 30,
    "external_url": 15,
    "bio_links": 15,
    "contact_fields": 5,
    "scrape_source": 5,
}

# Filosofia: 1 keyword di nicchia corretta = match diretto (recall sopra
# precisione, l'AI filtra solo i deboli). Con pass_threshold=10, positive_term_bonus
# deve essere >=10 cosi' una sola keyword positiva supera la soglia su qualunque campo.
DEFAULT_SCORE_RULES = {
    "strong_term_bonus": 18,
    "positive_term_bonus": 10,
    "negative_term_penalty": 25,
    "external_url_bonus": 8,
    "contact_available_bonus": 4,
}

LIST_FIELDS = (
    "language_hints",
    "positive_terms",
    "strong_terms",
    "negative_terms",
    "positive_concepts",
    "negative_concepts",
)


def safe_json_loads(raw: Any, default: Any):
    if raw is None or raw == "":
        return deepcopy(default)
    if isinstance(raw, (dict, list)):
        return deepcopy(raw)
    try:
        return json.loads(raw)
    except Exception:
        return deepcopy(default)


def safe_json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _normalize_string(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _normalize_list(values: Any) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list):
        values = [values]
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        item = _normalize_string(value)
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return sorted(out)


def _normalize_int_map(raw: Any, defaults: dict[str, int]) -> dict[str, int]:
    out = dict(defaults)
    if isinstance(raw, dict):
        for key, value in raw.items():
            try:
                out[str(key)] = int(value)
            except (TypeError, ValueError):
                continue
    return out


def normalize_compiled_rules(rules: dict[str, Any] | None) -> dict[str, Any]:
    raw = dict(rules or {})
    normalized: dict[str, Any] = {
        "target_label": _normalize_string(raw.get("target_label") or "custom_target") or "custom_target",
        "language_hints": _normalize_list(raw.get("language_hints") or ["it", "en"]),
        "field_weights": _normalize_int_map(raw.get("field_weights"), DEFAULT_FIELD_WEIGHTS),
        "score_rules": _normalize_int_map(raw.get("score_rules"), DEFAULT_SCORE_RULES),
    }
    for field in LIST_FIELDS:
        if field == "language_hints":
            continue
        normalized[field] = _normalize_list(raw.get(field))
    return normalized


def validate_compiled_rules(rules: dict[str, Any] | None) -> dict[str, Any]:
    normalized = normalize_compiled_rules(rules)
    if not (
        normalized["positive_terms"]
        or normalized["strong_terms"]
        or normalized["negative_terms"]
        or normalized["positive_concepts"]
        or normalized["negative_concepts"]
    ):
        raise ValueError("compiled_rules deve contenere almeno un criterio")
    return normalized


def rules_hash(rules: dict[str, Any]) -> str:
    normalized = normalize_compiled_rules(rules)
    payload = json.dumps(normalized, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
