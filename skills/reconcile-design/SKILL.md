---
name: reconcile-design
description: Work out what a reconciliation check for a specific pipeline should actually compare - the natural key, the volatile fields, where the delivery log lives, and what "the same record" means. Use before running a reconciliation, or when someone wants to add data-loss detection to a pipeline that has none.
---

# Design a reconciliation check for this pipeline

Running the check is easy. Knowing **what to compare** is the part that takes judgement, and getting
it wrong produces either a wall of false positives or a clean report that means nothing.

Work through these with the person. Do not guess — the answers are specific to their system.

## 1. What is the natural key?

Not the surrogate ID. The identifier that means "this is the same real-world thing" on both sides.

- A surrogate `id` assigned by the destination **cannot** be the key — the source has never seen it
- Composite keys are normal: `studentUniqueId` + `schoolYear`, `order_id` + `line_number`
- If the source has no stable key, that is itself the finding. **Say so.** A pipeline whose records
  cannot be identified across the boundary cannot be reconciled, and that is worth knowing before
  anyone spends money on tooling.

⚠️ Check for duplicate natural keys in the source. If the same key appears twice, one will silently
win downstream and nobody is told which.

## 2. Which fields are volatile?

Fields that change on every write and carry no meaning about the content. These must be excluded
from content comparison or every record looks modified.

Common ones: `_etag`, `_lastModifiedDate`, `updated_at`, `ingested_at`, `batch_id`, ETL run IDs,
surrogate keys, and anything holding the current timestamp.

Ask: *"if this field changed but nothing else did, would anyone care?"* If no, exclude it.

## 3. Is there a delivery log, and does anyone trust it?

Many pipelines keep an idempotency or delivery journal so work is not repeated. That log is
**worth auditing specifically**, because if it records a delivery that did not happen, the record
is never retried and the error is permanent and silent.

Ask:
- Does the loader keep a record of what it sent?
- Is an entry written **before or after** the destination confirms? Writing before is the bug.
- What happens on a 429, a timeout, or a partial batch failure?

## 4. Is there a previous run to compare against?

Detecting stale re-ships needs the destination as it was **before** this run. A snapshot, a backup,
or yesterday's export all work. Without one you can still detect missing records — you just cannot
tell a correct unchanged record from a stale one.

## 5. What can you actually get read access to?

The check needs exports, not credentials. Usually the lowest-friction path is:

- a source extract the pipeline already produces
- a destination export, an API pull, or a read replica query
- the delivery log, if one exists

**Read-only, no production credentials.** If someone is offering write access or a production login,
that is more than this needs.

## 6. What deadline does this attach to?

★ This is the question that decides whether the work happens. Silent data loss is invisible by
definition, so nobody has it on a schedule. It gets funded when it attaches to a deadline that
already exists — a state submission window, a go-live, an audit, a contract renewal, a migration
cutover. Ask what is coming up.

## Then

Once those are answered, run the `reconcile` skill with the flags they imply. If the answers reveal
there is no stable key, or no delivery log, or no prior snapshot, **say which checks are therefore
impossible** rather than running a reduced check and reporting it as clean.

A check that cannot fail is not a check.
