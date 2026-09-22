import React, { useEffect, useState } from 'react';
import { fetchJson } from '../../services/api/client.js';
import { resolveSiteApiBase } from '../liveApi.js';

function formatCount(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return '—';
  return new Intl.NumberFormat('en-GB').format(n);
}

export default function OverallCounter() {
  const [play, setPlay] = useState(null);

  useEffect(() => {
    let cancelled = false;
    const apiBase = resolveSiteApiBase();

    async function refresh() {
      try {
        const next = await fetchJson(apiBase, '/api/v1/play/counts', undefined, 7000);
        if (!cancelled) setPlay(next);
      } catch {
        if (!cancelled) setPlay(null);
      }
    }

    refresh();
    const timer = window.setInterval(refresh, 60_000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);

  const metrics = [
    ['Overall', play?.total_count],
    ['Humanitarian', play?.humanitarian_count],
    ['Maritime', play?.maritime_count],
    ['Investigations', play?.investigation_count],
  ];

  return (
    <section className="overall-counter" aria-label="SeaCommons all-time public case totals">
      {metrics.map(([label, value]) => (
        <div className="overall-counter__metric" key={label}>
          <span>{label}</span>
          <strong>{play ? formatCount(value) : '…'}</strong>
        </div>
      ))}
    </section>
  );
}
