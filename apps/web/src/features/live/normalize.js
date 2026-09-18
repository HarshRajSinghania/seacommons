const BLOCKED_PUBLIC_TRANSPORTS = Object.freeze([
  'nitter',
  'twscrape',
  'scrape',
  'unofficial',
]);

function isRecord(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function isGeoGeometry(value) {
  if (value === null) return true;
  if (!isRecord(value) || typeof value.type !== 'string') return false;
  return Array.isArray(value.coordinates)
    || (value.type === 'GeometryCollection' && Array.isArray(value.geometries));
}

function finiteNumber(value) {
  if (value === null || value === '') return null;
  const normalized = Number(value);
  return Number.isFinite(normalized) ? normalized : null;
}

/** Normalize the public edge contract into the same GeoJSON shape as the VM feed. */
export function edgeEventToFeature(event) {
  if (!isRecord(event)
    || typeof event.id !== 'string'
    || typeof event.type !== 'string'
    || typeof event.source !== 'string'
    || typeof event.observed_at !== 'string'
    || !isGeoGeometry(event.geometry ?? null)) {
    return null;
  }
  const props = isRecord(event.properties) ? event.properties : {};
  const lifecycleState = ['active', 'resolved', 'archived', 'needs_review']
    .includes(props.incident_lifecycle)
    ? props.incident_lifecycle
    : 'active';
  const incidentId = typeof props.incident_id === 'string' && props.incident_id
    ? props.incident_id
    : event.id;
  const radius = finiteNumber(props.radius_m);
  const locationPrecision = typeof props.location_precision === 'string'
    ? props.location_precision
    : radius !== null && radius > 20000 ? 'area' : 'reported_or_derived';
  const repostCount = finiteNumber(props.repost_count);
  const featureId = `intel:${incidentId}`;
  return {
    type: 'Feature',
    id: featureId,
    geometry: event.geometry ?? null,
    properties: {
      schema: 'org.seacommons.live-signal/v1',
      id: featureId,
      type: event.type === 'distress_observation' ? 'twitter' : event.type,
      tier: 'operational',
      kind: lifecycleState === 'active' ? 'distress' : lifecycleState,
      incident_lifecycle: lifecycleState,
      severity: typeof props.severity === 'string' ? props.severity : 'low',
      verification_status: typeof props.verification_status === 'string'
        ? props.verification_status
        : 'unverified_public_source',
      title: typeof props.title === 'string' ? props.title : 'Maritime signal',
      text: typeof props.text === 'string' ? props.text : '',
      url: typeof event.source_url === 'string' ? event.source_url : '',
      source: event.source || 'public feed',
      timestamp_utc: event.observed_at,
      source_timestamp_utc: event.observed_at,
      received_at: typeof event.received_at === 'string' ? event.received_at : event.observed_at,
      location_precision: locationPrecision,
      ...(radius !== null ? { location_uncertainty_m: radius } : {}),
      // Canonical semantic category (colour is a pure function of this, never
      // severity). Carried across the edge transport so live.seacommons.org
      // and the VM feed classify a signal identically.
      ...(typeof props.visual_category === 'string'
        ? { visual_category: props.visual_category } : {}),
      ...(typeof props.visual_color === 'string'
        ? { visual_color: props.visual_color } : {}),
      ...(typeof props.category_label === 'string'
        ? { category_label: props.category_label } : {}),
      ...(typeof props.maritime_domain === 'string'
        ? { maritime_domain: props.maritime_domain } : {}),
      ...(typeof props.humanitarian_case_type === 'string'
        ? { humanitarian_case_type: props.humanitarian_case_type } : {}),
      ...(typeof props.location_status === 'string'
        ? { location_status: props.location_status } : {}),
      ...(typeof props.coordinate_source === 'string'
        ? { coordinate_source: props.coordinate_source }
        : {}),
      ...(repostCount !== null
        ? { repost_count: repostCount }
        : {}),
      ...(Array.isArray(props.thread_reposts) ? { thread_reposts: props.thread_reposts } : {}),
      ...(typeof props.area_weather_narrowed === 'boolean'
        ? { area_weather_narrowed: props.area_weather_narrowed }
        : {}),
    },
  };
}

export function edgeSnapshotToFeatures(snapshot) {
  if (!isRecord(snapshot) || !Array.isArray(snapshot.events)) return [];
  return snapshot.events.map(edgeEventToFeature).filter(Boolean);
}

/** Enforce the browser-side defense-in-depth filter for VM public features. */
export function receivedSignalFeatures(features) {
  if (!Array.isArray(features)) return [];
  return features.filter((feature) => {
    if (!isRecord(feature) || feature.type !== 'Feature' || !isRecord(feature.properties)) {
      return false;
    }
    const properties = feature.properties;
    const policy = String(properties.source_policy || '').toLowerCase();
    const transport = String(properties.via || properties.scrape_source || '').toLowerCase();
    return !BLOCKED_PUBLIC_TRANSPORTS.some(
      (blocked) => policy === blocked || transport.includes(blocked),
    )
      && properties.type !== 'sar_model'
      && properties.title !== 'Computed SAR drift product'
      && properties.source !== 'SeaCommons engine';
  });
}

/** Replace the rendered drift for one Intel event without duplicating stale versions. */
export function mergeIntelDriftUpdate(collection, message) {
  if (!isRecord(message)
    || typeof message.id !== 'string'
    || !isRecord(message.drift)
    || !isRecord(message.drift.trajectory)) {
    return collection;
  }
  const previousFeatures = Array.isArray(collection?.features) ? collection.features : [];
  const keep = previousFeatures.filter(
    (feature) => feature?.properties?.intel_event_id !== message.id,
  );
  const drift = message.drift;
  const newFeatures = [drift.trajectory, drift.cone_24h]
    .filter(isRecord)
    .map((feature) => ({
      ...feature,
      properties: {
        ...(isRecord(feature.properties) ? feature.properties : {}),
        intel_event_id: message.id,
        intel_title: drift.title,
        // Drift colour inherits its origin signal's category, never a severity.
        origin_category: drift.origin_category ?? drift.visual_category,
        visual_category: drift.visual_category ?? drift.origin_category,
        visual_color: drift.visual_color,
        category_label: drift.category_label,
        intel_source: drift.source,
        auto_drift: true,
      },
    }));
  if (isRecord(drift.impact_point) && Array.isArray(drift.impact_point.features)) {
    for (const feature of drift.impact_point.features.filter(isRecord)) {
      newFeatures.push({
        ...feature,
        properties: {
          ...(isRecord(feature.properties) ? feature.properties : {}),
          intel_event_id: message.id,
          auto_drift: true,
        },
      });
    }
  }
  return { type: 'FeatureCollection', features: [...keep, ...newFeatures] };
}
