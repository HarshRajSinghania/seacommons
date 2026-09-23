# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import hashlib

from core.radio.ais_association import (
    associate_and_persist_dsc,
    public_association,
)
from core.radio.burst import RadioBurstDetector
from core.radio.provider import DecodedRadioMessage, RadioObservation

_burst_detector = RadioBurstDetector()


def _persist_radio_observation(observation: RadioObservation) -> None:
    from core.db.session import session_scope
    from core.observability import record_remote_radio_event
    from core.radio.source_observation import persist_radio_observation

    try:
        with session_scope() as db:
            persist_radio_observation(db, observation)
        record_remote_radio_event(
            provider=observation.provider, state="connected", outcome="observation"
        )
    except Exception:
        record_remote_radio_event(
            provider=observation.provider, state="connected", outcome="persist_failed"
        )
        raise


def _persist_radio_burst(burst) -> None:
    from core.db.session import session_scope
    from core.radio.burst_store import correlate_and_persist_recent, persist_radio_burst

    with session_scope() as db:
        persist_radio_burst(db, burst)
        correlate_and_persist_recent(db, burst)


def handle_radio_observation(observation: RadioObservation) -> None:
    """Persist signal metadata and closed RF bursts; never infer a decoded message."""
    _persist_radio_observation(observation)
    for burst in _burst_detector.ingest(observation):
        _persist_radio_burst(burst)


def _raw_evidence_ref(message: DecodedRadioMessage) -> str:
    native = message.provider_message_id
    if not native:
        material = "|".join(
            (
                message.kind,
                message.physical_lineage,
                message.observed_at.isoformat(),
                str(message.frequency_hz),
            )
        )
        native = hashlib.blake2s(material.encode("utf-8"), digest_size=12).hexdigest()
    return f"radio-decoded:{message.provider}:{native}"


def get_structured_radio_runtime():
    from core.radio.structured_runtime import get_structured_radio_runtime as _get

    return _get()


def handle_decoded_radio_message(message: DecodedRadioMessage) -> dict[str, object]:
    """Route explicit decoder output, then correlate accepted DSC with AIS."""
    runtime = get_structured_radio_runtime()
    common = {
        "receiver_id": message.receiver_id,
        "physical_lineage": message.physical_lineage,
        "observed_at": message.observed_at,
        "frequency_hz": message.frequency_hz,
        "source_terms": message.source_terms,
        "raw_evidence_ref": _raw_evidence_ref(message),
    }
    if message.kind == "dsc":
        result = runtime.ingest_dsc(message.payload, **common)
        observation_id = str(result.get("observation_id") or "")
        if result.get("accepted") and observation_id:
            try:
                association = associate_and_persist_dsc(message, observation_id=observation_id)
            except Exception:
                association = None
            if association is not None:
                result["ais_association"] = public_association(association)
                if association.episode_id:
                    # A strong radio↔AIS match enriches an existing canonical
                    # case. It never mints a parallel radio-only episode.
                    result["maritime_episode_id"] = association.episode_id
        return result
    return runtime.ingest_navtex(
        message.payload,
        decoder_message_id=message.provider_message_id,
        **common,
    )


def radio_acquisition_status() -> dict[str, object]:
    from core.config import config
    from core.radio.decoder_runtime import radio_decoder_status
    from core.radio.runtime import get_remote_radio_status

    status = get_remote_radio_status(include_receivers=True)
    channels = list(status.get("channels") or [])
    decoder = dict(radio_decoder_status())
    frames = int(decoder.get("frames") or 0)
    decoded = int(decoder.get("decoded") or 0)
    dropped = int(decoder.get("dropped") or 0)
    decoder["decode_ratio"] = round(decoded / frames, 6) if frames else 0.0
    decoder["drop_ratio"] = round(dropped / frames, 6) if frames else 0.0
    if not status.get("enabled"):
        state = "disabled"
    elif int(status.get("started") or 0) == 0:
        state = "offline"
    else:
        receivers = status.get("receivers") or []
        any_connected = any(row.get("state") == "connected" for row in receivers)
        under_replicated = any(
            int(row.get("active") or 0) < int(row.get("desired") or 0)
            for row in channels
        )
        state = "live" if any_connected and not under_replicated else "degraded"
    structured_enabled = bool(config.STRUCTURED_RADIO_ENABLED)
    decoder_enabled = bool(decoder.get("enabled"))
    decoder_frames = int(decoder.get("frames") or 0)
    if not structured_enabled:
        structured_state = "disabled"
    elif not decoder_enabled:
        structured_state = "offline"
    elif decoder_frames == 0:
        structured_state = "idle"
    elif not bool(decoder.get("worker_alive")) or int(decoder.get("errors") or 0) > 0:
        structured_state = "degraded"
    elif int(decoder.get("decoded") or 0) == 0:
        # RF transport can be healthy while semantic DSC extraction is not.
        # Do not label structured radio "live" merely because frames are
        # flowing; that would hide the exact failure mode that prevents radio
        # evidence from entering the investigation pipeline.
        structured_state = "degraded"
    else:
        structured_state = "live"
    return {
        "state": state,
        "structured_enabled": structured_enabled,
        "structured_state": structured_state,
        "decoder": decoder,
        "configured": int(status.get("configured") or 0),
        "started": int(status.get("started") or 0),
        "failed": int(status.get("failed") or 0),
        "receivers": list(status.get("receivers") or []),
        "channels": list(status.get("channels") or []),
    }


def register_radio_acquisition_status() -> None:
    from core.acquisition.status import register_acquisition_status

    register_acquisition_status("radio", "Radio", radio_acquisition_status)
