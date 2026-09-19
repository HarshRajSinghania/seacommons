import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { classifyEventVisual, isAlarmPhoneSource, signalCategoryOf } from '../features/intel/categories.js';
import { locationLabel, relativeTime } from '../features/live/eventPresentation.js';

const ALARM_PHONE_SOURCE = 'Alarm Phone';
const SEV_LABELS = ['critical', 'high', 'medium', 'low'];
const TYPE_ICONS = {
  distress:        '🆘',
  correlated_alert:'⚡',
  twitter:         '𝕏',
  mastodon:        '🐘',
  bluesky:         '🦋',
  whatsapp:        'WA',
  telegram:        'TG',
  partner:         'P',
  news:            '📰',
  iom_incident:    '🔴',
  ais_spike:       '📡',
  ais_anomaly:     '⚠️',
  gdacs:           '🌍',
  vessel_incident: '⚓',
  ngo_activity:    '🚢',
  manual:          '✍️',
};

export const DOMAIN_COLORS = {
  sar:        '#ff3b3b',
  sanctions:  '#f472b6',
  grey_zone:  '#f59e0b',
  safety:     '#38bdf8',
  piracy:     '#a78bfa',
  smuggling:  '#fb923c',
  iuu_fishing:'#4ade80',
  environmental: '#34d399',
};

// Operational tiers (mirrors IntelEvent.tier() on the backend).
const TIERS = [
  { key: 'operational', label: 'Operational', sub: 'Distress & SAR calls' },
  { key: 'news',        label: 'News & reports', sub: 'Situational updates' },
  { key: 'signal',      label: 'Signals',     sub: 'AIS & movement telemetry' },
];
// Vessel-name/type split for card titles. Every backend-composed title that
// carries a resolved vessel name appends it last as " — NAME" (see
// core/intel/vessel_incident_monitor.py:160 and fusion.py's alert.summary
// convention) — split on the last occurrence so a name containing an en
// dash elsewhere in the string still parses correctly.
function parseTitleVessel(title) {
  const t = String(title || '');
  const idx = t.lastIndexOf(' — ');
  if (idx === -1) return { label: t, name: null };
  return { label: t.slice(0, idx), name: t.slice(idx + 3) };
}

const PUBLIC_TIERS = [
  { key: 'operational', label: 'Operational', sub: 'Active public-eligible signals' },
  { key: 'news', label: 'Context', sub: 'Published contextual observations' },
  { key: 'signal', label: 'Observations', sub: 'Published telemetry and evidence' },
];

function eventTier(p) {
  // Backend supplies `tier`; fall back to type-based inference for cached/legacy events.
  if (p.tier) return p.tier;
  if (p.type === 'distress') return 'operational';
  if (p.type === 'ais_spike' || p.type === 'ngo_activity') return 'signal';
  return 'news';
}

function eventMainCategory(p = {}) {
  if (p.main_category === 'humanitarian' || p.main_category === 'maritime') {
    return p.main_category;
  }
  return p.maritime_domain === 'sar' ? 'humanitarian' : 'maritime';
}

function eventIncidentType(p = {}) {
  return p.incident_type || signalCategoryOf(p) || 'maritime_context';
}

// Average of a Polygon's exterior-ring vertices -- good enough for "fly
// here" / display purposes; not a true area-weighted centroid.
function polygonCentroid(polygonCoords) {
  const ring = polygonCoords?.[0];
  if (!Array.isArray(ring) || !ring.length) return null;
  let sumLon = 0;
  let sumLat = 0;
  for (const [lon, lat] of ring) { sumLon += lon; sumLat += lat; }
  return [sumLon / ring.length, sumLat / ring.length];
}

// ── Manual Injection Form ─────────────────────────────────────────────────────
function ManualInjectForm({ apiBase, onClose, onSuccess }) {
  const [form, setForm] = useState({
    title: '',
    text: '',
    source: 'manual',
    severity: 'high',
    type: 'manual',
    lat: '',
    lon: '',
    url: '',
  });
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');

  function setField(k, v) {
    setForm((cur) => ({ ...cur, [k]: v }));
  }

  async function handleSubmit(e) {
    e.preventDefault();
    if (!form.title.trim()) { setError('Title is required'); return; }
    setSubmitting(true);
    setError('');
    try {
      const body = {
        title: form.title.trim(),
        text: form.text.trim(),
        source: form.source.trim() || 'manual',
        severity: form.severity,
        type: form.type,
        url: form.url.trim(),
        lat: form.lat !== '' ? parseFloat(form.lat) : null,
        lon: form.lon !== '' ? parseFloat(form.lon) : null,
      };
      const resp = await fetch(`${apiBase}/api/v1/intel/manual`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!resp.ok) {
        const txt = await resp.text().catch(() => '');
        const msg = txt && !txt.trimStart().startsWith('<')
          ? txt
          : `HTTP ${resp.status} — backend unavailable`;
        throw new Error(msg);
      }
      onSuccess?.();
      onClose?.();
    } catch (err) {
      setError(err.message || 'Submission failed');
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="intel-inject-overlay" onClick={(e) => { if (e.target === e.currentTarget) onClose?.(); }}>
      <div className="intel-inject-modal">
        <div className="intel-inject-header">
          <strong>Inject intel event</strong>
          <button className="intel-inject-close" onClick={onClose}>✕</button>
        </div>
        <form onSubmit={handleSubmit} className="intel-inject-form">
          <label>
            Title *
            <input value={form.title} onChange={(e) => setField('title', e.target.value)} placeholder="Brief description of the event" />
          </label>
          <label>
            Details
            <textarea value={form.text} onChange={(e) => setField('text', e.target.value)} rows={3} placeholder="Additional context, quotes, source details…" />
          </label>
          <div className="intel-inject-row">
            <label>
              Severity
              <select value={form.severity} onChange={(e) => setField('severity', e.target.value)}>
                {SEV_LABELS.map((s) => <option key={s} value={s}>{s}</option>)}
              </select>
            </label>
            <label>
              Type
              <select value={form.type} onChange={(e) => setField('type', e.target.value)}>
                {['manual', 'distress', 'news', 'iom_incident'].map((t) => <option key={t} value={t}>{t}</option>)}
              </select>
            </label>
          </div>
          <div className="intel-inject-row">
            <label>
              Lat
              <input type="number" step="any" value={form.lat} onChange={(e) => setField('lat', e.target.value)} placeholder="e.g. 35.8" />
            </label>
            <label>
              Lon
              <input type="number" step="any" value={form.lon} onChange={(e) => setField('lon', e.target.value)} placeholder="e.g. 13.4" />
            </label>
          </div>
          <label>
            Source
            <input value={form.source} onChange={(e) => setField('source', e.target.value)} placeholder="e.g. phone call, email, partner org" />
          </label>
          <label>
            URL
            <input value={form.url} onChange={(e) => setField('url', e.target.value)} placeholder="https://…" />
          </label>
          {error && <p className="intel-inject-error">{error}</p>}
          <div className="intel-inject-actions">
            <button type="submit" disabled={submitting}>{submitting ? 'Saving…' : 'Save event'}</button>
            <button type="button" onClick={onClose}>Cancel</button>
          </div>
        </form>
      </div>
    </div>
  );
}

// ── Timeline grouping ────────────────────────────────────────────────────────
function groupByHour(events) {
  const groups = [];
  let currentKey = null;
  let currentGroup = null;
  for (const ev of events) {
    const ts = ev.properties?.timestamp_utc;
    if (!ts) continue;
    const d = new Date(ts);
    const key = `${d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short' })} ${d.getHours().toString().padStart(2, '0')}:00`;
    if (key !== currentKey) {
      if (currentGroup) groups.push(currentGroup);
      currentKey = key;
      currentGroup = { key, events: [] };
    }
    currentGroup.events.push(ev);
  }
  if (currentGroup) groups.push(currentGroup);
  return groups;
}


// ── Main component ───────────────────────────────────────────────────────────
export default function IntelDashboard({
  apiBase,
  publicMode = false,
  intelEvents,
  intelStats,
  liveModeCounts = null,
  feedStatus = 'live',
  liveMode = 'humanitarian',
  activeSignalCategories,
  liveFacetFilters = null,
  alarmPhoneOn = true,
  showAisAlerts,
  setShowAisAlerts,
  mapRef,
  selectedEventId,
  onOpenReport,
}) {
  const [search, setSearch] = useState('');
  const [channelFilter, setChannelFilter] = useState('all');
  const [sourceFilter, setSourceFilter] = useState('all');
  const [tierFilter, setTierFilter] = useState('all');   // evidence/presentation tier
  const [mainCategoryFilter, setMainCategoryFilter] = useState('all');
  const [incidentTypeFilter, setIncidentTypeFilter] = useState('all');
  const [viewMode, setViewMode] = useState('list');   // 'list' | 'timeline'
  const [showInject, setShowInject] = useState(false);
  const [injectSuccess, setInjectSuccess] = useState(false);
  const selectedRowRef = useRef(null);

  useEffect(() => {
    selectedRowRef.current?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }, [selectedEventId]);

  useEffect(() => {
    if (!publicMode) return;
    setChannelFilter('all');
    setSourceFilter('all');
    setTierFilter('all');
    setMainCategoryFilter('all');
    setIncidentTypeFilter('all');
  }, [liveMode, publicMode]);

  // Filtered + searched events
  const filteredEvents = useMemo(() => {
    let evs = showAisAlerts ? intelEvents : intelEvents.filter((f) => f.properties?.type !== 'ais_spike');

    // Signals selector — per-category toggle (public Live panel). Mirrors
    // exactly what the map's per-category layers show, so the feed cards and
    // the map never disagree about what is currently visible.
    if (activeSignalCategories) {
      evs = evs.filter((f) => activeSignalCategories.has(signalCategoryOf(f.properties || {})));
    }
    if (!alarmPhoneOn) {
      evs = evs.filter((f) => !isAlarmPhoneSource(f.properties?.source));
    }
    if (liveFacetFilters?.satellite) evs = evs.filter((f) => Boolean(f.properties?.has_satellite));
    if (liveFacetFilters?.sanctions) evs = evs.filter((f) => Boolean(f.properties?.sanctions_matched));
    if (liveFacetFilters?.corroborated) evs = evs.filter((f) => Boolean(f.properties?.corroborated));

    if (tierFilter !== 'all') evs = evs.filter((f) => eventTier(f.properties || {}) === tierFilter);
    if (mainCategoryFilter !== 'all') {
      evs = evs.filter((f) => {
        const p = f.properties || {};
        return eventMainCategory(p) === mainCategoryFilter;
      });
    }
    if (incidentTypeFilter !== 'all') {
      evs = evs.filter((f) => eventIncidentType(f.properties || {}) === incidentTypeFilter);
    }
    if (channelFilter !== 'all') evs = evs.filter((f) => f.properties?.type === channelFilter);
    if (sourceFilter === ALARM_PHONE_SOURCE) {
      // The account's source string varies by ingester/tweet (display name
      // "Alarm Phone" vs handle "alarm_phone") -- match either, same as the
      // public Signals selector's Alarm Phone toggle.
      evs = evs.filter((f) => isAlarmPhoneSource(f.properties?.source));
    } else if (sourceFilter !== 'all') {
      evs = evs.filter((f) => f.properties?.source === sourceFilter);
    }
    if (search.trim()) {
      const q = search.trim().toLowerCase();
      evs = evs.filter((f) => {
        const p = f.properties || {};
        return (
          (p.title || '').toLowerCase().includes(q) ||
          (p.text  || '').toLowerCase().includes(q) ||
          (p.source || '').toLowerCase().includes(q)
        );
      });
    }
    const selected = intelEvents.find((feature) => (
      String(feature.properties?.id || '') === String(selectedEventId || '')
    ));
    if (selected && !evs.some((feature) => feature === selected)) evs.push(selected);
    return [...evs].sort((left, right) => (
      Date.parse(right.properties?.timestamp_utc || 0)
      - Date.parse(left.properties?.timestamp_utc || 0)
    ));
  }, [intelEvents, channelFilter, sourceFilter, tierFilter, mainCategoryFilter, incidentTypeFilter, showAisAlerts, activeSignalCategories, liveFacetFilters, alarmPhoneOn, search, selectedEventId]);

  const presentIncidentTypes = useMemo(() => {
    const seen = new Set();
    for (const feature of intelEvents) seen.add(eventIncidentType(feature.properties || {}));
    return [...seen].filter(Boolean).sort();
  }, [intelEvents]);

  const mainCategoryCounts = useMemo(() => {
    const counts = { humanitarian: 0, maritime: 0 };
    for (const feature of intelEvents) {
      const category = eventMainCategory(feature.properties || {});
      counts[category] = (counts[category] || 0) + 1;
    }
    return counts;
  }, [intelEvents]);

  // Group the visible events by operational tier (operational pinned on top).
  const tierGroups = useMemo(() => {
    const g = { operational: [], news: [], signal: [] };
    for (const f of filteredEvents) {
      const t = eventTier(f.properties || {});
      (g[t] || g.news).push(f);
    }
    return g;
  }, [filteredEvents]);

  const visibleTiers = publicMode ? PUBLIC_TIERS : TIERS;

  // Available channel types
  const channelTypes = useMemo(() => {
    const types = new Set(intelEvents.map((f) => f.properties?.type).filter(Boolean));
    return Array.from(types).sort();
  }, [intelEvents]);

  const timelineGroups = useMemo(
    () => viewMode === 'timeline' ? groupByHour(filteredEvents) : [],
    [filteredEvents, viewMode],
  );

  const handleInjectSuccess = useCallback(() => {
    setInjectSuccess(true);
    window.setTimeout(() => setInjectSuccess(false), 3000);
  }, []);

  function flyTo(coords) {
    if (coords && mapRef?.current) {
      mapRef.current.flyTo({ center: coords, zoom: 9, duration: 800 });
    }
  }

  function renderEvent(feat) {
    const p = feat.properties || {};
    const coords = feat.geometry?.type === 'Polygon'
      ? polygonCentroid(feat.geometry.coordinates)
      : feat.geometry?.coordinates;
    const eventId = String(p.id || p.title || '');
    const isSelected = eventId === String(selectedEventId || '');
    const visual = classifyEventVisual(p);
    const parsedTitle = parseTitleVessel(p.operational_label || p.title);
    // A humanitarian distress report is about people, not a vessel identity:
    // never headline it with a bare MMSI.
    const isHumanitarianRow = visual.key === 'humanitarian_alarm_phone'
      || visual.key === 'distress'
      || isAlarmPhoneSource(p.source)
      || Boolean(p.humanitarian_case_type);
    const vesselName = p.vessel_name
      || p.ship_name
      || parsedTitle.name
      || (!isHumanitarianRow && (p.linked_mmsi || p.mmsi)
        ? `MMSI ${p.linked_mmsi || p.mmsi}`
        : null)
      || p.operational_label
      || p.title
      || (isHumanitarianRow ? 'Distress report' : 'Unknown vessel');
    const incidentType = eventIncidentType(p).replace(/_/g, ' ');
    // F-12: report time visible in every row; a missing coordinate reads as a
    // reason (OCR PROCESSING / OCR DISPUTED / REGION ONLY / WITHHELD / NOT
    // EXTRACTED), never a bare "position unavailable".
    const reported = relativeTime(p.timestamp_utc);
    const location = locationLabel(p, coords);
    const hasPoint = location.tone === 'ok' || location.tone === 'review';

    return (
      <li
        key={eventId}
        ref={isSelected ? selectedRowRef : null}
        className={`intel-log-row${isSelected ? ' is-selected' : ''}`}
      >
        <button
          type="button"
          aria-current={isSelected ? 'true' : undefined}
          onClick={() => {
            if (hasPoint) flyTo(coords);
            onOpenReport?.(feat);
          }}
          title={p.timestamp_utc ? new Date(p.timestamp_utc).toLocaleString('it-IT') : p.title}
        >
          <i
            className="intel-log-dot"
            style={{ color: visual.color, background: visual.color }}
            aria-label={visual.label}
            title={visual.label}
          />
          <strong>{vesselName}</strong>
          <span>{incidentType}{p.input_modality ? ` · ${p.input_modality.replace('_', ' ')}` : ''}</span>
          {reported && <span className="intel-log-time">{reported}</span>}
          <code className={`intel-log-loc intel-log-loc--${location.tone}`}>{location.text}</code>
        </button>
      </li>
    );
  }
  return (
    <div className="panel-stack">

      {!publicMode && (
        <section className="panel-block intel-operator-actions">
          <button className="intel-inject-trigger" onClick={() => setShowInject(true)} title="Inject manual event">
            + Manual event
          </button>
          {injectSuccess && <p style={{ color: '#22c55e', fontSize: 11, margin: 0 }}>Event saved and broadcast.</p>}
        </section>
      )}

      {/* Stats row */}
      <section className="panel-block" style={{ paddingTop: 0, paddingBottom: 8 }}>
        <div className="osint-stats-row">
          {publicMode ? (<>
            <div className="osint-stat"><strong>{(liveModeCounts?.humanitarian || 0) + (liveModeCounts?.maritime || 0)}</strong><span>live total</span></div>
            <div className="osint-stat osint-stat--critical"><strong>{liveModeCounts?.humanitarian || 0}</strong><span>humanitarian</span></div>
            <div className="osint-stat"><strong>{liveModeCounts?.maritime || 0}</strong><span>maritime</span></div>
          </>) : (<>
            <div className="osint-stat"><strong>{intelStats.total}</strong><span>events</span></div>
            <div className="osint-stat osint-stat--critical"><strong>{mainCategoryCounts.humanitarian}</strong><span>humanitarian</span></div>
            <div className="osint-stat"><strong>{mainCategoryCounts.maritime}</strong><span>maritime</span></div>
            <div className="osint-stat"><strong>{filteredEvents.length}</strong><span>shown</span></div>
          </>)}
        </div>
      </section>

      {/* Controls */}
      <section className="panel-block" style={{ paddingTop: 0, paddingBottom: 0 }}>
        {/* Search */}
        <div className="intel-search-row">
          <input
            className="intel-search-input"
            type="search"
            placeholder="Search title, text, source…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>

        {/* Tier filter — primary operational control */}
        <div className="intel-tier-row">
          <button
            className={`intel-tier-btn ${tierFilter === 'all' ? 'is-active' : ''}`}
            onClick={() => setTierFilter('all')}
          >All</button>
          {visibleTiers.map((t) => (
            <button
              key={t.key}
              className={`intel-tier-btn intel-tier-btn--${t.key} ${tierFilter === t.key ? 'is-active' : ''}`}
              onClick={() => setTierFilter((cur) => cur === t.key ? 'all' : t.key)}
              title={t.sub}
            >
              {t.key === 'operational' && <span className="intel-tier-dot" />}
              {t.label}
              {!publicMode ? <span className="intel-tier-count">{tierGroups[t.key].length}</span> : null}
            </button>
          ))}
        </div>

        {/* Product taxonomy: exactly two main categories. */}
        <div className="intel-filter-row" style={{ marginTop: 6 }}>
          {[
            ['all', 'all'],
            ['humanitarian', 'humanitarian'],
            ['maritime', 'maritime'],
          ].map(([key, label]) => (
            <button
              key={key}
              className={`intel-filter-btn ${mainCategoryFilter === key ? 'is-active' : ''}`}
              onClick={() => setMainCategoryFilter(key)}
            >{label}</button>
          ))}
          {!publicMode ? (
            <button
              className={`intel-filter-btn ${showAisAlerts ? 'is-active' : ''}`}
              onClick={() => setShowAisAlerts((v) => !v)}
              title="Show raw AIS detector output in the operator view"
            >raw AIS</button>
          ) : null}
          {!publicMode && (
            <button
              className={`intel-filter-btn ${sourceFilter === ALARM_PHONE_SOURCE ? 'is-active' : ''}`}
              onClick={() => setSourceFilter((cur) => cur === ALARM_PHONE_SOURCE ? 'all' : ALARM_PHONE_SOURCE)}
              title="Source facet: Alarm Phone"
            >Alarm Phone</button>
          )}
        </div>

        <div className="intel-search-row" style={{ marginTop: 6 }}>
          <select
            className="intel-search-input"
            value={incidentTypeFilter}
            onChange={(event) => setIncidentTypeFilter(event.target.value)}
            aria-label="Incident type"
          >
            <option value="all">All incident types</option>
            {presentIncidentTypes.map((type) => (
              <option key={type} value={type}>{type.replace(/_/g, ' ')}</option>
            ))}
          </select>
        </div>

        {/* Channel filter */}
        {channelTypes.length > 0 && (
          <div className="intel-filter-row" style={{ marginTop: 4 }}>
            <button
              className={`intel-filter-btn intel-filter-btn--channel ${channelFilter === 'all' ? 'is-active' : ''}`}
              onClick={() => setChannelFilter('all')}
            >all channels</button>
            {channelTypes.map((t) => (
              <button
                key={t}
                className={`intel-filter-btn intel-filter-btn--channel ${channelFilter === t ? 'is-active' : ''}`}
                onClick={() => setChannelFilter((cur) => cur === t ? 'all' : t)}
              >{TYPE_ICONS[t] || ''} {t.replace(/_/g, ' ')}</button>
            ))}
          </div>
        )}

        {/* View toggle */}
        <div style={{ display: 'flex', gap: 6, marginTop: 6, marginBottom: 2 }}>
          <button
            className={`intel-filter-btn ${viewMode === 'list' ? 'is-active' : ''}`}
            onClick={() => setViewMode('list')}
          >List</button>
          <button
            className={`intel-filter-btn ${viewMode === 'timeline' ? 'is-active' : ''}`}
            onClick={() => setViewMode('timeline')}
          >Timeline</button>
        </div>
      </section>

      {/* Event list / timeline */}
      <section className="panel-block" style={{ padding: 0 }}>
        {viewMode === 'list' ? (
          filteredEvents.length === 0 ? (
            <ul className="intel-list">
              <li className="intel-empty">
                {(() => {
                  const filtersActive = tierFilter !== 'all'
                    || mainCategoryFilter !== 'all'
                    || incidentTypeFilter !== 'all'
                    || channelFilter !== 'all'
                    || sourceFilter !== 'all'
                    || !!search;
                  // A successful response with zero events is NOT the same as a
                  // dropped connection or the initial connect (docs/fixes.md
                  // Phase 0.4).
                  if (filtersActive) return 'No events matching filters';
                  if (feedStatus === 'loading') return 'Connecting to live feed…';
                  if (feedStatus === 'offline') {
                    return 'Live feed unavailable — reconnecting.';
                  }
                  if (feedStatus === 'stale' || feedStatus === 'retrying') {
                    return 'Connection interrupted — reconnecting.';
                  }
                  if (publicMode) {
                    return (
                      <div className="intel-live-empty">
                        <i />
                        <strong>No {liveMode === 'all' ? '' : 'humanitarian '}signal received</strong>
                        <span>{liveMode === 'all'
                          ? 'Listening to official APIs, partner channels and AIS anomaly detection.'
                          : 'Listening to official APIs and explicitly published partner channels.'}</span>
                      </div>
                    );
                  }
                  return 'No events';
                })()}
              </li>
            </ul>
          ) : (
            // Grouped by tier — operational (distress) always pinned on top.
            visibleTiers.map((t) => {
              const group = tierGroups[t.key];
              if (!group.length) return null;
              // Public Live is an operational <=24h surface. Historical and
              // terminal incidents belong to play.seacommons.org, never a
              // collapsible archive bucket inside this feed.
              const live = group;
              return (
                <div key={t.key} className={`intel-tier-group intel-tier-group--${t.key}`}>
                  <div className="intel-tier-head">
                    {t.key === 'operational' && <span className="intel-tier-dot" />}
                    <span className="intel-tier-head-label">{t.label}</span>
                    <span className="intel-tier-head-sub">{t.sub}</span>
                    {!publicMode ? <span className="intel-tier-head-count">{live.length}</span> : null}
                  </div>
                  <ul className="intel-list" style={{ margin: 0 }}>
                    {live.map(renderEvent)}
                  </ul>
                </div>
              );
            })
          )
        ) : (
          <div className="intel-timeline">
            {timelineGroups.length === 0 ? (
              <div className="intel-empty">No events matching filters</div>
            ) : timelineGroups.map((group) => (
              <div key={group.key} className="intel-timeline-group">
                <div className="intel-timeline-hour">{group.key}</div>
                <ul className="intel-list" style={{ margin: 0 }}>
                  {group.events.map(renderEvent)}
                </ul>
              </div>
            ))}
          </div>
        )}
      </section>

      {/* Channel breakdown */}
      {!publicMode && Object.keys(intelStats.by_type || {}).length > 0 && (
        <section className="panel-block">
          <p className="section-kicker">By channel</p>
          <ul className="signal-list" style={{ marginTop: 4 }}>
            {Object.entries(intelStats.by_type).sort((a, b) => b[1] - a[1]).map(([type, count]) => (
              <li
                key={type}
                style={{ cursor: 'pointer' }}
                onClick={() => setChannelFilter((cur) => cur === type ? 'all' : type)}
                className={channelFilter === type ? 'is-active' : ''}
              >
                <strong>{TYPE_ICONS[type] || ''} {type.replace(/_/g, ' ')}</strong>
                <span>{count}</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {/* Manual injection modal */}
      {!publicMode && showInject && (
        <ManualInjectForm
          apiBase={apiBase}
          onClose={() => setShowInject(false)}
          onSuccess={handleInjectSuccess}
        />
      )}
    </div>
  );
}
