# Mobile Map Renderer Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure public Live and Play always provide an interactive case map on mobile even when MapLibre 6 cannot initialize WebGL2.

**Architecture:** Keep MapLibre as primary renderer. Add a shared Leaflet fallback adapter that consumes the same canonical public GeoJSON/features and preserves existing selection/report flows.

**Tech Stack:** React, MapLibre GL 6.5+, Leaflet 1.9+, Vite, Node test runner, Playwright.

**Spec:** `docs/superpowers/specs/2026-09-13-mobile-map-renderer-fallback-design.md`

## Global Constraints
- MapLibre 6 remains primary; no downgrade.
- Fallback is visual only and cannot change publication/lifecycle semantics.
- No coordinates are invented for geometry-less records.
- Existing Esri public basemap is reused.
- Mobile success means map + markers + pan/zoom + marker selection, not merely a non-error screen.

---
### Task 1: Shared Leaflet fallback adapter

**Files:**
- Create: `apps/web/src/features/map/fallbackRenderer.js`
- Create: `apps/web/src/features/map/fallbackRenderer.test.js`
- Modify: `apps/web/package.json`
- Modify: `apps/web/package-lock.json`

**Interfaces:**
- Produces `createFallbackMap({ container, center, zoom, onFeatureSelect })`.
- Returned adapter exposes `setFeatures(features)`, `fitFeatures(features)`, `flyTo({ center, zoom })`, `resize()`, and `destroy()`.

- [ ] Write failing tests proving geometry-less features are skipped and point markers invoke `onFeatureSelect` with the canonical feature.
- [ ] Run `node --test src/features/map/fallbackRenderer.test.js` and verify RED.
- [ ] Add Leaflet dependency and implement the minimal adapter with Esri raster tiles.
- [ ] Re-run the test and verify GREEN.
- [ ] Commit `feat: add mobile fallback map renderer`.

### Task 2: Live renderer failover

**Files:**
- Modify: `apps/web/src/main.jsx`
- Modify: `apps/web/src/features/live/mobileResilience.test.js`

**Interfaces:**
- Consumes `createFallbackMap`.
- Existing `openIntelReport(feature)` remains the selection callback.

- [ ] Add a failing regression that the MapLibre init catch mounts fallback instead of ending in map-error state.
- [ ] Run the focused Live tests and verify RED.
- [ ] Implement failover and synchronize canonical Live features into the active renderer.
- [ ] Verify marker selection still opens the existing Live report.
- [ ] Run `npm run test:live` and verify GREEN.
- [ ] Commit `fix: keep live map interactive without webgl`.
### Task 3: Play renderer failover

**Files:**
- Modify: `apps/web/src/features/play/PlayTimeline.jsx`
- Modify: `apps/web/src/features/play/timeline.test.js`

**Interfaces:**
- Consumes `createFallbackMap`.
- Existing `setSelectedId(incident_id)` remains the dossier selection path.

- [ ] Add a failing regression for Play failover with archive point markers.
- [ ] Run `npm run test:play` and verify RED.
- [ ] Implement fallback synchronization from `filteredIncidents` and preserve mobile archive/dossier controls.
- [ ] Verify geometry-less archive records stay in the list but not on the map.
- [ ] Run `npm run test:play` and verify GREEN.
- [ ] Commit `fix: keep play map interactive without webgl`.

### Task 4: Mobile browser qualification and release

**Files:**
- Modify: `apps/web/e2e/public-live.spec.js`
- Modify: `apps/web/e2e/public-play.spec.js`

- [ ] Add E2E coverage that forces WebGL2 absence/GPU initialization failure and verifies a fallback basemap plus at least one canonical case marker on Live and Play.
- [ ] Run focused E2E and verify RED before final implementation adjustments, then GREEN.
- [ ] Run `npm test && npm run lint && npm run build && npm audit --omit=dev --audit-level=high`.
- [ ] Push branch, open PR, require Full CI + CodeQL + Chromium E2E green.
- [ ] Merge exact head, wait for Vercel production READY, run production browser smoke, and verify Live/Play production bundles and map tile reachability.
