import React, { useEffect, useRef, useState } from 'react';
import { Ticker } from '../ui/index.js';
import { fetchJson } from '../services/api/client.js';
import { receivedSignalFeatures } from '../features/live/normalize.js';
import { resolveSiteApiBase, LIVE_HOST_URL } from './liveApi.js';

const POLL_MS = 20000;
const MAX_ITEMS = 8;

// Rose is reserved for Humanitarian across the public SeaCommons UI.
// Maritime uses the sea token; the header does not encode severity as colour.
function categoryToken(properties) {
  const category = String(properties?.main_category || properties?.maritime_domain || '').toLowerCase();
  return category === 'humanitarian' ? 'rose' : 'sea';
}

function relativeTime(isoString) {
  const then = Date.parse(isoString);
  if (!Number.isFinite(then)) return '';
  const minutes = Math.max(0, Math.round((Date.now() - then) / 60000));
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

/** Compact single-line live-signal ticker in the header, replacing the static tagline. */
export default function HeaderLive() {
  const [items, setItems] = useState(null); // null = loading, [] = confirmed empty
  const [failed, setFailed] = useState(false);
  const aliveRef = useRef(true);

  useEffect(() => {
    aliveRef.current = true;
    const apiBase = resolveSiteApiBase();

    async function poll() {
      try {
        const data = await fetchJson(apiBase, '/api/v1/live/signals?limit=30&days=2', undefined, 6000);
        if (!aliveRef.current) return;
        const features = receivedSignalFeatures(data.features)
          .filter((f) => f.properties?.timestamp_utc)
          .sort((a, b) => Date.parse(b.properties.timestamp_utc) - Date.parse(a.properties.timestamp_utc))
          .slice(0, MAX_ITEMS);
        setItems(features);
        setFailed(false);
      } catch {
        if (!aliveRef.current) return;
        setFailed(true);
      }
    }

    poll();
    const timer = window.setInterval(poll, POLL_MS);
    return () => {
      aliveRef.current = false;
      window.clearInterval(timer);
    };
  }, []);

  const top = items?.[0]?.properties;
  const dotState = failed ? 'idle' : top ? categoryToken(top) : 'idle';

  return (
    <a className="site-header__live" href={LIVE_HOST_URL} aria-label="Open Live — latest public cases">
      <i className={`site-header__live-dot is-${dotState}`} aria-hidden="true" />
      <span className="site-header__live-label">Live</span>
      <span className="site-header__live-ticker">
        {failed && <span className="site-header__live-status">Live feed unavailable</span>}
        {!failed && items === null && <span className="site-header__live-status">Connecting…</span>}
        {!failed && items?.length === 0 && <span className="site-header__live-status">No active public cases</span>}
        {!failed && items && items.length > 0 && (
          <Ticker
            duration={26}
            separator="·"
            items={items.map((f) => `${f.properties.title} — ${relativeTime(f.properties.timestamp_utc)}`)}
          />
        )}
      </span>
    </a>
  );
}
