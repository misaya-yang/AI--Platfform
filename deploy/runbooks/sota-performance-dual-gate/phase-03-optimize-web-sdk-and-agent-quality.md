# Phase 03 - Optimize Web SDK and agent quality

- PHASE_ID: SPD-03
- FEATURE_ID: SPD-F004
- DEPENDS_ON: SPD-02

## Outcome

The console and SDKs meet their scale contracts and the three recorded AI Platform
quality failures improve through general harness behavior rather than benchmark routing.

## Scope

In:

- Route/locale/heavy-component loading, long-message rendering, session restore waterfall,
  SDK SSE normalization, evidence receipts, code-execution discovery, and eval normalization.

Out:

- Disabling thinking, prompt-regex routing, default semantic cache, or hiding failed tools.

## Done when

- [x] Public entry gzip is at most 330 KB and Assistant incremental gzip at most 220 KB.
- [x] 200-message restore and 20k-character streaming have no sustained task over 50 ms.
- [x] Python, Java, Dart, and CLI parse the same nested SSE fixture.
- [x] Finance normalization keeps adversarial negatives; engineering returns verified JSON-only code; research retains complete bounded evidence.
- [x] Focused and broad Web/SDK/eval gates pass with no tool or failure visibility regression.

## Verify

| Check | Command or observation | Proves |
| --- | --- | --- |
| Web | `pnpm -C web type-check && pnpm -C web lint && pnpm -C web build` plus configured Playwright | Bundle, rendering, and journey contracts pass. |
| SDK/eval | Shared SSE fixture tests and focused native-parity validators | Cross-client envelope and quality fixes are real and non-overfit. |

Use the smallest check that can falsify the outcome. Add runtime, browser, migration, or reviewer evidence only when the outcome requires it.

## Stop or confirm

- Stop when a dependency is incomplete, the same failure repeats without a new hypothesis, or the iteration cap is reached.
- Set `waiting_confirmation` only for a real authority boundary.
- Treat any evaluator relaxation as required review; it must retain explicit negative mutations.
