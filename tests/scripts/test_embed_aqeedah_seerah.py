from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace


def _load_script():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "embed_aqeedah_seerah.py"
    spec = importlib.util.spec_from_file_location("embed_aqeedah_seerah", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_embed_script_requires_dataset_and_explicit_collection_confirmation_for_writes():
    script = _load_script()

    errors = script.validate_args(
        SimpleNamespace(
            dry_run=False,
            google_api_key="dummy-key",
            dataset_id="",
            collection=script.DEFAULT_COLLECTION,
            confirm_upsert="",
        )
    )

    assert "--dataset-id is required for non-dry-run writes" in errors
    assert "--confirm-upsert must exactly match --collection for non-dry-run writes" in errors
