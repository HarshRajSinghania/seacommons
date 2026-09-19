# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import pytest
from core.config import SuezCanalConfig
from core.config_validation import check_configuration, validate_configuration


def _cfg(**overrides) -> SuezCanalConfig:
    return SuezCanalConfig(_env_file=None, **overrides)


def test_opendrift_prewarm_defaults_to_enabled(monkeypatch):
    monkeypatch.delenv("OPENDRIFT_PREWARM_ENABLED", raising=False)

    settings = SuezCanalConfig(_env_file=None)

    assert settings.OPENDRIFT_PREWARM_ENABLED is True


def test_opendrift_prewarm_can_be_disabled(monkeypatch):
    monkeypatch.setenv("OPENDRIFT_PREWARM_ENABLED", "false")

    settings = SuezCanalConfig(_env_file=None)

    assert settings.OPENDRIFT_PREWARM_ENABLED is False


def test_default_configuration_has_no_errors_or_warnings():
    report = check_configuration(_cfg())

    assert report.errors == []
    assert report.warnings == []


def test_invalid_job_execution_mode_is_an_error():
    with pytest.raises(RuntimeError, match="JOB_EXECUTION_MODE"):
        validate_configuration(_cfg(JOB_EXECUTION_MODE="async"))


def test_demo_public_mode_on_production_is_an_error():
    with pytest.raises(RuntimeError, match="DEMO_PUBLIC_MODE"):
        validate_configuration(_cfg(RUNTIME_PROFILE="production", DEMO_PUBLIC_MODE=True))


def test_reused_aisstream_key_for_ngo_subscription_is_valid():
    assert check_configuration(
        _cfg(AISSTREAM_KEY="k1", AISSTREAM_NGO_KEY="k1")
    ).ok
    assert check_configuration(
        _cfg(AISSTREAM_KEY="k1", AISSTREAM_NGO_KEY="k2")
    ).ok


def test_drift_worker_url_without_secret_is_an_error():
    with pytest.raises(RuntimeError, match="DRIFT_WORKER_SECRET"):
        validate_configuration(_cfg(DRIFT_WORKER_URL="https://drift.internal"))


def test_inverted_correlation_thresholds_are_an_error():
    with pytest.raises(RuntimeError, match="CORRELATION_CONFIDENCE"):
        validate_configuration(
            _cfg(CORRELATION_CONFIDENCE_ALERT=0.9, CORRELATION_CONFIDENCE_URGENT=0.8)
        )


def test_twikit_enabled_without_cookies_is_a_warning_not_an_error():
    report = check_configuration(_cfg(TWIKIT_ENABLED=True))

    assert report.ok
    assert any("TWIKIT_COOKIES_FILE" in w for w in report.warnings)


def test_partial_meta_configuration_warns():
    report = check_configuration(_cfg(META_APP_ID="id", META_APP_SECRET="secret"))

    assert report.ok
    assert any("Meta WhatsApp" in w for w in report.warnings)


def test_remote_radio_defaults_fail_closed():
    settings = _cfg()

    assert settings.REMOTE_RADIO_ENABLED is False
    assert settings.REMOTE_RADIO_MAX_RECEIVERS > 0
    assert settings.REMOTE_RADIO_CONNECT_TIMEOUT_S > 0
    assert settings.REMOTE_RADIO_FAILOVER_ENABLED is False
    assert settings.REMOTE_RADIO_CHANNEL_REPLICAS == 3
    assert settings.REMOTE_RADIO_FAILOVER_STALE_S == 30
    assert settings.REMOTE_RADIO_FAILOVER_RETRY_S == 120
    assert settings.REMOTE_RADIO_RECEIVERS_JSON == ""
    assert settings.REMOTE_RADIO_RECEIVERS_FILE == ""
