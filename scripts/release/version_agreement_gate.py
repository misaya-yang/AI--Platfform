#!/usr/bin/env python3
"""Verify every release service's live /version projection against one manifest."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from scripts.release.compatibility_manifest import SERVICES, ManifestError, _load


class VersionAgreementError(RuntimeError):
    pass


def _fetch(url: str) -> dict[str, Any]:
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise VersionAgreementError(
            "version endpoint must be an http(s) URL without userinfo, query, or fragment"
        )
    try:
        with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310
            if response.status != 200:
                raise VersionAgreementError(f"version endpoint returned HTTP {response.status}")
            payload = json.load(response)
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise VersionAgreementError("version endpoint unavailable") from exc
    if not isinstance(payload, dict):
        raise VersionAgreementError(f"version endpoint did not return an object: {url}")
    return payload


def verify(manifest: dict[str, Any], endpoints: dict[str, Any]) -> dict[str, Any]:
    if (
        manifest.get("schema_version") != "ai-platform/compatibility-manifest/v1"
        or manifest.get("status") not in {"release_candidate", "released"}
    ):
        raise VersionAgreementError("compatibility manifest is not a runnable release")
    if set(endpoints) != SERVICES:
        raise VersionAgreementError("version endpoint set differs from compatibility services")
    source = manifest.get("source") if isinstance(manifest.get("source"), dict) else {}
    services = manifest.get("services") if isinstance(manifest.get("services"), dict) else {}
    observed: dict[str, str] = {}
    for service in sorted(SERVICES):
        url = endpoints[service]
        if not isinstance(url, str):
            raise VersionAgreementError(f"version endpoint URL is invalid: {service}")
        payload = _fetch(url)
        record = services.get(service) if isinstance(services.get(service), dict) else {}
        expected = {
            "schema_version": "ai-platform/service-version/v1",
            "service_id": service,
            "release_id": manifest.get("release_id"),
            "git_sha": source.get("git_sha"),
            "image_digest": record.get("image_digest"),
        }
        if any(payload.get(key) != value for key, value in expected.items()):
            raise VersionAgreementError(f"live /version identity mismatch: {service}")
        manifest_sha = payload.get("manifest_sha256")
        if not isinstance(manifest_sha, str) or len(manifest_sha) != 64:
            raise VersionAgreementError(f"live manifest digest is invalid: {service}")
        observed[service] = manifest_sha
    if len(set(observed.values())) != 1:
        raise VersionAgreementError("services did not consume the same manifest bytes")
    return {"result": "pass", "services": sorted(observed), "manifest_sha256": next(iter(observed.values()))}


def main() -> int:
    manifest_path = os.environ.get("AI_PLATFORM_COMPATIBILITY_MANIFEST_PATH", "")
    raw_endpoints = os.environ.get("AI_PLATFORM_VERSION_ENDPOINTS", "")
    try:
        if not manifest_path or not raw_endpoints:
            raise VersionAgreementError("compatibility manifest and version endpoints are required")
        endpoints = json.loads(raw_endpoints)
        if not isinstance(endpoints, dict):
            raise VersionAgreementError("version endpoints must be a JSON object")
        result = verify(_load(Path(manifest_path), "compatibility manifest"), endpoints)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (VersionAgreementError, ManifestError, json.JSONDecodeError) as exc:
        print(f"VERSION AGREEMENT ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
