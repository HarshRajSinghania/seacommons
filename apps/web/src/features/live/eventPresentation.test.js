import test from 'node:test';
import assert from 'node:assert/strict';

import { caseEvidenceLabel, locationLabel, relativeTime } from './eventPresentation.js';

const NOW = Date.parse('2026-09-01T12:00:00Z');

test('relativeTime never goes negative on clock skew', () => {
  assert.equal(relativeTime('2026-09-01T12:05:00Z', NOW), 'just now');
});

test('relativeTime buckets minutes, hours, days', () => {
  assert.equal(relativeTime('2026-09-01T11:18:00Z', NOW), '42 min ago');
  assert.equal(relativeTime('2026-09-01T09:00:00Z', NOW), '3 h ago');
  assert.equal(relativeTime('2026-08-30T12:00:00Z', NOW), '2 d ago');
  assert.equal(relativeTime(undefined, NOW), '');
});

test('case evidence label explains why a Live case exists instead of repeating transport', () => {
  assert.equal(
    caseEvidenceLabel({
      episode_family: 'gap_episode',
      offshore_reason_codes: [
        'OFFSHORE_CONTEXT',
        'LOCAL_AIS_COVERAGE_HEALTHY',
        'PROLONGED_OFFSHORE_GAP',
      ],
    }),
    'prolonged offshore gap · local AIS coverage healthy',
  );
  assert.equal(
    caseEvidenceLabel({
      episode_family: 'spoofing_episode',
      reason_codes: [
        'SUSTAINED_POSITION_RELOCATION',
        'HIGH_CONFIDENCE_POSITION_INTEGRITY_ANOMALY',
      ],
    }),
    'sustained position relocation · high-confidence position anomaly',
  );
  assert.equal(
    caseEvidenceLabel({
      episode_family: 'safety_episode',
      reason_codes: ['PERSISTENT_AIS_REPORTED_SAFETY_STATE'],
    }),
    'persistent AIS NUC state',
  );
});

test('case evidence label has a useful family fallback for older dossiers', () => {
  assert.equal(caseEvidenceLabel({ episode_family: 'gap_episode' }), 'offshore AIS gap dossier');
});

test('a real point shows coordinates and uncertainty', () => {
  const label = locationLabel({ location_uncertainty_m: 1200 }, [11.9423, 34.2715]);
  assert.equal(label.tone, 'ok');
  assert.match(label.text, /34\.2715, 11\.9423/);
  assert.match(label.text, /±1\.2 km/);
});

test('disputed OCR is flagged for review, with or without a point', () => {
  assert.equal(
    locationLabel({ coordinate_review_status: 'machine_ocr_disputed_needs_review' }, null).text,
    'OCR DISPUTED · REVIEW REQUIRED',
  );
  assert.equal(
    locationLabel({ coordinate_review_status: 'machine_ocr_disputed_needs_review' }, [1, 2]).tone,
    'review',
  );
});

test('a land humanitarian case never shows a maritime point', () => {
  assert.equal(
    locationLabel({ humanitarian_case_type: 'land_humanitarian' }, null).text,
    'LOCATION WITHHELD',
  );
});

test('missing coordinate reads as a reason, not "position unavailable"', () => {
  assert.equal(locationLabel({ media_transport: 'x_media_ocr' }, null).text, 'OCR PROCESSING');
  assert.equal(locationLabel({ coordinate_source: 'region_area' }, null).text, 'REGION ONLY');
  assert.equal(locationLabel({}, null).text, 'POSITION NOT EXTRACTED');
});
