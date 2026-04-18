"""Normalizer for fuzzy artist/album matching."""
import pytest

from backend.services.scan import normalize


@pytest.mark.parametrize("raw,expected", [
    ("The Beatles", "beatles"),
    ("The The", "the"),  # only leading "The " stripped once
    ("Beyoncé", "beyonce"),
    ("Café del Mar", "cafe del mar"),
    ("Abbey Road (Remastered 2019)", "abbey road"),
    ("Abbey Road [Deluxe Edition]", "abbey road"),
    ("Abbey Road  (Deluxe)  (Remastered)", "abbey road"),
    ("  Extra   Spaces  ", "extra spaces"),
    ("AC/DC", "ac/dc"),
    ("Sigur Rós", "sigur ros"),
    ("", ""),
])
def test_normalize_cases(raw, expected):
    assert normalize(raw) == expected


def test_normalize_handles_none():
    assert normalize(None) == ""
