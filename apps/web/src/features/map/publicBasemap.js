export const PUBLIC_BASEMAP_TILE_URL = 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}';

export function publicBasemapSource() {
  return {
    type: 'raster',
    tiles: [PUBLIC_BASEMAP_TILE_URL],
    tileSize: 256,
    maxzoom: 19,
    attribution: 'Esri, HERE, Garmin, FAO, NOAA, USGS, OpenStreetMap contributors, and the GIS User Community',
  };
}
