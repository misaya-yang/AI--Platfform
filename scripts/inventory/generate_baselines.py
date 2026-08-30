#!/usr/bin/env python3
"""ARC-00 fact-baseline runner.

Regenerates every machine-readable baseline for the ``2026-08-post-rag``
baseline id into ``docs/architecture/baselines/2026-08-post-rag/``:

* ``service-topology.json``       — deployment units, bounded contexts, routes;
* ``data-access-inventory.json``  — PostgreSQL objects, table readers/writers,
                                    Qdrant/Redis/object-store namespaces;
* ``loc-baseline.json``           — line counts and the oversized-file ledger;
* ``dependency-baseline.json``    — declared deps and cross-unit import edges;
* ``skip-baseline.json``          — every statically discoverable skip/xfail;
* ``contract-freeze.json``        — SHA-256 digests of offline-computable
                                    public-contract artifacts.

Stdlib-only, like every generator it calls: any machine with Python 3.10+
and a checkout can rebuild the baselines.

Determinism contract: at one Git revision the output is byte-identical on
every run (sorted output, no wall-clock fields, identity derived from Git and
file contents only). ``--verify`` recomputes every baseline in memory and
fails closed if the committed copy drifts from the current tree.

Usage:
    python3 scripts/inventory/generate_baselines.py            # write all
    python3 scripts/inventory/generate_baselines.py --verify   # diff only
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import OUTPUT_DIR  # noqa: E402

# (module, output file) in a fixed order; contract_freeze last because it
# consumes data_access and service_topology results.
GENERATORS = (
    ("service_topology", "service-topology.json"),
    ("data_access", "data-access-inventory.json"),
    ("loc_baseline", "loc-baseline.json"),
    ("dependency_baseline", "dependency-baseline.json"),
    ("skip_baseline", "skip-baseline.json"),
    ("contract_freeze", "contract-freeze.json"),
)


def _serialized(payload: dict) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--verify",
        action="store_true",
        help="recompute every baseline and fail if it differs from the committed copy",
    )
    args = parser.parse_args()

    drift = 0
    for module_name, output_name in GENERATORS:
        module = importlib.import_module(module_name)
        payload = module.build()
        text = _serialized(payload)
        # Fail closed on non-serializable content before touching the tree.
        json.loads(text)
        target = OUTPUT_DIR / output_name
        if args.verify:
            if not target.is_file():
                print(f"DRIFT  {output_name}: missing on disk")
                drift += 1
            elif target.read_text(encoding="utf-8") != text:
                print(f"DRIFT  {output_name}: tree no longer matches the committed baseline")
                drift += 1
            else:
                print(f"OK     {output_name}")
        else:
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            print(f"wrote  {target}")

    if args.verify and drift:
        print(
            f"\n{drift} baseline(s) drifted. Re-run without --verify at the intended "
            "revision, review the diff, and commit the refreshed baseline."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
