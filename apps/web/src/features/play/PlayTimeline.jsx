import React, { useEffect, useMemo, useRef, useState } from 'react';

import { fetchJson } from '../../services/api/client.js';
import { createVesselArrowImage, VESSEL_COLOR } from '../map/vesselMarker.js';
import { createFallbackMap } from '../map/fallbackRenderer.js';
import {
  incidentCollection,
  mergeIncidentPages,
  playMapStyle,
  incidentStatusAtCutoff,
  incidentsAtCutoff,
  normalizeTimeline,
  resolveGlobalTimelinePosition,
  satelliteRasterDescriptor,
  satelliteFootprintCollection,
  selectFrame,
  selectPreferredSatelliteObservation,
  statusLabel,
  timelineAtCutoff,
  archiveCoverage,
  groupArchiveByMonth,
  satelliteContextTileUrl,
} from './timeline.js';

const TIMELINE_MAX = 1000;

function evidenceCollection(timeline = []) {
  return {
    type: 'FeatureCollection',
    features: timeline
      .filter((item) => item?.geometry)
      .map((item) => ({
        type: 'Feature',
        geometry: item.geometry,
        properties: {
          id: item.id,
          kind: item.type,
          at: item.at,
          title: item.title || '',
        },
      })),
  };
}

function removeSatelliteLayer(map) {
  if (!map) return;
  if (map.getLayer('play-satellite')) map.removeLayer('play-satellite');
  if (map.getSource('play-satellite')) map.removeSource('play-satellite');
}

function addSatelliteLayer(map, observation) {
  removeSatelliteLayer(map);
  const descriptor = satelliteRasterDescriptor(observation);
  if (!descriptor) return;
  map.addSource('play-satellite', descriptor);
  map.addLayer({
    id: 'play-satellite',
    type: 'raster',
    source: 'play-satellite',
    paint: { 'raster-opacity': 0.72, 'raster-fade-duration': 120 },
  }, 'play-incidents');
}

function itemTime(value) {
  try {
    return new Intl.DateTimeFormat('en-GB', {
      day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit',
      timeZone: 'UTC', hour12: false,
    }).format(new Date(value));
  } catch {
    return value || '—';
  }
}

function cutoffLabel(state) {
  if (state.mode === 'all') return 'ALL';
  try {
    return new Intl.DateTimeFormat('en-GB', {
      day: '2-digit', month: 'short', year: 'numeric',
      timeZone: 'UTC',
    }).format(new Date(state.cutoff));
  } catch {
    return state.cutoff || 'PAST';
  }
}

export default function PlayTimeline({ apiBase }) {
  const mapNodeRef = useRef(null);
  const mapRef = useRef(null);
  const fallbackMapRef = useRef(null);
  const [mapReady, setMapReady] = useState(false);
  const [fallbackReady, setFallbackReady] = useState(false);
  const [mapError, setMapError] = useState('');
  const [incidents, setIncidents] = useState([]);
  const [archiveTotal, setArchiveTotal] = useState(null);
  const [selectedId, setSelectedId] = useState('');
  const [caseData, setCaseData] = useState(null);
  const [globalPosition, setGlobalPosition] = useState(TIMELINE_MAX);
  const [casesOpen, setCasesOpen] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [satelliteMission, setSatelliteMission] = useState('auto');
  const [archiveFilter, setArchiveFilter] = useState('all');
  const [satelliteVisible, setSatelliteVisible] = useState(true);
  const [satelliteContextMission, setSatelliteContextMission] = useState('VIIRS NOAA-21');

  const globalState = useMemo(
    () => resolveGlobalTimelinePosition(incidents, globalPosition, TIMELINE_MAX),
    [incidents, globalPosition],
  );
  const visibleIncidents = useMemo(
    () => incidentsAtCutoff(incidents, globalState.cutoff),
    [incidents, globalState.cutoff],
  );
  const filteredIncidents = useMemo(() => visibleIncidents.filter((incident) => {
    if (archiveFilter === 'humanitarian') return incident.domain === 'humanitarian';
    if (archiveFilter === 'maritime') return incident.domain === 'maritime';
    if (archiveFilter === 'investigations') return incident.domain === 'investigation';
    if (archiveFilter === 'correlated') return incident.case_type === 'correlated_alert';
    return true;
  }), [visibleIncidents, archiveFilter]);
  const coverage = useMemo(() => archiveCoverage(incidents), [incidents]);
  const archiveGroups = useMemo(() => groupArchiveByMonth(filteredIncidents), [filteredIncidents]);
  const selectedIncident = useMemo(
    () => visibleIncidents.find((item) => item.incident_id === selectedId) || null,
    [visibleIncidents, selectedId],
  );
  const contextDay = useMemo(() => {
    const value = globalState.cutoff || selectedIncident?.reported_at || coverage.end;
    return value ? String(value).slice(0, 10) : new Date(Date.now() - 24 * 3600 * 1000).toISOString().slice(0, 10);
  }, [globalState.cutoff, selectedIncident?.reported_at, coverage.end]);
  const fullTimeline = useMemo(
    () => normalizeTimeline(caseData?.timeline || []),
    [caseData],
  );
  const visibleTimeline = useMemo(
    () => timelineAtCutoff(fullTimeline, globalState.cutoff),
    [fullTimeline, globalState.cutoff],
  );
  const frame = useMemo(
    () => selectFrame(visibleTimeline, Math.max(0, visibleTimeline.length - 1)),
    [visibleTimeline],
  );
  const satellite = useMemo(
    () => selectPreferredSatelliteObservation(visibleTimeline, frame.item?.at, satelliteMission),
    [visibleTimeline, frame.item?.at, satelliteMission],
  );
  const satelliteMissions = useMemo(
    () => [...new Set(visibleTimeline
      .filter((item) => item?.type === 'satellite' && item?.properties?.mission)
      .map((item) => item.properties.mission))],
    [visibleTimeline],
  );

  useEffect(() => {
    let cancelled = false;
    async function loadIncidents({ full = true } = {}) {
      try {
        let collected = [];
        let offset = 0;
        const seenOffsets = new Set();
        while (!cancelled) {
          if (seenOffsets.has(offset)) throw new Error('Play archive pagination repeated an offset');
          seenOffsets.add(offset);
          const payload = await fetchJson(apiBase, `/api/v1/play/incidents?limit=100&offset=${offset}`, undefined, 30_000);
          const page = Array.isArray(payload?.incidents) ? payload.incidents : [];
          collected = mergeIncidentPages(collected, page);
          if (cancelled) return;
          setIncidents((previous) => mergeIncidentPages((offset > 0 || !full) ? previous : [], collected));
          if (offset === 0) setLoading(false);
          if (payload?.next_offset == null || !full) break;
          const nextOffset = Number(payload.next_offset);
          if (!Number.isFinite(nextOffset) || nextOffset <= offset) {
            throw new Error('Play archive pagination returned an invalid next_offset');
          }
          offset = nextOffset;
        }
        if (!cancelled) setError('');
      } catch (exc) {
        if (!cancelled) setError(exc?.message || 'Play archive unavailable');
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    loadIncidents({ full: true });
    const timer = window.setInterval(() => loadIncidents({ full: false }), 60_000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [apiBase]);

  useEffect(() => {
    let cancelled = false;
    async function loadCounts() {
      try {
        const payload = await fetchJson(apiBase, '/api/v1/play/counts', undefined, 30_000);
        if (!cancelled && Number.isFinite(Number(payload?.total_count))) {
          setArchiveTotal(Number(payload.total_count));
        }
      } catch {
        // Map/archive loading remains usable even if the lightweight counter is unavailable.
      }
    }
    loadCounts();
    const timer = window.setInterval(loadCounts, 60_000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [apiBase]);

  useEffect(() => {
    setSatelliteMission('auto');
  }, [selectedId]);

  useEffect(() => {
    if (satelliteMission !== 'auto' && !satelliteMissions.includes(satelliteMission)) {
      setSatelliteMission('auto');
    }
  }, [satelliteMission, satelliteMissions]);

  useEffect(() => {
    if (selectedId && !visibleIncidents.some((item) => item.incident_id === selectedId)) {
      setSelectedId('');
      setCaseData(null);
    }
  }, [selectedId, visibleIncidents]);

  useEffect(() => {
    let cancelled = false;
    if (!selectedId) {
      setCaseData(null);
      return () => { cancelled = true; };
    }
    setLoading(true);
    fetchJson(apiBase, `/api/v1/play/incidents/${encodeURIComponent(selectedId)}/timeline`)
      .then((payload) => {
        if (!cancelled) {
          setCaseData(payload);
          setError('');
        }
      })
      .catch((exc) => { if (!cancelled) setError(exc?.message || 'Timeline unavailable'); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [apiBase, selectedId]);

  useEffect(() => {
    let disposed = false;
    async function initMap() {
      if (window.__SEACOMMONS_FORCE_MAP_FALLBACK__ === true) throw new Error('forced map fallback');
      await import('maplibre-gl/dist/maplibre-gl.css');
      const { default: maplibregl } = await import('maplibre-gl');
      if (disposed || !mapNodeRef.current || mapRef.current) return;
      const map = new maplibregl.Map({
        container: mapNodeRef.current,
        style: playMapStyle(), center: [14.8, 35.7], zoom: 3.55,
        attributionControl: false,
      });
      map.addControl(new maplibregl.NavigationControl({ showCompass: true }), 'bottom-right');
      map.on('load', () => {
        if (disposed) return;
        map.addImage('vessel-arrow', createVesselArrowImage(48), { sdf: true });
        map.addSource('play-incidents', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
        map.addLayer({
          id: 'play-incidents', type: 'circle', source: 'play-incidents',
          filter: ['!=', ['get', 'marker_kind'], 'vessel'],
          paint: {
            'circle-radius': ['case', ['==', ['get', 'incident_id'], selectedId], 8, 5],
            'circle-color': ['match', ['get', 'domain'], 'maritime', '#8ed8ff', 'investigation', '#f7b955', '#ff746f'],
            'circle-opacity': 0.9,
            'circle-stroke-color': '#071014', 'circle-stroke-width': 1.5,
          },
        });
        map.addLayer({
          id: 'play-vessels', type: 'symbol', source: 'play-incidents',
          filter: ['==', ['get', 'marker_kind'], 'vessel'],
          layout: {
            'icon-image': 'vessel-arrow',
            'icon-size': ['case', ['==', ['get', 'incident_id'], selectedId], 0.46, 0.34],
            'icon-rotate': ['coalesce', ['get', 'course'], 0],
            'icon-rotation-alignment': 'map',
            'icon-allow-overlap': true,
          },
          paint: { 'icon-color': VESSEL_COLOR, 'icon-opacity': 0.94 },
        });
        map.addSource('play-evidence', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
        map.addLayer({
          id: 'play-evidence-fill', type: 'fill', source: 'play-evidence',
          filter: ['==', ['geometry-type'], 'Polygon'],
          paint: { 'fill-color': '#65d9d0', 'fill-opacity': 0.16 },
        });
        map.addLayer({
          id: 'play-evidence-line', type: 'line', source: 'play-evidence',
          filter: ['==', ['geometry-type'], 'LineString'],
          paint: { 'line-color': '#8ed8ff', 'line-width': 3, 'line-opacity': 0.9 },
        });
        map.addSource('play-satellite-footprints', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
        map.addLayer({
          id: 'play-satellite-footprint-fill', type: 'fill', source: 'play-satellite-footprints',
          paint: { 'fill-color': '#75a7ff', 'fill-opacity': 0.08 },
        });
        map.addLayer({
          id: 'play-satellite-footprint-line', type: 'line', source: 'play-satellite-footprints',
          paint: { 'line-color': '#75a7ff', 'line-width': 1.5, 'line-opacity': 0.85, 'line-dasharray': [2, 2] },
        });
        const selectArchiveFeature = (event) => {
          const id = event.features?.[0]?.properties?.incident_id;
          if (id) {
            setSelectedId(String(id));
            setCasesOpen(false);
          }
        };
        for (const layerId of ['play-incidents', 'play-vessels']) {
          map.on('click', layerId, selectArchiveFeature);
          map.on('mouseenter', layerId, () => { map.getCanvas().style.cursor = 'pointer'; });
          map.on('mouseleave', layerId, () => { map.getCanvas().style.cursor = ''; });
        }
        mapRef.current = map;
        map.resize();
        setMapError('');
        setMapReady(true);
      });
    }
    async function activateFallbackMap() {
      if (disposed || !mapNodeRef.current) return;
      try { mapRef.current?.remove(); } catch { /* partial MapLibre init */ }
      mapRef.current = null;
      mapNodeRef.current.replaceChildren();
      const fallback = await createFallbackMap({
        container: mapNodeRef.current,
        center: [14.8, 35.7],
        zoom: 3.55,
        onFeatureSelect: (feature) => {
          const incidentId = feature?.properties?.incident_id;
          if (incidentId) {
            setSelectedId(String(feature.properties.incident_id));
            setCasesOpen(false);
          }
        },
      });
      if (disposed) { fallback.destroy(); return; }
      fallbackMapRef.current = fallback;
      setMapError('');
      setFallbackReady(true);
    }
    initMap().catch(() => activateFallbackMap().catch(() => {
      if (!disposed) setMapError('Map unavailable. Archive cases remain available.');
    }));
    return () => {
      disposed = true;
      try { mapRef.current?.remove(); } catch { /* already removed */ }
      mapRef.current = null;
      fallbackMapRef.current?.destroy();
      fallbackMapRef.current = null;
    };
  }, []);

  useEffect(() => {
    const fallback = fallbackMapRef.current;
    if (!fallbackReady || !fallback) return;
    fallback.setFeatures(incidentCollection(filteredIncidents).features);
    if (filteredIncidents.length) fallback.fitFeatures(incidentCollection(filteredIncidents).features);
    const geometry = selectedIncident?.geometry;
    if (selectedId && geometry?.type === 'Point' && Array.isArray(geometry.coordinates)) {
      fallback.flyTo({ center: geometry.coordinates, zoom: 6.5 });
    }
  }, [fallbackReady, filteredIncidents, selectedId, selectedIncident]);

  useEffect(() => {
    const map = mapRef.current;
    if (!mapReady || !map) return;
    map.getSource('play-incidents')?.setData(incidentCollection(filteredIncidents));
    map.getSource('play-evidence')?.setData(evidenceCollection(visibleTimeline));
    map.getSource('play-satellite-footprints')?.setData(satelliteFootprintCollection(visibleTimeline));
    if (map.getLayer('play-incidents')) {
      map.setPaintProperty('play-incidents', 'circle-radius', ['case', ['==', ['get', 'incident_id'], selectedId], 8, 5]);
    }
    if (map.getLayer('play-vessels')) {
      map.setLayoutProperty('play-vessels', 'icon-size', ['case', ['==', ['get', 'incident_id'], selectedId], 0.46, 0.34]);
    }
    addSatelliteLayer(map, satellite);

    const geometry = selectedIncident?.geometry;
    if (selectedId && geometry?.type === 'Point' && Array.isArray(geometry.coordinates)) {
      const mobile = window.matchMedia?.('(max-width: 680px)')?.matches;
      map.easeTo({
        center: geometry.coordinates,
        zoom: Math.max(map.getZoom(), 6.5),
        padding: mobile ? { top: 24, bottom: Math.round(window.innerHeight * 0.66), left: 24, right: 24 } : { top: 20, bottom: 80, left: 412, right: 20 },
        duration: 450,
      });
    }
  }, [mapReady, filteredIncidents, visibleTimeline, selectedId, selectedIncident, satellite]);

  useEffect(() => {
    const map = mapRef.current;
    if (mapReady && map) window.setTimeout(() => map.resize(), 40);
  }, [mapReady, selectedId, casesOpen, filteredIncidents.length]);

  useEffect(() => {
    const map = mapRef.current;
    if (!mapReady || !map) return;
    const source = map.getSource('satelliteContext');
    source?.setTiles?.([satelliteContextTileUrl(contextDay, satelliteContextMission)]);
    if (map.getLayer('satellite-context')) map.setLayoutProperty('satellite-context', 'visibility', satelliteVisible ? 'visible' : 'none');
  }, [mapReady, satelliteVisible, contextDay, satelliteContextMission]);

  const status = selectedIncident ? incidentStatusAtCutoff(selectedIncident, globalState.cutoff) : 'outcome_unknown';
  const frameProps = frame.item?.properties || {};
  const satelliteProps = satellite?.properties || {};
  const modeLabel = cutoffLabel(globalState);

  return (
    <main className={`cop-shell is-live-mode play-public-shell play-shell ${selectedId ? 'has-selection' : ''}`}>
      <header className="play-header" aria-hidden="true">
        <div>
          <p className="play-eyebrow">SeaCommons / temporal OSINT reconstruction</p>
          <h1>PLAY</h1>
        </div>
        <div className="play-header__actions">
          <div className="play-header__meta">
            <span>{globalState.mode === 'all' ? 'Complete archive' : 'Historical cutoff'}</span>
            <strong>{globalState.mode === 'all' && archiveTotal != null ? archiveTotal : visibleIncidents.length}</strong>
          </div>
        </div>
      </header>

      <button className="live-feed-toggle play-mobile-cases-toggle" type="button" onClick={() => setCasesOpen((value) => !value)}>
        Archive · {archiveTotal != null ? archiveTotal : `${visibleIncidents.length} loaded`}
      </button>

      <aside className={`live-feed-panel is-open play-archive-panel play-cases ${casesOpen ? 'is-mobile-open' : ''}`}>
        <header className="live-feed-panel__header">
          <div className="live-feed-panel__eyebrow">
            <span className="live-feed-panel__mark">SC</span>
            <span>SEACOMMONS / MEDITERRANEAN</span>
            <b><i /> ARCHIVE</b>
          </div>
          <div className="live-feed-panel__title">
            <div><p>TEMPORAL OSINT RECONSTRUCTION</p><h1>Play</h1></div>
            <button type="button" onClick={() => setCasesOpen(false)} aria-label="Close archive">−</button>
          </div>
          <div className="signals-selector play-filter-selector" role="group" aria-label="Play archive filters">
            <div className="signals-selector__list">
              <button type="button" className={`signals-selector__link ${archiveFilter === 'all' ? 'is-active' : ''}`} onClick={() => setArchiveFilter('all')}><span className="signals-selector__box" aria-hidden="true" />ALL</button>
              <button type="button" className={`signals-selector__link ${archiveFilter === 'humanitarian' ? 'is-active' : ''}`} onClick={() => setArchiveFilter('humanitarian')}><span className="signals-selector__box" aria-hidden="true" />HUMANITARIAN</button>
              <button type="button" className={`signals-selector__link ${archiveFilter === 'maritime' ? 'is-active' : ''}`} onClick={() => setArchiveFilter('maritime')}><span className="signals-selector__box" aria-hidden="true" />MARITIME</button>
              <button type="button" className={`signals-selector__link ${archiveFilter === 'investigations' ? 'is-active' : ''}`} onClick={() => setArchiveFilter('investigations')}><span className="signals-selector__box" aria-hidden="true" />INVESTIGATIONS</button>
              <button type="button" className={`signals-selector__link ${archiveFilter === 'correlated' ? 'is-active' : ''}`} onClick={() => setArchiveFilter('correlated')}><span className="signals-selector__box" aria-hidden="true" />CORRELATED</button>
              <button type="button" className={`signals-selector__link ${satelliteVisible ? 'is-active' : ''}`} onClick={() => setSatelliteVisible((value) => !value)}><span className="signals-selector__box" aria-hidden="true" />SATELLITE</button>
            </div>
          </div>
          <div className="play-archive-continuity">
            <p className="live-feed-panel__continuity"><span />Evidence is bounded to what was knowable at the selected cutoff.</p>
            {coverage.start && coverage.end ? (
              <p>{coverage.start.slice(0, 10)} → {coverage.end.slice(0, 10)} · {coverage.coveredMonths.length} months with public cases · {coverage.missingMonths.length} continuity gaps</p>
            ) : null}
          </div>
        </header>
        <div className="live-feed-panel__body play-cases__list">
          {archiveGroups.map((group) => (
            <section className="play-archive-month" key={group.key}>
              <header><strong>{group.label}</strong><span>{group.incidents.length} cases</span></header>
              {group.incidents.map((incident) => {
                const counts = incident.evidence_counts || {};
                return (
                  <button
                    key={incident.incident_id}
                    type="button"
                    className={`play-case ${selectedId === incident.incident_id ? 'is-active' : ''}`}
                    onClick={() => { setSelectedId(incident.incident_id); setCasesOpen(false); }}
                  >
                    <span className="play-case__time">{itemTime(incident.reported_at)}</span>
                    <strong>{incident.title || 'SeaCommons incident'}</strong>
                    <span>{incident.domain || 'humanitarian'} · {incident.source || 'source'} · {statusLabel(incidentStatusAtCutoff(incident, globalState.cutoff))}</span>
                    {(Number(counts.drift) > 0 || Number(counts.satellite) > 0) ? (
                      <span className="play-case__evidence">
                        {Number(counts.drift) > 0 ? <b>DRIFT {counts.drift}</b> : null}
                        {Number(counts.satellite) > 0 ? <b>SAT {counts.satellite}</b> : null}
                      </span>
                    ) : null}
                  </button>
                );
              })}
            </section>
          ))}
          {!filteredIncidents.length && !loading ? <div className="play-empty">No incidents at this time.</div> : null}
        </div>
      </aside>

      <section className="map-stage play-map-stage">
        <div className="map-frame play-map" ref={mapNodeRef} />
        <div className="play-all-badge"><strong>{modeLabel}</strong><span>{archiveTotal != null ? `${archiveTotal} archive` : `${visibleIncidents.length} loaded`}</span></div>
        {loading ? <div className="play-map-message">Loading temporal evidence…</div> : null}
        {error ? <div className="play-map-message is-error">{error}</div> : null}
        {mapError ? <div className="play-map-message is-error">{mapError}</div> : null}
        {selectedId ? (
          <div className="play-map-status">
            <span>{selectedIncident?.source || 'incident'}</span>
            <strong>{statusLabel(status)}</strong>
            {satellite ? <span>{satelliteProps.mission || satellite.source} · {satelliteProps.temporal_relation || 'snapshot'}</span> : null}
          </div>
        ) : null}
      </section>

      <aside className={`cone-panel cone-panel--intel play-evidence ${selectedId ? 'is-open' : ''}`}>
        <div className="play-sheet-handle" />
        <div className="cone-panel-header play-section-head">
          <span>Case dossier</span>
          <button className="cone-close-btn" type="button" onClick={() => setSelectedId('')}>×</button>
        </div>
        <div className="play-evidence__scroll">
          <section className="play-card">
            <p>{selectedIncident?.domain || caseData?.domain || 'incident'}</p>
            <h2>{selectedIncident?.title || 'SeaCommons incident'}</h2>
            <time>{itemTime(selectedIncident?.reported_at)}</time>
            <span>{selectedIncident?.source || '—'}</span>
          </section>
          <section className="play-card play-card--rows">
            <div><span>Status</span><strong>{statusLabel(status)}</strong></div>
            <div><span>View</span><strong>{modeLabel}</strong></div>
            <div><span>Evidence known</span><strong>{visibleTimeline.length}</strong></div>
            {selectedIncident?.evidence_stage ? <div><span>Evidence stage</span><strong>{statusLabel(selectedIncident.evidence_stage)}</strong></div> : null}
            {selectedIncident?.reason_codes?.length ? <div><span>Why flagged</span><strong>{selectedIncident.reason_codes.join(' · ')}</strong></div> : null}
            {frame.item ? <div><span>Latest at cutoff</span><strong>{frame.item.type}</strong></div> : null}
            {frameProps.model ? <div><span>Model</span><strong>{frameProps.model}</strong></div> : null}
            {frameProps.reason_code ? <div><span>Reason</span><strong>{frameProps.reason_code}</strong></div> : null}
          </section>
          <section className="play-card">
            <p>Latest evidence at this time</p>
            <h3>{frame.item?.title || 'No evidence available before this cutoff'}</h3>
            {frame.item ? <time>{itemTime(frame.item.at)}</time> : null}
            <span>{frame.item?.source || '—'}</span>
            {frameProps.url ? <a href={frameProps.url} target="_blank" rel="noreferrer">Source ↗</a> : null}
          </section>
          <section className="play-card">
            <p>Satellite context</p>
            {satelliteProps.asset_ref ? (
              <figure className="play-satellite-preview">
                <img
                  src={satelliteProps.asset_ref}
                  alt={`Satellite quicklook ${satelliteProps.mission || ''}`.trim()}
                  loading="lazy"
                  referrerPolicy="no-referrer"
                />
                <figcaption>Satellite quicklook · {satelliteProps.mission || satellite?.source || 'acquisition'}</figcaption>
              </figure>
            ) : null}
            <label className="play-satellite-selector">
              <span>Global imagery</span>
              <select value={satelliteContextMission} onChange={(event) => setSatelliteContextMission(event.target.value)}>
                <option value="VIIRS NOAA-21">NOAA-21 · dated true color</option>
                <option value="VIIRS NOAA-20">NOAA-20 · dated true color</option>
                <option value="VIIRS Suomi-NPP">Suomi-NPP · dated true color</option>
              </select>
            </label>
            <span>Context date {contextDay} · NASA EOSDIS GIBS</span>
            <label className="play-satellite-selector">
              <span>Case acquisition</span>
              <select value={satelliteMission} onChange={(event) => setSatelliteMission(event.target.value)}>
                <option value="auto">Auto · Sentinel first</option>
                {satelliteMissions.map((mission) => <option key={mission} value={mission}>{mission}</option>)}
              </select>
            </label>
            <h3>{satelliteProps.mission || 'No acquisition before this time'}</h3>
            {satellite ? <time>{itemTime(satellite.at)}</time> : null}
            <span>{satelliteProps.sensor_type || satellite?.source || '—'}</span>
            {Number.isFinite(Number(satelliteProps.temporal_delta_s)) ? <span>Δt {Math.round(Number(satelliteProps.temporal_delta_s) / 3600)} h from report</span> : null}
            {satelliteProps.product_id ? <span>Product {satelliteProps.product_id}</span> : null}
            {satelliteProps.temporal_relation ? <span>{satelliteProps.temporal_relation}</span> : null}
            {satelliteProps.polarisation?.length ? <span>Polarisation {satelliteProps.polarisation.join(' / ')}</span> : null}
            {satelliteProps.cloud_cover != null && Number.isFinite(Number(satelliteProps.cloud_cover)) ? <span>Cloud {Math.round(Number(satelliteProps.cloud_cover))}%</span> : null}
            {satelliteProps.evidence_status ? <span>Evidence {satelliteProps.evidence_status}</span> : null}
            {satelliteProps.source_url ? <a href={satelliteProps.source_url} target="_blank" rel="noreferrer">Acquisition record ↗</a> : null}
          </section>
        </div>
      </aside>

      <footer className="play-timeline-bar">
        <span className="play-timeline-end">{coverage.start ? coverage.start.slice(0, 10) : 'PAST'}</span>
        <button type="button" disabled={globalPosition <= 0} onClick={() => setGlobalPosition((value) => Math.max(0, value - 50))}>←</button>
        <input
          aria-label="Global archive timeline"
          type="range"
          min="0"
          max={TIMELINE_MAX}
          value={globalPosition}
          onChange={(event) => setGlobalPosition(Number(event.target.value))}
        />
        <button type="button" disabled={globalPosition >= TIMELINE_MAX} onClick={() => setGlobalPosition((value) => Math.min(TIMELINE_MAX, value + 50))}>→</button>
        <div className="play-timeline-bar__label"><strong>{modeLabel}</strong><span>{globalState.mode === 'all' ? `through ${coverage.end?.slice(0, 10) || 'latest'}` : `imagery ${contextDay}`}</span></div>
        <button className="play-all-reset" type="button" disabled={globalState.mode === 'all'} onClick={() => setGlobalPosition(TIMELINE_MAX)}>ALL</button>
      </footer>
    </main>
  );
}
