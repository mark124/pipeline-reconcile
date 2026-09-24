#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Differential test: random scenarios with known injected defects, checked
against reconcile.py's own --json-out.

Every other test in this repo asserts a case somebody thought of. This one
generates scenarios with ground truth known by construction -- loss, stale
re-ships, corruption, orphans, volatile _etag noise at two depths, and values
chosen from the types that have caused bugs before (2.5, "3.10", "007", true,
null) -- and compares the reported finding sets against what was injected.

    python3 tests/fuzz_groundtruth.py [path/to/reconcile.py] [scenarios]

Takes about 30 seconds for the default 300. Writes only into a temp directory.

Contributed by a reviewer during the v1.3 review, and kept because it covers the
whole class rather than the particular bugs already found. Adapted here to use
the running interpreter (so it works on Windows) and a scratch directory.
"""
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TARGET = os.path.join(HERE, os.pardir, "skills", "reconcile", "scripts", "reconcile.py")


def main():
    target = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TARGET)
    scenarios = int(sys.argv[2]) if len(sys.argv) > 2 else 300
    work = tempfile.mkdtemp(prefix="reconcile-fuzz-")
    bad = 0
    try:
        for seed in range(scenarios):
            rnd = random.Random(seed)
            n = rnd.randint(1, 60)
            base = {("K%d" % i): {"k": "K%d" % i,
                                  "v": rnd.choice([1, 2.5, "3.10", "007", True, None, "x"]),
                                  "n": {"id": rnd.randint(1, 9), "_etag": "e%d" % rnd.randint(0, 9)},
                                  "_etag": "t"} for i in range(n)}
            src = {k: json.loads(json.dumps(r)) for k, r in base.items()}

            changed = set(rnd.sample(sorted(src), rnd.randint(0, n // 3)))
            for k in changed:
                src[k]["v"] = "CHG-" + k
            lose = set(rnd.sample(sorted(src), rnd.randint(0, n // 4)))
            stale = set(rnd.sample(sorted(changed - lose), rnd.randint(0, len(changed - lose))))
            corrupt = set(rnd.sample(sorted(set(src) - lose - changed), rnd.randint(0, 3) if n > 10 else 0))
            extra = {"Z%d" % i for i in range(rnd.randint(0, 3))}

            dest = []
            for k, r in src.items():
                if k in lose:
                    continue
                o = json.loads(json.dumps(base[k] if k in stale else r))
                if k in corrupt:
                    o["v"] = "CORRUPT"
                o["_etag"] = "new"
                o["n"]["_etag"] = "new"                  # volatile noise, must be ignored
                dest.append(o)
            dest += [{"k": z} for z in extra]
            rnd.shuffle(dest)
            journal = [{"k": k, "status": "delivered"} for k in src]

            paths = {}
            for name, rows in (("fs", src.values()), ("fd", dest),
                               ("fb", base.values()), ("fj", journal)):
                paths[name] = os.path.join(work, name + ".jsonl")
                with open(paths[name], "w", encoding="utf-8") as fh:
                    fh.write("".join(json.dumps(r) + "\n" for r in rows))
            out = os.path.join(work, "out.json")

            p = subprocess.run(
                [sys.executable, target,
                 "--source", paths["fs"], "--dest", paths["fd"], "--key", "k",
                 "--baseline", paths["fb"], "--journal", paths["fj"],
                 "--ignore", "_etag,*._etag", "--json-out", out],
                capture_output=True, text=True)

            with open(out, encoding="utf-8") as fh:
                f = json.load(fh)
            want = {"missing": sorted(lose), "stale": sorted(stale), "phantom": sorted(lose),
                    "mismatched": sorted(corrupt), "orphaned": sorted(extra)}
            got = {k: f[k] for k in want}
            # Orphans fail the run as of 1.4.0, so they belong in the expected code.
            exp_rc = 1 if (lose or stale or corrupt or extra) else 0

            if got != want or p.returncode != exp_rc:
                bad += 1
                if bad <= 3:
                    diff = {k: (got[k], want[k]) for k in want if got[k] != want[k]}
                    print("  seed %d: rc %d (wanted %d) %s" % (seed, p.returncode, exp_rc, diff))
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print("%d random scenarios, %d disagreements with ground truth" % (scenarios, bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
