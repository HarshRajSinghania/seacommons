# SPDX-License-Identifier: AGPL-3.0-or-later
"""IHM NAVAREA III in-force warnings (docs section 9): official sources as
evidence/context integrated into the existing SourceObservation/evidence
architecture, not a parallel "news intelligence" pipeline. Parser tested
against synthetic XML fixtures (no live network access in this
environment) -- field-name mapping should be re-verified against a real
feed pull before relying on it operationally; see the module docstring.
"""
from __future__ import annotations

import pytest


_SAMPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<warnings>
  <warning id="120/26">
    <issueDate>2026-09-18T10:00:00+00:00</issueDate>
    <area>NAVAREA III</area>
    <subject>Firing exercise</subject>
    <text>NAVAL EXERCISE IN PROGRESS 40-15.5N 003-30.2E FROM 181000Z TO 182200Z SEP.</text>
  </warning>
  <warning id="121/26">
    <issueDate>2026-09-19T06:00:00+00:00</issueDate>
    <area>NAVAREA III</area>
    <subject>GNSS interference reported</subject>
    <text>GPS JAMMING REPORTED IN AREA BOUNDED BY 36-00.0N 005-30.0W AND 36-20.0N 005-10.0W.</text>
  </warning>
  <warning id="122/26">
    <issueDate>2026-09-19T08:00:00+00:00</issueDate>
    <area>NAVAREA III</area>
    <subject>Wreck removal</subject>
    <text>WRECK REMOVAL OPERATIONS 38-40.0N 000-10.0E. VESSELS REQUESTED KEEP CLEAR 500M.</text>
  </warning>
</warnings>"""

_MALFORMED_ENTRY_XML = """<?xml version="1.0" encoding="UTF-8"?>
<warnings>
  <warning>
    <subject>No id, no text -- should be skipped</subject>
  </warning>
  <warning id="200/26">
    <issueDate>2026-09-20T00:00:00+00:00</issueDate>
    <subject>Navigation caution</subject>
    <text>ROUTINE NAVIGATIONAL CAUTION, NO SPECIFIC POSITION.</text>
  </warning>
</warnings>"""


def test_parses_multiple_warnings_with_categories_and_coordinates():
    from core.mda.navarea3 import parse_navarea3_xml

    warnings = parse_navarea3_xml(_SAMPLE_XML)
    assert len(warnings) == 3
    by_id = {w.warning_id: w for w in warnings}

    firing = by_id["120/26"]
    assert firing.category == "firing_exercise"
    assert firing.lat == pytest.approx(40.2583, abs=1e-3)
    assert firing.lon == pytest.approx(3.5033, abs=1e-3)
    assert firing.area == "NAVAREA III"

    gnss = by_id["121/26"]
    assert gnss.category == "gnss_interference"
    assert len(gnss.coordinates) == 2

    other = by_id["122/26"]
    assert other.category == "navigational_warning"


def test_skips_warning_missing_id_or_text_but_keeps_the_rest():
    from core.mda.navarea3 import parse_navarea3_xml

    warnings = parse_navarea3_xml(_MALFORMED_ENTRY_XML)
    assert len(warnings) == 1
    assert warnings[0].warning_id == "200/26"


def test_malformed_top_level_xml_raises_for_the_caller_to_catch():
    from core.mda.navarea3 import parse_navarea3_xml

    with pytest.raises(Exception):
        parse_navarea3_xml("<not><valid xml")


def test_billion_laughs_payload_is_rejected_not_expanded():
    from core.mda.navarea3 import parse_navarea3_xml

    bomb = """<?xml version="1.0"?>
<!DOCTYPE warnings [
  <!ENTITY lol "lol">
  <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
]>
<warnings><warning id="1"><text>&lol2;</text></warning></warnings>"""
    with pytest.raises(Exception):
        parse_navarea3_xml(bomb)


def test_poll_navarea3_is_graceful_on_network_outage(monkeypatch):
    from core.mda import navarea3

    def _raise(*_args, **_kwargs):
        raise ConnectionError("no route to host")

    class _FakeHttpx:
        @staticmethod
        def get(*args, **kwargs):
            raise ConnectionError("no route to host")

    monkeypatch.setitem(__import__("sys").modules, "httpx", _FakeHttpx)
    assert navarea3.poll_navarea3() == 0


def test_poll_navarea3_ingests_and_persists_observations(monkeypatch):
    from core.mda import navarea3
    from core.intel.store import intel_store

    class _FakeResponse:
        text = _SAMPLE_XML

        def raise_for_status(self):
            return None

    class _FakeHttpx:
        @staticmethod
        def get(*args, **kwargs):
            return _FakeResponse()

    monkeypatch.setitem(__import__("sys").modules, "httpx", _FakeHttpx)

    ingested = navarea3.poll_navarea3()
    assert ingested == 3

    event = intel_store.get_durable("navarea3:120/26")
    assert event is not None
    assert event.type == "navwarning"
    assert event.source == navarea3.NAVAREA3_SOURCE
    assert event.metadata["warning_category"] == "firing_exercise"
    assert event.metadata["publication_status"] == "internal"


def test_official_warning_never_becomes_a_public_case_by_itself(monkeypatch):
    """docs section 9: official warnings contextualize a case but must never
    automatically become a vessel accusation -- ingested internal only."""
    from core.mda import navarea3
    from core.intel.store import intel_store

    class _FakeResponse:
        text = _SAMPLE_XML

        def raise_for_status(self):
            return None

    class _FakeHttpx:
        @staticmethod
        def get(*args, **kwargs):
            return _FakeResponse()

    monkeypatch.setitem(__import__("sys").modules, "httpx", _FakeHttpx)
    navarea3.poll_navarea3()

    for warning_id in ("120/26", "121/26", "122/26"):
        event = intel_store.get_durable(f"navarea3:{warning_id}")
        assert event is not None
        assert event.metadata["publication_status"] == "internal"
        assert event.metadata["is_distress"] is False
