#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate a synthetic dataset that exhibits both defects, so the harness can be
demonstrated in thirty seconds without anybody's student data.

The point is that nobody has to grant access or wait on an approval to see the
tool work. They run this, they run reconcile.py, they watch it fail on a pipeline
that reported success.

Nothing here is real. 1,000 synthetic students, deterministic seed.

    python3 make_fixtures.py                # writes ./fixtures/
    python3 make_fixtures.py --out /tmp/f   # anywhere else

Writes only to --out, which defaults to the CURRENT directory, never to the
installed plugin.
"""
import argparse
import json
import os
import random

N = 1000
SEED = 20260723

FIRST = ["Avery", "Jordan", "Riley", "Casey", "Quinn", "Rowan", "Sage", "Emerson",
         "Finley", "Harper", "Marlowe", "Tatum", "Wren", "Ellis", "Blake"]
LAST = ["Okafor", "Nakamura", "Delgado", "Petrov", "Haddad", "Lindqvist", "Mwangi",
        "Castellanos", "Bergeron", "Varga", "Ibrahim", "Sorensen", "Duarte"]


def student(i, rnd, birth_year=2009):
    return {
        "studentUniqueId": "S%06d" % i,
        "firstName": rnd.choice(FIRST),
        "lastSurname": rnd.choice(LAST),
        "birthDate": "%d-%02d-%02d" % (birth_year, rnd.randint(1, 12), rnd.randint(1, 28)),
        "gradeLevelDescriptor": "uri://ed-fi.org/GradeLevelDescriptor#%s Grade"
                                % rnd.choice(["Ninth", "Tenth", "Eleventh", "Twelfth"]),
        "_etag": "%016x" % rnd.getrandbits(64),          # volatile, must be ignored
        "_lastModifiedDate": "2026-07-23T04:00:00Z",      # volatile, must be ignored
    }


def write(outdir, name, rows):
    path = os.path.join(outdir, name)
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    print("  %-26s %5d rows" % (name, len(rows)))
    return path


def main():
    ap = argparse.ArgumentParser(description="Write synthetic reconciliation fixtures.")
    ap.add_argument("--out", default=os.path.join(os.getcwd(), "fixtures"),
                    help="directory to write into (default: ./fixtures)")
    args = ap.parse_args()
    outdir = os.path.abspath(args.out)
    os.makedirs(outdir, exist_ok=True)
    rnd = random.Random(SEED)

    # ---- the PREVIOUS run. Everything landed, everything was correct.
    baseline = [student(i, rnd) for i in range(N)]

    # ---- TODAY's source. 40 students were corrected upstream: a grade-level fix.
    #
    # Only students NOT already in twelfth grade are eligible. An earlier version
    # of this generator ignored that and "corrected" 2 students to the value they
    # already held -- no content change, so nothing for the harness to detect. It
    # reported 15 stale against 17 injected and the harness was right both times.
    # Kept as a comment because it is the exact failure this tool exists to catch:
    # a count you assumed rather than measured.
    TWELFTH = "uri://ed-fi.org/GradeLevelDescriptor#Twelfth Grade"
    eligible = [i for i in range(N) if baseline[i]["gradeLevelDescriptor"] != TWELFTH]
    corrected = set(rnd.sample(eligible, 40))
    source = []
    for i, prev in enumerate(baseline):
        rec = dict(prev)
        if i in corrected:
            rec["gradeLevelDescriptor"] = TWELFTH
            rec["_etag"] = "%016x" % rnd.getrandbits(64)
        source.append(rec)

    # ---- what ACTUALLY landed, with both defects injected.
    #
    # lightbeam#92 : 23 records hit a 429, got written to the journal as
    #                delivered, and were therefore never retried. Gone.
    rate_limited = set(rnd.sample(sorted(set(range(N)) - corrected), 23))
    #
    # earthmover#191 : 17 of the 40 corrections re-shipped the PREVIOUS output.
    #                  The run was green. The grade level is still last week's.
    stale = set(sorted(corrected)[:17])

    dest = []
    for i, rec in enumerate(source):
        if i in rate_limited:
            continue                                   # never landed at all
        out = dict(baseline[i]) if i in stale else dict(rec)
        out["_lastModifiedDate"] = "2026-07-23T05:14:00Z"   # every row looks freshly written
        dest.append(out)

    # ---- the delivery journal. It claims a clean run, and records NO error at
    #      all for the 23 that never landed. That absence is the whole defect: an
    #      earlier version of this fixture also stamped httpStatus 429 on those
    #      rows, which made them trivially findable and so demonstrated a bug
    #      nobody actually has. A log that admits the 429 is a log you can fix
    #      from. lightbeam#92 is the log that does not.
    journal = []
    for i in range(N):
        journal.append({
            "studentUniqueId": "S%06d" % i,
            "status": "delivered",                     # <- the lie, for 23 of them
            "httpStatus": 200,                         # <- and it shows no error
            "timestamp": "2026-07-23T05:14:00Z",
        })

    print("\nfixtures written to %s\n" % outdir)
    write(outdir, "students_source.jsonl", source)
    write(outdir, "students_dest.jsonl", dest)
    write(outdir, "students_baseline.jsonl", baseline)
    write(outdir, "delivery_journal.jsonl", journal)

    rel = os.path.relpath(outdir, os.getcwd())
    print("""
  Injected, and invisible to the pipeline's own success report:
    %2d records rate-limited, logged as delivered, never retried   [lightbeam#92]
    %2d corrections re-shipped as the previous run's output        [earthmover#191]
    %2d corrections landed correctly

  Now run:

    python3 reconcile.py --source %s/students_source.jsonl \\
        --dest %s/students_dest.jsonl --key studentUniqueId \\
        --journal %s/delivery_journal.jsonl \\
        --baseline %s/students_baseline.jsonl
""" % (len(rate_limited), len(stale), len(corrected) - len(stale), rel, rel, rel, rel))


if __name__ == "__main__":
    main()
