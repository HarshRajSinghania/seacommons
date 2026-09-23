import React, { useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import './styles.css';
import './ui/ui.css';
import './suez-theme.css';
import MapFloatingPanel from './components/ConePanel.jsx';
import ScenarioModal from './components/ScenarioModal.jsx';
import SimHistory from './components/SimHistory.jsx';
import IntelDashboard from './components/IntelDashboard.jsx';
import LayerToggles, { LAYER_GROUPS } from './components/LayerToggles.jsx';
import Legend from './components/Legend.jsx';
import AlertRail from './components/AlertRail.jsx';
import MdaPanel from './components/MdaPanel.jsx';
import {
  categoryColorExpression,
  categoryOf,
  classifyEventVisual,
  signalCategoryOf,
  INTEL_MAP_CATEGORIES,
  isAlarmPhoneSource,
} from './features/intel/categories.js';
import { mdaAnomalyColorExpression, mdaCategoryKey, MDA_ANOMALY_CATEGORIES } from './features/intel/mdaCategories.js';
import { AuthGate } from './auth.jsx';
import CasesWorkspace from './components/CasesWorkspace.jsx';
import CivilSarFleetPanel from './components/CivilSarFleetPanel.jsx';
import PlayCesium from './components/PlayCesium.jsx';
import UnrealPixelStream from './components/UnrealPixelStream.jsx';
import ArchiveTimeline from './components/ArchiveTimeline.jsx';
import { SettingsWorkspace, SystemWorkspace } from './components/SettingsWorkspace.jsx';
import { buildEnvironmentSnapshot, createScenario } from './simulation/contracts.js';
import { loadStoredSimulations, storeScenario } from './simulation/scenarioStore.js';
import { computeDriftInWorker } from './simulation/workerClient.js';
import { fetchJson } from './services/api/client.js';
import { useLiveFeed } from './hooks/useLiveFeed.js';
import { FEED_STATUS_LABEL, FEED_STATUS_TONE, liveSignalTotal } from './features/live/feedStatus.js';
import { mergeIntelDriftUpdate } from './features/live/normalize.js';
import { receiverChannelLabel } from './features/live/pipelineStatus.js';
import { initialLayerVisibility, layerVisibilityStorageKey } from './features/live/layerDefaults.js';
import { splitObservedTrackSegments } from './features/live/observedTrack.js';
import { vesselReportFeature } from './features/live/vesselVisual.js';
import { createVesselArrowImage } from './features/map/vesselMarker.js';
import { publicBasemapSource } from './features/map/publicBasemap.js';
import { createFallbackMap } from './features/map/fallbackRenderer.js';
import { mergeLiveDrifts } from './simulation/liveTracking.js';

// Short two-tone chime for a correlated OSINT alert. Web Audio only; silent
// when the operator has muted alerts (localStorage) or the browser blocks
// autoplay audio before a user gesture.
function playAlertBeep() {
  try {
    if (window.localStorage.getItem('seacommons_alert_mute') === '1') return;
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    const ctx = new Ctx();
    const gain = ctx.createGain();
    gain.gain.value = 0.0001;
    gain.connect(ctx.destination);
    [880, 1245].forEach((freq, i) => {
      const osc = ctx.createOscillator();
      osc.type = 'sine';
      osc.frequency.value = freq;
      osc.connect(gain);
      const t0 = ctx.currentTime + i * 0.16;
      osc.start(t0);
      gain.gain.setValueAtTime(0.0001, t0);
      gain.gain.exponentialRampToValueAtTime(0.22, t0 + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.15);
      osc.stop(t0 + 0.16);
    });
    window.setTimeout(() => ctx.close(), 600);
  } catch {
    /* audio unavailable — the banner + rail still surface the alert */
  }
}

// Real-world-scaled circle radius: interpolate exponentially with zoom so
// `location_uncertainty_m` (meters) renders as an actually-to-scale area
// rather than a fixed pixel size. cos(37°) ≈ 0.8 approximates the whole
// Mediterranean band well enough for a visual "this is imprecise" cue.
const METERS_TO_PX_RADIUS = [
  'interpolate', ['exponential', 2], ['zoom'],
  0, ['/', ['coalesce', ['get', 'location_uncertainty_m'], 0], 156543.03392 * 0.8],
  22, ['/', ['coalesce', ['get', 'location_uncertainty_m'], 0], (156543.03392 * 0.8) / Math.pow(2, 22)],
];

// Exact/low-uncertainty points render normally. A coordinate recovered from
// a source map pin remains a meaningful estimated centre even when its error
// radius is large, so it renders BOTH the centre marker and the uncertainty
// halo. Region-only centroids keep the halo-only treatment to avoid implying
// a precision the source never supplied.
const _PRECISE_POINT_FILTER = ['all',
  ['==', ['geometry-type'], 'Point'],
  ['any',
    ['<=', ['coalesce', ['get', 'location_uncertainty_m'], 0], 20000],
    ['in', ['coalesce', ['get', 'coordinate_source'], ''], ['literal', [
      'media_pin_landmark', 'media_ocr_consensus', 'media_ocr_text',
    ]]],
  ],
];

// CATEGORY determines colour; LIFECYCLE is only secondary styling.
// Humanitarian distress — Alarm Phone included — keeps its category colour
// (red) in EVERY lifecycle state. A resolved Alarm Phone does NOT turn green:
// "resolved" reads as a dimmer fill + a solid (non-dashed) outline + a badge.
// `visual_color` is the backend-assigned canonical category colour; the
// fallback is red because this source only ever carries humanitarian distress.
const LIFECYCLE_CORE_COLOR = ['coalesce', ['get', 'visual_color'], '#ff3b3b'];
const LIFECYCLE_PULSE_FILL = ['coalesce', ['get', 'visual_color'], '#ff3b3b'];
const LIFECYCLE_PULSE_STROKE = ['coalesce', ['get', 'visual_color'], '#ff3b3b'];
const LIFECYCLE_AREA_FILL = ['coalesce', ['get', 'visual_color'], '#ff3b3b'];
const LIFECYCLE_AREA_STROKE = ['coalesce', ['get', 'visual_color'], '#ff3b3b'];
// Lifecycle -> secondary styling only (opacity + outline dash), never hue.
const LIFECYCLE_FILL_OPACITY = ['match', ['get', 'incident_lifecycle'],
  'resolved', 0.16, 'archived', 0.10, 'needs_review', 0.34, 0.4];
const LIFECYCLE_STROKE_OPACITY = ['match', ['get', 'incident_lifecycle'],
  'resolved', 0.7, 'archived', 0.5, 0.92];
const LIFECYCLE_OUTLINE_DASH = ['match', ['get', 'incident_lifecycle'],
  'resolved', ['literal', [1, 0]], 'archived', ['literal', [1, 0]], ['literal', [3, 2]]];

const PUBLIC_DEMO_HOSTS = new Set(['play.seacommons.org', 'demo.seacommons.org']);
const LIVE_HOSTS = new Set(['live.seacommons.org', 'console.seacommons.org', 'engine.seacommons.org']);
// The public Live map only ever fetches data for these layer groups (see the
// ngo-vessels/platforms effects and loadWeatherGridForMap's isPublicLiveHost
// guard) — weather/MDA/archive layers stay hidden. Raw AIS belongs to the
// operator console, not the public signal field: Live stays case-first.
// Humanitarian and Security used to be two exclusive bundles the mode switch
// swapped between; the per-category Signals selector now shows/hides each of
// these individually, so the allow-list is their union -- always available,
// never gated by a mode.
const PUBLIC_LIVE_LAYER_GROUPS = new Set([
  'nautical', 'sar', 'fused', 'observed_tracks', 'drift_models', 'simulation', 'ngo_vessels', 'platforms', 'spikes',
  'radio_receivers', 'radio_dsc',
  // Classified public cases render through dedicated intel-cat-* layers.
  // Keep raw AIS excluded, but allow every stable OSINT category layer.
  ...INTEL_MAP_CATEGORIES.map((category) => `intel_${category.key}`),
]);
// Signals selector: two macro groups (the original Humanitarian/Maritime
// Security split), each block-tickable and independently expandable to its
// own SIGNAL_CATEGORIES-level sub-toggles. vessel_incident ("unable to
// manoeuvre" etc.) sits under Humanitarian as safety context, not Security —
// see docs/prompt.md phase 4. Correlated alerts in this deployment are
// overwhelmingly sanctions/grey-zone (STS rendezvous, cable proximity,
// sanctioned-vessel matches), so that one sits under Security.
// Category `key`s here must stay in sync with the closed sets
// apps/api/core/domain/incident_taxonomy.py exports as
// STABLE_HUMANITARIAN_INCIDENT_TYPES / STABLE_MARITIME_INCIDENT_TYPES --
// that module's own property test is what keeps every `incident_type` the
// backend can produce inside this list.
const SIGNALS_MACRO_GROUPS = [
  {
    key: 'humanitarian',
    label: 'Humanitarian',
    categories: [
      { key: 'distress', label: 'Distress', groupKey: 'signal_type_distress' },
      { key: 'rescue', label: 'Rescue', groupKey: 'signal_type_rescue' },
      { key: 'missing', label: 'Missing', groupKey: 'signal_type_missing' },
      { key: 'shipwreck', label: 'Shipwreck', groupKey: 'signal_type_shipwreck' },
      { key: 'pushback', label: 'Pushback', groupKey: 'signal_type_pushback' },
      { key: 'migration_incident', label: 'Migration incident', groupKey: 'signal_type_migration_incident' },
      { key: 'sar_activity', label: 'SAR activity', groupKey: 'signal_type_sar_activity' },
      { key: 'land_humanitarian', label: 'Land humanitarian', groupKey: 'signal_type_land_humanitarian' },
      { key: 'resolution', label: 'Resolution', groupKey: 'signal_type_resolution' },
      { key: 'humanitarian_context', label: 'Humanitarian context', groupKey: 'signal_type_humanitarian_context' },
    ],
  },
  {
    key: 'maritime',
    label: 'Maritime',
    categories: [
      { key: 'ais_gap', label: 'AIS gaps', groupKey: 'signal_type_ais_gap' },
      { key: 'position_integrity', label: 'Position integrity anomalies', groupKey: 'signal_type_position_integrity' },
      { key: 'dark_activity', label: 'Dark activity hypotheses', groupKey: 'signal_type_dark_activity' },
      { key: 'spoofing', label: 'Spoofing hypotheses', groupKey: 'signal_type_spoofing' },
      { key: 'transfer', label: 'Transfers / rendezvous', groupKey: 'signal_type_transfer' },
      { key: 'loitering', label: 'Loitering', groupKey: 'signal_type_loitering' },
      { key: 'infrastructure_proximity', label: 'Infrastructure proximity', groupKey: 'signal_type_infrastructure' },
      { key: 'navigation_safety', label: 'Navigation safety', groupKey: 'signal_type_navigation_safety' },
      { key: 'identity_integrity', label: 'Identity integrity', groupKey: 'signal_type_identity_integrity' },
      { key: 'port_call', label: 'Port call', groupKey: 'signal_type_port_call' },
      { key: 'piracy_security', label: 'Piracy / security', groupKey: 'signal_type_piracy_security' },
      { key: 'environmental_hazard', label: 'Environmental hazard', groupKey: 'signal_type_environmental_hazard' },
      { key: 'context_report', label: 'Context report', groupKey: 'signal_type_context_report' },
      { key: 'public_observation', label: 'Public observation', groupKey: 'signal_type_public_observation' },
      { key: 'maritime_context', label: 'Maritime context', groupKey: 'signal_type_maritime_context' },
    ],
  },
];
// Flat list every other consumer (activeSignalCategories, per-category
// counts, "All") already works against — one shape, not two.
const SIGNALS_TOGGLE_CATEGORIES = SIGNALS_MACRO_GROUPS.flatMap((g) => g.categories);
const isPublicDemoHost = PUBLIC_DEMO_HOSTS.has(window.location.hostname);
const isPublicLiveHost = window.location.hostname === 'live.seacommons.org';
const isLiveHost = LIVE_HOSTS.has(window.location.hostname);
const APP_PROFILE = isLiveHost
  ? 'live'
  : isPublicDemoHost
    ? 'demo'
    : import.meta.env.VITE_APP_PROFILE === 'live'
      ? 'live'
      : 'demo';

// Cloudflare edge Live feed (core/live_edge_publisher.py -> apps/edge/src/live.js).
// Zero-cost, WebSocket-pushed alternative to polling the Oracle VM directly.
// Both paths share the exact same lifecycle policy (core/intel/lifecycle.py),
// so a converted edge event and a VM /api/v1/live/signals feature must be
// interchangeable to the rest of this file — same `kind`/`incident_lifecycle`
// semantics, same property names the map layers already filter/color on.
const LIVE_EDGE_BASE = String(import.meta.env.VITE_LIVE_EDGE_BASE || '').replace(/\/$/, '');

function enrichCaseGeo(geojson, lat, lon) {
  // Idempotent: replaying an already-enriched collection must not duplicate the origin marker.
  if (geojson.features?.some((f) => f.properties?.type === 'origin_point')) return geojson;
  // Provenance (product policy §3): a user-run simulation is tagged so it can
  // never be confused with a persisted operational Alarm Phone drift
  // (`auto_drift: true`), even though both render on the map.
  const tagged = (geojson.features || []).map((feature) => ({
    ...feature,
    properties: { ...(feature.properties || {}), trajectory_kind: 'user_simulation', auto_drift: false },
  }));
  return {
    ...geojson,
    features: [
      ...tagged,
      {
        type: 'Feature',
        geometry: { type: 'Point', coordinates: [Number(lon), Number(lat)] },
        properties: { type: 'origin_point', trajectory_kind: 'user_simulation' },
      },
    ],
  };
}

function guessApiBase() {
  const envBase = import.meta.env.VITE_API_BASE;
  if (envBase) return envBase.replace(/\/$/, '');
  const { protocol, hostname, port, origin } = window.location;
  const isLocal = hostname === 'localhost' || hostname === '127.0.0.1'
    || port === '3000' || port === '5173' || port === '4173';
  if (isLocal) {
    const saved = window.localStorage.getItem('seacommons_api_base');
    if (saved) return saved.replace(/\/$/, '');
    return `${protocol}//${hostname}:8000`;
  }
  if (hostname === 'demo.seacommons.org') return 'https://demo-api.seacommons.org';
  // Every other production host (including console/engine) goes through the
  // same-origin Vercel proxy (apps/web/api/proxy.js), which reaches the real
  // backend by its direct IP — api.seacommons.org's DNS record points at a
  // dead address, so calling it cross-origin from the browser fails even
  // though the backend itself is up. Ignore stale localStorage entries that
  // may point to a retired backend.
  return origin;
}

function loadLocalSettings() {
  return {
    timezeroHost: window.localStorage.getItem('seacommons_tz_host') || 'localhost',
    timezeroPort: window.localStorage.getItem('seacommons_tz_port') || '4371',
    timezeroEnabled: window.localStorage.getItem('seacommons_tz_enabled') || 'false',
  };
}

async function fetchOpenMeteoEnvironment(lat, lon) {
  const latitude = Number(lat);
  const longitude = Number(lon);
  const edgeBase = String(import.meta.env.VITE_EDGE_API_BASE || '').replace(/\/$/, '');
  if (edgeBase) {
    const edgeController = new AbortController();
    const edgeTimer = window.setTimeout(() => edgeController.abort(), 8000);
    try {
      const edgeUrl = new URL(`${edgeBase}/v1/environment`);
      edgeUrl.searchParams.set('lat', latitude.toFixed(5));
      edgeUrl.searchParams.set('lon', longitude.toFixed(5));
      const edgeResponse = await fetch(edgeUrl, { signal: edgeController.signal });
      if (!edgeResponse.ok) throw new Error(`SeaCommons Edge ${edgeResponse.status}`);
      const edgePayload = await edgeResponse.json();
      if (!Array.isArray(edgePayload.forecast_frames) || !edgePayload.forecast_frames.length) {
        throw new Error('SeaCommons Edge returned no forcing frames');
      }
      return edgePayload;
    } catch {
      // Direct provider access remains a no-Oracle fallback while the edge
      // gateway is unavailable or before it is deployed.
    } finally {
      window.clearTimeout(edgeTimer);
    }
  }
  const weatherUrl = new URL('https://api.open-meteo.com/v1/forecast');
  weatherUrl.searchParams.set('latitude', latitude.toFixed(4));
  weatherUrl.searchParams.set('longitude', longitude.toFixed(4));
  weatherUrl.searchParams.set(
    'current',
    'temperature_2m,relative_humidity_2m,apparent_temperature,is_day,precipitation,weather_code,cloud_cover,visibility,surface_pressure,wind_speed_10m,wind_direction_10m,wind_gusts_10m',
  );
  weatherUrl.searchParams.set('hourly', 'wind_speed_10m,wind_direction_10m,wind_gusts_10m');
  weatherUrl.searchParams.set('wind_speed_unit', 'ms');
  weatherUrl.searchParams.set('timezone', 'UTC');
  weatherUrl.searchParams.set('past_days', '2');
  weatherUrl.searchParams.set('forecast_days', '3');

  const marineUrl = new URL('https://marine-api.open-meteo.com/v1/marine');
  marineUrl.searchParams.set('latitude', latitude.toFixed(4));
  marineUrl.searchParams.set('longitude', longitude.toFixed(4));
  marineUrl.searchParams.set(
    'current',
    'wave_height,wave_direction,wave_period,sea_surface_temperature,ocean_current_velocity,ocean_current_direction',
  );
  marineUrl.searchParams.set(
    'hourly',
    'wave_height,wave_direction,wave_period,sea_surface_temperature,ocean_current_velocity,ocean_current_direction',
  );
  marineUrl.searchParams.set('timezone', 'UTC');
  marineUrl.searchParams.set('past_days', '2');
  marineUrl.searchParams.set('forecast_days', '3');

  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), 10000);
  try {
    const [weatherResponse, marineResponse] = await Promise.all([
      fetch(weatherUrl, { signal: controller.signal }),
      fetch(marineUrl, { signal: controller.signal }),
    ]);
    if (!weatherResponse.ok || !marineResponse.ok) {
      throw new Error(`Open-Meteo ${weatherResponse.status}/${marineResponse.status}`);
    }
    const [weatherPayload, marinePayload] = await Promise.all([
      weatherResponse.json(),
      marineResponse.json(),
    ]);
    const current = weatherPayload.current || {};
    const marine = marinePayload.current || {};
    const marineUnits = marinePayload.current_units || {};
    const currentVelocity = Number(marine.ocean_current_velocity);
    const currentSpeedMs = marineUnits.ocean_current_velocity === 'km/h'
      ? currentVelocity / 3.6
      : currentVelocity;
    const timestamp = String(current.time || marine.time || new Date().toISOString());
    const weatherHourly = weatherPayload.hourly || {};
    const marineHourly = marinePayload.hourly || {};
    const marineHourlyUnits = marinePayload.hourly_units || {};
    const marineIndexByTime = new Map(
      (marineHourly.time || []).map((time, index) => [time, index]),
    );
    const forecastFrames = (weatherHourly.time || []).map((time, weatherIndex) => {
      const marineIndex = marineIndexByTime.get(time);
      if (marineIndex === undefined) return null;
      const hourlyCurrent = Number(marineHourly.ocean_current_velocity?.[marineIndex]);
      const hourlyCurrentMs = marineHourlyUnits.ocean_current_velocity === 'km/h'
        ? hourlyCurrent / 3.6
        : hourlyCurrent;
      const values = [
        weatherHourly.wind_speed_10m?.[weatherIndex],
        weatherHourly.wind_direction_10m?.[weatherIndex],
        hourlyCurrentMs,
        marineHourly.ocean_current_direction?.[marineIndex],
        marineHourly.wave_height?.[marineIndex],
        marineHourly.wave_period?.[marineIndex],
        marineHourly.wave_direction?.[marineIndex],
      ].map(Number);
      if (!values.every(Number.isFinite)) return null;
      return {
        time_utc: `${time}:00Z`,
        wind: { speed_m_s: values[0], direction_deg: values[1] },
        current: { speed_m_s: values[2], direction_deg: values[3] },
        waves: {
          significant_height_m: values[4],
          period_s: values[5],
          direction_deg: values[6],
        },
      };
    }).filter(Boolean);

    return {
      timestamp_utc: timestamp.endsWith('Z') || timestamp.includes('+') ? timestamp : `${timestamp}:00Z`,
      source: 'Open-Meteo weather + marine best match',
      wind: {
        speed_ms: Number(current.wind_speed_10m),
        speed_kn: Number(current.wind_speed_10m) * 1.94384,
        direction_deg: Number(current.wind_direction_10m),
        gust_speed_ms: Number(current.wind_gusts_10m),
      },
      waves: {
        significant_height_m: Number(marine.wave_height),
        period_s: Number(marine.wave_period),
        direction_deg: Number(marine.wave_direction),
        direction_source: 'Open-Meteo marine model',
      },
      ocean: {
        water_temp_c: Number(marine.sea_surface_temperature),
        current_speed_ms: currentSpeedMs,
        current_dir_deg: Number(marine.ocean_current_direction),
      },
      air: {
        temp_c: Number(current.temperature_2m),
        apparent_temp_c: Number(current.apparent_temperature),
        humidity_pct: Number(current.relative_humidity_2m),
        pressure_hpa: Number(current.surface_pressure),
        visibility_km: Number(current.visibility) / 1000,
        cloud_cover_pct: Number(current.cloud_cover),
        precipitation_mm: Number(current.precipitation),
        weather_code: Number(current.weather_code),
        is_day: Number(current.is_day) === 1,
      },
      environmental_model: {
        kind: 'modelled-current-conditions',
        weather_resolution: 'best-match',
        marine_resolution: '5–9 km nominal',
        navigation_use: false,
      },
      forecast_frames: forecastFrames,
    };
  } finally {
    window.clearTimeout(timer);
  }
}

function mergeEnvironment(base, realtime) {
  if (!realtime) return base;
  return {
    ...base,
    ...realtime,
    wind: { ...(base?.wind || {}), ...realtime.wind },
    waves: { ...(base?.waves || {}), ...realtime.waves },
    ocean: { ...(base?.ocean || {}), ...realtime.ocean },
    air: { ...(base?.air || {}), ...realtime.air },
  };
}

function sceneEnvironmentSnapshot(weather) {
  if (!weather) return null;
  const windSpeed = Number(weather.wind?.speed_ms);
  const windDirection = Number(weather.wind?.direction_deg);
  const currentSpeed = Number(weather.ocean?.current_speed_ms);
  const currentDirection = Number(weather.ocean?.current_dir_deg);
  const waveHeight = Number(weather.waves?.significant_height_m);
  const wavePeriod = Number(weather.waves?.period_s);
  const waveDirection = Number(weather.waves?.direction_deg);
  if (![windSpeed, windDirection, currentSpeed, currentDirection, waveHeight, wavePeriod]
    .every(Number.isFinite)) return null;
  const hasWaveDirection = Number.isFinite(waveDirection);
  return {
    observed_at: weather.timestamp_utc || new Date().toISOString(),
    wind: {
      speed_m_s: Math.max(0, windSpeed),
      direction_deg: ((windDirection % 360) + 360) % 360,
      direction_convention: 'from',
      source: weather.source || 'environmental feed',
    },
    current: {
      speed_m_s: Math.max(0, currentSpeed),
      direction_deg: ((currentDirection % 360) + 360) % 360,
      direction_convention: 'to',
      source: weather.source || 'environmental feed',
    },
    waves: {
      significant_height_m: Math.max(0, waveHeight),
      period_s: Math.max(.1, wavePeriod),
      direction_deg: hasWaveDirection
        ? ((waveDirection % 360) + 360) % 360
        : ((windDirection % 360) + 360) % 360,
      direction_convention: 'from',
      direction_source: hasWaveDirection
        ? 'directional-wave-product'
        : 'wind-proxy',
    },
  };
}

function Pill({ label, tone = 'default' }) {
  return <span className={`pill tone-${tone}`}>{label}</span>;
}

const VESSEL_TYPES = [
  { value: 'rubber_boat',     label: 'Rubber boat' },
  { value: 'life_raft',       label: 'Life raft' },
  { value: 'fishing_vessel',  label: 'Fishing vessel' },
  { value: 'wooden_boat',     label: 'Wooden boat' },
  { value: 'sailboat',        label: 'Sailboat' },
  { value: 'motorboat',       label: 'Motorboat' },
  { value: 'container_ship',  label: 'Cargo / container' },
  { value: 'unknown',         label: 'Unknown' },
];

const RISK_LEVELS = [
  { value: 'high',   label: 'High' },
  { value: 'medium', label: 'Medium' },
  { value: 'low',    label: 'Low' },
];

const MAPTILER_KEY = import.meta.env.VITE_MAPTILER_KEY;
const OWM_KEY = import.meta.env.VITE_OWM_KEY;
const UNREAL_PIXEL_STREAM_URL = String(import.meta.env.VITE_UNREAL_PIXEL_STREAM_URL || '').trim();

function mapStyle() {
  const satellite = MAPTILER_KEY
    ? {
      type: 'raster',
      url: `https://api.maptiler.com/tiles/satellite-v2/tiles.json?key=${MAPTILER_KEY}`,
      tileSize: 256,
      attribution: '&copy; MapTiler satellite imagery providers',
    }
    : {
      type: 'raster',
      tiles: ['https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}'],
      tileSize: 256,
      maxzoom: 19,
      attribution: 'Esri, Maxar, Earthstar Geographics, and the GIS User Community',
    };
  return {
    version: 8,
    sources: {
      osm: publicBasemapSource(),
      satellite,
    },
    layers: [
      { id: 'osm', type: 'raster', source: 'osm' },
      { id: 'satellite', type: 'raster', source: 'satellite', layout: { visibility: 'none' } },
    ],
  };
}

function weatherGridToVectors(featureCollection) {
  const features = (featureCollection?.features || []).map((feature) => {
    const [lon, lat] = feature.geometry.coordinates;
    const speed = Number(feature.properties.wind_speed_ms || 0);
    const dirDeg = Number(feature.properties.wind_dir_deg || 0);
    const theta = (dirDeg * Math.PI) / 180;
    const vectorScale = Math.max(0.12, Math.min(0.36, 0.08 + speed * 0.018));
    const endLon = lon + vectorScale * Math.sin(theta);
    const endLat = lat + vectorScale * Math.cos(theta);
    return {
      type: 'Feature',
      geometry: { type: 'LineString', coordinates: [[lon, lat], [endLon, endLat]] },
      properties: { ...feature.properties },
    };
  });
  return { type: 'FeatureCollection', features };
}

function formatDistance(vessel) {
  if (!vessel) return '—';
  return `${vessel.distance_nm.toFixed(1)} nm`;
}

function vesselTone(vessel) {
  if (!vessel) return 'default';
  if ((vessel.type || '').toString().toUpperCase() === 'SAR') return 'ok';
  return 'info';
}

/** Build GeoJSON for proximity rendering: circles at vessel positions + lines from distress. */
function buildProximityGeojson(vessels, distressLat, distressLon) {
  if (!vessels.length || !Number.isFinite(distressLat) || !Number.isFinite(distressLon)) {
    return {
      vessels: { type: 'FeatureCollection', features: [] },
      lines:   { type: 'FeatureCollection', features: [] },
    };
  }
  const vesselFeatures = vessels.map((v, idx) => ({
    type: 'Feature',
    geometry: { type: 'Point', coordinates: [v.lon, v.lat] },
    properties: {
      mmsi: v.mmsi,
      ship_name: v.ship_name,
      type: v.type,
      distance_nm: v.distance_nm,
      rank: idx + 1,
    },
  }));
  const lineFeatures = vessels.map((v) => ({
    type: 'Feature',
    geometry: {
      type: 'LineString',
      coordinates: [[distressLon, distressLat], [v.lon, v.lat]],
    },
    properties: { ship_name: v.ship_name, distance_nm: v.distance_nm },
  }));
  return {
    vessels: { type: 'FeatureCollection', features: vesselFeatures },
    lines:   { type: 'FeatureCollection', features: lineFeatures },
  };
}


/** True course from the last two distinct observed AIS positions. */
function observedTrackCourse(points) {
  if (!Array.isArray(points) || points.length < 2) return 0;
  const end = points[points.length - 1];
  let start = null;
  for (let index = points.length - 2; index >= 0; index -= 1) {
    const candidate = points[index];
    if (Number(candidate?.lat) !== Number(end?.lat) || Number(candidate?.lon) !== Number(end?.lon)) {
      start = candidate;
      break;
    }
  }
  if (!start) return 0;
  const lat1 = Number(start.lat) * Math.PI / 180;
  const lat2 = Number(end.lat) * Math.PI / 180;
  const deltaLon = (Number(end.lon) - Number(start.lon)) * Math.PI / 180;
  const y = Math.sin(deltaLon) * Math.cos(lat2);
  const x = Math.cos(lat1) * Math.sin(lat2)
    - Math.sin(lat1) * Math.cos(lat2) * Math.cos(deltaLon);
  return (Math.atan2(y, x) * 180 / Math.PI + 360) % 360;
}

function App() {
  const [sidebarOpen, setSidebarOpen] = useState(isPublicLiveHost);
  const [activePanel, setActivePanel] = useState(
    isPublicLiveHost ? 'osint' : APP_PROFILE === 'demo' ? 'sim' : 'live',
  );
  const [apiBase, setApiBase] = useState(guessApiBase);
  const [localSettings, setLocalSettings] = useState(loadLocalSettings);
  const [summary, setSummary] = useState(null);
  const [stats, setStats] = useState(null);
  const [vessels, setVessels] = useState({ type: 'FeatureCollection', features: [] });
  const [ngoVessels, setNgoVessels] = useState({ type: 'FeatureCollection', features: [] });
  const [sanctionedVessels, setSanctionedVessels] = useState({ type: 'FeatureCollection', features: [] });
  // Map layers only ever receive fleet features that have a real position;
  // the fleet panel gets the complete registry (F-13).
  const sarMapFeatures = useMemo(() => ({
    type: 'FeatureCollection',
    features: (ngoVessels.features || []).filter((f) => f.geometry?.coordinates),
  }), [ngoVessels]);
  const [platforms, setPlatforms] = useState({ type: 'FeatureCollection', features: [] });
  const [alerts, setAlerts] = useState({ type: 'FeatureCollection', features: [] });
  const [caseGeojson, setCaseGeojson] = useState({ type: 'FeatureCollection', features: [] });
  const [weather, setWeather] = useState(null);
  const [weatherGrid, setWeatherGrid] = useState({ type: 'FeatureCollection', features: [] });
  const [weatherVectors, setWeatherVectors] = useState({ type: 'FeatureCollection', features: [] });
  const [play3D, setPlay3D] = useState(APP_PROFILE === 'demo');
  const [playRenderer, setPlayRenderer] = useState('cesium');
  const [playSimulationOpen, setPlaySimulationOpen] = useState(false);
  const [timezero, setTimezero] = useState(null);
  const [selectedVessel, setSelectedVessel] = useState(null);
  const [receiverMesh, setReceiverMesh] = useState({ catalogued: 0, reachable: 0, eligible: 0, active: 0, receivers: [] });
  const [radioSummary, setRadioSummary] = useState({ events: [], messages: [] });
  const [nearestVessels, setNearestVessels] = useState([]);
  const [mapReady, setMapReady] = useState(false);
  const [fallbackReady, setFallbackReady] = useState(false);
  const [mapError, setMapError] = useState('');
  const [selectionMode, setSelectionMode] = useState(false);
  const [cursorHint, setCursorHint] = useState({ visible: false, x: 0, y: 0 });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [caseStatus, setCaseStatus] = useState('idle');
  const [caseLog, setCaseLog] = useState([]);
  const [mapPanel, setMapPanel] = useState(null);
  const [conePanelHidden, setConePanelHidden] = useState(false);
  const [showScenario, setShowScenario] = useState(false);
  const [scenarioType, setScenarioType] = useState('distress');
  const [caseEventId, setCaseEventId] = useState(null);
  const [simHistory, setSimHistory] = useState(loadStoredSimulations);
  const [activeScenario, setActiveScenario] = useState(null);
  const [activeSimId, setActiveSimId] = useState(null);
  const [intelDrifts, setIntelDrifts] = useState({ type: 'FeatureCollection', features: [] });
  const [alertFlash, setAlertFlash] = useState(null);
  // Always fetch the full mode=all set on the public host: the Signals
  // selector's category toggles (activeSignalCategories, below) control what
  // is actually *shown*, independent of how much the client has fetched.
  // Tying the fetch itself to a category toggle (an earlier version of this)
  // meant switching to a narrower default view also starved every other
  // category down to whatever the edge-only humanitarian snapshot happened
  // to carry -- a handful of items on a quiet day, reading as "nothing on
  // the map" even though categories the user still wanted (hazard, IOM,
  // NGO, fused...) had plenty of eligible content sitting on the VM.
  const [liveMode] = useState(() => (isPublicLiveHost ? 'all' : 'humanitarian'));
  const seenAlertIdsRef = useRef(null);
  const { intelEvents, setIntelEvents, feedStatus, liveModeCounts, liveRoleCounts, pipelineSources } = useLiveFeed({
    apiBase,
    edgeBase: LIVE_EDGE_BASE,
    isPublicLiveHost,
    liveMode,
    onCriticalDistress: (props) => {
      setActivePanel('osint');
      setSidebarOpen(true);
      if (props?.type === 'correlated_alert') {
        setAlertFlash(props);
        playAlertBeep();
        window.setTimeout(() => setAlertFlash(null), 8000);
      }
    },
    onDriftUpdate: (message) => {
      setIntelDrifts((previous) => mergeIntelDriftUpdate(previous, message));
    },
  });
  const [liveEstimateClock, setLiveEstimateClock] = useState(Date.now());
  const [selectedSarResponse, setSelectedSarResponse] = useState(null);
  const [signalsExpanded, setSignalsExpanded] = useState(false);
  const [expandedMacros, setExpandedMacros] = useState(() => new Set());
  const [liveFacets, setLiveFacets] = useState(() => ({
    satellite: false,
    radio: false,
    sanctions: false,
    sar_fleet: false,
  }));
  const [showAisAlerts, setShowAisAlerts] = useState(false);
  const [showVesselLinks, setShowVesselLinks] = useState(false);
  const [baseMap, setBaseMap] = useState(() => {
    if (isPublicLiveHost) return 'standard';
    try {
      return window.localStorage.getItem('seacommons_base_map') === 'satellite' ? 'satellite' : 'standard';
    } catch {
      return 'standard';
    }
  });
  const layerVisStorageKey = layerVisibilityStorageKey(isPublicLiveHost);
  const [layerVis, setLayerVis] = useState(() => initialLayerVisibility({
    publicLive: isPublicLiveHost,
    storage: window.localStorage,
  }));
  const [triggeringDrift, setTriggeringDrift] = useState(() => new Set());
  // triggerIntelDrift() adds an id here right away so the "…" spinner shows
  // before the first server-confirmed 'computing' status arrives, but only
  // ever removes it again on a request-level network error — a successful
  // POST leaves it in the set forever. Left alone, that permanently pins the
  // card on the spinner: p.drift_status can move on to 'completed'/'failed'
  // (visible after a reload, since triggeringDrift resets on remount) while
  // the live view still ORs in this stale flag and never shows it. Prune an
  // id the moment its event reports a definitive, non-computing status.
  useEffect(() => {
    if (triggeringDrift.size === 0) return;
    setTriggeringDrift((prev) => {
      let changed = false;
      const next = new Set(prev);
      for (const feat of intelEvents) {
        const id = feat.properties?.id;
        const status = feat.properties?.drift_status;
        if (id && status && status !== 'computing' && next.has(id)) {
          next.delete(id);
          changed = true;
        }
      }
      return changed ? next : prev;
    });
  }, [intelEvents, triggeringDrift]);

  // Flash + chime when a NEW correlated_alert appears (the poll transport does
  // not push per-event, so detect it from the feed array). The first pass just
  // seeds the seen-set so existing alerts never re-fire on reload.
  useEffect(() => {
    if (isPublicLiveHost) return;
    const ids = intelEvents
      .filter((f) => f.properties?.type === 'correlated_alert')
      .map((f) => f.properties?.id)
      .filter(Boolean);
    if (seenAlertIdsRef.current === null) {
      seenAlertIdsRef.current = new Set(ids);
      return;
    }
    const fresh = intelEvents.find((f) => f.properties?.type === 'correlated_alert'
      && !seenAlertIdsRef.current.has(f.properties?.id));
    ids.forEach((id) => seenAlertIdsRef.current.add(id));
    if (fresh) {
      setAlertFlash(fresh.properties);
      playAlertBeep();
      window.setTimeout(() => setAlertFlash(null), 8000);
    }
  }, [intelEvents, isPublicLiveHost]);
  const caseEventIdRef = useRef(null);
  const caseStatusRef = useRef('idle');
  const simParamsRef = useRef({});
  const [form, setForm] = useState({
    lat: APP_PROFILE === 'demo' ? '35.52' : '',
    lon: APP_PROFILE === 'demo' ? '14.08' : '',
    persons: '1',
    vessel_type: 'rubber_boat',
    risk_level: 'high',
  });

  const mapNodeRef = useRef(null);
  const mapRef = useRef(null);
  const fallbackMapRef = useRef(null);
  const liveSignalsFramedRef = useRef(false);
  const selectionModeRef = useRef(false);
  const activePanelRef = useRef(isPublicLiveHost ? 'osint' : APP_PROFILE === 'demo' ? 'sim' : 'live');

  const selectedLat = parseFloat(form.lat);
  const selectedLon = parseFloat(form.lon);
  const displayedIntelDrifts = useMemo(
    () => mergeLiveDrifts(
      intelDrifts,
      null,
      intelEvents,
      new Date(liveEstimateClock),
    ),
    [intelDrifts, intelEvents, liveEstimateClock],
  );
  const selectedIntelEventId = mapPanel?.type === 'intel'
    ? mapPanel.feature?.properties?.id
    : null;
  const resolvedMapPanel = useMemo(() => {
    if (mapPanel?.type !== 'intel') return mapPanel;
    const selectedId = String(mapPanel.feature?.properties?.id || '').replace(/^intel:/, '');
    const canonicalFeature = intelEvents.find((feature) => (
      String(feature.properties?.id || '').replace(/^intel:/, '') === selectedId
    ));
    return canonicalFeature ? { ...mapPanel, feature: canonicalFeature } : mapPanel;
  }, [intelEvents, mapPanel]);

  useEffect(() => {
    let alive = true;
    const map = mapRef.current;
    const empty = { type: 'FeatureCollection', features: [] };
    const clearResponse = () => {
      if (!alive) return;
      setSelectedSarResponse(null);
      map?.getSource('ngo-response-lines')?.setData(empty);
      map?.getSource('ngo-response-points')?.setData(empty);
    };
    if (!isPublicLiveHost || !mapReady || !selectedIntelEventId || resolvedMapPanel?.type !== 'intel') {
      clearResponse();
      return () => { alive = false; };
    }
    const props = resolvedMapPanel.feature?.properties || {};
    const isHumanitarian = props.main_category === 'humanitarian'
      || Boolean(props.humanitarian_case_id)
      || Boolean(props.is_distress)
      || props.maritime_domain === 'sar';
    if (!isHumanitarian || props.entity_kind === 'vessel' || props.report_type === 'vessel') {
      clearResponse();
      return () => { alive = false; };
    }
    const rawId = String(selectedIntelEventId).replace(/^intel:/, '');
    fetchJson(apiBase, `/api/v1/live/signals/${encodeURIComponent(rawId)}/response`, undefined, 8000)
      .then((data) => {
        if (!alive) return;
        setSelectedSarResponse(data || null);
        const features = Array.isArray(data?.geojson?.features) ? data.geojson.features : [];
        const lines = features.filter((feature) => feature.geometry?.type === 'LineString');
        const points = features.filter((feature) => feature.geometry?.type === 'Point');
        const currentMap = mapRef.current;
        currentMap?.getSource('ngo-response-lines')?.setData({ type: 'FeatureCollection', features: lines });
        currentMap?.getSource('ngo-response-points')?.setData({ type: 'FeatureCollection', features: points });
      })
      .catch(() => clearResponse());
    return () => { alive = false; };
  }, [apiBase, isPublicLiveHost, mapReady, resolvedMapPanel, selectedIntelEventId]);

  function panelFocusCoordinates(panel) {
    const geometry = panel?.feature?.geometry;
    if (!geometry) return null;
    if (geometry.type === 'Point' && Array.isArray(geometry.coordinates)) return geometry.coordinates;
    if (geometry.type === 'LineString' && Array.isArray(geometry.coordinates) && geometry.coordinates.length) {
      return geometry.coordinates[Math.floor((geometry.coordinates.length - 1) / 2)] || geometry.coordinates[0];
    }
    if (geometry.type === 'Polygon' && Array.isArray(geometry.coordinates?.[0]) && geometry.coordinates[0].length) {
      const ring = geometry.coordinates[0];
      const total = ring.reduce((acc, point) => [acc[0] + Number(point[0]), acc[1] + Number(point[1])], [0, 0]);
      return [total[0] / ring.length, total[1] / ring.length];
    }
    return null;
  }

  function mobilePanelMapPadding() {
    return {
      top: 24,
      right: 24,
      bottom: Math.round(window.innerHeight * 0.66),
      left: 24,
    };
  }

  function selectIntelReport(feature) {
    setActivePanel('osint');
    if (!window.matchMedia('(max-width: 680px)').matches) setSidebarOpen(true);
    setMapPanel({ type: 'intel', feature });
    setConePanelHidden(false);
  }

  function openIntelReport(feature) {
    const coordinates = panelFocusCoordinates({ feature });
    const isMobile = window.matchMedia('(max-width: 680px)').matches;
    if (coordinates && mapRef.current && !isMobile) {
      mapRef.current.flyTo({ center: coordinates, zoom: feature?.geometry?.type === 'Polygon' ? 7.2 : 9, duration: 800, essential: true });
    }
    selectIntelReport(feature);
  }

  function openVesselReport(feature) {
    if (!feature) return;
    if (isPublicLiveHost) {
      const props = feature.properties || {};
      const isTrackedResponder = (
        props.intel_type === 'ngo_vessel'
        || props.vessel_class === 'ngo'
        || props.vessel_class === 'coastguard'
      );
      if (!isTrackedResponder) return;
    }
    const reportFeature = vesselReportFeature(feature);
    setSelectedVessel(null);
    setMapPanel({ type: 'intel', feature: reportFeature });
    setConePanelHidden(false);
    if (!window.matchMedia('(max-width: 680px)').matches) setSidebarOpen(true);
  }

  useEffect(() => {
    const map = mapRef.current;
    if (!isPublicLiveHost || !map || !mapReady || !window.matchMedia('(max-width: 680px)').matches) return;
    if (conePanelHidden || !resolvedMapPanel) {
      map.easeTo({ padding: { top: 0, right: 0, bottom: 0, left: 0 }, duration: 220, essential: true });
      return;
    }
    const coordinates = panelFocusCoordinates(resolvedMapPanel);
    if (!coordinates) return;
    map.easeTo({
      center: coordinates,
      zoom: Math.max(map.getZoom(), resolvedMapPanel.type === 'intel' ? 7.8 : 6.5),
      padding: mobilePanelMapPadding(),
      duration: 420,
      essential: true,
    });
  }, [conePanelHidden, mapReady, resolvedMapPanel]);

  useEffect(() => {
    if (mapPanel?.type === 'cone') setConePanelHidden(false);
  }, [mapPanel]);

  // Mobile: the drift cone panel and the dashboard sheet are both bottom sheets,
  // so they must be mutually exclusive — opening the cone closes the dashboard.
  const coneVisible = mapPanel?.type === 'cone' && !conePanelHidden;
  useEffect(() => {
    if (coneVisible && window.matchMedia('(max-width: 680px)').matches) {
      setSidebarOpen(false);
    }
  }, [coneVisible]);

  function pushCaseLog(message) {
    setCaseLog((cur) => [
      { id: `${Date.now()}-${Math.random()}`, message, at: new Date().toISOString() },
      ...cur,
    ].slice(0, 20));
  }

  async function loadWeatherFor(lat, lon) {
    if (isPublicLiveHost) {
      const realtime = await fetchOpenMeteoEnvironment(lat, lon);
      const payload = mergeEnvironment({}, realtime);
      setWeather(payload);
      pushCaseLog(`Weather ${payload.source} @ ${Number(lat).toFixed(3)}, ${Number(lon).toFixed(3)}`);
      return payload;
    }
    const [baseResult, realtimeResult] = await Promise.allSettled([
      fetchJson(
        apiBase,
        `/api/v1/weather?lat=${encodeURIComponent(lat)}&lon=${encodeURIComponent(lon)}`,
      ),
      fetchOpenMeteoEnvironment(lat, lon),
    ]);
    if (baseResult.status === 'rejected' && realtimeResult.status === 'rejected') {
      throw baseResult.reason;
    }
    const payload = mergeEnvironment(
      baseResult.status === 'fulfilled' ? baseResult.value : {},
      realtimeResult.status === 'fulfilled' ? realtimeResult.value : null,
    );
    setWeather(payload);
    pushCaseLog(`Weather ${payload.source} @ ${Number(lat).toFixed(3)}, ${Number(lon).toFixed(3)}`);
    return payload;
  }

  async function loadWeatherGridForMap(map) {
    if (!map) return;   // guard: button can be pressed before map init completes
    if (isPublicLiveHost) return; // Live forcing is sampled per incident at the edge.
    if (map.getZoom() < 4) return;  // world/globe view — a 4x4 grid over the planet is meaningless
    const bounds = map.getBounds();
    const payload = await fetchJson(
      apiBase,
      `/api/v1/weather/grid?lat_min=${bounds.getSouth().toFixed(3)}&lat_max=${bounds.getNorth().toFixed(3)}&lon_min=${bounds.getWest().toFixed(3)}&lon_max=${bounds.getEast().toFixed(3)}&n=4`,
      undefined,
      25000,  // 25s — backend batch timeout is 20s + network headroom
    );
    setWeatherGrid(payload);
    setWeatherVectors(weatherGridToVectors(payload));
  }

  async function triggerIntelDrift(eventId, lat, lon, vesselType = 'rubber_boat') {
    setTriggeringDrift((prev) => new Set([...prev, eventId]));
    try {
      await fetchJson(apiBase, '/api/v1/intel/auto-drift', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ intel_event_id: eventId, lat, lon, vessel_type: vesselType || 'rubber_boat' }),
      }, 8000);
      setIntelEvents((prev) => prev.map((f) =>
        (f.properties?.id === eventId || f.properties?.drift_event_id === eventId)
          ? { ...f, properties: { ...f.properties, drift_status: 'computing' } }
          : f
      ));
    } catch (err) {
      setTriggeringDrift((prev) => { const n = new Set(prev); n.delete(eventId); return n; });
    }
  }

  async function loadNearestVessels(lat, lon) {
    if (!Number.isFinite(Number(lat)) || !Number.isFinite(Number(lon))) return [];
    const payload = await fetchJson(
      apiBase,
      `/api/v1/vessels/nearest?lat=${encodeURIComponent(lat)}&lon=${encodeURIComponent(lon)}&limit=8`,
    );
    setNearestVessels(payload.vessels || []);
    return payload.vessels || [];
  }

  useEffect(() => {
    window.localStorage.setItem('seacommons_api_base', apiBase);
  }, [apiBase]);

  useEffect(() => {
    selectionModeRef.current = selectionMode;
  }, [selectionMode]);

  useEffect(() => {
    activePanelRef.current = activePanel;
  }, [activePanel]);

  useEffect(() => {
    caseEventIdRef.current = caseEventId;
  }, [caseEventId]);

  useEffect(() => {
    caseStatusRef.current = caseStatus;
  }, [caseStatus]);

  // Product policy §2: there is ONE authoritative Drift pipeline —
  // BACKEND / WORKER = authoritative scientific Drift, FRONTEND = visualization
  // only. Public Live no longer runs a competing in-browser drift model for
  // Alarm Phone incidents; it consumes the persisted operational drift from
  // /api/v1/live/drifts (see the polling effect below). Browser drift remains
  // ONLY as the explicitly user-triggered simulation tool (runSarCaseAt).

  useEffect(() => {
    if (!isPublicLiveHost) return undefined;
    const timer = window.setInterval(() => setLiveEstimateClock(Date.now()), 30_000);
    return () => window.clearInterval(timer);
  }, []);

  // ── Intel drift traces polling ───────────────────────────────────────────────
  useEffect(() => {
    let alive = true;
    let driftTimer = null;
    // Public Live consumes the backend/edge persisted operational Drift; the
    // authenticated console consumes the operator drift projection. Neither
    // clears the source — a transient fetch failure keeps the last good frame.
    async function loadDrifts() {
      try {
        const path = isPublicLiveHost ? '/api/v1/live/drifts' : '/api/v1/intel/drifts';
        const data = await fetchJson(apiBase, path);
        if (alive && data.features) setIntelDrifts(data);
      } catch { /* ignore — keep the last good drift frame */ }
      if (alive) {
        driftTimer = window.setTimeout(loadDrifts, isPublicLiveHost ? 30_000 : 120_000);
      }
    }
    loadDrifts();
    return () => {
      alive = false;
      window.clearTimeout(driftTimer);
    };
  }, [apiBase]);

  useEffect(() => {
    if (isPublicLiveHost) {
      setPlatforms({ type: 'FeatureCollection', features: [] });
      return undefined;
    }
    const path = isPublicLiveHost ? '/api/v1/live/platforms' : '/api/v1/zones/platforms';
    fetchJson(apiBase, path)
      .then(d => { if (d.features) setPlatforms(d); })
      .catch(() => {});
  }, [apiBase, isPublicLiveHost]);

  // ── MDA / dark-vessel layers ───────────────────────────────────────────────
  // The operator MDA workspace keeps its specialist API. Public Live receives
  // security signals through the canonical mode-aware Live feed instead.
  const [mdaReference, setMdaReference] = useState({ type: 'FeatureCollection', features: [] });
  const [mdaJamming, setMdaJamming] = useState({ type: 'FeatureCollection', features: [] });
  const [mdaAnomalies, setMdaAnomalies] = useState([]);
  useEffect(() => {
    if (isPublicLiveHost) return undefined;
    let alive = true;
    const pull = async () => {
      try {
        const [ref, jam] = await Promise.all([
          fetchJson(apiBase, '/api/v1/mda/reference').catch(() => null),
          fetchJson(apiBase, '/api/v1/mda/jamming').catch(() => null),
        ]);
        if (!alive) return;
        if (ref?.features) setMdaReference(ref);
        if (jam?.features) setMdaJamming(jam);
      } catch { /* offline */ }
    };
    pull();
    const t = window.setInterval(pull, 90_000);
    return () => { alive = false; window.clearInterval(t); };
  }, [apiBase, isPublicLiveHost]);
  useEffect(() => {
    if (isPublicLiveHost) return undefined;
    let alive = true;
    const path = '/api/v1/mda/anomalies';
    const pull = async () => {
      try {
        const anom = await fetchJson(apiBase, `${path}?hours=72`).catch(() => null);
        if (alive && Array.isArray(anom?.anomalies)) setMdaAnomalies(anom.anomalies);
      } catch { /* offline */ }
    };
    pull();
    const t = window.setInterval(pull, 90_000);
    return () => { alive = false; window.clearInterval(t); };
  }, [apiBase, isPublicLiveHost]);

  // ── Rehydrate case history from the DB (survives page refresh) ──────────────
  // GeoJSON is loaded lazily on "Show" to keep the initial payload small.
  useEffect(() => {
    // Public Live archives incident lifecycle directly from the edge feed;
    // simulation history is local-first and must not require Oracle.
    if (isPublicLiveHost) return undefined;
    let alive = true;
    (async () => {
      try {
        const data = await fetchJson(
          apiBase,
          APP_PROFILE === 'demo' ? '/api/v1/live/archives' : '/api/v1/alerts',
        );
        const records = Array.isArray(data) ? data : data?.archives;
        if (!alive || !Array.isArray(records)) return;
        const completed = APP_PROFILE === 'demo'
          ? records.slice(0, 8)
          : records.filter((alert) => alert.status === 'completed').slice(0, 8);
        if (!completed.length) return;
        setSimHistory((prev) => {
          const known = new Set(prev.map((s) => s.id));
          const restored = completed
            .filter((archive) => !known.has(archive.id || archive.event_id))
            .map((archive) => {
              const event = archive.event || archive;
              return {
              id: archive.id || archive.event_id,
              label: `${(event.vessel_type || 'case').replace(/_/g, ' ')} @ ${Number(event.lat).toFixed(3)}, ${Number(event.lon).toFixed(3)}`,
              ts: event.timestamp || new Date().toISOString(),
              geojson: null,   // fetched on first replay
              lat: Number(event.lat),
              lon: Number(event.lon),
              params: { vesselType: event.vessel_type, persons: event.persons },
            };
            });
          return [...prev, ...restored].slice(0, 10);
        });
      } catch { /* backend cold — history stays session-only */ }
    })();
    return () => { alive = false; };
  }, [apiBase]);

  useEffect(() => {
    if (isPublicDemoHost) {
      setNgoVessels({ type: 'FeatureCollection', features: [] });
      return undefined;
    }
    const path = isPublicLiveHost
      ? (liveFacets.sar_fleet ? '/api/v1/live/sar-fleet' : null)
      : '/api/v1/intel/ngo';
    if (!path) {
      setNgoVessels({ type: 'FeatureCollection', features: [] });
      return undefined;
    }
    let alive = true;
    let timer = null;
    async function loadNgoVessels() {
      try {
        const data = await fetchJson(apiBase, path, undefined, 8000);
        if (alive && data?.features) setNgoVessels(data);
      } catch { /* keep last good frame */ }
      if (alive) timer = window.setTimeout(loadNgoVessels, 15_000);
    }
    loadNgoVessels();
    return () => {
      alive = false;
      window.clearTimeout(timer);
    };
  }, [apiBase, isPublicLiveHost, isPublicDemoHost, liveFacets.sar_fleet]);

  useEffect(() => {
    if (!isPublicLiveHost || !liveFacets.sanctions) {
      setSanctionedVessels({ type: 'FeatureCollection', features: [] });
      return undefined;
    }
    let alive = true;
    let timer = null;
    const load = async () => {
      try {
        const data = await fetchJson(
          apiBase,
          '/api/v1/live/sanctioned-vessels',
          undefined,
          8000,
        );
        if (alive && data?.features) setSanctionedVessels(data);
      } catch {
        // Keep the last good fresh-only frame; the server itself refuses stale positions.
      }
      if (alive) timer = window.setTimeout(load, 15_000);
    };
    load();
    return () => {
      alive = false;
      window.clearTimeout(timer);
    };
  }, [apiBase, liveFacets.sanctions]);

  useEffect(() => {
    window.localStorage.setItem('seacommons_tz_host', localSettings.timezeroHost);
    window.localStorage.setItem('seacommons_tz_port', localSettings.timezeroPort);
    window.localStorage.setItem('seacommons_tz_enabled', localSettings.timezeroEnabled);
  }, [localSettings]);

  useEffect(() => {
    liveSignalsFramedRef.current = false;
  }, [liveMode]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapReady) return;
    map.getCanvas().style.cursor = APP_PROFILE === 'demo' && (activePanel === 'sim' || selectionMode)
      ? 'crosshair'
      : '';
  }, [activePanel, selectionMode, mapReady]);

  useEffect(() => {
    const map = mapRef.current;
    if (!isPublicLiveHost || !map || !mapReady || window.innerWidth <= 820) return;
    map.easeTo({
      padding: { top: 0, right: 0, bottom: 0, left: sidebarOpen ? 392 : 0 },
      duration: 420,
      essential: true,
    });
  }, [sidebarOpen, mapReady]);

  useEffect(() => {
    const map = mapRef.current;
    const features = intelEvents || [];
    if (!isPublicLiveHost || !map || !mapReady || liveSignalsFramedRef.current || !features.length) return;
    let west = Infinity;
    let south = Infinity;
    let east = -Infinity;
    let north = -Infinity;
    let coordinateCount = 0;
    const extendCoordinates = (coordinates) => {
      if (!Array.isArray(coordinates)) return;
      if (
        coordinates.length >= 2
        && Number.isFinite(Number(coordinates[0]))
        && Number.isFinite(Number(coordinates[1]))
      ) {
        const longitude = Number(coordinates[0]);
        const latitude = Number(coordinates[1]);
        if (longitude < -6 || longitude > 37 || latitude < 28 || latitude > 47) return;
        west = Math.min(west, longitude);
        south = Math.min(south, latitude);
        east = Math.max(east, longitude);
        north = Math.max(north, latitude);
        coordinateCount += 1;
        return;
      }
      coordinates.forEach(extendCoordinates);
    };
    features.forEach((feature) => extendCoordinates(feature.geometry?.coordinates));
    if (coordinateCount < 2 || ![west, south, east, north].every(Number.isFinite)) return;
    liveSignalsFramedRef.current = true;
    map.fitBounds([[west, south], [east, north]], {
      padding: window.innerWidth > 820
        ? { top: 100, right: 100, bottom: 100, left: sidebarOpen ? 470 : 100 }
        : { top: 110, right: 38, bottom: 90, left: 38 },
      maxZoom: 7.2,
      duration: 1100,
      essential: true,
    });
  }, [intelEvents, mapReady, sidebarOpen]);

  // ── Map init ────────────────────────────────────────────────────────────────
  useEffect(() => {
    if (!mapNodeRef.current || mapRef.current) return;
    let disposed = false;
    let liveMap = null;

    async function initMap() {
      if (window.__SEACOMMONS_FORCE_MAP_FALLBACK__ === true) throw new Error('forced map fallback');
      await import('maplibre-gl/dist/maplibre-gl.css');
      const { default: maplibregl } = await import('maplibre-gl');
      if (disposed || mapRef.current) return;

      const map = new maplibregl.Map({
        container: mapNodeRef.current,
        style: mapStyle(),
        center: APP_PROFILE === 'live' ? [15.2, 36.1] : [14.3, 31.0],
        zoom: APP_PROFILE === 'live' ? 4.15 : 1.9,
        padding: isPublicLiveHost && window.innerWidth > 820
          ? { top: 0, right: 0, bottom: 0, left: 392 }
          : 0,
        attributionControl: !isPublicLiveHost,
      });

      // ── Globe intro: 3D globe that flattens to 2D as you zoom in ──────────
      map.on('style.load', () => {
        try { map.setProjection({ type: 'globe' }); } catch { /* projection unsupported — flat fallback */ }
      });

      // Slow rotation until the first user interaction
      let spinning = APP_PROFILE !== 'live';
      const SPIN_DEG_PER_STEP = 8;
      const SPIN_STEP_MS = 6000;
      function spinStep() {
        if (!spinning) return;
        const c = map.getCenter();
        map.easeTo({
          center: [c.lng - SPIN_DEG_PER_STEP, c.lat],
          duration: SPIN_STEP_MS,
          easing: (t) => t,
        });
      }
      function stopSpin() {
        if (!spinning) return;
        spinning = false;
        map.stop();
      }
      map.on('moveend', () => { if (spinning) spinStep(); });
      for (const ev of ['mousedown', 'wheel', 'touchstart', 'dragstart', 'pitchstart']) {
        map.on(ev, stopSpin);
      }

      map.addControl(new maplibregl.GeolocateControl({
        positionOptions: { enableHighAccuracy: true },
        trackUserLocation: true,
        showUserHeading: true,
      }), 'top-right');
      map.addControl(new maplibregl.ScaleControl({ maxWidth: 110, unit: 'nautical' }), 'bottom-right');

      liveMap = map;

      let weatherTimer = null;
      map.on('moveend', () => {
        if (isPublicLiveHost) return;
        window.clearTimeout(weatherTimer);
        weatherTimer = window.setTimeout(() => {
          loadWeatherGridForMap(map).catch((err) => setError(err.message || 'Weather grid unavailable'));
        }, 220);
      });

      map.on('load', () => {
        // Register vessel arrow SDF icon
        map.addImage('vessel-arrow', createVesselArrowImage(48), { sdf: true });

        // Nautical seamarks (OpenSeaMap): buoys, lights, beacons, fairways,
        // depth contours — an open, free overlay drawn directly on top of
        // the base map/satellite, standard IHO-style chart symbology. Added
        // first (right after the base style, before every data source
        // below) so every marker/vessel layer draws on top of it, never
        // the other way round.
        map.addSource('seamarks', {
          type: 'raster',
          tiles: ['https://tiles.openseamap.org/seamark/{z}/{x}/{y}.png'],
          tileSize: 256,
          maxzoom: 18,
          attribution: '&copy; OpenSeaMap contributors',
        });
        map.addLayer({ id: 'seamarks-layer', type: 'raster', source: 'seamarks', paint: { 'raster-opacity': 0.9 } });

        // sources
        map.addSource('weather-points',    { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
        map.addSource('weather-vectors',   { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
        map.addSource('vessels',           { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
        map.addSource('selected-vessel-track', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
        map.addSource('radio-receivers', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
        map.addSource('radio-dsc', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
        map.addSource('vessels-ngo',       { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
        map.addSource('sanctioned-vessels', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
        map.addSource('platforms',         { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
        map.addSource('alerts',            { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
        map.addSource('sar-case',          { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
        map.addSource('proximity-lines',   { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
        map.addSource('proximity-vessels', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
        map.addSource('mda-reference',     { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
        map.addSource('mda-jamming',       { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
        map.addSource('mda-anomaly',       { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
        map.addSource('intel-events',      { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
        map.addSource('intel-selected',    { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
        map.addSource('intel-distress',    { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
        map.addSource('intel-fused',       { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
        map.addSource('intel-spike',       { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
        map.addSource('intel-vessels',     { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
        map.addSource('intel-observed-tracks', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
        map.addSource('intel-drifts',      { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
        map.addSource('intel-vessel-links', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
        map.addSource('live-nearby-vessels', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
        map.addSource('ngo-response-lines',  { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
        map.addSource('ngo-response-points', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });

        // weather vectors
        map.addLayer({
          id: 'weather-vectors', type: 'line', source: 'weather-vectors',
          paint: {
            'line-color': ['interpolate', ['linear'], ['get', 'beaufort'],
              0, '#7dd3fc', 4, '#22c55e', 7, '#f59e0b', 10, '#ef4444'],
            'line-width': 2, 'line-opacity': 0.68,
          },
        });
        map.addLayer({
          id: 'weather-points', type: 'circle', source: 'weather-points',
          paint: {
            'circle-radius': 3.5, 'circle-opacity': 0.9,
            'circle-color': ['interpolate', ['linear'], ['get', 'beaufort'],
              0, '#7dd3fc', 4, '#22c55e', 7, '#f59e0b', 10, '#ef4444'],
            'circle-stroke-width': 0.8, 'circle-stroke-color': '#04131a',
          },
        });

        // ── Layer z-order (bottom → top) ─────────────────────────────────────
        // weather → MDA context → platforms → intel-drift (bg) → alerts → sar-case
        // → proximity → vessels → NGO → intel-drift-points → intel-events → sar-impact

        // ── MDA / dark-vessel context layers (operator only, off by default) ──
        map.addLayer({
          id: 'mda-jamming-fill', type: 'fill', source: 'mda-jamming',
          layout: { visibility: 'none' },
          paint: {
            'fill-color': '#a855f7',
            'fill-opacity': ['interpolate', ['linear'], ['coalesce', ['get', 'score'], 0.3], 0.25, 0.06, 1, 0.22],
          },
        });
        map.addLayer({
          id: 'mda-infra-lines', type: 'line', source: 'mda-reference',
          filter: ['in', ['get', 'kind'], ['literal', ['cable', 'pipeline']]],
          layout: { visibility: 'none' },
          paint: {
            'line-color': ['match', ['get', 'kind'], 'cable', '#38bdf8', '#f59e0b'],
            'line-width': 1.4, 'line-opacity': 0.7, 'line-dasharray': [3, 2],
          },
        });
        map.addLayer({
          id: 'mda-infra-points', type: 'circle', source: 'mda-reference',
          filter: ['==', ['get', 'kind'], 'platform'],
          layout: { visibility: 'none' },
          paint: {
            'circle-radius': 4, 'circle-color': 'rgba(250,204,21,0.15)',
            'circle-stroke-color': '#facc15', 'circle-stroke-width': 1.2,
          },
        });
        map.addLayer({
          id: 'mda-sts-zones', type: 'fill', source: 'mda-reference',
          filter: ['==', ['get', 'kind'], 'sts_zone'],
          layout: { visibility: 'none' },
          paint: { 'fill-color': '#f472b6', 'fill-opacity': 0.08, 'fill-outline-color': '#f472b6' },
        });
        map.addLayer({
          id: 'mda-anomaly-layer', type: 'circle', source: 'mda-anomaly',
          layout: { visibility: 'none' },
          paint: {
            'circle-radius': ['match', ['get', 'type'], 'correlated_alert', 7, 5],
            'circle-color': mdaAnomalyColorExpression(),
            'circle-opacity': 0.95,
            'circle-stroke-width': 1.4,
            'circle-stroke-color': '#04131a',
          },
        });

        // Public-safe radio receiver mesh. Coordinates are deliberately coarse
        // (3 decimals) and the API never exposes receiver endpoint or physical lineage.
        map.addLayer({
          id: 'radio-receivers-halo', type: 'circle', source: 'radio-receivers',
          paint: {
            'circle-radius': ['match', ['get', 'active'], true, 10, 7],
            'circle-color': ['match', ['get', 'active'], true, 'rgba(167,139,250,0.24)', 'rgba(148,163,184,0.12)'],
            'circle-blur': 0.7,
          },
        });
        map.addLayer({
          id: 'radio-receivers-layer', type: 'circle', source: 'radio-receivers',
          paint: {
            'circle-radius': ['match', ['get', 'active'], true, 5.5, 4],
            'circle-color': ['match', ['get', 'active'], true, '#a78bfa', '#94a3b8'],
            'circle-opacity': ['match', ['get', 'state'], 'offline', 0.35, 0.92],
            'circle-stroke-width': 1.2,
            'circle-stroke-color': '#07151b',
          },
        });

        // Geolocated decoded DSC messages. A radio burst without a decoded
        // position never becomes a map point.
        map.addLayer({
          id: 'radio-dsc-halo', type: 'circle', source: 'radio-dsc',
          paint: {
            'circle-radius': 10,
            'circle-color': ['match', ['get', 'category'], 'distress', 'rgba(255,59,59,0.24)', 'rgba(245,158,11,0.2)'],
            'circle-blur': 0.7,
          },
        });
        map.addLayer({
          id: 'radio-dsc-layer', type: 'circle', source: 'radio-dsc',
          paint: {
            'circle-radius': 5.5,
            'circle-color': ['match', ['get', 'category'], 'distress', '#ff3b3b', 'urgency', '#f59e0b', 'safety', '#38bdf8', '#a78bfa'],
            'circle-stroke-width': 1.4,
            'circle-stroke-color': '#07151b',
          },
        });

        // Oil/gas platforms — static infrastructure, rendered just above weather
        map.addLayer({
          id: 'platforms-halo', type: 'circle', source: 'platforms',
          paint: {
            'circle-radius': 7,
            'circle-color': ['match', ['get', 'platform_type'],
              'oil', 'rgba(251,146,60,0.20)',
                     'rgba(250,204,21,0.18)'],
            'circle-blur': 0.5,
          },
        });
        map.addLayer({
          id: 'platforms-layer', type: 'circle', source: 'platforms',
          paint: {
            'circle-radius': 4,
            'circle-color': ['match', ['get', 'platform_type'],
              'oil', '#fb923c',
                     '#fbbf24'],
            'circle-opacity': 0.88,
            'circle-stroke-width': 1.5,
            'circle-stroke-color': '#0d1f26',
          },
        });

        // Solid line = positions actually observed through AIS.  Dashed lines
        // below remain model forecasts, so observation and inference cannot be
        // mistaken for one another.
        map.addLayer({
          id: 'intel-observed-track-line', type: 'line', source: 'intel-observed-tracks',
          filter: ['==', '$type', 'LineString'],
          paint: {
            'line-color': ['match', ['get', 'maritime_domain'],
              'sanctions', '#f472b6', '#38bdf8'],
            'line-width': 2.4,
            'line-opacity': 0.86,
          },
        });

        // Grouped maritime-security cases are vessels, not generic alert dots.
        // Keep the marker bare: the triangle shows the latest observed AIS
        // course and the separate selection layer supplies the only glow.
        // Icon halos around this hand-built SDF render as squares on some GPUs.
        map.addLayer({
          id: 'intel-vessel-core', type: 'symbol', source: 'intel-vessels',
          layout: {
            'icon-image': 'vessel-arrow',
            'icon-size': ['interpolate', ['linear'], ['zoom'], 5, 0.34, 10, 0.58, 14, 0.74],
            'icon-rotate': ['coalesce', ['get', 'course'], 0],
            'icon-rotation-alignment': 'map',
            'icon-allow-overlap': true,
            'icon-ignore-placement': true,
          },
          paint: {
            'icon-color': '#7dd3fc',
            'icon-opacity': 1,
          },
        });

        map.addLayer({
          id: 'intel-selected-glow', type: 'circle', source: 'intel-selected',
          paint: {
            'circle-radius': 23,
            'circle-color': ['coalesce', ['get', 'visual_color'], '#a9ffda'],
            'circle-opacity': 0.22,
            'circle-blur': 0.72,
          },
        });
        map.addLayer({
          id: 'intel-selected-ring', type: 'circle', source: 'intel-selected',
          paint: {
            'circle-radius': 15,
            'circle-color': 'rgba(0,0,0,0)',
            'circle-stroke-width': 2.5,
            'circle-stroke-color': ['coalesce', ['get', 'visual_color'], '#a9ffda'],
            'circle-stroke-opacity': 0.95,
          },
        });

        // Intel auto-drift background cones & lines (faintest, furthest back)
        // Drift colour inherits its origin signal's semantic category
        // (visual_color) — Alarm Phone drift is red because the origin is
        // Alarm Phone, never because of a severity. Uncertainty is encoded
        // through fill opacity / line width, not hue.
        const _DRIFT_COLOR = ['coalesce', ['get', 'visual_color'], '#ff3b3b'];
        map.addLayer({
          id: 'intel-drift-cone', type: 'fill', source: 'intel-drifts',
          filter: ['==', '$type', 'Polygon'],
          paint: {
            'fill-color': _DRIFT_COLOR,
            'fill-opacity': isPublicLiveHost ? 0.14 : 0.07,
            'fill-outline-color': _DRIFT_COLOR,
          },
        });
        map.addLayer({
          id: 'intel-drift-line', type: 'line', source: 'intel-drifts',
          filter: ['==', '$type', 'LineString'],
          paint: {
            'line-color': _DRIFT_COLOR,
            'line-opacity': isPublicLiveHost ? 0.92 : 0.5,
            'line-width': isPublicLiveHost ? 2.4 : 1.5,
            'line-dasharray': [3, 3],
          },
        });

        // Historical SAR drift cones & trajectories
        map.addLayer({
          id: 'alerts-cone', type: 'fill', source: 'alerts',
          filter: ['==', '$type', 'Polygon'],
          paint: {
            'fill-color': ['match', ['get', 'type'],
              'cone_6h',  'rgba(255,180,60,0.22)',
              'cone_12h', 'rgba(255,120,40,0.17)',
                          'rgba(255,60,30,0.13)'],
            'fill-outline-color': ['match', ['get', 'type'],
              'cone_6h',  'rgba(255,180,60,0.55)',
              'cone_12h', 'rgba(255,120,40,0.45)',
                          'rgba(255,60,30,0.38)'],
          },
        });
        map.addLayer({
          id: 'alerts-layer', type: 'line', source: 'alerts',
          filter: ['==', '$type', 'LineString'],
          paint: { 'line-color': '#ff7b54', 'line-width': 2.5, 'line-opacity': 0.9 },
        });

        // Active SAR case cone & trajectory
        map.addLayer({
          id: 'sar-case-cone', type: 'fill', source: 'sar-case',
          filter: ['==', '$type', 'Polygon'],
          paint: {
            'fill-color': ['match', ['get', 'type'],
              'cone_6h',  'rgba(255,224,109,0.18)',
              'cone_12h', 'rgba(255,180,80,0.14)',
                          'rgba(255,120,60,0.10)'],
            'fill-outline-color': ['match', ['get', 'type'],
              'cone_6h',  'rgba(255,224,109,0.55)',
              'cone_12h', 'rgba(255,180,80,0.45)',
                          'rgba(255,120,60,0.35)'],
          },
        });
        map.addLayer({
          id: 'sar-case-line', type: 'line', source: 'sar-case',
          filter: ['==', '$type', 'LineString'],
          paint: {
            'line-color': '#ff3b3b',
            'line-width': 3,
            'line-opacity': 0.95,
          },
        });
        map.addLayer({
          id: 'sar-case-traj-arrows', type: 'symbol', source: 'sar-case',
          filter: ['all', ['==', ['geometry-type'], 'LineString'], ['==', ['get', 'type'], 'trajectory']],
          layout: {
            'symbol-placement': 'line',
            'symbol-spacing': 100,
            'icon-image': 'vessel-arrow',
            'icon-size': 0.55,
            'icon-rotation-alignment': 'map',
            'icon-allow-overlap': true,
            'icon-ignore-placement': true,
          },
          paint: { 'icon-color': '#ff3b3b', 'icon-opacity': 1.0 },
        });

        // Proximity: dashed lines from distress to nearest vessels
        map.addLayer({
          id: 'proximity-lines', type: 'line', source: 'proximity-lines',
          paint: {
            'line-color': '#f97316',
            'line-width': 1.5,
            'line-opacity': 0.65,
            'line-dasharray': [4, 5],
          },
        });

        // Proximity: highlighted vessel triangles (orange glow + arrow)
        map.addLayer({
          id: 'proximity-vessels-halo', type: 'circle', source: 'proximity-vessels',
          paint: { 'circle-radius': 14, 'circle-color': 'rgba(249,115,22,0.20)', 'circle-blur': 0.8 },
        });
        map.addLayer({
          id: 'proximity-vessels-layer', type: 'symbol', source: 'proximity-vessels',
          layout: {
            'icon-image': 'vessel-arrow',
            'icon-size': 0.56,
            'icon-rotate': ['coalesce', ['get', 'course'], 0],
            'icon-rotation-alignment': 'map',
            'icon-allow-overlap': true,
            'icon-ignore-placement': true,
          },
          paint: {
            'icon-color': '#7dd3fc',
            'icon-opacity': 1.0,
            'icon-halo-color': '#431407',
            'icon-halo-width': 2.0,
          },
        });

        // AIS contacts use chart-like motion semantics: moving contacts are
        // heading triangles; stationary/unknown contacts are circles. Hue stays
        // semantic (Maritime AIS blue), while shape carries motion state.
        map.addLayer({
          id: 'vessels-stationary-layer', type: 'circle', source: 'vessels',
          filter: ['any', ['match', ['get', 'nav_status'], [1, 5], true, false], ['<=', ['coalesce', ['get', 'speed'], 0], 0.5]],
          paint: {
            'circle-radius': ['interpolate', ['linear'], ['zoom'], 5, 3.3, 10, 5.0, 14, 6.2],
            'circle-color': '#60a5fa',
            'circle-opacity': 0.96,
            'circle-stroke-color': '#021318',
            'circle-stroke-width': 1.4,
          },
        });
        map.addLayer({
          id: 'vessels-layer', type: 'symbol', source: 'vessels',
          filter: ['all', ['match', ['get', 'nav_status'], [1, 5], false, true], ['>', ['coalesce', ['get', 'speed'], 0], 0.5]],
          layout: {
            'icon-image': 'vessel-arrow',
            'icon-size': ['interpolate', ['linear'], ['zoom'], 5, 0.30, 10, 0.50, 14, 0.65],
            'icon-rotate': ['coalesce', ['get', 'heading_deg'], ['get', 'heading'], ['get', 'course'], 0],
            'icon-rotation-alignment': 'map',
            'icon-allow-overlap': true,
            'icon-ignore-placement': true,
          },
          paint: {
            'icon-color': '#38bdf8',
            'icon-opacity': 0.96,
            'icon-halo-color': '#021318',
            'icon-halo-width': 1.2,
          },
        });
        map.addLayer({
          id: 'selected-vessel-track', type: 'line', source: 'selected-vessel-track',
          paint: {
            'line-color': '#38bdf8',
            'line-width': ['interpolate', ['linear'], ['zoom'], 5, 1.3, 10, 2.2, 14, 3.0],
            'line-opacity': 0.88,
          },
        });

        // Civil NGO SAR vessels use the same triangle and differ only by hue.
        map.addLayer({
          id: 'vessels-ngo-stationary', type: 'symbol', source: 'vessels-ngo',
          filter: ['any', ['match', ['get', 'nav_status'], [1, 5], true, false], ['<=', ['coalesce', ['get', 'speed'], 0], 0.5]],
          layout: {
            'icon-image': 'vessel-arrow',
            'icon-size': ['interpolate', ['linear'], ['zoom'], 5, 0.28, 10, 0.44, 14, 0.56],
            'icon-rotate': 0,
            'icon-allow-overlap': true,
            'icon-ignore-placement': true,
          },
          paint: {
            'icon-color': '#8bf0c5',
            'icon-opacity': 1,
            'icon-halo-color': '#021318',
            'icon-halo-width': 1.5,
          },
        });
        map.addLayer({
          id: 'vessels-ngo', type: 'symbol', source: 'vessels-ngo',
          filter: ['all', ['match', ['get', 'nav_status'], [1, 5], false, true], ['>', ['coalesce', ['get', 'speed'], 0], 0.5]],
          layout: {
            'icon-image': 'vessel-arrow',
            'icon-size': ['interpolate', ['linear'], ['zoom'], 5, 0.36, 10, 0.58, 14, 0.72],
            'icon-rotate': ['coalesce', ['get', 'course'], 0],
            'icon-rotation-alignment': 'map',
            'icon-allow-overlap': true,
            'icon-ignore-placement': true,
          },
          paint: {
            'icon-color': '#8bf0c5',
            'icon-opacity': 1.0,
            'icon-halo-color': '#021318',
            'icon-halo-width': 1.8,
          },
        });

        // Sanctions is an evidence/identity facet, not a third incident
        // category. Only the server's <=10-minute strong-identifier subset is
        // rendered, and only while the public Sanctions filter is active.
        map.addLayer({
          id: 'sanctioned-vessels-stationary', type: 'symbol', source: 'sanctioned-vessels',
          filter: ['any', ['match', ['get', 'nav_status'], [1, 5], true, false], ['<=', ['coalesce', ['get', 'speed'], 0], 0.5]],
          layout: {
            visibility: 'none',
            'icon-image': 'vessel-arrow',
            'icon-size': ['interpolate', ['linear'], ['zoom'], 5, 0.30, 10, 0.46, 14, 0.58],
            'icon-rotate': 0,
            'icon-allow-overlap': true,
            'icon-ignore-placement': true,
          },
          paint: {
            'icon-color': '#f59e0b',
            'icon-opacity': 0.98,
            'icon-halo-color': '#1c1202',
            'icon-halo-width': 1.6,
          },
        });
        map.addLayer({
          id: 'sanctioned-vessels-moving', type: 'symbol', source: 'sanctioned-vessels',
          filter: ['all', ['match', ['get', 'nav_status'], [1, 5], false, true], ['>', ['coalesce', ['get', 'speed'], 0], 0.5]],
          layout: {
            visibility: 'none',
            'icon-image': 'vessel-arrow',
            'icon-size': ['interpolate', ['linear'], ['zoom'], 5, 0.38, 10, 0.60, 14, 0.74],
            'icon-rotate': ['coalesce', ['get', 'heading'], ['get', 'course'], 0],
            'icon-rotation-alignment': 'map',
            'icon-allow-overlap': true,
            'icon-ignore-placement': true,
          },
          paint: {
            'icon-color': '#f59e0b',
            'icon-opacity': 0.98,
            'icon-halo-color': '#1c1202',
            'icon-halo-width': 1.8,
          },
        });

        // Intel auto-drift impact points (above vessels)
        map.addLayer({
          id: 'intel-drift-point', type: 'circle', source: 'intel-drifts',
          filter: ['all', ['==', ['geometry-type'], 'Point'], ['!=', ['get', 'type'], 'current_estimate']],
          paint: {
            'circle-radius': 4,
            'circle-color': ['coalesce', ['get', 'visual_color'], '#ff3b3b'],
            'circle-opacity': 0.78,
            'circle-stroke-width': 1,
            'circle-stroke-color': '#04131a',
          },
        });
        map.addLayer({
          id: 'intel-current-estimate-halo', type: 'circle', source: 'intel-drifts',
          filter: ['all', ['==', ['geometry-type'], 'Point'], ['==', ['get', 'type'], 'current_estimate']],
          paint: {
            'circle-radius': 13,
            'circle-color': 'rgba(255,224,109,.18)',
            'circle-blur': .65,
          },
        });
        map.addLayer({
          id: 'intel-current-estimate', type: 'circle', source: 'intel-drifts',
          filter: ['all', ['==', ['geometry-type'], 'Point'], ['==', ['get', 'type'], 'current_estimate']],
          paint: {
            'circle-radius': 6,
            'circle-color': '#ffe06d',
            'circle-opacity': .96,
            'circle-stroke-width': 2,
            'circle-stroke-color': '#201b08',
          },
        });

        // Intel → vessel correlation lines (toggled manually via showVesselLinks)
        map.addLayer({
          id: 'intel-vessel-links-layer', type: 'line', source: 'intel-vessel-links',
          layout: { visibility: 'none' },
          paint: {
            // Correlation line inherits the linked signal's category colour,
            // never a severity ramp.
            'line-color': ['coalesce', ['get', 'visual_color'], '#8bf0c5'],
            'line-width': 1.5,
            'line-opacity': 0.7,
            'line-dasharray': [3, 3],
          },
        });

        // Intel event circles. AIS loiter/spike and the fusion engine's
        // correlated alerts each render on their own dedicated layer/source
        // (intel-spike / intel-fused) so they can be toggled and styled apart.
        const _noAisFilter = ['all',
          ['!=', ['get', 'type'], 'ais_spike'],
          ['!=', ['get', 'type'], 'ais_anomaly'],
          ['!=', ['get', 'type'], 'correlated_alert'],
        ];
        // SeaCommons classifies, it does not score: the marker outline is a
        // static contrast stroke, never a severity ramp. Category drives colour;
        // lifecycle drives opacity + outline dash; evidence quality drives the
        // uncertainty geometry — none of them is `severity`.
        const _markerStrokeW = 1.1;
        const _markerStrokeC = '#04131a';
        // One toggleable circle layer per OSINT signal category over the shared
        // intel-events source, so the Layers panel lets the operator pick which
        // signal types appear. `intel-events-layer` stays as the catch-all for
        // any type not in a named category.
        for (const cat of INTEL_MAP_CATEGORIES) {
          const f = ['in', ['get', 'type'], ['literal', cat.types]];
          map.addLayer({
            id: `intel-cat-${cat.key}-halo`, type: 'circle', source: 'intel-events', filter: f,
            paint: { 'circle-radius': 13, 'circle-color': cat.color, 'circle-opacity': 0.12, 'circle-blur': 0.8 },
          });
          map.addLayer({
            id: `intel-cat-${cat.key}`, type: 'symbol', source: 'intel-events', filter: f,
            layout: {
              'icon-image': 'vessel-arrow',
              'icon-size': ['interpolate', ['linear'], ['zoom'], 5, 0.34, 10, 0.52, 14, 0.66],
              'icon-rotate': 0,
              'icon-allow-overlap': true,
              'icon-ignore-placement': true,
            },
            paint: {
              'icon-color': cat.color,
              'icon-opacity': 0.96,
              'icon-halo-color': _markerStrokeC,
              'icon-halo-width': _markerStrokeW,
            },
          });
        }
        const _namedTypes = INTEL_MAP_CATEGORIES.flatMap((c) => c.types);
        const _otherFilter = ['all', _noAisFilter, ['!', ['in', ['get', 'type'], ['literal', _namedTypes]]]];
        map.addLayer({
          id: 'intel-events-halo', type: 'circle', source: 'intel-events', filter: _otherFilter,
          paint: { 'circle-radius': 13, 'circle-color': 'rgba(139,240,197,0.1)', 'circle-blur': 0.8 },
        });
        map.addLayer({
          id: 'intel-events-layer', type: 'symbol', source: 'intel-events', filter: _otherFilter,
          layout: {
            'icon-image': 'vessel-arrow',
            'icon-size': ['interpolate', ['linear'], ['zoom'], 5, 0.34, 10, 0.52, 14, 0.66],
            'icon-rotate': 0,
            'icon-allow-overlap': true,
            'icon-ignore-placement': true,
          },
          paint: {
            'icon-color': categoryColorExpression(),
            'icon-opacity': 0.96,
            'icon-halo-color': _markerStrokeC,
            'icon-halo-width': _markerStrokeW,
          },
        });

        // AIS spike / anomaly markers — small hollow rings, hidden by default.
        // anomaly (spoofing / dark-zone) reads warmer than a routine loiter spike.
        map.addLayer({
          id: 'intel-spike-layer', type: 'circle', source: 'intel-spike',
          layout: { visibility: 'none' },
          paint: {
            'circle-radius': ['match', ['get', 'type'], 'ais_anomaly', 5, 4],
            'circle-color': ['coalesce', ['get', 'visual_color'], '#60a5fa'],
            'circle-opacity': 0.16,
            'circle-stroke-width': 1.5,
            'circle-stroke-color': ['coalesce', ['get', 'visual_color'], '#60a5fa'],
          },
        });

        // Fusion engine: correlated OSINT alerts. Pulsing ring, colour by
        // maritime domain, always on top.
        const _domainColor = ['match', ['get', 'maritime_domain'],
          'sanctions', '#f472b6',
          'grey_zone', '#f59e0b',
          'safety',    '#38bdf8',
          'piracy',    '#a78bfa',
                       '#ff3b3b'];
        const _fusedColor = ['coalesce', ['get', 'visual_color'], _domainColor];
        map.addLayer({
          id: 'intel-fused-pulse', type: 'circle', source: 'intel-fused',
          paint: {
            'circle-radius': 8,
            'circle-color': 'transparent',
            'circle-stroke-color': _fusedColor,
            'circle-stroke-width': 2,
            'circle-stroke-opacity': 0.5,
          },
        });
        map.addLayer({
          id: 'intel-fused-core', type: 'circle', source: 'intel-fused',
          paint: {
            'circle-radius': 6,
            'circle-color': _fusedColor,
            'circle-opacity': 0.95,
            'circle-stroke-width': 1.5,
            'circle-stroke-color': '#04131a',
          },
        });

        // Real search-area polygon (core.intel.area_extract): a sea-only
        // shape that actually follows what the report names — a single
        // place, or the corridor between several — rather than an arbitrary
        // circle. area_low_confidence (couldn't be narrowed by weather, and
        // still large) gets a visibly different, more tentative dashed
        // outline and lower fill so "we genuinely don't know more than
        // this" reads differently from a normal, already-narrow area.
        map.addLayer({
          id: 'intel-distress-polygon-fill', type: 'fill', source: 'intel-distress',
          filter: ['==', ['geometry-type'], 'Polygon'],
          paint: {
            'fill-color': LIFECYCLE_AREA_FILL,
            'fill-opacity': ['*', ['match', ['get', 'location_precision'], 'area_low_confidence', 0.5, 1.0], LIFECYCLE_FILL_OPACITY, 0.3],
          },
        });
        map.addLayer({
          id: 'intel-distress-polygon-outline', type: 'line', source: 'intel-distress',
          filter: ['==', ['geometry-type'], 'Polygon'],
          paint: {
            'line-color': LIFECYCLE_AREA_STROKE,
            'line-width': 1.5,
            'line-opacity': LIFECYCLE_STROKE_OPACITY,
            'line-dasharray': ['match', ['get', 'location_precision'], 'area_low_confidence', ['literal', [2, 2]], ['literal', [1, 0]]],
          },
        });

        // Circle area indicator: when a report only carries a place/region
        // centroid (no exact position) and no real polygon was built for
        // it, show a real-world-scaled translucent circle instead of a pin
        // that implies false precision. Radius tracks location_uncertainty_m
        // in meters (not screen pixels) via the standard MapLibre
        // meters→pixels-per-zoom conversion.
        map.addLayer({
          id: 'intel-distress-area', type: 'circle', source: 'intel-distress',
          filter: ['all',
            ['==', ['geometry-type'], 'Point'],
            ['>', ['coalesce', ['get', 'location_uncertainty_m'], 0], 20000],
          ],
          paint: {
            'circle-radius': METERS_TO_PX_RADIUS,
            'circle-color': LIFECYCLE_AREA_FILL,
            'circle-opacity': ['*', LIFECYCLE_FILL_OPACITY, 0.3],
            'circle-stroke-color': LIFECYCLE_AREA_STROKE,
            'circle-stroke-opacity': LIFECYCLE_STROKE_OPACITY,
            'circle-stroke-width': 1,
          },
        });

        // ── Distress beacons — pulsing rings for all three lifecycle states
        // (active/resolved/archived). Color is a static per-feature match
        // expression (LIFECYCLE_*); only radius/opacity animate, so the
        // pulse loop never needs to know about color at all.
        //
        // Region-only centroids remain halo-only. Image-derived map pins may
        // intentionally render this centre marker together with the halo: the
        // centre is evidence-derived, while the halo communicates uncertainty.
        map.addLayer({
          id: 'intel-distress-pulse', type: 'circle', source: 'intel-distress',
          filter: _PRECISE_POINT_FILTER,
          paint: {
            'circle-radius': 8,
            'circle-color': LIFECYCLE_PULSE_FILL,
            'circle-opacity': ['*', 0.45, LIFECYCLE_FILL_OPACITY, 2.5],
            'circle-stroke-color': LIFECYCLE_PULSE_STROKE,
            'circle-stroke-opacity': LIFECYCLE_STROKE_OPACITY,
            'circle-stroke-width': 1.5,
          },
        });
        map.addLayer({
          id: 'intel-distress-core', type: 'symbol', source: 'intel-distress',
          filter: _PRECISE_POINT_FILTER,
          layout: {
            'icon-image': 'vessel-arrow',
            'icon-size': ['interpolate', ['linear'], ['zoom'], 5, 0.38, 10, 0.58, 14, 0.72],
            'icon-rotate': 0,
            'icon-allow-overlap': true,
            'icon-ignore-placement': true,
          },
          paint: {
            // Humanitarian is always red; lifecycle changes opacity, never hue.
            'icon-color': LIFECYCLE_CORE_COLOR,
            'icon-opacity': ['match', ['get', 'incident_lifecycle'], 'archived', 0.6, 'resolved', 0.8, 1],
            'icon-halo-color': '#fff4bf',
            'icon-halo-width': 1.5,
          },
        });
        const distressHoverPopup = new maplibregl.Popup({
          closeButton: false, closeOnClick: false, offset: 10,
          className: 'intel-hover-popup',
        });
        const LIFECYCLE_LABEL = { resolved: 'Resolved', needs_review: 'Needs review', archived: 'Archived', active: 'Active' };
        const _escapeHtml = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
          { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
        ));
        map.on('mouseenter', 'intel-distress-core', () => { map.getCanvas().style.cursor = 'pointer'; });
        map.on('mousemove', 'intel-distress-core', (event) => {
          const feature = event.features?.[0];
          if (!feature) return;
          const [lon, lat] = feature.geometry.coordinates;
          const props = feature.properties || {};
          const label = LIFECYCLE_LABEL[props.incident_lifecycle] || 'Active';
          // Vessel name / incident type when the backend title carries them
          // (AIS-sourced vessel_incident events always do — "<kind>: <sub> —
          // <NAME>"); lifecycle status becomes the secondary line instead of
          // the only thing shown.
          const html = props.title
            ? `<strong>${_escapeHtml(props.title)}</strong><span>${label} · ${lat.toFixed(4)}, ${lon.toFixed(4)}</span>`
            : `<strong>${label}</strong> · ${lat.toFixed(4)}, ${lon.toFixed(4)}`;
          distressHoverPopup
            .setLngLat([lon, lat])
            .setHTML(html)
            .addTo(map);
        });
        map.on('mouseleave', 'intel-distress-core', () => {
          map.getCanvas().style.cursor = APP_PROFILE === 'demo' && (activePanelRef.current === 'sim' || selectionModeRef.current) ? 'crosshair' : '';
          distressHoverPopup.remove();
        });
        const openDistressMapFeature = (event) => {
          const feature = event.features?.[0];
          if (!feature) return;
          const coordinates = panelFocusCoordinates({ feature });
          if (!coordinates) return;
          const [lon, lat] = coordinates;
          map.flyTo({
            center: coordinates,
            zoom: feature.geometry?.type === 'Polygon' ? 7.2 : 9,
            duration: 800,
            essential: true,
          });
          selectIntelReport(feature);
          const props = feature.properties || {};
          if (!isPublicLiveHost && props.id && props.drift_eligible
              && props.drift_status !== 'completed' && props.drift_status !== 'computing') {
            triggerIntelDrift(
              props.drift_event_id || props.id,
              lat,
              lon,
              props.drift_vessel_type,
            );
          }
          event.originalEvent?.stopPropagation?.();
        };
        for (const layerId of ['intel-distress-core', 'intel-distress-area', 'intel-distress-polygon-fill', 'intel-distress-polygon-outline']) {
          map.on('mouseenter', layerId, () => { map.getCanvas().style.cursor = 'pointer'; });
          map.on('mouseleave', layerId, () => { map.getCanvas().style.cursor = ''; });
          map.on('click', layerId, openDistressMapFeature);
        }

        // Pulse animation: oscillate the outer ring radius/opacity ~1.4s.
        // Only numeric properties are touched here — color stays whatever
        // the LIFECYCLE_* match expressions set per feature.
        let pulseRaf = null;
        const pulseStart = performance.now();
        function distressPulse(now) {
          if (!map.getLayer('intel-distress-pulse')) return;
          const elapsed = Math.max(0, now - pulseStart);
          const t = (elapsed % 1400) / 1400;                  // 0..1
          const r = 8 + 14 * t;                               // grow ring
          const o = 0.45 * (1 - t);                           // fade out
          map.setPaintProperty('intel-distress-pulse', 'circle-radius', r);
          map.setPaintProperty('intel-distress-pulse', 'circle-opacity', o);
          map.setPaintProperty('intel-distress-pulse', 'circle-stroke-opacity', Math.min(1, Math.max(0, 1 - t)));
          if (map.getLayer('intel-fused-pulse')) {
            map.setPaintProperty('intel-fused-pulse', 'circle-radius', 7 + 16 * t);
            map.setPaintProperty('intel-fused-pulse', 'circle-stroke-opacity', 0.6 * (1 - t));
          }
          if (map.getLayer('intel-selected-glow')) {
            map.setPaintProperty('intel-selected-glow', 'circle-radius', 19 + 13 * t);
            map.setPaintProperty('intel-selected-glow', 'circle-opacity', 0.32 * (1 - t));
          }
          pulseRaf = requestAnimationFrame(distressPulse);
        }
        pulseRaf = requestAnimationFrame(distressPulse);
        map.once('remove', () => { if (pulseRaf) cancelAnimationFrame(pulseRaf); });

        // Vessels reported near an active Live distress point (AIS, refreshed
        // periodically — see the liveNearbyVessels effect below).
        map.addLayer({
          id: 'live-nearby-vessels-halo', type: 'circle', source: 'live-nearby-vessels',
          paint: { 'circle-radius': 11, 'circle-color': 'rgba(56,189,248,0.22)', 'circle-blur': 0.8 },
        });
        map.addLayer({
          id: 'live-nearby-vessels-layer', type: 'symbol', source: 'live-nearby-vessels',
          layout: {
            'icon-image': 'vessel-arrow',
            'icon-size': 0.48,
            'icon-rotate': ['coalesce', ['get', 'course'], 0],
            'icon-rotation-alignment': 'map',
            'icon-allow-overlap': true,
            'icon-ignore-placement': true,
          },
          paint: {
            'icon-color': '#7dd3fc',
            'icon-halo-color': '#03212e',
            'icon-halo-width': 1.6,
          },
        });
        const liveVesselPopup = new maplibregl.Popup({
          closeButton: false, closeOnClick: false, offset: 10,
          className: 'intel-hover-popup',
        });
        map.on('mouseenter', 'live-nearby-vessels-layer', () => { map.getCanvas().style.cursor = 'pointer'; });
        map.on('mousemove', 'live-nearby-vessels-layer', (event) => {
          const feature = event.features?.[0];
          if (!feature) return;
          const [lon, lat] = feature.geometry.coordinates;
          const props = feature.properties || {};
          liveVesselPopup
            .setLngLat([lon, lat])
            .setHTML(`<strong>${_escapeHtml(props.ship_name || 'Vessel')}</strong><br/>${_escapeHtml(props.distance_km ?? '?')} km from distress`)
            .addTo(map);
        });
        map.on('mouseleave', 'live-nearby-vessels-layer', () => {
          map.getCanvas().style.cursor = APP_PROFILE === 'demo' && (activePanelRef.current === 'sim' || selectionModeRef.current) ? 'crosshair' : '';
          liveVesselPopup.remove();
        });

        // Per-episode NGO response analysis — lines from the episode to each
        // known NGO vessel + vessel points. Data is written by IntelDashboard's
        // "NGO response" panel from GET /api/v1/live/signals/{id}/response.
        map.addLayer({
          id: 'ngo-response-lines-layer', type: 'line', source: 'ngo-response-lines',
          paint: {
            // line-dasharray is a paint property; a data-driven match on the
            // boolean heading_toward needs `case` (match branch labels must be
            // strings/numbers, never booleans).
            'line-dasharray': [2, 1],
            'line-color': ['case', ['==', ['get', 'heading_toward'], true], '#38bdf8', '#94a3b8'],
            'line-width': 1.4,
            'line-opacity': 0.75,
          },
        });
        map.addLayer({
          id: 'ngo-response-points-layer', type: 'symbol', source: 'ngo-response-points',
          layout: {
            'icon-image': 'vessel-arrow',
            'icon-size': 0.56,
            'icon-rotate': ['coalesce', ['get', 'course'], 0],
            'icon-rotation-alignment': 'map',
            'icon-allow-overlap': true,
            'icon-ignore-placement': true,
          },
          paint: {
            'icon-color': '#8bf0c5',
            'icon-opacity': 1.0,
            'icon-halo-color': '#03212e',
            'icon-halo-width': 1.5,
          },
        });
        const ngoResponsePopup = new maplibregl.Popup({
          closeButton: false, closeOnClick: false, offset: 10,
          className: 'intel-hover-popup',
        });
        map.on('mouseenter', 'ngo-response-points-layer', () => { map.getCanvas().style.cursor = 'pointer'; });
        map.on('mousemove', 'ngo-response-points-layer', (event) => {
          const feature = event.features?.[0];
          if (!feature) return;
          const [lon, lat] = feature.geometry.coordinates;
          const props = feature.properties || {};
          ngoResponsePopup
            .setLngLat([lon, lat])
            .setHTML(
              `<strong>${_escapeHtml(props.name || 'NGO vessel')}</strong>` +
              (props.org ? `<br/>${_escapeHtml(props.org)}` : '') +
              `<br/>${props.heading_toward ? '→ heading toward' : 'not heading toward'}` +
              (props.eta_h != null ? `<br/>ETA ~${Number(props.eta_h).toFixed(1)}h` : '') +
              `<br/>${_escapeHtml(props.distance_nm)} nm`
            )
            .addTo(map);
        });
        map.on('mouseleave', 'ngo-response-points-layer', () => {
          map.getCanvas().style.cursor = APP_PROFILE === 'demo' && (activePanelRef.current === 'sim' || selectionModeRef.current) ? 'crosshair' : '';
          ngoResponsePopup.remove();
        });

        // Active SAR impact point — topmost layer
        map.addLayer({
          id: 'sar-case-points', type: 'circle', source: 'sar-case',
          filter: ['==', '$type', 'Point'],
          paint: {
            'circle-radius': ['match', ['get', 'type'], 'origin_point', 8, 6],
            'circle-color': ['match', ['get', 'type'], 'origin_point', '#ff3b3b', '#fff4bf'],
            'circle-stroke-width': 2,
            'circle-stroke-color': ['match', ['get', 'type'], 'origin_point', '#ffffff', '#ff7b54'],
          },
        });

        // NGO fleet was added earlier in this sequence (before intel-fused /
        // intel-spike, which draw hundreds of security dots on a busy day)
        // -- later-added layers paint on top in MapLibre, so a genuinely
        // small teal dot could end up buried under anomaly clutter at the
        // same pixel. moveLayer with no beforeId brings a layer to the very
        // top of whatever exists at call time; doing it last here, after
        // every other addLayer in this setup, guarantees the NGO fleet is
        // always the top-most vessel marker regardless of how much security
        // noise is on screen.
        for (const ngoLayerId of ['vessels-ngo-stationary', 'vessels-ngo']) {
          if (map.getLayer(ngoLayerId)) map.moveLayer(ngoLayerId);
        }

        // Hover popup — shows the event's coordinates without needing to open the sidebar.
        const intelHoverPopup = new maplibregl.Popup({
          closeButton: false, closeOnClick: false, offset: 10,
          className: 'intel-hover-popup',
        });
        const _escapeHoverHtml = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
          { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
        ));
        const _hoverLabel = (feature, lat, lon) => {
          const p = feature.properties || {};
          // props.title carries the resolved "<name> — <what>" summary when
          // the backend has one (vessel_incident/correlated_alert both do);
          // fall back to the generic alert_type/domain or bare coordinates.
          if (p.title) {
            // MDA anomaly features carry `category` (see mdaCategoryKey) --
            // show its short tag ("AIS circolare", "Sanzionata", ...)
            // instead of raw coordinates, same idea as the correlated_alert
            // domain label below.
            const mdaInfo = p.category ? MDA_ANOMALY_CATEGORIES[p.category] : null;
            const sub = mdaInfo ? mdaInfo.tag
              : p.type === 'correlated_alert' ? (p.maritime_domain || 'sar')
              : `${lat.toFixed(4)}, ${lon.toFixed(4)}`;
            return `<strong>${_escapeHoverHtml(p.title)}</strong><span>${_escapeHoverHtml(sub)}</span>`;
          }
          if (p.type === 'correlated_alert') {
            return `<strong>${(p.alert_type || 'alert').replace(/_/g, ' ')}</strong><span>${p.maritime_domain || 'sar'}</span>`;
          }
          const kind = (p.type || 'signal').replace(/_/g, ' ');
          return `<strong>${kind}</strong><span>${lat.toFixed(4)}, ${lon.toFixed(4)}</span>`;
        };
        // Every OSINT circle layer (per-category + catch-all + spike + fused)
        // shares the same hover popup and click-to-open behaviour.
        const _intelClickLayers = [
          ...INTEL_MAP_CATEGORIES.map((c) => `intel-cat-${c.key}`),
          'intel-events-layer', 'intel-spike-layer', 'intel-fused-core', 'intel-vessel-core', 'mda-anomaly-layer',
        ];
        for (const lid of _intelClickLayers) {
          map.on('mouseenter', lid, () => { map.getCanvas().style.cursor = 'pointer'; });
          map.on('mousemove', lid, (event) => {
            const feature = event.features?.[0];
            if (!feature) return;
            const [lon, lat] = feature.geometry.coordinates;
            intelHoverPopup.setLngLat([lon, lat]).setHTML(_hoverLabel(feature, lat, lon)).addTo(map);
          });
          map.on('mouseleave', lid, () => {
            map.getCanvas().style.cursor = APP_PROFILE === 'demo' && (activePanelRef.current === 'sim' || selectionModeRef.current) ? 'crosshair' : '';
            intelHoverPopup.remove();
          });
          map.on('click', lid, (event) => {
            const feature = event.features?.[0];
            if (!feature) return;
            const [lon, lat] = feature.geometry.coordinates;
            map.flyTo({ center: [lon, lat], zoom: 9, duration: 800 });
            setActivePanel('osint');
            if (!window.matchMedia('(max-width: 680px)').matches) setSidebarOpen(true);
            setMapPanel({ type: 'intel', feature });
            setConePanelHidden(false);
            const props = feature.properties || {};
            if ((lid === 'intel-events-layer' || lid === 'intel-vessel-core') && props.id && props.drift_eligible
                && props.drift_status !== 'completed' && props.drift_status !== 'computing') {
              triggerIntelDrift(
                props.drift_event_id || props.id,
                lat,
                lon,
                props.drift_vessel_type,
              );
            }
            event.originalEvent?.stopPropagation?.();
          });
        }
        map.on('mouseenter', 'intel-drift-line', () => { map.getCanvas().style.cursor = 'pointer'; });
        map.on('mouseleave', 'intel-drift-line', () => {
          map.getCanvas().style.cursor = APP_PROFILE === 'demo' && (activePanelRef.current === 'sim' || selectionModeRef.current) ? 'crosshair' : '';
        });
        map.on('click', 'intel-drift-line', (event) => {
          const feature = event.features?.[0];
          if (!feature) return;
          setMapPanel({
            type: 'trajectory',
            feature,
            caseStatus: feature.properties?.verification_status || 'modelled',
          });
          setConePanelHidden(false);
          event.originalEvent?.stopPropagation?.();
        });

        map.on('mouseenter', 'radio-receivers-layer', () => { map.getCanvas().style.cursor = 'pointer'; });
        map.on('mouseleave', 'radio-receivers-layer', () => {
          map.getCanvas().style.cursor = APP_PROFILE === 'demo' && (activePanelRef.current === 'sim' || selectionModeRef.current) ? 'crosshair' : '';
        });
        map.on('click', 'radio-receivers-layer', (event) => {
          const feature = event.features?.[0];
          if (!feature) return;
          setMapPanel({ type: 'radio_receiver', feature });
          setConePanelHidden(false);
          event.originalEvent?.stopPropagation?.();
        });

        map.on('mouseenter', 'radio-dsc-layer', () => { map.getCanvas().style.cursor = 'pointer'; });
        map.on('mouseleave', 'radio-dsc-layer', () => { map.getCanvas().style.cursor = ''; });
        map.on('click', 'radio-dsc-layer', (event) => {
          const feature = event.features?.[0];
          if (!feature) return;
          setMapPanel({ type: 'radio_message', feature });
          setConePanelHidden(false);
          event.originalEvent?.stopPropagation?.();
        });

        // Vessel click opens the same full Live report used by every signal.
        for (const lyr of ['vessels-layer', 'vessels-stationary-layer', 'vessels-ngo', 'vessels-ngo-stationary', 'proximity-vessels-layer', 'live-nearby-vessels-layer']) {
          map.on('mouseenter', lyr, () => { map.getCanvas().style.cursor = 'pointer'; });
          map.on('mouseleave', lyr, () => {
            map.getCanvas().style.cursor = APP_PROFILE === 'demo' && (activePanelRef.current === 'sim' || selectionModeRef.current) ? 'crosshair' : '';
          });
          map.on('click', lyr, (event) => {
            const feature = event.features?.[0];
            if (!feature) return;
            openVesselReport(feature);
            event.originalEvent?.stopPropagation?.();
          });
        }

        // Drift cone click
        map.on('mouseenter', 'sar-case-cone', () => { map.getCanvas().style.cursor = 'pointer'; });
        map.on('mouseleave', 'sar-case-cone', () => {
          map.getCanvas().style.cursor = APP_PROFILE === 'demo' && (activePanelRef.current === 'sim' || selectionModeRef.current) ? 'crosshair' : '';
        });
        map.on('click', 'sar-case-cone', (event) => {
          const feature = event.features?.[0];
          if (feature) {
            setMapPanel({ type: 'cone', feature, eventId: caseEventIdRef.current, caseStatus: caseStatusRef.current, simParams: simParamsRef.current });
            event.originalEvent?.stopPropagation?.();
          }
        });
        map.on('mouseenter', 'sar-case-points', () => { map.getCanvas().style.cursor = 'pointer'; });
        map.on('mouseleave', 'sar-case-points', () => {
          map.getCanvas().style.cursor = APP_PROFILE === 'demo' && (activePanelRef.current === 'sim' || selectionModeRef.current) ? 'crosshair' : '';
        });
        map.on('click', 'sar-case-points', (event) => {
          const feature = event.features?.[0];
          if (feature) {
            setMapPanel({ type: 'cone', feature, eventId: caseEventIdRef.current, caseStatus: caseStatusRef.current, simParams: simParamsRef.current });
            event.originalEvent?.stopPropagation?.();
          }
        });

        map.on('mousemove', (event) => {
          if (APP_PROFILE !== 'demo' || (activePanelRef.current !== 'sim' && !selectionModeRef.current)) return;
          setCursorHint({ visible: true, x: event.point.x, y: event.point.y });
        });
        map.on('mouseleave', () => {
          setCursorHint((cur) => ({ ...cur, visible: false }));
        });

        map.on('click', (event) => {
          // Globe intro: at world zoom a click dives into the Mediterranean
          // instead of opening the SAR scenario modal.
          if (map.getZoom() < 4.5) {
            stopSpin();
            map.flyTo({ center: [14.3, 35.8], zoom: 6.3, duration: 2400, essential: true });
            return;
          }
          if (isPublicLiveHost) return;
          const hit = map.queryRenderedFeatures(event.point, {
            layers: ['sar-case-cone', 'sar-case-points', 'vessels-layer', 'vessels-ngo', 'proximity-vessels-layer', 'intel-events-layer', 'intel-fused-core', 'intel-vessel-core', 'intel-spike-layer',
              ...INTEL_MAP_CATEGORIES.map((c) => `intel-cat-${c.key}`)].filter((l) => map.getLayer(l)),
          });
          if (hit.length > 0) return;

          const nextLat = event.lngLat.lat.toFixed(5);
          const nextLon = event.lngLat.lng.toFixed(5);
          setForm((cur) => ({ ...cur, lat: nextLat, lon: nextLon }));
          setSelectionMode(false);
          setCursorHint({ visible: false, x: 0, y: 0 });
          if (APP_PROFILE === 'demo' && (activePanelRef.current === 'sim' || selectionModeRef.current)) {
            setShowScenario(true);
            loadNearestVessels(nextLat, nextLon).catch(() => {});
            return;
          }
          loadWeatherFor(nextLat, nextLon).catch((err) => setError(err.message || 'Weather unavailable'));
        });

        map.getSource('weather-points')?.setData(weatherGrid);
        map.getSource('weather-vectors')?.setData(weatherVectors);
        map.getSource('vessels')?.setData(vessels);
        map.getSource('vessels-ngo')?.setData(sarMapFeatures);
        map.getSource('platforms')?.setData(platforms);
        map.getSource('alerts')?.setData(alerts);

        // Platform hover tooltip
        map.on('mouseenter', 'platforms-layer', (e) => {
          map.getCanvas().style.cursor = 'pointer';
          const p = e.features?.[0]?.properties || {};
          map.getCanvas().title = `${p.name} — ${p.operator} (${p.platform_type})`;
        });
        map.on('mouseleave', 'platforms-layer', () => {
          map.getCanvas().style.cursor = '';
          map.getCanvas().title = '';
        });
        map.getSource('sar-case')?.setData(caseGeojson);
        setMapError('');
        setMapReady(true);
        if (!isPublicLiveHost) loadWeatherGridForMap(map).catch(() => {});
        spinStep();   // begin the globe intro rotation
      });

      mapRef.current = map;
    }

    async function activateFallbackMap() {
      if (disposed || !mapNodeRef.current) return;
      try { liveMap?.remove(); } catch { /* partial MapLibre init */ }
      mapRef.current = null;
      mapNodeRef.current.replaceChildren();
      const fallback = await createFallbackMap({
        container: mapNodeRef.current,
        center: APP_PROFILE === 'live' ? [15.2, 36.1] : [14.3, 31.0],
        zoom: APP_PROFILE === 'live' ? 4.15 : 1.9,
        onFeatureSelect: selectIntelReport,
      });
      if (disposed) { fallback.destroy(); return; }
      fallbackMapRef.current = fallback;
      setMapError('');
      setFallbackReady(true);
    }

    initMap().catch(() => activateFallbackMap().catch(() => {
      if (!disposed) setMapError('Map unavailable. Live signals remain available below.');
    }));
    return () => {
      disposed = true;
      if (liveMap) liveMap.remove();
      fallbackMapRef.current?.destroy();
      fallbackMapRef.current = null;
    };
  }, []);

  // ── Map source updates ───────────────────────────────────────────────────────
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapReady || !map.isStyleLoaded()) return;
    map.getSource('weather-points')?.setData(weatherGrid);
    map.getSource('weather-vectors')?.setData(weatherVectors);
  }, [weatherGrid, weatherVectors, mapReady]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapReady || !map.isStyleLoaded()) return;
    map.getSource('vessels')?.setData(vessels);
  }, [vessels, mapReady]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapReady || !map.isStyleLoaded()) return;
    map.getSource('vessels-ngo')?.setData(sarMapFeatures);
  }, [sarMapFeatures, mapReady]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapReady || !map.isStyleLoaded()) return;
    const visible = isPublicLiveHost && liveFacets.sanctions;
    map.getSource('sanctioned-vessels')?.setData(
      visible ? sanctionedVessels : { type: 'FeatureCollection', features: [] },
    );
    for (const layerId of ['sanctioned-vessels-stationary', 'sanctioned-vessels-moving']) {
      if (map.getLayer(layerId)) {
        map.setLayoutProperty(layerId, 'visibility', visible ? 'visible' : 'none');
      }
    }
  }, [sanctionedVessels, liveFacets.sanctions, mapReady]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapReady || !map.isStyleLoaded()) return;
    map.getSource('platforms')?.setData(platforms);
  }, [platforms, mapReady]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapReady || !map.isStyleLoaded()) return;
    map.getSource('alerts')?.setData(alerts);
  }, [alerts, mapReady]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapReady) return;
    const src = map.getSource('sar-case');
    if (src) src.setData(caseGeojson);
  }, [caseGeojson, mapReady]);

  // Proximity overlay: update whenever nearest vessels or distress point changes
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapReady || !map.isStyleLoaded()) return;
    if (isPublicLiveHost) {
      map.getSource('proximity-vessels')?.setData({ type: 'FeatureCollection', features: [] });
      map.getSource('proximity-lines')?.setData({ type: 'FeatureCollection', features: [] });
      return;
    }
    const { vessels: vfc, lines: lfc } = buildProximityGeojson(nearestVessels, selectedLat, selectedLon);
    map.getSource('proximity-vessels')?.setData(vfc);
    map.getSource('proximity-lines')?.setData(lfc);
  }, [nearestVessels, selectedLat, selectedLon, mapReady]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapReady) return;
    const effectiveBaseMap = isPublicLiveHost ? 'standard' : baseMap;
    if (map.getLayer('osm')) map.setLayoutProperty('osm', 'visibility', effectiveBaseMap === 'standard' ? 'visible' : 'none');
    if (map.getLayer('satellite')) map.setLayoutProperty('satellite', 'visibility', effectiveBaseMap === 'satellite' ? 'visible' : 'none');
    try {
      window.localStorage.setItem('seacommons_base_map', effectiveBaseMap);
    } catch { /* quota */ }
    if (isPublicLiveHost && baseMap !== 'standard') setBaseMap('standard');
  }, [baseMap, mapReady]);

  // Layer group visibility — applied to every MapLibre layer in the group
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapReady) return;
    for (const group of LAYER_GROUPS) {
      const on = group.defaultOff ? layerVis[group.key] === true : layerVis[group.key] !== false;
      const enabled = isPublicLiveHost
        ? PUBLIC_LIVE_LAYER_GROUPS.has(group.key) && on
        : on;
      const vis = enabled ? 'visible' : 'none';
      for (const id of group.layers) {
        if (map.getLayer(id)) map.setLayoutProperty(id, 'visibility', vis);
      }
    }
    try { window.localStorage.setItem(layerVisStorageKey, JSON.stringify(layerVis)); } catch { /* quota */ }
  }, [layerVis, mapReady]);

  function isLayerGroupOn(key) {
    const group = LAYER_GROUPS.find((g) => g.key === key);
    return group?.defaultOff ? layerVis[key] === true : layerVis[key] !== false;
  }

  function toggleLayerGroup(key) {
    const group = LAYER_GROUPS.find((g) => g.key === key);
    setLayerVis((cur) => {
      const on = group?.defaultOff ? cur[key] === true : cur[key] !== false;
      return { ...cur, [key]: !on };
    });
  }

  // Signals selector "All" link — select-all, or deselect-all when every
  // category is already on.
  function toggleAllSignals() {
    const allOn = SIGNALS_TOGGLE_CATEGORIES.every((c) => isLayerGroupOn(c.groupKey));
    setLayerVis((cur) => {
      const next = { ...cur };
      for (const c of SIGNALS_TOGGLE_CATEGORIES) next[c.groupKey] = !allOn;
      return next;
    });
  }

  // Macro group (Humanitarian / Maritime) block tick — on/off
  // for every sub-category underneath in one click.
  function toggleMacroSignals(macroKey) {
    const macro = SIGNALS_MACRO_GROUPS.find((g) => g.key === macroKey);
    if (!macro) return;
    const allOn = macro.categories.every((c) => isLayerGroupOn(c.groupKey));
    setLayerVis((cur) => {
      const next = { ...cur };
      for (const c of macro.categories) next[c.groupKey] = !allOn;
      return next;
    });
  }

  function toggleMacroExpanded(macroKey) {
    setExpandedMacros((cur) => {
      const next = new Set(cur);
      if (next.has(macroKey)) next.delete(macroKey);
      else next.add(macroKey);
      return next;
    });
  }

  // Live per-category counts for the Signals selector — recomputed on every
  // intelEvents update (WS push / poll), independent of which categories are
  // currently toggled on, so switching one off never zeroes its own count.
  const signalCategoryCounts = useMemo(() => {
    const counts = {};
    for (const feature of intelEvents) {
      const key = signalCategoryOf(feature?.properties || {});
      counts[key] = (counts[key] || 0) + 1;
    }
    return counts;
  }, [intelEvents]);
  const alarmPhoneCount = useMemo(
    () => intelEvents.filter((f) => isAlarmPhoneSource(f?.properties?.source)).length,
    [intelEvents],
  );

  // 'other' has no dedicated toggle (see SIGNALS_TOGGLE_CATEGORIES) and stays
  // visible regardless of the selector, same as its map layer (bundled into
  // the always-relevant 'sar' group).
  const activeSignalCategories = useMemo(() => {
    const active = new Set(['other']);
    for (const c of SIGNALS_TOGGLE_CATEGORIES) {
      if (isLayerGroupOn(c.groupKey)) active.add(c.key);
    }
    return active;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [layerVis]);

  const visibleLivePointFeatures = useMemo(() => {
    let positioned = intelEvents.filter((feature) => (
      (feature.geometry?.type === 'Point' || feature.geometry?.type === 'Polygon')
      && feature.geometry?.coordinates
    ));
    if (!isPublicLiveHost) return positioned;
    const alarmPhoneOn = isLayerGroupOn('alarm_phone');
    return positioned.filter((feature) => {
      const props = feature.properties || {};
      if (!activeSignalCategories.has(signalCategoryOf(props))) return false;
      if (!alarmPhoneOn && isAlarmPhoneSource(props.source)) return false;
      if (liveFacets.satellite && !props.has_satellite) return false;
      if (liveFacets.radio && !props.has_radio) return false;
      if (liveFacets.sanctions && !props.sanctions_matched) return false;
      return true;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intelEvents, activeSignalCategories, layerVis.alarm_phone, liveFacets]);

  const fallbackVesselFeatures = useMemo(() => {
    const byId = new Map();
    const sources = [
      ...(sarMapFeatures.features || []),
      ...(liveFacets.sanctions ? (sanctionedVessels.features || []) : []),
    ];
    for (const feature of sources) {
      if (!feature?.geometry?.coordinates) continue;
      const report = vesselReportFeature(feature);
      const key = report.properties?.mmsi || report.properties?.id || `${report.geometry.coordinates}`;
      byId.set(String(key), report);
    }
    return [...byId.values()];
  }, [sarMapFeatures, sanctionedVessels, liveFacets.sanctions]);

  useEffect(() => {
    const fallback = fallbackMapRef.current;
    if (!fallbackReady || !fallback) return;
    const payload = { incidents: visibleLivePointFeatures, vessels: fallbackVesselFeatures };
    fallback.setFeatures(payload);
    if (!liveSignalsFramedRef.current && (visibleLivePointFeatures.length || fallbackVesselFeatures.length)) {
      fallback.fitFeatures(payload);
      liveSignalsFramedRef.current = true;
    }
  }, [fallbackReady, visibleLivePointFeatures, fallbackVesselFeatures]);

  // Intel events map layer — the backend's `kind` is "distress" (active),
  // "resolved" or "archived" (all three still distress-tier, pulsing, colored
  // by incident_lifecycle — see LIFECYCLE_* expressions above) or "context"
  // (news, static, no pulse). All non-context kinds share one source/layer
  // set; only fill color varies per feature via the match expressions.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapReady || !map.isStyleLoaded()) return;
    const positioned = visibleLivePointFeatures;
    const typeOf = (f) => f.properties?.type;
    const isVesselEpisode = (f) => String(f.properties?.episode_id || f.properties?.id || '').startsWith('vessel-episode:');
    const vesselEpisodes = positioned.filter(isVesselEpisode).map((feature) => {
      const properties = feature.properties || {};
      const visual = classifyEventVisual(properties);
      const points = Array.isArray(properties.observed_track) ? properties.observed_track : [];
      const latest = points[points.length - 1] || {};
      const course = Number.isFinite(Number(latest.cog))
        ? Number(latest.cog)
        : observedTrackCourse(points);
      return {
        ...feature,
        properties: {
          ...properties,
          course,
          visual_category: visual.key,
          visual_color: visual.color,
          visual_label: visual.label,
        },
      };
    });
    const nonVessel = positioned.filter((feature) => !isVesselEpisode(feature));
    const isSpike = (f) => typeOf(f) === 'ais_spike' || typeOf(f) === 'ais_anomaly';
    const fused = nonVessel.filter((f) => typeOf(f) === 'correlated_alert');
    const spikes = nonVessel.filter(isSpike);
    const rest = nonVessel.filter((f) => typeOf(f) !== 'correlated_alert' && !isSpike(f));
    const isDistressTier = (f) => {
      const p = f.properties || {};
      return p.kind === 'distress' || p.kind === 'resolved' || p.kind === 'needs_review' || p.kind === 'archived'
        || p.tier === 'operational' || p.type === 'distress';
    };
    const distress = rest.filter(isDistressTier);
    const distressIds = new Set(distress.map((f) => f.properties?.id));
    const others = rest.filter((f) => !distressIds.has(f.properties?.id));
    map.getSource('intel-events')?.setData({ type: 'FeatureCollection', features: others });
    map.getSource('intel-distress')?.setData({ type: 'FeatureCollection', features: distress });
    map.getSource('intel-fused')?.setData({ type: 'FeatureCollection', features: fused });
    map.getSource('intel-spike')?.setData({ type: 'FeatureCollection', features: spikes });
    map.getSource('intel-vessels')?.setData({ type: 'FeatureCollection', features: vesselEpisodes });
    const observedTracks = intelEvents.flatMap((feature) => {
      const p = feature.properties || {};
      const points = Array.isArray(p.observed_track) ? p.observed_track : [];
      return splitObservedTrackSegments(points).map((segment, segmentIndex) => ({
        type: 'Feature',
        geometry: { type: 'LineString', coordinates: segment.map((point) => [point.lon, point.lat]) },
        properties: {
          episode_id: p.episode_id || p.id,
          segment_index: segmentIndex,
          mmsi: p.linked_mmsi || p.mmsi,
          maritime_domain: p.maritime_domain,
          track_kind: 'observed_ais',
        },
      }));
    });
    map.getSource('intel-observed-tracks')?.setData({ type: 'FeatureCollection', features: observedTracks });
  }, [intelEvents, mapReady, visibleLivePointFeatures]);

  // MDA layer data
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapReady || !map.isStyleLoaded()) return;
    map.getSource('mda-reference')?.setData(mdaReference);
    map.getSource('mda-jamming')?.setData(mdaJamming);
    map.getSource('mda-anomaly')?.setData({
      type: 'FeatureCollection',
      features: mdaAnomalies
        .filter((a) => Number.isFinite(a.lat) && Number.isFinite(a.lon))
        .map((a) => ({
          type: 'Feature',
          geometry: { type: 'Point', coordinates: [a.lon, a.lat] },
          properties: { id: a.id, type: a.type, severity: a.severity, title: a.title,
            anomaly_type: a.anomaly_type, mmsi: a.mmsi,
            category: mdaCategoryKey(a.type, a.anomaly_type) },
        })),
    });
  }, [mdaReference, mdaJamming, mdaAnomalies, mapReady]);

  // Live nearby vessels: AIS positions around each active distress point,
  // refreshed on an interval. /api/v1/vessels/nearest reads an already-cached
  // in-memory registry (no new AIS/network call per request), so this is
  // cheap even polled every few minutes — 3 min balances "feels live" against
  // request volume on the free-tier API box.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapReady) return undefined;
    if (isPublicLiveHost) {
      map.getSource('live-nearby-vessels')?.setData({ type: 'FeatureCollection', features: [] });
      return undefined;
    }
    const LIVE_VESSEL_REFRESH_MS = 3 * 60 * 1000;
    const MAX_DISTRESS_POINTS = 8;
    const VESSELS_PER_POINT = 4;
    let alive = true;
    let timer = null;

    async function refresh() {
      const points = intelEvents
        .filter((f) => {
          const p = f.properties || {};
          return (p.tier === 'operational' || p.type === 'distress') && f.geometry?.coordinates;
        })
        .slice(0, MAX_DISTRESS_POINTS);
      if (!points.length) {
        map.getSource('live-nearby-vessels')?.setData({ type: 'FeatureCollection', features: [] });
        return;
      }
      const results = await Promise.all(points.map(async (point) => {
        const [lon, lat] = point.geometry.coordinates;
        try {
          const data = await fetchJson(
            apiBase,
            `/api/v1/vessels/nearest?lat=${encodeURIComponent(lat)}&lon=${encodeURIComponent(lon)}&limit=${VESSELS_PER_POINT}`,
            undefined,
            8000,
          );
          return (data?.vessels || []).map((v) => ({ ...v, distress_id: point.properties?.id }));
        } catch {
          return [];
        }
      }));
      if (!alive) return;
      const byMmsi = new Map();
      for (const vessel of results.flat()) {
        const key = vessel.mmsi || `${vessel.lat},${vessel.lon}`;
        const existing = byMmsi.get(key);
        if (!existing || (vessel.distance_km ?? Infinity) < (existing.distance_km ?? Infinity)) {
          byMmsi.set(key, vessel);
        }
      }
      const features = Array.from(byMmsi.values())
        .filter((v) => Number.isFinite(v.lat) && Number.isFinite(v.lon))
        .map((v) => ({
          type: 'Feature',
          geometry: { type: 'Point', coordinates: [v.lon, v.lat] },
          properties: {
            mmsi: v.mmsi, ship_name: v.ship_name, course: v.course,
            distance_km: v.distance_km, distress_id: v.distress_id,
          },
        }));
      map.getSource('live-nearby-vessels')?.setData({ type: 'FeatureCollection', features });
    }

    refresh();
    timer = window.setInterval(refresh, LIVE_VESSEL_REFRESH_MS);
    return () => { alive = false; if (timer) window.clearInterval(timer); };
  }, [intelEvents, mapReady, apiBase]);

  // Receiver mesh is provenance/infrastructure, not a third public content
  // compartment. Poll a bounded public-safe snapshot and render only coarse
  // station locations with operational state.
  useEffect(() => {
    const map = mapRef.current;
    if (!mapReady || !map?.getSource('radio-receivers')) return undefined;
    let alive = true;
    let timer = null;
    const refresh = async () => {
      try {
        const data = await fetchJson(apiBase, '/api/v1/live/receivers/mesh?limit=64', undefined, 10000);
        if (!alive) return;
        setReceiverMesh(data || { catalogued: 0, reachable: 0, eligible: 0, active: 0, receivers: [] });
        const features = (data?.receivers || [])
          .filter((receiver) => Number.isFinite(Number(receiver.latitude)) && Number.isFinite(Number(receiver.longitude)))
          .map((receiver) => ({
            type: 'Feature',
            geometry: { type: 'Point', coordinates: [Number(receiver.longitude), Number(receiver.latitude)] },
            properties: {
              receiver_id: receiver.receiver_id || '',
              station_label: receiver.station_label || receiver.receiver_id || 'Radio receiver',
              network_family: receiver.network_family || '',
              country: receiver.country || '',
              state: receiver.state || 'catalogued',
              score: Number(receiver.score || 0),
              active: Boolean(receiver.active),
              listen_available: Boolean(receiver.listen_available),
              frequency_hz: receiver.frequency_hz,
              mode: receiver.mode || '',
              capabilities_json: JSON.stringify(receiver.capabilities || []),
            },
          }));
        map.getSource('radio-receivers')?.setData({ type: 'FeatureCollection', features });
      } catch {
        if (alive) map.getSource('radio-receivers')?.setData({ type: 'FeatureCollection', features: [] });
      }
    };
    refresh();
    timer = window.setInterval(refresh, 60_000);
    return () => { alive = false; if (timer) window.clearInterval(timer); };
  }, [apiBase, mapReady]);

  // Decoded radio messages and correlated RF events are public-safe summaries.
  // Only DSC messages carrying an explicit decoded position become map points.
  useEffect(() => {
    const map = mapRef.current;
    if (!mapReady || !map?.getSource('radio-dsc')) return undefined;
    let alive = true;
    let timer = null;
    const refresh = async () => {
      try {
        const [eventData, messageData] = await Promise.all([
          fetchJson(apiBase, '/api/v1/live/radio/events?limit=50', undefined, 10000),
          fetchJson(apiBase, '/api/v1/live/radio/messages?limit=50', undefined, 10000),
        ]);
        if (!alive) return;
        const messages = Array.isArray(messageData?.messages) ? messageData.messages : [];
        setRadioSummary({ events: eventData?.events || [], messages });
        const features = messages
          .filter((message) => message.kind === 'dsc' && Number.isFinite(Number(message.latitude)) && Number.isFinite(Number(message.longitude)))
          .map((message) => ({
            type: 'Feature',
            geometry: { type: 'Point', coordinates: [Number(message.longitude), Number(message.latitude)] },
            properties: {
              observation_id: message.observation_id || '', kind: 'dsc',
              category: message.category || 'safety', mmsi: message.mmsi || '',
              nature_code: message.nature_code || '', frequency_hz: Number(message.frequency_hz || 0),
              observed_at: message.observed_at || '',
              ais_association_json: JSON.stringify(message.ais_association || null),
            },
          }));
        map.getSource('radio-dsc')?.setData({ type: 'FeatureCollection', features });
      } catch {
        if (alive) {
          setRadioSummary({ events: [], messages: [] });
          map.getSource('radio-dsc')?.setData({ type: 'FeatureCollection', features: [] });
        }
      }
    };
    refresh();
    timer = window.setInterval(refresh, 60_000);
    return () => { alive = false; if (timer) window.clearInterval(timer); };
  }, [apiBase, mapReady]);

  // Selected AIS report trail. The public map never downloads every historical
  // trajectory; it fetches the bounded public dossier only for the contact the
  // reader opened, then paints that LineString as an explicit AIS trail layer.
  useEffect(() => {
    const map = mapRef.current;
    const source = map?.getSource('selected-vessel-track');
    if (!map || !source || !mapReady || !map.isStyleLoaded()) return undefined;
    const props = resolvedMapPanel?.feature?.properties || {};
    const mmsi = props.entity_kind === 'vessel' ? props.mmsi || props.report_id : null;
    if (!mmsi || layerVis.ais_trails === false) {
      source.setData({ type: 'FeatureCollection', features: [] });
      return undefined;
    }
    let alive = true;
    fetchJson(apiBase, `/api/v1/live/vessels/${encodeURIComponent(mmsi)}/context?hours=168`, undefined, 12000)
      .then((data) => {
        if (alive) source.setData(data?.track || { type: 'FeatureCollection', features: [] });
      })
      .catch(() => {
        if (alive) source.setData({ type: 'FeatureCollection', features: [] });
      });
    return () => { alive = false; };
  }, [apiBase, layerVis.ais_trails, mapReady, resolvedMapPanel]);

  // Intel drift traces map layer
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapReady || !map.isStyleLoaded()) return;
    map.getSource('intel-drifts')?.setData(displayedIntelDrifts);
  }, [displayedIntelDrifts, mapReady]);

  useEffect(() => {
    const map = mapRef.current;
    const source = map?.getSource('intel-selected');
    if (!map || !source || !mapReady || !map.isStyleLoaded()) return;
    const feature = resolvedMapPanel?.type === 'intel' && !conePanelHidden ? resolvedMapPanel.feature : null;
    let coordinates = feature?.geometry?.type === 'Point' ? feature.geometry.coordinates : null;
    if (!coordinates && feature?.geometry?.type === 'Polygon') {
      const ring = feature.geometry.coordinates?.[0] || [];
      if (ring.length) {
        const sum = ring.reduce((acc, point) => [acc[0] + Number(point[0]), acc[1] + Number(point[1])], [0, 0]);
        coordinates = [sum[0] / ring.length, sum[1] / ring.length];
      }
    }
    if (!feature || !coordinates) {
      source.setData({ type: 'FeatureCollection', features: [] });
      return;
    }
    const visual = classifyEventVisual(feature.properties || {});
    source.setData({
      type: 'FeatureCollection',
      features: [{
        type: 'Feature',
        geometry: { type: 'Point', coordinates },
        properties: {
          id: feature.properties?.id,
          visual_category: visual.key,
          visual_color: visual.color,
        },
      }],
    });
  }, [conePanelHidden, mapReady, resolvedMapPanel]);

  // Intel → vessel correlation lines (manual toggle only)
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapReady || !map.isStyleLoaded()) return;
    const layer = map.getLayer('intel-vessel-links-layer');
    if (!layer) return;
    if (!showVesselLinks) {
      map.setLayoutProperty('intel-vessel-links-layer', 'visibility', 'none');
      map.getSource('intel-vessel-links')?.setData({ type: 'FeatureCollection', features: [] });
      return;
    }
    // Build a lookup of MMSI → vessel coordinates from the current registry
    const vesselByMmsi = {};
    for (const f of (vessels.features || [])) {
      const mmsi = f.properties?.mmsi;
      if (mmsi && f.geometry?.coordinates) vesselByMmsi[String(mmsi)] = f.geometry.coordinates;
    }
    const features = [];
    for (const ev of intelEvents) {
      const p = ev.properties || {};
      const evCoords = ev.geometry?.coordinates;
      if (!evCoords || !p.linked_mmsi) continue;
      const vesselCoords = vesselByMmsi[String(p.linked_mmsi)];
      if (!vesselCoords) continue;
      features.push({
        type: 'Feature',
        geometry: { type: 'LineString', coordinates: [evCoords, vesselCoords] },
        properties: {
          visual_color: classifyEventVisual(p).color,
          mmsi: p.linked_mmsi,
          title: p.title,
        },
      });
    }
    map.getSource('intel-vessel-links')?.setData({ type: 'FeatureCollection', features });
    map.setLayoutProperty('intel-vessel-links-layer', 'visibility', 'visible');
  }, [showVesselLinks, intelEvents, vessels, mapReady]);

  // ── Initial data load + polling ──────────────────────────────────────────────
  useEffect(() => {
    // Public Live now exposes the public-safe AIS registry as an explicit map
    // layer. Operator-only alert archives remain suppressed below.
    let alive = true;
    let running = false;           // guard: skip tick if previous loadAll still in flight
    let consecutiveErrors = 0;
    let lastVesselTs = null;       // ISO timestamp of last successful vessel fetch
    let vesselSnapshot = null;     // latest merged GeoJSON snapshot
    const seenAt = new Map();      // mmsi → epoch ms of last appearance in a feed diff
    const VESSEL_TTL_MS = 30 * 60 * 1000;  // drop vessels silent for 30 min

    async function fetchVessels() {
      // First call: full load. Subsequent: incremental diff via ?since=.
      const isFirst = !lastVesselTs;
      const url = lastVesselTs ? `/api/v1/vessels?since=${encodeURIComponent(lastVesselTs)}` : '/api/v1/vessels';
      const timeoutMs = isFirst ? 20000 : 12000;
      const data = await fetchJson(apiBase, url, undefined, timeoutMs);
      if (!data?.features) return;
      lastVesselTs = new Date().toISOString();
      const now = Date.now();
      data.features.forEach((f) => { if (f.properties?.mmsi) seenAt.set(f.properties.mmsi, now); });
      if (!vesselSnapshot) {
        vesselSnapshot = data;
      } else {
        const updated = new Map(data.features.map((f) => [f.properties?.mmsi, f]));
        const merged = vesselSnapshot.features.map((f) => updated.get(f.properties?.mmsi) || f);
        data.features.forEach((f) => { if (!vesselSnapshot.features.some((e) => e.properties?.mmsi === f.properties?.mmsi)) merged.push(f); });
        // Expire vessels not seen in any diff for VESSEL_TTL_MS — otherwise a
        // vessel that left the feed stays painted on the map until full reload.
        const fresh = merged.filter((f) => {
          const ts = seenAt.get(f.properties?.mmsi);
          return ts === undefined || (now - ts) < VESSEL_TTL_MS;
        });
        vesselSnapshot = { type: 'FeatureCollection', features: fresh };
      }
      return vesselSnapshot;
    }

    async function loadSummary() {
      try {
        const summaryPayload = await fetchJson(apiBase, '/api/v1/ops/summary', undefined, 8000);
        if (!alive) return;
        setSummary(summaryPayload);
        setTimezero(summaryPayload?.backend?.timezero || null);
      } catch { /* non-critical */ }
      try {
        const statsPayload = await fetchJson(apiBase, '/api/v1/ops/stats', undefined, 10000);
        if (!alive) return;
        setStats(statsPayload);
      } catch { /* non-critical */ }
    }

    async function loadAll() {
      if (running) return;  // previous fetch still in flight — skip this tick
      running = true;
      try {
        const [vesselsPayload, alertsPayload] = await Promise.all([
          isPublicLiveHost
            ? Promise.resolve({ type: 'FeatureCollection', features: [] })
            : fetchVessels(),
          isPublicLiveHost
            ? Promise.resolve({ type: 'FeatureCollection', features: [] })
            : fetchJson(apiBase, '/api/v1/alerts/geojson'),
        ]);
        if (!alive) return;
        if (vesselsPayload) setVessels(vesselsPayload);
        setAlerts(alertsPayload);
        setError('');
        consecutiveErrors = 0;
      } catch (err) {
        if (!alive) return;
        consecutiveErrors += 1;
        // Only show error after 2 consecutive failures to avoid transient flicker
        if (consecutiveErrors >= 2) setError(err.message || 'Backend unreachable');
      } finally {
        running = false;
        if (alive) setLoading(false);
      }
      if (!isPublicLiveHost) loadSummary();
    }
    loadAll();
    // 30s interval: vessels update on AIS heartbeat (~every 2 min) so 15s was wasteful
    const id = window.setInterval(loadAll, 30000);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, [apiBase]);

  useEffect(() => {
    if (!Number.isFinite(selectedLat) || !Number.isFinite(selectedLon)) return;
    let cancelled = false;
    // Debounced: lat/lon also change on every keystroke in the manual inputs,
    // so wait for typing to settle before hitting /vessels/nearest.
    const timer = window.setTimeout(() => {
      loadNearestVessels(selectedLat, selectedLon).catch(() => {
        if (!cancelled) setNearestVessels([]);
      });
      loadWeatherFor(selectedLat, selectedLon).catch(() => {
        // The renderer retains its last valid environmental state.
      });
    }, 400);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [apiBase, selectedLat, selectedLon]);

  // ── Derived data ─────────────────────────────────────────────────────────────
  const topStats = useMemo(() => {
    if (!summary) return [];
    const openAlerts = stats?.sar?.open_alerts ?? 0;
    return [
      { label: 'AIS',      value: summary.traffic?.registry?.active_30m ?? '—', tone: 'ok' },
      { label: 'Signals',  value: liveSignalTotal(liveModeCounts, intelEvents.length || stats?.signals?.recent_event_count || 0), tone: 'info' },
      { label: 'Feed',     value: FEED_STATUS_LABEL[feedStatus] || 'sync', tone: FEED_STATUS_TONE[feedStatus] || 'info' },
      { label: 'Alerts',   value: openAlerts,                                    tone: openAlerts > 0 ? 'warn' : 'default' },
      { label: 'Forensics',value: stats?.sar?.forensic_packets ?? '—',           tone: 'default' },
    ];
  }, [summary, stats, intelEvents.length, feedStatus, liveModeCounts]);

  const serviceRows = useMemo(() => {
    if (!summary) return [];
    return [
      { name: 'AISStream', state: summary.backend?.aisstream_connected ? 'live' : 'degraded', detail: summary.backend?.aisstream_connected ? `live feed (${summary.backend?.aisstream_messages} msgs)` : 'feed unavailable' },
      { name: 'CMEMS',     state: summary.backend?.cmems_configured ? 'ready' : 'degraded', detail: summary.backend?.cmems_configured ? 'live currents configured' : 'credentials missing' },
      { name: 'Image OCR', state: summary.backend?.image_ocr?.available ? 'ready' : 'degraded', detail: summary.backend?.image_ocr?.available ? 'map-screenshot coordinates readable' : (summary.backend?.image_ocr?.tesseract ? 'Pillow missing' : 'tesseract not installed') },
      { name: 'Redis',     state: summary.backend?.redis_configured ? 'ok' : 'off',      detail: summary.backend?.redis_configured ? 'cache active' : 'not configured' },
      { name: 'Database',  state: summary.backend?.database ?? '—',                      detail: summary.backend?.database === 'postgres' ? 'persistent' : 'local' },
      { name: 'Scheduler', state: summary.scheduler?.running ? 'live' : 'off',           detail: summary.scheduler?.running ? `${summary.scheduler?.jobs?.length || 0} jobs active` : 'not running' },
      { name: 'TimeZero',  state: timezero ? (timezero.enabled ? (timezero.reachable ? 'reachable' : 'off') : 'disabled') : 'pending', detail: timezero ? `${timezero.host}:${timezero.port}` : 'pending' },
      { name: 'WhatsApp',  state: summary.channels?.whatsapp?.configured ? 'ready' : 'off', detail: summary.channels?.whatsapp?.outbound_ready ? 'Twilio inbound and outbound credentials ready' : summary.channels?.whatsapp?.inbound_ready ? 'Twilio signed inbound ready' : 'Twilio credentials missing' },
      { name: 'Telegram',  state: summary.channels?.telegram?.configured ? 'ready' : 'off', detail: summary.channels?.telegram?.configured ? (summary.channels?.telegram?.operations_chat ? 'inbound and operations chat ready' : 'inbound ready; operations chat missing') : 'bot or webhook secret missing' },
      { name: 'Partner link', state: summary.channels?.partner_webhook?.configured ? 'ready' : 'off', detail: summary.channels?.partner_webhook?.configured ? 'signed partner webhook ready' : 'partner webhook secret missing' },
    ];
  }, [summary, timezero]);

  // ── Actions ──────────────────────────────────────────────────────────────────
  async function loadWeather() {
    setWeather(null);
    try {
      await loadWeatherFor(form.lat, form.lon);
    } catch (err) {
      setError(err.message || 'Weather unavailable');
    }
  }

  async function runSarCaseAt(lat, lon, overrides = {}) {
    const persons = form.persons;
    const vesselType = form.vessel_type;
    const riskLevel = form.risk_level;
    const activeSType = overrides.scenarioType || scenarioType;
    const latitude = Number(lat);
    const longitude = Number(lon);
    const personsAboard = Number(persons);
    if (!Number.isFinite(latitude) || latitude < -90 || latitude > 90 ||
        !Number.isFinite(longitude) || longitude < -180 || longitude > 180) {
      setCaseStatus('invalid coordinates');
      setError('Enter valid coordinates: latitude -90..90 and longitude -180..180.');
      return;
    }
    if (!Number.isInteger(personsAboard) || personsAboard < 1 || personsAboard > 10000) {
      setCaseStatus('invalid persons');
      setError('Persons aboard must be a whole number between 1 and 10,000.');
      return;
    }
    simParamsRef.current = { scenarioType: activeSType, vesselType, persons, riskLevel, lat, lon };
    setCaseStatus('starting…');
    const emptyGeo = { type: 'FeatureCollection', features: [] };
    setCaseGeojson(emptyGeo);
    mapRef.current?.getSource('sar-case')?.setData(emptyGeo);
    setMapPanel(null);
    setError('');

    let nearby = nearestVessels;
    if (!nearby.length) {
      try { nearby = await loadNearestVessels(lat, lon); } catch { nearby = []; }
    }

    pushCaseLog(`${activeSType.replace('_', ' ')} @ ${Number(lat).toFixed(4)}, ${Number(lon).toFixed(4)}`);
    if (nearby.length) {
      pushCaseLog(`Nearest: ${nearby.map((v) => `${v.ship_name} (${v.distance_nm.toFixed(1)} nm)`).join(', ')}`);
    }

    // Public Play is independent from Oracle: live forcing is snapshotted and
    // numerical integration runs inside a Web Worker. OpenDrift can validate
    // this same scenario asynchronously when dedicated capacity is available.
    if (APP_PROFILE === 'demo') {
      try {
        setCaseStatus('loading live fields');
        const liveWeather = await loadWeatherFor(latitude, longitude);
        const environmentSnapshot = buildEnvironmentSnapshot(liveWeather, latitude, longitude);
        const localScenarioId = globalThis.crypto?.randomUUID?.() || `scenario-${Date.now()}`;
        const observedAt = new Date().toISOString();
        setCaseEventId(localScenarioId);
        setCaseStatus('computing in browser');
        pushCaseLog(`Live snapshot ${environmentSnapshot.snapshot_id} · ${environmentSnapshot.frames.length} forcing frames`);

        const simulationResult = await computeDriftInWorker({
          scenario: {
            scenario_id: localScenarioId,
            observed_at: observedAt,
            origin: { lat: latitude, lon: longitude },
            subject: { kind: vesselType, persons: personsAboard },
          },
          environmentSnapshot,
          options: { duration_hours: 24, particles: 128 },
        }, (progress) => setCaseStatus(`computing ${Math.round(progress * 100)}%`));
        const scenario = createScenario({
          scenarioId: localScenarioId,
          lat: latitude,
          lon: longitude,
          observedAt,
          scenarioType: activeSType,
          vesselType,
          persons: personsAboard,
          riskLevel,
          environmentSnapshot,
          simulationResult,
        });
        const enriched = enrichCaseGeo(simulationResult.geojson, latitude, longitude);
        setCaseGeojson(enriched);
        mapRef.current?.getSource('sar-case')?.setData(enriched);
        setCaseStatus('completed · live browser engine');
        pushCaseLog(
          `Trajectory ready locally · ${simulationResult.diagnostics.particles} particles · ${simulationResult.diagnostics.steps} steps`,
        );
        const entry = {
          id: localScenarioId,
          label: `${activeSType.replace(/_/g, ' ')} @ ${latitude.toFixed(3)}, ${longitude.toFixed(3)}`,
          ts: observedAt,
          geojson: enriched,
          lat: latitude,
          lon: longitude,
          params: simParamsRef.current,
          scenario,
        };
        storeScenario(scenario);
        setActiveScenario(scenario);
        setSimHistory((previous) => [entry, ...previous.filter((item) => item.id !== localScenarioId)].slice(0, 10));
        setActiveSimId(localScenarioId);
        const cone = enriched.features.find((feature) => feature.properties?.type === 'cone_24h')
          || enriched.features.find((feature) => feature.geometry?.type === 'Polygon');
        setMapPanel({
          type: 'cone',
          feature: cone,
          eventId: localScenarioId,
          caseStatus: 'live browser engine',
          simParams: simParamsRef.current,
          legalAnalysis: null,
        });
        mapRef.current?.flyTo({
          center: [longitude, latitude], zoom: 8.4, essential: true, duration: 900,
        });
        return;
      } catch (err) {
        setCaseStatus('environment unavailable');
        setError(err.message || 'Live environmental simulation failed');
        pushCaseLog(`Simulation stopped: ${err.message || 'live fields unavailable'}`);
        return;
      }
    }

    try {
      const created = await fetchJson(apiBase, '/api/v1/alert', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          lat: latitude,
          lon: longitude,
          timestamp: new Date().toISOString(),
          persons: personsAboard,
          vessel_type: vesselType,
          risk_level: riskLevel,
          scenario_type: activeSType,
          domain: 'ocean_sar',
          environment: sceneEnvironmentSnapshot(weather),
        }),
      });

      setCaseStatus(`queued ${created.event_id.slice(0, 8)}`);
      setCaseEventId(created.event_id);
      pushCaseLog(`Alert queued ${created.event_id.slice(0, 8)}`);

      let consecutiveErrors = 0;
      for (let i = 0; i < 180; i += 1) {
        // ── Poll status — only retry on transient network errors ──────────────
        let statusResp;
        try {
          statusResp = await fetchJson(apiBase, `/api/v1/alert/${created.event_id}`);
          consecutiveErrors = 0;
        } catch (netErr) {
          consecutiveErrors += 1;
          if (consecutiveErrors >= 5) throw netErr;
          pushCaseLog(`Poll retry ${consecutiveErrors}/5…`);
          const delay = i < 20 ? 2000 : i < 60 ? 3000 : 5000;
          await new Promise((resolve) => window.setTimeout(resolve, delay));
          continue;
        }

        // ── Logical terminal states — fail/complete immediately, no retry ────
        if (statusResp.status === 'failed') {
          throw new Error(statusResp.drift_result?.metadata?.error || 'Simulation failed');
        }
        if (statusResp.status === 'completed') {
          const geojson = await fetchJson(apiBase, `/api/v1/alert/${created.event_id}/geojson`);
          const enriched = enrichCaseGeo(geojson, lat, lon);
          setCaseGeojson(enriched);
          mapRef.current?.getSource('sar-case')?.setData(enriched);
          setCaseStatus('completed');
          pushCaseLog(`Drift ready ${created.event_id.slice(0, 8)}`);
          // Save to session history (session-only — clears on page refresh)
          const _simEntry = {
            id: created.event_id,
            label: `${activeSType.replace(/_/g, ' ')} @ ${Number(lat).toFixed(3)}, ${Number(lon).toFixed(3)}`,
            ts: new Date().toISOString(),
            geojson: enriched,
            lat: Number(lat),
            lon: Number(lon),
            params: simParamsRef.current,
          };
          setSimHistory(prev => [_simEntry, ...prev.filter(s => s.id !== created.event_id).slice(0, 9)]);
          setActiveSimId(created.event_id);
          const trajFeature = geojson.features?.find(f => f.geometry?.type === 'LineString');
          const trajCoords = trajFeature?.geometry?.coordinates;
          const trajParam = trajCoords ? encodeURIComponent(JSON.stringify(trajCoords)) : '';
          const waveH = weather?.waves?.significant_height_m ?? '';
          const windMs = weather?.wind?.speed_ms ?? '';
          const analysisUrl = `/api/v1/zones/classify?lat=${lat}&lon=${lon}`
            + `&vessel_type=${encodeURIComponent(vesselType)}&persons=${persons}`
            + `&duration_h=24${trajParam ? `&traj=${trajParam}` : ''}`
            + `${waveH !== '' ? `&weather_wave=${waveH}` : ''}`
            + `${windMs !== '' ? `&weather_wind=${windMs}` : ''}`;
          fetchJson(apiBase, analysisUrl)
            .then(law => setMapPanel(prev => prev ? { ...prev, legalAnalysis: law } : prev))
            .catch(() => {});
          setMapPanel({ type: 'cone', feature: geojson.features?.[0] || null,
            eventId: created.event_id, caseStatus: 'completed',
            simParams: simParamsRef.current, legalAnalysis: null });
          mapRef.current?.flyTo({
            center: [Number(lon), Number(lat)],
            zoom: 8.4, essential: true, duration: 900,
          });
          return;
        }
        if (statusResp.status === 'processing' && caseStatusRef.current.startsWith('queued')) {
          setCaseStatus(`computing ${created.event_id.slice(0, 8)}`);
          pushCaseLog('Drift computing…');
        }
        const delay = i < 20 ? 2000 : i < 60 ? 3000 : 5000;
        await new Promise((resolve) => window.setTimeout(resolve, delay));
      }
      setCaseStatus('timeout');
      pushCaseLog('SAR case: timeout — server may still be computing');
    } catch (err) {
      setCaseStatus('error');
      setError(err.message || 'SAR case failed');
      pushCaseLog(`Error: ${err.message || 'unknown'}`);
    }
  }

  async function runSarCase(event) {
    if (event?.preventDefault) event.preventDefault();
    await runSarCaseAt(form.lat, form.lon, { scenarioType });
  }

  function updateSetting(key, value) {
    setLocalSettings((cur) => ({ ...cur, [key]: value }));
  }

  function setField(key, value) {
    setForm((cur) => ({ ...cur, [key]: value }));
  }

  function focusVessel(vessel) {
    const feature = {
      type: 'Feature',
      geometry: { type: 'Point', coordinates: [Number(vessel.lon), Number(vessel.lat)] },
      properties: { ...vessel, mmsi: vessel.mmsi, ship_name: vessel.ship_name || vessel.name },
    };
    openVesselReport(feature);
    mapRef.current?.flyTo({
      center: [Number(vessel.lon), Number(vessel.lat)],
      zoom: 8.7,
      essential: true,
      duration: 800,
    });
  }

  async function replaySim(sim) {
    let geojson = sim.geojson;
    if (!geojson) {
      // Persisted case restored from DB — fetch its drift GeoJSON on demand
      try {
        const archivePath = APP_PROFILE === 'demo'
          ? `/api/v1/live/archives/${encodeURIComponent(sim.id)}/geojson`
          : `/api/v1/alert/${encodeURIComponent(sim.id)}/geojson`;
        geojson = await fetchJson(apiBase, archivePath);
        setSimHistory((prev) => prev.map((s) => (s.id === sim.id ? { ...s, geojson } : s)));
      } catch (err) {
        setError(err.message || 'Could not load saved case');
        return;
      }
    }
    const enriched = enrichCaseGeo(geojson, sim.lat, sim.lon);
    setCaseGeojson(enriched);
    setActiveScenario(sim.scenario || null);
    mapRef.current?.getSource('sar-case')?.setData(enriched);
    setActiveSimId(sim.id);
    setCaseStatus('completed');
    mapRef.current?.flyTo({ center: [sim.lon, sim.lat], zoom: 8.4, essential: true, duration: 900 });
    setMapPanel({
      type: 'cone',
      feature: enriched.features?.[0] || null,
      eventId: sim.id,
      caseStatus: 'completed',
      simParams: sim.params,
      legalAnalysis: null,
    });
  }

  const intelStats = useMemo(() => {
    const by_type = {};
    const by_sev = {};
    for (const f of intelEvents) {
      const p = f.properties || {};
      if (p.type) by_type[p.type] = (by_type[p.type] || 0) + 1;
      if (p.severity) by_sev[p.severity] = (by_sev[p.severity] || 0) + 1;
    }
    return { total: intelEvents.length, by_type, by_sev };
  }, [intelEvents]);

  const isOnSim = APP_PROFILE === 'demo' && (activePanel === 'sim' || selectionMode);
  const simulationRunning = caseStatus.startsWith('starting') || caseStatus.startsWith('queued') || caseStatus.startsWith('computing');

  // ── Render ────────────────────────────────────────────────────────────────────
  return (
    <main className={`cop-shell ${play3D && APP_PROFILE === 'demo' ? 'is-play-mode' : ''} ${isPublicLiveHost ? 'is-live-mode' : ''}`}>
      <section className="map-stage">
        <div className={`map-frame ${play3D ? 'is-concealed' : ''}`} ref={mapNodeRef} />
        {APP_PROFILE === 'demo' ? (
          <PlayCesium
            active={play3D && playRenderer === 'cesium'}
            geojson={caseGeojson}
            weather={weather}
            lat={form.lat}
            lon={form.lon}
            persons={form.persons}
            selectionEnabled={playSimulationOpen && selectionMode}
            onPick={(pickedLat, pickedLon) => {
              setForm((current) => ({ ...current, lat: pickedLat.toFixed(5), lon: pickedLon.toFixed(5) }));
              setSelectionMode(false);
              loadWeatherFor(pickedLat, pickedLon).catch(() => {});
              loadNearestVessels(pickedLat, pickedLon).catch(() => {});
            }}
          />
        ) : null}
        {APP_PROFILE === 'demo' && UNREAL_PIXEL_STREAM_URL ? (
          <UnrealPixelStream
            active={play3D && playRenderer === 'unreal'}
            streamUrl={UNREAL_PIXEL_STREAM_URL}
            scenario={activeScenario}
          />
        ) : null}
        {APP_PROFILE === 'demo' && !play3D ? (
          <ArchiveTimeline mapRef={mapRef} mapReady={mapReady} apiBase={apiBase} />
        ) : null}

        <div className={`map-toolbar ${play3D ? 'is-3d' : ''}`}>
          <div className="toolbar-pills">
            {APP_PROFILE === 'demo' ? (
              <button className={`map-toggle ${play3D ? 'is-on' : ''}`} type="button" onClick={() => setPlay3D((current) => !current)}>
                {play3D ? '3D sea' : '2D chart'}
              </button>
            ) : null}
            {APP_PROFILE === 'demo' && play3D && UNREAL_PIXEL_STREAM_URL ? (
              <button
                className={`map-toggle ${playRenderer === 'unreal' ? 'is-on' : ''}`}
                type="button"
                onClick={() => setPlayRenderer((current) => current === 'cesium' ? 'unreal' : 'cesium')}
              >
                {playRenderer === 'unreal' ? 'Unreal stream' : 'Cesium local'}
              </button>
            ) : null}
            {topStats.map((stat) => (
              <Pill key={stat.label} label={`${stat.label}: ${stat.value}`} tone={stat.tone} />
            ))}
          </div>
        </div>

        {!play3D ? (
          <LayerToggles
            visibility={layerVis}
            onToggle={toggleLayerGroup}
            baseMap={baseMap}
            onBaseMapChange={setBaseMap}
            allowSatellite={!isPublicLiveHost}
            allowed={isPublicLiveHost ? PUBLIC_LIVE_LAYER_GROUPS : null}
          />
        ) : null}
        {!play3D ? <Legend /> : null}
        {!play3D && !isPublicLiveHost ? (
          <AlertRail
            intelEvents={intelEvents}
            onFocus={(lat, lon) => mapRef.current?.flyTo({ center: [Number(lon), Number(lat)], zoom: 8, duration: 800 })}
            onOpenCase={() => setActivePanel('cases')}
          />
        ) : null}

        {alertFlash ? (
          <div className={`map-banner alert ${sidebarOpen ? 'sidebar-open' : ''}`}>
            ⚡ Correlated alert · {(alertFlash.maritime_domain || 'sar')} · {(alertFlash.alert_type || '').replace(/_/g, ' ')}
          </div>
        ) : null}
        {error  ? <div className={`map-banner error ${sidebarOpen ? 'sidebar-open' : ''}`}>{error}</div> : null}
        {loading ? <div className={`map-banner ${sidebarOpen ? 'sidebar-open' : ''}`}>Connecting to backend…</div> : null}
        {mapError ? <div className={`map-banner map-banner--error ${sidebarOpen ? 'sidebar-open' : ''}`}>{mapError}</div> : null}

        {isOnSim && cursorHint.visible ? (
          <div className="map-cursor-hint" style={{ left: cursorHint.x + 18, top: cursorHint.y + 22 }}>
            Click to set distress origin
          </div>
        ) : null}

        {!play3D && !isPublicLiveHost ? (
          <div className={`map-overlay ${sidebarOpen ? 'sidebar-open' : ''}`}>
            <div className="overlay-card">
              <span className="overlay-label">Selected point</span>
              <strong>{Number.isFinite(selectedLat) ? `${selectedLat.toFixed(5)}, ${selectedLon.toFixed(5)}` : '—'}</strong>
              <span>{isOnSim ? 'Click map to set coordinates.' : 'Click map for point forecast.'}</span>
            </div>
          </div>
        ) : null}

        {selectedVessel && !play3D && !isPublicLiveHost ? (
          <div className={`map-overlay-vessel ${sidebarOpen ? 'sidebar-open' : ''}`}>
            <div className="overlay-card">
              <span className="overlay-label">Selected vessel</span>
              <strong>{selectedVessel.ship_name || selectedVessel.name || selectedVessel.mmsi}</strong>
              <span>
                {(selectedVessel.type || selectedVessel.ship_type || 'unknown').toString()} · {selectedVessel.speed ?? selectedVessel.sog ?? '—'} kn · {selectedVessel.mmsi || 'n/a'}
              </span>
            </div>
          </div>
        ) : null}

        {caseLog.length > 0 ? (
          <div className="case-log-panel">
            <div className="log-header">Case log — {caseStatus}</div>
            <ul className="log-list">
              {caseLog.map((entry) => (
                <li key={entry.id}>
                  <span>{entry.message}</span>
                  <time>{new Date(entry.at).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit' })}</time>
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        {/* Cone detail panel — right side, appears when clicking a drift cone or a signal marker */}
        {mapPanel?.type === 'intel' && !conePanelHidden && <div className="intel-report-map-overlay" aria-hidden="true" />}
        {['cone', 'trajectory', 'intel', 'radio_receiver', 'radio_message'].includes(mapPanel?.type) && !conePanelHidden && (
          <MapFloatingPanel
            panel={resolvedMapPanel}
            onClose={() => setConePanelHidden(true)}
            onComputeDrift={null}
            apiBase={apiBase}
            publicMode={isPublicLiveHost}
            intelDrifts={displayedIntelDrifts}
            loadNearestVessels={loadNearestVessels}
            onTriggerIntelDrift={triggerIntelDrift}
            sarResponse={selectedSarResponse}
          />
        )}
        {['cone', 'trajectory', 'intel', 'radio_receiver', 'radio_message'].includes(mapPanel?.type) && conePanelHidden && (
          <button
            className="cone-reopen-btn"
            onClick={() => setConePanelHidden(false)}
            title="Open drift panel"
          >
            SAR ›
          </button>
        )}

        {/* Scenario modal — center, appears when clicking empty map */}
        {APP_PROFILE === 'demo' && showScenario && (
          <ScenarioModal
            lat={form.lat}
            lon={form.lon}
            form={form}
            onFormChange={setField}
            onConfirm={({ scenarioType: sType }) => {
              setScenarioType(sType);
              setShowScenario(false);
              runSarCaseAt(form.lat, form.lon, { scenarioType: sType });
            }}
            onClose={() => setShowScenario(false)}
          />
        )}

        {APP_PROFILE === 'demo' ? (
          <>
            <button
              type="button"
              className={`play-simulation-launch ${playSimulationOpen ? 'is-open' : ''}`}
              aria-expanded={playSimulationOpen}
              onClick={() => {
                setPlaySimulationOpen((open) => {
                  const next = !open;
                  setSelectionMode(next);
                  return next;
                });
              }}
            >
              <span className="play-simulation-launch__pulse" />
              <span>
                <small>ENGINE</small>
                SIMULATION
              </span>
              <b>{playSimulationOpen ? '×' : '+'}</b>
            </button>

            <aside className={`play-simulation-panel ${playSimulationOpen ? 'is-open' : ''}`} aria-hidden={!playSimulationOpen}>
              <header className="play-simulation-panel__header">
                <div>
                  <p className="section-kicker">Live fields / browser physics</p>
                  <h2>Drift simulation</h2>
                </div>
                <span className={`play-origin-state ${selectionMode ? 'is-selecting' : ''}`}>
                  {selectionMode ? 'CLICK SEA' : 'ORIGIN SET'}
                </span>
              </header>

              <div className="play-origin-readout">
                <span>ORIGIN / WGS84</span>
                <strong>{form.lat}, {form.lon}</strong>
                <small>Click the 3D sea to move the simulation origin.</small>
              </div>

              <form onSubmit={runSarCase} className="play-simulation-form">
                <div className="demo-form">
                  <label>
                    Latitude
                    <input inputMode="decimal" value={form.lat} onChange={(e) => setField('lat', e.target.value)} />
                  </label>
                  <label>
                    Longitude
                    <input inputMode="decimal" value={form.lon} onChange={(e) => setField('lon', e.target.value)} />
                  </label>
                  <label>
                    Persons
                    <input type="number" min="1" value={form.persons} onChange={(e) => setField('persons', e.target.value)} />
                  </label>
                  <label>
                    Risk
                    <select value={form.risk_level} onChange={(e) => setField('risk_level', e.target.value)}>
                      {RISK_LEVELS.map((risk) => <option key={risk.value} value={risk.value}>{risk.label}</option>)}
                    </select>
                  </label>
                </div>

                <label className="field-block">
                  Vessel type
                  <select value={form.vessel_type} onChange={(e) => setField('vessel_type', e.target.value)}>
                    {VESSEL_TYPES.map((vessel) => <option key={vessel.value} value={vessel.value}>{vessel.label}</option>)}
                  </select>
                </label>

                <div className="play-simulation-actions">
                  <button type="submit" disabled={simulationRunning}>
                    {simulationRunning ? 'COMPUTING…' : 'RUN SIMULATION'}
                  </button>
                  <button
                    type="button"
                    className={selectionMode ? 'is-active' : ''}
                    disabled={simulationRunning}
                    onClick={() => setSelectionMode((active) => !active)}
                  >
                    {selectionMode ? 'CANCEL PICK' : 'PICK ON SEA'}
                  </button>
                </div>
              </form>

              <div className="play-engine-state">
                <span>ENGINE STATUS</span>
                <strong>{caseStatus}</strong>
              </div>

              <section className="play-nearest">
                <div className="play-nearest__head">
                  <span>NEAREST LIVE AIS</span>
                  <b>{nearestVessels.length}</b>
                </div>
                <ul>
                  {nearestVessels.slice(0, 5).map((vessel, index) => (
                    <li key={`${vessel.mmsi}-${vessel.distance_km}`}>
                      <span>0{index + 1}</span>
                      <strong>{vessel.ship_name}</strong>
                      <small>{formatDistance(vessel)}</small>
                    </li>
                  ))}
                </ul>
              </section>

              <section className="play-archive">
                <div className="play-nearest__head">
                  <span>RECENT SIMULATION ARCHIVE</span>
                  <b>{simHistory.length}</b>
                </div>
                {simHistory.length ? (
                  <ul>
                    {simHistory.slice(0, 6).map((simulation, index) => (
                      <li key={simulation.id} className={simulation.id === activeSimId ? 'is-active' : ''}>
                        <span>0{index + 1}</span>
                        <div>
                          <strong>{simulation.label}</strong>
                          <small>{new Date(simulation.ts).toLocaleString('en-GB', {
                            day: '2-digit',
                            month: 'short',
                            hour: '2-digit',
                            minute: '2-digit',
                          })}</small>
                        </div>
                        <button
                          type="button"
                          onClick={() => {
                            setSelectionMode(false);
                            replaySim(simulation);
                          }}
                        >
                          {simulation.id === activeSimId ? 'LOADED' : 'LOAD 3D'}
                        </button>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p>No completed simulation is available yet.</p>
                )}
              </section>
            </aside>
          </>
        ) : null}
      </section>

      {isPublicLiveHost ? (
        <>
          <aside className={`live-feed-panel ${sidebarOpen ? 'is-open' : 'is-collapsed'}`} aria-hidden={!sidebarOpen}>
            <header className="live-feed-panel__header">
              <div className="live-feed-panel__eyebrow">
                <span className="live-feed-panel__mark">SC</span>
                <span>SEACOMMONS / MEDITERRANEAN</span>
                <b><i /> 24 / 7</b>
              </div>
              <div className="live-feed-panel__title">
                <div>
                  <p>PUBLIC SIGNAL FIELD</p>
                  <h1>Live feed</h1>
                </div>
                <button type="button" onClick={() => setSidebarOpen(false)} aria-label="Collapse live feed">−</button>
              </div>
              <div className="signals-selector">
                <div className="signals-selector__row">
                  <span className="signals-selector__label">Investigations</span>
                  <a
                    href="#all"
                    className={`signals-selector__link ${
                      SIGNALS_TOGGLE_CATEGORIES.every((c) => isLayerGroupOn(c.groupKey)) ? 'is-active' : ''
                    }`}
                    onClick={(event) => { event.preventDefault(); toggleAllSignals(); }}
                  >All<span className="signals-selector__count">{liveSignalTotal(liveModeCounts, intelEvents.length)}</span></a>
                  <button
                    type="button"
                    className={`signals-selector__chevron ${signalsExpanded ? 'is-open' : ''}`}
                    aria-expanded={signalsExpanded}
                    aria-label={signalsExpanded ? 'Collapse investigation categories' : 'Expand investigation categories'}
                    onClick={() => setSignalsExpanded((open) => !open)}
                  ><i /></button>
                </div>
                {signalsExpanded && (
                  <div className="signals-selector__macros" role="group" aria-label="Investigation categories">
                    <div className="signals-selector__role-summary">
                      Cases {Number(liveRoleCounts?.humanitarian_case || 0)} · Episodes {Number(liveRoleCounts?.maritime_episode || 0)} · Evidence {Number(liveRoleCounts?.maritime_evidence || 0)} · Operational signals {Number(liveRoleCounts?.operational_signal || 0)}
                    </div>
                    <div className="signals-selector__role-summary">Evidence filters</div>
                    <div className="signals-selector__list">
                      {[
                        ['satellite', 'Satellite evidence'],
                        ['radio', 'Radio evidence'],
                        ['sanctions', 'Sanctions'],
                        ['sar_fleet', 'Civil SAR fleet'],
                      ].map(([key, label]) => (
                        <a
                          key={key}
                          href={`#facet-${key}`}
                          className={`signals-selector__link signals-selector__link--nested ${liveFacets[key] ? 'is-active' : ''}`}
                          aria-pressed={liveFacets[key]}
                          onClick={(event) => {
                            event.preventDefault();
                            setLiveFacets((current) => ({ ...current, [key]: !current[key] }));
                          }}
                        >
                          <span className="signals-selector__box" aria-hidden="true" />
                          {label}
                        </a>
                      ))}
                    </div>
                    {SIGNALS_MACRO_GROUPS.map((macro) => {
                      const macroOn = macro.categories.every((c) => isLayerGroupOn(c.groupKey));
                      const sampledMacroCount = macro.categories.reduce(
                        (sum, c) => sum + (signalCategoryCounts[c.key] || 0), 0,
                      );
                      const canonicalMacroCount = Number(liveModeCounts?.[macro.key]);
                      const macroCount = Number.isFinite(canonicalMacroCount) ? canonicalMacroCount : sampledMacroCount;
                      const macroExpanded = expandedMacros.has(macro.key);
                      return (
                        <div key={macro.key} className="signals-selector__macro">
                          <div className="signals-selector__row">
                            <a
                              href={`#${macro.key}`}
                              className={`signals-selector__link signals-selector__link--macro ${macroOn ? 'is-active' : ''}`}
                              aria-pressed={macroOn}
                              onClick={(event) => { event.preventDefault(); toggleMacroSignals(macro.key); }}
                            >
                              <span className="signals-selector__box" aria-hidden="true" />
                              {macro.label}
                              <span className="signals-selector__count">{macroCount}</span>
                            </a>
                            <button
                              type="button"
                              className={`signals-selector__chevron ${macroExpanded ? 'is-open' : ''}`}
                              aria-expanded={macroExpanded}
                              aria-label={macroExpanded ? `Collapse ${macro.label}` : `Expand ${macro.label}`}
                              onClick={() => toggleMacroExpanded(macro.key)}
                            ><i /></button>
                          </div>
                          {macroExpanded && (
                            <div className="signals-selector__list">
                              {macro.categories.map((cat) => (
                                <a
                                  key={cat.key}
                                  href={`#${cat.key}`}
                                  className={`signals-selector__link signals-selector__link--nested ${isLayerGroupOn(cat.groupKey) ? 'is-active' : ''}`}
                                  aria-pressed={isLayerGroupOn(cat.groupKey)}
                                  onClick={(event) => { event.preventDefault(); toggleLayerGroup(cat.groupKey); }}
                                >
                                  <span className="signals-selector__box" aria-hidden="true" />
                                  {cat.label}
                                  <span className="signals-selector__count">{signalCategoryCounts[cat.key] || 0}</span>
                                </a>
                              ))}
                              {/* Alarm Phone: a source, not a type -- a
                                  refinement within Distress. */}
                              {macro.key === 'humanitarian' && (
                                <a
                                  href="#alarm_phone"
                                  className={`signals-selector__link signals-selector__link--nested2 ${isLayerGroupOn('alarm_phone') ? 'is-active' : ''}`}
                                  aria-pressed={isLayerGroupOn('alarm_phone')}
                                  onClick={(event) => { event.preventDefault(); toggleLayerGroup('alarm_phone'); }}
                                >
                                  <span className="signals-selector__box" aria-hidden="true" />
                                  Alarm Phone
                                  <span className="signals-selector__count">{alarmPhoneCount}</span>
                                </a>
                              )}
                            </div>
                          )}
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
              <p className="live-feed-panel__continuity">
                <span />
                Approved collectors remain active when this map is closed.
              </p>

            </header>

            <div className="live-feed-panel__body">
              <IntelDashboard
                apiBase={apiBase}
                publicMode
                intelEvents={intelEvents}
                intelStats={intelStats}
                liveModeCounts={liveModeCounts}
                feedStatus={feedStatus}
                liveMode={liveMode}
                activeSignalCategories={activeSignalCategories}
                liveFacetFilters={liveFacets}
                alarmPhoneOn={isLayerGroupOn('alarm_phone')}
                showAisAlerts={showAisAlerts}
                setShowAisAlerts={setShowAisAlerts}
                mapRef={mapRef}
                selectedEventId={selectedIntelEventId}
                onOpenReport={openIntelReport}
              />
            </div>
          </aside>
          <button
            type="button"
            className={`live-feed-toggle ${sidebarOpen ? 'is-panel-open' : ''}`}
            onClick={() => setSidebarOpen((open) => !open)}
            aria-label={sidebarOpen ? 'Collapse live feed' : 'Open live feed'}
          >
            <i />
            LIVE FEED
          </button>
        </>
      ) : null}

      {!isPublicLiveHost && APP_PROFILE !== 'demo' ? (
        <>
      <nav className="workspace-nav" aria-label="Operational views">
        <span className={`runtime-badge runtime-badge--${APP_PROFILE}`}>{APP_PROFILE}</span>
        <button className={!sidebarOpen ? 'is-active' : ''} onClick={() => setSidebarOpen(false)}>Map</button>
        {['cases','live','osint','mda','settings','system'].map((view) => (
          <button key={view} className={sidebarOpen && activePanel === view ? 'is-active' : ''} onClick={() => { setActivePanel(view); setSidebarOpen(true); }}>
            {view === 'settings' ? 'Settings' : view === 'system' ? 'System' : view === 'mda' ? 'MDA' : view}
          </button>
        ))}
      </nav>
      <section className={`workspace-overlay ${sidebarOpen ? 'is-open' : ''}`} aria-hidden={!sidebarOpen}>
        <button type="button" className="workspace-close" aria-label="Close workspace" onClick={() => setSidebarOpen(false)}>×</button>
        {/* Mobile bottom-sheet grab handle — hidden on desktop via CSS */}
        <header className="workspace-header">
          <p className="workspace-kicker">SeaCommons / SAR pilot</p>
          <h2>Operational dashboard</h2>
          <div className="sidebar-tabs sidebar-tabs--6">
            <button className={activePanel === 'cases' ? 'is-active' : ''} onClick={() => setActivePanel('cases')}>Cases</button>
            <button className={activePanel === 'live'     ? 'is-active' : ''} onClick={() => setActivePanel('live')}>Live</button>
            <button className={activePanel === 'osint'    ? 'is-active' : ''} onClick={() => setActivePanel('osint')}>
              OSINT{intelEvents.length > 0 && <span className="tab-badge">{intelEvents.length}</span>}
            </button>
            <button className={activePanel === 'mda'      ? 'is-active' : ''} onClick={() => setActivePanel('mda')}>
              MDA{mdaAnomalies.length > 0 && <span className="tab-badge">{mdaAnomalies.length}</span>}
            </button>
            <button className={activePanel === 'settings' ? 'is-active' : ''} onClick={() => setActivePanel('settings')}>Settings</button>
            <button className={activePanel === 'system' ? 'is-active' : ''} onClick={() => setActivePanel('system')}>System</button>
          </div>
        </header>

        <div className="workspace-content">

          {activePanel === 'cases' ? (
            <CasesWorkspace apiBase={apiBase} fetchJson={fetchJson} onLocate={(lat, lon) => {
              setForm(cur => ({ ...cur, lat: String(lat), lon: String(lon) }));
              mapRef.current?.flyTo({ center: [Number(lon), Number(lat)], zoom: 8 });
            }} />
          ) : null}

          {/* ── LIVE TAB ── */}
          {activePanel === 'live' ? (
            <div className="panel-stack">
              <section className="panel-block">
                <p className="section-kicker">Live conditions</p>
                <h3>Operational weather</h3>
                <div className="info-grid">
                  <div className="info-box">
                    <strong>Overlay</strong>
                    <span>Native weather grid</span>
                  </div>
                  <div className="info-box">
                    <strong>Weather source</strong>
                    <span>{weather ? weather.source : 'Open-Meteo / CMEMS'}</span>
                  </div>
                </div>
                {weather ? (
                  <div className="weather-card">
                    <span>Wind {weather.wind?.speed_ms ?? '—'} m/s {weather.wind?.direction_label ?? ''}</span>
                    <span>Wave {weather.waves?.significant_height_m ?? '—'} m</span>
                    <span>Current {weather.ocean?.current_speed_ms ?? '—'} m/s</span>
                    <span>Drift {weather.sar_conditions?.drift_speed_ms ?? '—'} m/s — {weather.sar_conditions?.drift_dir_deg ?? '—'}°</span>
                  </div>
                ) : null}
                <div className="action-row">
                  <button onClick={loadWeather}>Load point forecast</button>
                  <button onClick={() => loadWeatherGridForMap(mapRef.current).catch((err) => setError(err.message || 'Weather grid unavailable'))}>Refresh overlay</button>
                </div>
              </section>

              <CivilSarFleetPanel
                fleet={ngoVessels}
                onSelectVessel={(mmsi) => {
                  const feature = (ngoVessels.features || []).find(
                    (f) => String(f.properties?.mmsi || '') === String(mmsi),
                  );
                  const coords = feature?.geometry?.coordinates;
                  if (coords && mapRef?.current) {
                    mapRef.current.flyTo({ center: coords, zoom: 9, duration: 800 });
                  }
                }}
              />

              <section className="panel-block">
                <p className="section-kicker">Recent signals</p>
                <h3>Event intake</h3>
                <ul className="signal-list">
                  {(stats?.signals?.recent_events || []).map((item) => (
                    <li key={`${item.timestamp}-${item.vessel_id || 'evt'}`}>
                      <strong>{item.ship_name || item.vessel_id || item.event_type}</strong>
                      <span>{item.adapter || item.protocol || 'unknown source'}</span>
                      <span>{item.status || item.event_type}</span>
                    </li>
                  ))}
                </ul>
              </section>
            </div>
          ) : null}

          {/* ── OSINT TAB ── */}
          {activePanel === 'osint' ? (
            <IntelDashboard
              apiBase={apiBase}
              publicMode={isPublicLiveHost}
              intelEvents={intelEvents}
              intelStats={intelStats}
              liveModeCounts={liveModeCounts}
              liveMode={liveMode}
              feedStatus={feedStatus}
              showAisAlerts={showAisAlerts}
              setShowAisAlerts={setShowAisAlerts}
              mapRef={mapRef}
              selectedEventId={selectedIntelEventId}
              onOpenReport={openIntelReport}
            />
          ) : null}

          {activePanel === 'mda' ? (
            <MdaPanel
              apiBase={apiBase}
              fetchJson={fetchJson}
              anomalies={mdaAnomalies}
              onFocus={(lat, lon) => mapRef.current?.flyTo({ center: [Number(lon), Number(lat)], zoom: 8, duration: 800 })}
              onOpenCase={() => setActivePanel('cases')}
            />
          ) : null}

          {/* ── DEMO TAB ── */}
          {APP_PROFILE === 'demo' && activePanel === 'sim' ? (
            <div className="panel-stack">
              <section className="panel-block">
                <p className="section-kicker">SAR simulation</p>
                <h3>New drift case</h3>
                <p className="panel-copy">
                  Click the map to set coordinates. Before computing drift, SeaCommons queries and displays the 5 nearest vessels to the distress point.
                </p>

                <form onSubmit={runSarCase}>
                  <div className="demo-form">
                    <label>
                      Latitude
                      <input inputMode="decimal" value={form.lat} onChange={(e) => setField('lat', e.target.value)} />
                    </label>
                    <label>
                      Longitude
                      <input inputMode="decimal" value={form.lon} onChange={(e) => setField('lon', e.target.value)} />
                    </label>
                    <label>
                      Persons aboard
                      <input type="number" min="1" value={form.persons} onChange={(e) => setField('persons', e.target.value)} />
                    </label>
                    <label>
                      Risk level
                      <select value={form.risk_level} onChange={(e) => setField('risk_level', e.target.value)}>
                        {RISK_LEVELS.map((r) => <option key={r.value} value={r.value}>{r.label}</option>)}
                      </select>
                    </label>
                  </div>

                  <div className="field-block" style={{ marginTop: 8 }}>
                    <span>Vessel type</span>
                    <select value={form.vessel_type} onChange={(e) => setField('vessel_type', e.target.value)}>
                      {VESSEL_TYPES.map((v) => <option key={v.value} value={v.value}>{v.label}</option>)}
                    </select>
                  </div>

                  <div className="action-row">
                    <button type="submit" disabled={simulationRunning}>{simulationRunning ? 'Computing…' : 'Compute drift'}</button>
                    <button type="button" disabled={simulationRunning} onClick={() => setSelectionMode((v) => !v)}>
                      {selectionMode ? 'Cancel selection' : 'Pick from map'}
                    </button>
                  </div>
                </form>

                {selectionMode ? (
                  <div className="demo-note">
                    Selection mode active — click the map to set coordinates.
                  </div>
                ) : null}

                <div className="status-strip">
                  <span>Status</span>
                  <strong>{caseStatus}</strong>
                </div>
              </section>

              <section className="panel-block">
                <p className="section-kicker">Proximity search</p>
                <h3>5 nearest vessels</h3>
                <ul className="service-list">
                  {nearestVessels.length ? nearestVessels.map((vessel, idx) => (
                    <li key={`${vessel.mmsi}-${vessel.distance_km}`}>
                      <div>
                        <strong>#{idx + 1} {vessel.ship_name}</strong>
                        <span>{(vessel.type || 'unknown').toString()} — {formatDistance(vessel)}</span>
                      </div>
                      <button className="link-button" type="button" onClick={() => focusVessel(vessel)}>
                        Focus
                      </button>
                    </li>
                  )) : (
                    <li>
                      <div>
                        <strong>No vessels found</strong>
                        <span>Select valid coordinates or check the AIS feed.</span>
                      </div>
                    </li>
                  )}
                </ul>
              </section>

              <SimHistory
                history={simHistory}
                activeId={activeSimId}
                onReplay={replaySim}
                onRemove={(id) => setSimHistory(prev => prev.filter(s => s.id !== id))}
                onClear={() => { setSimHistory([]); setActiveSimId(null); }}
              />
            </div>
          ) : null}


          {/* ── SETTINGS / SYSTEM ── */}
          {activePanel === 'settings' ? (
            <SettingsWorkspace />
          ) : null}

          {activePanel === 'system' ? (
            <SystemWorkspace
              apiBase={apiBase}
              setApiBase={setApiBase}
              fetchJson={fetchJson}
              localSettings={localSettings}
              updateSetting={updateSetting}
              serviceRows={serviceRows}
            />
          ) : null}

        </div>
      </section>
        </>
      ) : null}
    </main>
  );
}

createRoot(document.getElementById('root')).render(<AuthGate><App /></AuthGate>);
