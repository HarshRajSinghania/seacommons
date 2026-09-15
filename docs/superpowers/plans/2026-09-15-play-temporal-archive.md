# Play Temporal Archive Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make public Live case-first and turn Play into a continuous dated archive that exposes existing drift and satellite evidence against the correct historical map date.

**Architecture:** Live remains a 24h classified-case surface with no raw AIS layer. Play consumes the public incident catalog, augments each case with drift/satellite counts, groups the archive by calendar time, and synchronizes its global imagery layer with the selected historical cutoff while preserving exact per-case Sentinel/VIIRS evidence overlays.

**Tech Stack:** FastAPI, SQLAlchemy, React, MapLibre GL, NASA GIBS, Copernicus Data Space assets.

**Spec:** User requirements from 2026-09-15 SeaCommons map/archive review.

## Global Constraints

- Public Live shows classified Humanitarian and Maritime cases, not the raw AIS fleet.
- Play is historical and date-aware; future evidence must never leak before the selected cutoff.
- Existing publication/privacy policy remains authoritative for catalog membership.
- Satellite overlays must keep provider and acquisition provenance visible.
- Drift remains modelled evidence, visibly distinct from observed positions.

---

### Task 1: Catalog evidence summary
**Files:** Modify `apps/api/core/api/routes/play.py`; test `apps/api/tests/test_play_routes.py` or nearest existing Play route tests.
**Interfaces:** Produces `evidence_counts: {drift, satellite}` per catalog incident.
- [ ] Add failing route/catalog test for drift and satellite counts.
- [ ] Batch-query completed drift rows and satellite observations for catalog IDs.
- [ ] Attach counts without N+1 queries.
- [ ] Run Play API tests and commit.

### Task 2: Archive continuity helpers
**Files:** Modify `apps/web/src/features/play/timeline.js`; test `timeline.test.js`.
**Interfaces:** Produces `archiveCoverage(incidents)`, `groupArchiveByMonth(incidents)`, `satelliteContextTileUrl(day, mission)`.
- [ ] Add failing tests for range, covered/missing months, grouping, and dated imagery URL.
- [ ] Implement deterministic UTC helpers.
- [ ] Run timeline tests and commit.

### Task 3: Date-synchronised Play map
**Files:** Modify `apps/web/src/features/play/PlayTimeline.jsx`, `timeline.js`.
**Interfaces:** Consumes historical cutoff or selected incident date; updates `satelliteContext` tiles accordingly.
- [ ] Add failing UI/source-contract test for temporal imagery updates.
- [ ] Update NASA GIBS layer when cutoff date changes.
- [ ] Keep selected per-case Copernicus/VIIRS asset above the global dated context.
- [ ] Run Play tests and commit.

### Task 4: Continuous archive UI
**Files:** Modify `PlayTimeline.jsx`, `play.css`.
**Interfaces:** Uses archive groups and evidence counts.
- [ ] Add failing UI test for date range/month headings/evidence badges.
- [ ] Render archive range, covered-month continuity and month sections.
- [ ] Render DRIFT/SAT badges per case where counts are non-zero.
- [ ] Show exact timeline start/end dates around the slider.
- [ ] Run Play tests, lint, build, commit.

### Task 5: Historical drift candidate audit
**Files:** Create `apps/api/core/intel/backfill_historical_drift.py`; tests under existing intel backfill tests.
**Interfaces:** Dry-run `run(apply=False, limit=...)` returns eligible/no-drift counts and never writes by default.
- [ ] Define public/humanitarian historical eligibility from persisted incident + real point + no completed drift.
- [ ] Add deterministic dry-run tests.
- [ ] Implement candidate discovery only; do not auto-run model jobs in production.
- [ ] Run tests and commit.

### Task 6: Verification and integration
- [ ] Run targeted API tests, Play tests, Live tests, map tests, lint, typecheck and production build.
- [ ] Verify production Play counts and a case timeline with drift/satellite evidence.
- [ ] Fast-forward `main`, push, verify Vercel production SHA and public endpoints.
