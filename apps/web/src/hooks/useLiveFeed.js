import { useEffect, useRef, useState } from 'react';

import { edgeSnapshotIsUsable } from '../simulation/liveTracking.js';
import { fetchJson } from '../services/api/client.js';
import { deriveFeedStatus } from '../features/live/feedStatus.js';
import { filterPublicLiveWindow, PUBLIC_LIVE_WINDOW_DAYS } from '../features/live/liveWindow.js';
import { normalizePipelineSources } from '../features/live/pipelineStatus.js';
import {
  edgeSnapshotToFeatures,
  receivedSignalFeatures,
} from '../features/live/normalize.js';

const PUBLIC_CACHE_KEY = 'seacommons_live_signal_cache_v2';
const OPERATOR_CACHE_KEY = 'seacommons_intel_cache';

function cacheKey(isPublicLiveHost, liveMode = 'humanitarian') {
  return isPublicLiveHost ? `${PUBLIC_CACHE_KEY}_${liveMode}` : OPERATOR_CACHE_KEY;
}

function loadCachedEvents(isPublicLiveHost, liveMode = 'humanitarian') {
  try {
    const cached = window.localStorage.getItem(cacheKey(isPublicLiveHost, liveMode));
    if (!cached) return [];
    const parsed = JSON.parse(cached);
    if (!Array.isArray(parsed)) return [];
    const recent = isPublicLiveHost ? filterPublicLiveWindow(parsed) : parsed;
    // Humanitarian eligibility is decided once, in backend policy
    // (core.live.projection._public_intel_feature), and applied identically by
    // the VM feed and the edge publisher. The browser must not layer a second
    // source policy on top (docs/fixes.md F-06).
    return isPublicLiveHost ? receivedSignalFeatures(recent) : recent;
  } catch {
    return [];
  }
}

function storeCachedEvents(isPublicLiveHost, features, liveMode = 'humanitarian') {
  try {
    window.localStorage.setItem(cacheKey(isPublicLiveHost, liveMode), JSON.stringify(features));
  } catch {
    // The live in-memory state remains authoritative when storage is unavailable/full.
  }
}

/** Own the Live/Intel transport lifecycle while exposing only UI-facing state. */
export function useLiveFeed({
  apiBase,
  edgeBase,
  isPublicLiveHost,
  liveMode = 'humanitarian',
  onCriticalDistress,
  onDriftUpdate,
}) {
  const [intelEvents, setIntelEvents] = useState(
    () => loadCachedEvents(isPublicLiveHost, liveMode),
  );
  const [liveModeCounts, setLiveModeCounts] = useState({ humanitarian: null, maritime: null });
  const [liveRoleCounts, setLiveRoleCounts] = useState({});
  const [pipelineSources, setPipelineSources] = useState([]);
  const [intelConnected, setIntelConnected] = useState(false);
  const [intelMode, setIntelMode] = useState('offline');
  // docs/fixes.md F-06 / Phase 0.4: a successful empty response, a transient
  // failure that still has a snapshot, and a hard outage must be distinct.
  const [feedStatus, setFeedStatus] = useState('loading');
  const edgeLiveActiveRef = useRef(false);
  const everSucceededRef = useRef(false);
  const consecutiveFailuresRef = useRef(0);
  const eventCountRef = useRef(intelEvents.length);
  const onCriticalDistressRef = useRef(onCriticalDistress);
  const onDriftUpdateRef = useRef(onDriftUpdate);

  useEffect(() => {
    eventCountRef.current = intelEvents.length;
  }, [intelEvents]);

  function markFeedSuccess(featureCount) {
    everSucceededRef.current = true;
    consecutiveFailuresRef.current = 0;
    setFeedStatus(deriveFeedStatus({
      everSucceeded: true,
      transportHealthy: true,
      lastResponseEmpty: featureCount === 0,
    }));
  }

  function markFeedFailure() {
    consecutiveFailuresRef.current += 1;
    setFeedStatus(deriveFeedStatus({
      everSucceeded: everSucceededRef.current,
      transportHealthy: false,
      consecutiveFailures: consecutiveFailuresRef.current,
      haveCachedEvents: eventCountRef.current > 0,
    }));
  }

  useEffect(() => {
    onCriticalDistressRef.current = onCriticalDistress;
    onDriftUpdateRef.current = onDriftUpdate;
  }, [onCriticalDistress, onDriftUpdate]);

  useEffect(() => {
    if (isPublicLiveHost) setIntelEvents(loadCachedEvents(true, liveMode));
    // Switching compartment restarts the transport lifecycle -- until the
    // first response for the new mode lands, the feed is loading, not empty.
    everSucceededRef.current = false;
    consecutiveFailuresRef.current = 0;
    setFeedStatus('loading');
  }, [isPublicLiveHost, liveMode]);

  // VM-hosted Intel/Live transport: polling starts immediately and WebSocket
  // takes over when a direct backend origin supports upgrades.
  useEffect(() => {
    if (isPublicLiveHost && edgeBase && liveMode === 'humanitarian') return undefined;
    const wsBase = apiBase.replace(/^http/, 'ws');
    const feedPath = isPublicLiveHost
      ? `/api/v1/live/signals?limit=150&days=${PUBLIC_LIVE_WINDOW_DAYS}&mode=${encodeURIComponent(liveMode)}`
      : '/api/v1/intel?limit=200&days=30';
    const streamPath = isPublicLiveHost
      ? `/api/v1/live/stream?mode=${encodeURIComponent(liveMode)}`
      : '/ws/intel';
    const pollIntervalMs = isPublicLiveHost ? 5000 : 30000;
    let ws = null;
    let pollTimer = null;
    let wsAlive = true;
    let polling = false;
    let alive = true;

    function handleWsMessage(event) {
      if (edgeLiveActiveRef.current) return;
      try {
        const message = JSON.parse(event.data);
        if (message.type === 'ping') return;
        if (message.type === 'snapshot') {
          const snapshotFeatures = isPublicLiveHost
            ? receivedSignalFeatures(message.features)
            : Array.isArray(message.features) ? message.features : [];
          setIntelEvents(snapshotFeatures);
          markFeedSuccess(snapshotFeatures.length);
          if (isPublicLiveHost && message.meta?.mode_counts) {
            setLiveModeCounts(message.meta.mode_counts);
            if (message.meta?.role_counts) setLiveRoleCounts(message.meta.role_counts);
          }
        } else if (message.type === 'Feature') {
          const incoming = isPublicLiveHost ? receivedSignalFeatures([message]) : [message];
          if (!incoming.length) return;
          setIntelEvents((previous) => {
            const incomingIds = new Set(
              incoming.map((feature) => feature.properties?.id).filter(Boolean),
            );
            const withoutStale = previous.filter(
              (feature) => !incomingIds.has(feature.properties?.id),
            );
            return [...incoming, ...withoutStale].slice(0, 300);
          });
          const properties = message.properties || {};
          if ((properties.type === 'distress' || properties.type === 'correlated_alert')
            && ['critical', 'high'].includes(properties.severity)) {
            onCriticalDistressRef.current?.(properties);
          }
        } else if (!isPublicLiveHost
          && message.type === 'event_update'
          && message.drift?.trajectory) {
          onDriftUpdateRef.current?.(message);
        }
      } catch {
        // Malformed WebSocket messages never replace the last valid state.
      }
    }

    async function pollOnce() {
      try {
        const data = await fetchJson(apiBase, feedPath);
        if (!alive || edgeLiveActiveRef.current) return;
        if (Array.isArray(data.features)) {
          const features = isPublicLiveHost
            ? receivedSignalFeatures(data.features)
            : data.features;
          setIntelEvents(features);
          storeCachedEvents(isPublicLiveHost, features, liveMode);
          if (isPublicLiveHost) {
            const counts = data.meta?.mode_counts;
            if (counts) setLiveModeCounts(counts);
            if (data.meta?.role_counts) setLiveRoleCounts(data.meta.role_counts);
          }
          setIntelConnected(true);
          setIntelMode((previous) => previous === 'ws' ? 'ws' : 'poll');
          markFeedSuccess(features.length);
        }
      } catch {
        if (!alive || edgeLiveActiveRef.current) return;
        setIntelConnected(false);
        setIntelMode((previous) => previous === 'ws' ? 'ws' : 'offline');
        markFeedFailure();
      }
    }

    async function pollLoop() {
      if (!alive || polling) return;
      polling = true;
      while (alive) {
        await pollOnce();
        if (!alive) break;
        await new Promise((resolve) => {
          pollTimer = window.setTimeout(resolve, pollIntervalMs);
        });
      }
      polling = false;
    }

    function tryWebSocket() {
      if (!alive || !wsAlive) return;
      const token = window.__SEACOMMONS_ACCESS_TOKEN__;
      ws = token
        ? new WebSocket(`${wsBase}${streamPath}`, ['bearer', token])
        : new WebSocket(`${wsBase}${streamPath}`);
      const openTimer = window.setTimeout(() => {
        if (ws && ws.readyState !== WebSocket.OPEN) ws.close();
      }, 4000);
      ws.onopen = () => {
        window.clearTimeout(openTimer);
        if (edgeLiveActiveRef.current) return;
        setIntelConnected(true);
        setIntelMode('ws');
      };
      ws.onclose = () => {
        window.clearTimeout(openTimer);
        if (alive) wsAlive = false;
      };
      ws.onerror = () => {
        window.clearTimeout(openTimer);
        ws?.close();
      };
      ws.onmessage = handleWsMessage;
    }

    pollLoop();
    const apiHost = apiBase.replace(/^https?:\/\//, '').split('/')[0];
    const isDirectBackend = apiBase !== window.location.origin
      || /^(localhost|127\.|192\.168\.|10\.|172\.(1[6-9]|2\d|3[01])\.)/.test(apiHost)
      || /^\d+\.\d+\.\d+\.\d+(:\d+)?$/.test(apiHost);
    if (isDirectBackend) tryWebSocket();

    return () => {
      alive = false;
      wsAlive = false;
      window.clearTimeout(pollTimer);
      ws?.close();
    };
  }, [apiBase, edgeBase, isPublicLiveHost, liveMode]);

  // Public Live is edge-first. Oracle is a rate-limited backup only after
  // repeated edge failures; cached edge state stays visible during failure.
  useEffect(() => {
    if (!isPublicLiveHost || !edgeBase || liveMode !== 'humanitarian') return undefined;
    let alive = true;
    let ws = null;
    let pollTimer = null;
    let reconnectTimer = null;
    let countTimer = null;
    let consecutiveFailures = 0;
    let lastBackupAttempt = 0;

    function applySnapshot(snapshot, transport = 'poll') {
      if (!alive || !edgeSnapshotIsUsable(snapshot)) return false;
      const features = edgeSnapshotToFeatures(snapshot);
      setIntelEvents(features);
      storeCachedEvents(true, features, 'humanitarian');
      setIntelConnected(true);
      setIntelMode(transport);
      markFeedSuccess(features.length);
      edgeLiveActiveRef.current = true;
      consecutiveFailures = 0;
      return true;
    }

    async function loadOracleBackup() {
      const now = Date.now();
      if (now - lastBackupAttempt < 60000) return;
      lastBackupAttempt = now;
      try {
        const data = await fetchJson(
          apiBase,
          `/api/v1/live/signals?limit=150&days=${PUBLIC_LIVE_WINDOW_DAYS}&mode=humanitarian`,
          undefined,
          3000,
        );
        if (!alive || !Array.isArray(data.features)) return;
        const features = receivedSignalFeatures(data.features);
        setIntelEvents(features);
        storeCachedEvents(true, features, 'humanitarian');
        if (data.meta?.mode_counts) setLiveModeCounts(data.meta.mode_counts);
        if (data.meta?.role_counts) setLiveRoleCounts(data.meta.role_counts);
        setIntelConnected(true);
        setIntelMode('poll');
        markFeedSuccess(features.length);
      } catch {
        // Preserve the last valid cached edge snapshot.
      }
    }

    async function refreshModeCounts() {
      window.clearTimeout(countTimer);
      try {
        const data = await fetchJson(
          apiBase,
          `/api/v1/live/signals?limit=20&days=${PUBLIC_LIVE_WINDOW_DAYS}&mode=all`,
          undefined,
          5000,
        );
        if (alive && data.meta?.mode_counts) setLiveModeCounts(data.meta.mode_counts);
        if (alive && data.meta?.role_counts) setLiveRoleCounts(data.meta.role_counts);
      } catch {
        // The current edge snapshot remains usable even when count enrichment fails.
      }
      if (alive) countTimer = window.setTimeout(refreshModeCounts, 60000);
    }

    async function pollSnapshot() {
      window.clearTimeout(pollTimer);
      try {
        const snapshot = await fetchJson(edgeBase, '/v1/live/snapshot', undefined, 4000);
        const transport = ws?.readyState === WebSocket.OPEN ? 'ws' : 'poll';
        if (!applySnapshot(snapshot, transport)) throw new Error('edge snapshot is unusable');
      } catch {
        if (!alive) return;
        consecutiveFailures += 1;
        edgeLiveActiveRef.current = false;
        setIntelConnected(false);
        setIntelMode('offline');
        markFeedFailure();
        if (consecutiveFailures >= 3) await loadOracleBackup();
      }
      if (alive) pollTimer = window.setTimeout(pollSnapshot, 10000);
    }

    function connectWebSocket() {
      if (!alive) return;
      ws = new WebSocket(`${edgeBase.replace(/^http/, 'ws')}/v1/live/stream`);
      const openTimer = window.setTimeout(() => {
        if (ws && ws.readyState !== WebSocket.OPEN) ws.close();
      }, 4000);
      ws.onopen = () => {
        window.clearTimeout(openTimer);
        setIntelConnected(true);
        setIntelMode('ws');
      };
      ws.onmessage = (event) => {
        try {
          const message = JSON.parse(event.data);
          if (message.type === 'snapshot') {
            if (!applySnapshot(message, 'ws')) edgeLiveActiveRef.current = false;
          } else if (['event', 'remove', 'source_health'].includes(message.type)) {
            pollSnapshot();
          }
        } catch {
          // Malformed messages never replace the last valid snapshot.
        }
      };
      ws.onclose = () => {
        window.clearTimeout(openTimer);
        if (alive) reconnectTimer = window.setTimeout(connectWebSocket, 5000);
      };
      ws.onerror = () => {
        window.clearTimeout(openTimer);
        ws?.close();
      };
    }

    pollSnapshot();
    refreshModeCounts();
    connectWebSocket();

    return () => {
      alive = false;
      edgeLiveActiveRef.current = false;
      window.clearTimeout(pollTimer);
      window.clearTimeout(reconnectTimer);
      window.clearTimeout(countTimer);
      ws?.close();
    };
  }, [apiBase, edgeBase, isPublicLiveHost, liveMode]);

  useEffect(() => {
    if (!isPublicLiveHost) return undefined;
    let alive = true;
    let timer = null;
    async function refreshPipeline() {
      try {
        const payload = await fetchJson(apiBase, '/api/v1/live/pipeline', undefined, 5000);
        if (alive) setPipelineSources(normalizePipelineSources(payload));
      } catch {
        // Keep the last good acquisition-health snapshot visible.
      }
      if (alive) timer = window.setTimeout(refreshPipeline, 30000);
    }
    refreshPipeline();
    return () => { alive = false; window.clearTimeout(timer); };
  }, [apiBase, isPublicLiveHost]);

  return {
    intelEvents,
    setIntelEvents,
    intelConnected,
    intelMode,
    feedStatus,
    liveModeCounts,
    liveRoleCounts,
    pipelineSources,
  };
}
