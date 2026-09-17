# SPDX-License-Identifier: AGPL-3.0-or-later
from core.intel.gfw_monitor import _dataset_for, _event_mmsis, _event_request_body


def test_gfw_uses_current_plural_event_datasets():
    assert _dataset_for("encounter") == "public-global-encounters-events:latest"
    assert _dataset_for("loitering") == "public-global-loitering-events:latest"
    assert _dataset_for("gap") == "public-global-gaps-events:latest"


def test_gfw_polygon_request_uses_current_v3_body_contract():
    body = _event_request_body("gap", "2026-09-10", "2026-09-17")
    assert body["datasets"] == ["public-global-gaps-events:latest"]
    assert "types" not in body
    assert body["startDate"] == "2026-09-10"
    assert body["endDate"] == "2026-09-17"
    assert body["geometry"]["type"] == "Polygon"


def test_gfw_extracts_subject_mmsi_from_supported_event_shapes():
    event = {
        "vessel": {"ssvid": "247123456"},
        "vessels": [
            {"ship": {"mmsi": "255987654"}},
            {"ssvid": "247123456"},
        ],
    }
    assert _event_mmsis(event) == ("247123456", "255987654")


def test_gfw_activity_does_not_imply_sanctions_domain():
    from core.intel.gfw_monitor import _classification_for

    assert _classification_for("encounter")[1] == "grey_zone"
    assert _classification_for("gap")[1] == "grey_zone"
    assert _classification_for("loitering")[1] == "grey_zone"
