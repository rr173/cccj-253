"""Unit tests for daily access statistics (slotcatalog.access).

Run from the repository root:

    python3 -m unittest discover -s tests -t . -v
"""

import contextlib
import io
import json
import os
import tempfile
import unittest

from slotcatalog import Catalog
from slotcatalog.__main__ import main as cli_main
from slotcatalog.access import ACCESS_FORMAT, AccessStats
from slotcatalog.errors import BatchError, RecordBatchError


def reg(booklet, part, slot=None, **kw):
    op = {"op": "register", "booklet": booklet, "part": part}
    if slot is not None:
        op["slot"] = slot
    op.update(kw)
    return op


def rec(record_id, date, version, booklet, slot, count=1):
    return {
        "id": record_id,
        "date": date,
        "version": version,
        "booklet": booklet,
        "slot": slot,
        "count": count,
    }


def add(record_id, date, version, booklet, slot, count=1):
    return dict(rec(record_id, date, version, booklet, slot, count), op="add")


def correct(record_id, date, version, booklet, slot, count=1):
    return dict(rec(record_id, date, version, booklet, slot, count), op="correct")


def state_doc(parts=("A", "B", "C"), versions=1):
    """B1 with approved parts A..C printed in V1..V<versions>."""
    ops = [{"op": "add_booklet", "booklet": "B1", "slot_width": 4}]
    for part in parts:
        ops.append(
            reg(
                "B1",
                part,
                title="件" + part,
                body="正文" + part,
                approved=True,
            )
        )
    ops.append({"op": "publish", "version": "V1"})
    for n in range(2, versions + 1):
        ops.append({"op": "publish", "version": "V%d" % n})
    cat = Catalog()
    cat.apply_batch(ops, batch_id="SETUP")
    return cat.save()


def open_stats(records=(), versions=1, state=None):
    stats = AccessStats.open(state if state is not None else state_doc(versions=versions))
    if records:
        stats.apply_records(records, group_id="LOAD")
    return stats


def groups(report):
    return {
        (day["date"], row["booklet"]): row
        for day in report["days"]
        for row in day["booklets"]
    }


def endpoints(row):
    return {(r["booklet"], r["part"]): r["count"] for r in row["public"]}


class TestInterpretation(unittest.TestCase):
    def test_public_unprinted_and_not_public_are_separated(self):
        stats = open_stats()
        stats.apply_batch(
            [{"op": "set_review", "booklet": "B1", "part": "C", "approved": False}],
            batch_id="REVIEW",
        )
        stats.apply_records(
            [
                add("r1", "2026-01-01", "V1", "B1", 0, 2),   # A public
                add("r2", "2026-01-01", "V1", "B1", 2, 3),   # C not approved
                add("r3", "2026-01-01", "V1", "B1", 9, 4),   # slot never printed
                add("r4", "2026-01-01", "V9", "B1", 0, 5),   # version unknown
                add("r5", "2026-01-01", "V1", "B9", 0, 6),   # booklet unknown
            ],
            group_id="LOAD",
        )
        row = groups(stats.report())[("2026-01-01", "B1")]
        self.assertEqual(endpoints(row), {("B1", "A"): 2})
        self.assertEqual(row["not_public"], 3)
        self.assertEqual(row["unprinted"], 9)
        self.assertEqual(row["public_total"], 2)
        self.assertEqual(row["total"], 14)
        # an unknown booklet is its own (day, booklet) group
        other = groups(stats.report())[("2026-01-01", "B9")]
        self.assertEqual(other["unprinted"], 6)
        self.assertEqual(
            row["total"],
            row["public_total"] + row["unprinted"] + row["not_public"],
        )

    def test_withdrawn_endpoint_is_not_public_and_body_is_not_reported(self):
        stats = open_stats()
        stats.apply_batch(
            [{"op": "withdraw", "booklet": "B1", "part": "B"}], batch_id="W"
        )
        stats.apply_records(
            [add("r1", "2026-01-01", "V1", "B1", 1, 7)], group_id="LOAD"
        )
        report = stats.report()
        row = groups(report)[("2026-01-01", "B1")]
        self.assertEqual(row["not_public"], 7)
        self.assertEqual(row["public"], [])
        text = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("正文B", text)

    def test_printed_entries_sharing_an_endpoint_are_merged(self):
        stats = open_stats()
        stats.apply_batch(
            [
                {"op": "replace", "booklet": "B1", "old": "A", "new": "B"},
                {"op": "replace", "booklet": "B1", "old": "B", "new": "C"},
            ],
            batch_id="CHAIN",
        )
        stats.apply_records(
            [
                add("r1", "2026-01-01", "V1", "B1", 0, 2),
                add("r2", "2026-01-01", "V1", "B1", 1, 3),
                add("r3", "2026-01-01", "V1", "B1", 2, 4),
            ],
            group_id="LOAD",
        )
        row = groups(stats.report())[("2026-01-01", "B1")]
        self.assertEqual(endpoints(row), {("B1", "C"): 9})

    def test_dates_and_booklets_are_tallied_separately(self):
        cat = Catalog()
        cat.apply_batch(
            [
                {"op": "add_booklet", "booklet": "B1", "slot_width": 4},
                {"op": "add_booklet", "booklet": "B2", "slot_width": 4},
                reg("B1", "A", body="a", approved=True),
                reg("B2", "A", body="b", approved=True),
                {"op": "publish", "version": "V1"},
            ],
            batch_id="SETUP",
        )
        stats = open_stats(state=cat.save())
        stats.apply_records(
            [
                add("r1", "2026-01-01", "V1", "B1", 0, 1),
                add("r2", "2026-01-02", "V1", "B1", 0, 2),
                add("r3", "2026-01-01", "V1", "B2", 0, 3),
            ],
            group_id="LOAD",
        )
        got = groups(stats.report())
        self.assertEqual(endpoints(got[("2026-01-01", "B1")]), {("B1", "A"): 1})
        self.assertEqual(endpoints(got[("2026-01-02", "B1")]), {("B1", "A"): 2})
        self.assertEqual(endpoints(got[("2026-01-01", "B2")]), {("B2", "A"): 3})

    def test_zero_groups_and_zero_rows_are_not_emitted(self):
        stats = open_stats()
        stats.apply_records(
            [add("r1", "2026-01-01", "V1", "B1", 0, 1)], group_id="LOAD"
        )
        stats.apply_records([{"op": "revoke", "id": "r1"}], group_id="EMPTY")
        self.assertEqual(stats.report(), {"ok": True, "format": stats.report()["format"], "days": []})
        self.assertEqual(stats.table, {})

    def test_report_filters(self):
        stats = open_stats()
        stats.apply_records(
            [
                add("r1", "2026-01-01", "V1", "B1", 0, 1),
                add("r2", "2026-01-02", "V1", "B1", 1, 2),
            ],
            group_id="LOAD",
        )
        only_day = stats.report(date="2026-01-02")
        self.assertEqual(list(groups(only_day)), [("2026-01-02", "B1")])
        self.assertEqual(stats.report(booklet="B9")["days"], [])
        with self.assertRaises(Exception):
            stats.report(date="2026-13-01")


class TestRecordGroups(unittest.TestCase):
    def test_duplicate_add_correct_and_revoke_of_missing_are_rejected_whole(self):
        stats = open_stats()
        stats.apply_records(
            [add("r1", "2026-01-01", "V1", "B1", 0, 1)], group_id="LOAD"
        )
        before = json.dumps(stats.save(), ensure_ascii=False, sort_keys=True)
        bad_groups = [
            [add("r1", "2026-01-02", "V1", "B1", 1, 1)],
            [correct("missing", "2026-01-02", "V1", "B1", 1, 1)],
            [{"op": "revoke", "id": "missing"}],
            [add("r2", "2026-01-02", "V1", "B1", 1, 1),
             {"op": "revoke", "id": "r2"}],                 # same id twice
            [add("r3", "2026-01-02", "V1", "B1", 1, 1),
             add("r4", "2026-01-02", "V1", "B1", 1, 1),
             add("r3", "2026-01-02", "V1", "B1", 2, 1)],
        ]
        for ops in bad_groups:
            with self.assertRaises(RecordBatchError):
                stats.apply_records(ops, group_id="BAD")
            self.assertEqual(
                json.dumps(stats.save(), ensure_ascii=False, sort_keys=True), before
            )

    def test_malformed_records_are_rejected(self):
        stats = open_stats()
        for bad in [
            {"op": "add", "id": "x", "date": "2026-2-01", "version": "V1",
             "booklet": "B1", "slot": 0, "count": 1},
            {"op": "add", "id": "x", "date": "2026-02-30", "version": "V1",
             "booklet": "B1", "slot": 0, "count": 1},
            {"op": "add", "id": "x", "date": "2026-02-01", "version": "V1",
             "booklet": "B1", "slot": 0, "count": 0},
            {"op": "add", "id": "x", "date": "2026-02-01", "version": "V1",
             "booklet": "B1", "slot": 0, "count": True},
            {"op": "add", "id": "", "date": "2026-02-01", "version": "V1",
             "booklet": "B1", "slot": 0, "count": 1},
            {"op": "nope", "id": "x"},
            {"op": "revoke", "id": "x", "count": 1},
        ]:
            with self.assertRaises(RecordBatchError):
                stats.apply_records([bad], group_id="BAD")

    def test_correct_and_revoke_change_only_their_buckets(self):
        stats = open_stats()
        stats.apply_records(
            [
                add("r1", "2026-01-01", "V1", "B1", 0, 5),
                add("r2", "2026-01-01", "V1", "B1", 1, 3),
            ],
            group_id="LOAD",
        )
        stats.apply_records(
            [correct("r1", "2026-01-03", "V1", "B1", 2, 5)], group_id="FIX"
        )
        got = groups(stats.report())
        self.assertEqual(endpoints(got[("2026-01-01", "B1")]), {("B1", "B"): 3})
        self.assertEqual(endpoints(got[("2026-01-03", "B1")]), {("B1", "C"): 5})
        self.assertEqual(stats.stats["records_recomputed"], 1)
        stats.apply_records([{"op": "revoke", "id": "r2"}], group_id="DROP")
        self.assertEqual(stats.checkpoint()["records"], 1)
        self.assertEqual(endpoints(groups(stats.report())[("2026-01-03", "B1")]),
                         {("B1", "C"): 5})

    def test_correcting_lenient_dates_and_padded_slots(self):
        stats = open_stats()
        stats.apply_records(
            [add("r1", "2026-01-01", "V1", "B1", "0002", 4)], group_id="LOAD"
        )
        self.assertEqual(endpoints(groups(stats.report())[("2026-01-01", "B1")]),
                         {("B1", "C"): 4})


class TestCatalogCorrections(unittest.TestCase):
    def setUp(self):
        self.stats = open_stats()
        self.stats.apply_records(
            [
                add("r1", "2026-01-01", "V1", "B1", 0, 2),
                add("r2", "2026-01-01", "V1", "B1", 1, 3),
                add("r3", "2026-01-01", "V1", "B1", 2, 4),
            ],
            group_id="LOAD",
        )

    def row(self):
        return groups(self.stats.report())[("2026-01-01", "B1")]

    def test_switch_to_not_public_and_back(self):
        self.stats.apply_batch(
            [
                {"op": "set_review", "booklet": "B1", "part": "B", "approved": False},
                {"op": "replace", "booklet": "B1", "old": "A", "new": "B"},
            ],
            batch_id="SWITCH",
        )
        row = self.row()
        # A's paper index now ends at B, which is not public
        self.assertEqual(endpoints(row), {("B1", "C"): 4})
        self.assertEqual(row["not_public"], 5)
        self.assertEqual(row["unprinted"], 0)
        # B is approved again: A's index resolves to the public endpoint B
        self.stats.apply_batch(
            [{"op": "set_review", "booklet": "B1", "part": "B", "approved": True}],
            batch_id="BACK",
        )
        row = self.row()
        self.assertEqual(endpoints(row),
                         {("B1", "B"): 5, ("B1", "C"): 4})
        self.assertEqual(row["not_public"], 0)

    def test_unreplace_restores_the_original_endpoint(self):
        self.stats.apply_batch(
            [{"op": "replace", "booklet": "B1", "old": "A", "new": "B"}],
            batch_id="EDGE",
        )
        self.assertEqual(endpoints(self.row())[("B1", "B")], 5)
        self.stats.apply_batch(
            [{"op": "unreplace", "booklet": "B1", "old": "A"}], batch_id="UN"
        )
        row = self.row()
        self.assertEqual(endpoints(row),
                         {("B1", "A"): 2, ("B1", "B"): 3, ("B1", "C"): 4})

    def test_withdraw_kills_historical_attribution(self):
        self.stats.apply_batch(
            [{"op": "withdraw", "booklet": "B1", "part": "A"}], batch_id="W"
        )
        row = self.row()
        self.assertNotIn(("B1", "A"), endpoints(row))
        self.assertEqual(row["not_public"], 2)

    def test_new_paper_edition_turns_unprinted_into_public(self):
        stats = open_stats()
        stats.apply_records(
            [add("late", "2026-01-01", "V2", "B1", 1, 6)], group_id="LOAD"
        )
        self.assertEqual(groups(stats.report())[("2026-01-01", "B1")]["unprinted"], 6)
        stats.apply_batch([{"op": "publish", "version": "V2"}], batch_id="PUB")
        row = groups(stats.report())[("2026-01-01", "B1")]
        self.assertEqual(endpoints(row), {("B1", "B"): 6})
        self.assertEqual(row["unprinted"], 0)
        self.assertEqual(stats.stats["records_recomputed"], 1)

    def test_failed_batch_leaves_catalog_and_tallies_untouched(self):
        before_catalog = self.stats.catalog.save()
        before_report = self.stats.report()
        before_records = dict(self.stats.records)
        with self.assertRaises(BatchError):
            self.stats.apply_batch(
                [{"op": "replace", "booklet": "B1", "old": "A", "new": "A"}],
                batch_id="CYCLE",
            )
        self.assertEqual(self.stats.report(), before_report)
        self.assertEqual(self.stats.records, before_records)
        after_catalog = self.stats.catalog.save()
        # the only difference is the catalog's own audit entry for the
        # rejected batch; no booklet, version or part changed
        self.assertEqual(after_catalog["booklets"], before_catalog["booklets"])
        self.assertEqual(after_catalog["versions"], before_catalog["versions"])


class TestIncrementalCost(unittest.TestCase):
    def wide(self, booklets=("B1",), parts=30, versions=3):
        cat = Catalog()
        ops = []
        for booklet_id in booklets:
            ops.append(
                {"op": "add_booklet", "booklet": booklet_id, "slot_width": 4}
            )
        for booklet_id in booklets:
            for i in range(parts):
                ops.append(
                    reg(booklet_id, "P%03d" % i, body="b", approved=True)
                )
        ops.append({"op": "publish", "version": "V1"})
        cat.apply_batch(ops, batch_id="SETUP")
        for n in range(2, versions + 1):
            cat.apply_batch(
                [{"op": "publish", "version": "V%d" % n}], batch_id="PUB%d" % n
            )
        stats = AccessStats.open(cat.save())
        ops = []
        for booklet_id in booklets:
            for i in range(parts):
                for n in range(1, versions + 1):
                    ops.append(
                        add(
                            "r-%s-%03d-%d" % (booklet_id, i, n),
                            "2026-01-01",
                            "V%d" % n,
                            booklet_id,
                            i,
                            1,
                        )
                    )
        stats.apply_records(ops, group_id="LOAD")
        return stats

    def test_amend_recomputes_only_its_printed_entries(self):
        stats = self.wide()
        stats.apply_batch(
            [{"op": "amend", "booklet": "B1", "part": "P007", "attachment": "补"}],
            batch_id="AMEND",
        )
        self.assertEqual(stats.stats["entries_recomputed"], 3)
        # only the 3 records on those printed entries, nothing else
        self.assertEqual(stats.stats["records_recomputed"], 3)

    def test_edge_change_touches_only_the_affected_chain(self):
        stats = self.wide()
        stats.apply_batch(
            [{"op": "replace", "booklet": "B1", "old": "P001", "new": "P002"}],
            batch_id="EDGE",
        )
        self.assertEqual(stats.stats["entries_recomputed"], 6)  # two parts x 3
        self.assertEqual(stats.stats["records_recomputed"], 6)

    def test_unrelated_booklet_records_are_not_scanned(self):
        stats = self.wide(booklets=("B1", "B2"))
        stats.apply_batch(
            [{"op": "withdraw", "booklet": "B1", "part": "P000"}], batch_id="W"
        )
        # only the 3 records of P000 in B1, never any B2 record
        self.assertEqual(stats.stats["records_recomputed"], 3)
        self.assertEqual(stats.stats["buckets_scanned"], 1)

    def test_reserve_only_batch_recomputes_nothing(self):
        stats = self.wide()
        stats.apply_batch(
            [{"op": "reserve", "booklet": "B1", "count": 5}], batch_id="RES"
        )
        self.assertEqual(stats.stats, {"entries_recomputed": 0,
                                       "records_recomputed": 0,
                                       "buckets_scanned": 0})

    def test_record_group_work_is_proportional_to_the_group(self):
        stats = self.wide(booklets=("B1", "B2"))
        stats.apply_records(
            [correct("r-B1-004-1", "2026-02-02", "V2", "B2", 4, 9)],
            group_id="FIX",
        )
        self.assertEqual(stats.stats["records_recomputed"], 1)
        self.assertEqual(stats.stats["buckets_scanned"], 2)

    def test_publish_indexes_only_the_new_edition(self):
        stats = self.wide(booklets=("B1", "B2"), parts=10, versions=2)
        stats.apply_batch([{"op": "publish", "version": "V9"}], batch_id="PUB")
        # entries of the new edition only: 10 parts x 2 booklets
        self.assertEqual(stats.stats["entries_recomputed"], 20)
        self.assertEqual(stats.stats["records_recomputed"], 0)


class TestPersistence(unittest.TestCase):
    def test_restart_is_byte_identical_and_report_identical(self):
        stats = open_stats(versions=3)
        stats.apply_records(
            [
                add("r1", "2026-01-01", "V1", "B1", 0, 2),
                add("r2", "2026-01-02", "V2", "B1", 4, 5),
                add("r3", "2026-01-02", "V9", "B1", 0, 7),
            ],
            group_id="LOAD",
        )
        stats.apply_batch(
            [{"op": "replace", "booklet": "B1", "old": "A", "new": "B"}],
            batch_id="EDGE",
        )
        stats.apply_records([{"op": "revoke", "id": "r2"}], group_id="DROP")
        text = json.dumps(stats.save(), ensure_ascii=False, sort_keys=True)
        self.assertTrue(text.startswith('{"access"'))
        self.assertEqual(stats.save()["format"], ACCESS_FORMAT)
        again = AccessStats.load(json.loads(text))
        self.assertEqual(
            json.dumps(again.save(), ensure_ascii=False, sort_keys=True), text
        )
        self.assertEqual(again.report(), stats.report())
        # and the resumed file keeps accepting both kinds of corrections
        again.apply_records(
            [correct("r3", "2026-01-02", "V1", "B1", 2, 7)], group_id="FIX"
        )
        row = groups(again.report())[("2026-01-02", "B1")]
        self.assertEqual(endpoints(row), {("B1", "C"): 7})

    def test_load_rejects_a_malformed_or_mismatched_document(self):
        stats = open_stats()
        stats.apply_records([add("r1", "2026-01-01", "V1", "B1", 0, 1)],
                            group_id="LOAD")
        doc = stats.save()
        with self.assertRaises(Exception):
            AccessStats.load({"format": "slot-catalog-state/1"})
        broken = json.loads(json.dumps(doc))
        broken["access"]["attribution"].pop("r1")
        with self.assertRaises(Exception):
            AccessStats.load(broken)

    def test_open_rejects_a_non_catalog_document(self):
        with self.assertRaises(Exception):
            AccessStats.open({"format": "nope"})

    def test_load_rejects_tallies_that_disagree_with_attribution(self):
        stats = open_stats()
        stats.apply_records([add("r1", "2026-01-01", "V1", "B1", 0, 1)],
                            group_id="LOAD")
        broken = json.loads(json.dumps(stats.save()))
        broken["access"]["buckets"][0]["public"][0]["count"] = 99
        with self.assertRaises(Exception):
            AccessStats.load(broken)


class TestCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def path(self, name):
        return os.path.join(self.tmp.name, name)

    def write(self, name, doc):
        with open(self.path(name), "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False)
        return self.path(name)

    def run_cli(self, args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli_main(args)
        return code, buf.getvalue()

    def boot(self):
        self.write("state.json", state_doc())
        self.write("records.json", [
            add("r1", "2026-01-01", "V1", "B1", 0, 2),
            add("r2", "2026-01-01", "V1", "B1", 9, 3),
        ])
        code, out = self.run_cli([
            "access-open", self.path("state.json"), self.path("records.json"),
            "--out", self.path("access.json"),
        ])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["added"], 2)

    def test_cli_report_records_apply_flow(self):
        self.boot()
        code, out = self.run_cli(["access-report", self.path("access.json")])
        self.assertEqual(code, 0)
        row = groups(json.loads(out))[("2026-01-01", "B1")]
        self.assertEqual(row["total"], 5)
        # a record correction moves counts
        self.write("fix.json", {"group_id": "FIX", "ops": [
            {"op": "correct", "id": "r2", "date": "2026-01-01",
             "version": "V1", "booklet": "B1", "slot": 1, "count": 3}]})
        code, _ = self.run_cli([
            "access-records", self.path("access.json"), self.path("fix.json"),
            "--out", self.path("access2.json")])
        self.assertEqual(code, 0)
        code, out = self.run_cli(["access-report", self.path("access2.json")])
        row = groups(json.loads(out))[("2026-01-01", "B1")]
        self.assertEqual(endpoints(row), {("B1", "A"): 2, ("B1", "B"): 3})
        # a catalog batch (a paper edition) folds in too
        self.write("pub.json", {"batch_id": "PUB", "ops": [
            {"op": "publish", "version": "V2"}]})
        code, _ = self.run_cli([
            "access-apply", self.path("access2.json"), self.path("pub.json"),
            "--out", self.path("access3.json")])
        self.assertEqual(code, 0)
        code, out = self.run_cli(["access-checkpoint", self.path("access3.json")])
        checkpoint = json.loads(out)["checkpoint"]
        self.assertEqual(checkpoint["records"], 2)

    def test_cli_failure_writes_no_file(self):
        self.boot()
        self.write("bad.json", {"group_id": "BAD", "ops": [
            {"op": "revoke", "id": "missing"}]})
        code, out = self.run_cli([
            "access-records", self.path("access.json"), self.path("bad.json"),
            "--out", self.path("access2.json")])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["error"]["code"], "record_not_found")
        self.assertEqual(json.loads(out)["error"]["batch_id"], "BAD")
        self.assertFalse(os.path.exists(self.path("access2.json")))
        self.write("badbatch.json", {"batch_id": "CYCLE", "ops": [
            {"op": "replace", "booklet": "B1", "old": "A", "new": "A"}]})
        code, out = self.run_cli([
            "access-apply", self.path("access.json"), self.path("badbatch.json"),
            "--out", self.path("access3.json")])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["error"]["code"], "replacement_cycle")
        self.assertFalse(os.path.exists(self.path("access3.json")))


if __name__ == "__main__":
    unittest.main()
