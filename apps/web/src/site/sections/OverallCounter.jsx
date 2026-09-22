import React, { useEffect, useMemo, useState } from 'react';
import { fetchJson } from '../../services/api/client.js';
import { resolveSiteApiBase } from '../liveApi.js';

function formatCount(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return '—';
  return new Intl.NumberFormat('en-GB').format(n);
}

function relativeFreshness(value) {
  const timestamp = Date.parse(value || '');
  if (!Number.isFinite(timestamp)) return 'freshness unavailable';
  const minutes = Math.max(0, Math.round((Date.now() - timestamp) / 60000));
  if (minutes < 1) return 'updated just now';
  if (minutes < 60) return `updated ${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `updated ${hours}h ago`;
  return `updated ${Math.round(hours / 24)}d ago`;
}

export default function OverallCounter() {
  const [data, setData] = useState(null);

  useEffect(() => {
    let cancelled = false;
    const apiBase = resolveSiteApiBase();

    async function refresh() {
      const [statusResult, playResult] = await Promise.allSettled([
        fetchJson(apiBase, '/api/v1/status?hours=24', undefined, 7000),
        fetchJson(apiBase, '/api/v1/play/counts', undefined, 7000),
      ]);
      if (cancelled) return;
      const status = statusResult.status === 'fulfilled' ? statusResult.value : null;
      const play = playResult.status === 'fulfilled' ? playResult.value : null;
      setData({ status, play });
    }

    refresh();
    const timer = window.setInterval(refresh, 60_000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);

  const status = data?.status;
  const play = data?.play;
  const metrics = [
    ['Observations · 24 h', status?.pipeline?.raw_observations, 'Immutable source envelopes'],
    ['Normalized events', status?.pipeline?.parsed_events, 'Shared event vocabulary'],
    ['Derived cues', status?.pipeline?.derived_cues, 'Rule/model outputs · not findings'],
    ['Episodes', status?.pipeline?.maritime_episodes, 'Related evidence grouped'],
    ['Live cases', status?.live?.total, `Humanitarian ${formatCount(status?.live?.humanitarian)} · Maritime ${formatCount(status?.live?.maritime)}`],
    ['Archive · all time', play?.total_count, 'Persisted public case records'],
  ];

  const sensorActivity = [
    ['AIS fixes', status?.sensor_activity?.ais_fixes],
    ['Radio events', status?.sensor_activity?.radio_events],
    ['Satellite observations', status?.sensor_activity?.satellite_observations],
    ['Active sources', status?.sensor_activity?.active_source_names],
  ];

  const newest = useMemo(() => {
    const values = [
      status?.freshness?.raw_observation,
      status?.freshness?.parsed_event,
      status?.freshness?.analysis_output,
    ].filter(Boolean);
    if (!values.length) return null;
    return values.sort((a, b) => Date.parse(b) - Date.parse(a))[0];
  }, [status]);

  return (
    <section className="overall-counter" aria-label="SeaCommons canonical pipeline totals">
      <div className="overall-counter__intro">
        <span>Pipeline / 24 h · Archive / all time</span>
        <strong>Evidence in motion</strong>
        <p>
          Pipeline and sensor activity use a rolling 24-hour window. Archive is the all-time
          public case catalog. These counts describe different objects and are not interchangeable.
        </p>
        <small className="overall-counter__freshness">{data ? relativeFreshness(newest) : 'connecting…'}</small>
        <div className="overall-counter__sensors" aria-label="Sensor activity in the last 24 hours">
          {sensorActivity.map(([label, value]) => (
            <span key={label}><b>{data ? formatCount(value) : '…'}</b>{label}</span>
          ))}
        </div>
      </div>
      <div className="overall-counter__grid">
        {metrics.map(([label, value, note]) => (
          <div className="overall-counter__metric" key={label}>
            <span>{label}</span>
            <strong>{data ? formatCount(value) : '…'}</strong>
            <small>{note}</small>
          </div>
        ))}
      </div>
    </section>
  );
}
