export const PUBLIC_LIVE_WINDOW_DAYS = 1;

export function filterPublicLiveWindow(features = [], nowMs = Date.now()) {
  const cutoff = new Date(
    nowMs - PUBLIC_LIVE_WINDOW_DAYS * 24 * 60 * 60 * 1000,
  ).toISOString();
  return (features || []).filter(
    (event) => (event?.properties?.timestamp_utc || '') >= cutoff,
  );
}
