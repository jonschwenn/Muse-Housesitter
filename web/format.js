"use strict";
const HousesitterFormat = (() => {
  const usable = (v) => typeof v === "number" && Number.isFinite(v);
  function fixed(v, digits) {
    if (!usable(v)) return "Unknown";
    const s = v.toFixed(digits);
    return Number(s) === 0 && s.startsWith("-") ? s.slice(1) : s;
  }
  const pct1 = (v) => fixed(v, 1);
  const pct = (v) => fixed(v, Number.isInteger(v) ? 0 : 1);
  const dbm = (v) => fixed(v, 0);
  const mbps = (v) =>
    usable(v) ? fixed(v, Math.abs(v) >= 9.95 ? 0 : 1) : "Unknown";
  const whole = (v) => fixed(v, 0);
  const signalLabel = (v) =>
    !usable(v) ? "Unknown" : v >= -60 ? "strong" : v >= -70 ? "good" : v >= -75 ? "weak" : "very weak";
  const utilizationLabel = (v) =>
    !usable(v) ? "Unknown" : v < 40 ? "light" : v <= 70 ? "busy" : "congested";
  const retryLabel = (v) =>
    !usable(v) ? "Unknown" : v < 10 ? "normal" : v <= 20 ? "elevated" : "high";
  function banded(v, base, labelFn) {
    if (!usable(v)) return "Unknown";
    const label = labelFn(v);
    for (let digits = base; digits <= base + 2; digits++) {
      const s = fixed(v, digits);
      if (labelFn(Number(s)) === label) return s;
    }
    // Two decimal places keep the explicit retry boundary examples readable.
    const digits = Math.min(base + 2, 2);
    const scale = 10 ** digits;
    const candidates = [Math.floor(v * scale) / scale, Math.ceil(v * scale) / scale]
      .filter((candidate) => labelFn(candidate) === label)
      .sort((a, b) => Math.abs(a - v) - Math.abs(b - v));
    return fixed(candidates[0], digits);
  }
  const signalText = (v) => banded(v, 0, signalLabel);
  const utilizationText = (v) => banded(v, Number.isInteger(v) ? 0 : 1, utilizationLabel);
  const retryText = (v) => banded(v, 1, retryLabel);
  return {
    fixed, pct1, pct, dbm, mbps, whole, banded,
    signalLabel, utilizationLabel, retryLabel, signalText, utilizationText, retryText,
  };
})();
if (typeof module !== "undefined") module.exports = HousesitterFormat;
else globalThis.HousesitterFormat = HousesitterFormat;
