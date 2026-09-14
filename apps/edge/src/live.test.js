import test from 'node:test';
import assert from 'node:assert/strict';
import { INCIDENT_LIFECYCLES, LOCATION_PRECISIONS } from './live-contracts.js';
import { classifyLiveStatus, LiveRoom, normalizeEvent, verifyIngestRequest } from './live.js';

function clone(value) {
  return value === undefined ? undefined : structuredClone(value);
}

class MemoryStorage {
  constructor(values = new Map()) {
    this.values = values;
  }

  async get(key) {
    return clone(this.values.get(key));
  }

  async put(keyOrEntries, value) {
    if (typeof keyOrEntries === 'string') {
      this.values.set(keyOrEntries, clone(value));
      return;
    }
    for (const [key, entry] of Object.entries(keyOrEntries)) {
      this.values.set(key, clone(entry));
    }
  }

  async deleteAll() {
    this.values.clear();
  }
}

class FakeSocket {
  constructor() {
    this.messages = [];
    this.closed = null;
  }

  send(message) {
    this.messages.push(message);
  }

  close(code, reason) {
    this.closed = { code, reason };
  }
}

class FakeState {
  constructor(storage = new MemoryStorage()) {
    this.storage = storage;
    this.sockets = [];
    this.backgroundTasks = [];
  }

  acceptWebSocket(socket) {
    this.sockets.push(socket);
  }

  getWebSockets() {
    return this.sockets;
  }

  waitUntil(task) {
    this.backgroundTasks.push(task);
  }
}

async function hmac(secret, body) {
  const key = await crypto.subtle.importKey(
    'raw',
    new TextEncoder().encode(secret),
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign'],
  );
  const signature = await crypto.subtle.sign('HMAC', key, new TextEncoder().encode(body));
  return [...new Uint8Array(signature)].map((byte) => byte.toString(16).padStart(2, '0')).join('');
}

async function ingest(room, event, secret = 'test-secret') {
  const body = JSON.stringify(event);
  const response = await room.ingest(new Request('https://edge.example/v1/live/events', {
    method: 'POST',
    headers: { 'X-SeaCommons-Signature': await hmac(secret, body) },
    body,
  }));
  return { response, body: await response.json() };
}

function incidentEvent({
  id,
  incidentId = 'incident-1',
  observedAt = '2099-08-26T10:00:00Z',
  removed = false,
}) {
  return {
    id,
    type: removed ? 'incident_removed' : 'distress_observation',
    source: 'alarm_phone',
    node: 'collector-1',
    observed_at: observedAt,
    visibility: 'public',
    properties: {
      incident_id: incidentId,
      incident_lifecycle: removed ? 'resolved' : 'active',
      location_precision: 'unpositioned',
      ...(removed ? { expired: true } : {}),
    },
  };
}

test('normalizes a public event with an explicit live expiry', async () => {
  const observedAt = '2026-08-02T12:00:00Z';
  const event = await normalizeEvent({
    type: 'distress_observation',
    source: 'alarm-phone',
    node: 'oracle-collector-1',
    observed_at: observedAt,
    confidence: 0.7,
    geometry: { type: 'Point', coordinates: [14.5, 35.5] },
  }, 'previous', 3600);

  assert.equal(event.schema, 'seacommons-event-v1');
  assert.equal(event.previous_hash, 'previous');
  assert.equal(event.visibility, 'public');
  assert.match(event.id, /^[a-f0-9]{64}$/);
  assert.match(event.hash, /^[a-f0-9]{64}$/);
  assert.equal(event.expires_at_ms, Date.parse(observedAt) + 3_600_000);
});

test('edge transport preserves the semantic category and origin metadata', async () => {
  // Product policy §13: the edge must not strip category / coordinate source /
  // uncertainty / origin id. It passes provider properties through verbatim.
  const event = await normalizeEvent({
    type: 'distress_observation',
    source: 'Alarm Phone',
    node: 'oracle-collector-1',
    observed_at: '2026-08-02T12:00:00Z',
    geometry: { type: 'Point', coordinates: [14.5, 35.5] },
    properties: {
      incident_id: 'ap-42',
      visual_category: 'humanitarian_alarm_phone',
      visual_color: '#ff3b3b',
      maritime_domain: 'sar',
      humanitarian_case_type: 'distress',
      coordinate_source: 'media_ocr_text',
      radius_m: 1500,
      incident_lifecycle: 'needs_review',
    },
  }, null, 3600);
  assert.equal(event.properties.visual_category, 'humanitarian_alarm_phone');
  assert.equal(event.properties.visual_color, '#ff3b3b');
  assert.equal(event.properties.coordinate_source, 'media_ocr_text');
  assert.equal(event.properties.humanitarian_case_type, 'distress');
  assert.equal(event.properties.incident_id, 'ap-42');
});

test('exports the canonical lifecycle and precision vocabulary', () => {
  assert.deepEqual(INCIDENT_LIFECYCLES, ['active', 'resolved', 'needs_review', 'archived']);
  assert.deepEqual(LOCATION_PRECISIONS, [
    'unpositioned',
    'approximate',
    'regional_centroid',
    'reported_or_derived',
    'area',
    'area_low_confidence',
  ]);
});

test('rejects values outside the canonical Live vocabulary', async () => {
  const base = {
    type: 'distress_observation',
    source: 'alarm-phone',
    observed_at: '2026-08-02T12:00:00Z',
  };

  await assert.rejects(
    normalizeEvent({ ...base, confidence: 1.2 }),
    /confidence must be between 0 and 1/,
  );
  await assert.rejects(
    normalizeEvent({ ...base, properties: { incident_lifecycle: 'closed' } }),
    /invalid incident_lifecycle/,
  );
  await assert.rejects(
    normalizeEvent({ ...base, geometry: { type: 'LineString', coordinates: [] } }),
    /geometry type must be/,
  );
});

test('accepts a correctly signed collector request', async () => {
  const secret = 'test-secret';
  const body = JSON.stringify({ type: 'source_health', source: 'gdacs', observed_at: '2026-08-02T12:00:00Z' });
  const request = new Request('https://edge.example/v1/live/events', {
    method: 'POST',
    headers: { 'X-SeaCommons-Signature': await hmac(secret, body) },
    body,
  });
  const result = await verifyIngestRequest(request, secret);
  assert.equal(result.ok, true);
  assert.equal(result.event.source, 'gdacs');
});

test('rejects a collector request with an invalid signature', async () => {
  const request = new Request('https://edge.example/v1/live/events', {
    method: 'POST',
    headers: { 'X-SeaCommons-Signature': 'bad' },
    body: '{}',
  });
  const result = await verifyIngestRequest(request, 'test-secret');
  assert.equal(result.ok, false);
  assert.equal(result.status, 401);
});

test('separates collector heartbeat health from event recency', () => {
  const now = Date.parse('2026-08-24T12:00:00Z');
  assert.equal(classifyLiveStatus({
    lastHeartbeatAt: '2026-08-24T11:59:20Z',
    eventCount: 0,
  }, now), 'live');
  assert.equal(classifyLiveStatus({
    lastHeartbeatAt: '2026-08-24T11:40:00Z',
    eventCount: 4,
  }, now), 'degraded');
  assert.equal(classifyLiveStatus({}, now), 'waiting');
});

test('duplicate delivery is idempotent and broadcasts only once', async () => {
  const state = new FakeState();
  const socket = new FakeSocket();
  state.sockets.push(socket);
  const room = new LiveRoom(state, { INGEST_SECRET: 'test-secret' });
  const event = incidentEvent({ id: 'incident-1:v1' });

  const first = await ingest(room, event);
  const duplicate = await ingest(room, event);
  const snapshot = await room.loadSnapshot();

  assert.equal(first.response.status, 202);
  assert.equal(duplicate.response.status, 200);
  assert.equal(duplicate.body.duplicate, true);
  assert.deepEqual(snapshot.events.map((entry) => entry.id), ['incident-1:v1']);
  assert.equal(socket.messages.length, 1);
});

test('an out-of-order observation cannot replace a newer incident version', async () => {
  const state = new FakeState();
  const room = new LiveRoom(state, { INGEST_SECRET: 'test-secret' });
  const newer = incidentEvent({
    id: 'incident-1:v2',
    observedAt: '2099-08-26T10:10:00Z',
  });
  const older = incidentEvent({
    id: 'incident-1:v1',
    observedAt: '2099-08-26T10:00:00Z',
  });

  const accepted = await ingest(room, newer);
  const headHash = accepted.body.hash;
  const stale = await ingest(room, older);
  const snapshot = await room.loadSnapshot();

  assert.equal(stale.response.status, 200);
  assert.equal(stale.body.stale, true);
  assert.deepEqual(snapshot.events.map((entry) => entry.id), ['incident-1:v2']);
  assert.equal(snapshot.head_hash, headHash);
});

test('restart retains the removal tombstone and reconnect snapshot', async () => {
  const storage = new MemoryStorage();
  const firstState = new FakeState(storage);
  const firstRoom = new LiveRoom(firstState, { INGEST_SECRET: 'test-secret' });
  await ingest(firstRoom, incidentEvent({ id: 'incident-1:active' }));
  const removal = await ingest(firstRoom, incidentEvent({
    id: 'incident-1:resolved',
    removed: true,
  }));

  const restartedState = new FakeState(storage);
  const restartedRoom = new LiveRoom(restartedState, { INGEST_SECRET: 'test-secret' });
  const delayedActive = await ingest(restartedRoom, incidentEvent({
    id: 'incident-1:delayed-active',
  }));
  const snapshot = await restartedRoom.loadSnapshot();

  assert.equal(delayedActive.body.stale, true);
  assert.deepEqual(snapshot.events, []);
  assert.equal(snapshot.head_hash, removal.body.hash);

  const OriginalResponse = globalThis.Response;
  const OriginalWebSocketPair = globalThis.WebSocketPair;
  class FakeWebSocketPair {
    constructor() {
      this.client = new FakeSocket();
      this.server = new FakeSocket();
    }
  }
  class SwitchingProtocolsResponse {
    constructor(body, options) {
      this.body = body;
      Object.assign(this, options);
    }
  }
  globalThis.Response = SwitchingProtocolsResponse;
  globalThis.WebSocketPair = FakeWebSocketPair;
  try {
    const response = await restartedRoom.stream({
      headers: new Headers({ Upgrade: 'websocket' }),
    });
    const reconnectMessage = JSON.parse(restartedState.sockets[0].messages[0]);
    assert.equal(response.status, 101);
    assert.equal(reconnectMessage.type, 'snapshot');
    assert.deepEqual(reconnectMessage.events, []);
    assert.equal(reconnectMessage.head_hash, removal.body.hash);
  } finally {
    globalThis.Response = OriginalResponse;
    if (OriginalWebSocketPair === undefined) delete globalThis.WebSocketPair;
    else globalThis.WebSocketPair = OriginalWebSocketPair;
  }
});
