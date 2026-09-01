import React, { useEffect, useMemo, useRef, useState } from 'react';

import { classifyEventVisual, descriptionOf, eventAnomalyLabel } from '../features/intel/categories.js';
import {
  assessmentConfidence,
  caveatsText,
  evidenceLevelText,
  interpretationText,
  observationText,
} from '../features/live/assessmentPresentation.js';
import { shipTypeLabel } from '../features/live/vesselType.js';
import { liveListenHttpUrl, pcm16leToFloat32 } from '../features/live/radioListen.js';

const HORIZON = {
  cone_6h:  '6 h drift zone',
  cone_12h: '12 h drift zone',
  cone_18h: '18 h drift zone',
  cone_24h: '24 h drift zone',
};

// SeaCommons classifies; it does not score. No LOW/MEDIUM/HIGH/CRITICAL risk
// rating is presented. Panels carry measured / scientific quantities only,
// rendered in one neutral accent.
const MEASURED_ACCENT = '#8bf0c5';

function SectionLabel({ children }) {
  return (
    <div style={{
      fontSize: 9, color: '#87cabc', textTransform: 'uppercase',
      letterSpacing: '0.13em', marginBottom: 5,
    }}>
      {children}
    </div>
  );
}

function Row({ label, value, color, mono }) {
  return (
    <div className="cone-row">
      <span>{label}</span>
      <strong style={color ? { color } : mono ? { fontFamily: 'monospace', fontSize: 10 } : {}}>
        {value ?? '—'}
      </strong>
    </div>
  );
}

function ZoneBadge({ zone }) {
  const col = MEASURED_ACCENT;
  return (
    <div style={{
      padding: '4px 8px', marginBottom: 4, borderRadius: 3,
      background: `${col}18`, border: `1px solid ${col}44`,
    }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <span style={{ fontSize: 10, fontWeight: 700, color: col }}>{zone.name}</span>
        <span style={{
          fontSize: 9, background: `${col}30`, color: col,
          padding: '1px 5px', borderRadius: 2,
        }}>{zone.zone_type.replace(/_/g, ' ')}</span>
      </div>
      <div style={{ fontSize: 10, color: '#87cabc', marginTop: 2 }}>
        {zone.mrcc}
        {zone.mrcc_tel ? <span style={{ opacity: 0.7 }}> · {zone.mrcc_tel}</span> : null}
      </div>
      {zone.warning && (
        <div style={{ fontSize: 9, color: '#ff3b3b', marginTop: 3, fontWeight: 600 }}>
          ⚠ {zone.warning}
        </div>
      )}
    </div>
  );
}

function SurvivalBar({ pct, color }) {
  return (
    <div style={{ height: 4, background: 'rgba(255,255,255,0.08)', borderRadius: 2, margin: '4px 0 6px' }}>
      <div style={{ height: '100%', width: `${Math.min(100, pct)}%`, background: color, borderRadius: 2, transition: 'width 0.4s' }} />
    </div>
  );
}

function LegalNote({ note, idx }) {
  return (
    <div style={{
      fontSize: 10, color: '#a0c4bb', lineHeight: 1.45,
      padding: '3px 0 3px 10px',
      borderLeft: '2px solid rgba(139,240,197,0.2)',
      marginBottom: 3,
    }}>
      {note}
    </div>
  );
}

function LocationView({ panel, onComputeDrift }) {
  return (
    <>
      <div className="cone-section">
        <Row label="Lat" value={panel.lat} mono />
        <Row label="Lon" value={panel.lon} mono />
      </div>

      {panel.vessels && panel.vessels.length > 0 && (
        <div className="cone-section">
          <SectionLabel>Nearest vessels</SectionLabel>
          {panel.vessels.slice(0, 3).map((v, i) => (
            <Row key={v.mmsi || i}
              label={v.ship_name || v.mmsi || '—'}
              value={v.distance_nm ? v.distance_nm.toFixed(1) + ' nm' : '—'}
            />
          ))}
        </div>
      )}

      {panel.weather && (
        <div className="cone-section">
          <SectionLabel>Conditions</SectionLabel>
          <Row label="Wind" value={`${panel.weather.wind?.speed_ms} m/s ${panel.weather.wind?.direction_label}`} />
          <Row label="Waves" value={`${panel.weather.waves?.significant_height_m} m`} />
        </div>
      )}

      <div className="cone-section" style={{ borderBottom: 'none' }}>
        <button
          onClick={onComputeDrift}
          style={{
            width: '100%', border: 0, borderRadius: 3, padding: '7px 0',
            background: 'linear-gradient(135deg,#83f4df,#70a2ff)',
            color: '#061015', fontWeight: 700, fontSize: 11, cursor: 'pointer',
          }}
        >
          Compute drift here
        </button>
      </div>
    </>
  );
}

function ConeView({ panel }) {
  const props = panel.feature?.properties || {};
  const label = HORIZON[props.type] || props.type || 'Drift zone';
  const sim = panel.simParams || {};
  const law = panel.legalAnalysis;

  const survivalPct = law
    ? Math.min(100, (law.survival.estimated_survival_hours / (law.duration_h || 24)) * 100)
    : null;
  const survivalColor = MEASURED_ACCENT;

  return (
    <>
      {/* ── Drift horizon & status ── */}
      <div className="cone-section">
        <Row label="Horizon" value={label} />
        {panel.eventId && <Row label="Event" value={panel.eventId.slice(0, 12) + '…'} />}
        {panel.caseStatus && <Row label="Status" value={panel.caseStatus} />}
        {law?.drift_nm != null && <Row label="Drift distance" value={`${law.drift_nm} nm`} />}
      </div>

      {/* ── Search area (probability of containment) ── */}
      {Number.isFinite(Number(props.area_km2)) && (
        <div className="cone-section">
          <SectionLabel>Search area · 90% containment</SectionLabel>
          <Row label="Area" value={`${Number(props.area_km2).toFixed(1)} km²`} />
          {Number.isFinite(Number(props.radius_p90_m)) && (
            <Row label="Radius (90%)" value={`${(Number(props.radius_p90_m) / 1000).toFixed(1)} km`} />
          )}
          {Number.isFinite(Number(props.radius_p50_m)) && (
            <Row label="Radius (50%)" value={`${(Number(props.radius_p50_m) / 1000).toFixed(1)} km`} />
          )}
          {Array.isArray(props.semi_axes_p90_m) && props.semi_axes_p90_m.length === 2 && (
            <Row
              label="Axes (90%)"
              value={`${(props.semi_axes_p90_m[0] / 1000).toFixed(1)} × ${(props.semi_axes_p90_m[1] / 1000).toFixed(1)} km`}
              mono
            />
          )}
          {props.particles && <Row label="Particles" value={props.particles} />}
        </div>
      )}

      {/* ── Simulation parameters ── */}
      {(sim.scenarioType || sim.vesselType || sim.persons) && (
        <div className="cone-section">
          <SectionLabel>Simulation</SectionLabel>
          {sim.scenarioType && <Row label="Emergency" value={sim.scenarioType.replace(/_/g, ' ')} />}
          {sim.vesselType  && <Row label="Vessel"    value={sim.vesselType.replace(/_/g, ' ')} />}
          {sim.persons     && <Row label="Persons"   value={sim.persons} />}
          {sim.lat && <Row label="Origin" value={`${Number(sim.lat).toFixed(4)}, ${Number(sim.lon).toFixed(4)}`} mono />}
        </div>
      )}

      {/* ── Survival estimate ── */}
      {law?.survival && (
        <div className="cone-section">
          <SectionLabel>Survival estimate (heuristic — not a validated model)</SectionLabel>
          <Row label="Est. survival window" value={`${law.survival.estimated_survival_hours} h`} />
          {survivalPct !== null && (
            <SurvivalBar pct={survivalPct} color={survivalColor} />
          )}
          <Row label="Sea state" value={`Bf ${law.survival.beaufort} — ${law.survival.sea_state}`} />
          <Row label="Wave height" value={`${law.survival.wave_height_m} m`} />
          <Row label="Capsizing likelihood (heuristic)" value={`${law.survival.capsizing_risk_pct}%`} />
          {law.warnings?.length > 0 && (
            <div style={{ marginTop: 4 }}>
              {law.warnings.map((w, i) => (
                <div key={i} style={{ fontSize: 9, color: '#ff3b3b', fontWeight: 700, marginBottom: 2 }}>
                  ⚠ {w}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* ── SAR zones ── */}
      {law?.origin_zones?.length > 0 && (
        <div className="cone-section">
          <SectionLabel>SAR zones (origin)</SectionLabel>
          {law.origin_zones.map((z) => <ZoneBadge key={z.id} zone={z} />)}
        </div>
      )}

      {/* ── Trajectory zones (if different from origin) ── */}
      {law?.trajectory_zones?.length > 0 && (() => {
        const originIds = new Set((law.origin_zones || []).map(z => z.id));
        const extra = law.trajectory_zones.filter(z => !originIds.has(z.id));
        return extra.length > 0 ? (
          <div className="cone-section">
            <SectionLabel>Zones entered during drift</SectionLabel>
            {extra.map((z) => <ZoneBadge key={z.id} zone={z} />)}
          </div>
        ) : null;
      })()}

      {/* ── MRCC contacts ── */}
      {law?.all_contacts?.length > 0 && (
        <div className="cone-section">
          <SectionLabel>MRCC contacts</SectionLabel>
          {law.all_contacts.map((c, i) => (
            <div key={i} className="cone-row" style={{ alignItems: 'flex-start' }}>
              <span style={{ flex: 1 }}>{c.name}</span>
              <strong style={{ fontFamily: 'monospace', fontSize: 10, color: '#8bf0c5' }}>{c.tel}</strong>
            </div>
          ))}
        </div>
      )}

      {/* ── Legal framework ── */}
      {law?.legal_notes?.length > 0 && (
        <div className="cone-section" style={{ borderBottom: 'none' }}>
          <SectionLabel>Legal framework</SectionLabel>
          {law.legal_notes.map((note, i) => <LegalNote key={i} note={note} idx={i} />)}
          <div style={{ fontSize: 9, color: '#4a6a64', marginTop: 6 }}>
            Sources: SOLAS V/33, UNCLOS Art. 98, IAMSAR Manual, IMO SAR Convention 1979
          </div>
        </div>
      )}
    </>
  );
}

const AIS_NAV_STATUS = {
  0: 'under way using engine',
  1: 'at anchor',
  2: 'not under command',
  3: 'restricted manoeuvrability',
  5: 'moored',
  6: 'aground',
  8: 'under way sailing',
};

function normalizeEventId(value) {
  return String(value || '').replace(/^intel:/, '');
}

function ageLabel(value) {
  const timestamp = Date.parse(value || '');
  if (!Number.isFinite(timestamp)) return null;
  const minutes = Math.max(0, Math.round((Date.now() - timestamp) / 60_000));
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours} h ago`;
  return `${Math.round(hours / 24)} d ago`;
}

function featurePosition(feature) {
  if (feature?.geometry?.type === 'Point') return feature.geometry.coordinates;
  const ring = feature?.geometry?.type === 'Polygon' ? feature.geometry.coordinates?.[0] : null;
  if (!Array.isArray(ring) || ring.length === 0) return null;
  const sum = ring.reduce((acc, point) => [acc[0] + Number(point[0]), acc[1] + Number(point[1])], [0, 0]);
  return [sum[0] / ring.length, sum[1] / ring.length];
}

function TrackGraphic({ feature, driftFeature, dossier }) {
  const props = feature?.properties || {};
  const publicTrack = Array.isArray(props.observed_track) ? props.observed_track : [];
  const dossierTrack = Array.isArray(dossier?.track_points) ? dossier.track_points : [];
  const observed = (publicTrack.length >= 2 ? publicTrack : dossierTrack)
    .map((point) => ({ lat: Number(point?.lat), lon: Number(point?.lon) }))
    .filter((point) => Number.isFinite(point.lat) && Number.isFinite(point.lon));
  const forecast = driftFeature?.geometry?.type === 'LineString'
    ? driftFeature.geometry.coordinates
      .map((point) => ({ lon: Number(point?.[0]), lat: Number(point?.[1]) }))
      .filter((point) => Number.isFinite(point.lat) && Number.isFinite(point.lon))
    : [];
  const position = featurePosition(feature);
  const fallback = position ? [{ lon: Number(position[0]), lat: Number(position[1]) }] : [];
  const all = [...observed, ...forecast, ...fallback];
  if (all.length === 0) return null;

  const width = 320;
  const height = 170;
  const pad = 18;
  const lats = all.map((point) => point.lat);
  const lons = all.map((point) => point.lon);
  const minLat = Math.min(...lats);
  const maxLat = Math.max(...lats);
  const minLon = Math.min(...lons);
  const maxLon = Math.max(...lons);
  const spanLat = maxLat - minLat || 0.01;
  const spanLon = maxLon - minLon || 0.01;
  const xy = (point) => [
    pad + ((point.lon - minLon) / spanLon) * (width - pad * 2),
    height - pad - ((point.lat - minLat) / spanLat) * (height - pad * 2),
  ];
  const path = (points) => points.map((point, index) => {
    const [x, y] = xy(point);
    return `${index === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  const current = xy(observed.at(-1) || forecast.at(-1) || fallback[0]);

  return (
    <div className="intel-report-graphic">
      <div className="intel-report-graphic__head">
        <strong>AIS movement reconstruction</strong>
        <span>observed track and model products</span>
      </div>
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="AIS track reconstruction">
        <defs>
          <pattern id="intel-report-grid" width="32" height="32" patternUnits="userSpaceOnUse">
            <path d="M 32 0 L 0 0 0 32" fill="none" stroke="rgba(120,190,205,.12)" strokeWidth="1" />
          </pattern>
        </defs>
        <rect width={width} height={height} fill="url(#intel-report-grid)" />
        {forecast.length >= 2 && <path className="intel-report-track intel-report-track--forecast" d={path(forecast)} />}
        {observed.length >= 2 && <path className="intel-report-track intel-report-track--observed" d={path(observed)} />}
        <circle cx={current[0]} cy={current[1]} r="11" className="intel-report-vessel-pulse" />
        <polygon
          points="0,-9 6,7 0,4 -6,7"
          transform={`translate(${current[0]} ${current[1]})`}
          className="intel-report-vessel"
        />
      </svg>
      <div className="intel-report-legend">
        {observed.length >= 2 && <span><i className="is-observed" /> AIS observed · {observed.length} fixes</span>}
        {forecast.length >= 2 && <span><i className="is-forecast" /> simulated drift · {forecast.length} steps</span>}
        {observed.length < 2 && forecast.length < 2 && <span>Latest reported position</span>}
      </div>
    </div>
  );
}

function marineTrafficMapUrl(props, feature) {
  const mmsi = props.linked_mmsi || props.mmsi;
  const coordinates = featurePosition(feature);
  if (!mmsi || !coordinates) return null;
  return `https://www.marinetraffic.com/en/ais/embed/zoom:10/centery:${Number(coordinates[1]).toFixed(5)}/centerx:${Number(coordinates[0]).toFixed(5)}/maptype:4/shownames:true/mmsi:${mmsi}/shipid:0`;
}

function EvidenceSources({ props, feature }) {
  const records = Array.isArray(props.source_records) && props.source_records.length
    ? props.source_records
    : [{
      source: props.source,
      title: props.title,
      url: props.url,
      timestamp_utc: props.timestamp_utc,
      verification_status: props.verification_status,
    }];
  const sources = records.filter((record) => record?.source || record?.url);
  if (!sources.length) return null;
  return (
    <details className="intel-evidence-sources" open={sources.length === 1}>
      <summary>Evidence sources ({sources.length})</summary>
      <div className="intel-evidence-sources__list">
        {sources.map((record, index) => {
          const isMarineTraffic = /marinetraffic/i.test(`${record.source || ''} ${record.url || ''}`);
          const mmsi = props.linked_mmsi || props.mmsi;
          const resolvedUrl = isMarineTraffic ? marineTrafficMapUrl(props, feature) : record.url;
          return (
          <article key={`${record.source}-${record.url}-${index}`}>
            <div>
              <strong>{isMarineTraffic ? 'MarineTraffic AIS' : record.source || 'Source'}</strong>
              <span>{String(record.verification_status || 'provenance recorded').replace(/_/g, ' ')}</span>
            </div>
            <p>{record.title || 'Maritime observation'}</p>
            {record.timestamp_utc && <time>{new Date(record.timestamp_utc).toLocaleString('it-IT')}</time>}
            {resolvedUrl && (
              <a href={resolvedUrl} target="_blank" rel="noopener noreferrer">
                {isMarineTraffic ? `Open MarineTraffic map · MMSI ${mmsi}` : 'Open original'} ↗
              </a>
            )}
          </article>
          );
        })}
      </div>
    </details>
  );
}

function IntelView({ panel, apiBase, publicMode, intelDrifts, loadNearestVessels, onTriggerIntelDrift }) {
  const props = panel.feature?.properties || {};
  const mmsi = props.linked_mmsi || props.mmsi;
  const isVesselReport = props.entity_kind === 'vessel' || props.report_type === 'vessel';
  const visual = classifyEventVisual(props);
  const isAlarmPhone = /alarm[ _-]?phone/i.test(`${props.source || ''} ${props.verification_status || ''}`)
    || visual.key === 'humanitarian_alarm_phone';
  const isHumanitarian = Boolean(
    props.humanitarian_case_id
    || isAlarmPhone
    || (props.maritime_domain === 'sar' && (props.is_distress || props.kind === 'distress'))
  );
  const [dossier, setDossier] = useState(null);
  const [dossierLoading, setDossierLoading] = useState(false);
  const [nearbyVessels, setNearbyVessels] = useState([]);
  const [nearbyStatus, setNearbyStatus] = useState('idle');
  const [forensic, setForensic] = useState(null);
  const [sarAnalysis, setSarAnalysis] = useState(null);
  const [sarStatus, setSarStatus] = useState('idle');
  const driftFeature = useMemo(() => {
    const features = Array.isArray(intelDrifts?.features) ? intelDrifts.features : [];
    const eventId = normalizeEventId(props.drift_event_id || props.id);
    return features.find((feature) => (
      feature.geometry?.type === 'LineString'
      && normalizeEventId(feature.properties?.intel_event_id) === eventId
    ));
  }, [intelDrifts, props.drift_event_id, props.id]);

  useEffect(() => {
    let alive = true;
    if (!mmsi) {
      setDossier(null);
      return undefined;
    }
    setDossier(null);
    setDossierLoading(true);
    const path = publicMode
      ? `/api/v1/live/vessels/${mmsi}/context?hours=168`
      : `/api/v1/mda/vessel/${mmsi}?hours=168`;
    fetch(`${apiBase}${path}`)
      .then((response) => response.ok ? response.json() : null)
      .then((data) => { if (alive) setDossier(data); })
      .catch(() => { if (alive) setDossier(null); })
      .finally(() => { if (alive) setDossierLoading(false); });
    return () => { alive = false; };
  }, [apiBase, mmsi, publicMode]);

  const coords = featurePosition(panel.feature);
  useEffect(() => {
    let alive = true;
    if (!isHumanitarian || !coords) {
      setSarAnalysis(null);
      setSarStatus('idle');
      return undefined;
    }
    const query = new URLSearchParams({
      lat: String(coords[1]),
      lon: String(coords[0]),
      persons: String(Math.max(1, Number(props.people_reported) || 1)),
      vessel_type: String(props.drift_vessel_type || 'rubber_boat'),
    });
    if (driftFeature?.geometry?.type === 'LineString') {
      query.set('traj', JSON.stringify(driftFeature.geometry.coordinates));
    }
    setSarStatus('loading');
    fetch(`${apiBase}/api/v1/zones/classify?${query.toString()}`)
      .then((response) => response.ok ? response.json() : null)
      .then((data) => {
        if (!alive) return;
        setSarAnalysis(data);
        setSarStatus(data ? 'ready' : 'unavailable');
      })
      .catch(() => {
        if (!alive) return;
        setSarAnalysis(null);
        setSarStatus('unavailable');
      });
    return () => { alive = false; };
  // Coordinates and trajectory belong to the canonical incident id.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiBase, props.id, driftFeature]);

  useEffect(() => {
    let alive = true;
    if (!loadNearestVessels || !coords) {
      setNearbyVessels([]);
      setNearbyStatus('idle');
      return undefined;
    }
    setNearbyStatus('loading');
    loadNearestVessels(coords[1], coords[0])
      .then((vessels) => {
        if (!alive) return;
        const others = (vessels || []).filter((vessel) => !mmsi || String(vessel.mmsi) !== String(mmsi));
        setNearbyVessels(others);
        setNearbyStatus(others.length ? 'ready' : 'empty');
      })
      .catch(() => {
        if (!alive) return;
        setNearbyVessels([]);
        setNearbyStatus('error');
      });
    return () => { alive = false; };
  // A new canonical event id is the only reason to repeat this point lookup.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.id]);

  useEffect(() => {
    let alive = true;
    if (publicMode || !props.id) {
      setForensic(null);
      return undefined;
    }
    const rawId = String(props.id).replace(/^intel:/, '');
    Promise.all([
      fetch(`${apiBase}/api/v1/forensic/${rawId}`),
      fetch(`${apiBase}/api/v1/forensic/${rawId}/verify`),
    ]).then(async ([recordResponse, verifyResponse]) => {
      const record = recordResponse.ok ? await recordResponse.json() : null;
      const verify = verifyResponse.ok ? await verifyResponse.json() : null;
      if (alive) setForensic(record ? { ...record, verify } : null);
    }).catch(() => { if (alive) setForensic(null); });
    return () => { alive = false; };
  }, [apiBase, props.id, publicMode]);

  const lifecycle = isVesselReport
    ? (props.motion_state || 'observed')
    : props.incident_lifecycle || (['resolved', 'needs_review', 'archived'].includes(props.kind) ? props.kind : 'active');
  const color = visual.color;
  const when = props.timestamp_utc || props.source_timestamp_utc;
  const eventType = isVesselReport ? 'Live AIS position' : eventAnomalyLabel(props);
  const sanctions = dossier?.identity?.sanctions || [];
  // docs/fixes.md M0.2: a case-specific EventAssessment (when the backend
  // has one for this event kind) takes priority over the older flat
  // confidence/evidence_level fields.
  const confidence = assessmentConfidence(props);
  const evidenceLevel = evidenceLevelText(
    props,
    isHumanitarian ? props.verification_level || 'public source report' : 'derived from maritime data',
  );
  const speedSamples = (dossier?.track_points || [])
    .map((point) => Number(point.sog))
    .filter((speed) => Number.isFinite(speed) && speed >= 0);
  const explicitSpeed = props.latest_sog == null || props.latest_sog === '' ? null : Number(props.latest_sog);
  const latestSpeed = Number.isFinite(explicitSpeed) ? explicitSpeed : speedSamples.at(-1);
  const averageSpeed = speedSamples.length
    ? speedSamples.reduce((sum, speed) => sum + speed, 0) / speedSamples.length
    : null;
  const maximumSpeed = speedSamples.length ? Math.max(...speedSamples) : null;

  return (
    <>
      <div className="cone-section">
        <div className="intel-report-title">
          <span style={{ background: color }} />
          <strong>{props.display_name || props.vessel_name || props.ship_name || dossier?.static?.name || props.title || 'Maritime signal'}</strong>
        </div>
        {props.text && <p className="intel-report-summary">{props.text}</p>}
        <Row label="Category" value={isVesselReport ? 'Maritime · AIS vessel' : visual.label} color={isVesselReport ? '#38bdf8' : color} />
        <Row label="Event" value={eventType} />
        <Row label="Status" value={lifecycle} color={color} />
        {props.verification_status && <Row label="Verification" value={String(props.verification_status).replace(/_/g, ' ')} />}
        <Row label="Reported" value={when ? new Date(when).toLocaleString('en-GB', { dateStyle: 'medium', timeStyle: 'short' }) : '—'} />
        {coords && <Row label="Coordinates" value={`${Number(coords[1]).toFixed(5)}, ${Number(coords[0]).toFixed(5)}`} mono />}
      </div>

      {isHumanitarian && Array.isArray(props.media_evidence)
        && props.media_evidence.some((m) => m && m.stored_media_url) && (
        <div className="cone-section intel-report-media">
          <SectionLabel>Source screenshot</SectionLabel>
          {props.media_evidence.filter((m) => m && m.stored_media_url).map((m, i) => (
            <a
              key={m.stored_media_url || i}
              href={props.url || m.source_url || '#'}
              target="_blank"
              rel="noreferrer"
              className="intel-report-screenshot-link"
            >
              <img
                src={m.stored_media_url}
                alt="Alarm Phone source screenshot"
                loading="lazy"
                className="intel-report-screenshot"
              />
            </a>
          ))}
          <p className="intel-report-provenance">
            IMAGE OCR · MACHINE EXTRACTED — UNVERIFIED
            {props.radius_m
              ? ` · ±${props.radius_m >= 1000
                  ? `${(props.radius_m / 1000).toFixed(1)} km`
                  : `${Math.round(props.radius_m)} m`}`
              : ''}
          </p>
        </div>
      )}

      <div className="cone-section intel-report-visual">
        <TrackGraphic feature={panel.feature} driftFeature={driftFeature} dossier={dossier} />
      </div>

      {!isAlarmPhone && (!isHumanitarian || mmsi) && <div className="cone-section">
        <SectionLabel>Professional vessel identity</SectionLabel>
        <Row label="Name" value={dossier?.static?.name || props.vessel_name || props.ship_name || '—'} />
        <Row label="MMSI" value={mmsi || '—'} mono />
        <Row label="IMO" value={dossier?.static?.imo || props.imo || '—'} mono />
        <Row label="Flag" value={dossier?.static?.flag || dossier?.identity?.mid_flag || props.flag || '—'} />
        <Row label="Vessel type" value={shipTypeLabel(dossier?.static?.ship_type ?? props.ship_type)} />
        {dossier?.static?.destination && <Row label="AIS destination" value={dossier.static.destination} />}
        {props.latest_nav_status != null && <Row label="Navigation" value={AIS_NAV_STATUS[props.latest_nav_status] || `status ${props.latest_nav_status}`} />}
        <Row
          label="Latest AIS speed"
          value={Number.isFinite(latestSpeed)
            ? `${latestSpeed.toFixed(1)} kn${latestSpeed <= 0.1 ? ' · stationary fix' : ''}`
            : 'Not available'}
        />
        {Number.isFinite(averageSpeed) && <Row label="Track average" value={`${averageSpeed.toFixed(1)} kn`} />}
        {Number.isFinite(maximumSpeed) && <Row label="Track maximum" value={`${maximumSpeed.toFixed(1)} kn`} />}
        {dossierLoading && <div className="intel-report-loading">Loading vessel registry and sanctions…</div>}
      </div>}

      <div className="cone-section">
        <SectionLabel>{isHumanitarian ? 'Humanitarian case' : isVesselReport ? 'Current navigation' : 'Why this was flagged'}</SectionLabel>
        {isHumanitarian ? (
          <>
            <Row label="Case" value={props.humanitarian_case_id || props.incident_id || props.id} mono />
            <Row label="Situation" value={String(props.humanitarian_case_type || eventType).replace(/_/g, ' ')} />
            <Row
              label="People reported"
              value={props.people_reported != null
                ? `${props.people_precision === 'approximate' ? '~' : ''}${props.people_reported}`
                : 'Not stated'}
            />
            <Row label="Case status" value={props.humanitarian_status || lifecycle} color={color} />
            <Row label="Position precision" value={String(props.location_precision || props.coordinate_review_status || 'unknown').replace(/_/g, ' ')} />
            <Row
              label="Source assessment"
              value={isAlarmPhone ? 'Direct humanitarian source · operator curated' : String(props.verification_level || props.verification_status || 'single public source').replace(/_/g, ' ')}
            />
            <p className="intel-report-note">
              {isAlarmPhone
                ? 'Alarm Phone is treated as a direct, specialised humanitarian source. The report remains distinct from official authority confirmation.'
                : 'This records what the cited humanitarian source reported; conflicting or later updates remain visible in the timeline.'}
            </p>
          </>
        ) : (
          <p className="intel-report-note">{props.detection_reason || props.detail || descriptionOf(props.type)}</p>
        )}
        {props.infrastructure && (
          <div className="intel-report-warning">
            <strong>Infrastructure proximity context</strong>
            <span>
              {Number.isFinite(Number(props.infrastructure.distance_km)) ? `${Number(props.infrastructure.distance_km).toFixed(1)} km from ` : ''}
              {props.infrastructure.name || props.infrastructure.kind}.
            </span>
            <small>Proximity and loitering are anomaly context, not proof of interference or intent.</small>
          </div>
        )}
        {props.status_note && <p className="intel-report-note">{props.status_note}</p>}
        {!driftFeature && coords && props.drift_eligible && onTriggerIntelDrift && (
          <button
            type="button"
            className="intel-report-action"
            disabled={props.drift_status === 'computing'}
            onClick={() => onTriggerIntelDrift(
              props.drift_event_id || props.id,
              coords[1],
              coords[0],
              props.drift_vessel_type,
            )}
          >
            {props.drift_status === 'computing' ? 'Computing drift…' : 'Generate drift forecast'}
          </button>
        )}
      </div>

      {coords && loadNearestVessels && (
        <div className="cone-section">
          <SectionLabel>Nearest AIS vessels</SectionLabel>
          {nearbyStatus === 'loading' && <div className="intel-report-loading">Searching recent AIS positions…</div>}
          {nearbyStatus === 'empty' && <p className="intel-report-note">No other recent AIS vessel found near this position.</p>}
          {nearbyStatus === 'error' && <p className="intel-report-warning">AIS proximity service is currently unavailable.</p>}
          {nearbyStatus === 'ready' && nearbyVessels.slice(0, 5).map((vessel) => (
            <Row
              key={vessel.mmsi || `${vessel.lat}-${vessel.lon}`}
              label={`${vessel.ship_name || vessel.mmsi || 'Vessel'} · ${shipTypeLabel(vessel.type)}`}
              value={[
                Number.isFinite(Number(vessel.distance_nm)) ? `${Number(vessel.distance_nm).toFixed(1)} nm` : null,
                ageLabel(vessel.last_seen),
              ].filter(Boolean).join(' · ') || '—'}
            />
          ))}
          {isHumanitarian && nearbyStatus === 'ready' && (
            <p className="intel-report-note">Proximity only: no nearby vessel is labelled as responder, involved party, or responsible without explicit corroborating evidence.</p>
          )}
        </div>
      )}

      {mmsi && <div className="cone-section">
        <SectionLabel>Recent port calls</SectionLabel>
        {dossierLoading && <div className="intel-report-loading">Reconstructing calls from AIS history…</div>}
        {!dossierLoading && (dossier?.recent_port_calls || []).length === 0 && (
          <p className="intel-report-note">No AIS-derived port call in the selected history window.</p>
        )}
        {(dossier?.recent_port_calls || []).map((call, index) => (
          <Row
            key={`${call.port}-${call.arrived_at || index}`}
            label={call.port}
            value={`${call.arrived_at ? new Date(call.arrived_at).toLocaleString('en-GB', { dateStyle: 'medium', timeStyle: 'short' }) : 'time unavailable'} · AIS-derived`}
          />
        ))}
        {(dossier?.recent_port_calls || []).length > 0 && (
          <p className="intel-report-note">Derived from slow or moored AIS fixes inside a port approach area; not an official port authority record.</p>
        )}
      </div>}

      {mmsi && (dossier?.radio_associations || []).length > 0 && (
        <div className="cone-section">
          <SectionLabel>Radio evidence</SectionLabel>
          {(dossier.radio_associations || []).slice(0, 6).map((association, index) => (
            <div className="mda-sanctions" key={`${association.created_at || association.ais_observed_at || index}`}>
              <strong>DSC ↔ AIS · {String(association.match_status || 'observed').replace(/_/g, ' ')}</strong>
              <span>{Math.round(Number(association.confidence || 0) * 100)}% association confidence</span>
              {Number.isFinite(Number(association.distance_km)) && <span>{Number(association.distance_km).toFixed(1)} km from AIS fix</span>}
              {association.ais_observed_at && <span>AIS {ageLabel(association.ais_observed_at)}</span>}
              {association.episode_eligible && <span>Cross-modal correlation eligible</span>}
            </div>
          ))}
          <p className="intel-report-note">Radio and AIS remain independent evidence lineages; only strong temporally and geographically coherent matches are promoted to a cross-modal episode.</p>
        </div>
      )}

      {(props.sanctions_matched || sanctions.length > 0) && (
        <div className="cone-section">
          <SectionLabel>Sanctions screening</SectionLabel>
          {sanctions.length === 0 ? <p className="intel-report-warning">Sanctions match recorded; detailed list record unavailable.</p> : sanctions.map((sanction, index) => (
            <article className="mda-sanctions" key={`${sanction.list}-${index}`}>
              <strong>{sanction.list}{sanction.program ? ` · ${sanction.program}` : ''}</strong>
              {sanction.reason && <span>{sanction.reason}</span>}
              {sanction.description && <span>{sanction.description}</span>}
              {sanction.listed_on && <span>listed {sanction.listed_on}</span>}
              {sanction.source_url && <a href={sanction.source_url} target="_blank" rel="noopener noreferrer">Official/list source ↗</a>}
            </article>
          ))}
        </div>
      )}

      {Array.isArray(props.updates) && props.updates.length > 0 && (
        <div className="cone-section">
          <SectionLabel>Episode updates</SectionLabel>
          <ol className="intel-report-updates">
            {props.updates.map((update) => (
              <li key={`${update.id}-${update.timestamp_utc}`}>
                <time>{update.timestamp_utc ? new Date(update.timestamp_utc).toLocaleString('it-IT') : '—'}</time>
                <span>{String(update.anomaly_type || update.type || 'signal').replace(/_/g, ' ')}</span>
              </li>
            ))}
          </ol>
        </div>
      )}

      {Array.isArray(props.thread_reposts) && props.thread_reposts.length > 0 && (
        <div className="cone-section">
          <SectionLabel>Case timeline</SectionLabel>
          <ol className="intel-report-updates">
            {props.thread_reposts.map((update) => (
              <li key={`${update.tweet_id}-${update.posted_at}`}>
                <time>{update.posted_at ? new Date(update.posted_at).toLocaleString('it-IT') : '—'}</time>
                <span>{update.note || String(update.kind || 'source update').replace(/_/g, ' ')}</span>
                {update.url && <a href={update.url} target="_blank" rel="noopener noreferrer">Open update ↗</a>}
              </li>
            ))}
          </ol>
        </div>
      )}

      {isHumanitarian ? (
        <div className="cone-section">
          <SectionLabel>Humanitarian forensic report</SectionLabel>
          <Row
            label="Source record"
            value={isAlarmPhone
              ? 'Alarm Phone · direct specialised humanitarian report'
              : `${props.source || 'Humanitarian source'} · provenance recorded`}
          />
          <Row label="Reported event" value={String(props.humanitarian_case_type || eventType).replace(/_/g, ' ')} />
          <Row label="Operational status" value={String(props.humanitarian_status || lifecycle).replace(/_/g, ' ')} color={color} />
          <Row
            label="Position evidence"
            value={String(props.ocr_engine || props.coordinate_source || 'source-stated position').replace(/_/g, ' ')}
          />
          <Row label="Position precision" value={String(props.location_precision || props.coordinate_review_status || 'unknown').replace(/_/g, ' ')} />
          {sarStatus === 'loading' && <div className="intel-report-loading">Classifying SAR responsibility…</div>}
          {sarStatus === 'unavailable' && <p className="intel-report-warning">SAR-zone classification is temporarily unavailable.</p>}
          {sarAnalysis?.primary_mrcc && (
            <>
              <Row
                label="Primary SAR coordination"
                value={`${sarAnalysis.primary_mrcc.name || 'Unknown'}${sarAnalysis.primary_mrcc.zone ? ` · ${sarAnalysis.primary_mrcc.zone}` : ''}`}
              />
              {sarAnalysis.primary_mrcc.tel && <Row label="MRCC contact" value={sarAnalysis.primary_mrcc.tel} mono />}
            </>
          )}
          {sarAnalysis?.origin_zones?.length > 0 && (
            <div className="intel-forensic-zones">
              <SectionLabel>SAR zones at reported position</SectionLabel>
              {sarAnalysis.origin_zones.map((zone) => <ZoneBadge key={zone.id} zone={zone} />)}
            </div>
          )}
          {sarAnalysis?.trajectory_zones?.some((zone) => !sarAnalysis.origin_zones?.some((origin) => origin.id === zone.id)) && (
            <div className="intel-forensic-zones">
              <SectionLabel>SAR zones intersected by modelled drift</SectionLabel>
              {sarAnalysis.trajectory_zones
                .filter((zone) => !sarAnalysis.origin_zones?.some((origin) => origin.id === zone.id))
                .map((zone) => <ZoneBadge key={zone.id} zone={zone} />)}
            </div>
          )}
          <Row
            label="AIS proximity check"
            value={nearbyStatus === 'ready'
              ? `${nearbyVessels.length} recent nearby vessel${nearbyVessels.length === 1 ? '' : 's'} examined`
              : nearbyStatus === 'empty' ? 'No recent nearby AIS vessel' : 'Pending or unavailable'}
          />
          <Row label="Vessel attribution" value="None assigned from proximity alone" />
          <p className="intel-report-note">
            SAR-zone mapping identifies the authority expected to coordinate a response at the reported position. It does not by itself prove that an authority or nearby vessel received the alert, failed to act, or bears legal responsibility.
          </p>
          {sarAnalysis?.legal_notes?.length > 0 && (
            <details className="intel-evidence-sources">
              <summary>Applicable SAR framework ({sarAnalysis.legal_notes.length})</summary>
              <div className="intel-evidence-sources__list">
                {sarAnalysis.legal_notes.map((note) => <LegalNote key={note} note={note} />)}
              </div>
            </details>
          )}
          {forensic && (
            <>
              <Row label="Evidence packet" value={forensic.verify?.valid ? 'signature valid' : 'signature invalid'} color={forensic.verify?.valid ? '#22c55e' : '#ef4444'} />
              <Row label="Classification" value={forensic.classification} />
              <Row label="Hash" value={forensic.hash_blake3 ? `${String(forensic.hash_blake3).slice(0, 18)}…` : '—'} mono />
            </>
          )}
        </div>
      ) : (
        <div className="cone-section">
          <SectionLabel>Evidence & forensic assessment</SectionLabel>
          <Row label="Observation" value={observationText(props, eventType)} />
          {/* docs/fixes.md M0.2: descriptionOf(type) is category help text
              only now -- the case-specific EventAssessment.interpretation
              (built from this event's own evidence, not a canned string
              per type) takes priority when the backend produced one. */}
          <Row label="Interpretation" value={interpretationText(props, descriptionOf(props.type))} />
          {caveatsText(props) && <Row label="Caveats" value={caveatsText(props)} />}
          <Row label="Evidence level" value={String(evidenceLevel).replace(/_/g, ' ')} />
          <Row
            label="Confidence"
            value={Number.isFinite(Number(confidence))
              ? `${Number(confidence) <= 1 ? (Number(confidence) * 100).toFixed(0) : Number(confidence).toFixed(0)}%`
              : 'Not quantified'}
          />
          <Row label="Collection" value={String(props.ocr_engine || props.coordinate_source || (mmsi ? 'AIS telemetry' : props.platform || 'public source')).replace(/_/g, ' ')} />
          <Row label="Validation" value={String(props.verification_status || 'provenance recorded').replace(/_/g, ' ')} />
          {forensic && (
            <>
              <Row label="Signature" value={forensic.verify?.valid ? 'valid' : 'invalid'} color={forensic.verify?.valid ? '#22c55e' : '#ef4444'} />
              <Row label="Classification" value={forensic.classification} />
              <Row label="Packet confidence" value={Number.isFinite(Number(forensic.confidence)) ? `${(Number(forensic.confidence) * 100).toFixed(0)}%` : '—'} />
              <Row label="Hash" value={forensic.hash_blake3 ? `${String(forensic.hash_blake3).slice(0, 18)}…` : '—'} mono />
            </>
          )}
        </div>
      )}

      <div className="cone-section" style={{ borderBottom: 'none' }}>
        <EvidenceSources props={props} feature={panel.feature} />
      </div>
    </>
  );
}

function TrajectoryView({ panel }) {
  const props = panel.feature?.properties || {};
  const meanSpeed = Number(props.mean_speed_ms);
  const maxSpeed = Number(props.max_speed_ms);
  const distance = Number(props.distance_m);
  return (
    <>
      <div className="cone-section">
        <SectionLabel>Live model product</SectionLabel>
        <Row label="Signal" value={props.intel_title || props.intel_event_id || 'Published signal'} />
        <Row label="Source" value={props.intel_source || 'published feed'} />
        <Row label="Status" value={props.verification_status || 'modelled'} color="#8bf0c5" />
      </div>
      <div className="cone-section">
        <SectionLabel>Trajectory dynamics</SectionLabel>
        <Row
          label="Mean drift speed"
          value={Number.isFinite(meanSpeed)
            ? `${meanSpeed.toFixed(2)} m/s · ${(meanSpeed * 1.943844).toFixed(2)} kn`
            : '—'}
        />
        <Row
          label="Peak drift speed"
          value={Number.isFinite(maxSpeed)
            ? `${maxSpeed.toFixed(2)} m/s · ${(maxSpeed * 1.943844).toFixed(2)} kn`
            : '—'}
        />
        <Row label="Path distance" value={Number.isFinite(distance) ? `${(distance / 1000).toFixed(1)} km` : '—'} />
        <Row label="Samples" value={props.sample_count || '—'} />
        <Row label="Interval" value={props.sample_interval_s ? `${Number(props.sample_interval_s) / 60} min` : '—'} />
      </div>
      <div className="cone-section" style={{ borderBottom: 'none' }}>
        <SectionLabel>Provenance</SectionLabel>
        <Row label="Model" value={props.model || 'OpenDrift Leeway'} />
        <Row label="Forcing" value={props.forcing_resolution || '—'} />
        <div style={{ fontSize: 9, color: '#87cabc', lineHeight: 1.45, marginTop: 6 }}>
          Forecast trajectory from time-varying environmental forcing. It is not an observed GPS track.
        </div>
      </div>
    </>
  );
}

function RadioLiveListen({ props, apiBase }) {
  const [status, setStatus] = useState('idle');
  const [format, setFormat] = useState(null);
  const abortRef = useRef(null);
  const contextRef = useRef(null);
  const nextStartRef = useRef(0);

  const closeContext = () => {
    const context = contextRef.current;
    contextRef.current = null;
    if (context && context.state !== 'closed') context.close().catch(() => {});
    nextStartRef.current = 0;
  };

  const stopListening = () => {
    const controller = abortRef.current;
    abortRef.current = null;
    if (controller) controller.abort();
    closeContext();
    setFormat(null);
    setStatus('idle');
  };

  useEffect(() => () => {
    abortRef.current?.abort();
    abortRef.current = null;
    closeContext();
  }, []);

  const schedulePcm = (context, payload, sampleRate) => {
    if (!payload.byteLength || context.state === 'closed') return;
    const samples = pcm16leToFloat32(payload);
    if (nextStartRef.current > context.currentTime + 0.75) return;
    const buffer = context.createBuffer(1, samples.length, sampleRate);
    buffer.copyToChannel(samples, 0);
    const source = context.createBufferSource();
    source.buffer = buffer;
    source.connect(context.destination);
    source.onended = () => source.disconnect();
    const startAt = Math.max(context.currentTime + 0.025, nextStartRef.current);
    source.start(startAt);
    nextStartRef.current = startAt + buffer.duration;
  };

  const startListening = async () => {
    if (status === 'connecting' || status === 'live') {
      stopListening();
      return;
    }
    const AudioContextCtor = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextCtor || !props.receiver_id) {
      setStatus('unavailable');
      return;
    }
    setStatus('connecting');
    const controller = new AbortController();
    abortRef.current = controller;
    try {
      const context = new AudioContextCtor();
      contextRef.current = context;
      nextStartRef.current = context.currentTime;
      await context.resume();
      const startedAt = Date.now();
      let maxSessionSeconds = 120;
      let carry = new Uint8Array(0);

      while (!controller.signal.aborted && Date.now() - startedAt < maxSessionSeconds * 1000) {
        const response = await fetch(liveListenHttpUrl(apiBase, props.receiver_id, window.location), {
          cache: 'no-store',
          signal: controller.signal,
        });
        if (!response.ok || !response.body) throw new Error(`listen stream ${response.status}`);
        const encoding = response.headers.get('x-seacommons-audio-encoding');
        const sampleRate = Number(response.headers.get('x-seacommons-sample-rate')) || 12_000;
        const channels = Number(response.headers.get('x-seacommons-channels')) || 1;
        const persistent = response.headers.get('x-seacommons-persistent');
        maxSessionSeconds = Number(response.headers.get('x-seacommons-listen-max-seconds')) || 120;
        if (encoding !== 'pcm_s16le' || channels !== 1 || persistent !== 'false') {
          throw new Error('unsupported live audio format');
        }
        const metadata = {
          type: 'audio_format', encoding, sample_rate_hz: sampleRate, channels,
          persistent: false, max_seconds: maxSessionSeconds,
        };
        setFormat(metadata);
        setStatus('live');

        const reader = response.body.getReader();
        while (!controller.signal.aborted) {
          const { value, done } = await reader.read();
          if (done) break;
          if (!value?.byteLength) continue;
          const merged = new Uint8Array(carry.byteLength + value.byteLength);
          merged.set(carry, 0);
          merged.set(value, carry.byteLength);
          const evenLength = merged.byteLength - (merged.byteLength % 2);
          if (evenLength) schedulePcm(context, merged.slice(0, evenLength), sampleRate);
          carry = evenLength < merged.byteLength ? merged.slice(evenLength) : new Uint8Array(0);
        }
      }

      if (abortRef.current === controller) {
        abortRef.current = null;
        closeContext();
        setStatus('ended');
      }
    } catch (error) {
      if (abortRef.current !== controller) return;
      abortRef.current = null;
      closeContext();
      setFormat(null);
      setStatus(error?.name === 'AbortError' ? 'idle' : 'unavailable');
    }
  };

  if (!props.listen_available) return null;
  const listening = status === 'connecting' || status === 'live';
  return (
    <div className="cone-section">
      <SectionLabel>Listen live</SectionLabel>
      <button type="button" className="intel-report-action" onClick={startListening}>
        {listening ? 'Stop listening' : 'Listen live'}
      </button>
      <p className="intel-report-note">
        Ephemeral stream only; persistent audio is not stored. Audio is relayed from the receiver VM through short HTTPS streams and bounded in memory.
      </p>
      <Row label="Listen status" value={status} />
      {format && <Row label="Audio" value={`${Number(format.sample_rate_hz) / 1000} kHz PCM · max ${format.max_seconds}s`} />}
    </div>
  );
}

function RadioReceiverView({ panel, apiBase }) {
  const props = panel?.feature?.properties || {};
  let capabilities;
  try { capabilities = JSON.parse(props.capabilities_json || '[]'); } catch { capabilities = []; }
  const ranges = capabilities.slice(0, 4).map((cap) => {
    const lo = Number(cap.min_hz);
    const hi = Number(cap.max_hz);
    if (!Number.isFinite(lo) || !Number.isFinite(hi)) return null;
    const unit = hi >= 1_000_000 ? 'MHz' : 'kHz';
    const scale = hi >= 1_000_000 ? 1_000_000 : 1_000;
    return `${(lo / scale).toFixed(lo < scale ? 3 : 1)}–${(hi / scale).toFixed(1)} ${unit}`;
  }).filter(Boolean);
  const coords = panel?.feature?.geometry?.coordinates || [];
  return (
    <>
      <div className="cone-section">
        <Row label="Station" value={props.station_label || props.receiver_id || 'Radio receiver'} />
        <Row label="Network" value={props.network_family || '—'} />
        <Row label="Country" value={props.country || '—'} />
        <Row label="State" value={props.active ? 'active' : props.state || 'catalogued'} />
        {props.active && Number(props.frequency_hz) > 0 && <Row label="Live channel" value={`${(Number(props.frequency_hz) / 1000).toFixed(1)} kHz · ${String(props.mode || 'audio').toUpperCase()}`} />}
        <Row label="Relevance score" value={Number.isFinite(Number(props.score)) ? `${Number(props.score).toFixed(1)} / 100` : '—'} />
        {coords.length >= 2 && <Row label="Approx. position" value={`${Number(coords[1]).toFixed(3)}, ${Number(coords[0]).toFixed(3)}`} mono />}
      </div>
      <div className="cone-section">
        <SectionLabel>Receiver capability</SectionLabel>
        {ranges.length ? ranges.map((range) => <Row key={range} label="Band" value={range} />) : <p className="intel-report-note">Capability metadata unavailable.</p>}
      </div>
      <RadioLiveListen props={props} apiBase={apiBase} />
      <div className="cone-section">
        <SectionLabel>Provenance</SectionLabel>
        <p className="intel-report-note">Public receiver infrastructure only. Endpoint and physical lineage are intentionally withheld; receiver observations enter the same evidence pipeline as other acquisition sources.</p>
      </div>
    </>
  );
}

function RadioMessageView({ panel, apiBase }) {
  const props = panel?.feature?.properties || {};
  let association = props.ais_association || null;
  if (!association && props.ais_association_json) {
    try { association = JSON.parse(props.ais_association_json); } catch { association = null; }
  }
  const isDsc = props.kind === 'dsc';
  const mmsi = props.mmsi || association?.mmsi || '';
  const [dossier, setDossier] = useState(null);

  useEffect(() => {
    if (!mmsi) { setDossier(null); return undefined; }
    let alive = true;
    const url = `${apiBase || ''}/api/v1/live/vessels/${encodeURIComponent(mmsi)}/context?hours=168`;
    fetch(url).then((response) => response.ok ? response.json() : null)
      .then((data) => { if (alive) setDossier(data); })
      .catch(() => { if (alive) setDossier(null); });
    return () => { alive = false; };
  }, [apiBase, mmsi]);

  const ports = dossier?.recent_port_calls || [];
  const trackPoints = dossier?.track_points || [];
  return (
    <>
      <div className="cone-section">
        <Row label="Protocol" value={isDsc ? 'DSC' : 'NAVTEX'} />
        <Row label="Frequency" value={Number(props.frequency_hz) ? `${(Number(props.frequency_hz) / 1000).toFixed(1)} kHz` : '—'} />
        <Row label="Observed" value={props.observed_at || '—'} mono />
        {isDsc && <Row label="Category" value={props.category || '—'} />}
        {isDsc && <Row label="MMSI" value={mmsi || '—'} mono />}
        {isDsc && <Row label="Nature" value={props.nature_code || '—'} />}
        {!isDsc && <Row label="Station" value={props.station_id || '—'} />}
        {!isDsc && <Row label="Subject" value={props.subject_id || '—'} />}
        {!isDsc && <Row label="Area" value={props.area || '—'} />}
      </div>
      {association && (
        <div className="cone-section">
          <SectionLabel>AIS association</SectionLabel>
          <Row label="Match" value={String(association.match_status || 'observed').replace(/_/g, ' ')} />
          <Row label="Confidence" value={`${Math.round(Number(association.confidence || 0) * 100)}%`} />
          {Number.isFinite(Number(association.distance_km)) && <Row label="Distance" value={`${Number(association.distance_km).toFixed(1)} km`} />}
          <p className="intel-report-note">AIS and radio remain independent evidence lineages. Only strong temporal and geographic agreement is episode-eligible.</p>
        </div>
      )}
      {dossier && (
        <div className="cone-section">
          <SectionLabel>Associated vessel report</SectionLabel>
          <Row label="Name" value={dossier.static?.name || `MMSI ${mmsi}`} />
          <Row label="IMO" value={dossier.static?.imo || '—'} mono />
          <Row label="Type" value={shipTypeLabel(dossier.static?.ship_type)} />
          <Row label="Destination" value={dossier.static?.destination || '—'} />
          <Row label="Track" value={`${trackPoints.length} AIS fixes · 7 days`} />
          <Row label="Port calls" value={ports.length ? `${ports.length} derived` : 'none in window'} />
          {ports.slice(0, 4).map((port, index) => (
            <Row key={`${port.port}-${index}`} label={index === 0 ? 'Recent port' : 'Earlier'} value={port.port || '—'} />
          ))}
        </div>
      )}
      {!isDsc && props.text && (
        <div className="cone-section">
          <SectionLabel>Decoded message</SectionLabel>
          <p className="intel-report-note" style={{ whiteSpace: 'pre-wrap' }}>{String(props.text).slice(0, 1200)}</p>
        </div>
      )}
    </>
  );
}

export default function MapFloatingPanel({
  panel,
  onClose,
  onComputeDrift,
  apiBase,
  publicMode,
  intelDrifts,
  loadNearestVessels,
  onTriggerIntelDrift,
}) {
  if (!panel) return null;

  const title = panel.type === 'trajectory'
    ? 'Live drift trajectory'
    : panel.type === 'cone'
      ? 'Drift projection'
      : panel.type === 'intel'
        ? 'Signal'
        : panel.type === 'radio_receiver'
          ? (panel.feature?.properties?.station_label || 'Radio receiver')
          : panel.type === 'radio_message'
            ? (panel.feature?.properties?.kind === 'navtex' ? 'NAVTEX message' : 'DSC message')
            : 'Selected point';
  const kicker = panel.type === 'trajectory'
    ? 'OpenDrift forecast'
    : panel.type === 'cone'
      ? 'SAR drift cone'
      : panel.type === 'intel'
        ? 'Live report'
        : panel.type === 'radio_receiver'
          ? 'Receiver mesh'
          : panel.type === 'radio_message'
            ? 'Structured radio evidence'
            : 'Map click';

  return (
    <div className={`cone-panel${['intel', 'radio_receiver', 'radio_message'].includes(panel.type) ? ' cone-panel--intel' : ''}`}>
      <div className="cone-panel-header" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div>
          <span className="section-kicker">{kicker}</span>
          <strong style={{ fontSize: 13, display: 'block', marginTop: 2 }}>{title}</strong>
        </div>
        <button className="cone-close-btn" onClick={onClose}>×</button>
      </div>

      {panel.type === 'location' && (
        <LocationView panel={panel} onComputeDrift={onComputeDrift} />
      )}
      {panel.type === 'cone' && (
        <ConeView panel={panel} />
      )}
      {panel.type === 'trajectory' && (
        <TrajectoryView panel={panel} />
      )}
      {panel.type === 'radio_receiver' && (
        <RadioReceiverView panel={panel} apiBase={apiBase} />
      )}
      {panel.type === 'radio_message' && (
        <RadioMessageView panel={panel} apiBase={apiBase} />
      )}
      {panel.type === 'intel' && (
        <IntelView
          panel={panel}
          apiBase={apiBase}
          publicMode={publicMode}
          intelDrifts={intelDrifts}
          loadNearestVessels={loadNearestVessels}
          onTriggerIntelDrift={onTriggerIntelDrift}
        />
      )}
    </div>
  );
}
