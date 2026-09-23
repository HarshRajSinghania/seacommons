// Shared signal taxonomy for the map, the legend and the OSINT panels.
// One place so the map colours, the legend rows and the dashboard icons never
// drift apart. `distress` (SAR), `correlated_alert` and the `ais_*` types each
// render on their own dedicated map layer and are listed here only for the
// legend / icon lookups.

export const SIGNAL_CATEGORIES = [
  { key: 'distress',   label: 'Active distress',   color: '#ff3b3b', types: ['distress'],
    description: 'A distress beacon (AIS-SART/MOB/EPIRB) or a corroborated SAR report. Auto-published — treat as real until stood down.' },
  { key: 'fused',      label: 'Correlated alert',  color: '#ffb347', types: ['correlated_alert'],
    description: 'SeaCommons groups related evidence into one analytical episode. Check verification and source lineage to see whether independent corroboration is established.' },
  { key: 'ais',        label: 'AIS anomaly / spike', color: '#60a5fa', types: ['ais_spike', 'ais_anomaly'],
    description: 'A transponder pattern that does not look like normal navigation — circular track, teleport jump, or a signal frozen in place. Flags identity questions, not identity conclusions.' },
  { key: 'incident',   label: 'Vessel incident',   color: '#fb923c', types: ['vessel_incident'],
    description: 'A vessel\'s own AIS navigational-status report: aground, or "not under command" (cannot manoeuvre). Not under command is frequently set for benign reasons — always needs operator review before acting on it.' },
  { key: 'hazard',     label: 'Natural hazard (GDACS)', color: '#f59e0b', types: ['gdacs'],
    description: 'Environmental/weather hazard from the GDACS global disaster feed — context, not a vessel-specific signal.' },
  { key: 'iom',        label: 'IOM missing migrants', color: '#b91c1c', types: ['iom_incident'],
    description: 'A recorded incident from IOM\'s Missing Migrants project — historical/aggregate reporting, not a live position.' },
  { key: 'social',     label: 'Social post',       color: '#818cf8', types: ['twitter', 'mastodon', 'bluesky'],
    description: 'An unverified public social-media report. Lowest-confidence source class until corroborated by another channel.' },
  { key: 'news',       label: 'News / RSS',        color: '#94a3b8', types: ['news'],
    description: 'A published news article matched to a location/event — secondary reporting, not a primary observation.' },
  { key: 'ngo',        label: 'NGO activity',      color: '#4ade80', types: ['ngo_activity'],
    description: 'A public status update from an NGO SAR vessel or operation.' },
  // docs/fixes.md M0.4: not a public semantic category -- a real signal
  // that doesn't match a known type, fails closed here rather than
  // inventing a category. Legend.jsx already excludes this key from
  // display; kept internally for counts/grouping only.
  { key: 'other',      label: 'Unclassified',      color: '#8bf0c5', types: [],
    description: 'Does not match a known category yet.' },
];

// Operational presentation taxonomy for grouped vessel episodes. Unlike the
// broad maritime domain, this describes what the signal actually says. It is
// shared by the map triangle, compact log, report header and legend.
// Canonical semantic visual taxonomy. Mirror of
// apps/api/core/domain/visual_category.py — colour is a pure function of
// semantic CATEGORY, never severity / OCR confidence / lifecycle.
export const EVENT_VISUAL_CATEGORIES = [
  { key: 'humanitarian_alarm_phone', label: 'Alarm Phone (humanitarian)', color: '#ff3b3b' },
  { key: 'civil_sar', label: 'Civil SAR / NGO', color: '#4ade80' },
  { key: 'state_sar', label: 'State SAR / Coast Guard', color: '#38bdf8' },
  { key: 'distress', label: 'Maritime distress', color: '#ff3b3b' },
  { key: 'navigation_casualty', label: 'Unable to manoeuvre / aground', color: '#38bdf8' },
  { key: 'spoofing', label: 'Position integrity', color: '#c084fc' },
  { key: 'ais_gap', label: 'AIS reporting gap / dark candidate', color: '#fb923c' },
  { key: 'loitering', label: 'Loitering / abnormal dwell', color: '#facc15' },
  { key: 'rendezvous', label: 'Rendezvous / ship-to-ship', color: '#f97316' },
  { key: 'sanctions', label: 'Sanctions match', color: '#f472b6' },
  { key: 'infrastructure', label: 'Infrastructure proximity', color: '#22d3ee' },
  { key: 'identity', label: 'Identity / flag anomaly', color: '#60a5fa' },
  { key: 'piracy', label: 'Piracy / security incident', color: '#a78bfa' },
  { key: 'environmental', label: 'Environmental hazard', color: '#34d399' },
  { key: 'needs_review', label: 'Needs operator review', color: '#f59e0b' },
  { key: 'resolved', label: 'Resolved', color: '#22c55e' },
  { key: 'archived', label: 'Archived', color: '#9aa0ab' },
  { key: 'context', label: 'Maritime context', color: '#8bf0c5' },
];

const _VISUAL_BY_KEY = EVENT_VISUAL_CATEGORIES.reduce((acc, category) => {
  acc[category.key] = category;
  return acc;
}, {});

function eventTokens(properties = {}) {
  const anomalyTypes = Array.isArray(properties.anomaly_types) ? properties.anomaly_types : [];
  return [
    ...anomalyTypes,
    properties.anomaly_type,
    properties.ais_nav_status_kind,
    properties.alert_type,
    properties.type,
    properties.title,
    properties.text,
    properties.detection_reason,
    properties.detail,
  ].filter(Boolean).join(' ').toLowerCase().replace(/[\s-]+/g, '_');
}

/**
 * Semantic visual category for a signal / vessel episode / drift feature.
 *
 * CATEGORY determines visual identity. It is NEVER inferred from `severity`
 * or a LOW/MEDIUM/HIGH/CRITICAL rating, and lifecycle never changes the
 * category (a resolved Alarm Phone is still red — lifecycle is communicated
 * separately through outline / opacity / badge).
 */
export function classifyEventVisual(properties = {}) {
  // Alarm Phone is red because of its category/source role: maritime point,
  // land point, region area, drift origin and drift trajectory/cone alike.
  if (isAlarmPhoneSource(properties.source)
      || properties.origin_category === 'humanitarian_alarm_phone'
      || properties.visual_category === 'humanitarian_alarm_phone') {
    return _VISUAL_BY_KEY.humanitarian_alarm_phone;
  }
  // Trust an explicit canonical category assigned by the backend contract.
  if (properties.visual_category && _VISUAL_BY_KEY[properties.visual_category]) {
    return _VISUAL_BY_KEY[properties.visual_category];
  }

  const tokens = eventTokens(properties);
  const navStatus = Number(properties.latest_nav_status);
  if (/circle_spoof|circular_spoof|spoofing|teleport|position_jump|impossible_speed|impossible_movement|gnss_manipulation/.test(tokens)) {
    return _VISUAL_BY_KEY.spoofing;
  }
  if (/ais_gap|dark_vessel|dark_activity|signal_gap|transponder_off|(^|_)gap($|_)/.test(tokens)) return _VISUAL_BY_KEY.ais_gap;
  if (/loiter|abnormal_dwell|stationary_anomaly/.test(tokens)) return _VISUAL_BY_KEY.loitering;
  if (/rendezvous|ship_to_ship|\bsts\b|proximity_pair/.test(tokens)) return _VISUAL_BY_KEY.rendezvous;
  if (properties.sanctions_matched || properties.maritime_domain === 'sanctions' || /sanction/.test(tokens)) return _VISUAL_BY_KEY.sanctions;
  if (properties.infrastructure || /pipeline|cable|infrastructure|platform_proximity/.test(tokens)) return _VISUAL_BY_KEY.infrastructure;
  if (/identity|flag_hopping|mmsi_mismatch|imo_mismatch|false_flag/.test(tokens)) return _VISUAL_BY_KEY.identity;
  if ([2, 3, 6].includes(navStatus)
      || /not_under_command|unable_to_man(?:oeu|eu)vre|restricted_man(?:oeu|eu)vrability|aground|engine_failure|mechanical_failure|disabled_vessel/.test(tokens)) {
    return _VISUAL_BY_KEY.navigation_casualty;
  }
  if (properties.maritime_domain === 'piracy' || /piracy|hijack|armed_robbery/.test(tokens)) return _VISUAL_BY_KEY.piracy;
  if (properties.maritime_domain === 'environmental' || /pollution|oil_spill|environmental/.test(tokens)) return _VISUAL_BY_KEY.environmental;

  // Red is reserved for Humanitarian distress. AIS safety self-reports
  // remain Maritime Safety even when their raw transport type is "distress".
  if (properties.maritime_domain === 'safety'
      && (properties.type === 'distress' || properties.ais_nav_status_kind === 'distress_beacon')) {
    return _VISUAL_BY_KEY.navigation_casualty;
  }

  // Humanitarian / SAR distress — red, by category, not severity.
  const humanitarianCaseType = String(properties.humanitarian_case_type || '').toLowerCase();
  if ((humanitarianCaseType && !['advocacy', 'unknown_humanitarian'].includes(humanitarianCaseType))
      || properties.kind === 'distress'
      || properties.type === 'distress'
      || (properties.maritime_domain === 'sar' && properties.is_distress)) {
    return _VISUAL_BY_KEY.distress;
  }
  return _VISUAL_BY_KEY.context;
}

export function eventAnomalyLabel(properties = {}) {
  const raw = (Array.isArray(properties.anomaly_types) && properties.anomaly_types[0])
    || properties.anomaly_type
    || properties.ais_nav_status_kind
    || properties.alert_type
    || properties.type
    || 'maritime signal';
  const labels = {
    circle_spoof: 'circular spoofing',
    circular_spoofing: 'circular spoofing',
    not_under_command: 'unable to manoeuvre',
    restricted_manoeuvrability: 'restricted manoeuvrability',
    restricted_maneuverability: 'restricted manoeuvrability',
    gap: 'AIS gap',
    ais_gap: 'AIS gap',
    rendezvous: 'ship-to-ship rendezvous',
  };
  return labels[raw] || String(raw).replace(/_/g, ' ');
}

// Categories that get their own toggleable map layer over the shared
// `intel-events` source (distress / fused / ais have dedicated sources).
export const INTEL_MAP_CATEGORIES = SIGNAL_CATEGORIES.filter(
  (c) => !['distress', 'fused', 'ais', 'other'].includes(c.key) && c.types.length,
);

const _BY_TYPE = SIGNAL_CATEGORIES.reduce((acc, cat) => {
  for (const t of cat.types) acc[t] = cat.key;
  return acc;
}, {});

const _COLOR_BY_KEY = SIGNAL_CATEGORIES.reduce((acc, cat) => {
  acc[cat.key] = cat.color;
  return acc;
}, {});

export function categoryOf(type) {
  return _BY_TYPE[type] || 'other';
}

export function signalCategoryOf(properties = {}) {
  // `properties.incident_type` must already be a member of the closed set
  // apps/api/core/domain/incident_taxonomy.py's STABLE_MARITIME_INCIDENT_TYPES
  // / STABLE_HUMANITARIAN_INCIDENT_TYPES defines (mirrored below by
  // SIGNALS_MACRO_GROUPS in main.jsx) -- the backend's own property test
  // enforces this. A raw, ungrouped `incident_type` here silently drops the
  // feature from the map (activeSignalCategories.has() below returns
  // false), which is exactly the production defect that module exists to
  // prevent going forward.
  const explicit = String(properties.incident_type || '').trim();
  const aliases = {
    incident: 'navigation_safety',
    hazard: 'environmental_hazard',
    rendezvous: 'transfer',
    sanctions: 'identity_integrity',
    identity: 'identity_integrity',
    social: 'public_observation',
    news: 'context_report',
    ngo: 'sar_activity',
    iom: 'migration_incident',
    ais: 'ais_gap',
    fused: 'maritime_context',
  };
  if (explicit) return aliases[explicit] || explicit;

  // Compatibility path for edge/archived records created before incident_type
  // became part of the public contract. Always translate the old visual/type
  // taxonomy into the new Humanitarian/Maritime incident-type vocabulary.
  const visual = classifyEventVisual(properties).key;
  const byVisual = {
    humanitarian_alarm_phone: 'distress',
    distress: 'distress',
    civil_sar: 'sar_activity',
    state_sar: 'sar_activity',
    navigation_casualty: 'navigation_safety',
    environmental: 'environmental_hazard',
    spoofing: 'position_integrity',
    ais_gap: 'ais_gap',
    loitering: 'loitering',
    rendezvous: 'transfer',
    sanctions: 'identity_integrity',
    infrastructure: 'infrastructure_proximity',
    identity: 'identity_integrity',
    piracy: 'piracy_security',
    context: 'maritime_context',
  };
  if (visual !== 'context' && byVisual[visual]) return byVisual[visual];
  return aliases[categoryOf(properties.type)] || 'maritime_context';
}

// Alarm Phone is a source, not a `type` -- its reports normalize to
// type=distress (or, pre-classification, type=twitter) alongside IOM/NGO/
// other operational sources. The Signals selector exposes it as its own
// toggle nested under Distress rather than folding it into the 'distress'
// SIGNAL_CATEGORIES entry.
export function isAlarmPhoneSource(source) {
  return /alarm.?phone/i.test(String(source || ''));
}

const _CAT_BY_KEY = SIGNAL_CATEGORIES.reduce((acc, cat) => {
  acc[cat.key] = cat;
  return acc;
}, {});

/** Human-readable definition + reliability caveat for a raw event `type`. */
export function descriptionOf(type) {
  return (_CAT_BY_KEY[categoryOf(type)] || _CAT_BY_KEY.other).description;
}

export function categoryColor(key) {
  return _COLOR_BY_KEY[key] || _COLOR_BY_KEY.other;
}

// MapLibre `match` expression: feature `type` -> category colour, for the
// generic intel-events circle layer.
export function categoryColorExpression() {
  const expr = ['match', ['get', 'type']];
  for (const cat of SIGNAL_CATEGORIES) {
    if (!cat.types.length) continue;
    expr.push(cat.types.length === 1 ? cat.types[0] : cat.types, cat.color);
  }
  expr.push(_COLOR_BY_KEY.other);
  return expr;
}
