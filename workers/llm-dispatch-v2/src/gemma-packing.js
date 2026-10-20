/** Correlated size feedback, separate from token-ratio calibration and quota classification. */
export const GEMMA_PACKING_REVISION = "gemma-complete-request-v1";
export const GEMMA_COMBINED_TARGET = 16000;
export const GEMMA_SIZE_WINDOW = 128;
export const GEMMA_MIN_SAMPLES = 20;
export const GEMMA_TARGET_FLOOR = 4000;

function validUsage(sample) {
  return Number.isSafeInteger(sample?.input) && sample.input > 0 &&
    Number.isSafeInteger(sample?.output) && sample.output >= 0 &&
    Number.isSafeInteger(sample.input + sample.output);
}

export function parseGemmaPacking(raw) {
  let value;
  try { value = typeof raw === "string" ? JSON.parse(raw) : raw; } catch { value = null; }
  const samples = Array.isArray(value?.samples) ? value.samples.filter(s =>
    typeof s?.id === "string" && s.id.length <= 256 &&
    Number.isFinite(s.estimate) && s.estimate >= 200 &&
    validUsage(s) &&
    typeof s.overflow === "boolean"
  ).slice(-GEMMA_SIZE_WINDOW) : [];
  const target = Number.isSafeInteger(value?.target) && value.target > 0 ? value.target : null;
  const fresh = Number.isSafeInteger(value?.fresh) && value.fresh >= 0
    ? Math.min(value.fresh, GEMMA_SIZE_WINDOW) : 0;
  return { target, samples, fresh };
}

/** One resolved packet, never a retry count. No automatic growth or timer-driven rebound. */
export function recordGemmaPacking(raw, sample) {
  const state = parseGemmaPacking(raw);
  if (!sample || typeof sample.id !== "string" || sample.id.length > 256 ||
      !Number.isFinite(sample.estimate) || sample.estimate < 200 ||
      !validUsage(sample) ||
      typeof sample.overflow !== "boolean" || state.samples.some(s => s.id === sample.id)) {
    return state;
  }
  state.samples.push(sample);
  state.samples = state.samples.slice(-GEMMA_SIZE_WINDOW);
  // Old backlog already larger than the published target cannot repeatedly push it down.
  if (state.target != null && sample.estimate > state.target) return state;
  state.fresh = Math.min(GEMMA_SIZE_WINDOW, state.fresh + 1);
  if (!sample.overflow) return state;
  const relevant = state.samples.filter(s => state.target == null || s.estimate <= state.target);
  if (relevant.length < GEMMA_MIN_SAMPLES || state.fresh < GEMMA_MIN_SAMPLES) return state;
  const failures = relevant.filter(s => s.overflow).length;
  if (failures / relevant.length < 0.05) return state;
  const ratios = relevant.map(s => (s.input + s.output) / s.estimate).sort((a, b) => a - b);
  const ratio = ratios[Math.max(0, Math.ceil(ratios.length * 0.95) - 1)];
  const reference = state.target ?? sample.estimate;
  const candidate = Math.max(GEMMA_TARGET_FLOOR, Math.floor(reference * 0.9), Math.min(
    Math.floor(GEMMA_COMBINED_TARGET / (ratio * 1.05)),
    Math.floor(sample.estimate * 0.95),
    state.target == null ? Infinity : Math.floor(state.target * 0.95)
  ));
  if (candidate < reference) {
    state.target = state.target == null ? candidate : Math.min(state.target, candidate);
    state.fresh = 0;
  }
  return state;
}

export function gemmaPackingStatus(raw) {
  const state = parseGemmaPacking(raw);
  const current = state.samples.filter(s => state.target == null || s.estimate <= state.target);
  return {
    policy_revision: GEMMA_PACKING_REVISION,
    input_estimate_target: state.target,
    resolved_packets: state.samples.length,
    correlated_size_overflows: state.samples.filter(s => s.overflow).length,
    current_target_resolved_packets: current.length,
    current_target_size_overflows: current.filter(s => s.overflow).length,
    fresh_resolutions_since_cut: state.fresh,
    minimum_resolved_packets: GEMMA_MIN_SAMPLES,
    at_safety_floor: state.target != null && state.target <= GEMMA_TARGET_FLOOR,
    denominator: "resolved Google successes and same-generation NVIDIA recoveries only",
    signal: "Google TPM refusal then NVIDIA full input plus output above 16000; empirical heuristic",
  };
}
