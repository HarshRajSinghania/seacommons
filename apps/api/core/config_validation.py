# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cross-field configuration validation.

`core.security.validate_production_security` checks that a production profile
has the mandatory building blocks. This module checks the orthogonal problem:
combinations of settings that are impossible or unsafe regardless of profile,
and combinations that are merely incomplete (a warning, not a failure).

It deliberately lives outside `core.config` so the settings class stays a
plain data holder rather than growing into a validation-and-domain god object.

Backwards compatibility: every check here fires only on a combination that is
already broken at runtime. No previously-working environment starts failing.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from core.config import SuezCanalConfig, config

_VALID_JOB_MODES = {"inline", "queue"}


@dataclass
class ConfigReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _is_production(cfg: SuezCanalConfig) -> bool:
    return cfg.RUNTIME_PROFILE.lower() in {"production", "prod"}


def check_configuration(cfg: SuezCanalConfig | None = None) -> ConfigReport:
    """Return impossible/unsafe combinations as errors, incomplete ones as warnings."""
    cfg = cfg or config
    report = ConfigReport()
    production = _is_production(cfg)

    # ── Impossible / unsafe ──────────────────────────────────────────────────
    if cfg.JOB_EXECUTION_MODE not in _VALID_JOB_MODES:
        report.errors.append(
            f"JOB_EXECUTION_MODE={cfg.JOB_EXECUTION_MODE!r} is not one of "
            f"{sorted(_VALID_JOB_MODES)}"
        )

    if production and cfg.DEMO_PUBLIC_MODE:
        report.errors.append(
            "DEMO_PUBLIC_MODE=true with RUNTIME_PROFILE=production: the public "
            "demo isolation (blocks operational mutations) cannot run on a "
            "production node"
        )

    if cfg.DRIFT_WORKER_URL and not cfg.DRIFT_WORKER_SECRET:
        report.errors.append(
            "DRIFT_WORKER_URL is set without DRIFT_WORKER_SECRET: the compute "
            "offload endpoint would be called unauthenticated"
        )

    if cfg.CORRELATION_CONFIDENCE_ALERT >= cfg.CORRELATION_CONFIDENCE_URGENT:
        report.errors.append(
            f"CORRELATION_CONFIDENCE_ALERT ({cfg.CORRELATION_CONFIDENCE_ALERT}) "
            f"must be below CORRELATION_CONFIDENCE_URGENT "
            f"({cfg.CORRELATION_CONFIDENCE_URGENT})"
        )

    # ── Incomplete but not fatal ─────────────────────────────────────────────
    if production and cfg.MOCK:
        report.warnings.append(
            "MOCK=true is set on a production profile; the operational runtime "
            "ignores it, but it usually signals a copied demo .env"
        )

    if cfg.TWIKIT_ENABLED and not cfg.TWIKIT_COOKIES_FILE:
        report.warnings.append(
            "TWIKIT_ENABLED=true without TWIKIT_COOKIES_FILE: the twikit "
            "monitor stays disabled until a cookies file is configured"
        )
    elif cfg.TWIKIT_ENABLED and not os.path.isfile(cfg.TWIKIT_COOKIES_FILE):
        report.warnings.append(
            f"TWIKIT_COOKIES_FILE={cfg.TWIKIT_COOKIES_FILE!r} does not exist; "
            "the twikit monitor stays disabled"
        )

    if cfg.TWIKIT_ALERTS_ENABLED and not (
        cfg.TELEGRAM_BOT_TOKEN and cfg.TELEGRAM_OPERATIONS_CHAT_ID
    ):
        report.warnings.append(
            "TWIKIT_ALERTS_ENABLED=true but TELEGRAM_BOT_TOKEN / "
            "TELEGRAM_OPERATIONS_CHAT_ID is missing: tracked-account alerts "
            "cannot be delivered"
        )

    if cfg.TWIKIT_PRIORITY_POLL_INTERVAL_S > cfg.TWIKIT_POLL_INTERVAL_S:
        report.warnings.append(
            "TWIKIT_PRIORITY_POLL_INTERVAL_S is larger than "
            "TWIKIT_POLL_INTERVAL_S: priority accounts would be polled less "
            "often than the base tier"
        )

    if cfg.TELEGRAM_OPERATIONS_CHAT_ID and not cfg.TELEGRAM_BOT_TOKEN:
        report.warnings.append(
            "TELEGRAM_OPERATIONS_CHAT_ID is set without TELEGRAM_BOT_TOKEN: "
            "operator Telegram commands and notifications are inert"
        )

    meta_fields = {
        "META_APP_ID": cfg.META_APP_ID,
        "META_APP_SECRET": cfg.META_APP_SECRET,
        "META_WEBHOOK_VERIFY_TOKEN": cfg.META_WEBHOOK_VERIFY_TOKEN,
    }
    set_meta = [k for k, v in meta_fields.items() if v]
    if set_meta and len(set_meta) != len(meta_fields):
        missing = sorted(k for k, v in meta_fields.items() if not v)
        report.warnings.append(
            "Partial Meta WhatsApp configuration: missing "
            f"{', '.join(missing)}; the webhook will fail closed"
        )

    return report


def validate_configuration(cfg: SuezCanalConfig | None = None) -> list[str]:
    """Raise on impossible/unsafe combinations; return the warning list."""
    report = check_configuration(cfg)
    if report.errors:
        raise RuntimeError(
            "Invalid configuration combination(s): " + "; ".join(report.errors)
        )
    return report.warnings


if __name__ == "__main__":  # pragma: no cover - pre-deploy check helper
    import sys

    _report = check_configuration()
    for _w in _report.warnings:
        print(f"WARNING: {_w}")
    for _e in _report.errors:
        print(f"ERROR: {_e}")
    print("configuration OK" if _report.ok else "configuration INVALID")
    sys.exit(0 if _report.ok else 1)
