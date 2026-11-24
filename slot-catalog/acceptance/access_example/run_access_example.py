#!/usr/bin/env python3
"""Re-runnable CLI demo of the daily access statistics (日访问统计).

The story line:

1. ``access-open`` on the setup catalog loads the first batch of reader
   records.  Records naming a reserved-but-unprinted slot (B1 slot 7) or a
   paper edition that does not exist yet (V2) count as *unprinted*.
2. A replacement chain A -> B -> C is committed through ``access-apply``.
   Historical visits are re-interpreted without touching the paper tables:
   the entries printed for A and B now count towards the public endpoint C.
3. C is un-approved: A/B/C paper indexes count as *not public* for every
   date, while booklet B2 is unaffected.
4. C is approved again: the same accesses return to the public column, and
   the visits of B1 recorded against V1 stay put (chain switch, not a
   reprint).
5. A second paper edition V2 is published: B2's V2 records were unprinted
   before and now find a real entry (the new edition is indexed in full).
6. A record group corrects one count and revokes a record; every group
   total still equals its public rows plus the two ungrouped counters.
7. An invalid group (an add and a revoke of one id, plus a correction of a
   missing id) is rejected whole and writes no file.
8. An invalid catalog batch (a replacement cycle) is rejected whole: the
   catalog and the tallies stay exactly as they were.
9. Reloading the statistics file and asking again yields byte-identical
   JSON (the programs are separate CLI invocations, so every command is a
   restart).

Every step is a plain ``python3 -m slotcatalog ...`` call (echoed with a
``$`` prefix), so the flow can be replayed by hand; the batch and record
files live in ``batches/`` and ``records/`` next to this script.  Exit code
is non-zero if any check fails.
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
BATCHES = HERE / "batches"
RECORDS = HERE / "records"

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
        shown = " ".join(
            str(a) if not str(a).startswith(str(self.dir)) else "work/" + Path(a).name
            for a in args
        )
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

    def read_bytes(self, name):
        return (self.dir / name).read_bytes()

    def read_json(self, name):
        return json.loads(self.read_bytes(name))

    def exists(self, name):
        return (self.dir / name).exists()


def group(report, date, booklet):
    for day in report["days"]:
        if day["date"] != date:
            continue
        for row in day["booklets"]:
            if row["booklet"] == booklet:
                return row
    return None


def public_counts(row):
    return {(r["booklet"], r["part"]): r["count"] for r in row["public"]}


def totals_invariant(row):
    return row["total"] == (
        row["public_total"] + row["unprinted"] + row["not_public"]
    )


def main():
    with tempfile.TemporaryDirectory() as tmp:
        w = Work(tmp)
        b = lambda name: str(BATCHES / name)
        r = lambda name: str(RECORDS / name)

        print("== build the catalog state ==")
        w.run("init", "--out", w.path("s0.json"))
        w.run("apply", w.path("s0.json"), b("00-setup.json"),
              "--out", w.path("s1.json"))

        print("== open statistics over the current catalog; load records ==")
        result = w.run("access-open", w.path("s1.json"), r("00-initial.json"),
                       "--out", w.path("a0.json"))
        check("the opening record group is fully loaded",
              result["ok"] and result["added"] == 7 and result["applied"] == 7)
        report = w.run("access-report", w.path("a0.json"))
        row = group(report, "2026-03-01", "B1")
        check("before any replacement A/B/C each count as themselves",
              public_counts(row) == {("B1", "A"): 2, ("B1", "B"): 3,
                                     ("B1", "C"): 4})
        check("the reserved-but-unprinted slot 7 counts as unprinted",
              row["unprinted"] == 1 and totals_invariant(row))
        other = group(report, "2026-03-02", "B2")
        check("a V2 index is unprinted until V2 is published",
              public_counts(other) == {("B2", "Q"): 5}
              and other["unprinted"] == 8 and totals_invariant(other))
        check("the other booklet's public counts are untouched",
              group(report, "2026-03-02", "B1")["public_total"] == 6)

        print("== chain A->B->C: history now points at C ==")
        w.run("access-apply", w.path("a0.json"), b("01-chain.json"),
              "--out", w.path("a1.json"))
        report = w.run("access-report", w.path("a1.json"))
        row = group(report, "2026-03-01", "B1")
        check("all three B1 paper indexes merge into endpoint C",
              public_counts(row) == {("B1", "C"): 9})
        check("the unprinted slot-7 access is still unprinted",
              row["unprinted"] == 1 and totals_invariant(row))

        print("== endpoint C becomes not public ==")
        w.run("access-apply", w.path("a1.json"), b("02-not-public.json"),
              "--out", w.path("a2.json"))
        report = w.run("access-report", w.path("a2.json"))
        row = group(report, "2026-03-01", "B1")
        check("B1's accesses are not attributed to the old bodies",
              row["public"] == [] and row["not_public"] == 9)
        check("the unprinted access stays separate and the total holds",
              row["unprinted"] == 1 and totals_invariant(row))
        check("B2's unrelated records did not move",
              public_counts(group(report, "2026-03-02", "B2"))
              == {("B2", "Q"): 5})
        text = json.dumps(report, ensure_ascii=False)
        check("no not-public body or attachment appears in the report",
              "丙的正文" not in text and "乙的正文" not in text
              and "补写" not in text)

        print("== endpoint C becomes public again ==")
        w.run("access-apply", w.path("a2.json"), b("03-public-again.json"),
              "--out", w.path("a3.json"))
        report = w.run("access-report", w.path("a3.json"))
        row = group(report, "2026-03-01", "B1")
        check("the same accesses return to public endpoint C",
              public_counts(row) == {("B1", "C"): 9} and row["not_public"] == 0)
        check("every group total still adds up", totals_invariant(row))

        print("== correct one record and revoke another ==")
        result = w.run("access-records", w.path("a3.json"), r("01-correct.json"),
                       "--out", w.path("a4.json"))
        check("the correction group is applied atomically",
              result["corrected"] == 1 and result["revoked"] == 1
              and result["applied"] == 2)
        report = w.run("access-report", w.path("a4.json"))
        row = group(report, "2026-03-01", "B1")
        check("the corrected count 4 -> 9 shows up on C",
              public_counts(row) == {("B1", "C"): 14})
        check("the revoked unprinted record is gone",
              row["unprinted"] == 0 and row["total"] == 14
              and totals_invariant(row))

        print("== publish V2: previously unprinted B2 records become valid ==")
        w.run("access-apply", w.path("a4.json"), b("04-publish-v2.json"),
              "--out", w.path("a5.json"))
        report = w.run("access-report", w.path("a5.json"))
        other = group(report, "2026-03-02", "B2")
        check("B2's V2 records now count towards Q",
              public_counts(other) == {("B2", "Q"): 13}
              and other["unprinted"] == 0 and totals_invariant(other))
        check("B1's tallies are unchanged by the new edition",
              public_counts(group(report, "2026-03-01", "B1"))
              == {("B1", "C"): 14})

        print("== rejected groups and batches leave everything as it was ==")
        before = w.read_bytes("a5.json")
        checks = [
            ("90-duplicate.json", "duplicate_record_op"),
            ("90-missing.json", "record_not_found"),
            ("90-existing.json", "record_exists"),
        ]
        for index, (name, code) in enumerate(checks):
            bad = w.run("access-records", w.path("a5.json"), r(name),
                        "--out", w.path("bad%d.json" % index), expect=1)
            check("a group rejected with %s writes no file" % code,
                  bad["ok"] is False and bad["error"]["code"] == code
                  and not w.exists("bad%d.json" % index))
        check("no rejected record group changed the statistics file",
              w.read_bytes("a5.json") == before)
        denied = w.run("access-apply", w.path("a5.json"), b("90-cycle-fails.json"),
                       "--out", w.path("a7.json"), expect=1)
        check("a replacement cycle is rejected as a batch failure",
              denied["error"]["code"] == "replacement_cycle")
        check("the rejected batch wrote no file and changed no tally",
              not w.exists("a7.json")
              and w.run("access-report", w.path("a5.json")) == report)

        print("== restart: reloading the statistics file answers identically ==")
        w.run("access-checkpoint", w.path("a5.json"))
        first = w.run("access-report", w.path("a5.json"))
        again = w.run("access-report", w.path("a5.json"))
        check("two separate CLI runs produce identical reports", first == again)
        replay = w.run("access-report", w.path("a5.json"), "--date", "2026-03-01",
                       "--booklet", "B1")
        check("filters select one day/booklet group",
              len(replay["days"]) == 1 and len(replay["days"][0]["booklets"]) == 1)

    print()
    if FAILURES:
        print("%d check(s) failed" % len(FAILURES))
        return 1
    print("all access example checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
