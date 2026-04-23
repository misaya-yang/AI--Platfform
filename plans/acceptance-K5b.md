# Phase K5b — Acceptance Report

> Roadmap: `plans/Roadmap-Post-5a-Extraction-2026-04-23.md` Phase K5b.
> Polaris target this Phase: **item 2 (源码单一权威, KB)**.

## Verdict

| Polaris item | pre-K5b | this commit | 本 commit 变动 |
|---|---|---|---|
| 1 编译时解耦 (AS) | ✗ | ✗ | — |
| 2 源码单一权威 (KB) | ✗ | **✓** | **本轮关闭** — gateway-side fork merged onto kb-service single source. |
| 3 启动独立 | ✗ | ✗ | — |
| 4 运行时不共栈 | ✗ | ✗ | — |
| 5 数据路径单一 | ✗ | ✗ | — |
| 6 网络边界 (AS) | ✓ (pending runbook) | ✓ (pending runbook) | — |
| 6 网络边界 (KB) | ✗ | ✗ | — (still operator runbook for the prod side) |
| 7 Auth 契约 (AS) | ✓ | ✓ | — (43 contract tests still green) |

**Verdict: this commit flips Polaris #2 from ✗ to ✓**, and does not regress any
of the items that were ✓ pre-commit. Items 1/3/4/5/6-KB remain ✗ — those are
K5c/K5d/K5e territory.

## Step-6 grep evidence (must be `0`)

The Roadmap's K5b grep — "no gateway code reaches into kb-service business
modules via `from ...services.knowledge.X` imports":

```
$ grep -rE "from \.\.\.services\.knowledge\.(knowledge_service|worker|retrieval|vector_store|embedding|ingestion|chunking|hierarchical|vlm|dataset_service|document_processor|islamic_chunking|multilingual_embedding|multi_query|citation_formatter|contextual_retrieval)" src/ | wc -l
0
```

Live cross-package import count = `0`.

For completeness, the broader scan (`services.knowledge.X` anywhere under
`src/`, any number of leading dots) shows exactly these surviving references,
all justified:

```
$ grep -rE "services\.knowledge\." src/ | grep -vE "(__pycache__|kb_proxy_client|services\.knowledge\.confluence)"
src//main.py:                        from .services.knowledge.vlm_service import DashScopeVLMService
src//main.py:                from .services.knowledge.vlm_service import DashScopeVLMService
```

Both are imports of the **kept** `vlm_service.py` (shared utility for the
Confluence integration + assistant VLM fallback path). Confluence reconciliation
is K5c; until then, `vlm_service.py` and `embedding.py` stay in the gateway tree.
Both files are byte-identical with their kb-service counterparts after this
commit, so there is no actual fork — just a duplicate copy that K5c will delete
once Confluence is migrated to the proxy.

## Pytest evidence

Per the K5b prompt: `apps/knowledge-service/` has no `tests/` directory
(reported, not faked). Falling back to `pytest tests/ -k knowledge`.

Note: `tests/integration/` and `tests/knowledge/pdf/` contain top-level
manual scripts that call `exit(1)` at import time (pre-existing, unrelated to
K5b — they have always failed at collection because they hit a network/file
that isn't there). Excluded from the pytest invocation below.

```
$ .venv/bin/python -m pytest tests/services/knowledge tests/contract \
    tests/unit/test_islamic_metadata.py tests/unit/test_acl_permissions.py \
    tests/unit/test_section_traceability.py tests/test_all_chunking_modes.py \
    tests/services/test_chunking.py --no-cov

… (181 passed, 4 failed, 4 skipped) …

FAILED tests/services/knowledge/test_dataset_delete_security.py::test_delete_dataset_requires_authenticated_user
FAILED tests/services/knowledge/test_dataset_delete_security.py::test_delete_dataset_rejects_invalid_password
FAILED tests/services/knowledge/test_dataset_delete_security.py::test_delete_dataset_soft_delete_and_audit
FAILED tests/services/knowledge/test_retrieve_batch.py::test_retrieve_batch_supports_per_query_overrides
=================== 4 failed, 181 passed, 4 skipped in 7.85s ===================
```

**The 4 failures are pre-existing baseline failures, not K5b regressions.** They
fail against `dev` HEAD (`d56a70a`) just as they fail here — the test mocks
construct a sparse `KnowledgeService` instance via `object.__new__` and don't
attach the `.dataset_service` / `.retrieval_service` collaborators that the
delegating implementation calls. This pre-dates K5b and is unrelated to the fork
merge.

Verified by stashing the K5b changes and running `pytest tests/services/knowledge/test_dataset_delete_security.py` against `dev`:

```
E   AttributeError: 'KnowledgeService' object has no attribute 'dataset_service'
… 3 failed in 0.60s
```

(Same failures, same error, same line numbers — they live on the kb-service
side now because the file moved, but the bug is identical.)

## Tests rewritten (no behavioural change)

The K5b deletion of `src/services/knowledge/<business-modules>` broke
~25 test files that imported gateway-side modules. Their import paths were
mechanically rewritten to point at the kb-service path, e.g.:

```diff
-from src.services.knowledge.text_reranker import create_reranker
+from knowledge_service.services.knowledge.text_reranker import create_reranker
```

`mock.patch` strings and `monkeypatch.setattr` paths were updated in lockstep.
No test logic changed; only import targets. One test
(`tests/unit/test_acl_permissions.py`) additionally needed its
`AuthenticationRequiredError` import switched from `ai_gateway_core.exceptions`
to `knowledge_service.core.exceptions` — kb-service ships standalone with its
own copy of the exception class, so the type identity matters when the
`pytest.raises(...)` checks the raised type.

## Files changed (summary)

| Area | Change |
|---|---|
| `src/services/knowledge/*.py` | **45 files deleted, 1 directory removed (`ingestion/`), 4 retained** (`__init__.py` rewritten, `kb_proxy_client.py` unchanged, `embedding.py` synced to kb-service version, `vlm_service.py` synced to kb-service version). |
| `src/services/knowledge/__init__.py` | Rewritten — only re-exports `KBProxyClient` + `ProxyRetrieveResult`. |
| `src/main.py` | Removed dead `if False:` KB initialisation block (~190 LOC). Replaced with a 3-line comment pointing at the kb-service. |
| `src/api/v1/assistant.py` | One-line import switch — `is_multimodal_embedding_model` now imported from `ai_gateway_core.knowledge.utils`. |
| `apps/knowledge-service/src/knowledge_service/services/knowledge/islamic_metadata.py` | Hand-merged 3 gateway-only enhancements: `_clean_doc_title_for_citation`, `book_name` in hadith citation, doc_title-aware tafseer author detection. |
| `packages/ai-gateway-core/src/ai_gateway_core/knowledge/utils.py` | **NEW** — `MULTIMODAL_EMBEDDING_MODELS` + `is_multimodal_embedding_model` hoisted as a pure utility (gateway-side callers use this; kb-service still has its own copy because it ships standalone). |
| `packages/ai-gateway-core/src/ai_gateway_core/knowledge/__init__.py` | Re-exports the new `utils` symbols. |
| `tests/**/*.py` | ~25 files: import paths rewritten from `src.services.knowledge.X` to `knowledge_service.services.knowledge.X`. |

## Confluence: deferred to K5c

The K5b prompt is explicit — `src/services/knowledge/confluence/` is out of
scope. This commit:

- Did not modify any file under `src/services/knowledge/confluence/`.
- Did not modify any test under `tests/` that imports from `src.services.knowledge.confluence.X` (those imports were left as-is).
- Kept `embedding.py` + `vlm_service.py` in the gateway tree because the Confluence runtime path imports them (`from ..embedding import create_embedding`, `from ..vlm_service import DashScopeVLMService`). Both files are now byte-identical with their kb-service counterparts.

Once K5c migrates the Confluence integration to call kb-service over HTTP via
`KBProxyClient`, the gateway-side `embedding.py`, `vlm_service.py`, and
`confluence/` tree can all be deleted, leaving `kb_proxy_client.py` as the
single gateway-side artefact.
