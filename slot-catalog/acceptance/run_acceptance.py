#!/usr/bin/env python3
"""Re-runnable acceptance harness.

Executes every ``*.scenario.json`` in this directory through the CLI
(``python -m slotcatalog run``) and diffs the output against the matching
``*.expected.json`` golden file.

    python3 acceptance/run_acceptance.py           # check
    python3 acceptance/run_acceptance.py --update  # bless current output

Exit code is non-zero if any scenario differs from its golden.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent


def run_scenario(path):
    proc = subprocess.run(
        [sys.executable, "-m", "slotcatalog", "run", str(path)],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    if proc.returncode != 0:
        raise SystemExit(
            "scenario %s crashed (exit %d):\n%s" % (path.name, proc.returncode, proc.stderr)
        )
    return proc.stdout


def main():
    update = "--update" in sys.argv
    scenarios = sorted(HERE.glob("*.scenario.json"))
    if not scenarios:
        print("no scenarios found")
        return 1
    failures = 0
    for scenario in scenarios:
        expected = scenario.with_name(
            scenario.name.replace(".scenario.json", ".expected.json")
        )
        actual = run_scenario(scenario)
        if update or not expected.exists():
            expected.write_text(actual, encoding="utf-8")
            print("WROTE %s" % expected.name)
            continue
        if actual != expected.read_text(encoding="utf-8"):
            failures += 1
            diff_path = scenario.with_name(
                scenario.name.replace(".scenario.json", ".actual.json")
            )
            diff_path.write_text(actual, encoding="utf-8")
            print("FAIL %s (actual output saved to %s)" % (scenario.name, diff_path.name))
        else:
            print("PASS %s" % scenario.name)
    if failures:
        print("%d scenario(s) differ from their golden files" % failures)
        return 1
    print("all %d scenario(s) match" % len(scenarios))
    return 0


if __name__ == "__main__":
    sys.exit(main())
