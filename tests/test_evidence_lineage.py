from core.intel.store import IntelEvent


def _event(source: str, *, event_type: str = "ais_anomaly", metadata: dict | None = None) -> IntelEvent:
    return IntelEvent(
        type=event_type,
        source=source,
        lat=35.9,
        lon=14.5,
        metadata=metadata or {},
    )


def test_aisstream_is_one_ais_sensor_lineage():
    from core.intel.evidence_lineage import lineage_for_event

    lineage = lineage_for_event(_event("AISStream"))

    assert lineage.source_family == "ais"
    assert lineage.sensor_family == "ais"
    assert lineage.independence_group == "ais_sensor_lineage"


def test_internal_mda_and_ais_aliases_share_ais_lineage():
    from core.intel.evidence_lineage import lineage_for_event

    groups = {
        lineage_for_event(_event(source)).independence_group
        for source in ("mda", "ais", "AIS incidents")
    }

    assert groups == {"ais_sensor_lineage"}

def test_gfw_ais_derived_events_do_not_become_independent_from_aisstream():
    from core.intel.evidence_lineage import lineage_for_event

    lineage = lineage_for_event(_event("GFW", event_type="gfw_event"))

    assert lineage.source_family == "ais_derived_event"
    assert lineage.sensor_family == "ais"
    assert lineage.independence_group == "ais_sensor_lineage"


def test_catalogued_non_ais_source_keeps_independent_group():
    from core.intel.evidence_lineage import lineage_for_event

    lineage = lineage_for_event(_event("GDACS", event_type="gdacs"))

    assert lineage.source_family == "official_hazard_alerting"
    assert lineage.sensor_family == "official_report"
    assert lineage.independence_group == "gdacs"


def test_unknown_source_is_not_assumed_independent():
    from core.intel.evidence_lineage import lineage_for_event

    lineage = lineage_for_event(_event("mystery-provider", event_type="unknown_event"))

    assert lineage.source_family == "unknown"
    assert lineage.sensor_family == "unknown"
    assert lineage.independence_group == "unknown"


def test_x_handle_and_ocr_from_same_platform_share_one_independence_group():
    from core.intel.evidence_lineage import lineage_for_event

    text = _event(
        "@alarm_phone", event_type="twitter",
        metadata={"platform": "x", "coordinate_source": "post_text"},
    )
    ocr = _event(
        "@alarm_phone", event_type="twitter",
        metadata={"platform": "x", "coordinate_source": "media_ocr_consensus"},
    )

    assert lineage_for_event(text).independence_group == "x_twitter_platform"
    assert lineage_for_event(ocr).independence_group == "x_twitter_platform"


def test_uncatalogued_rss_news_has_conservative_reporting_lineage():
    from core.intel.evidence_lineage import lineage_for_event

    event = _event("Reuters", event_type="news", metadata={"transport": "rss"})

    lineage = lineage_for_event(event)
    assert lineage.source_family == "secondary_reporting"
    assert lineage.sensor_family == "public_report"
    assert lineage.independence_group == "secondary_news_reporting"


def test_sqlalchemy_event_uses_json_meta_for_lineage():
    from core.db.models import IntelEventDB
    from core.intel.evidence_lineage import lineage_for_event

    row = IntelEventDB(
        id="orm-lineage-event",
        timestamp_utc="2026-09-19T13:00:00+00:00",
        type="twitter",
        severity="info",
        title="Alarm Phone report",
        source="@alarm_phone",
        meta={"platform": "x", "coordinate_source": "post_text"},
    )

    lineage = lineage_for_event(row)
    assert lineage.independence_group == "x_twitter_platform"
    assert lineage.sensor_family == "public_report"
