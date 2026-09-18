export const DEFAULT_LAYER_STORAGE_KEY = 'seacommons_layer_vis';
export const PUBLIC_LIVE_LAYER_STORAGE_KEY = 'seacommons_live_layer_vis_v3';

const BASE_DEFAULTS = Object.freeze({
  vessels: true,
  ais_moving: true,
  ais_stationary: true,
  ais_trails: true,
  radio_receivers: true,
  radio_dsc: true,
  ngo_vessels: true,
  weather: true,
  sar: true,
  fused: true,
  spikes: true,
  platforms: true,
  alerts: true,
});

export function layerVisibilityStorageKey(publicLive = false) {
  return publicLive ? PUBLIC_LIVE_LAYER_STORAGE_KEY : DEFAULT_LAYER_STORAGE_KEY;
}
export function initialLayerVisibility({ publicLive = false, storage = globalThis.localStorage } = {}) {
  const defaults = {
    ...BASE_DEFAULTS,
    ...(publicLive ? {
      ais_moving: false,
      ais_stationary: false,
      ais_trails: false,
    } : {}),
  };
  try {
    const key = layerVisibilityStorageKey(publicLive);
    const saved = JSON.parse(storage?.getItem?.(key) || '{}');
    const merged = { ...defaults, ...saved };
    if (publicLive) {
      // Public Live is case-first. Raw AIS is never restored from browser cache:
      // vessels belong in the console, while live.seacommons.org maps classified cases.
      merged.ais_moving = false;
      merged.ais_stationary = false;
      merged.ais_trails = false;
      merged.ngo_vessels = true;
      merged.nautical = true;
    }
    return merged;
  } catch {
    return defaults;
  }
}
