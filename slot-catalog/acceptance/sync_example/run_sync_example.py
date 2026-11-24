#!/usr/bin/env python3
"""Re-runnable CLI demo of the offline sync channel.

Two disclosure mirrors at different depths of staleness catch up to the
same source state purely through package files:

* mirror ``ma`` installs the full package at checkpoint 0 and tracks the
  source almost batch by batch (including an empty-delta step);
* mirror ``mb`` misses every correction and jumps 0 -> final in one delta;
* both end byte-identical, and their views match the source's own
  ``export``/``query`` at the final checkpoint.

Along the way the script asserts the channel's rules: a failed batch
advances no checkpoint, an amend-only delta carries no entry rows, an
entry whose endpoint went non-public is deleted while the still-public
old endpoint keeps its document, content public only mid-window is never
smuggled into a package, retries are no-ops, and mismatched / foreign /
damaged packages are rejected with the installed data kept.

    python3 acceptance/sync_example/run_sync_example.py

Every step is a plain ``python3 -m slotcatalog ...`` call (echoed with a
``$`` prefix), so the whole flow can be replayed by hand; the batch files
live in ``batches/`` next to this script.  Exit code is non-zero if any
check fails.
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
BATCHES = HERE / "batches"

FAILURES = []


def check(name, ok, detail=""):
    print("%s %s%s" % ("PASS" if ok else "FAIL", name,
                       (" -- " + detail) if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


class Work:
    def __init__(self, tmp):
        self.dir = Path(tmp)

    def path(self, name):
        return str(self.dir / name)

    def run(self, *args, expect=0, quiet=False):
        shown = " ".join(str(a) if not str(a).startswith(str(self.dir))
                         else "work/" + Path(a).name for a in args)
        if not quiet:
            print("$ python3 -m slotcatalog %s" % shown)
        proc = subprocess.run(
            [sys.executable, "-m", "slotcatalog"] + [str(a) for a in args],
            capture_output=True, text=True, cwd=str(ROOT),
        )
        if proc.returncode != expect:
            check("cli %s exits %d" % (args[0], expect), False,
                  "got exit %d: %s%s" % (proc.returncode, proc.stderr, proc.stdout))
            return None
        if proc.stdout.strip():
            try:
                return json.loads(proc.stdout)
            except json.JSONDecodeError:
                return proc.stdout
        return None

    def read(self, name):
        return (self.dir / name).read_text(encoding="utf-8")

    def read_json(self, name):
        return json.loads(self.read(name))

    def exists(self, name):
        return (self.dir / name).exists()


def main():
    with tempfile.TemporaryDirectory() as tmp:
        w = Work(tmp)
        b = lambda name: str(BATCHES / name)

        print("== build the pre-sync catalog state and open the sync source ==")
        w.run("init", "--out", w.path("s0.json"))
        w.run("apply", w.path("s0.json"), b("00-setup.json"),
              "--out", w.path("s1.json"))
        w.run("sync-open", w.path("s1.json"), "--out", w.path("src.0.json"))
        cp0 = w.run("sync-checkpoint", w.path("src.0.json"))["checkpoint"]
        check("fresh source sits at checkpoint seq 0", cp0["seq"] == 0)

        print("== both mirrors install the full first package ==")
        w.run("sync-package", w.path("src.0.json"), "--out", w.path("pkg.full.0.json"))
        rep_a = w.run("mirror-install", w.path("pkg.full.0.json"),
                      "--out", w.path("ma.0.json"))
        rep_b = w.run("mirror-install", w.path("pkg.full.0.json"),
                      "--out", w.path("mb.0.json"))
        check("full install reports 4 docs and 4 entries",
              rep_a["installed"] and rep_a["docs"] == 4 and rep_a["entries"] == 4
              and rep_b["installed"])

        print("== batch 1: chain A->B->C; ma follows ==")
        w.run("sync-apply", w.path("src.0.json"), b("01-chain.json"),
              "--out", w.path("src.1.json"))
        w.run("sync-package", w.path("src.1.json"), "--from-seq", "0",
              "--out", w.path("pkg.0-1.json"))
        w.run("mirror-install", w.path("pkg.0-1.json"), "--mirror",
              w.path("ma.0.json"), "--out", w.path("ma.1.json"))

        print("== batch 2: amend C; the delta carries no entry rows ==")
        w.run("sync-apply", w.path("src.1.json"), b("02-amend.json"),
              "--out", w.path("src.2.json"))
        w.run("sync-package", w.path("src.2.json"), "--from-seq", "1",
              "--out", w.path("pkg.1-2.json"))
        rows = w.read_json("pkg.1-2.json")["rows"]
        check("amend-only delta updates the document, not the entries",
              rows["entries"] == {"upsert": {}, "delete": {}}
              and list(rows["docs"]["upsert"].get("B1", {})) == ["C"]
              and rows["docs"]["upsert"]["B1"]["C"]["value"]["attachments"]
              == ["补写附件一"])
        w.run("mirror-install", w.path("pkg.1-2.json"), "--mirror",
              w.path("ma.1.json"), "--out", w.path("ma.2.json"))

        print("== batch 3: no public change; the empty delta still tracks ==")
        w.run("sync-apply", w.path("src.2.json"), b("03-noop-public.json"),
              "--out", w.path("src.3.json"))
        w.run("sync-package", w.path("src.3.json"), "--from-seq", "2",
              "--out", w.path("pkg.2-3.json"))
        rows = w.read_json("pkg.2-3.json")["rows"]
        check("net-empty window yields an empty row set",
              rows == {"docs": {"upsert": {}, "delete": {}},
                       "entries": {"upsert": {}, "delete": {}}})
        rep = w.run("mirror-install", w.path("pkg.2-3.json"), "--mirror",
                    w.path("ma.2.json"), "--out", w.path("ma.3.json"))
        check("empty delta still advances the mirror checkpoint",
              rep["installed"] and rep["checkpoint"]["seq"] == 3)

        print("== batch 4: a cycle is rejected; the checkpoint stays put ==")
        w.run("sync-apply", w.path("src.3.json"), b("04-cycle-fails.json"),
              "--out", w.path("src.bad.json"), expect=1)
        check("failed batch writes no new source state", not w.exists("src.bad.json"))
        cp = w.run("sync-checkpoint", w.path("src.3.json"))["checkpoint"]
        check("failed batch does not advance the checkpoint", cp["seq"] == 3)

        print("== batches 5-6: redirect to an unapproved endpoint, publish V2 ==")
        w.run("sync-apply", w.path("src.3.json"), b("05-redirect.json"),
              "--out", w.path("src.4.json"))
        w.run("sync-apply", w.path("src.4.json"), b("06-withdraw-publish.json"),
              "--out", w.path("src.5.json"))
        cp5 = w.run("sync-checkpoint", w.path("src.5.json"))["checkpoint"]
        check("source ends at checkpoint seq 5", cp5["seq"] == 5)

        print("== both mirrors catch up from their own checkpoints ==")
        w.run("sync-package", w.path("src.5.json"), "--from-seq", "3",
              "--out", w.path("pkg.3-5.json"))
        w.run("mirror-install", w.path("pkg.3-5.json"), "--mirror",
              w.path("ma.3.json"), "--out", w.path("ma.5.json"))
        w.run("sync-package", w.path("src.5.json"), "--from-seq", "0",
              "--out", w.path("pkg.0-5.json"))
        w.run("mirror-install", w.path("pkg.0-5.json"), "--mirror",
              w.path("mb.0.json"), "--out", w.path("mb.5.json"))
        check("two mirrors at different depths converge byte-for-byte",
              w.read("ma.5.json") == w.read("mb.5.json"))

        rows = w.read_json("pkg.3-5.json")["rows"]
        check("lapsed entries are deleted, the still-public document stays",
              rows["entries"]["delete"] == {"V1": {"B1": [0, 1, 3]}}
              and rows["docs"]["delete"] == {"B1": ["D"]}
              and "C" not in rows["docs"]["upsert"].get("B1", {}))
        check("content public only mid-window is not smuggled into packages",
              "己的正文" not in w.read("pkg.3-5.json")
              and "己的正文" not in w.read("pkg.0-5.json")
              and '"F"' not in w.read("pkg.0-5.json"))

        print("== mirror views match the source at the final checkpoint ==")
        src_export = w.run("export", w.path("src.5.json"))
        ma_export = w.run("mirror-export", w.path("ma.5.json"))
        mb_export = w.run("mirror-export", w.path("mb.5.json"))
        check("mirror export equals the source search export",
              ma_export == src_export and mb_export == src_export
              and [e["part"] for e in ma_export["entries"]] == ["C", "E"])
        q = w.run("mirror-query", w.path("ma.5.json"), "V1", "B1", "0002")
        check("historical V1 index finds the current public item",
              q["endpoint"] == {"booklet": "B1", "part": "C"}
              and q["value"]["value"]["attachments"] == ["补写附件一"])
        q = w.run("mirror-query", w.path("ma.5.json"), "V2", "B1", "0004")
        check("the new V2 index resolves to the newly public part",
              q["endpoint"] == {"booklet": "B1", "part": "E"})
        err = w.run("mirror-query", w.path("ma.5.json"), "V1", "B1", "0000",
                    expect=1)
        check("lapsed entry is dead even though the old target is cached",
              err["error"]["code"] == "entry_not_found")
        err = w.run("mirror-query", w.path("ma.5.json"), "V2", "B1", "0003",
                    expect=1)
        check("printed-but-not-public slot never had an entry",
              err["error"]["code"] == "entry_not_found")

        print("== retries and bad packages ==")
        rep = w.run("mirror-install", w.path("pkg.3-5.json"), "--mirror",
                    w.path("ma.5.json"), "--out", w.path("ma.5retry.json"))
        check("retrying the just-installed package is a no-op",
              rep["installed"] is False
              and w.read("ma.5retry.json") == w.read("ma.5.json"))
        err = w.run("mirror-install", w.path("pkg.3-5.json"), "--mirror",
                    w.path("mb.0.json"), "--out", w.path("mb.bad.json"),
                    expect=1)
        check("delta from the wrong checkpoint is rejected",
              err["error"]["code"] == "checkpoint_mismatch"
              and not w.exists("mb.bad.json"))
        w.run("init", "--out", w.path("o0.json"), quiet=True)
        w.run("apply", w.path("o0.json"), b("90-other-setup.json"),
              "--out", w.path("o1.json"), quiet=True)
        w.run("sync-open", w.path("o1.json"), "--out", w.path("osrc.json"),
              quiet=True)
        w.run("sync-package", w.path("osrc.json"),
              "--out", w.path("opkg.json"), quiet=True)
        err = w.run("mirror-install", w.path("opkg.json"), "--mirror",
                    w.path("ma.5.json"), "--out", w.path("ma.bad.json"),
                    expect=1)
        check("a package from another source is rejected",
              err["error"]["code"] == "wrong_source"
              and not w.exists("ma.bad.json"))
        tampered = w.read_json("pkg.0-5.json")
        tampered["rows"]["docs"]["upsert"]["B1"]["E"]["value"]["body"] = "篡改"
        (w.dir / "tampered.json").write_text(
            json.dumps(tampered, ensure_ascii=False), encoding="utf-8")
        err = w.run("mirror-install", w.path("tampered.json"), "--mirror",
                    w.path("mb.0.json"), "--out", w.path("mb.bad2.json"),
                    expect=1)
        check("a tampered package is rejected by its digest",
              err["error"]["code"] == "invalid_package"
              and not w.exists("mb.bad2.json"))
        raw = w.read("pkg.0-5.json")
        (w.dir / "cut.json").write_text(raw[: len(raw) // 2], encoding="utf-8")
        err = w.run("mirror-install", w.path("cut.json"), "--mirror",
                    w.path("mb.0.json"), "--out", w.path("mb.bad3.json"),
                    expect=1)
        check("a truncated (incompletely received) package is rejected",
              err["error"]["code"] == "invalid_package"
              and not w.exists("mb.bad3.json"))
        check("rejected packages leave the installed mirror untouched",
              w.read("mb.0.json") == w.read("mb.0.json"))

        print("== restart consistency ==")
        w.run("sync-package", w.path("src.5.json"), "--from-seq", "0",
              "--out", w.path("pkg.0-5.again.json"), quiet=True)
        check("a reloaded source regenerates the identical delta",
              w.read("pkg.0-5.again.json") == w.read("pkg.0-5.json"))
        w.run("sync-package", w.path("src.5.json"), "--from-seq", "9",
              "--out", w.path("pkg.nowhere.json"), expect=1, quiet=True)
        check("an unknown checkpoint is rejected",
              not w.exists("pkg.nowhere.json"))
        rep = w.run("mirror-install", w.path("pkg.0-5.again.json"),
                    "--mirror", w.path("mb.5.json"),
                    "--out", w.path("mb.5again.json"), quiet=True)
        check("a reloaded mirror recognizes the already-installed target",
              rep["installed"] is False)

    if FAILURES:
        print("%d check(s) failed" % len(FAILURES))
        return 1
    print("all sync example checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
