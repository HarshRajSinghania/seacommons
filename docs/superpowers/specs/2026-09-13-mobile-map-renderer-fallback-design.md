# Mobile Map Renderer Fallback Design

## Goal
Guarantee that public Live and Play always render an interactive map with the same public cases and points on mobile, including devices where MapLibre 6 cannot initialize WebGL2.

## Constraints
- Keep MapLibre 6 as the primary renderer where WebGL2 works.
- Do not downgrade to MapLibre 5.
- The fallback must not alter ingestion, lifecycle, publication policy, classification, or archive semantics.
- Live and Play must consume the same canonical feature data they already use.
- The fallback must provide basemap, pan, zoom, incident markers, tap/click selection, fit-to-visible-cases, and report/dossier opening.
- No fake coordinates: records without geometry remain in feed/archive lists but are not placed on the map.

## Architecture
Introduce a shared renderer boundary in `apps/web/src/features/map/`. It attempts MapLibre first. If MapLibre construction or initial load fails with a GPU/WebGL initialization failure, it mounts a Leaflet renderer into the same map container.

Leaflet is a visual fallback only. A small adapter exposes the operations needed by public Live/Play: `setFeatures`, `fitFeatures`, `flyTo`, `resize`, and `destroy`. Marker click callbacks continue to call the existing Live report and Play dossier selection handlers.

The fallback uses the same shared Esri raster basemap already deployed. Marker appearance is derived from the existing canonical domain/category properties. Geometry uncertainty remains represented in report metadata; the fallback does not invent or tighten precision.

## Failure behavior
A MapLibre GPU initialization failure must not produce the current “Map unavailable” terminal state. Instead the app switches renderer and marks the map usable after Leaflet is ready. Only if both renderers fail should the existing map-error banner appear, while feed/archive data remains usable.

## Verification
Tests must force MapLibre initialization failure and prove that fallback markers are rendered from canonical public features, marker selection opens the existing report/dossier path, and non-geolocated records are omitted from the map only. Existing desktop MapLibre behavior must remain unchanged.
