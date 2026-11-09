/** Current SQLite route-ledger operations used by admission and claim settlement. */
import { FULL_TOKEN_BUDGET_WINDOWS } from "./pacing.js";

export function readRouteLedgers(sql) {
  return new Map([...sql.exec("SELECT * FROM routes")].map(row => [row.route_id, row]));
}

export function getOrCreateRouteLedger(sql, routeId, now, catalogRoute) {
  const rows = [...sql.exec("SELECT * FROM routes WHERE route_id = ?", routeId)];
  if (rows.length > 0) return rows[0];
  const seedBudget = (Number(catalogRoute?.tpm) || 0) * FULL_TOKEN_BUDGET_WINDOWS;
  sql.exec(
    `INSERT INTO routes (
      route_id, rpm_window_start, rpm_count, rpd_window_start, rpd_count, rpd_day_key,
      tpm_window_start, tpm_reserved, full_token_budget, token_budget_updated_at,
      provisional_reservation, settled_usage, cost_accumulated,
      throttle_streak, last_provider_status, blocked_until, buffer_seconds,
      payment_required_streak
    ) VALUES (?, 0, 0, 0, 0, '', 0, 0, ?, ?, 0, 0, 0, 0, NULL, NULL, 0, 0)`,
    routeId, seedBudget, now
  );
  return {
    route_id: routeId, rpm_window_start: 0, rpm_count: 0, rpd_window_start: 0,
    rpd_day_key: "", rpd_count: 0, tpd_used: 0, input_window_json: "[]",
    tpm_window_start: 0, tpm_reserved: 0, full_token_budget: seedBudget,
    token_budget_updated_at: now, provisional_reservation: 0, settled_usage: 0,
    cost_accumulated: 0, throttle_streak: 0, last_provider_status: null,
    blocked_until: null, buffer_seconds: 0, payment_required_streak: 0,
  };
}

export function writeRouteClaim(sql, route) {
  sql.exec(
    `UPDATE routes SET
      rpm_window_start=?, rpm_count=?, rpd_window_start=?, rpd_count=?, rpd_day_key=?,
      tpm_window_start=?, tpm_reserved=?, full_token_budget=?, token_budget_updated_at=?,
      provisional_reservation=?, tpd_used=?, tpd_updated_at=?, input_window_json=?
     WHERE route_id=?`,
    route.rpm_window_start, route.rpm_count, route.rpd_window_start, route.rpd_count,
    route.rpd_day_key, route.tpm_window_start, route.tpm_reserved, route.full_token_budget,
    route.token_budget_updated_at, route.provisional_reservation, route.tpd_used,
    route.tpd_updated_at, route.input_window_json, route.route_id
  );
}
