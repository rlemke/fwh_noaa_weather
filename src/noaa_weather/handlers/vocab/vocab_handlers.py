"""Event-facet handlers for the ``weather.Vocab`` namespace.

Wires ResolveElement / ListElements (weather.ffl) to the GHCN-Daily element
catalogue — the deterministic NL->element step that lets a composer turn "max
temperature" into TMAX without memorising the GHCN element codes. Pure
in-process lookups (no network, no I/O). The supported set mirrors the parser's
``_noaa_tools.ghcn_parse._ELEMENT_SET``.

``Json``-typed returns are emitted as JSON strings, matching the fleet
convention (e.g. osm.Vocab / census.Vocab).
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

log = logging.getLogger(__name__)

NAMESPACE = "weather.Vocab"

# GHCN-Daily element code -> human description + NL synonym phrases. The set
# mirrors ghcn_parse._ELEMENT_SET; descriptions/synonyms are the vocabulary.
_ELEMENTS: dict[str, dict[str, Any]] = {
    "TMAX": {"description": "Daily maximum temperature",
             "synonyms": ["max temperature", "maximum temperature", "high temperature",
                          "highs", "daily high", "hottest", "heat"]},
    "TMIN": {"description": "Daily minimum temperature",
             "synonyms": ["min temperature", "minimum temperature", "low temperature",
                          "lows", "daily low", "coldest", "cold"]},
    "PRCP": {"description": "Daily total precipitation",
             "synonyms": ["precipitation", "precip", "rain", "rainfall", "wet"]},
    "SNOW": {"description": "Daily snowfall",
             "synonyms": ["snow", "snowfall", "fresh snow"]},
    "SNWD": {"description": "Snow depth on the ground",
             "synonyms": ["snow depth", "snowpack", "snow on ground", "snow cover"]},
}

_WORD = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def _score(term_tokens: set[str], candidate: str) -> float:
    cand = _tokens(candidate)
    if not cand or not term_tokens:
        return 0.0
    overlap = term_tokens & cand
    if not overlap:
        return 0.0
    if term_tokens == cand:
        return 1.0
    return min(0.95, 0.5 + 0.45 * (len(overlap) / len(term_tokens)))


def _resolve(term: str) -> tuple[str, float, str]:
    """Best GHCN element for an NL term: (code, confidence, description)."""
    term_tokens = _tokens(term)
    best_code, best_conf = "", 0.0
    for code, meta in _ELEMENTS.items():
        candidates = [code, meta["description"], *meta["synonyms"]]
        conf = max((_score(term_tokens, c) for c in candidates), default=0.0)
        if conf > best_conf:
            best_code, best_conf = code, conf
    if not best_code:
        return "", 0.0, ""
    return best_code, round(best_conf, 3), _ELEMENTS[best_code]["description"]


def handle_resolve_element(params: dict[str, Any]) -> dict[str, Any]:
    """Resolve a natural-language term to its GHCN-Daily element code."""
    term = (params.get("term") or "").strip()
    step_log = params.get("_step_log")
    if not term:
        raise ValueError("ResolveElement: term is required")
    code, conf, desc = _resolve(term)
    if step_log:
        if code:
            step_log(f"ResolveElement: {term!r} -> {code} ({desc}, conf {conf:.2f})", level="success")
        else:
            step_log(f"ResolveElement: {term!r} -> no known GHCN element", level="warning")
    return {"result": {"element": code, "confidence": conf, "description": desc}}


def handle_list_elements(params: dict[str, Any]) -> dict[str, Any]:
    """List every GHCN element code the vocabulary covers."""
    step_log = params.get("_step_log")
    elements = [{"element": code, "description": meta["description"]}
                for code, meta in _ELEMENTS.items()]
    if step_log:
        step_log(f"ListElements: {len(elements)} GHCN elements", level="success")
    return {"elements": json.dumps(elements), "count": len(elements)}


_DISPATCH: dict[str, Any] = {
    f"{NAMESPACE}.ResolveElement": handle_resolve_element,
    f"{NAMESPACE}.ListElements": handle_list_elements,
}


def handle(payload: dict[str, Any]) -> dict[str, Any]:
    """RegistryRunner entrypoint."""
    facet = payload["_facet_name"]
    handler = _DISPATCH.get(facet)
    if handler is None:
        raise ValueError(f"Unknown facet: {facet}")
    return handler(payload)


def register_handlers(runner) -> None:
    """Register with a RegistryRunner."""
    for facet_name in _DISPATCH:
        runner.register_handler(
            facet_name=facet_name,
            module_uri=f"file://{os.path.abspath(__file__)}",
            entrypoint="handle",
        )


def register_vocab_handlers(poller) -> None:
    """Register with an AgentPoller."""
    for facet_name, handler in _DISPATCH.items():
        poller.register(facet_name, handler)
        log.debug("Registered vocab handler: %s", facet_name)
