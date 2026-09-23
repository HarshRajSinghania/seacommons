const STATIONARY_KN = 0.5;

export function normalizeVesselName(value, mmsi = '') {
  const text = String(value || '').replace(/\s+/g, ' ').trim();
  if (text && text !== String(mmsi || '').trim()) return text.toUpperCase();
  return mmsi ? `MMSI ${mmsi}` : 'VESSEL';
}

export function vesselMotionState(properties = {}) {
  const raw = properties.speed ?? properties.sog ?? properties.latest_sog;
  if (raw == null || raw === '') return 'unknown';
  const speed = Number(raw);
  if (!Number.isFinite(speed)) return 'unknown';
  return speed <= STATIONARY_KN ? 'stationary' : 'moving';
}

export function vesselReportFeature(feature) {
  const p = feature?.properties || {};
  const mmsi = String(p.mmsi || p.vessel_id || '');
  const displayName = normalizeVesselName(p.display_name || p.ship_name || p.name, mmsi);
  const sanctionLists = Array.isArray(p.sanctions_lists) ? p.sanctions_lists : [];
  const sanctionPrograms = Array.isArray(p.sanctions_programs) ? p.sanctions_programs : [];
  const sanctions = Array.isArray(p.sanctions) && p.sanctions.length
    ? p.sanctions
    : sanctionLists.map((list, index) => ({
      list,
      program: sanctionPrograms[index] || sanctionPrograms[0] || '',
      reason: 'Public sanctions-list identity match',
    }));
  return {
    ...feature,
    properties: {
      ...p,
      ...(sanctions.length ? { sanctions } : {}),
      id: p.id || `vessel:${mmsi}`,
      title: displayName,
      display_name: displayName,
      vessel_name: displayName,
      entity_kind: 'vessel',
      report_type: 'vessel',
      report_id: mmsi,
      maritime_domain: 'safety',
      type: p.type || 'ais_vessel',
      latest_sog: p.speed ?? p.sog ?? null,
      latest_course: p.course ?? p.cog ?? null,
      latest_heading: p.heading ?? null,
      latest_nav_status: p.nav_status ?? null,
      motion_state: p.motion_state || vesselMotionState(p),
    },
  };
}
