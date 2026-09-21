import json
from types import SimpleNamespace


def _row(kind, payload, **extra):
    base = {
        "observation_id": "obs-1", "observation_type": kind,
        "observed_at": "2026-09-07T16:00:00+00:00", "lat": 35.9, "lon": 14.5,
        "provenance": {"frequency_hz": 2187500, "receiver_id": "secret-rx",
                       "physical_lineage": "secret-lineage",
                       "structured_payload": json.dumps(payload)},
    }
    base.update(extra)
    return SimpleNamespace(**base)


def test_public_dsc_message_keeps_structure_and_hides_receiver_identity():
    from core.radio.public_messages import project_public_radio_message
    row = _row("dsc_message", {
        "category": "distress", "mmsi": "123456789", "nature_code": "grounding",
        "nature_description": "Grounding", "format": "distress",
        "from_mmsi": "123456789", "to_mmsi": "987654321",
        "telecommand_1": "J3E", "telecommand_2": None,
        "end_of_sequence": "EOS", "distress_time": "1600",
        "reported_frequency": "2187.5 kHz",
    })
    public = project_public_radio_message(row)
    assert public["kind"] == "dsc"
    assert public["mmsi"] == "123456789"
    assert public["category"] == "distress"
    assert public["nature_description"] == "Grounding"
    assert public["format"] == "distress"
    assert public["from_mmsi"] == "123456789"
    assert public["to_mmsi"] == "987654321"
    assert public["reported_frequency"] == "2187.5 kHz"
    assert "receiver_id" not in public and "lineage" not in str(public)


def test_public_navtex_message_is_context_only_and_bounded():
    from core.radio.public_messages import project_public_radio_message
    text = "ZCZC KA01\n" + ("WARNING " * 200) + "\nNNNN"
    row = _row("navtex_message", {"station_id": "K", "subject_id": "A", "message_id": "01", "area": "central med", "text": text}, lat=None, lon=None)
    public = project_public_radio_message(row)
    assert public["kind"] == "navtex"
    assert public["station_id"] == "K"
    assert len(public["text"]) <= 1200
    assert public["latitude"] is None and public["longitude"] is None
