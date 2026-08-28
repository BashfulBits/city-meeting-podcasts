import test from "node:test";
import assert from "node:assert/strict";
import {
  minInterRequestGapMs,
  availableTokenBudget,
  reservationFor,
  earliestSafeStart,
  routeHasCapacityFor,
  computeRouteLaneWait,
  paymentRequiredBackoffUntil,
  FULL_TOKEN_BUDGET_WINDOWS,
} from "../src/pacing.js";

const NOW = 1_700_000_000_000;

function freshRoute(overrides = {}) {
  return {
    route_id: "r1",
    rpm: 30,
    rpd: 500,
    tpm: 10000,
    rpm_window_start: 0,
    rpm_count: 0,
    rpd_window_start: 0,
    rpd_count: 0,
    tpm_window_start: 0,
    tpm_reserved: 0,
    full_token_budget: 10000 * FULL_TOKEN_BUDGET_WINDOWS,
    token_budget_updated_at: NOW,
    throttle_streak: 0,
    blocked_until: null,
    buffer_seconds: 0,
    ...overrides,
  };
}

function job(overrides = {}) {
  return { input_token_estimate: 500, max_output_token_estimate: 200, ...overrides };
}

test("minInterRequestGapMs paces evenly to the RPM limit", () => {
  assert.equal(minInterRequestGapMs({ rpm: 30 }), 2000); // 60000/30
  assert.equal(minInterRequestGapMs({ rpm: 60 }), 1000);
  assert.equal(minInterRequestGapMs({ rpm: 0 }), 0);
  assert.equal(minInterRequestGapMs({}), 0);
});

test("availableTokenBudget refills linearly up to the FULL_TOKEN_BUDGET_WINDOWS cap", () => {
  const route = freshRoute({ full_token_budget: 0, token_budget_updated_at: NOW, tpm: 6000 });
  assert.equal(availableTokenBudget(route, NOW), 0);
  assert.equal(availableTokenBudget(route, NOW + 30_000), 3000); // half a minute -> half of tpm
  const cap = 6000 * FULL_TOKEN_BUDGET_WINDOWS;
  assert.equal(availableTokenBudget(route, NOW + 999_000_000), cap); // long idle caps, doesn't grow unbounded
});

test("reservationFor takes the max of client estimate, floor, and calibrated margin", () => {
  const j = job({ input_token_estimate: 500, max_output_token_estimate: 200 }); // 700
  assert.equal(reservationFor(j), 700);
  assert.equal(reservationFor(j, { estimateFloor: 1000 }), 1000);
  assert.equal(reservationFor(j, { calibratedMargin: 1500 }), 1500);
  assert.equal(reservationFor(j, { estimateFloor: 800, calibratedMargin: 600 }), 800);
});

test("earliestSafeStart admits immediately when a fresh route has full headroom", () => {
  const route = freshRoute();
  const result = earliestSafeStart(route, job(), NOW, NOW);
  assert.equal(result.notBeforeAt, NOW);
  assert.equal(result.reservation, 700);
});

test("earliestSafeStart waits for the next RPM window once the window is full", () => {
  const route = freshRoute({ rpm: 30, rpm_window_start: NOW, rpm_count: 30 });
  const result = earliestSafeStart(route, job(), NOW, NOW);
  assert.equal(result.notBeforeAt, NOW + 60_000);
});

test("earliestSafeStart does not wait once the RPM window has rolled over", () => {
  const route = freshRoute({ rpm: 30, rpm_window_start: NOW - 61_000, rpm_count: 30 });
  const result = earliestSafeStart(route, job(), NOW, NOW);
  assert.equal(result.notBeforeAt, NOW);
});

test("earliestSafeStart waits for the next RPD window once the daily cap is full", () => {
  const route = freshRoute({ rpd: 500, rpd_window_start: NOW, rpd_count: 500 });
  const result = earliestSafeStart(route, job(), NOW, NOW);
  assert.equal(result.notBeforeAt, NOW + 86_400_000);
});

test("earliestSafeStart waits for token budget to refill for an oversized job", () => {
  // A job needing 5000 tokens on a route with only 1000 currently available (tpm=6000, budget
  // drained to near-zero) must wait for enough to accumulate, not be rejected outright.
  const route = freshRoute({ tpm: 6000, full_token_budget: 1000, token_budget_updated_at: NOW });
  const bigJob = job({ input_token_estimate: 4000, max_output_token_estimate: 1000 }); // 5000
  const result = earliestSafeStart(route, bigJob, NOW, NOW);
  assert.ok(result.notBeforeAt > NOW);
  // Refill rate is tpm/60000 tokens/ms; deficit is 5000-1000=4000 tokens.
  const expectedWaitMs = Math.ceil((4000 * 60_000) / 6000);
  assert.equal(result.notBeforeAt, NOW + expectedWaitMs);
});

test("earliestSafeStart returns null when a reservation exceeds the route's burst ceiling outright", () => {
  const route = freshRoute({ tpm: 1000 }); // ceiling = 1000 * FULL_TOKEN_BUDGET_WINDOWS = 5000
  const hugeJob = job({ input_token_estimate: 4000, max_output_token_estimate: 2000 }); // 6000 > 5000
  const result = earliestSafeStart(route, hugeJob, NOW, NOW);
  assert.equal(result, null);
});

test("earliestSafeStart honors an additive route buffer from repeated 429s", () => {
  const route = freshRoute({ buffer_seconds: 30 });
  const result = earliestSafeStart(route, job(), NOW, NOW);
  assert.equal(result.notBeforeAt, NOW + 30_000);
});

test("earliestSafeStart honors an explicit blocked_until floor", () => {
  const route = freshRoute({ blocked_until: NOW + 45_000 });
  const result = earliestSafeStart(route, job(), NOW, NOW);
  assert.equal(result.notBeforeAt, NOW + 45_000);
});

test("earliestSafeStart never returns a time before the lane's own earliestCandidateTime", () => {
  const route = freshRoute();
  const laneTime = NOW + 10_000; // a predecessor in this lane already occupies until here
  const result = earliestSafeStart(route, job(), laneTime, NOW);
  assert.equal(result.notBeforeAt, laneTime);
});

test("routeHasCapacityFor is false when the safe start plus call duration exceeds the window", () => {
  const route = freshRoute({ blocked_until: NOW + 20_000 });
  const withinWindow = routeHasCapacityFor(route, job(), NOW, 25, { callDurationCeilingMs: 1000 });
  assert.equal(withinWindow, true); // 20s block + 1s ceiling < 25s window

  const tooLate = routeHasCapacityFor(route, job(), NOW, 25, { callDurationCeilingMs: 6000 });
  assert.equal(tooLate, false); // 20s block + 6s ceiling > 25s window
});

test("routeHasCapacityFor is false for a permanently-oversized reservation", () => {
  const route = freshRoute({ tpm: 100 });
  const hugeJob = job({ input_token_estimate: 10_000, max_output_token_estimate: 10_000 });
  assert.equal(routeHasCapacityFor(route, hugeJob, NOW, 25), false);
});

test("computeRouteLaneWait returns wait_ms relative to now, plus the inter-request gap", () => {
  const route = freshRoute({ rpm: 30 });
  const laneTime = NOW + 5000;
  const result = computeRouteLaneWait(route, job(), laneTime, NOW);
  assert.equal(result.wait_ms, 5000);
  assert.equal(result.not_before_at, laneTime);
  assert.equal(result.min_inter_request_gap_ms, 2000);
  assert.equal(result.reservation, 700);
});

test("computeRouteLaneWait returns null for a permanently-oversized reservation", () => {
  const route = freshRoute({ tpm: 100 });
  const hugeJob = job({ input_token_estimate: 10_000, max_output_token_estimate: 10_000 });
  assert.equal(computeRouteLaneWait(route, hugeJob, NOW, NOW), null);
});

test("paymentRequiredBackoffUntil blocks until the start of the next UTC day on the first 402", () => {
  const now = Date.UTC(2024, 0, 3, 15, 0, 0); // Wed 2024-01-03 15:00 UTC
  assert.equal(paymentRequiredBackoffUntil(1, now), Date.UTC(2024, 0, 4));
});

test("paymentRequiredBackoffUntil blocks until next UTC Monday on the second consecutive 402", () => {
  const wednesday = Date.UTC(2024, 0, 3); // 2024-01-03 is a Wednesday
  assert.equal(paymentRequiredBackoffUntil(2, wednesday), Date.UTC(2024, 0, 8)); // next Monday

  // A streak reached exactly on a Monday still gets a full week out, not zero.
  const monday = Date.UTC(2024, 0, 1); // 2024-01-01 is a Monday
  assert.equal(paymentRequiredBackoffUntil(2, monday), Date.UTC(2024, 0, 8));
});

test("paymentRequiredBackoffUntil blocks until the start of next UTC month on the third+ consecutive 402", () => {
  const now = Date.UTC(2024, 0, 20);
  assert.equal(paymentRequiredBackoffUntil(3, now), Date.UTC(2024, 1, 1));
  // Stays pinned to "next month," not escalating indefinitely past this point.
  assert.equal(paymentRequiredBackoffUntil(5, now), Date.UTC(2024, 1, 1));
});

test("paymentRequiredBackoffUntil rolls the year over correctly for a December streak", () => {
  const december = Date.UTC(2024, 11, 15);
  assert.equal(paymentRequiredBackoffUntil(3, december), Date.UTC(2025, 0, 1));
});
