# SPDX-License-Identifier: AGPL-3.0-or-later
"""docs/fixes.md sec 2 / sec 7 -- translated / near-duplicate incident folding.

Alarm Phone posts the same distress alert in English and French minutes
apart, and a text-only alert followed by one carrying the map screenshot.
The content hash differs every time, so both used to become separate markers.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.intel.translation_dedup import (
    find_translation_twin,
    incident_signature,
    signatures_match,
)


class _Event:
    def __init__(self, _id, text, ts, *, distress=True, handle="alarm_phone", title=""):
        self.id = _id
        self.text = text
        self.title = title
        self.timestamp_utc = ts
        self.metadata = {"tracked_account": handle, "is_distress": distress}


NOW = datetime(2026, 8, 24, 6, 30, tzinfo=timezone.utc)


# ── the real August 2026 pairs ───────────────────────────────────────────────
_REAL_PAIRS = [
    (
        "🆘 We alerted the authorities to 127 people who left Banjul, #Gambia, on August 7.",
        "🆘 Nous avons alerté les autorités à propos de 127 personnes parties de Banjul, #Gambie le 7 août.",
    ),
    (
        "🆘 15 people missing on their way to #Spain! left #Cherchell, #Algeria on August 13.",
        "🆘 15 personnes disparues sur la route d’#Espagne! parties de #Cherchel, #Algerie le 13 aout.",
    ),
    (
        "🆘 We were alerted to 26 people who left Ain Taya, #Algeria, 3 days ago.",
        "🆘 Nous avons été informés que 26 personnes avaient quitté AinTaya #Algérie il y a 3 jours.",
    ),
    (
        "⚫ #Shipwreck in the #WesternMed. a boat with 9 people left Oran, #Algeria, on August 10.",
        "#Naufrage en #MéditerranéeOccidentale. un bateau transportant 9 personnes avait quitté Oran, #Algérie.",
    ),
]


def test_real_translated_pairs_match():
    for english, french in _REAL_PAIRS:
        assert signatures_match(
            incident_signature(english), incident_signature(french)
        ), (english, french)


def test_same_language_text_then_map_version_matches():
    a = "🆘 from ~30 people in distress in the #Aegean, #Greece close to #Farmakonisi"
    b = "🆘 from ~30 people in distress in the #Aegean, #Greece close to #Farmakonisi, among them 4 children"
    assert signatures_match(incident_signature(a), incident_signature(b))


def test_different_headcount_never_matches():
    a = incident_signature("🆘 30 people in distress near #Farmakonisi")
    b = incident_signature("🆘 12 people in distress near #Farmakonisi")
    assert not signatures_match(a, b)


def test_same_count_different_place_does_not_match():
    a = incident_signature("🆘 15 people missing near #Oran, #Algeria")
    b = incident_signature("🆘 15 people rescued near #Sfax, #Tunisia")
    assert not signatures_match(a, b)


def test_advocacy_post_has_no_usable_signature():
    sig = incident_signature("SOS Mediterranee published its annual report today")
    assert not sig.is_usable


def test_find_twin_returns_earliest_match_within_window():
    english = _Event(
        "ap-en",
        "🆘 We alerted the authorities to 127 people who left Banjul, #Gambia.",
        (NOW - timedelta(minutes=3)).isoformat(),
    )
    twin = find_translation_twin(
        "🆘 Nous avons alerté les autorités à propos de 127 personnes parties de Banjul, #Gambie.",
        handle="alarm_phone",
        distress=True,
        now=NOW,
        candidates=[english],
    )
    assert twin is english


def test_find_twin_ignores_matches_outside_the_time_window():
    old = _Event(
        "ap-old",
        "🆘 127 people who left Banjul, #Gambia.",
        (NOW - timedelta(hours=6)).isoformat(),
    )
    assert (
        find_translation_twin(
            "🆘 127 personnes parties de Banjul, #Gambie.",
            handle="alarm_phone",
            distress=True,
            now=NOW,
            candidates=[old],
        )
        is None
    )


def test_find_twin_requires_same_distress_class():
    news = _Event(
        "ap-news",
        "127 people who left Banjul, #Gambia -- a retrospective report.",
        (NOW - timedelta(minutes=2)).isoformat(),
        distress=False,
    )
    assert (
        find_translation_twin(
            "🆘 127 personnes parties de Banjul, #Gambie, en détresse maintenant.",
            handle="alarm_phone",
            distress=True,
            now=NOW,
            candidates=[news],
        )
        is None
    )


def test_westernmed_algiers_alger_translation_pair_matches():
    english = (
        "🆘 9 people in distress in the #WesternMed. We were alerted by worried relatives "
        "to the group who left from Algiers, #Algeria, 2 days ago."
    )
    french = (
        "🆘 9 personnes en détresse en mer. Nous avons été alertés par des proches inquiets "
        "d’être sans nouvelles du bateau parti il y a 2 jours d’Alger #Algérie."
    )
    assert signatures_match(incident_signature(english), incident_signature(french))
