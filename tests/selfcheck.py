#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run the commands the README actually prints, then the regression probes.

Two rounds of review found "the documented demo does not run as written". Proof-
reading did not fix that twice, so the demo is now executed rather than read: the
shell block is lifted out of README.md and run in a scratch directory.

    python3 tests/selfcheck.py

Exit 0 if everything passes. stdlib only, writes only into a temp directory.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
failures = []


def check(name, ok, detail=""):
    print("  %-46s %s%s" % (name, "pass" if ok else "FAIL", ("  " + detail) if detail and not ok else ""))
    if not ok:
        failures.append(name)


def run(args, cwd):
    p = subprocess.run([PY] + args, cwd=cwd, capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def demo_block(path):
    """The first fenced block in a doc that invokes make_fixtures."""
    text = open(os.path.join(ROOT, path), encoding="utf-8").read()
    for block in re.findall(r"```\n(.*?)```", text, re.S):
        if "make_fixtures.py" in block:
            return block
    return None


def test_doc_claims():
    """Claims the docs make that the code must still honour.

    Each entry is (file, phrase, must_be_present). Two consecutive reviews found
    documentation that described a previous version's behaviour, and SKILL.md is
    what Claude reads to choose flags -- a stale line there makes Claude pass the
    wrong ones.
    """
    claims = [
        ("skills/reconcile/SKILL.md", "Bare names are stripped at every depth", False),
        ("skills/reconcile/SKILL.md", "top level only", True),
        ("skills/reconcile/SKILL.md", "`*._etag`", True),
        ("skills/reconcile/SKILL.md", "--journal-key", True),
        ("README.md", "MISMATCHED", True),
        ("README.md", "--allow-orphans", True),
        ("skills/reconcile/SKILL.md", "--allow-orphans", True),
        ("skills/reconcile/SKILL.md", "--allow-empty", True),
        # The old unqualified promise; orphans and exit 2 now need saying.
        ("README.md", "**exits 1 on any finding** — so it can", False),
    ]
    for path, phrase, want in claims:
        text = open(os.path.join(ROOT, path), encoding="utf-8").read()
        present = phrase in text
        check("%s %s %r" % (os.path.basename(path), "states" if want else "no longer says", phrase[:44]),
              present == want)

    # The default in the docs must be the default in the code.
    sys.path.insert(0, os.path.join(ROOT, "skills", "reconcile", "scripts"))
    from reconcile import DEFAULT_IGNORE
    skill = open(os.path.join(ROOT, "skills", "reconcile", "SKILL.md"), encoding="utf-8").read()
    check("SKILL.md quotes the real --ignore default", DEFAULT_IGNORE in skill,
          "code says %r" % DEFAULT_IGNORE)

    # CRLF must be read in BINARY. Python's text mode rewrites \r\n to \n, so a
    # normal open() cannot see this at all -- which is why it survived five
    # reviews. A SKILL.md whose frontmatter opens "---\r\n" may not parse, and
    # then the skill never loads and nothing else here would notice.
    crlf = []
    for root, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", "fixtures", ".git")]
        for f in files:
            p = os.path.join(root, f)
            with open(p, "rb") as fh:
                if b"\r\n" in fh.read():
                    crlf.append(os.path.relpath(p, ROOT))
    check("no file has CRLF line endings", not crlf, ", ".join(crlf[:4]))
    check(".gitattributes pins eol=lf", os.path.exists(os.path.join(ROOT, ".gitattributes")))


def test_demo(tmp, doc, label):
    block = demo_block(doc)
    if block is None:
        check("%s demo block found" % label, False, "no fenced block calls make_fixtures.py")
        return
    work = os.path.join(tmp, label.lower().replace(".", "").replace(" ", "-"))
    shutil.copytree(os.path.join(ROOT, "skills"), os.path.join(work, "skills"))

    # Join backslash continuations, drop comments, and run each line as written.
    block = block.replace("${CLAUDE_PLUGIN_ROOT}/", "").replace("$CLAUDE_PLUGIN_ROOT/", "")
    lines = [l for l in block.replace("\\\n", " ").splitlines() if l.strip() and not l.strip().startswith("#")]
    last_rc, last_out = 0, ""
    for line in lines:
        argv = line.split()
        if argv and argv[0] in ("python3", "python"):
            argv = argv[1:]
        last_rc, last_out = run(argv, work)
        if "make_fixtures" in line:
            check("%s: make_fixtures.py runs as written" % label, last_rc == 0, last_out.strip()[-160:])

    check("%s: reconcile.py runs as written" % label,
          "Traceback" not in last_out and last_rc in (0, 1), last_out.strip()[-200:])
    check("%s: exits 1 on the seeded defects" % label, last_rc == 1)
    for want in ("23 missing", "17 stale", "23 phantom"):
        check("%s: reports %s" % (label, want), want in last_out.replace("|", " "),
              last_out.strip()[-200:])


def test_probes(tmp):
    """Every bug found in review, as an executable regression."""
    work = os.path.join(tmp, "probes")
    os.makedirs(work)
    rec = os.path.join(ROOT, "skills", "reconcile", "scripts", "reconcile.py")

    def w(name, text):
        with open(os.path.join(work, name), "w", encoding="utf-8") as fh:
            fh.write(text)

    cases = [
        # name,                       files,                                         argv,                                              want
        ("corrupted content, no baseline",
         {"a1.jsonl": '{"k":"A","amt":100}\n', "a2.jsonl": '{"k":"A","amt":0}\n'},
         ["--source", "a1.jsonl", "--dest", "a2.jsonl", "--key", "k"], 1),
        ("record owning an items list is kept",
         {"b1.jsonl": '{"oid":"B","items":[{"s":1}]}\n', "b2.jsonl": '{"oid":"B","items":[{"s":1}]}\n'},
         ["--source", "b1.jsonl", "--dest", "b2.jsonl", "--key", "oid"], 0),
        ("unparseable source row fails the run",
         {"c1.jsonl": '{"k":"C"}\n{NOPE\n', "c2.jsonl": '{"k":"C"}\n'},
         ["--source", "c1.jsonl", "--dest", "c2.jsonl", "--key", "k"], 1),
        ("duplicate destination key fails the run",
         {"d1.jsonl": '{"k":"D"}\n', "d2.jsonl": '{"k":"D"}\n{"k":"D"}\n'},
         ["--source", "d1.jsonl", "--dest", "d2.jsonl", "--key", "k"], 1),
        ("csv vs jsonl, unchanged, is clean",
         {"e1.csv": "k,amt\nE,5\n", "e2.jsonl": '{"k":"E","amt":5}\n'},
         ["--source", "e1.csv", "--dest", "e2.jsonl", "--key", "k"], 0),
        ("composite key cannot collide",
         {"f1.jsonl": '{"a":"x|y","b":"z"}\n', "f2.jsonl": '{"a":"x","b":"y|z"}\n'},
         ["--source", "f1.jsonl", "--dest", "f2.jsonl", "--key", "a", "--key", "b"], 1),
        ("1 matches 1.0",
         {"g1.jsonl": '{"k":1}\n', "g2.jsonl": '{"k":1.0}\n'},
         ["--source", "g1.jsonl", "--dest", "g2.jsonl", "--key", "k"], 0),
        ("20-digit ids keep every digit",
         {"i1.jsonl": '{"k":"12345678901234567891"}\n', "i2.jsonl": '{"k":"12345678901234567892"}\n'},
         ["--source", "i1.jsonl", "--dest", "i2.jsonl", "--key", "k"], 1),
        ("nested id is content, not noise",
         {"j1.jsonl": '{"k":"J","school":{"id":101}}\n', "j2.jsonl": '{"k":"J","school":{"id":999}}\n'},
         ["--source", "j1.jsonl", "--dest", "j2.jsonl", "--key", "k"], 1),
        ("ragged csv row does not crash",
         {"k1.csv": "k,n\nK,a\nK2,b,EXTRA\n", "k2.jsonl": '{"k":"K","n":"a"}\n{"k":"K2","n":"b"}\n'},
         ["--source", "k1.csv", "--dest", "k2.jsonl", "--key", "k"], 1),
        ("csv True matches json true",
         {"m1.csv": "k,on\nM,True\n", "m2.jsonl": '{"k":"M","on":true}\n'},
         ["--source", "m1.csv", "--dest", "m2.jsonl", "--key", "k"], 0),
        ("*.name opts into any-depth ignore",
         {"h1.jsonl": '{"k":"H","meta":{"_etag":"a"}}\n', "h2.jsonl": '{"k":"H","meta":{"_etag":"b"}}\n'},
         ["--source", "h1.jsonl", "--dest", "h2.jsonl", "--key", "k", "--ignore", "*._etag"], 0),
        ("bare name does not strip nested fields",
         {"h1.jsonl": '{"k":"H","meta":{"_etag":"a"}}\n', "h2.jsonl": '{"k":"H","meta":{"_etag":"b"}}\n'},
         ["--source", "h1.jsonl", "--dest", "h2.jsonl", "--key", "k", "--ignore", "_etag"], 1),
        ("section code 3.10 -> 3.1 is a change",
         {"q1.jsonl": '{"k":"Q","section":"3.10"}\n', "q2.jsonl": '{"k":"Q","section":"3.1"}\n'},
         ["--source", "q1.jsonl", "--dest", "q2.jsonl", "--key", "k"], 1),
        ("keys 3.10 and 3.1 are different keys",
         {"r1.jsonl": '{"k":"3.10"}\n', "r2.jsonl": '{"k":"3.1"}\n'},
         ["--source", "r1.jsonl", "--dest", "r2.jsonl", "--key", "k"], 1),
        ("5.0 still agrees with 5",
         {"s1.jsonl": '{"k":"S","n":5.0}\n', "s2.jsonl": '{"k":"S","n":5}\n'},
         ["--source", "s1.jsonl", "--dest", "s2.jsonl", "--key", "k"], 0),
        ("unkeyable destination row is reported, not just MISSING",
         {"t1.jsonl": '{"k":"T1"}\n', "t2.jsonl": '{"nokey":"T1"}\n'},
         ["--source", "t1.jsonl", "--dest", "t2.jsonl", "--key", "k"], 1),
        ("orphan-only run fails by default",
         {"v1.jsonl": '{"k":"V1"}\n', "v2.jsonl": '{"k":"V1"}\n{"k":"ORPHAN"}\n'},
         ["--source", "v1.jsonl", "--dest", "v2.jsonl", "--key", "k"], 1),
        ("--allow-orphans excuses them",
         {"v1.jsonl": '{"k":"V1"}\n', "v2.jsonl": '{"k":"V1"}\n{"k":"ORPHAN"}\n'},
         ["--source", "v1.jsonl", "--dest", "v2.jsonl", "--key", "k", "--allow-orphans"], 0),
        ("empty source exits 2, not clean",
         {"e0.jsonl": '', "e1.jsonl": ''},
         ["--source", "e0.jsonl", "--dest", "e1.jsonl", "--key", "k"], 2),
        ("header-only CSV exits 2",
         {"e2.csv": 'k,v\n', "e1.jsonl": ''},
         ["--source", "e2.csv", "--dest", "e1.jsonl", "--key", "k"], 2),
        ("empty journal exits 2",
         {"e3.jsonl": '{"k":"E"}\n', "e1.jsonl": ''},
         ["--source", "e3.jsonl", "--dest", "e3.jsonl", "--journal", "e1.jsonl", "--key", "k"], 2),
        ("empty baseline exits 2",
         {"e3.jsonl": '{"k":"E"}\n', "e1.jsonl": ''},
         ["--source", "e3.jsonl", "--dest", "e3.jsonl", "--baseline", "e1.jsonl", "--key", "k"], 2),
        ("--allow-empty is honoured",
         {"e0.jsonl": '', "e1.jsonl": ''},
         ["--source", "e0.jsonl", "--dest", "e1.jsonl", "--key", "k", "--allow-empty"], 0),
        ("empty DESTINATION is total loss, not exit 2",
         {"e3.jsonl": '{"k":"E"}\n', "e1.jsonl": ''},
         ["--source", "e3.jsonl", "--dest", "e1.jsonl", "--key", "k"], 1),
        ("--json-out onto the source is refused",
         {"w1.jsonl": '{"k":"W"}\n'},
         ["--source", "w1.jsonl", "--dest", "w1.jsonl", "--key", "k",
          "--json-out", "w1.jsonl"], 2),
        ("--json-out onto the journal is refused",
         {"w1.jsonl": '{"k":"W"}\n', "w2.jsonl": '{"k":"W","status":"delivered"}\n'},
         ["--source", "w1.jsonl", "--dest", "w1.jsonl", "--key", "k",
          "--journal", "w2.jsonl", "--json-out", "w2.jsonl"], 2),
        ("--json-out elsewhere still works",
         {"w1.jsonl": '{"k":"W"}\n'},
         ["--source", "w1.jsonl", "--dest", "w1.jsonl", "--key", "k",
          "--json-out", "findings.json"], 0),
    ]

    for name, files, argv, want in cases:
        for fn, body in files.items():
            w(fn, body)
        rc, out = run([rec] + argv, work)
        check(name, rc == want and "Traceback" not in out, "exit %d, wanted %d" % (rc, want))

    # A crash must exit 2, never 1 -- otherwise CI reads it as findings.
    rc, out = run([rec, "--source", "nope.jsonl", "--dest", "nope.jsonl", "--key", "k"], work)
    check("missing input exits 2, not 1", rc == 2, "exit %d" % rc)

    # A phantom check that matched no journal row has checked nothing.
    w("p1.jsonl", '{"k":"P1"}\n{"k":"P2"}\n')
    w("p2.jsonl", '')
    w("pj.jsonl", '{"recordId":"P1","status":"delivered"}\n{"recordId":"P2","status":"delivered"}\n')
    rc, out = run([rec, "--source", "p1.jsonl", "--dest", "p2.jsonl",
                   "--journal", "pj.jsonl", "--key", "k"], work)
    check("journal key mismatch exits 2, not clean", rc == 2, "exit %d" % rc)
    check("and says which flag to pass", "--journal-key" in out, out.strip()[-120:])

    rc, out = run([rec, "--source", "p1.jsonl", "--dest", "p2.jsonl", "--journal", "pj.jsonl",
                   "--key", "k", "--journal-key", "recordId"], work)
    check("with --journal-key it finds the phantoms", rc == 1 and "PHANTOM" in out)

    # Partial journal coverage is a finding, not a silent skip.
    w("pj2.jsonl", '{"k":"P1","status":"delivered"}\n{"nokey":"x","status":"delivered"}\n')
    rc, out = run([rec, "--source", "p1.jsonl", "--dest", "p2.jsonl",
                   "--journal", "pj2.jsonl", "--key", "k"], work)
    check("partly unkeyable journal is reported", "could not be keyed" in out, out.strip()[-160:])


def test_journal():
    sys.path.insert(0, os.path.join(ROOT, "skills", "reconcile", "scripts"))
    from reconcile import journal_verdict, _scalar
    for rec, want in [({"status": "delivered"}, "ok"), ({"status": "created"}, "ok"),
                      ({"code": "ALG1"}, "unlabelled"), ({"httpStatus": 429}, "fail"),
                      ({"status": "delivered", "httpStatus": 503}, "fail"),
                      ({"status": "VendorWord"}, "unknown"), ({}, "unlabelled")]:
        check("journal %-34s -> %s" % (rec, want), journal_verdict(rec) == want,
              "got %s" % journal_verdict(rec))
    check("leading zeros preserved", _scalar("007") != _scalar("7"))
    check("trailing zeros preserved", _scalar("3.10") != _scalar("3.1"))
    check("beyond 2^53 stays exact", _scalar("9007199254740993") != _scalar("9007199254740992"))
    check("5, 5.0 and \"5\" still agree", _scalar(5) == _scalar("5.0") == _scalar("5"))


def main():
    tmp = tempfile.mkdtemp(prefix="reconcile-selfcheck-")
    try:
        print("\nDocumented demos, executed as written")
        test_demo(tmp, "README.md", "README")
        test_demo(tmp, "skills/reconcile/SKILL.md", "SKILL.md")
        print("\nDocumentation claims still true of the code")
        test_doc_claims()
        print("\nRegression probes")
        test_probes(tmp)
        print("\nJournal classifier and value normalisation")
        test_journal()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("")
    if failures:
        print("%d FAILED: %s" % (len(failures), ", ".join(failures)))
        return 1
    print("all checks pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
