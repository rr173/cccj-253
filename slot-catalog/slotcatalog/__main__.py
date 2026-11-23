"""Command line interface.

usage:
  python -m slotcatalog run SCENARIO.json
  python -m slotcatalog apply STATE.json BATCH.json [--out NEWSTATE.json]
  python -m slotcatalog query STATE.json VERSION BOOKLET SLOT
  python -m slotcatalog export STATE.json [--booklet BOOKLET]

All output is deterministic JSON (sorted keys, UTF-8, no timestamps).
"""

import json
import sys

from .catalog import Catalog
from .errors import BatchError, CatalogError
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
