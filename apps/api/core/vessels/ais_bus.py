# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import logging
from typing import Callable, Any

logger = logging.getLogger(__name__)

_PositionHook = Callable[..., None]
_ObservationHook = Callable[[Any], None]
_position_hooks: list[_PositionHook] = []
_observation_hooks: list[_ObservationHook] = []


def register_position_hook(hook: _PositionHook) -> None:
    if hook not in _position_hooks:
        _position_hooks.append(hook)


def register_observation_hook(hook: _ObservationHook) -> None:
    """Subscribe to the canonical AIS observation, including source time."""
    if hook not in _observation_hooks:
        _observation_hooks.append(hook)


def position_hook_count() -> int:
    return len(_position_hooks)


def observation_hook_count() -> int:
    return len(_observation_hooks)


def publish(observation: Any) -> None:
    for hook in tuple(_observation_hooks):
        try:
            hook(observation)
        except Exception:
            logger.debug("AIS observation hook failed", exc_info=True)
    publish_legacy(
        observation.mmsi, observation.ship_name, observation.lat, observation.lon,
        observation.sog, observation.nav_status, observation.cog, observation.heading,
        observation.received_at,
    )


def publish_legacy(
    mmsi: str, ship_name: str, lat: float, lon: float,
    sog: float | None, nav_status: int | None,
    cog: float | None, heading: float | None, received_at,
) -> None:
    args = (mmsi, ship_name, lat, lon, sog, nav_status, cog, heading, received_at)
    for hook in tuple(_position_hooks):
        try:
            hook(*args)
        except Exception:
            logger.debug("AIS position hook failed", exc_info=True)


def _reset_for_tests() -> None:
    _position_hooks.clear()
    _observation_hooks.clear()
