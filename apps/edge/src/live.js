import {
  FEDERATED_EVENT_SCHEMA,
  INCIDENT_LIFECYCLES,
  LOCATION_PRECISIONS,
  PUBLIC_GEOMETRY_TYPES,
} from './live-contracts.js';

const MAX_EVENTS = 500;
const DEFAULT_TTL_SECONDS = 24 * 60 * 60;
const DEFAULT_HEARTBEAT_MAX_AGE_SECONDS = 120;
const INCIDENT_LIFECYCLE_SET = new Set(INCIDENT_LIFECYCLES);
const LOCATION_PRECISION_SET = new Set(LOCATION_PRECISIONS);

function validateGeometry(geometry) {
  if (geometry === null || geometry === undefined) return null;
  if (typeof geometry !== 'object' || Array.isArray(geometry)) {
    throw new Error('geometry must be an object or null');
  }
  if (!PUBLIC_GEOMETRY_TYPES.includes(geometry.type)) {
    throw new Error('geometry type must be Point, Polygon, MultiPolygon, or null');
  }
  if (!Array.isArray(geometry.coordinates)) throw new Error('geometry coordinates are required');
  return geometry;
}

function validateProperties(properties) {
  if (properties === undefined || properties === null) return {};
  if (typeof properties !== 'object' || Array.isArray(properties)) {
    throw new Error('properties must be an object');
  }
  const lifecycle = properties.incident_lifecycle;
  if (lifecycle !== undefined && lifecycle !== null && !INCIDENT_LIFECYCLE_SET.has(lifecycle)) {
    throw new Error('invalid incident_lifecycle');
  }
  const precision = properties.location_precision;
  if (precision !== undefined && precision !== null && !LOCATION_PRECISION_SET.has(precision)) {
    throw new Error('invalid location_precision');
  }
  return properties;
}

function json(payload, status = 200, headers = {}) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json; charset=utf-8', ...headers },
  });
}

async function sha256Hex(value) {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value));
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, '0')).join('');
}

async function hmacHex(secret, value) {
  const key = await crypto.subtle.importKey(
    'raw',
    new TextEncoder().encode(secret),
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign'],
  );
  const signature = await crypto.subtle.sign('HMAC', key, new TextEncoder().encode(value));
  return [...new Uint8Array(signature)].map((byte) => byte.toString(16).padStart(2, '0')).join('');
}

function timingSafeEqual(left, right) {
  if (left.length !== right.length) return false;
  let mismatch = 0;
  for (let index = 0; index < left.length; index += 1) {
    mismatch |= left.charCodeAt(index) ^ right.charCodeAt(index);
  }
  return mismatch === 0;
}

function ttlSeconds(env) {
  const parsed = Number(env.LIVE_EVENT_TTL_SECONDS || DEFAULT_TTL_SECONDS);
  return Number.isFinite(parsed) && parsed >= 60 ? parsed : DEFAULT_TTL_SECONDS;
}

function heartbeatMaxAgeSeconds(env) {
  const parsed = Number(env.LIVE_HEARTBEAT_MAX_AGE_SECONDS || DEFAULT_HEARTBEAT_MAX_AGE_SECONDS);
  return Number.isFinite(parsed) && parsed >= 30 ? parsed : DEFAULT_HEARTBEAT_MAX_AGE_SECONDS;
}

function ageSeconds(value, now = Date.now()) {
  const parsed = Date.parse(value || '');
  if (!Number.isFinite(parsed)) return null;
  return Math.max(0, Math.round((now - parsed) / 1000));
}

export function classifyLiveStatus({ lastHeartbeatAt = null, eventCount = 0 } = {}, now = Date.now(), maxAgeSeconds = DEFAULT_HEARTBEAT_MAX_AGE_SECONDS) {
  const heartbeatAge = ageSeconds(lastHeartbeatAt, now);
  if (heartbeatAge !== null && heartbeatAge <= maxAgeSeconds) return 'live';
  if (eventCount > 0) return 'degraded';
  return 'waiting';
}

function isFresh(event, env, now = Date.now()) {
  const expiresAt = Number(event.expires_at_ms || 0);
  if (expiresAt) return expiresAt > now;
  const received = Date.parse(event.received_at || event.observed_at || '');
  return Number.isFinite(received) && received + ttlSeconds(env) * 1000 > now;
}

function isStaleIncidentVersion(current, incoming, removed) {
  if (!current) return false;
  const currentObservedAt = Date.parse(current.observed_at || '');
  const incomingObservedAt = Date.parse(incoming.observed_at || '');
  if (Number.isFinite(currentObservedAt) && incomingObservedAt < currentObservedAt) return true;
  // A removal is a tombstone for the source observation it resolves. A
  // delayed retry for that observation must not make the incident active
  // again; only a genuinely newer source observation may supersede it.
  return Boolean(current.removed)
    && !removed
    && Number.isFinite(currentObservedAt)
    && incomingObservedAt <= currentObservedAt;
}

export async function verifyIngestRequest(request, secret) {
  if (!secret) return { ok: false, status: 503, error: 'INGEST_SECRET is not configured' };
  const body = await request.text();
  const supplied = request.headers.get('X-SeaCommons-Signature') || '';
  const expected = await hmacHex(secret, body);
  if (!timingSafeEqual(supplied.toLowerCase(), expected)) {
    return { ok: false, status: 401, error: 'invalid event signature' };
  }
  try {
    return { ok: true, body, event: JSON.parse(body) };
  } catch {
    return { ok: false, status: 400, error: 'invalid JSON' };
  }
}

export async function normalizeEvent(input, previousHash = null, ttl = DEFAULT_TTL_SECONDS) {
  if (!input || typeof input !== 'object') throw new Error('event must be an object');
  if (!input.type || !input.source || !input.observed_at) {
    throw new Error('type, source and observed_at are required');
  }
  const numericConfidence = input.confidence === null || input.confidence === undefined
    ? null
    : Number(input.confidence);
  if (numericConfidence !== null
      && (!Number.isFinite(numericConfidence) || numericConfidence < 0 || numericConfidence > 1)) {
    throw new Error('confidence must be between 0 and 1');
  }
  const receivedAt = new Date().toISOString();
  const observedAtMs = Date.parse(input.observed_at);
  const event = {
    schema: FEDERATED_EVENT_SCHEMA,
    type: String(input.type),
    source: String(input.source),
    node: String(input.node || 'unknown'),
    observed_at: new Date(input.observed_at).toISOString(),
    received_at: receivedAt,
    // Live age is measured from the report, not from delayed delivery. This
    // prevents a replay/backfill from reviving an already retired incident.
    expires_at_ms: observedAtMs + ttl * 1000,
    visibility: input.visibility === 'private' ? 'private' : 'public',
    confidence: numericConfidence,
    geometry: validateGeometry(input.geometry),
    properties: validateProperties(input.properties),
    source_url: input.source_url ? String(input.source_url) : null,
    previous_hash: previousHash,
  };
  event.id = input.id ? String(input.id) : await sha256Hex(JSON.stringify(event));
  event.hash = await sha256Hex(JSON.stringify(event));
  return event;
}

export class LiveRoom {
  constructor(state, env) {
    this.state = state;
    this.env = env;
  }

  async fetch(request) {
    const url = new URL(request.url);
    if (url.pathname.endsWith('/stream')) return this.stream(request);
    if (url.pathname.endsWith('/snapshot')) return this.snapshot();
    if (url.pathname.endsWith('/status')) return this.status();
    if (url.pathname.endsWith('/heartbeat') && request.method === 'POST') return this.heartbeat(request);
    if (url.pathname.endsWith('/reset') && request.method === 'POST') return this.reset(request);
    if (url.pathname.endsWith('/events') && request.method === 'POST') return this.ingest(request);
    return json({ error: 'not found' }, 404);
  }

  async stream(request) {
    if (request.headers.get('Upgrade') !== 'websocket') {
      return json({ error: 'websocket upgrade required' }, 426);
    }
    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair);
    this.state.acceptWebSocket(server);
    server.send(JSON.stringify({ type: 'snapshot', ...(await this.loadSnapshot()) }));
    return new Response(null, { status: 101, webSocket: client });
  }

  async snapshot() {
    return json(await this.loadSnapshot(), 200, {
      'Cache-Control': 'no-store, max-age=0',
    });
  }

  async status() {
    const snapshot = await this.loadSnapshot();
    const eventAgeSeconds = ageSeconds(snapshot.updated_at);
    const heartbeatAgeSeconds = ageSeconds(snapshot.last_heartbeat_at);
    return json({
      status: classifyLiveStatus(
        { lastHeartbeatAt: snapshot.last_heartbeat_at, eventCount: snapshot.events.length },
        Date.now(),
        heartbeatMaxAgeSeconds(this.env),
      ),
      updated_at: snapshot.updated_at,
      age_seconds: eventAgeSeconds,
      last_event_at: snapshot.updated_at,
      event_age_seconds: eventAgeSeconds,
      last_heartbeat_at: snapshot.last_heartbeat_at,
      heartbeat_age_seconds: heartbeatAgeSeconds,
      event_count: snapshot.events.length,
      counts: snapshot.counts,
      sources: snapshot.source_health,
      ttl_seconds: ttlSeconds(this.env),
      websocket_clients: this.state.getWebSockets().length,
    });
  }

  async heartbeat(request) {
    const verified = await verifyIngestRequest(request, this.env.INGEST_SECRET);
    if (!verified.ok) return json({ error: verified.error }, verified.status);
    const input = verified.event;
    if (!input || typeof input !== 'object' || !input.source) {
      return json({ error: 'source is required' }, 400);
    }
    let health;
    try {
      health = await this.recordSourceHealth(input);
    } catch {
      return json({ error: 'observed_at must be a valid timestamp' }, 400);
    }
    const message = JSON.stringify({ type: 'source_health', source: health });
    for (const socket of this.state.getWebSockets()) {
      try { socket.send(message); } catch { socket.close(1011, 'health broadcast failed'); }
    }
    return json({ accepted: true, source: health }, 202);
  }

  async reset(request) {
    const verified = await verifyIngestRequest(request, this.env.INGEST_SECRET);
    if (!verified.ok) return json({ error: verified.error }, verified.status);
    await this.state.storage.deleteAll();
    for (const socket of this.state.getWebSockets()) {
      try { socket.send(JSON.stringify({ type: 'reset', at: new Date().toISOString() })); } catch { socket.close(1011, 'reset broadcast failed'); }
    }
    return json({ reset: true, at: new Date().toISOString() }, 200);
  }

  async ingest(request) {
    const verified = await verifyIngestRequest(request, this.env.INGEST_SECRET);
    if (!verified.ok) return json({ error: verified.error }, verified.status);

    const previousHash = await this.state.storage.get('head_hash') || null;
    let event;
    try {
      event = await normalizeEvent(verified.event, previousHash, ttlSeconds(this.env));
    } catch (error) {
      return json({ error: error instanceof Error ? error.message : 'invalid event' }, 400);
    }
    if (event.visibility !== 'public') return json({ error: 'private events are not accepted by public Live' }, 403);

    const seen = await this.state.storage.get(`event:${event.id}`);
    if (seen) return json({ accepted: true, duplicate: true, event_id: event.id }, 200);

    const existing = await this.state.storage.get('events') || [];
    const fresh = existing.filter((item) => isFresh(item, this.env));
    const targetId = String(event.properties?.incident_id || event.id);
    const removed = Boolean(event.properties?.expired || event.type === 'incident_removed');
    const incidentVersionKey = `incident:${targetId}`;
    const visibleVersion = fresh.find(
      (item) => String(item.properties?.incident_id || item.id) === targetId,
    );
    const currentVersion = await this.state.storage.get(incidentVersionKey) || (
      visibleVersion ? {
        event_id: visibleVersion.id,
        observed_at: visibleVersion.observed_at,
        removed: false,
      } : null
    );
    if (isStaleIncidentVersion(currentVersion, event, removed)) {
      await this.state.storage.put(`event:${event.id}`, true);
      return json({ accepted: true, stale: true, event_id: event.id }, 200);
    }
    const withoutPreviousVersion = fresh.filter((item) => String(item.properties?.incident_id || item.id) !== targetId);
    // Only an explicit "expired" signal from the canonical publisher removes
    // an incident outright. It covers a direct resolution as well as the
    // 7-day cutoff; the edge never tries to reclassify incident text itself.
    const events = removed ? withoutPreviousVersion : [...withoutPreviousVersion, event].slice(-MAX_EVENTS);

    await this.state.storage.put({
      events,
      head_hash: event.hash,
      updated_at: event.received_at,
      [`event:${event.id}`]: true,
      [incidentVersionKey]: {
        event_id: event.id,
        observed_at: event.observed_at,
        removed,
      },
    });

    const message = JSON.stringify({ type: removed ? 'remove' : 'event', event, incident_id: targetId });
    for (const socket of this.state.getWebSockets()) {
      try { socket.send(message); } catch { socket.close(1011, 'broadcast failed'); }
    }

    if (this.env.NOSTR_BRIDGE_URL) {
      this.state.waitUntil(fetch(this.env.NOSTR_BRIDGE_URL, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(this.env.NOSTR_BRIDGE_TOKEN ? { Authorization: `Bearer ${this.env.NOSTR_BRIDGE_TOKEN}` } : {}),
        },
        body: JSON.stringify(event),
      }).catch(() => undefined));
    }

    if (this.env.LIVE_SNAPSHOTS) {
      const snapshot = JSON.stringify(await this.loadSnapshot());
      this.state.waitUntil(this.env.LIVE_SNAPSHOTS.put('live/latest.json', snapshot, {
        httpMetadata: { contentType: 'application/json' },
      }));
    }

    return json({ accepted: true, removed, event_id: event.id, hash: event.hash }, 202);
  }

  async loadSnapshot() {
    const stored = await this.state.storage.get('events') || [];
    const events = stored.filter((event) => isFresh(event, this.env));
    if (events.length !== stored.length) await this.state.storage.put('events', events);
    const sourceHealth = await this.loadSourceHealth();
    const lifecycleCounts = events.reduce((counts, event) => {
      const lifecycle = String(event.properties?.incident_lifecycle || 'active');
      counts[lifecycle] = (counts[lifecycle] || 0) + 1;
      return counts;
    }, { active: 0, resolved: 0, needs_review: 0, archived: 0 });
    return {
      schema: 'seacommons-live-snapshot-v1',
      mode: 'ephemeral-live',
      generated_at: new Date().toISOString(),
      updated_at: await this.state.storage.get('updated_at') || null,
      head_hash: await this.state.storage.get('head_hash') || null,
      last_heartbeat_at: sourceHealth.reduce(
        (latest, source) => !latest || source.received_at > latest ? source.received_at : latest,
        null,
      ),
      source_health: sourceHealth,
      counts: { total: events.length, ...lifecycleCounts },
      ttl_seconds: ttlSeconds(this.env),
      events,
    };
  }

  async recordSourceHealth(input, receivedAt = new Date().toISOString()) {
    const source = String(input.source || '').trim();
    const node = String(input.node || 'unknown').trim() || 'unknown';
    const key = `${node}:${source}`;
    const stored = await this.state.storage.get('source_health') || {};
    const requestedStatus = String(input.status || 'active').toLowerCase();
    const status = ['active', 'degraded', 'offline'].includes(requestedStatus)
      ? requestedStatus
      : 'degraded';
    const health = {
      source,
      node,
      status,
      observed_at: new Date(input.observed_at || receivedAt).toISOString(),
      received_at: receivedAt,
    };
    stored[key] = health;
    await this.state.storage.put('source_health', stored);
    return health;
  }

  async loadSourceHealth(now = Date.now()) {
    const stored = await this.state.storage.get('source_health') || {};
    const maxAge = heartbeatMaxAgeSeconds(this.env);
    return Object.values(stored).map((source) => {
      const sourceAge = ageSeconds(source.received_at, now);
      return {
        ...source,
        age_seconds: sourceAge,
        status: sourceAge !== null && sourceAge <= maxAge ? source.status : 'offline',
      };
    }).sort((left, right) => left.source.localeCompare(right.source));
  }
}

export function liveRoomStub(env) {
  const id = env.LIVE_ROOM.idFromName('mediterranean-public-live');
  return env.LIVE_ROOM.get(id);
}
