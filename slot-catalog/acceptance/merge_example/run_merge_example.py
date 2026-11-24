#!/usr/bin/env python3
"""Re-runnable CLI verification for the offline merge command.

Runs ``python -m slotcatalog merge`` on the committed example inputs and
checks, all through the CLI:

* the merge report and the merged state match the golden files;
* the merged state is directly usable by ``query``/``export``/``apply``;
* the merge is deterministic (a second run is byte-identical);
* failures behave: tampered base hash -> non-zero exit and no output file;
  an existing --out target is never overwritten; inputs stay byte-identical.

    python3 acceptance/merge_example/run_merge_example.py           # check
    python3 acceptance/merge_example/run_merge_example.py --update  # bless

Exit code is non-zero if any check fails.  If the branch files no longer
match base.json (its bytes pin the declared SHA-256), regenerate everything
with gen_example.py.
"""

import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent

BASE = HERE / "base.json"
BRANCHES = [HERE / "branch-alpha.json", HERE / "branch-beta.json"]
EXPECTED_REPORT = HERE / "expected.report.json"
EXPECTED_STATE = HERE / "expected.state.json"

FAILURES = []


def check(name, ok, detail=""):
    print("%s %s%s" % ("PASS" if ok else "FAIL", name, (" -- " + detail) if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def run_cli(args):
    return subprocess.run(
        [sys.executable, "-m", "slotcatalog"] + args,
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    update = "--update" in sys.argv

    # 0. the committed branch files must pin the committed base bytes
    digest = sha(BASE)
    pins_ok = all(
        json.loads(p.read_text(encoding="utf-8")).get("base_sha256") == digest
        for p in BRANCHES
    )
    check("branch files pin the base sha256", pins_ok,
          "run gen_example.py to regenerate")
    if not pins_ok:
        return 1
    inputs_before = {p: sha(p) for p in [BASE] + BRANCHES}

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "merged.json"
        args = ["merge", str(BASE)] + [str(p) for p in BRANCHES] + ["--out", str(out)]

        # 1. merge succeeds; report matches the golden (minus the out path)
        proc = run_cli(args)
        check("merge exits 0", proc.returncode == 0, proc.stderr)
        report = json.loads(proc.stdout)
        report.pop("out", None)
        if update:
            EXPECTED_REPORT.write_text(
                json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
        expected_report = json.loads(EXPECTED_REPORT.read_text(encoding="utf-8"))
        check("merge report matches golden", report == expected_report)

        # 2. merged state matches the golden
        state_text = out.read_text(encoding="utf-8")
        if update:
            EXPECTED_STATE.write_text(state_text, encoding="utf-8")
        check("merged state matches golden",
              state_text == EXPECTED_STATE.read_text(encoding="utf-8"))

        # 3. the merged state is directly usable by query / export / apply
        q = run_cli(["query", str(out), "V1", "B1", "0001"])
        qdoc = json.loads(q.stdout)
        check("query on merged state resolves through merged edges",
              q.returncode == 0
              and qdoc["printed"]["part"] == "P-002"
              and qdoc["current"]["endpoint"]["part"] == "P-201"
              and qdoc["current"]["status"] == "public")
        e = run_cli(["export", str(out), "--booklet", "B1"])
        edoc = json.loads(e.stdout)
        check("search export on merged state",
              e.returncode == 0
              and [en["part"] for en in edoc["entries"]]
              == ["P-003", "P-101", "P-202", "P-201", "P-102"])
        batch = Path(tmp) / "post.json"
        batch.write_text(
            json.dumps({"batch_id": "POST", "ops": [
                {"op": "register", "booklet": "B1", "part": "P-301",
                 "body": "合稿后登记", "approved": True}
            ]}),
            encoding="utf-8",
        )
        out2 = Path(tmp) / "merged2.json"
        a = run_cli(["apply", str(out), str(batch), "--out", str(out2)])
        applied = json.loads(out2.read_text(encoding="utf-8"))
        check("apply continues on merged state",
              a.returncode == 0
              and applied["booklets"]["B1"]["parts"]["P-301"]["slot"] == 6)

        # 4. determinism: a second run is byte-identical
        out3 = Path(tmp) / "merged3.json"
        proc3 = run_cli(args[:-1] + [str(out3)])
        check("merge is deterministic",
              proc3.returncode == 0
              and out3.read_bytes() == out.read_bytes()
              and json.loads(proc3.stdout)["moves"] == json.loads(proc.stdout)["moves"])

        # 5. a tampered base hash fails with a locatable reason, no output
        tampered = Path(tmp) / "tampered.json"
        bad = json.loads(BRANCHES[0].read_text(encoding="utf-8"))
        bad["base_sha256"] = "0" * 64
        tampered.write_text(json.dumps(bad), encoding="utf-8")
        noout = Path(tmp) / "noout.json"
        proc5 = run_cli(["merge", str(BASE), str(tampered), str(BRANCHES[1]),
                         "--out", str(noout)])
        err = json.loads(proc5.stdout)
        check("base mismatch rejected without output",
              proc5.returncode != 0
              and err["error"]["code"] == "base_mismatch"
              and err["error"]["details"]["branch"] == "alpha"
              and not noout.exists())

        # 6. an existing --out target is never overwritten
        keep = Path(tmp) / "keep.json"
        keep.write_text("pre-existing", encoding="utf-8")
        proc6 = run_cli(["merge", str(BASE), str(BRANCHES[0]), str(BRANCHES[1]),
                         "--out", str(keep)])
        err6 = json.loads(proc6.stdout)
        check("existing output is not overwritten",
              proc6.returncode != 0
              and err6["error"]["code"] == "output_exists"
              and keep.read_text(encoding="utf-8") == "pre-existing")

    # 7. all inputs are byte-identical afterwards
    check("inputs unchanged",
          all(sha(p) == h for p, h in inputs_before.items()))

    if FAILURES:
        print("%d check(s) failed" % len(FAILURES))
        return 1
    print("all merge example checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
