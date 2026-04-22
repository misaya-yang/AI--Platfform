"""Artifact store + template + signing + service tests (Phase 4)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from docgen.storage import (
    LocalArtifactStore,
    MemoryArtifactStore,
)
from docgen.storage.base import compute_sha256
from docgen.storage.signing import sign_url, verify_url
from docgen.templates import (
    Template,
    TemplateRegistry,
    default_registry,
)


# -------- signing --------


def test_sign_and_verify_url_roundtrip():
    url = "file:///tmp/foo/bar.docx"
    signed = sign_url(url, ttl_seconds=60, tenant_id="acme")
    assert verify_url(signed)


def test_sign_url_tampered_fails():
    url = "file:///tmp/foo/bar.docx"
    signed = sign_url(url, ttl_seconds=60, tenant_id="acme")
    tampered = signed.replace("acme", "other")
    assert not verify_url(tampered)


def test_sign_url_expired_fails():
    url = "file:///tmp/foo/bar.docx"
    # TTL of -1 second → already expired.
    signed = sign_url(url, ttl_seconds=-1, tenant_id="acme")
    assert not verify_url(signed)


def test_sign_url_tampered_signature_rejected_timing_safe():
    """Flip a char in the sig — verify must reject via hmac.compare_digest."""
    url = "file:///tmp/foo/bar.docx"
    signed = sign_url(url, ttl_seconds=60, tenant_id="acme")
    # Locate the sig= param and flip one hex char.
    import re
    m = re.search(r"sig=([0-9a-f]+)", signed)
    assert m is not None
    sig = m.group(1)
    # Flip first char: 'a' <-> 'b', else toggle low bit.
    flipped_first = "b" if sig[0] != "b" else "a"
    tampered_sig = flipped_first + sig[1:]
    tampered = signed.replace(f"sig={sig}", f"sig={tampered_sig}")
    assert not verify_url(tampered)


def test_sign_url_nonascii_signature_rejected():
    """Attacker-controlled non-ASCII sig must not raise — just fail."""
    url = "file:///tmp/foo/bar.docx"
    signed = sign_url(url, ttl_seconds=60, tenant_id="acme")
    import re
    tampered = re.sub(r"sig=[0-9a-f]+", "sig=ééé", signed)
    assert verify_url(tampered) is False


# -------- LocalArtifactStore --------


@pytest.mark.asyncio
async def test_local_store_put_get_list_delete(tmp_path):
    store = LocalArtifactStore(tmp_path / "artifacts")

    # Seed a fake artifact on disk.
    src = tmp_path / "sample.docx"
    src.write_bytes(b"PK\x03\x04--fake docx--")
    art = await store.put(src, tenant_id="t1", session_id="s1", turn_id="turn-1", doc_type="docx")
    assert art.artifact_id
    assert art.sha256 == compute_sha256(src)

    roundtrip = await store.get(art.artifact_id, tenant_id="t1")
    assert roundtrip is not None
    assert roundtrip.size_bytes == art.size_bytes

    url = await store.download_url(art.artifact_id, tenant_id="t1")
    assert "tenant=t1" in url and "sig=" in url and verify_url(url)

    listing = await store.list_session(tenant_id="t1", session_id="s1")
    assert len(listing) == 1

    assert await store.delete(art.artifact_id, tenant_id="t1")
    assert await store.get(art.artifact_id, tenant_id="t1") is None


@pytest.mark.asyncio
async def test_local_store_tenant_isolation(tmp_path):
    """T1 cannot see T2's artifacts even if they share an artifact_id guess."""
    store = LocalArtifactStore(tmp_path / "artifacts")
    f1 = tmp_path / "a.docx"
    f2 = tmp_path / "b.docx"
    f1.write_bytes(b"aaa")
    f2.write_bytes(b"bbb")
    a1 = await store.put(f1, tenant_id="alice", session_id="s", turn_id="t", doc_type="docx")
    await store.put(f2, tenant_id="bob", session_id="s", turn_id="t", doc_type="docx")

    # Look up alice's artifact as bob → None.
    assert await store.get(a1.artifact_id, tenant_id="bob") is None
    # Alice sees one, Bob sees one.
    a_list = await store.list_session(tenant_id="alice", session_id="s")
    b_list = await store.list_session(tenant_id="bob", session_id="s")
    assert len(a_list) == 1 and len(b_list) == 1
    assert a_list[0].artifact_id != b_list[0].artifact_id


# -------- MemoryArtifactStore --------


@pytest.mark.asyncio
async def test_memory_store_basic(tmp_path):
    store = MemoryArtifactStore()
    src = tmp_path / "sheet.xlsx"
    src.write_bytes(b"PK--xlsx")
    a = await store.put(src, tenant_id="t", session_id="s", turn_id="u", doc_type="xlsx")
    assert (await store.get(a.artifact_id, tenant_id="t")) is not None
    # Wrong tenant → None
    assert (await store.get(a.artifact_id, tenant_id="other")) is None


# -------- templates --------


def test_builtin_templates_present():
    reg = default_registry()
    names = [t.name for t in reg.list()]
    assert "default" in names
    assert "minimal" in names
    assert "enterprise-dark" in names


def test_template_theme_resolves():
    reg = default_registry()
    t = reg.get("default")
    theme = t.theme()
    assert len(theme.palette) >= 1


def test_tenant_template_overrides_isolated():
    reg = default_registry()
    custom = Template(
        name="ours",
        description="Tenant-branded template",
        palette_name="bold-magenta",
        font_pair_name="helvetica-helvetica",
    )
    reg.register(custom, tenant_id="acme")
    # Only acme sees "ours"
    assert any(t.name == "ours" for t in reg.list(tenant_id="acme"))
    assert all(t.name != "ours" for t in reg.list(tenant_id="other"))


# -------- DocgenService --------


@pytest.mark.asyncio
async def test_docgen_service_end_to_end(tmp_path):
    from docgen.service import DocgenService, GenerateRequest

    store = LocalArtifactStore(tmp_path / "artifacts")
    svc = DocgenService(artifact_store=store, verify=True, max_fix_rounds=1)

    req = GenerateRequest(
        tenant_id="t1",
        session_id="sess",
        turn_id="turn-1",
        doc_type="docx",
        title="service-smoke",
        goal="end-to-end smoke",
        body_markdown="# Hi\nPara\n",
    )
    resp = await svc.generate(req)
    assert resp.artifact.tenant_id == "t1"
    assert resp.download_url.startswith("file://")
    assert resp.passed


@pytest.mark.asyncio
async def test_docgen_service_stream_events_and_artifact(tmp_path):
    from docgen.service import DocgenService, GenerateRequest

    store = LocalArtifactStore(tmp_path / "artifacts")
    svc = DocgenService(artifact_store=store, verify=False)  # faster

    req = GenerateRequest(
        tenant_id="t1",
        session_id="sess",
        turn_id="turn-1",
        doc_type="docx",
        title="stream-smoke",
        goal="stream",
        body_markdown="# S\nX",
    )
    events = []
    async for ev in svc.stream(req):
        events.append(ev)
    event_names = [e.event for e in events]
    assert event_names[:4] == ["plan", "ir", "render", "done"]
    assert event_names[-1] == "artifact"
    # artifact event carries a signed URL and non-zero size
    last = events[-1]
    assert last.data["url"] and last.data["size"] > 0


def test_docgen_service_exposes_skills_system_prompt_stub():
    from docgen.service import DocgenService

    svc = DocgenService()
    stub = svc.system_prompt_stub()
    for t in ("docx", "pptx", "xlsx", "pdf"):
        assert f"- {t}:" in stub


def test_docgen_service_expand_skill_for_format():
    from docgen.service import DocgenService

    svc = DocgenService()
    expanded = svc.expand_skill("pptx", resources=["editing"])
    assert "Skill: pptx" in expanded
    assert "Resource: editing.md" in expanded
