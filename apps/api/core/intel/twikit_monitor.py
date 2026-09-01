# SPDX-License-Identifier: AGPL-3.0-or-later
"""Twikit X monitor — account-focused public-tweet ingestion via a real session.

Twikit talks to X's public web/GraphQL endpoints using cookies exported from the
account's browser. Unlike the paid official API, this is free, but it *is*
account-emulation: it must be treated as the least-trusted X source.

Scope (per operator directive): track a curated list of *specific* accounts
(NGO SAR monitors such as @alarm_phone), NOT generic keyword searches.
Polling is tiered, not always-on: each sweep only fetches accounts whose poll
interval has elapsed. Priority accounts (default @alarm_phone) are polled
every ~45 s for near-real-time pickup, the rest every ~5 min. A Telegram
notification is fired when any tracked account posts.

X offers no push notification for "another account posted" (the paid official
Streaming API is enterprise-only and twikit's own streaming only covers your
own tweet engagement/DMs), so some polling is unavoidable — the X web app
itself polls roughly every 30 s. The tiered scheduler below keeps that cost
minimal.

Ingestion semantics ("strong distress parsing"): a tweet is tagged as distress
only when it is an actionable direct distress/SAR call (is_direct_distress_call
— the same strict signal the Alarm Phone monitor uses), never for resolved or
contextual posts. Non-distress posts from tracked accounts are still ingested
    but tagged `report_kind="news"` (or `"resolved"`), so the operational/distress
    feed is never polluted by commentary.

Repost semantics (operator directive): a repost (retweeted_tweet) of an alert
that already opened an incident must answer that SAME thread — it is recorded
onto the parent incident's `thread_reposts` bookkeeping and never spawns a new
marker, never bumps a version, and never re-broadcasts an update. Only if the
original was never tracked is the original content ingested (deduplicated by
tweet id), so nothing is lost.

Quote-tweet semantics: a quote tweet of an already-tracked incident is
threaded the same way as a plain repost (no new marker, no re-broadcast), but
— unlike a plain RT — the quoting account's own caption is new content (e.g.
"confirmed rescued"), so it is kept as a `note` on the thread record instead
of being discarded. When the quoted tweet is NOT already tracked, its text
and media are merged with the quoting account's caption for distress/geo
extraction: Alarm Phone and other tracked NGOs sometimes quote a source (a
witness, a partner NGO) with only a short caption of their own, and the real
distress content — including the map screenshot with GPS coordinates — lives
in the quoted tweet, not the caption. Losing that would mean losing the
signal entirely.

Session resilience: a transient failure while establishing the twikit session
(e.g. a network blip at process start) retries with the same capped backoff
used for poll errors, rather than permanently killing the monitor until the
next process restart. If every tracked account fails to poll for several
consecutive cycles — a strong signal the session itself has gone stale, not
that every account broke independently — the client is rebuilt from the same
cookies file on the next cycle.


Security posture (must be preserved):
  * Strictly opt-in: `TWIKIT_ENABLED=true` AND `TWIKIT_COOKIES_FILE` must point
    at an existing file, otherwise the monitor stays dormant.
  * Cookies live on disk (0600, owned by the service user) or in a secrets
    manager — never in the repo, never in logs. Cookie contents are never logged.
  * Read-only: only profile/timeline reads are called. No follow/like/retweet/
    DM/block calls, ever. Exponential backoff on rate limits (cap 15 min)
    protects the real account from aggressive polling.
  * Every event is labelled `source_policy="unofficial"` — EXCEPT confirmed
    distress calls from these specific tracked accounts, which the operator
    has explicitly chosen to surface on the public live map
    (`publication_status="published"`, `source_policy="operator_published"`).
    Non-distress news from the same accounts stays operator-only and never
    feeds the public map or distress triangulation.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import shutil
import threading
import time
from datetime import UTC, datetime
from typing import Any, Optional

from core.config import config
from core.intel.auto_drift_client import request_auto_drift
from core.intel.area_extract import extract_area
from core.intel.forensic_link import attach_forensic_packet
from core.intel.lifecycle import has_own_reply_resolution
from core.intel.geoextract import (
    classify_severity,
    extract_coords,
    extract_numeric_coords,
    extract_relative_coords,
    find_all_place_matches,
    is_direct_distress_call,
    is_resolved_distress,
    place_match_precision,
)
from core.intel.humanitarian import humanitarian_case_metadata
from core.intel.ngo_registry import NGO_TWITTER_HANDLES
from core.intel.store import IntelEvent, intel_store
from core.intel.twitter_monitor import _make_title

logger = logging.getLogger(__name__)

_SOURCE_NAME = "X / Twitter (twikit)"
_REQUIRED_COOKIES = ("auth_token", "ct0")
_MAX_BACKOFF_S = 900  # 15 min — be extremely gentle with the real account
_BASE_POLL_INTERVAL_S = 300  # non-priority accounts
_PRIORITY_POLL_INTERVAL_S = 45  # near-real-time accounts (default @alarm_phone)
_SLEEP_CAP_S = 60.0  # wake up at most this often to recompute due accounts
_SESSION_REBUILD_AFTER_FAILURES = 3  # consecutive all-accounts-failed cycles
# How often to re-check active incidents for a self-reply update ("Rescued to
# #Lampedusa!"). These never appear via the account's own "Tweets"/"Replies"
# timeline fetch — verified live: X excludes an account's replies to its own
# thread from both — only tweet.replies on the original tweet surfaces them.
# 15 min is gentle (one extra call per still-active incident per cycle).
_REPLY_CHECK_INTERVAL_S = 900
_REPLY_CHECK_STARTUP_DELAY_S = 60
# Give a tweet time to scroll further back than _REPLY_CHECK_TIMELINE_SIZE
# covers before treating its absence as suspicious, not just "not fetched
# far enough back yet" on an active-posting day.
_UNREACHABLE_MIN_AGE_S = 6 * 3600


def _age_seconds(timestamp_utc: str) -> Optional[float]:
    try:
        posted = datetime.fromisoformat(timestamp_utc)
    except (TypeError, ValueError):
        return None
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=UTC)
    return (datetime.now(UTC) - posted).total_seconds()
# How far back into an account's own timeline to look for a still-active
# incident's original tweet. Generous enough to comfortably cover the 7-day
# candidate window even for an account posting several times a day.
_REPLY_CHECK_TIMELINE_SIZE = 40
_PRIORITY_DEFAULT = "alarm_phone"
_DEFAULT_ACCOUNTS = list(NGO_TWITTER_HANDLES)
# Alarm Phone (and the other tracked SAR NGOs) publish the actual GPS position
# as a map screenshot, not in the tweet text. Same host allow-list as the
# shared OCR path (x_media_utils._ocr_photo).
_ALLOWED_MEDIA_HOSTS = frozenset({"pbs.twimg.com"})

_ALARM_PHONE_HANDLES = frozenset({"alarm_phone", "alarmphone"})


def _tracked_image_accounts() -> frozenset[str]:
    extra = {
        part.strip().lstrip("@").lower()
        for part in str(getattr(config, "ALARM_PHONE_IMAGE_V2_ACCOUNTS", "") or "").split(",")
        if part.strip()
    }
    return _ALARM_PHONE_HANDLES | extra


def _analyze_tweet_image(
    url: str, *, context_places: tuple[str, ...] = (), sea_snap: bool = True
):
    """Structured image understanding seam for one tracked X media URL."""
    from core.intel.image_extraction import extract_from_url

    return extract_from_url(url, context_places=context_places, sea_snap=sea_snap)


class TwikitMonitor:
    """Poll tracked X accounts via a twikit session; source_policy='unofficial'."""

    def __init__(
        self,
        enabled: bool = False,
        cookies_file: str = "",
        accounts: str = "",
        poll_interval_s: int = _BASE_POLL_INTERVAL_S,
        priority_accounts: str = "",
        priority_poll_interval_s: int = _PRIORITY_POLL_INTERVAL_S,
        alerts_enabled: bool = False,
    ) -> None:
        self._enabled = bool(enabled)
        self._cookies_file = (cookies_file or "").strip()
        self._accounts = self._parse_accounts(accounts)
        self._poll_interval_s = int(poll_interval_s)
        self._priority_accounts = self._parse_priority(priority_accounts)
        self._priority_poll_interval_s = int(priority_poll_interval_s)
        self._alerts_enabled = bool(alerts_enabled)
        self._since_id: dict[str, int] = {}
        self._users: dict[str, Any] = {}
        self._next_poll_ts: dict[str, float] = {}
        self._next_reply_check_ts = 0.0
        self._flagged_unreachable: set[str] = set()
        self._running = False
        self._thread: threading.Thread | None = None
        self._backoff = 0.0

    @staticmethod
    def _parse_accounts(raw: str) -> list[str]:
        accounts = [a.strip().lstrip("@") for a in raw.split(",") if a.strip()]
        return accounts or list(_DEFAULT_ACCOUNTS)

    @staticmethod
    def _parse_priority(raw: str) -> set[str]:
        accounts = {a.strip().lstrip("@") for a in raw.split(",") if a.strip()}
        return accounts or {_PRIORITY_DEFAULT}

    def _interval_for(self, handle: str) -> int:
        return (
            self._priority_poll_interval_s
            if handle in self._priority_accounts
            else self._poll_interval_s
        )

    @property
    def priority_accounts(self) -> list[str]:
        return sorted(self._priority_accounts)

    @property
    def configured(self) -> bool:
        if not self._enabled:
            return False
        if not self._cookies_file:
            logger.info("X (twikit) disabled: TWIKIT_COOKIES_FILE is not set")
            return False
        if not os.path.isfile(self._cookies_file):
            logger.warning(
                "X (twikit) disabled: cookies file not found: %s", self._cookies_file
            )
            return False
        return True

    @property
    def tracked_accounts(self) -> list[str]:
        return list(self._accounts)

    def start(self) -> None:
        if self._running:
            return
        if not self.configured:
            return
        from core.intel.source_registry import source_registry

        source_registry.register(_SOURCE_NAME, "twitter")
        source_registry.register_targets(_SOURCE_NAME, self._accounts)
        self._next_poll_ts = {handle: time.monotonic() for handle in self._accounts}
        # A short delay, not the full interval: a reply can land on an
        # already-tracked incident at any time, independent of when this
        # process happens to (re)start — a deploy restart must not open an
        # up-to-15-minute blind window before resuming self-reply checks.
        # Still delayed slightly so it doesn't stack on top of the initial
        # per-account poll burst.
        self._next_reply_check_ts = time.monotonic() + _REPLY_CHECK_STARTUP_DELAY_S
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="intel-x-twikit",
        )
        self._thread.start()
        logger.info(
            "X (twikit) monitor started; tracking %d account(s) | "
            "priority (%s) poll=%ss | base poll=%ss | alerts=%s",
            len(self._accounts),
            ", ".join(sorted(self._priority_accounts)),
            self._priority_poll_interval_s,
            self._poll_interval_s,
            "on" if self._alerts_enabled else "off",
        )

    def stop(self) -> None:
        self._running = False

    def _run_loop(self) -> None:
        try:
            asyncio.run(self._async_loop())
        except Exception as exc:  # pragma: no cover - defensive
            logger.error("X (twikit) monitor crashed: %s", exc)

    async def _async_loop(self) -> None:
        from core.intel.source_registry import source_registry

        client = None
        consecutive_full_failures = 0
        while self._running:
            if client is None:
                try:
                    client = await self._build_client()
                except Exception as exc:
                    logger.error(
                        "X (twikit) session setup failed (%s); retrying with backoff "
                        "instead of giving up. Cookie file: %s",
                        exc,
                        self._cookies_file,
                    )
                    source_registry.record_poll(_SOURCE_NAME, error=str(exc))
                    await asyncio.sleep(self._next_delay("session setup failed"))
                    continue

            now = time.monotonic()
            due = [h for h in self._accounts if self._next_poll_ts[h] <= now]
            new_count = 0
            failed = 0
            if due:
                for handle in due:
                    handle_new_count = 0
                    try:
                        for tweet in await self._fetch_account(client, handle):
                            if self._ingest(tweet, handle):
                                new_count += 1
                                handle_new_count += 1
                        source_registry.record_target_poll(
                            _SOURCE_NAME,
                            handle,
                            events_found=handle_new_count,
                        )
                        self._next_poll_ts[handle] = time.monotonic() + self._interval_for(handle)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        failed += 1
                        # A missing, renamed, or temporarily unavailable account
                        # must observe the same polling interval as a successful
                        # one.  Leaving it immediately due creates a hot loop and
                        # can make the whole source look unavailable even while
                        # other tracked accounts are healthy.
                        self._next_poll_ts[handle] = (
                            time.monotonic() + self._interval_for(handle)
                        )
                        # Evict the cached user object: a renamed/suspended-then
                        # -reinstated account or a one-off API hiccup should not
                        # wedge this handle into the same failure forever.
                        self._users.pop(handle, None)
                        source_registry.record_target_poll(
                            _SOURCE_NAME,
                            handle,
                            error=str(exc),
                        )
                        logger.warning("X (twikit) poll error for @%s: %s", handle, exc)
            error = "poll failed" if due and failed == len(due) else None
            # A failed timeline poll and the reply scan consume separate X
            # endpoints, but firing both in the same cycle compounds a rate
            # limit and delays recovery. Preserve the reply scan for the next
            # healthy/idle cycle instead.
            if error is None and now >= self._next_reply_check_ts:
                try:
                    await self._check_self_replies(client)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("X (twikit) self-reply check failed: %s", exc)
                self._next_reply_check_ts = time.monotonic() + _REPLY_CHECK_INTERVAL_S

            if error:
                consecutive_full_failures += 1
                if consecutive_full_failures >= _SESSION_REBUILD_AFTER_FAILURES:
                    logger.warning(
                        "X (twikit) %d consecutive fully-failed polls; rebuilding the "
                        "session (cookies may have rotated or expired)",
                        consecutive_full_failures,
                    )
                    client = None
                    self._users.clear()
                    consecutive_full_failures = 0
            elif due:
                # Only a real successful poll proves the session recovered.
                # Idle scheduler ticks between retries must not erase backoff.
                consecutive_full_failures = 0
                self._backoff = 0.0
            source_registry.record_poll(_SOURCE_NAME, events_found=new_count, error=error)
            await asyncio.sleep(self._next_delay(error))

    def _next_delay(self, error: Optional[str]) -> float:
        if error:
            self._backoff = min(self._backoff * 2 + 30, _MAX_BACKOFF_S) if self._backoff else 30.0
            return self._backoff
        now = time.monotonic()
        next_due = min(
            (self._next_poll_ts.get(h, now) for h in self._accounts),
            default=now,
        )
        return min(max(next_due - now, 1.0), _SLEEP_CAP_S)

    async def _build_client(self):
        from twikit import Client

        cookies = self._load_cookies()
        if cookies is None:
            raise RuntimeError("cookies file could not be parsed")
        missing = [k for k in _REQUIRED_COOKIES if k not in cookies]
        if missing:
            raise RuntimeError(
                f"cookies file is missing required keys: {', '.join(missing)}"
            )
        client = Client(language="en")
        client.set_cookies(cookies)
        logger.info(
            "X (twikit) session cookies loaded (%d cookies); content never logged",
            len(cookies),
        )
        return client

    def _load_cookies(self) -> Optional[dict[str, str]]:
        try:
            with open(self._cookies_file, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except Exception as exc:
            logger.error("X (twikit) could not read cookies file: %s", exc)
            return None
        if isinstance(raw, dict):
            return {str(k): str(v) for k, v in raw.items() if v is not None}
        if isinstance(raw, list):
            out: dict[str, str] = {}
            for item in raw:
                if isinstance(item, dict) and item.get("name") and item.get("value") is not None:
                    out[str(item["name"])] = str(item["value"])
            return out
        return None

    async def _fetch_account(self, client, handle: str) -> list[Any]:
        user = self._users.get(handle)
        if user is None:
            user = await client.get_user_by_screen_name(handle)
            self._users[handle] = user
        # UserTweetsAndReplies contains the normal profile posts as well as
        # standalone replies. Alarm Phone sometimes publishes a new distress
        # call as a reply to an authority/partner; UserTweets silently misses
        # those calls. One combined request keeps the same rate budget.
        result = await user.get_tweets("Replies", count=20)
        since = self._since_id.get(handle, 0)
        fresh: list[Any] = []
        for tweet in result:
            try:
                tweet_id = int(str(tweet.id))
            except (TypeError, ValueError):
                continue
            if tweet_id <= since:
                continue
            fresh.append(tweet)
        if fresh:
            self._since_id[handle] = max(since, max(int(str(tweet.id)) for tweet in fresh))
        return fresh

    @staticmethod
    def _quoted_tweet(tweet: Any) -> Optional[Any]:
        """Best-effort access to a quote-tweet's quoted status.

        twikit/twifork have exposed the quoted tweet under different attribute
        names across versions/forks (``quote``, ``quoted_tweet``,
        ``quoted_status``). Try each so a library upgrade never silently drops
        the quoted content — which is often where the actual distress text
        and coordinate screenshot live, not in the quoting account's caption.
        """
        for attr in ("quote", "quoted_tweet", "quoted_status"):
            quoted = getattr(tweet, attr, None)
            if quoted is not None and getattr(quoted, "id", None) is not None:
                return quoted
        return None

    @staticmethod
    def _tweet_media_urls(
        tweet: Any, *, tweet_id: str = "", quoted_tweet: Any = None
    ) -> list[str]:
        """Resolve allow-listed, original-resolution X media canonically."""
        from core.intel.x_media import resolve_x_media

        resolution = resolve_x_media(
            tweet,
            tweet_id or str(getattr(tweet, "id", "")),
            quoted_tweet,
            allow_syndication=config.X_MEDIA_SYNDICATION_FALLBACK,
        )
        if not resolution.candidates:
            if resolution.failure_reason == "candidates_failed_host_allowlist":
                logger.warning(
                    "X (twikit) tweet %s: candidate media URL(s) found but none "
                    "matched the allowed host (%s); shapes tried: %s",
                    tweet_id, sorted(_ALLOWED_MEDIA_HOSTS), resolution.shapes_tried,
                )
            else:
                logger.debug(
                    "X (twikit) tweet %s: no media found in any known shape (%s) — "
                    "this tweet likely has no attached image",
                    tweet_id, resolution.shapes_tried,
                )
        return resolution.urls

    def _ocr_tweet_media(
        self,
        tweet_id: str,
        urls: list[str],
        *,
        context_places: tuple[str, ...] = (),
        sea_snap: bool = True,
    ) -> tuple[Optional[tuple[float, float]], bool, str, dict[str, Any]]:
        """Analyse images through ImageExtractionResult, preserving legacy tuple output."""
        if not urls:
            return None, False, "none", {}
        last_assessment: dict[str, Any] = {}
        for url in urls:
            try:
                result = _analyze_tweet_image(
                    url, context_places=context_places, sea_snap=sea_snap
                )
            except Exception as exc:
                logger.debug("X (twikit) media OCR failed for %s (%s): %s", tweet_id, url, exc)
                continue
            last_assessment = result.as_metadata()
            candidate, attempted, method, diagnostics = result.legacy_tuple()
            if candidate is not None:
                diagnostics["image_assessment"] = last_assessment
                return candidate, attempted, method, diagnostics
        return None, True, "none", {"image_assessment": last_assessment}

    def _event_context_places(self, event_id: str) -> tuple[str, ...]:
        event = intel_store.get(event_id)
        if event is None:
            return ()
        return tuple(event.metadata.get("context_place_names") or ())

    def _event_sea_snap(self, event_id: str) -> bool:
        event = intel_store.get(event_id)
        if event is None:
            return True
        meta = event.metadata
        return (
            meta.get("humanitarian_case_type") != "land_humanitarian"
            and meta.get("location_status") != "withheld_from_maritime_map"
        )

    def _capture_media_evidence(
        self, event_id: str, urls: list[str], *, method: str | None,
        coords: Optional[tuple[float, float]], ocr_diag: dict[str, Any],
    ) -> None:
        """Durably store the source images + record the normalized evidence
        object and the explicit outcome bucket (docs/prompt.md P1)."""
        try:
            from core.intel.media_evidence import (
                capture_media_evidence,
                classify_media_outcome,
            )

            stored = intel_store.get(event_id)
            source_url = stored.url if stored is not None else None
            engine = "easyocr" if str(method or "").startswith("easyocr") else "tesseract"
            evidence = capture_media_evidence(
                urls,
                source_url=source_url,
                ocr_method=method if coords is not None else None,
                ocr_coord=coords,
                ocr_engine=engine if coords is not None else None,
                interengine_distance_m=ocr_diag.get("interengine_distance_m"),
            )
            intel_store.update_metadata(event_id, metadata={
                "media_evidence": [e.as_dict() for e in evidence],
                "media_outcome": classify_media_outcome(evidence, coords, method),
            })
        except Exception as exc:  # pragma: no cover - evidence capture is best effort
            logger.debug("media evidence capture failed for %s: %s", event_id, exc)

    def _apply_media_ocr(self, event_id: str, urls: list[str]) -> None:
        """Run in a pool worker: OCR, upgrade the stored position, drift."""
        from core.intel.location_evidence import (
            evidence_from_ocr_method,
            ocr_result_label,
        )
        from core.observability import record_ocr_result

        try:
            coords, attempted, method, ocr_diag = self._ocr_tweet_media(
                event_id,
                urls,
                context_places=self._event_context_places(event_id),
                sea_snap=self._event_sea_snap(event_id),
            )
            self._capture_media_evidence(
                event_id, urls, method=method, coords=coords, ocr_diag=ocr_diag,
            )
            if coords is None:
                record_ocr_result("no_coordinate")
                # Visible in prod logs: was OCR even possible, and did it run
                # but fail to read a coordinate? (Alarm Phone posts the
                # position as a map screenshot -- a silent miss here is a
                # lost distress location.)
                import shutil

                logger.warning(
                    "media OCR: no coordinate for %s (images=%d, tesseract=%s, attempted=%s)",
                    event_id, len(urls), bool(shutil.which("tesseract")), attempted,
                )
                image_assessment = ocr_diag.get("image_assessment") or {}
                if attempted or image_assessment:
                    intel_store.update_metadata(
                        event_id,
                        metadata={"ocr_attempted": bool(attempted), **image_assessment},
                    )
                self._auto_drift_if_live(event_id, force=False)
                return
            logger.info(
                "media OCR: %s -> %.5f,%.5f via %s for %s",
                "coordinate", coords[0], coords[1], method, event_id,
            )
            # OCR-method -> evidence semantics live in one place now
            # (core.intel.location_evidence), shared with the historical
            # backfill so the two can never drift apart again (F-04 / F-05).
            evidence = evidence_from_ocr_method(
                method,
                coords[0],
                coords[1],
                interengine_distance_m=ocr_diag.get("interengine_distance_m"),
                estimated_position_error_m=ocr_diag.get("estimated_position_error_m"),
            )
            record_ocr_result(ocr_result_label(method))
            upgraded = intel_store.enrich_location(
                event_id,
                lat=coords[0],
                lon=coords[1],
                metadata={
                    **evidence.as_metadata(),
                    **(ocr_diag.get("image_assessment") or {}),
                    "media_transport": "x_media_ocr",
                    "ocr_attempted": True,
                    "media_count": len(urls),
                    # The image just moved the position; any drift that ran off
                    # the earlier fallback point is stale. Mark it superseded
                    # atomically with the new coordinate so a process restart
                    # between the two async writes can never leave an old
                    # completed drift pinned to the new position. Whether a
                    # fresh drift actually runs is decided next, by the one
                    # F-01 eligibility gate (a disputed read / land coordinate
                    # is turned away there, and marked ineligible).
                    "drift_status": "superseded",
                },
            )
            if not upgraded:
                # A better source (e.g. explicit text coords) is already stored;
                # do not clobber it, but still ensure a drift is requested.
                self._auto_drift_if_live(event_id, force=False)
                return
            # Recompute the drift from the corrected position. The gate decides
            # eligibility: a single-engine OCR coordinate that lands in the sea
            # is a valid origin (policy /2); a disputed read or a land
            # coordinate is rejected and recorded, not silently dropped.
            self._auto_drift_if_live(event_id, force=True)
            upgraded_event = intel_store.get(event_id)
            if upgraded_event is not None:
                attach_forensic_packet(upgraded_event)
        except Exception as exc:
            logger.debug("X (twikit) media OCR enrichment failed for %s: %s", event_id, exc)

    def _schedule_media_ocr(self, tweet_id: str, event_id: str, urls: list[str]) -> None:
        # docs/fixes.md F-02: one bounded pool, deduped by event identity --
        # a media burst can no longer pile up unbounded waiting threads.
        from core.intel.media_ocr_queue import media_ocr_queue

        urls_snapshot = list(urls)
        outcome = media_ocr_queue.submit(
            f"x-ocr:{event_id}",
            lambda: self._apply_media_ocr(event_id, urls_snapshot),
        )
        if outcome in {"deferred_queue_full", "dropped"}:
            intel_store.update_metadata(
                event_id,
                metadata={"ocr_queue_state": outcome, "ocr_attempted": False},
            )

    def _schedule_media_ocr_shadow(self, tweet_id: str, event_id: str, urls: list[str]) -> None:
        """Analyze media without enriching, publishing, notifying, or drifting."""
        from core.intel.media_ocr_queue import media_ocr_queue
        from core.observability import record_ocr_result

        urls_snapshot = list(urls)

        def run_shadow() -> None:
            coords, attempted, method, diagnostics = self._ocr_tweet_media(
                event_id,
                urls_snapshot,
                context_places=self._event_context_places(event_id),
                sea_snap=self._event_sea_snap(event_id),
            )
            result = "shadow_coordinate" if coords is not None else (
                "shadow_no_coordinate" if attempted else "shadow_not_attempted"
            )
            record_ocr_result(result)
            intel_store.update_metadata(
                event_id,
                metadata={
                    "image_assessment_shadow": {
                        "result": result,
                        "method": method,
                        "coordinate": list(coords) if coords else None,
                        **(diagnostics.get("image_assessment") or {}),
                    }
                },
            )
            logger.info(
                "media OCR shadow: event=%s images=%d attempted=%s result=%s method=%s",
                event_id, len(urls_snapshot), attempted, result, method,
            )

        outcome = media_ocr_queue.submit(f"x-ocr-shadow:{event_id}", run_shadow)
        if outcome in {"deferred_queue_full", "dropped"}:
            logger.warning("media OCR shadow queue: event=%s state=%s", event_id, outcome)

    def _auto_drift_if_live(self, event_id: str, *, force: bool = False) -> None:
        """Auto-drift for a new live episode, unless one already ran (or is running).

        `force=True` is used after an OCR position upgrade: the previously
        scheduled drift (if any) used the weaker fallback coordinates, so the
        shared model slot is unblocked and the drift is recomputed.
        """
        if not config.INTEL_AUTO_DRIFT_ENABLED:
            return
        stored = intel_store.get(event_id)
        if stored is None or stored.lat is None or stored.lon is None:
            return
        if stored.metadata.get("drift_status") in {"computing", "completed"} and not force:
            return
        # Pre-flight the F-01 evidence gate here too: a disputed / unverified /
        # region-only OCR result must produce exactly zero drift requests, not
        # a request the route then rejects.
        from core.intel.drift_service import is_auto_drift_eligible

        eligible, reason = is_auto_drift_eligible(stored)
        if not eligible:
            logger.info("X (twikit) auto-drift not eligible for %s: %s", event_id, reason)
            from core.observability import record_ocr_drift_rejected

            record_ocr_drift_rejected()
            intel_store.update_metadata(
                event_id,
                metadata={"drift_status": "ineligible", "drift_ineligible_reason": reason},
            )
            return
        try:
            request_auto_drift(stored.id, stored.lat, stored.lon, vessel_type="rubber_boat")
        except Exception as exc:
            logger.debug("X (twikit) auto-drift deferred for %s: %s", event_id, exc)

    def _record_source_observation(self, tweet: Any, handle: str) -> None:
        """docs/fixes.md M1.2: a durable, lossless SourceObservation for
        every tweet this monitor receives (Alarm Phone and every other
        tracked account sharing this same _ingest path) -- independent of
        how the classification/threading logic below routes it (a brand
        new incident, a repost/quote/reply thread, a translation twin, or
        dropped as too short). Best-effort and strictly additive: never
        raises into _ingest, never alters what gets classified/published.
        The existing intel_store.add() write path below remains
        authoritative until a parity comparison (a later PR) proves this
        envelope is equivalent -- this does not replace or touch it.
        """
        try:
            from core.db.session import session_scope
            from core.intel.source_observation import record_observation

            text = str(getattr(tweet, "text", "") or "")
            with session_scope() as db:
                record_observation(
                    db,
                    # Not yet classified at this point in _ingest (distress/
                    # resolved is computed further down) -- "review" is the
                    # pre-triage humanitarian lane, not a guess at the real one.
                    service="humanitarian",
                    lane="review",
                    observation_type="source_post",
                    source_name=handle,
                    source_policy="official_api",
                    source_id=str(tweet.id),
                    observed_at=self._timestamp(tweet),
                    raw_payload=text,
                    source_url=f"https://x.com/i/web/status/{tweet.id}",
                )
        except Exception as exc:
            logger.debug("twikit_monitor: source_observation record skipped for %s: %s", tweet.id, exc)

    def _record_media_source_observations(
        self, media_urls: list[str], *, tweet_id: str, handle: str, observed_at: str,
    ) -> None:
        """docs/fixes.md M1.2: a durable SourceObservation per attached
        media item (the 8th, final M1.2 source -- the raw evidence an
        Alarm Phone/tracked-account map-screenshot image *is*, distinct
        from the tweet's own text captured by _record_source_observation
        above). This is the RAW OBSERVATION layer only: the media URL the
        source actually attached, not what OCR later extracts from it --
        OCR is a DETERMINISTIC FEATURE/EXTRACTION step downstream in the
        canonical flow and stays exactly where it already runs
        (_apply_media_ocr), untouched by this.

        Idempotent by (source_name, media URL): the same image re-served
        under a repost/quote resolves to one observation, not a duplicate
        -- unlike the parent tweet's source_id (the tweet id), a media URL
        is itself the source's own stable identifier for that asset.
        Batched into one session per tweet (typically 1-4 images).
        Best-effort and strictly additive: never touches OCR, threading,
        or classification below.
        """
        if not media_urls:
            return
        try:
            from core.db.session import session_scope
            from core.intel.source_observation import record_observation

            with session_scope() as db:
                for url in media_urls:
                    record_observation(
                        db,
                        service="humanitarian",
                        lane="review",
                        observation_type="media_attachment",
                        source_name=handle,
                        source_policy="official_api",
                        source_id=url[:256],
                        observed_at=observed_at,
                        raw_payload=url,
                        source_url=url,
                        subject_refs=[f"tweet:{tweet_id}"],
                    )
        except Exception as exc:
            logger.debug(
                "twikit_monitor: media source_observation record skipped for %s: %s", tweet_id, exc
            )

    def _ingest(self, tweet: Any, handle: str) -> bool:
        self._record_source_observation(tweet, handle)
        original = getattr(tweet, "retweeted_tweet", None)
        if original is not None and getattr(original, "id", None) is not None:
            return self._thread_repost(tweet, handle, original, kind="repost")

        quoted = self._quoted_tweet(tweet)
        if quoted is not None:
            quoted_parent = intel_store.find_by_tweet_id(str(quoted.id))
            if quoted_parent is not None:
                # Amplifying an already-tracked incident — thread it like a
                # repost (no new marker, no re-broadcast), but keep the
                # caption: unlike a plain RT a quote often carries new
                # operational info (e.g. "confirmed rescued").
                return self._thread_repost(tweet, handle, quoted, kind="quote")

        # A direct reply to an already-tracked incident is a case update, not
        # a new independent incident (docs/deep-research-report.md #5/#7/#21:
        # "reply: very strong case linkage" / "correlation_status = linked").
        # Cross-account, unlike _check_self_replies below which only threads
        # an account's replies to its OWN earlier tweet -- this is the hard
        # graph relationship (X's own in_reply_to edge), so it never needs
        # the probabilistic scoring reserved for content that has no shared
        # tweet/thread at all. Deliberately does NOT touch the parent's
        # lifecycle/severity from the reply's content -- unlike a self-reply,
        # an arbitrary replying account's claim ("rescued!") is not
        # confirmation; it only becomes visible correlated evidence on the
        # incident's timeline.
        in_reply_to_id = str(getattr(tweet, "in_reply_to", "") or "")
        if in_reply_to_id and quoted is None:
            reply_parent = intel_store.find_by_tweet_id(in_reply_to_id)
            if reply_parent is not None:
                return self._thread_reply(tweet, handle, reply_parent)

        own_text = str(getattr(tweet, "text", "") or "")
        quoted_text = str(getattr(quoted, "text", "") or "") if quoted is not None else ""
        # The real distress content (and its GPS map screenshot) often lives
        # in the quoted tweet, not the tracked account's short caption — feed
        # both to distress/geo extraction so a terse "🆘" caption quoting a
        # full report is never silently dropped or mis-classified.
        combined_text = f"{own_text}\n{quoted_text}".strip() if quoted_text else own_text
        if len(combined_text) < 10:
            return False

        distress = is_direct_distress_call(combined_text)
        resolved = is_resolved_distress(combined_text)
        severity = classify_severity(combined_text) if distress else "low"
        author = ""
        try:
            user = tweet.user
            author = str(getattr(user, "screen_name", "") or "")
        except Exception:
            pass

        # Alarm Phone (and the tracked SAR NGOs) publish the real GPS position
        # as a map screenshot. Priority: explicit text coords > OCR of attached
        # images > declared relative offset > place-name centroid.
        text_coords = extract_numeric_coords(combined_text) if distress else None
        from core.intel.x_media import resolve_x_media

        media_resolution = resolve_x_media(
            tweet,
            str(tweet.id),
            quoted,
            allow_syndication=config.X_MEDIA_SYNDICATION_FALLBACK,
        )
        media_urls = media_resolution.urls
        self._record_media_source_observations(
            media_urls, tweet_id=str(tweet.id), handle=handle, observed_at=self._timestamp(tweet),
        )

        # A translated / same-language re-issue of an incident already tracked
        # (Alarm Phone posts EN + FR minutes apart, and a text-only alert then
        # one carrying the map) must thread onto that incident, not raise a
        # second marker for one boat (docs/fixes.md sec 2 / sec 7). Skipped for
        # a quote/reply -- those are already routed above by their hard edge.
        if quoted is None and not in_reply_to_id:
            twin = self._translation_twin(combined_text, handle, distress)
            if twin is not None:
                return self._thread_translation(tweet, twin, own_text, media_urls)
        media_count = len(media_urls)
        ocr_pending = False
        ocr_shadow_pending = False
        ocr_available = bool(
            shutil.which("tesseract") or importlib.util.find_spec("easyocr") is not None
        )
        handle_tracked_for_images = handle.lower() in _tracked_image_accounts()
        alarm_phone_image_v2 = (
            handle_tracked_for_images and config.ALARM_PHONE_IMAGE_V2_ENABLED
        )
        if (distress or alarm_phone_image_v2) and not text_coords and media_count:
            if ocr_available:
                ocr_pending = True
            else:
                # The real GPS position is almost certainly in the images but the
                # OCR engine is not installed (e.g. Docker image built without
                # tesseract) — say so instead of silently keeping a centroid.
                logger.warning(
                    "X (twikit) distress tweet %s has %d media image(s) but tesseract "
                    "is not installed; image OCR skipped (coordinate falls back to "
                    "text/place extraction)",
                    tweet.id,
                    media_count,
                )
        elif (
            media_count
            and not text_coords
            and handle_tracked_for_images
            and config.ALARM_PHONE_IMAGE_V2_SHADOW
            and ocr_available
        ):
            ocr_shadow_pending = True
        media_coords: Optional[tuple[float, float]] = None
        relative_coords = (
            extract_relative_coords(combined_text)
            if distress and not text_coords else None
        )
        place_coords = (
            extract_coords(combined_text)
            if distress and not (text_coords or relative_coords) else None
        )
        # A country/sea/strait-scale match ("Libya", "Central Med") implies a
        # far larger "could be anywhere in here" than a specific city or
        # small island does — reporting both at the same flat radius made
        # the map draw an equally-tight-looking circle for both, which reads
        # as false precision for the coarse case.
        place_precision = place_match_precision(combined_text) if place_coords else None
        timestamp_utc = self._timestamp(tweet)

        # No source better than a bare place match: rather than a single
        # (possibly arbitrary) point, follow what the report actually names
        # — a real sea-only search polygon, narrowed by wave data only if
        # the report itself claims rough weather. area_result stays None
        # (falling back to the plain centroid below) whenever it can't
        # build an honest area — never a fabricated one.
        area_result = None
        if place_coords and not (text_coords or relative_coords):
            try:
                report_time = datetime.fromisoformat(timestamp_utc)
            except ValueError:
                report_time = None
            area_result = extract_area(combined_text, report_time=report_time)

        coords = (
            text_coords or media_coords or relative_coords
            or (area_result.centroid if area_result else place_coords)
        )
        coordinate_source = (
            "post_text" if text_coords
            else "media_ocr_text" if media_coords
            else "relative_place_offset" if relative_coords
            else "region_area" if area_result
            else "place_centroid" if place_coords
            else "none"
        )
        location_uncertainty_m = (
            250 if text_coords
            else 1500 if media_coords
            else 15_000 if relative_coords
            else None if area_result  # the polygon itself is the uncertainty
            else (120_000 if place_precision == "imprecise" else 25_000) if place_coords
            else None
        )

        caption_place_names = [
            name for name, _coords, _tier in find_all_place_matches(combined_text)
        ][:10]

        # Prefer the tracked account's own words for the title/display text;
        # fall back to the combined text only when the caption alone is too
        # thin to summarize (e.g. a bare "🆘" quoting a full report).
        title_source = own_text if len(own_text.strip()) >= 10 else combined_text
        display_text = own_text if own_text.strip() else combined_text

        event = IntelEvent(
            type="twitter",
            severity=severity,
            lat=coords[0] if coords else None,
            lon=coords[1] if coords else None,
            title=_make_title(title_source, author or handle),
            text=display_text[:500],
            url=f"https://x.com/i/web/status/{tweet.id}",
            source=author or handle,
            author=author or handle,
            timestamp_utc=timestamp_utc,
            metadata={
                "tweet_id": str(tweet.id),
                **humanitarian_case_metadata(
                    combined_text,
                    incident_id=str(tweet.id),
                    source=handle,
                    distress=distress,
                    resolved=resolved,
                ),
                **({
                    "area_geojson": area_result.polygon,
                    "area_confidence": area_result.confidence,
                    "area_weather_narrowed": area_result.weather_narrowed,
                } if area_result else {}),
                "platform": "x",
                "source_policy": "operator_published" if distress else "unofficial",
                "publication_status": "published" if distress else "private",
                "is_distress": distress,
                "report_kind": "distress" if distress else ("resolved" if resolved else "news"),
                "distress_classification": (
                    "direct_call" if distress else ("resolved" if resolved else "context")
                ),
                "coordinate_source": coordinate_source,
                "location_policy": "operational_maritime_only",
                **({
                    "location_suppressed_reason": "non_operational_context",
                } if not distress else {}),
                "coordinate_review_status": (
                    "not_required"
                    if coordinate_source in {"post_text", "navtext"}
                    else "machine_ocr_unverified"
                    if coordinate_source == "media_ocr_text"
                    else "not_applicable"
                ),
                # Credit the specific tracked X/Twitter account by name rather than
                # bucketing it under a generic trust tier — e.g. "alarm_phone_twitter",
                # shown in the UI (underscores stripped) as "alarm phone twitter".
                "verification_status": (
                    "machine_extracted_unverified"
                    if coordinate_source == "media_ocr_text"
                    else f"{handle}_twitter"
                ),
                "location_uncertainty_m": location_uncertainty_m,
                "media_count": media_count,
                **media_resolution.as_diagnostics(),
                # Kept so a later re-process (core.intel.backfill_alarm_phone)
                # never has to resolve the tweet again.
                **({"media_urls": media_urls[:6]} if media_urls else {}),
                "media_transport": (
                    "x_media_ocr" if ocr_pending
                    else "x_media_ocr_shadow" if ocr_shadow_pending
                    else "none"
                ),
                "ocr_attempted": False,
                **({"context_place_names": caption_place_names} if caption_place_names else {}),
                "provenance": "twikit_account_timeline",
                "tracked_account": handle,
                "quoted_tweet_id": str(quoted.id) if quoted is not None else None,
                "quoted_tweet_url": (
                    f"https://x.com/i/web/status/{quoted.id}" if quoted is not None else None
                ),
            },
        )
        added = intel_store.add(event, dedup_key=f"x:{tweet.id}")
        if added and self._alerts_enabled:
            self._notify(event)
        if added and distress:
            attach_forensic_packet(event)
        if ocr_pending and added:
            # The real position is almost certainly in the images — OCR in a
            # background thread, then upgrade the stored position and drift on
            # the corrected coordinates (never block the 45 s poll loop).
            self._schedule_media_ocr(str(tweet.id), event.id, media_urls)
        elif ocr_shadow_pending and added:
            self._schedule_media_ocr_shadow(str(tweet.id), event.id, media_urls)
        elif not added:
            # Duplicate content_hash — `event` above was never stored, so its
            # id is a throwaway UUID; recover the real stored event first.
            existing = (
                intel_store.find_by_content_hash(event.content_hash())
                or intel_store.find_by_source_url(event.source, event.url)
            )
            if existing is not None:
                if str(existing.metadata.get("tweet_id") or "") != str(tweet.id):
                    # The tracked account deleted its earlier post and reposted
                    # near-identical text (same content_hash, new tweet id) —
                    # without this the stored event keeps linking to a dead
                    # status forever, which reads to users as "never updated".
                    intel_store.refresh_source_link(
                        existing.id,
                        tweet_id=str(tweet.id),
                        url=f"https://x.com/i/web/status/{tweet.id}",
                    )
                if coords:
                    # Re-seen tweet (this or another collector) carrying a
                    # better position — enrich instead of creating a dup.
                    location_metadata = {
                        key: value
                        for key, value in event.metadata.items()
                        if key not in {"first_source_seen_at", "last_source_seen_at", "source_scan_count"}
                    }
                    intel_store.enrich_location(
                        existing.id,
                        lat=coords[0],
                        lon=coords[1],
                        metadata=location_metadata,
                    )
        if (
            added
            and distress
            and config.INTEL_AUTO_DRIFT_ENABLED
            and event.lat is not None
            and event.lon is not None
            and event.metadata.get("drift_status") not in {"computing", "completed"}
            and not ocr_pending
            and area_result is None
        ):
            # With OCR pending the drift fires from the worker once the image
            # position is known, so the cone never uses a centroid fallback.
            # An area result has no single defensible starting point at all —
            # a leeway simulation from its centroid would imply a false
            # precision the polygon itself exists specifically to avoid.
            # Route through the gated helper so the same F-01 evidence policy
            # applies to this inline path.
            self._auto_drift_if_live(event.id, force=False)
        return added

    def _thread_repost(
        self, repost: Any, handle: str, original: Any, *, kind: str = "repost"
    ) -> bool:
        """A repost/quote must answer the SAME thread, never spawn a new marker.

        If the original alert is already tracked in the store, the repost is
        recorded onto that incident's thread bookkeeping without touching any
        field the live edge publishes (no version bump, no marker update). If
        the original was never seen, the original content is ingested so the
        signal is not lost.

        A quote tweet (``kind="quote"``) additionally carries the quoting
        account's own caption, which — unlike a plain RT — can be new
        operational information (e.g. "confirmed rescued"), so it is kept as
        a ``note`` on the thread record instead of being discarded.
        """
        original_id = str(getattr(original, "id", "") or "")
        repost_id = str(getattr(repost, "id", "") or "")
        if not original_id:
            return False

        parent = intel_store.find_by_tweet_id(original_id)
        if parent is None:
            return self._ingest(original, handle)

        record: dict[str, Any] = {
            "tweet_id": repost_id,
            "posted_at": self._timestamp(repost),
            "url": f"https://x.com/i/web/status/{repost_id}",
            "kind": kind,
        }
        if kind == "quote":
            note = str(getattr(repost, "text", "") or "").strip()
            if note:
                record["note"] = note[:500]
        added = intel_store.append_thread_repost(parent.id, record)
        if added:
            logger.info(
                "X (twikit) %s %s by @%s threaded onto incident %s (no new event)",
                kind,
                repost_id,
                handle,
                parent.id,
            )
        return False

    def _translation_twin(
        self, combined_text: str, handle: str, distress: bool
    ) -> Optional[IntelEvent]:
        """The already-stored incident this post is a re-issue of, if any."""
        try:
            from core.intel.translation_dedup import find_translation_twin

            recent = intel_store.events(type_filter="twitter", max_age_days=1)
            return find_translation_twin(
                combined_text,
                handle=handle,
                distress=distress,
                now=datetime.now(UTC),
                candidates=recent,
            )
        except Exception as exc:  # never let dedup block ingestion
            logger.debug("X (twikit) translation-twin check failed: %s", exc)
            return None

    def _thread_translation(
        self,
        tweet: Any,
        parent: IntelEvent,
        own_text: str,
        media_urls: list[str],
    ) -> bool:
        """Fold a translated / near-duplicate re-post onto its canonical incident.

        No new marker. If this copy carries the map screenshot and the parent
        is still on a place-name fallback, hand the media to the parent so the
        one surviving incident gets the real position (docs/fixes.md sec 2).
        """
        tweet_id = str(getattr(tweet, "id", "") or "")
        if not tweet_id:
            return False
        record: dict[str, Any] = {
            "tweet_id": tweet_id,
            "posted_at": self._timestamp(tweet),
            "url": f"https://x.com/i/web/status/{tweet_id}",
            "kind": "translation",
        }
        note = str(own_text or "").strip()
        if note:
            record["note"] = note[:500]
        intel_store.append_thread_repost(parent.id, record)

        parent_source = str(parent.metadata.get("coordinate_source") or "")
        parent_has_point = parent_source in {
            "post_text", "media_ocr_text", "media_ocr_consensus", "media_pin_landmark",
        }
        if media_urls and not parent_has_point:
            known = list(parent.metadata.get("media_urls") or [])
            merged = known + [u for u in media_urls if u not in known]
            intel_store.update_metadata(parent.id, metadata={"media_urls": merged[:6]})
            if (
                config.ALARM_PHONE_IMAGE_V2_ENABLED
                and (
                    shutil.which("tesseract")
                    or importlib.util.find_spec("easyocr") is not None
                )
            ):
                self._schedule_media_ocr(tweet_id, parent.id, media_urls)
        logger.info(
            "X (twikit) %s folded onto incident %s as a translated duplicate "
            "(no new marker)",
            tweet_id,
            parent.id,
        )
        return False

    def _thread_reply(self, reply: Any, handle: str, parent: IntelEvent) -> bool:
        """A direct reply to an already-tracked incident is a case update.

        Threaded onto the parent's record exactly like a quote (no new
        marker, no re-broadcast, no independent lifecycle) -- the reply's own
        text becomes the operational note, since (unlike a quote) there is no
        separate quoted body to combine it with.
        """
        reply_id = str(getattr(reply, "id", "") or "")
        if not reply_id:
            return False
        record: dict[str, Any] = {
            "tweet_id": reply_id,
            "posted_at": self._timestamp(reply),
            "url": f"https://x.com/i/web/status/{reply_id}",
            "kind": "reply",
        }
        note = str(getattr(reply, "text", "") or "").strip()
        if note:
            record["note"] = note[:500]
        added = intel_store.append_thread_repost(parent.id, record)
        if added:
            logger.info(
                "X (twikit) reply %s by @%s threaded onto incident %s (no new event)",
                reply_id,
                handle,
                parent.id,
            )
        return False

    async def _check_self_replies(self, client) -> None:
        """Thread delayed reply updates (any author) onto still-active incidents.

        Verified live: a tracked account's own reply to its own earlier
        tweet ("Rescued to #Lampedusa! ...") never appears via
        user.get_tweets("Tweets"/"Replies", ...) at the top level — X
        excludes it from both. It DOES appear nested under the original
        tweet's own `.replies`, but only when that tweet is reached via the
        account's own timeline: X groups a tweet together with the author's
        own reply-thread as a single "profile-conversation" module there,
        and twikit exposes it as a plain, already-complete list.

        get_tweet_by_id(...).replies looked like an equivalent shortcut (one
        fetch per event, no full timeline scan) but isn't: verified live
        against a real 3-reply thread (self, stranger, self) that its cursor
        cuts off after 2 replies and never surfaces the 3rd via `.next()` —
        silently dropping the actual resolution reply. The timeline path
        does not have this gap, so re-walking each tracked account's recent
        timeline (already fetched every poll anyway) is used instead.

        Threads EVERY reply on the tracked tweet, not just same-author ones
        (explicit user follow-up: "i reply sono agli stessi tweet quindi
        vanno ricontrollati ogni tot" -- a real-time cross-account reply is
        already caught in _ingest()/_thread_reply(), but a case can stay
        "active" for days and a cross-account reply arriving on an
        already-processed old tweet needs this periodic re-walk to ever be
        seen at all, same as a delayed self-reply). Correlated evidence, not
        confirmation either way -- see _thread_own_replies.

        Also self-audits: any still-unresolved candidate whose own tweet
        does not turn up in its account's fetched timeline — old enough that
        it should have, if the tweet were still reachable — likely means the
        tweet was deleted with no matching repost (so the ingestion-side
        dead-link fix never had anything to latch onto). That case can't be
        auto-healed here, so it's surfaced instead of silently sitting wrong
        on the live map — see _flag_unreachable_tweets().
        """
        in_memory = intel_store.events(type_filter="twitter", max_age_days=7)
        durable = intel_store.persisted_events(types=["twitter"], max_age_days=7, limit=None)
        by_event_id = {event.id: event for event in durable}
        by_event_id.update({event.id: event for event in in_memory})
        candidates = [
            event
            for event in by_event_id.values()
            if event.metadata.get("is_distress") and event.metadata.get("tweet_id")
        ]
        if not candidates:
            return
        by_tweet_id = {str(event.metadata["tweet_id"]): event for event in candidates}
        handles = {
            str(event.metadata.get("tracked_account") or "")
            for event in candidates
        } - {""}
        seen_tweet_ids: set[str] = set()
        for handle in handles:
            user = self._users.get(handle)
            if user is None:
                try:
                    user = await client.get_user_by_screen_name(handle)
                except Exception as exc:
                    logger.debug("X (twikit) reply check: could not resolve @%s: %s", handle, exc)
                    continue
                self._users[handle] = user
            try:
                recent = await user.get_tweets("Tweets", count=_REPLY_CHECK_TIMELINE_SIZE)
            except Exception as exc:
                logger.debug("X (twikit) reply check: could not fetch @%s timeline: %s", handle, exc)
                continue
            for tweet in recent:
                tweet_id = str(tweet.id)
                seen_tweet_ids.add(tweet_id)
                event = by_tweet_id.get(tweet_id)
                if event is None:
                    continue
                replies = await self._reply_pages(getattr(tweet, "replies", None))
                self._thread_own_replies(event, tweet, replies)

        # Old but still active incidents fall outside the 40-item profile
        # window. Fetch those originals directly and follow Twikit's reply
        # cursor; otherwise a later resolution reply can remain invisible.
        for tweet_id, event in by_tweet_id.items():
            if tweet_id in seen_tweet_ids:
                continue
            try:
                tweet = await client.get_tweet_by_id(tweet_id)
            except Exception as exc:
                logger.debug("X (twikit) reply check: tweet %s unavailable: %s", tweet_id, exc)
                continue
            seen_tweet_ids.add(tweet_id)
            replies = await self._reply_pages(getattr(tweet, "replies", None))
            self._thread_own_replies(event, tweet, replies)
        unresolved = [
            event for tweet_id, event in by_tweet_id.items()
            if tweet_id not in seen_tweet_ids and not has_own_reply_resolution(event)
        ]
        self._flag_unreachable_tweets(unresolved)

    @staticmethod
    async def _reply_pages(result: Any, *, max_pages: int = 4) -> list[Any]:
        """Flatten the initial Twikit reply Result and its bounded cursors."""
        replies = list(result or [])
        page = result
        for _ in range(max_pages - 1):
            fetch_next = getattr(page, "next", None)
            if not callable(fetch_next):
                break
            try:
                page = await fetch_next()
            except Exception:
                break
            if not page:
                break
            replies.extend(list(page))
        return replies

    def _thread_own_replies(self, event: IntelEvent, original: Any, replies: list[Any]) -> None:
        """Thread every reply found under `original` onto `event`.

        Not restricted to the original author (docs/deep-research-report.md
        #5/#7/#21 + explicit user follow-up) -- a cross-account reply on an
        old already-tracked tweet is exactly as strong a hard-graph link as
        one on a fresh tweet (X's own in_reply_to edge), it just wasn't
        visible until this periodic re-walk fetched it. Same no-new-marker
        contract, same "correlated evidence, not confirmation" posture as
        _thread_reply (real-time path) -- neither ever mutates the parent's
        severity from reply content.
        """
        for reply in replies:
            reply_id = str(getattr(reply, "id", "") or "")
            if not reply_id:
                continue
            reply_text = str(getattr(reply, "text", "") or "").strip()
            record: dict[str, Any] = {
                "tweet_id": reply_id,
                "posted_at": self._timestamp(reply),
                "url": f"https://x.com/i/web/status/{reply_id}",
                "kind": "reply",
            }
            if reply_text:
                record["note"] = reply_text[:500]
            added = intel_store.append_thread_repost(event.id, record)
            if added:
                logger.info(
                    "X (twikit) reply %s threaded onto incident %s (periodic re-check)",
                    reply_id, event.id,
                )

    def _flag_unreachable_tweets(self, unresolved: list[IntelEvent]) -> None:
        """Alert once per incident when its own tweet vanished from the
        timeline before we ever saw a resolution for it.

        Most likely cause: the tracked account deleted the tweet without a
        matching repost (a genuine edit/retraction, not the delete+repost
        pattern the ingestion-side dead-link fix already self-heals). That
        case has no reply thread to recover a correction from, so it can't
        be fixed automatically — surfaced here instead of just staying wrong
        on the live map until someone happens to notice.
        """
        now = time.monotonic()
        for event in unresolved:
            if event.id in self._flagged_unreachable:
                continue
            age_s = _age_seconds(event.timestamp_utc)
            if age_s is None or age_s < _UNREACHABLE_MIN_AGE_S:
                continue
            self._flagged_unreachable.add(event.id)
            logger.warning(
                "X (twikit) incident %s (%s) no longer found in @%s's timeline and has no "
                "resolution reply — tweet may have been deleted without a repost; needs a "
                "manual check",
                event.id, event.url, event.metadata.get("tracked_account"),
            )
            try:
                from core.notifications import telegram
            except Exception:
                continue
            body = (
                f"⚠️ Possibile link morto — @{event.metadata.get('tracked_account')}\n"
                f"{event.title[:150]}\n"
                f"Il tweet originale non è più nella timeline e non risulta risolto.\n"
                f"🔗 {event.url}"
            )
            threading.Thread(
                target=telegram, args=(body,), daemon=True, name="intel-x-stale-link-alert",
            ).start()
        self._flagged_unreachable &= {event.id for event in unresolved}

    def _notify(self, event: IntelEvent) -> None:
        """Fire-and-forget Telegram notification when a tracked account posts."""
        try:
            from core.notifications import telegram
        except Exception:
            return
        kind = event.metadata.get("report_kind", "news")
        account = event.author or event.metadata.get("tracked_account", "X")
        if kind == "distress":
            header = f"🚨 DISTRESS — @{account}"
        elif kind == "resolved":
            header = f"✅ RESOLVED — @{account}"
        else:
            header = f"📰 @{account}"
        location = ""
        if event.lat is not None and event.lon is not None:
            location = f"\n🌍 {event.lat:.4f}, {event.lon:.4f}"
        body = (
            f"{header}\n"
            f"Severità: {event.severity.upper()}\n"
            f"{event.text[:220]}{'…' if len(event.text) > 220 else ''}"
            f"{location}\n"
            f"🔗 {event.url}"
        )
        threading.Thread(target=telegram, args=(body,), daemon=True, name="intel-x-twikit-alert").start()

    @staticmethod
    def _timestamp(tweet: Any) -> str:
        try:
            created = getattr(tweet, "created_at_datetime", None)
            if created:
                return created.isoformat()
        except Exception:
            pass
        try:
            created_raw = getattr(tweet, "created_at", "")
            if created_raw:
                parsed = datetime.strptime(created_raw, "%a %b %d %H:%M:%S %z %Y")
                return parsed.isoformat()
        except Exception:
            pass
        return datetime.now(UTC).isoformat()
