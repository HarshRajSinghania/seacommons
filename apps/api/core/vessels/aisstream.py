# SPDX-License-Identifier: AGPL-3.0-or-later
"""
AISStream.io WebSocket client - feeds real AIS position data into VesselRegistry.

Connects to wss://stream.aisstream.io/v0/stream, subscribes to Mediterranean
bounding box, processes PositionReport and ShipStaticData messages.
Auto-reconnects on disconnect.

A second, low-volume connection additionally tracks the known NGO/SAR fleet
by MMSI (AISStream's FiltersShipMMSI, capped at 50 values) with a global
bounding box. It reuses the primary account key unless an override key is
configured. This isolates fleet freshness from the high-volume Mediterranean
stream and prevents stale last-known SAR positions from being mistaken for
current movement.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Compatibility wrappers: all downstream consumers still use the historical
# nine-argument hook contract, but hook ownership now lives in ais_bus so new
# providers can share one fan-out without changing consumer signatures.
# `_position_hooks` remains as an alias because existing tests/internal tools
# reset it directly; wrappers resynchronise the bus if that alias is replaced.
from core.vessels import ais_bus as _ais_bus
_position_hooks = _ais_bus._position_hooks


def _sync_compat_hook_list() -> None:
    global _position_hooks
    if _position_hooks is not _ais_bus._position_hooks:
        _ais_bus._position_hooks = _position_hooks


def register_position_hook(hook) -> None:
    _sync_compat_hook_list()
    _ais_bus.register_position_hook(hook)


def position_hook_count() -> int:
    _sync_compat_hook_list()
    return _ais_bus.position_hook_count()

# Mediterranean + Black Sea bounding box [lat_min, lon_min], [lat_max, lon_max]
_BBOX = [[[28.0, -6.0], [47.0, 42.0]]]
# Whole-world box, used only for the MMSI-filtered NGO-fleet subscription.
_GLOBAL_BBOX = [[[-90.0, -180.0], [90.0, 180.0]]]
_WS_URL = "wss://stream.aisstream.io/v0/stream"

_SHIP_TYPE_MAP = {
    range(20, 30): "WING_IN_GROUND",
    range(30, 32): "FISHING",
    range(31, 33): "TOWING",
    range(33, 35): "DREDGING",
    range(35, 36): "DIVING",
    range(36, 37): "MILITARY",
    range(37, 38): "SAILING",
    range(38, 40): "PLEASURE",
    range(40, 50): "HSC",
    range(50, 56): "PILOT",
    range(56, 58): "SAR",
    range(58, 59): "TUG",
    range(59, 60): "PORT_TENDER",
    range(60, 70): "PASSENGER",
    range(70, 80): "CARGO",
    range(80, 90): "TANKER",
    range(90, 100): "OTHER",
}


def _ship_type_label(type_code: int) -> str:
    for r, label in _SHIP_TYPE_MAP.items():
        if type_code in r:
            return label
    return "UNKNOWN"


class AISStreamClient:
    """Background thread that streams live AIS data from AISStream.io."""

    def __init__(
        self,
        api_key: str,
        *,
        label: str = "Mediterranean",
        bbox: list | None = None,
        mmsi_filter: list[str] | None = None,
        on_observation=None,
        on_health=None,
        publish_legacy: bool = True,
        update_registry: bool | None = None,
    ):
        self._api_key = api_key
        self._label = label
        self._bbox = bbox or _BBOX
        self._mmsi_filter = mmsi_filter
        self._on_observation = on_observation
        self._on_health = on_health
        self._publish_legacy = bool(publish_legacy)
        self._update_registry = (
            self._publish_legacy if update_registry is None else bool(update_registry)
        )
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._connected = False
        self._last_message_at = None
        self._error = None
        self.messages_received = 0

    @property
    def connected(self) -> bool:
        return self._connected

    def health(self):
        from core.vessels.ais_provider import AISProviderHealth
        return AISProviderHealth(
            provider="aisstream", connected=self._connected,
            last_message_at=self._last_message_at,
            messages_received=self.messages_received, error=self._error,
        )

    def _emit_health(self) -> None:
        if self._on_health is None:
            return
        try:
            self._on_health(self.health())
        except Exception:
            logger.debug("AISStream health callback failed", exc_info=True)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"aisstream-{self._label}"
        )
        self._thread.start()
        logger.info("AISStream client started (%s)", self._label)

    def stop(self) -> None:
        self._stop.set()
        self._connected = False
        self._emit_health()

    def _run(self) -> None:
        from core.vessels.registry import registry
        from core.intel.source_registry import source_registry
        import websockets.sync.client as ws_sync

        # AIS never reported into the same health/alerting system every other
        # source already uses -- verified live: the whole Mediterranean feed
        # went completely silent (connects, subscribes, then times out with
        # zero messages every cycle -- a known, unresolved upstream AISstream
        # bug, github.com/aisstream/aisstream/issues/15) for 7+ hours with
        # nothing surfacing it anywhere except a user noticing "no nearby
        # vessels" on the map. record_poll on every disconnect means the
        # existing 15-minute source_health job (core/scheduler.py) now alerts
        # within a few reconnect cycles instead.
        source_name = "aisstream_" + self._label.lower().replace(" ", "_").replace("(", "").replace(")", "")
        source_registry.register(source_name, "ais")

        backoff = 2
        last_report_ts = time.monotonic()
        messages_since_report = 0
        while not self._stop.is_set():
            try:
                logger.info("AISStream: connecting to %s (%s)", _WS_URL, self._label)
                # AISStream has repeatedly left the socket healthy and flowing
                # while failing the websockets library's protocol-level ping
                # watchdog. The default ping_interval=20/ping_timeout=20 then
                # tears down a productive stream every ~45-60 seconds. This
                # client already owns stronger application-level liveness:
                # recv(timeout=60), reconnect on timeout/error, source-health
                # reporting, and a forced reconnect after three minutes with
                # no PositionReports. Disable only the redundant protocol ping.
                with ws_sync.connect(
                    _WS_URL,
                    open_timeout=15,
                    ping_interval=None,
                ) as ws:
                    # Subscribe
                    sub = {
                        "APIKey": self._api_key,
                        "BoundingBoxes": self._bbox,
                        "FilterMessageTypes": ["PositionReport", "ShipStaticData"],
                    }
                    if self._mmsi_filter:
                        sub["FiltersShipMMSI"] = self._mmsi_filter
                    ws.send(json.dumps(sub))
                    self._connected = True
                    self._error = None
                    self._emit_health()
                    backoff = 2
                    logger.info("AISStream: subscribed (%s)", self._label)

                    silent_windows = 0
                    positions_this_window = 0
                    while not self._stop.is_set():
                        raw = ws.recv(timeout=60)
                        if not raw:
                            continue
                        try:
                            msg = json.loads(raw)
                            self._handle(msg, registry)
                            self.messages_received += 1
                            self._last_message_at = datetime.now(timezone.utc)
                            messages_since_report += 1
                            if msg.get("MessageType") == "PositionReport":
                                positions_this_window += 1
                        except Exception as e:
                            logger.debug("AISStream msg parse error: %s", e)
                        now = time.monotonic()
                        if now - last_report_ts >= 60:
                            source_registry.record_poll(source_name, events_found=messages_since_report)
                            # Known AISStream bug: the socket stays open (and
                            # keepalives may arrive) but no real PositionReports
                            # flow -- aisstream/aisstream#15. Force a fresh
                            # connection after 3 empty minutes rather than sit
                            # on a dead feed.
                            silent_windows = silent_windows + 1 if positions_this_window == 0 else 0
                            messages_since_report = 0
                            positions_this_window = 0
                            last_report_ts = now
                            if silent_windows >= 3:
                                logger.warning(
                                    "AISStream (%s): 3 min with no PositionReports — forcing reconnect",
                                    self._label,
                                )
                                break

            except Exception as exc:
                self._connected = False
                self._error = str(exc)
                self._emit_health()
                source_registry.record_poll(source_name, events_found=messages_since_report, error=str(exc))
                messages_since_report = 0
                last_report_ts = time.monotonic()
                if not self._stop.is_set():
                    logger.warning(
                        "AISStream (%s) disconnected: %s  retry in %ds",
                        self._label, exc, backoff,
                    )
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 60)

    def _handle(self, msg: dict, registry) -> None:
        mtype = msg.get("MessageType", "")
        meta = msg.get("MetaData", {})
        mmsi = str(meta.get("MMSI", "")).strip()
        if not mmsi:
            return

        if mtype == "PositionReport":
            pr = msg.get("Message", {}).get("PositionReport", {})
            lat = pr.get("Latitude") or meta.get("latitude")
            lon = pr.get("Longitude") or meta.get("longitude")
            cog = pr.get("Cog")
            sog = pr.get("Sog")
            hdg = pr.get("TrueHeading")
            nav_status = pr.get("NavigationalStatus")
            if lat is not None and lon is not None:
                name = meta.get("ShipName", "").strip()
                if self._update_registry:
                    registry.upsert(
                        mmsi,
                        ship_name=name or None,
                        lat=float(lat),
                        lon=float(lon),
                        course=float(cog) if cog is not None else None,
                        speed=float(sog) if sog is not None else None,
                        heading=float(hdg) if hdg is not None and hdg != 511 else None,
                        nav_status=int(nav_status) if nav_status is not None else None,
                    )
                received_at = datetime.now(timezone.utc)
                from core.vessels.ais_provider import AISPositionObservation
                observation = AISPositionObservation(
                    mmsi=mmsi, ship_name=name, lat=float(lat), lon=float(lon),
                    sog=float(sog) if sog is not None else None,
                    cog=float(cog) if cog is not None else None,
                    heading=float(hdg) if hdg is not None and hdg != 511 else None,
                    nav_status=int(nav_status) if nav_status is not None else None,
                    observed_at=received_at, received_at=received_at,
                    provider="aisstream", upstream_source="aisstream",
                )
                if self._on_observation is not None:
                    try:
                        self._on_observation(observation)
                    except Exception:
                        logger.debug("AIS provider observation callback failed", exc_info=True)
                if self._publish_legacy:
                    from core.vessels.ais_bus import publish
                    publish(observation)

        elif mtype == "ShipStaticData":
            sd = msg.get("Message", {}).get("ShipStaticData", {})
            name = (sd.get("Name") or meta.get("ShipName") or "").strip()
            registry.upsert(
                mmsi,
                ship_name=name or None,
                imo=sd.get("ImoNumber"),
                ship_type=sd.get("Type"),
                flag=sd.get("Flag"),
                destination=(sd.get("Destination") or "").strip() or None,
            )


# Module-level singletons - started by main.py lifespan
_client: AISStreamClient | None = None
_ngo_client: AISStreamClient | None = None


def get_client() -> AISStreamClient | None:
    """The primary Mediterranean-bbox client (used for ops health reporting)."""
    return _client


def get_ngo_client() -> AISStreamClient | None:
    """The secondary, MMSI-filtered global client tracking the known NGO fleet."""
    return _ngo_client


def start(
    api_key: str, *, ngo_api_key: str = "", on_observation=None, on_health=None,
    publish_legacy: bool = True,
) -> AISStreamClient:
    """Start the regional stream plus a global MMSI-filtered SAR-fleet stream.

    ngo_api_key is an optional override. When omitted, the fleet stream reuses
    api_key. A live production probe verifies that AISStream accepts the
    concurrent subscription; keeping it MMSI-filtered makes it low-volume and
    isolates SAR position freshness from regional stream churn.
    """
    global _client, _ngo_client
    _client = AISStreamClient(
        api_key, label="Mediterranean", on_observation=on_observation, on_health=on_health,
        publish_legacy=publish_legacy,
    )
    _client.start()

    effective_ngo_key = ngo_api_key or api_key
    if effective_ngo_key:
        try:
            from core.intel.ngo_registry import NGO_VESSELS

            ngo_mmsi = list(NGO_VESSELS.keys())[:50]
            _ngo_client = AISStreamClient(
                effective_ngo_key,
                label="NGO fleet (global)",
                bbox=_GLOBAL_BBOX,
                mmsi_filter=ngo_mmsi,
                on_observation=on_observation,
                on_health=None,
                # Dedicated freshness stream: update vessel state, but do not
                # fan duplicate copies into legacy detector hooks.
                publish_legacy=False,
                update_registry=True,
            )
            _ngo_client.start()
        except Exception:
            logger.warning("AISStream NGO-fleet subscription failed to start", exc_info=True)

    return _client
