#!/usr/bin/env node
// National count baseline for the hourly camera pipeline.
//
// Compares this run's national feature counts against the median of recent
// ACCEPTED runs and blocks the upload when a count falls below the floor. The
// absolute floors in fetch.mjs stay as a first line of defence; this catches
// the case they cannot — a dataset that is plausible in absolute terms but far
// below what the previous runs actually produced.
//
// Usage:
//   node data/cameras/baseline.mjs --meta <meta.json> --history <history.jsonl> [--report <out.md>] [--run-id <id>]
//
// Exits 0 when the run is accepted, 1 when it is rejected. The history file is
// rewritten with the new entry in BOTH cases, so a rejected run is recorded.

export const DEFAULT_CONFIG = {
  fields: ['us', 'ca', 'rawTotal'],
  window: 24, // accepted runs considered for the median (~1 day at hourly cadence)
  minSamples: 6, // observe-only until this many accepted samples exist for a field
  floorRatio: 0.95, // block below 95% of the median
  historyCap: 720, // ~30 days of hourly entries
};

/** Parse newline-delimited JSON, skipping malformed lines rather than throwing. */
export function parseHistory(text) {
  const entries = [];
  for (const line of String(text ?? '').split('\n')) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    try {
      const parsed = JSON.parse(trimmed);
      if (parsed && typeof parsed === 'object') entries.push(parsed);
    } catch {
      // A truncated or corrupt line must not take the pipeline down. Losing a
      // sample degrades the baseline toward observe-only, which is safe.
      console.warn(`Skipping malformed history line: ${trimmed.slice(0, 120)}`);
    }
  }
  return entries;
}

export function serializeHistory(entries) {
  return entries.map((e) => JSON.stringify(e)).join('\n') + '\n';
}

export function median(values) {
  if (values.length === 0) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 0 ? (sorted[mid - 1] + sorted[mid]) / 2 : sorted[mid];
}

/**
 * Median of the last `window` ACCEPTED values for `field`. Accepted-only is what
 * stops a bad run from becoming the new normal: without it, repeated failures
 * would walk the baseline down until the guard accepts anything.
 */
export function baselineFor(entries, field, window) {
  const values = entries
    .filter((e) => e?.status === 'accepted')
    .map((e) => e?.[field])
    .filter((v) => typeof v === 'number' && Number.isFinite(v));
  const recent = values.slice(-window);
  return { baseline: median(recent), samples: recent.length };
}

export function evaluate(observed, entries, config = DEFAULT_CONFIG) {
  const checks = [];

  for (const field of config.fields) {
    const value = observed?.[field];
    const { baseline, samples } = baselineFor(entries, field, config.window);

    if (typeof value !== 'number' || !Number.isFinite(value)) {
      checks.push({ field, observed: value ?? null, baseline, samples, floor: null, verdict: 'below-floor' });
      continue;
    }

    if (samples < config.minSamples) {
      checks.push({ field, observed: value, baseline, samples, floor: null, verdict: 'observing' });
      continue;
    }

    const floor = baseline * config.floorRatio;
    checks.push({
      field,
      observed: value,
      baseline,
      samples,
      floor,
      verdict: value < floor ? 'below-floor' : 'ok',
    });
  }

  const status = checks.some((c) => c.verdict === 'below-floor') ? 'rejected' : 'accepted';
  return { status, checks };
}

export function appendCapped(entries, entry, cap) {
  return [...entries, entry].slice(-cap);
}
