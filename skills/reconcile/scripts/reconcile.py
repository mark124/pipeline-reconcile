#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Reconcile what a pipeline was GIVEN against what actually LANDED.

Written against two real defects found in the Ed-Fi ingestion tools in July 2026:

  earthmover#191  a corrected transformation silently re-ships stale output
  lightbeam#92    a record that gets a 429 is written to the idempotency log as
                  though it were delivered, so it is never re-sent

Both have the same shape: THE RUN REPORTS SUCCESS WHILE RECORDS DO NOT LAND, and
nothing downstream can tell. A green checkmark is not evidence of delivery. This
asserts the thing the green checkmark is supposed to mean.

Design constraints, because these are what let someone actually run it:
  - stdlib only. No install, no virtualenv, no dependency review.
  - READ ONLY. It opens files. It never writes to a source or a destination.
  - NO PRODUCTION CREDENTIALS. It takes exports, not connection strings.
  - It FAILS. Exit code 1 on any finding, so it can sit in CI and block a release.
    Orphans are a finding too; --allow-orphans excuses them for incremental loads.
  - Every finding names the key, so "we lost 12 records" becomes 12 rows you can
    paste into a ticket.

Usage
-----
    python3 reconcile.py --source src.jsonl --dest dst.jsonl --key studentUniqueId

    python3 reconcile.py \
        --source students_src.jsonl \
        --dest   students_ods.jsonl \
        --key    studentUniqueId \
        --journal delivery_log.jsonl \
        --baseline previous_run_dest.jsonl

(On Windows the interpreter is usually `python`, not `python3`.)

Inputs are JSONL or CSV, one record per line/row. --key may be repeated to build
a composite natural key, which is what most Ed-Fi resources actually need.

What it checks
--------------
  1. COUNT        source rows vs destination rows
  2. MISSING      keys present in source, absent downstream          <- the loss
  3. MISMATCHED   key present both sides, content differs            <- corruption
  4. STALE        destination content matches the PREVIOUS run while
                  the source has changed                            <- #191
  5. PHANTOM      keys the journal marks delivered that are not downstream  <- #92
  6. DUPLICATE    the same natural key twice, in source or destination
  7. UNREADABLE   source rows that could not be parsed, keyed or aligned

Comparing values
----------------
Values are normalised before hashing so that 5, 5.0 and "5" compare equal --
without this a CSV export never matches a JSON one and every row reports as
changed. The normalisation is EXACT: integers go through int() and decimals
through Decimal, never float, so a 20-digit account number keeps all 20 digits.
An earlier version used float() here and two different 20-digit IDs collapsed to
the same value, which reported clean. Leading zeros are preserved, so "007" is
NOT 7, and so are TRAILING zeros, so the section code "3.10" is NOT "3.1".
"True" and true agree. Empty and absent fields are treated alike, because CSV
cannot express the difference.

One deliberate consequence: "5.50" and 5.5 report as MISMATCHED even though they
are numerically equal. This tool errs toward a finding you can dismiss in a
second rather than a silent match you will never see.

Scoping --ignore
----------------
A bare name (`_etag`) is stripped at the TOP LEVEL ONLY. Use `*._etag` to strip
it at every depth, or a dotted path (`meta.updated_at`) for one exact location.
The default deliberately keeps `id` top-level-only: a nested `id` is usually a
reference to another entity, and stripping those hides a student being moved to
a different school.

Exit codes
----------
  0  clean
  1  findings: missing, mismatched, stale, phantom, duplicate, orphaned, or a row
     that could not be read
  2  the check could not run meaningfully: a missing file, an EMPTY source,
     baseline or journal, a journal whose key never matched, --json-out aimed
     at one of the inputs, or a crash

Exit 2 exists because every one of those states used to print a clean result. A
crashed export leaves a 0-byte file, and comparing nothing against nothing
reports that everything arrived.
"""
import argparse
import copy
import csv
import hashlib
import json
import os
import re
import sys
from collections import Counter
from decimal import Decimal, InvalidOperation

__version__ = "1.4.2"

# ASCII unit separator. Joining composite keys on a printable character lets
# ("a|b", "c") collide with ("a", "b|c"); this cannot appear in normal data, and
# is escaped if it somehow does.
KEY_SEP = "\x1f"

# Canonical form only, so "007" stays "007" and never merges with 7.
_INT_RE = re.compile(r"^-?(?:0|[1-9][0-9]*)$")
_DEC_RE = re.compile(r"^-?(?:0|[1-9][0-9]*)\.[0-9]+$")
_HTTP_ERR = re.compile(r"^[45][0-9][0-9]$")

ENVELOPE_FIELDS = ("items", "data", "results", "value", "records")
CSV_EXTRA = "__extra_columns__"

DEFAULT_IGNORE = "id,*._etag,*._lastModifiedDate,*.lastModifiedDate"


# ---------------------------------------------------------------- normalising

def _dec_str(d):
    """Plain decimal text, never scientific notation.

    Trailing zeros are NOT dropped. "3.10" and "3.1" are different section codes,
    standard numbers and software versions, and normalising them together merged
    two distinct keys into one in 1.2.0 and reported a silent match. Only an
    integral value canonicalises, so 5, 5.0 and "5" still agree.
    """
    if d == d.to_integral_value():
        return str(int(d))
    return format(d, "f")


def _scalar(v):
    """One canonical text form for a value, so CSV and JSON agree.

    5, 5.0 and "5" all become "5". "007" stays "007". "True" and true both become
    "true". None and "" both become "", because a CSV blank and an absent JSON
    field mean the same thing in practice.

    Nothing here goes through float(). A 20-digit identifier must survive intact.
    """
    if v is None:
        return ""
    if isinstance(v, bool):                 # bool before int: True is an int
        return "true" if v else "false"
    if isinstance(v, int):                  # Python ints are exact at any size
        return str(v)
    if isinstance(v, Decimal):
        return _dec_str(v)
    if isinstance(v, float):                # only if a caller hands us one
        return _dec_str(Decimal(repr(v)))
    if isinstance(v, str):
        s = v.strip()
        low = s.lower()
        if low in ("true", "false"):
            return low
        if _INT_RE.match(s):
            return str(int(s))              # exact, no float
        if _DEC_RE.match(s):
            try:
                d = Decimal(s)
            except InvalidOperation:
                return s
            if d == d.to_integral_value():
                return str(int(d))      # "5.0" agrees with 5
            return s                    # "3.10" stays "3.10"
        return s
    return str(v)


def parse_ignore(spec):
    """Split --ignore into top-level names, any-depth names, and exact paths."""
    top, anydepth, paths = set(), set(), []
    for raw in spec.split(","):
        x = raw.strip()
        if not x:
            continue
        if x.startswith("*."):
            anydepth.add(x[2:])
        elif "." in x:
            paths.append(x)
        else:
            top.add(x)
    return top, anydepth, paths


def _strip_paths(obj, paths):
    """Remove exact dotted paths (meta.updated_at) from a copy of the record."""
    if not paths:
        return obj
    out = copy.deepcopy(obj)
    for path in paths:
        parts = path.split(".")
        cur = out
        for p in parts[:-1]:
            if isinstance(cur, dict) and p in cur:
                cur = cur[p]
            else:
                cur = None
                break
        if isinstance(cur, dict):
            cur.pop(parts[-1], None)
    return out


def _normalize(obj, anydepth, top_ignore, top=True):
    """Recursively canonicalise a record and drop volatile / empty fields."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in anydepth:
                continue
            if top and k in top_ignore:
                continue
            nv = _normalize(v, anydepth, top_ignore, False)
            if nv == "":                     # absent and empty are the same thing
                continue
            out[k] = nv
        return out
    if isinstance(obj, list):
        return [_normalize(x, anydepth, top_ignore, False) for x in obj]
    return _scalar(obj)


def content_hash(rec, top_ignore, anydepth, paths):
    trimmed = _normalize(_strip_paths(rec, paths), anydepth, top_ignore)
    canonical = json.dumps(trimmed, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- loading

def dig(rec, dotted):
    """Fetch a possibly nested field: studentReference.studentUniqueId."""
    cur = rec
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _is_envelope(obj, keyfields):
    """True only for an API page wrapper, never for a record that owns the key.

    Before 1.1.0 any object with an "items" list was unwrapped and replaced by its
    contents, so an order record {"order_id":..., "items":[...]} vanished from BOTH
    sides and the run exited clean having compared nothing at all.
    """
    if keyfields and any(dig(obj, k) is not None for k in keyfields):
        return False
    for f in ENVELOPE_FIELDS:
        if isinstance(obj.get(f), list):
            return True
    return False


def _envelope_rows(obj):
    for f in ENVELOPE_FIELDS:
        if isinstance(obj.get(f), list):
            return [x for x in obj[f] if isinstance(x, dict)]
    return []


def load(path, label, keyfields=None):
    """Read JSONL or CSV into a list of dicts. Returns (rows, unreadable, fmt)."""
    if not os.path.exists(path):
        sys.stderr.write("ERROR: %s file not found: %s\n" % (label, path))
        sys.exit(2)

    rows, bad, ragged, unwrapped = [], 0, 0, 0
    fmt = "csv" if path.lower().endswith(".csv") else "jsonl"
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        if fmt == "csv":
            # restkey keeps a ragged row from inserting a None key, which used to
            # crash the content hash on sort_keys and surface as "findings".
            for r in csv.DictReader(fh, restkey=CSV_EXTRA):
                d = dict(r)
                if CSV_EXTRA in d or None in d:
                    ragged += 1             # column alignment is untrustworthy
                    continue
                rows.append(d)
        else:
            for n, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    # parse_float=Decimal so a long decimal never loses digits
                    obj = json.loads(line, parse_float=Decimal)
                except ValueError:
                    bad += 1
                    if bad <= 3:
                        sys.stderr.write("  warn: %s line %d is not valid JSON\n" % (label, n))
                    continue
                if isinstance(obj, list):
                    rows.extend(x for x in obj if isinstance(x, dict))
                elif isinstance(obj, dict):
                    if _is_envelope(obj, keyfields):
                        rows.extend(_envelope_rows(obj))
                        unwrapped += 1
                    else:
                        rows.append(obj)
    if bad:
        sys.stderr.write("  warn: %s had %d unparseable lines\n" % (label, bad))
    if ragged:
        sys.stderr.write("  warn: %s had %d CSV rows with the wrong column count\n"
                         % (label, ragged))
    if unwrapped:
        sys.stderr.write("  note: %s: unwrapped %d API page envelope(s)\n" % (label, unwrapped))
    return rows, bad + ragged, fmt


def make_key(rec, keyfields):
    parts = []
    for k in keyfields:
        s = _scalar(dig(rec, k))
        if s == "":
            return None
        parts.append(s.replace(KEY_SEP, "\\x1f"))
    return KEY_SEP.join(parts)


def show_key(k):
    return k.replace(KEY_SEP, " + ")


def index_by_key(rows, keyfields, label, top_ignore, anydepth, paths):
    """Map natural key -> (record, content hash). Reports duplicate keys."""
    out, dupes, keyless = {}, Counter(), 0
    for rec in rows:
        k = make_key(rec, keyfields)
        if k is None:
            keyless += 1
            continue
        if k in out:
            dupes[k] += 1
        out[k] = (rec, content_hash(rec, top_ignore, anydepth, paths))
    if keyless:
        sys.stderr.write("  warn: %s had %d rows with a missing/blank key field\n"
                         % (label, keyless))
    return out, dupes, keyless


# ---------------------------------------------------------------- journal

# Deliberately specific. Generic names like "code" and "response" were here in
# 1.1.0 and a course code of "ALG1" was read as a delivery failure, which moved
# the record out of PHANTOM and hid the headline finding.
STATUS_FIELDS = ("status", "result", "state", "outcome", "http", "httpStatus",
                 "http_status", "statusCode", "status_code", "responseCode")
OK_VALUES = ("delivered", "ok", "success", "sent", "complete", "completed",
             "created", "updated", "upserted", "accepted", "200", "201", "202", "204")
FAIL_HINTS = ("error", "failed", "failure", "timeout", "retry", "throttl",
              "reject", "abort", "skipped", "dropped", "429")


def journal_verdict(rec):
    """Classify one delivery-log row: "ok", "fail", "unknown" or "unlabelled".

    An unrecognised value is NOT a failure. A journal entry exists because
    something claimed to deliver; only an explicit failure signal overturns that.
    Treating unknown as failure hid real phantom deliveries in 1.1.0.
    """
    seen, explicit_ok = False, False
    for f in STATUS_FIELDS:
        if f not in rec:
            continue
        seen = True
        v = _scalar(rec[f]).lower()
        if v == "":
            continue
        if v in OK_VALUES:
            explicit_ok = True
            continue
        if _HTTP_ERR.match(v) or any(h in v for h in FAIL_HINTS):
            return "fail"
    if not seen:
        return "unlabelled"
    return "ok" if explicit_ok else "unknown"


# ---------------------------------------------------------------- reporting

def show(title, keys, limit, detail=None):
    print("\n  %s: %d" % (title, len(keys)))
    if not keys:
        return
    for k in sorted(keys)[:limit]:
        extra = ("   %s" % detail[k]) if detail and k in detail else ""
        print("      %s%s" % (show_key(k), extra))
    if len(keys) > limit:
        print("      ... and %d more (raise --show to see them)" % (len(keys) - limit))


def main():
    ap = argparse.ArgumentParser(
        description="Prove what landed. Exit 1 if anything is missing, changed or unverifiable.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", required=True, help="what the pipeline was given (JSONL or CSV)")
    ap.add_argument("--dest", required=True, help="what is actually in the ODS/warehouse")
    ap.add_argument("--key", required=True, action="append",
                    help="natural key field; repeat for a composite key. Dotted paths allowed.")
    ap.add_argument("--journal", help="delivery/idempotency log to audit (catches lightbeam#92)")
    ap.add_argument("--journal-key", help="key field inside the journal (defaults to --key)")
    ap.add_argument("--baseline", help="PREVIOUS run's destination (catches earthmover#191)")
    ap.add_argument("--ignore", default=DEFAULT_IGNORE,
                    help="volatile fields excluded from content comparison. A bare name is "
                         "top-level only; *.name strips at every depth; a.b.c is one exact "
                         "path. Default: " + DEFAULT_IGNORE)
    ap.add_argument("--show", type=int, default=20, help="how many offending keys to print")
    ap.add_argument("--json-out", help="write the full finding set here, for a ticket")
    ap.add_argument("--allow-unreadable", action="store_true",
                    help="do not fail the run on source rows that could not be read")
    ap.add_argument("--allow-orphans", action="store_true",
                    help="do not fail on keys found downstream but not in the source. Use for "
                         "an incremental load, where the destination holds more history than "
                         "today's extract.")
    ap.add_argument("--allow-empty", action="store_true",
                    help="permit an input file with zero rows. Without this an empty source, "
                         "baseline or journal exits 2, because a check against nothing reports "
                         "clean.")
    args = ap.parse_args()

    top_ignore, anydepth, paths = parse_ignore(args.ignore)
    keyfields = args.key
    HASH = (top_ignore, anydepth, paths)

    # ---- this tool promises it never writes to a source or a destination, and
    #      --json-out is the only thing it writes. A typo there would overwrite an
    #      export with the finding set, so refuse before anything is read.
    if args.json_out:
        out = os.path.realpath(args.json_out)
        for flag, path in (("--source", args.source), ("--dest", args.dest),
                           ("--baseline", args.baseline), ("--journal", args.journal)):
            if path and os.path.realpath(path) == out:
                sys.stderr.write(
                    "ERROR: --json-out is the same file as %s (%s).\n"
                    "       Writing there would destroy an input. Nothing was read or written.\n"
                    % (flag, args.json_out))
                return 2

    src_rows, src_bad, src_fmt = load(args.source, "source", keyfields)
    dst_rows, dst_bad, dst_fmt = load(args.dest, "dest", keyfields)
    base_rows, jrows, j_unreadable = None, None, 0
    if args.baseline:
        base_rows, _, _ = load(args.baseline, "baseline", keyfields)
    if args.journal:
        jrows, j_unreadable, _ = load(args.journal, "journal", keyfields)

    # ---- an input with no rows means the comparison ran against nothing, and a
    #      comparison against nothing reports clean. A crashed export job leaves a
    #      0-byte file, which is the commonest real version of exactly that.
    #      An empty DESTINATION is not caught here: that is total loss, and every
    #      source key reports MISSING, which is the correct answer.
    if not args.allow_empty:
        empty = []
        if not src_rows:
            empty.append("--source %s" % args.source)
        if args.baseline and not base_rows:
            empty.append("--baseline %s" % args.baseline)
        if args.journal and not jrows:
            empty.append("--journal %s" % args.journal)
        if empty:
            sys.stderr.write(
                "ERROR: no rows in %s.\n"
                "       Checking against an empty file reports clean, which is how a failed\n"
                "       export passes CI. Pass --allow-empty if this is genuinely expected.\n"
                % ", ".join(empty))
            return 2

    src, src_dupes, src_keyless = index_by_key(src_rows, keyfields, "source", *HASH)
    dst, dst_dupes, dst_keyless = index_by_key(dst_rows, keyfields, "dest", *HASH)

    print("=" * 74)
    print("  RECONCILIATION  v%s" % __version__)
    print("=" * 74)
    print("  key           : %s" % " + ".join(keyfields))
    print("  source        : %-7d rows  ->  %d distinct keys  (%s)" % (len(src_rows), len(src), src_fmt))
    print("  destination   : %-7d rows  ->  %d distinct keys  (%s)" % (len(dst_rows), len(dst), dst_fmt))

    if src_fmt != dst_fmt:
        print("\n  NOTE: comparing %s against %s. Values are normalised so 5, 5.0 and \"5\"" % (src_fmt, dst_fmt))
        print("        match, but CSV cannot carry nested structure. Content findings across")
        print("        formats cover only the fields both sides can express.")

    if not src and src_rows:
        print("\n  WARNING: %d source rows produced NO usable keys. Check --key." % len(src_rows))

    src_keys, dst_keys = set(src), set(dst)
    missing = src_keys - dst_keys           # the loss
    orphaned = dst_keys - src_keys
    shared = src_keys & dst_keys

    # ---- content comparison across every shared key. Before 1.1.0 this ran ONLY
    #      when --baseline was supplied, so a record whose value had been corrupted
    #      in transit exited 0 and reported "present downstream, and current".
    changed = [k for k in shared if src[k][1] != dst[k][1]]

    stale, mismatched = {}, {}
    if args.baseline:
        base, _, _ = index_by_key(base_rows, keyfields, "baseline", *HASH)
        for k in changed:
            if k in base and dst[k][1] == base[k][1]:
                stale[k] = "dest still == previous run"
            else:
                mismatched[k] = "source and dest differ"
    else:
        for k in changed:
            mismatched[k] = "source and dest differ"

    findings = {
        "missing": sorted(show_key(k) for k in missing),
        "mismatched": sorted(show_key(k) for k in mismatched),
        "stale": sorted(show_key(k) for k in stale),
        "orphaned": sorted(show_key(k) for k in orphaned),
        "duplicate_source_keys": sorted(show_key(k) for k in src_dupes),
        "duplicate_dest_keys": sorted(show_key(k) for k in dst_dupes),
        "phantom": [],
        "journal_failed": [],
        "unreadable_source_rows": src_bad + src_keyless,
        "baseline_used": bool(args.baseline),
    }

    show("MISSING downstream (records that did not land)", missing, args.show)
    show("MISMATCHED (present both sides, content differs)", set(mismatched), args.show, mismatched)
    if args.baseline:
        show("STALE (source changed, destination is last run's output)  [earthmover#191]",
             set(stale), args.show, stale)
    show("ORPHANED downstream (never in source)%s"
         % ("  [not counted: --allow-orphans]" if args.allow_orphans else ""),
         orphaned, args.show)
    if src_dupes:
        show("DUPLICATE natural keys in SOURCE (one will silently win)", set(src_dupes), args.show)
    if dst_dupes:
        show("DUPLICATE natural keys in DESTINATION (double load?)", set(dst_dupes), args.show)

    # ---- lightbeam#92: the journal says delivered, the ODS disagrees.
    phantom, jfailed, journal_unchecked = set(), set(), 0
    if args.journal:
        jkey = [args.journal_key] if args.journal_key else keyfields
        delivered, unlabelled, unknown, j_keyless = set(), 0, 0, 0
        for rec in jrows:
            k = make_key(rec, jkey)
            if k is None:
                j_keyless += 1
                continue
            verdict = journal_verdict(rec)
            if verdict == "fail":
                if k not in dst_keys:
                    jfailed.add(k)          # known-failed and still not downstream
            else:
                if verdict == "unlabelled":
                    unlabelled += 1
                elif verdict == "unknown":
                    unknown += 1
                delivered.add(k)
        # A phantom check that matched no journal row at all has checked nothing,
        # and "PHANTOM: 0" would read as evidence of a clean log. Refuse instead.
        if jrows and j_keyless == len(jrows):
            sys.stderr.write(
                "ERROR: no journal row has a value for %s.\n"
                "       The phantom check would run against zero rows and report clean.\n"
                "       Pass --journal-key with the field name the journal actually uses.\n"
                % " + ".join(jkey))
            return 2

        phantom = delivered - dst_keys
        findings["phantom"] = sorted(show_key(k) for k in phantom)
        findings["journal_failed"] = sorted(show_key(k) for k in jfailed)
        findings["unreadable_journal_rows"] = j_keyless + j_unreadable
        journal_unchecked = j_keyless + j_unreadable
        print("\n  journal       : %d rows, %d read as delivery claims" % (len(jrows), len(delivered)))
        if j_keyless or j_unreadable:
            print("     %d rows could not be keyed or read, so they were never checked"
                  % (j_keyless + j_unreadable))
            print("     against the destination. Check --journal-key.")
        if unlabelled:
            print("     %d rows carry NO recognised status field" % unlabelled)
        if unknown:
            print("     %d rows carry a status this tool does not recognise" % unknown)
        if unlabelled or unknown:
            print("     Both were read as delivery claims, because a journal entry exists to")
            print("     record an attempt. If that is wrong here, the phantom check is not")
            print("     meaningful against this log.")
        show("PHANTOM DELIVERIES (journal says delivered, not in destination)  [lightbeam#92]",
             phantom, args.show)
        show("JOURNAL RECORDS A FAILURE, still not downstream (the retry backlog)",
             jfailed, args.show)

    unreadable = src_bad + src_keyless
    dest_unreadable = dst_bad + dst_keyless
    findings["unreadable_dest_rows"] = dest_unreadable

    # A destination row that could not be read or keyed makes its source record
    # look MISSING, which is the right verdict for the wrong reason. Say so.
    if dest_unreadable:
        print("\n  NOTE: %d DESTINATION rows could not be read or keyed. Any of the %d"
              % (dest_unreadable, len(missing)))
        print("        MISSING keys above may in fact have landed in one of them.")

    fail = (len(missing) + len(mismatched) + len(stale) + len(phantom)
            + len(src_dupes) + len(dst_dupes))
    if not args.allow_orphans:
        fail += len(orphaned)
    if not args.allow_unreadable:
        fail += unreadable + dest_unreadable + journal_unchecked

    print("\n" + "=" * 74)
    if fail:
        pct = 100.0 * len(missing) / len(src) if src else 0.0
        print("  RESULT: FINDINGS")
        print("     %d missing (%.2f%% of source) | %d mismatched | %d stale | %d phantom"
              % (len(missing), pct, len(mismatched), len(stale), len(phantom)))
        if orphaned and not args.allow_orphans:
            print("     %d orphaned (pass --allow-orphans if the destination keeps history)"
                  % len(orphaned))
        if src_dupes or dst_dupes:
            print("     %d duplicate source keys | %d duplicate destination keys"
                  % (len(src_dupes), len(dst_dupes)))
        if unreadable or dest_unreadable or journal_unchecked:
            print("     %d source / %d destination / %d journal rows could not be read,"
                  % (unreadable, dest_unreadable, journal_unchecked))
            print("     so they were never checked")
            if args.allow_unreadable:
                print("     (not counted: --allow-unreadable)")
    else:
        if args.baseline:
            print("  RESULT: every source key is present downstream, matches the source,")
            print("          and is not last run's output.")
        else:
            print("  RESULT: every source key is present downstream and matches the source.")
            print("          No --baseline given, so a stale re-ship CANNOT be ruled out.")
    print("=" * 74)

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(findings, fh, indent=2)
        print("  findings written to %s" % args.json_out)

    return 1 if fail else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except KeyboardInterrupt:
        sys.exit(2)
    except Exception as exc:
        # A crash must never look like a finding. Exit 2, not 1.
        sys.stderr.write("ERROR: reconcile failed: %s: %s\n" % (type(exc).__name__, exc))
        sys.exit(2)
