# SPDX-License-Identifier: AGPL-3.0-or-later
"""IHM NAVAREA III official-feed parsing and persistence contracts.

Fixtures include the real NAVAREASVIGOR field schema verified against IHM's
public navareas_crudo.xml feed on 2026-09-20.
"""
from __future__ import annotations

from xml.etree.ElementTree import ParseError

import pytest
from defusedxml.common import DefusedXmlException

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

    with pytest.raises(ParseError):
        parse_navarea3_xml("<not><valid xml")


def test_billion_laughs_payload_is_rejected_not_expanded():
    from core.mda.navarea3 import parse_navarea3_xml

    bomb = """<?xml version="1.0"?>
<!DOCTYPE warnings [
  <!ENTITY lol "lol">
  <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
]>
<warnings><warning id="1"><text>&lol2;</text></warning></warnings>"""
    with pytest.raises(DefusedXmlException):
        parse_navarea3_xml(bomb)


def test_poll_navarea3_is_graceful_on_network_outage(monkeypatch):
    from core.mda import navarea3

    class _FailingClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def request(self, *_args, **_kwargs):
            raise ConnectionError("no route to host")

    monkeypatch.setattr("core.net.outbound.FixedOriginClient", _FailingClient)
    assert navarea3.poll_navarea3() == 0


def test_poll_navarea3_ingests_and_persists_observations(monkeypatch):
    from core.intel.store import intel_store
    from core.mda import navarea3

    class _FakeResponse:
        body = _SAMPLE_XML.encode("utf-8")

        def raise_for_status(self):
            return None

    class _FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def request(self, *_args, **_kwargs):
            return _FakeResponse()

    monkeypatch.setattr("core.net.outbound.FixedOriginClient", _FakeClient)

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
    from core.intel.store import intel_store
    from core.mda import navarea3

    class _FakeResponse:
        body = _SAMPLE_XML.encode("utf-8")

        def raise_for_status(self):
            return None

    class _FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def request(self, *_args, **_kwargs):
            return _FakeResponse()

    monkeypatch.setattr("core.net.outbound.FixedOriginClient", _FakeClient)
    navarea3.poll_navarea3()

    for warning_id in ("120/26", "121/26", "122/26"):
        event = intel_store.get_durable(f"navarea3:{warning_id}")
        assert event is not None
        assert event.metadata["publication_status"] == "internal"
        assert event.metadata["is_distress"] is False


_REAL_IHM_XML = """<?xml version="1.0" encoding="UTF-8"?>
<dataroot generated="2026-09-17T11:27:33">
  <NAVAREASVIGOR>
    <nnuna>220092</nnuna>
    <nnunaf>0092/22</nnunaf>
    <nfemi>2022-02-25T00:00:00</nfemi>
    <nlocae>MAR NEGRO NOROCCIDENTAL</nlocae>
    <nlocai>BLACK SEA NORTHWESTERN PART</nlocai>
    <ntees>ZONA DE MINAS 46-04.0N 033-12.8E</ntees>
    <ntein>DUE TO MINE DANGER 46-04.0N 033-12.8E 46-31.7 N 030-46.6E</ntein>
    <nasun>ZONA DE MINAS</nasun>
    <nasuni>MINES AREA</nasuni>
  </NAVAREASVIGOR>
</dataroot>"""


def test_real_ihm_navareasvigor_schema_is_parsed():
    from core.mda.navarea3 import DEFAULT_NAVAREA3_URL, parse_navarea3_xml

    warnings = parse_navarea3_xml(_REAL_IHM_XML)
    assert DEFAULT_NAVAREA3_URL.endswith("/ihm/XML/navareas_crudo.xml")
    assert len(warnings) == 1
    warning = warnings[0]
    assert warning.warning_id == "0092/22"
    assert warning.source_record_id == "220092"
    assert warning.issued_at == "2022-02-25T00:00:00"
    assert warning.area == "BLACK SEA NORTHWESTERN PART"
    assert warning.subject == "MINES AREA"
    assert warning.text.startswith("DUE TO MINE DANGER")
    assert warning.snapshot_generated == "2026-09-17T11:27:33"
    assert warning.lat == pytest.approx(46.0667, abs=1e-3)
    assert warning.lon == pytest.approx(33.2133, abs=1e-3)
    assert len(warning.coordinates) == 2


def test_navarea_source_revisions_are_immutable_and_distinct():
    from core.db.models import SourceObservationDB
    from core.db.session import session_scope
    from core.mda.navarea3 import Navarea3Warning, _record_source_observation

    base = {
        "warning_id": "0092/22", "source_record_id": "220092",
        "issued_at": "2022-02-25T00:00:00", "area": "BLACK SEA NORTHWESTERN PART",
        "subject": "MINES AREA", "category": "strike_warning",
        "lat": 46.0667, "lon": 33.2133, "coordinates": ((46.0667, 33.2133),),
    }
    prefix = "navarea3:0092/22:%"
    with session_scope() as db:
        db.query(SourceObservationDB).filter(
            SourceObservationDB.source_name == "IHM NAVAREA III",
            SourceObservationDB.source_id.like(prefix),
        ).delete(synchronize_session=False)

    _record_source_observation(Navarea3Warning(
        **base, text="DUE TO MINE DANGER", snapshot_generated="2026-09-17T11:27:33",
    ))
    _record_source_observation(Navarea3Warning(
        **base, text="DUE TO MINE DANGER - UPDATED", snapshot_generated="2026-09-20T10:00:00",
    ))

    with session_scope() as db:
        rows = db.query(SourceObservationDB).filter(
            SourceObservationDB.source_name == "IHM NAVAREA III",
            SourceObservationDB.source_id.like(prefix),
        ).all()
        assert len(rows) == 2
        assert len({row.source_id for row in rows}) == 2
        assert {row.provenance["snapshot_generated"] for row in rows} == {
            "2026-09-17T11:27:33", "2026-09-20T10:00:00",
        }


def test_navwarning_deterministic_id_refreshes_in_memory_case():
    from core.intel.store import IntelEvent, IntelStore

    store = IntelStore(maxlen=10)
    store._persist = lambda _event: None
    first = IntelEvent(
        id="navarea3:0092/22", type="navwarning", title="Old title",
        text="Old text", source="IHM NAVAREA III",
        metadata={"publication_status": "internal"},
    )
    updated = IntelEvent(
        id="navarea3:0092/22", type="navwarning", title="Updated title",
        text="Updated text", source="IHM NAVAREA III",
        metadata={"publication_status": "internal", "snapshot_generated": "later"},
    )
    assert store.add(first, dedup_key=first.id) is True
    assert store.add(updated, dedup_key=updated.id) is False
    current = store.get(first.id)
    assert current is not None
    assert current.title == "Updated title"
    assert current.text == "Updated text"
    assert current.metadata["snapshot_generated"] == "later"
    assert len(store.events()) == 1
