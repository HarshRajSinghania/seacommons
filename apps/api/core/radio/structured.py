# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from core.radio.provider import _normalize_identifier

_DSC_CATEGORIES = frozenset({"distress", "urgency", "safety", "routine", "unknown"})
_MAX_NAVTEX_TEXT_CHARS = 8192
_MAX_FREE_TEXT_CHARS = 256


def _required_text(value: str, *, field: str, max_chars: int = _MAX_FREE_TEXT_CHARS) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} must not be empty")
    return text[:max_chars]


def _validate_common(
    *,
    receiver_id: str,
    physical_lineage: str,
    observed_at: datetime,
    frequency_hz: int,
    raw_evidence_ref: str,
    decoder_message_id: str,
) -> tuple[str, str, str, str]:
    receiver = _normalize_identifier(receiver_id, field="receiver_id")
    lineage = _normalize_identifier(physical_lineage, field="physical_lineage")
    raw_ref = _required_text(raw_evidence_ref, field="raw_evidence_ref", max_chars=512)
    message_id = _required_text(decoder_message_id, field="decoder_message_id", max_chars=256)
    if frequency_hz <= 0:
        raise ValueError("frequency_hz must be positive")
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    return receiver, lineage, raw_ref, message_id


@dataclass(frozen=True)
class DSCObservation:
    receiver_id: str
    physical_lineage: str
    observed_at: datetime
    frequency_hz: int
    source_terms: str | None
    raw_evidence_ref: str
    decoder_message_id: str
    category: str
    mmsi: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    nature_code: str | None = None
    field_presence: tuple[str, ...] = ()
    # Extended decoded fields (docs section 8): preserved verbatim when the
    # decoder supplies them, never invented when it doesn't -- every one of
    # these defaults to None/() rather than a guessed value.
    format: str | None = None
    from_mmsi: str | None = None
    to_mmsi: str | None = None
    nature_description: str | None = None
    telecommand_1: str | None = None
    telecommand_2: str | None = None
    end_of_sequence: str | None = None
    distress_time: str | None = None
    reported_frequency: float | None = None

    def __post_init__(self) -> None:
        receiver, lineage, raw_ref, message_id = _validate_common(
            receiver_id=self.receiver_id,
            physical_lineage=self.physical_lineage,
            observed_at=self.observed_at,
            frequency_hz=self.frequency_hz,
            raw_evidence_ref=self.raw_evidence_ref,
            decoder_message_id=self.decoder_message_id,
        )
        category = str(self.category or "").strip().lower()
        if category not in _DSC_CATEGORIES:
            category = "unknown"
        mmsi = str(self.mmsi or "").strip() or None
        if mmsi is not None and (not mmsi.isdigit() or len(mmsi) > 9):
            raise ValueError("mmsi must be numeric and at most 9 digits")
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("coordinates require both latitude and longitude")
        if self.latitude is not None and not -90 <= self.latitude <= 90:
            raise ValueError("latitude out of range")
        if self.longitude is not None and not -180 <= self.longitude <= 180:
            raise ValueError("longitude out of range")

        def _mmsi_or_none(value: object, *, field: str) -> str | None:
            text = str(value or "").strip() or None
            if text is not None and (not text.isdigit() or len(text) > 9):
                raise ValueError(f"{field} must be numeric and at most 9 digits")
            return text

        object.__setattr__(self, "receiver_id", receiver)
        object.__setattr__(self, "physical_lineage", lineage)
        object.__setattr__(self, "raw_evidence_ref", raw_ref)
        object.__setattr__(self, "decoder_message_id", message_id)
        object.__setattr__(self, "category", category)
        object.__setattr__(self, "mmsi", mmsi)
        object.__setattr__(
            self,
            "nature_code",
            str(self.nature_code or "").strip().lower()[:64] or None,
        )
        object.__setattr__(self, "source_terms", str(self.source_terms or "").strip() or None)
        object.__setattr__(
            self,
            "field_presence",
            tuple(sorted({str(name).strip().lower() for name in self.field_presence if str(name).strip()})),
        )
        object.__setattr__(self, "format", str(self.format or "").strip().lower()[:32] or None)
        object.__setattr__(self, "from_mmsi", _mmsi_or_none(self.from_mmsi, field="from_mmsi"))
        object.__setattr__(self, "to_mmsi", _mmsi_or_none(self.to_mmsi, field="to_mmsi"))
        object.__setattr__(
            self, "nature_description",
            str(self.nature_description or "").strip()[:128] or None,
        )
        object.__setattr__(self, "telecommand_1", str(self.telecommand_1 or "").strip()[:64] or None)
        object.__setattr__(self, "telecommand_2", str(self.telecommand_2 or "").strip()[:64] or None)
        object.__setattr__(
            self, "end_of_sequence", str(self.end_of_sequence or "").strip()[:32] or None,
        )
        object.__setattr__(self, "distress_time", str(self.distress_time or "").strip()[:16] or None)
        object.__setattr__(
            self, "reported_frequency",
            float(self.reported_frequency) if self.reported_frequency is not None else None,
        )


@dataclass(frozen=True)
class NAVTEXObservation:
    receiver_id: str
    physical_lineage: str
    observed_at: datetime
    frequency_hz: int
    source_terms: str | None
    raw_evidence_ref: str
    decoder_message_id: str
    station_id: str
    subject_id: str
    message_id: str
    area: str | None
    text: str

    def __post_init__(self) -> None:
        receiver, lineage, raw_ref, decoder_message_id = _validate_common(
            receiver_id=self.receiver_id,
            physical_lineage=self.physical_lineage,
            observed_at=self.observed_at,
            frequency_hz=self.frequency_hz,
            raw_evidence_ref=self.raw_evidence_ref,
            decoder_message_id=self.decoder_message_id,
        )
        station_id = _required_text(self.station_id, field="station_id", max_chars=8).upper()
        subject_id = _required_text(self.subject_id, field="subject_id", max_chars=8).upper()
        message_id = _required_text(self.message_id, field="message_id", max_chars=32)
        text = str(self.text or "").strip()
        if not text:
            raise ValueError("text must not be empty")

        object.__setattr__(self, "receiver_id", receiver)
        object.__setattr__(self, "physical_lineage", lineage)
        object.__setattr__(self, "raw_evidence_ref", raw_ref)
        object.__setattr__(self, "decoder_message_id", decoder_message_id)
        object.__setattr__(self, "station_id", station_id)
        object.__setattr__(self, "subject_id", subject_id)
        object.__setattr__(self, "message_id", message_id)
        object.__setattr__(self, "area", str(self.area or "").strip()[:128] or None)
        object.__setattr__(self, "text", text[:_MAX_NAVTEX_TEXT_CHARS])
        object.__setattr__(self, "source_terms", str(self.source_terms or "").strip() or None)
