"""Tests for the weather.Vocab event-facet handlers (NL term -> GHCN element)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from noaa_weather.handlers.vocab import vocab_handlers as vh


def test_element_set_matches_parser():
    from noaa_weather.tools._noaa_tools.ghcn_parse import _ELEMENT_SET
    assert set(vh._ELEMENTS) == set(_ELEMENT_SET)


@pytest.mark.parametrize("term,code", [
    ("max temperature", "TMAX"),
    ("daily high", "TMAX"),
    ("minimum temperature", "TMIN"),
    ("rainfall", "PRCP"),
    ("precipitation", "PRCP"),
    ("snowfall", "SNOW"),
    ("snow depth", "SNWD"),
    ("TMAX", "TMAX"),
])
def test_resolve_known_terms(term, code):
    out = vh.handle_resolve_element({"term": term})["result"]
    assert out["element"] == code
    assert out["confidence"] > 0.0
    assert out["description"]


def test_resolve_unknown_term_is_empty_not_error():
    out = vh.handle_resolve_element({"term": "humidity"})["result"]
    assert out["element"] == "" and out["confidence"] == 0.0


def test_resolve_requires_term():
    with pytest.raises(ValueError):
        vh.handle_resolve_element({"term": "  "})


def test_list_elements():
    out = vh.handle_list_elements({})
    elements = json.loads(out["elements"])
    assert out["count"] == len(elements) == 5
    assert {e["element"] for e in elements} == {"TMAX", "TMIN", "PRCP", "SNOW", "SNWD"}
    assert all(e["description"] for e in elements)


def test_dispatch_and_registration():
    assert set(vh._DISPATCH) == {"weather.Vocab.ResolveElement", "weather.Vocab.ListElements"}
    runner = MagicMock()
    vh.register_handlers(runner)
    assert runner.register_handler.call_count == 2
    # RegistryRunner entrypoint routes by _facet_name
    res = vh.handle({"_facet_name": "weather.Vocab.ResolveElement", "term": "rainfall"})
    assert res["result"]["element"] == "PRCP"
