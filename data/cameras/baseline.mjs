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

import { readFile, writeFile } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';

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

const FLAGS = { '--meta': 'meta', '--history': 'history', '--report': 'report', '--run-id': 'runId' };

export function parseArgs(argv) {
  const out = { meta: null, history: null, report: null, runId: null };

  for (let i = 0; i < argv.length; i++) {
    const key = FLAGS[argv[i]];
    if (!key) throw new Error(`Unknown argument: ${argv[i]}`);
    const value = argv[i + 1];
    if (value === undefined || FLAGS[value]) throw new Error(`Missing value for ${argv[i]}`);
    out[key] = value;
    i++;
  }

  for (const required of ['meta', 'history']) {
    if (!out[required]) throw new Error(`Missing required argument: --${required}`);
  }

  return out;
}

const fmt = (n) => (typeof n === 'number' && Number.isFinite(n) ? Math.round(n).toLocaleString('en-US') : '—');

export function formatReport(result, runId) {
  const lines = [
    `**Status:** ${result.status}`,
    '',
    '| field | observed | baseline (median) | floor | samples | verdict |',
    '|---|---|---|---|---|---|',
  ];

  for (const c of result.checks) {
    lines.push(
      `| \`${c.field}\` | ${fmt(c.observed)} | ${fmt(c.baseline)} | ${fmt(c.floor)} | ${c.samples} | ${c.verdict} |`
    );
  }

  lines.push('');
  if (result.status === 'rejected') {
    lines.push(
      'The upload was **blocked**. The previously published data is still serving.',
      '',
      'Either the upstream fetch lost data, or the dataset genuinely changed and the',
      'baseline needs to catch up. Check the run log for Overpass remarks first.'
    );
  }
  if (runId) {
    lines.push('', `Run: https://github.com/${process.env.GITHUB_REPOSITORY ?? 'flockhopper3/deflock-data'}/actions/runs/${runId}`);
  }

  return lines.join('\n');
}

async function main() {
  const args = parseArgs(process.argv.slice(2));

  const meta = JSON.parse(await readFile(args.meta, 'utf8'));
  const observed = meta.totals;
  if (!observed) throw new Error(`${args.meta} has no "totals" key — is fetch.mjs up to date?`);

  let historyText = '';
  try {
    historyText = await readFile(args.history, 'utf8');
  } catch (err) {
    if (err.code !== 'ENOENT') throw err;
    console.log('No history file yet — first run, observe-only.');
  }

  const entries = parseHistory(historyText);
  const result = evaluate(observed, entries, DEFAULT_CONFIG);

  for (const c of result.checks) {
    console.log(
      `${c.field}: observed=${fmt(c.observed)} baseline=${fmt(c.baseline)} floor=${fmt(c.floor)} samples=${c.samples} -> ${c.verdict}`
    );
  }

  const entry = {
    ts: new Date().toISOString(),
    ...observed,
    status: result.status,
    ...(args.runId ? { runId: args.runId } : {}),
  };
  // Written on BOTH paths so a rejected run is recorded for forensics. It is
  // excluded from future baselines by its status, not by its absence.
  await writeFile(args.history, serializeHistory(appendCapped(entries, entry, DEFAULT_CONFIG.historyCap)));

  const report = formatReport(result, args.runId);
  if (args.report) await writeFile(args.report, report);

  if (result.status === 'rejected') {
    console.error('\nBaseline check FAILED — refusing to publish.\n');
    console.error(report);
    process.exit(1);
  }

  console.log('\nBaseline check passed.');
}

// Only run as a CLI, never on import — baseline.test.mjs imports this module.
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((err) => {
    console.error(err.message ?? err);
    process.exit(1);
  });
}
