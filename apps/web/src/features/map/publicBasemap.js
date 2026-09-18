export const PUBLIC_BASEMAP_TILE_URL = 'https://tile.openstreetmap.org/{z}/{x}/{y}.png';
export const PUBLIC_BASEMAP_LABEL_URL = '';
export const PUBLIC_SEAMARK_TILE_URL = 'https://tiles.openseamap.org/seamark/{z}/{x}/{y}.png';

export function publicBasemapSource() {
  return {
    type: 'raster',
    tiles: [PUBLIC_BASEMAP_TILE_URL],
    tileSize: 256,
    maxzoom: 19,
    attribution: '© OpenStreetMap contributors',
  };
}
