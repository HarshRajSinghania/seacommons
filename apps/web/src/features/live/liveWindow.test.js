import assert from "node:assert/strict";
import test from "node:test";

import { PUBLIC_LIVE_WINDOW_DAYS, filterPublicLiveWindow } from "./liveWindow.js";

test("public Live is a one-day operational surface", () => {
  assert.equal(PUBLIC_LIVE_WINDOW_DAYS, 1);
  const now = Date.parse("2026-09-15T12:00:00Z");
  const fresh = { properties: { timestamp_utc: "2026-09-15T11:00:00Z" } };
  const stale = { properties: { timestamp_utc: "2026-09-13T11:00:00Z" } };
  assert.deepEqual(filterPublicLiveWindow([fresh, stale], now), [fresh]);
});
