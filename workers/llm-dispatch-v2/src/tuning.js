/**
 * Non-secret tuning for the Worker, compiled from config/dispatch_tuning.yml.
 *
 * These were Cloudflare `vars` until they hit the Workers Free limit of 64 variables per Worker
 * (secrets count). Precedence, highest first: a Cloudflare variable or secret of the same name (a
 * deliberate incident-time override), the compiled value, then each read site's own literal fallback.
 * Values are surfaced as strings, exactly as dashboard variables always were, so every existing
 * `Number(env.NAME)` / `env.NAME ?? fallback` read behaves unchanged.
 */
import TUNING from "./dispatch_tuning.json" with { type: "json" };

const VALUES = TUNING.values;
const WRAPPED = Symbol.for("citypods.dispatch.tuning");

export function withTuning(env) {
  const base = env || {};
  if (base[WRAPPED]) return base;
  return new Proxy(base, {
    get(target, prop) {
      if (prop === WRAPPED) return true;
      const actual = Reflect.get(target, prop, target);
      if (actual !== undefined || typeof prop !== "string") return actual;
      return Object.hasOwn(VALUES, prop) ? String(VALUES[prop]) : undefined;
    },
    has(target, prop) {
      return Reflect.has(target, prop) || (typeof prop === "string" && Object.hasOwn(VALUES, prop));
    },
  });
}

export const TUNING_NAMES = Object.freeze(Object.keys(VALUES));
