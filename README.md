# Pipeline Reconcile

**Prove what landed.** A Claude Code plugin that checks whether the records a data pipeline reported
as delivered actually arrived.

A run that exits zero means the job finished. It does not mean the records arrived. This asserts the
thing the green checkmark is supposed to mean, and exits non-zero when it isn't true.

## Install

```
/plugin marketplace add mark124/pipeline-reconcile
/plugin install pipeline-reconcile@rowset
```

## What it catches

Three failure classes a run's own success report cannot see:

| | what happens | why nothing notices |
|---|---|---|
| **Silent loss** | records never arrive | no error, no retry, nothing wrong in the log |
| **Stale re-ship** | a corrected transformation re-ships the *previous* run's output | the run succeeded; the data just didn't change |
| **Phantom delivery** | a record is logged as delivered when it wasn't — e.g. after a 429 | the log now lies, so it is never retried |

Plus the ordinary ones: content that changed in transit, records that arrived twice, and source rows
that could not be read at all.

## Two skills

**`/reconcile`** — runs the check. Reports MISSING, MISMATCHED, STALE, ORPHANED, PHANTOM and
DUPLICATE findings **by key**, so "we lost 23 records" becomes 23 identifiers you can paste into a
ticket.

**`/reconcile-design`** — works out what a check for *your* pipeline should compare: the natural key,
the volatile fields to ignore, whether a delivery log exists and whether it can be trusted, and
whether a prior snapshot exists to detect stale output against.

## Designed to clear a security review quickly

- **stdlib only** — no install, no dependency audit
- **read-only** — it opens files; it never writes to a source or destination
- **no production credentials** — it takes exports, not connection strings
- **exits 1 on any finding** — loss, corruption, duplication, orphans, or rows it could not
  read; `--allow-orphans` excuses the orphans on an incremental load
- **exits 2 when an input is empty** — a crashed export leaves a 0-byte file, and a check against
  nothing reports clean

## See it work in thirty seconds, on nobody's data

From the root of a clone of this repo:

```
python3 skills/reconcile/scripts/make_fixtures.py
python3 skills/reconcile/scripts/reconcile.py \
    --source   fixtures/students_source.jsonl \
    --dest     fixtures/students_dest.jsonl \
    --key      studentUniqueId \
    --journal  fixtures/delivery_journal.jsonl \
    --baseline fixtures/students_baseline.jsonl
```

If you installed the plugin rather than cloning it, the scripts live under
`${CLAUDE_PLUGIN_ROOT}/skills/reconcile/scripts/`, and `make_fixtures.py` still writes `./fixtures`
in whatever directory you run it from. It takes `--out` to put them elsewhere.

(On Windows, `python` rather than `python3`.)

1,000 synthetic records, deterministic seed, nothing real. The fixture injects 23 rate-limited
records logged as delivered and 17 corrections that re-shipped last run's output. The check finds
23 missing, 17 stale, and 23 phantom journal entries, and exits 1.

## What it will not tell you

- **Without `--baseline`, a stale re-ship cannot be ruled out.** A changed record reports as
  MISMATCHED; distinguishing "this is last week's output" from "this write was partial" needs the
  previous destination. The clean-run message says so rather than implying a check that didn't run.
- **Across CSV and JSON, only the fields both formats can express are compared.** Values are
  normalised so `5`, `5.0` and `"5"` match, and leading zeros are preserved so `"007"` is not `7`.
  But CSV carries no nested structure.
- **It compares exports, not systems.** If the export is wrong, so is the answer.

## Provenance

Written against two defects found in open-source Ed-Fi ingestion tooling in July 2026 — a corrected
transformation silently re-shipping stale output
([earthmover#191](https://github.com/edanalytics/earthmover/issues/191)), and a failed record written
to the idempotency log as though it had been delivered, so it is never re-sent
([lightbeam#92](https://github.com/edanalytics/lightbeam/issues/92)). Both were reported with a
runnable reproduction and a suggested fix, alongside a pull request for a related retry defect
([lightbeam#93](https://github.com/edanalytics/lightbeam/pull/93)). The failure classes are not Ed-Fi
specific: any pipeline with a source, a destination and a delivery log can exhibit all three.

Two notes kept deliberately, because both are the failure this tool exists to catch.

The first version of the fixture generator injected 17 stale records and the checker found 15.
**The checker was right** — two of the "corrections" set a field to the value it already held, so
there was nothing to detect. The generator was counting what it *intended* rather than what it
*did*.

And the checker itself has been caught in three consecutive reviews, every time by the same failure
it exists to find: **a check that ran on nothing and reported clean.**

- **v1.0** — a record containing an `items` list was mistaken for an API page, so both sides were
  emptied and the run compared nothing at all, and passed. Four others alongside it.
- **v1.1** — the fix for those normalised values through `float()`, which collapsed two different
  20-digit account numbers into one value; and stripping `id` at every depth hid a student being
  moved to a different school.
- **v1.2** — if the delivery log keyed on a different field than `--key`, no journal row matched,
  and `PHANTOM: 0` was printed as though the log were clean. It now exits 2 and names the flag.
  Normalising `"3.10"` to `"3.1"` also merged two distinct section codes.
- **v1.3** — an empty input passed clean. A crashed export leaves a 0-byte file, so "checked
  nothing, reported clean" had been sitting in the most ordinary case of all along. Now exit 2.

Every one is an executable regression in `tests/selfcheck.py`. It also lifts the demo commands out
of this README *and* out of SKILL.md and runs them, and asserts that specific documentation claims
still match the code — because "the documented demo doesn't run" was a finding in two consecutive
reviews, and proof-reading did not fix it either time.

```
python3 tests/selfcheck.py          # demos, doc claims, and every past bug — about a second
python3 tests/fuzz_groundtruth.py   # 300 random scenarios against ground truth, ~30s
```

The fuzz harness generates scenarios whose injected loss, stale re-ships, corruption and orphans are
known by construction, then compares them against what the tool reports. It exists because every
other test here asserts a case somebody thought of, and the bugs above were all cases nobody did.

A check that cannot fail is not a check — including this one.

## Privacy

**This plugin collects nothing and transmits nothing.** There is no telemetry, no update check, no
analytics, and no outbound request of any kind — the scripts import only the Python standard library
and open no network connections. Nothing is sent to Rowset LLC, to Anthropic, or to anyone else.

Everything happens on the machine it runs on:

- It **reads** the files you name on the command line, and nothing else
- It **writes** only to `--json-out`, and refuses if that path is one of the inputs
- It takes **no credentials** — it works on exports, not connection strings, so it has no route to a
  production system

One thing to handle with care: your data stays yours, but it is still your data. `--json-out` writes
**natural key values** for every finding — student IDs, order numbers, account numbers, whatever your
key is — and `--show` prints them to the terminal. Treat that output like the exports it came from,
and use `--show 0` when you are screen-sharing.

See [SECURITY.md](SECURITY.md) for how to report a vulnerability.

## Licence

MIT — see [LICENSE](LICENSE).

Rowset LLC · [rowset.co](https://rowset.co) · `mark@rowset.co`
