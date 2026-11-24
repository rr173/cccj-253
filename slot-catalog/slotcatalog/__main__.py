"""Command line interface.

usage:
  python -m slotcatalog init [--out STATE.json]
  python -m slotcatalog run SCENARIO.json
  python -m slotcatalog apply STATE.json BATCH.json [--out NEWSTATE.json]
  python -m slotcatalog merge BASE.json BRANCH1.json BRANCH2.json [...] --out MERGED.json
  python -m slotcatalog query STATE.json VERSION BOOKLET SLOT
  python -m slotcatalog export STATE.json [--booklet BOOKLET]
  python -m slotcatalog sync-open STATE.json [--out SRC.json]
  python -m slotcatalog sync-apply SRC.json BATCH.json [--out NEWSRC.json]
  python -m slotcatalog sync-checkpoint SRC.json
  python -m slotcatalog sync-package SRC.json [--from-seq N [--from-source HEX]] [--out PKG.json]
  python -m slotcatalog mirror-install PKG.json [--mirror M.json] --out NEWM.json
  python -m slotcatalog mirror-checkpoint M.json
  python -m slotcatalog mirror-query M.json VERSION BOOKLET SLOT
  python -m slotcatalog mirror-export M.json [--booklet BOOKLET]

All output is deterministic JSON (sorted keys, UTF-8, no timestamps).

``merge`` folds two or more offline branch files (priority: first listed
wins) into the shared base state.  The merged state goes to --out (an
existing file is never overwritten); the merge report, listing every
relocated registration, is printed to stdout.  On failure nothing is
written and the exit code is non-zero.

The ``sync-*`` commands run a single-writer sync source on top of a
catalog state: ``sync-open`` opens the channel, ``sync-apply`` commits one
correction batch (every successful batch is a new checkpoint, a failed
batch advances nothing and writes nothing), ``sync-package`` emits a full
package or, with ``--from-seq``, a net delta from that checkpoint to the
source's current one.  The ``mirror-*`` commands run the disclosure
mirror: ``mirror-install`` atomically installs a verified package (a
retry of the just-installed package is a no-op; mismatched, foreign or
damaged packages are rejected with the installed data kept),
``mirror-query`` resolves a printed paper index against the live entries,
``mirror-export`` lists the public endpoints the mirror currently serves.

``query``/``export`` also accept a sync-source file and read the catalog
inside it, so a source operator can compare its views against a mirror's.
"""

import json
import os
import sys
import tempfile

from .catalog import Catalog
from .errors import BatchError, CatalogError, InvalidPackage, OutputExists
from .merge import merge_branches
from .scenario import run_scenario
from .sync import SOURCE_FORMAT, Mirror, SyncSource


def _load_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _load_package_json(path):
    """A package that does not parse is rejected, not an io error."""
    try:
        return _load_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        raise InvalidPackage(
            "package %r cannot be read as JSON (damaged or incomplete): %s"
            % (path, exc)
        )


def _load_catalog(path):
    """Load a catalog state; a sync-source file yields its inner catalog."""
    doc = _load_json(path)
    if isinstance(doc, dict) and doc.get("format") == SOURCE_FORMAT:
        doc = doc["catalog"]
    return Catalog.load(doc)


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


def _pop_flag(args, flag, takes_value=True):
    if flag not in args:
        return None
    i = args.index(flag)
    if takes_value:
        if i + 1 >= len(args):
            raise CatalogError("%s needs a value" % flag)
        value = args[i + 1]
        del args[i : i + 2]
        return value
    del args[i]
    return True


def _cmd_sync_open(args):
    out = _pop_flag(args, "--out")
    if len(args) != 1:
        sys.stderr.write("usage: python -m slotcatalog sync-open STATE.json [--out SRC.json]\n")
        return 2
    doc = _load_json(args[0])
    if isinstance(doc, dict) and doc.get("format") == SOURCE_FORMAT:
        raise CatalogError(
            "%r is already an opened sync source" % args[0]
        )
    src = SyncSource.open(doc)
    _dump(src.save(), out)
    return 0


def _cmd_sync_apply(args):
    out = _pop_flag(args, "--out")
    if len(args) != 2:
        sys.stderr.write(
            "usage: python -m slotcatalog sync-apply SRC.json BATCH.json [--out NEWSRC.json]\n"
        )
        return 2
    src = SyncSource.load(_load_json(args[0]))
    batch = _load_json(args[1])
    if isinstance(batch, list):
        batch = {"ops": batch}
    result = src.apply_batch(batch.get("ops", []), batch_id=batch.get("batch_id"))
    if out is None:
        _dump(src.save())
    else:
        _dump(src.save(), out)
        result = dict(result)
        result["checkpoint"] = src.checkpoint()
        _dump(result)
    return 0


def _cmd_sync_checkpoint(args):
    if len(args) != 1:
        sys.stderr.write("usage: python -m slotcatalog sync-checkpoint SRC.json\n")
        return 2
    src = SyncSource.load(_load_json(args[0]))
    _dump({"ok": True, "checkpoint": src.checkpoint()})
    return 0


def _cmd_sync_package(args):
    out = _pop_flag(args, "--out")
    from_seq = _pop_flag(args, "--from-seq")
    from_source = _pop_flag(args, "--from-source")
    if len(args) != 1:
        sys.stderr.write(
            "usage: python -m slotcatalog sync-package SRC.json "
            "[--from-seq N [--from-source HEX]] [--out PKG.json]\n"
        )
        return 2
    src = SyncSource.load(_load_json(args[0]))
    if from_seq is None:
        if from_source is not None:
            sys.stderr.write("--from-source only makes sense with --from-seq\n")
            return 2
        package = src.full_package()
    else:
        try:
            seq = int(from_seq)
        except ValueError:
            sys.stderr.write("--from-seq needs an integer, got %r\n" % from_seq)
            return 2
        package = src.delta_package(seq, from_source=from_source)
    _dump(package, out)
    return 0


def _cmd_mirror_install(args):
    out = _pop_flag(args, "--out")
    mirror_path = _pop_flag(args, "--mirror")
    if len(args) != 1 or out is None:
        sys.stderr.write(
            "usage: python -m slotcatalog mirror-install PKG.json "
            "[--mirror M.json] --out NEWM.json\n"
        )
        return 2
    if os.path.exists(out):
        raise OutputExists(
            "output %r already exists; refusing to overwrite" % out, out=out
        )
    mirror = None
    if mirror_path is not None:
        mirror = Mirror.load(_load_json(mirror_path))
    package = _load_package_json(args[0])
    if mirror is None:
        mirror = Mirror()
    report = mirror.install(package)
    _write_no_clobber(out, mirror.save())
    _dump(report)
    return 0


def _cmd_mirror_checkpoint(args):
    if len(args) != 1:
        sys.stderr.write("usage: python -m slotcatalog mirror-checkpoint M.json\n")
        return 2
    mirror = Mirror.load(_load_json(args[0]))
    _dump({"ok": True, "checkpoint": mirror.checkpoint()})
    return 0


def _cmd_mirror_query(args):
    if len(args) != 4:
        sys.stderr.write(
            "usage: python -m slotcatalog mirror-query M.json VERSION BOOKLET SLOT\n"
        )
        return 2
    mirror = Mirror.load(_load_json(args[0]))
    _dump(mirror.query(args[1], args[2], args[3]))
    return 0


def _cmd_mirror_export(args):
    booklet = _pop_flag(args, "--booklet")
    if len(args) != 1:
        sys.stderr.write(
            "usage: python -m slotcatalog mirror-export M.json [--booklet BOOKLET]\n"
        )
        return 2
    mirror = Mirror.load(_load_json(args[0]))
    _dump(mirror.export(booklet))
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        sys.stderr.write(__doc__)
        return 2
    command, args = argv[0], argv[1:]
    try:
        if command == "init":
            out = _pop_flag(args, "--out")
            if args:
                sys.stderr.write("usage: python -m slotcatalog init [--out STATE.json]\n")
                return 2
            _dump(Catalog().save(), out)
            return 0
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
            cat = _load_catalog(args[0])
            _dump(cat.query_slot(args[1], args[2], args[3]))
            return 0
        if command == "export":
            booklet = None
            if "--booklet" in args:
                i = args.index("--booklet")
                booklet = args[i + 1]
            cat = _load_catalog(args[0])
            _dump(cat.search_export(booklet))
            return 0
        if command == "sync-open":
            return _cmd_sync_open(args)
        if command == "sync-apply":
            return _cmd_sync_apply(args)
        if command == "sync-checkpoint":
            return _cmd_sync_checkpoint(args)
        if command == "sync-package":
            return _cmd_sync_package(args)
        if command == "mirror-install":
            return _cmd_mirror_install(args)
        if command == "mirror-checkpoint":
            return _cmd_mirror_checkpoint(args)
        if command == "mirror-query":
            return _cmd_mirror_query(args)
        if command == "mirror-export":
            return _cmd_mirror_export(args)
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
