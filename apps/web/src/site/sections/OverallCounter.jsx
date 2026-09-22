import React, { useEffect, useState } from 'react';
import { fetchJson } from '../../services/api/client.js';
import { resolveSiteApiBase } from '../liveApi.js';

function formatCount(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return '—';
  return new Intl.NumberFormat('en-GB').format(n);
}

export default function OverallCounter() {
  const [data, setData] = useState(null);

  useEffect(() => {
    let cancelled = false;
    const apiBase = resolveSiteApiBase();

    Promise.allSettled([
      fetchJson(apiBase, '/api/v1/status?hours=24', undefined, 7000),
      fetchJson(apiBase, '/api/v1/play/counts', undefined, 7000),
    ]).then(([statusResult, playResult]) => {
      if (cancelled) return;
      const status = statusResult.status === 'fulfilled' ? statusResult.value : null;
      const play = playResult.status === 'fulfilled' ? playResult.value : null;
      setData({ status, play });
    });

    return () => { cancelled = true; };
  }, []);

  const status = data?.status;
  const play = data?.play;
  const metrics = [
    ['Archive', play?.total_count, 'Public case record'],
    ['Live now', status?.live?.total, 'Qualified investigations'],
    ['24 h observations', status?.pipeline?.raw_observations, 'Received source records'],
    ['Radio', status?.sensor_activity?.radio_events, 'RF events · 24 h'],
    ['Satellite', status?.sensor_activity?.satellite_observations, 'Acquisitions · 24 h'],
  ];

  return (
    <section className="overall-counter" aria-label="SeaCommons current system totals">
      <div className="overall-counter__intro">
        <span>Current system view</span>
        <strong>Evidence in motion</strong>
        <p>Live shows only qualified investigations. The wider system continues to ingest, compare and preserve evidence.</p>
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
