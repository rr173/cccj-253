"""Command line interface.

usage:
  python -m slotcatalog run SCENARIO.json
  python -m slotcatalog apply STATE.json BATCH.json [--out NEWSTATE.json]
  python -m slotcatalog merge BASE.json BRANCH1.json BRANCH2.json [...] --out MERGED.json
  python -m slotcatalog query STATE.json VERSION BOOKLET SLOT
  python -m slotcatalog export STATE.json [--booklet BOOKLET]

All output is deterministic JSON (sorted keys, UTF-8, no timestamps).

``merge`` folds two or more offline branch files (priority: first listed
wins) into the shared base state.  The merged state goes to --out (an
existing file is never overwritten); the merge report, listing every
relocated registration, is printed to stdout.  On failure nothing is
written and the exit code is non-zero.
"""

import json
import os
import sys
import tempfile

from .catalog import Catalog
from .errors import BatchError, CatalogError, OutputExists
from .merge import merge_branches
from .scenario import run_scenario


def _load_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _dump(doc, out=None):
    text = json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if out is None or out == "-":
        sys.stdout.write(text)
    else:
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(text)


def _write_no_clobber(path, doc):
    """Write ``doc`` as deterministic JSON, refusing to overwrite anything.

    The document is staged in a temporary file and hard-linked into place,
    so a failure never leaves a half-written output and an existing target
    is never replaced.
    """
    text = json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(
        dir=directory, prefix=".slotcatalog-merge-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.link(tmp, path)  # atomic; fails if ``path`` already exists
    except FileExistsError:
        raise OutputExists(
            "output %r already exists; refusing to overwrite" % path, out=path
        )
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        sys.stderr.write(__doc__)
        return 2
    command, args = argv[0], argv[1:]
    try:
        if command == "run":
            _dump(run_scenario(_load_json(args[0])))
            return 0
        if command == "apply":
            out = None
            if "--out" in args:
                i = args.index("--out")
                out = args[i + 1]
                del args[i : i + 2]
            cat = Catalog.load(_load_json(args[0]))
            batch = _load_json(args[1])
            if isinstance(batch, list):
                batch = {"ops": batch}
            cat.apply_batch(batch.get("ops", []), batch_id=batch.get("batch_id"))
            _dump(cat.save(), out)
            return 0
        if command == "merge":
            out = None
            if "--out" in args:
                i = args.index("--out")
                if i + 1 >= len(args):
                    sys.stderr.write("merge --out needs a path\n")
                    sys.stderr.write(__doc__)
                    return 2
                out = args[i + 1]
                del args[i : i + 2]
            if out is None or len(args) < 3:
                sys.stderr.write(
                    "usage: python -m slotcatalog merge BASE.json "
                    "BRANCH1.json BRANCH2.json [...] --out MERGED.json\n"
                )
                return 2
            if os.path.exists(out):
                raise OutputExists(
                    "output %r already exists; refusing to overwrite" % out,
                    out=out,
                )
            with open(args[0], "rb") as fh:
                base_bytes = fh.read()
            branch_docs = [_load_json(path) for path in args[1:]]
            state, report = merge_branches(base_bytes, branch_docs)
            report["out"] = out
            _write_no_clobber(out, state)
            _dump(report)
            return 0
        if command == "query":
            cat = Catalog.load(_load_json(args[0]))
            _dump(cat.query_slot(args[1], args[2], args[3]))
            return 0
        if command == "export":
            booklet = None
            if "--booklet" in args:
                i = args.index("--booklet")
                booklet = args[i + 1]
            cat = Catalog.load(_load_json(args[0]))
            _dump(cat.search_export(booklet))
            return 0
        sys.stderr.write("unknown command %r\n" % command)
        sys.stderr.write(__doc__)
        return 2
    except BatchError as exc:
        _dump({"ok": False, "error": exc.to_dict()})
        return 1
    except CatalogError as exc:
        _dump({"ok": False, "error": exc.to_dict()})
        return 1
    except (OSError, json.JSONDecodeError) as exc:
        sys.stderr.write("io error: %s\n" % exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
