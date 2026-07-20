import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import {
  DEFAULT_CONFIG,
  parseHistory,
  serializeHistory,
  median,
  baselineFor,
  evaluate,
  appendCapped,
  parseArgs,
  formatReport,
} from './baseline.mjs';

/** Build `n` accepted history entries all holding the same value for every field. */
const accepted = (n, value) =>
  Array.from({ length: n }, (_, i) => ({
    ts: `2026-07-${String(i + 1).padStart(2, '0')}T00:00:00Z`,
    us: value,
    ca: value,
    rawTotal: value,
    status: 'accepted',
  }));

describe('median', () => {
  it('returns null for no values', () => {
    assert.equal(median([]), null);
  });

  it('returns the middle value for an odd count', () => {
    assert.equal(median([3, 1, 2]), 2);
  });

  it('averages the two middle values for an even count', () => {
    assert.equal(median([1, 2, 3, 4]), 2.5);
  });

  it('does not mutate its input', () => {
    const values = [3, 1, 2];
    median(values);
    assert.deepEqual(values, [3, 1, 2]);
  });
});

describe('parseHistory', () => {
  it('parses newline-delimited JSON and tolerates a trailing newline', () => {
    const entries = parseHistory('{"us":1}\n{"us":2}\n');
    assert.equal(entries.length, 2);
    assert.equal(entries[1].us, 2);
  });

  it('returns an empty array for empty, whitespace, or nullish input', () => {
    assert.deepEqual(parseHistory(''), []);
    assert.deepEqual(parseHistory('  \n \n'), []);
    assert.deepEqual(parseHistory(undefined), []);
  });

  it('skips malformed lines rather than throwing', () => {
    const entries = parseHistory('{"us":1}\nNOT JSON\n{"us":3}\n');
    assert.equal(entries.length, 2);
    assert.deepEqual(entries.map((e) => e.us), [1, 3]);
  });

  it('round-trips through serializeHistory', () => {
    const entries = [{ us: 1, status: 'accepted' }, { us: 2, status: 'rejected' }];
    assert.deepEqual(parseHistory(serializeHistory(entries)), entries);
  });
});

describe('baselineFor', () => {
  it('reports zero samples for empty history', () => {
    assert.deepEqual(baselineFor([], 'us', 24), { baseline: null, samples: 0 });
  });

  it('ignores rejected entries', () => {
    const entries = [
      ...accepted(3, 100),
      { ts: 'x', us: 1, ca: 1, rawTotal: 1, status: 'rejected' },
    ];
    assert.deepEqual(baselineFor(entries, 'us', 24), { baseline: 100, samples: 3 });
  });

  it('uses only the most recent `window` accepted values', () => {
    const entries = [...accepted(5, 100), ...accepted(3, 200)];
    assert.deepEqual(baselineFor(entries, 'us', 3), { baseline: 200, samples: 3 });
  });

  it('ignores entries missing the field or holding a non-finite value', () => {
    const entries = [
      { ts: 'a', us: 100, status: 'accepted' },
      { ts: 'b', status: 'accepted' },
      { ts: 'c', us: null, status: 'accepted' },
      { ts: 'd', us: 'oops', status: 'accepted' },
    ];
    assert.deepEqual(baselineFor(entries, 'us', 24), { baseline: 100, samples: 1 });
  });
});

describe('evaluate', () => {
  const config = { ...DEFAULT_CONFIG, fields: ['us'], minSamples: 6, window: 24, floorRatio: 0.95 };

  it('is observe-only below minSamples, however far off the value is', () => {
    const result = evaluate({ us: 1 }, accepted(5, 100_000), config);
    assert.equal(result.status, 'accepted');
    assert.equal(result.checks[0].verdict, 'observing');
  });

  it('accepts a value exactly at the floor', () => {
    const result = evaluate({ us: 95_000 }, accepted(6, 100_000), config);
    assert.equal(result.status, 'accepted');
    assert.equal(result.checks[0].verdict, 'ok');
    assert.equal(result.checks[0].floor, 95_000);
  });

  it('rejects a value one below the floor', () => {
    const result = evaluate({ us: 94_999 }, accepted(6, 100_000), config);
    assert.equal(result.status, 'rejected');
    assert.equal(result.checks[0].verdict, 'below-floor');
  });

  it('accepts a value above baseline (growth is never blocked)', () => {
    const result = evaluate({ us: 200_000 }, accepted(6, 100_000), config);
    assert.equal(result.status, 'accepted');
  });

  it('rejects when any one of several fields is below its floor', () => {
    const multi = { ...config, fields: ['us', 'ca', 'rawTotal'] };
    const result = evaluate({ us: 100_000, ca: 1, rawTotal: 100_000 }, accepted(6, 100_000), multi);
    assert.equal(result.status, 'rejected');
    assert.deepEqual(
      result.checks.map((c) => c.verdict),
      ['ok', 'below-floor', 'ok']
    );
  });

  it('is observe-only for a field with no history, even when others have history', () => {
    const multi = { ...config, fields: ['us', 'newField'] };
    const result = evaluate({ us: 100_000, newField: 5 }, accepted(6, 100_000), multi);
    assert.equal(result.status, 'accepted');
    assert.equal(result.checks[1].verdict, 'observing');
  });

  it('resists poisoning: a run of rejected entries does not move the baseline', () => {
    const poisoned = [
      ...accepted(6, 100_000),
      ...Array.from({ length: 20 }, () => ({ ts: 'bad', us: 1_000, status: 'rejected' })),
    ];
    const result = evaluate({ us: 94_999 }, poisoned, config);
    assert.equal(result.status, 'rejected', 'rejected entries must never become the new normal');
    assert.equal(result.checks[0].baseline, 100_000);
  });

  it('treats a missing observed value as a rejection, not a pass', () => {
    const result = evaluate({}, accepted(6, 100_000), config);
    assert.equal(result.status, 'rejected');
  });
});

describe('appendCapped', () => {
  it('appends within the cap', () => {
    assert.deepEqual(appendCapped([{ a: 1 }], { a: 2 }, 5), [{ a: 1 }, { a: 2 }]);
  });

  it('drops the oldest entries beyond the cap', () => {
    const entries = Array.from({ length: 5 }, (_, i) => ({ a: i }));
    const out = appendCapped(entries, { a: 99 }, 3);
    assert.deepEqual(out, [{ a: 3 }, { a: 4 }, { a: 99 }]);
  });

  it('does not mutate its input', () => {
    const entries = [{ a: 1 }];
    appendCapped(entries, { a: 2 }, 5);
    assert.equal(entries.length, 1);
  });

  it('evicts oldest rejected entries first, preserving accepted entries, when history is mostly rejected', () => {
    const entries = [
      { a: 1, status: 'accepted' },
      { a: 2, status: 'rejected' },
      { a: 3, status: 'rejected' },
      { a: 4, status: 'rejected' },
    ];
    const out = appendCapped(entries, { a: 5, status: 'rejected' }, 3);
    assert.equal(out.length, 3);
    assert.deepEqual(
      out.find((e) => e.status === 'accepted'),
      { a: 1, status: 'accepted' },
      'the accepted entry must survive eviction'
    );
    // Chronological order preserved: accepted entry stays first, oldest rejected (a:2, a:3) dropped.
    assert.deepEqual(out.map((e) => e.a), [1, 4, 5]);
  });

  it('regression: a sustained run of rejected entries never evicts enough accepted history to collapse the baseline', () => {
    const cap = 10;
    const acceptedCount = 3;
    let history = accepted(acceptedCount, 100_000);

    // Fill the remaining slots up to cap with rejected entries, one append at a time
    // (mirrors how the pipeline calls appendCapped once per run).
    for (let i = 0; history.length < cap; i++) {
      history = appendCapped(history, { ts: `fill-${i}`, us: 1, status: 'rejected' }, cap);
    }
    assert.equal(history.length, cap);

    // Simulate a sustained outage: far more consecutive rejected runs than the cap.
    for (let i = 0; i < 50; i++) {
      history = appendCapped(history, { ts: `outage-${i}`, us: 1, status: 'rejected' }, cap);
    }

    assert.equal(history.length, cap, 'history must never exceed cap');
    const { samples } = baselineFor(history, 'us', 24);
    assert.ok(
      samples >= acceptedCount,
      `expected at least ${acceptedCount} accepted samples to survive the outage, got ${samples}`
    );
  });

  it('drops the oldest accepted entries when the whole history is accepted and over cap (existing behavior preserved)', () => {
    const entries = accepted(5, 100);
    const out = appendCapped(entries, { us: 999, status: 'accepted' }, 3);
    assert.equal(out.length, 3);
    assert.deepEqual(out.map((e) => e.us), [100, 100, 999]);
  });

  it('never returns more entries than cap regardless of accepted/rejected composition', () => {
    const mixed = Array.from({ length: 20 }, (_, i) => ({
      a: i,
      status: i % 3 === 0 ? 'accepted' : 'rejected',
    }));
    const out = appendCapped(mixed, { a: 99, status: 'rejected' }, 7);
    assert.ok(out.length <= 7);
  });

  it('does not mutate its input when evicting rejected entries ahead of accepted ones', () => {
    const entries = [
      { a: 1, status: 'accepted' },
      { a: 2, status: 'rejected' },
      { a: 3, status: 'rejected' },
    ];
    const snapshot = JSON.parse(JSON.stringify(entries));
    appendCapped(entries, { a: 4, status: 'rejected' }, 2);
    assert.deepEqual(entries, snapshot);
  });
});

describe('parseArgs', () => {
  it('parses the required flags', () => {
    const args = parseArgs(['--meta', '/tmp/meta.json', '--history', '/tmp/h.jsonl']);
    assert.equal(args.meta, '/tmp/meta.json');
    assert.equal(args.history, '/tmp/h.jsonl');
    assert.equal(args.report, null);
    assert.equal(args.runId, null);
  });

  it('parses the optional flags', () => {
    const args = parseArgs([
      '--meta', 'm', '--history', 'h', '--report', 'r.md', '--run-id', '123',
    ]);
    assert.equal(args.report, 'r.md');
    assert.equal(args.runId, '123');
  });

  it('rejects an unknown flag rather than ignoring it', () => {
    assert.throws(() => parseArgs(['--meta', 'm', '--history', 'h', '--oops']), /unknown argument/i);
  });

  it('rejects a missing required flag', () => {
    assert.throws(() => parseArgs(['--meta', 'm']), /--history/);
  });

  it('rejects a flag with no value', () => {
    assert.throws(() => parseArgs(['--meta', '--history', 'h']), /--meta/);
  });
});

describe('formatReport', () => {
  it('names every below-floor field and includes the numbers', () => {
    const result = {
      status: 'rejected',
      checks: [
        { field: 'us', observed: 94_000, baseline: 100_000, samples: 24, floor: 95_000, verdict: 'below-floor' },
        { field: 'ca', observed: 540, baseline: 538, samples: 24, floor: 511.1, verdict: 'ok' },
        { field: 'rawTotal', observed: 95_000, baseline: 100_000, samples: 24, floor: 95_000, verdict: 'ok' },
      ],
    };
    const md = formatReport(result, '29721490530');
    assert.match(md, /\bus\b/);
    assert.match(md, /94,?000/);
    assert.match(md, /100,?000/);
    assert.match(md, /29721490530/);
    assert.match(md, /below-floor/);
  });

  it('renders an observing check without inventing a floor', () => {
    const result = {
      status: 'accepted',
      checks: [{ field: 'us', observed: 100, baseline: null, samples: 2, floor: null, verdict: 'observing' }],
    };
    const md = formatReport(result, null);
    assert.match(md, /observing/);
    assert.doesNotMatch(md, /NaN|null%/);
  });
});
