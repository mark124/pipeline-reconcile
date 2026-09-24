---
name: reconcile
description: Check that the records a data pipeline reported as delivered actually landed. Catches silent data loss, content corrupted in transit, stale re-shipped output, and journal entries that claim a delivery that never happened. Use when someone asks whether a load, sync, ETL run, or migration actually worked.
---

# Reconcile a pipeline run

A pipeline run that exits zero means the job finished. It does not mean the records arrived.
This skill asserts the thing the green checkmark is supposed to mean.

## When to use this

- Someone asks "did that load actually work?" or "why is this record missing downstream?"
- A migration, backfill, nightly sync or ETL run needs verifying before anyone trusts it
- A destination row count looks close-but-not-equal to the source and nobody knows why
- A corrected record was re-shipped and the downstream copy still looks old

## The three failure classes

These are the ones a run's own success report cannot see. All three came from real defects.

**1. Silent loss.** Records never arrive. No error, no retry, nothing in the log that looks wrong.
Often a rate limit, a dropped batch, or a swallowed exception in a loop.

**2. Stale re-ship.** A transformation is corrected upstream, the run succeeds, and the destination
still holds the *previous* run's output. The fix shipped; the data did not change.

**3. Phantom delivery.** A record is written to an idempotency or delivery log as delivered when it
was not — for instance after a 429 — so it is never retried. **The log is now actively lying**, and
every subsequent run will skip it.

## How to run the check

`scripts/reconcile.py` is stdlib-only, read-only, and needs no production credentials. It reads
exports, never a live system.

```
python3 ${CLAUDE_PLUGIN_ROOT}/skills/reconcile/scripts/reconcile.py \
    --source SOURCE --dest DEST --key FIELD [options]
```

On Windows the interpreter is usually `python`, not `python3`.

| flag | meaning |
|---|---|
| `--source` | what the pipeline was given (JSONL or CSV) |
| `--dest` | what is actually in the destination |
| `--key` | the natural key; repeat for composite keys; dotted paths work (`a.b.c`) |
| `--journal` | a delivery/idempotency log, to catch phantom deliveries |
| `--baseline` | the PREVIOUS run's destination, to catch stale re-ships |
| `--ignore` | volatile fields excluded from content comparison |
| `--show` | how many offending keys to print (default 20) |
| `--json-out` | write the full finding set to a file |
| `--allow-unreadable` | do not fail on source rows that could not be parsed or keyed |
| `--allow-orphans` | do not fail on keys found downstream but never in the source |
| `--allow-empty` | permit an input file with zero rows (otherwise exit 2) |

**Exit code 1 on any finding** — missing, mismatched, stale, phantom, duplicate, orphaned, or a
row it could not read — so it can sit in CI and block a release. **Exit code 2** means the check
could not run meaningfully: a missing file, an empty input, a journal whose key never matched, or
`--json-out` pointed at one of the inputs.

## Reading the output

- **MISSING** — in the source, absent downstream. This is the loss.
- **MISMATCHED** — present both sides, but the content differs. Corruption or a partial write.
- **STALE** — present both sides, the source has changed, and the destination still matches the
  previous run. Needs `--baseline`.
- **ORPHANED** — downstream but never in the source. Usually a stale destination or a bad join.
  This fails the run by default. On an incremental load the destination legitimately holds more
  history than today's extract, so pass `--allow-orphans` there.
- **PHANTOM** — the journal says delivered, the destination disagrees.
- **DUPLICATE** — the same natural key twice. In the source, one will silently win downstream; in
  the destination, something loaded twice.

Every finding is reported **by key**, so "we lost 23 records" becomes 23 identifiers for a ticket.

## Four things that decide whether the answer means anything

**The journal usually keys on a different field.** Delivery logs rarely repeat the business key —
they carry `recordId`, `resourceId` or a URL fragment. If `--journal-key` is wrong, no journal row
can be matched and the phantom check silently examines nothing. The tool now refuses to run rather
than print `PHANTOM: 0` in that case, but check the journal's own field names before you trust a
clean phantom result.


**Without `--baseline`, a stale re-ship cannot be ruled out.** A changed record shows up as
MISMATCHED, but the check cannot tell you the destination is holding *last run's* output rather than
a partial write. The clean-run message says so explicitly. If stale re-ships are the worry, get a
snapshot of the previous destination.

**Choosing `--ignore` matters more than it looks.** Most systems stamp every row with fields that
change on every write and say nothing about whether the content changed: `_etag`,
`_lastModifiedDate`, `updated_at`, surrogate `id`s. Hash those and **every record looks modified**,
which is precisely how a real drift gets buried in noise.

`--ignore` has three scopes, and the difference matters:

| form | strips | use for |
|---|---|---|
| `_etag` | top level only | a field you know sits at the root |
| `*._etag` | every depth | a stamp the system writes throughout the payload |
| `meta.updated_at` | that one exact path | anything you want removed surgically |

The default is `id,*._etag,*._lastModifiedDate,*.lastModifiedDate`. **`id` is deliberately
top-level-only**: a nested `id` is usually a reference to another entity, so stripping those
everywhere would hide a student being moved to a different school.

**Rows that could not be read are a finding, not a warning.** Source lines that fail to parse, or
that have no value for the key, cannot be checked at all — and if the source is where rows are going
bad, that is silent loss inside the tool that exists to catch silent loss. They fail the run.
`--allow-unreadable` downgrades that when the input is known to be ragged.

## Comparing a CSV against JSON

Values are normalised before comparison, so `5`, `5.0` and `"5"` match and a CSV export does not
report every row as changed. Leading **and trailing** zeros are preserved — `"007"` is not `7`, and
the section code `"3.10"` is not `"3.1"`. One deliberate consequence: `"5.50"` and `5.5` report as
MISMATCHED although they are numerically equal. That is the safe direction — a finding you dismiss
in a second beats a silent match you never see. Empty and absent
fields are treated alike, because CSV cannot express the difference. The tool says so in its header
when the two sides are different formats: CSV carries no nested structure, so across formats the
content check only covers what both sides can express.

## Demonstrating it without anyone's data

`scripts/make_fixtures.py` generates 1,000 synthetic records with two defects injected — 23 records
rate-limited and logged as delivered, 17 corrections re-shipped as the previous run's output. It
writes to `./fixtures` by default and takes `--out` for anywhere else.

```
python3 ${CLAUDE_PLUGIN_ROOT}/skills/reconcile/scripts/make_fixtures.py
python3 ${CLAUDE_PLUGIN_ROOT}/skills/reconcile/scripts/reconcile.py \
    --source fixtures/students_source.jsonl \
    --dest fixtures/students_dest.jsonl --key studentUniqueId \
    --journal fixtures/delivery_journal.jsonl \
    --baseline fixtures/students_baseline.jsonl
```

Finds 23 missing, 17 stale, 23 phantom, and exits 1. Useful when someone needs to see the check work
before granting access to anything real.

## Provenance

Written against two defects found in open-source Ed-Fi ingestion tooling in July 2026: a corrected
transformation silently re-shipping stale output, and a rate-limited record written to the
idempotency log as though delivered. Both were reported with reproductions. The failure classes are
not specific to Ed-Fi — any pipeline with a source, a destination and a delivery log can exhibit all
three.

★ A note worth keeping: the first version of the fixture generator claimed 17 stale records and the
harness found 15. The harness was right — two of the "corrections" set a field to the value it
already held, so there was nothing to detect. The generator was counting what it *intended* rather
than what it *did*. That is the exact failure this tool exists to catch.
