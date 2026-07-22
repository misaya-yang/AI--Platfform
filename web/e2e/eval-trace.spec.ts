import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page } from "@playwright/test";

import { installClientAuth } from "./support/helpers";

const permissions = [
  "console:dashboard:view",
  "console:eval:view",
  "console:eval:run",
  "conversation:playground:access",
];

function jsonResponse(body: unknown, status = 200) {
  return {
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  };
}

function nowIso() {
  return new Date("2026-06-26T08:00:00.000Z").toISOString();
}

function traceSummary(overrides: Record<string, unknown> = {}) {
  return {
    trace_id: "11111111-1111-4111-8111-111111111111",
    trace_family: "assistant",
    workflow_kind: "ai_assistant_chat",
    tenant_id: "tenant-a",
    user_id: "eval-user",
    session_id: "session-a",
    run_id: "run-a",
    request_id: "request-a",
    model_id: "qwen3.6-plus",
    provider: "dashscope",
    status: "succeeded",
    started_at: nowIso(),
    ended_at: nowIso(),
    first_token_latency_ms: 120,
    total_latency_ms: 980,
    input_tokens: 10,
    output_tokens: 20,
    total_tokens: 30,
    total_cost_cents: 0,
    input_preview: "hello Authorization: Bearer [redacted]",
    output_preview: "safe assistant answer",
    redaction_state: { input_preview: "redacted_truncated", payloads: "redacted_truncated" },
    metadata: {
      mode: "streaming_first",
      transcript_locator: {
        locator_version: "assistant-transcript-v1",
        session_id: "session-a",
        run_id: "run-a",
        request_id: "request-a",
        turn_index: 4,
        turn_id: "session-a:turn:4",
        previous_user_turns: 3,
        history_message_count: 6,
        message_index: 7,
        current_message_preview: "hello refund transcript anchor",
        transcript_excerpt:
          "user: previous refund policy question\nassistant: prior answer\nuser: hello refund transcript anchor",
        transcript_fingerprint: "abc123locator",
        excerpt_message_count: 3,
        bounded: true,
      },
    },
    scores_count: 1,
    created_at: nowIso(),
    updated_at: nowIso(),
    ...overrides,
  };
}

async function installEvalHarness(page: Page) {
  await installClientAuth(page, {
    user_id: "eval-user",
    email: "eval@example.com",
    display_name: "Eval User",
    permissions,
    effective_permissions: permissions,
  });

  let scores = [
    {
      score_id: "22222222-2222-4222-8222-222222222222",
      trace_id: "11111111-1111-4111-8111-111111111111",
      span_id: null,
      score_name: "quality",
      score_type: "numeric",
      numeric_value: 0.9,
      boolean_value: null,
      categorical_value: null,
      text_value: null,
      label: "good",
      explanation: "Grounded answer",
      scorer_type: "human",
      evaluator_version: null,
      created_by: "eval-user",
      metadata: {},
      created_at: nowIso(),
    },
  ];
  const datasetId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
  const importedCaseIds = new Set<string>();
  const evaluatorId = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
  const experimentId = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
  const observedTraceFamilies: string[] = [];

  await page.route("**/api/v1/eval/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());

    if (request.method() === "GET" && url.pathname.endsWith("/api/v1/eval/dashboard")) {
      await route.fulfill(
        jsonResponse({
          metrics: {
            total_traces: 4,
            scored_traces: 1,
            example_count: 10,
            pass_rate: 0.9,
            trajectory_pass_rate: 0.96,
            critical_failures: 0,
            judge_pending_count: 2,
            latest_baseline: "assistant-baseline",
            latest_candidate: "candidate",
          },
          run_health: { succeeded_runs: 1, failed_runs: 0 },
          queue_health: { queued_jobs: 0, failed_jobs: 0 },
          latest_gate_status: { status: "pass" },
        })
      );
      return;
    }

    if (request.method() === "GET" && url.pathname.endsWith("/api/v1/eval/summary")) {
      await route.fulfill(
        jsonResponse({
          total_traces: 4,
          failed_traces: 1,
          succeeded_traces: 3,
          assistant_traces: 2,
          langgraph_traces: 1,
          rag_traces: 1,
          avg_latency_ms: 980,
          p95_latency_ms: 1610,
          total_tokens: 60,
          total_cost_cents: 0,
          scored_traces: 1,
          window_days: Number(url.searchParams.get("days") || 7),
        })
      );
      return;
    }

    if (request.method() === "GET" && url.pathname.endsWith("/api/v1/eval/knowledge/summary")) {
      await route.fulfill(
        jsonResponse({
          window_days: Number(url.searchParams.get("days") || 7),
          dataset_id: url.searchParams.get("dataset_id"),
          rag_traces: 1,
          ragas_scored_traces: 0,
          metrics: [
            {
              metric: "context_relevancy",
              average_score: 0.0,
              scored_count: 0,
              pass_count: 0,
              fail_count: 0,
              review_count: 0,
            },
          ],
          latest_judge_model: null,
        })
      );
      return;
    }

    if (
      request.method() === "POST" &&
      url.pathname.endsWith("/api/v1/eval/knowledge/support-kb/batch-score")
    ) {
      const payload = request.postDataJSON();
      expect(payload.evaluator_id).toBe(evaluatorId);
      expect(payload.only_unscored).toBe(true);
      await route.fulfill(
        jsonResponse(
          {
            dataset_id: "support-kb",
            evaluator_id: evaluatorId,
            matched: 1,
            queued: 1,
            skipped: 0,
          },
          202
        )
      );
      return;
    }

    if (request.method() === "GET" && url.pathname.endsWith("/api/v1/eval/datasets")) {
      await route.fulfill(
        jsonResponse({
          datasets: [],
          total: 0,
          limit: Number(url.searchParams.get("limit") || 50),
          offset: Number(url.searchParams.get("offset") || 0),
        })
      );
      return;
    }

    if (request.method() === "GET" && url.pathname.endsWith(`/api/v1/eval/datasets/${datasetId}/examples`)) {
      await route.fulfill(
        jsonResponse({
          examples: [],
          total: 0,
          limit: Number(url.searchParams.get("limit") || 200),
          offset: Number(url.searchParams.get("offset") || 0),
        })
      );
      return;
    }

    if (request.method() === "GET" && url.pathname.endsWith("/api/v1/eval/evaluators")) {
      await route.fulfill(
        jsonResponse({
          evaluators: [],
          total: 0,
          limit: Number(url.searchParams.get("limit") || 50),
          offset: Number(url.searchParams.get("offset") || 0),
        })
      );
      return;
    }

    if (request.method() === "GET" && url.pathname.endsWith("/api/v1/eval/experiments")) {
      await route.fulfill(
        jsonResponse({
          experiments: [],
          total: 0,
          limit: Number(url.searchParams.get("limit") || 50),
          offset: Number(url.searchParams.get("offset") || 0),
        })
      );
      return;
    }

    if (
      request.method() === "GET" &&
      url.pathname.endsWith(`/api/v1/eval/experiments/${experimentId}`)
    ) {
      await route.fulfill(
        jsonResponse({
          experiment_id: experimentId,
          tenant_id: "tenant-a",
          dataset_id: datasetId,
          name: "assistant-baseline",
          description: "",
          baseline_run_id: null,
          target_config: { trace_family: "assistant", model_id: "current" },
          metadata: { source: "eval_console" },
          created_by: "eval-user",
          created_at: nowIso(),
          updated_at: nowIso(),
          runs: [],
        })
      );
      return;
    }

    if (request.method() === "GET" && url.pathname.endsWith("/api/v1/eval/threads/session-a")) {
      await route.fulfill(
        jsonResponse({
          thread_id: "session-a",
          traces: [
            traceSummary(),
            traceSummary({
              trace_id: "33333333-3333-4333-8333-333333333333",
              status: "failed",
              run_id: "run-b",
              request_id: "request-b",
              total_latency_ms: 1610,
              scores_count: 0,
              output_preview: "tool failed with password=[redacted]",
              metadata: {
                mode: "streaming_first",
                transcript_locator: {
                  turn_index: 2,
                  turn_id: "session-a:turn:2",
                  current_message_preview: "unrelated account lookup",
                  transcript_excerpt: "user: unrelated account lookup",
                  transcript_fingerprint: "def456locator",
                },
              },
            }),
          ],
          total: 2,
          metrics: { turn_count: 2, failed_traces: 1, total_latency_ms: 2590 },
        })
      );
      return;
    }

    if (
      request.method() === "GET" &&
      url.pathname.endsWith("/api/v1/eval/experiment-runs/ffffffff-ffff-4fff-8fff-ffffffffffff")
    ) {
      await route.fulfill(
        jsonResponse({
          run_id: "ffffffff-ffff-4fff-8fff-ffffffffffff",
          experiment_id: experimentId,
          tenant_id: "tenant-a",
          evaluator_id: evaluatorId,
          dataset_id: datasetId,
          status: "succeeded",
          target_snapshot: { trace_family: "assistant" },
          score_summary: { quality: 0.9 },
          metrics: { trace_count: 1 },
          error_message: null,
          created_by: "eval-user",
          started_at: nowIso(),
          finished_at: nowIso(),
          created_at: nowIso(),
          updated_at: nowIso(),
        })
      );
      return;
    }

    if (
      request.method() === "GET" &&
      url.pathname.endsWith("/api/v1/eval/experiment-runs/ffffffff-ffff-4fff-8fff-ffffffffffff/results")
    ) {
      await route.fulfill(
        jsonResponse({
          run: {
            run_id: "ffffffff-ffff-4fff-8fff-ffffffffffff",
            experiment_id: experimentId,
            tenant_id: "tenant-a",
            evaluator_id: evaluatorId,
            dataset_id: datasetId,
            status: "succeeded",
            run_mode: "rescore_trace",
            target_snapshot: { trace_family: "assistant" },
            score_summary: { average_score: 0.9, scored_count: 1 },
            metrics: { trace_count: 1 },
            created_by: "eval-user",
            created_at: nowIso(),
          },
          cases: [],
          total: 0,
          limit: 200,
          offset: 0,
        })
      );
      return;
    }

    if (request.method() === "GET" && url.pathname.endsWith("/api/v1/eval/traces")) {
      const traceFamily = url.searchParams.get("trace_family") || "assistant";
      observedTraceFamilies.push(traceFamily);
      const traces =
        traceFamily === "rag"
          ? [
              traceSummary({
                trace_id: "55555555-5555-4555-8555-555555555555",
                trace_family: "rag",
                workflow_kind: "rag_retrieval",
                run_id: "rag-run-a",
                request_id: "rag-request-a",
                model_id: null,
                provider: "knowledge-service",
                output_preview: "2 retrieved documents",
                metadata: {
              dataset_id: "support-kb",
              "gen_ai.retrieval.query.text": "refund policy",
              retrieval: { dataset_ids: ["support-kb"], document_count: 2 },
            },
            input_preview: "refund policy",
                scores_count: 0,
              }),
            ]
          : traceFamily === "langgraph_proxy"
            ? [
                traceSummary({
                  trace_id: "66666666-6666-4666-8666-666666666666",
                  trace_family: "langgraph_proxy",
                  workflow_kind: "langgraph_agent_run",
                  session_id: "thread-a",
                  run_id: "lg-run-a",
                  request_id: "lg-request-a",
                  model_id: null,
                  provider: "langgraph",
                  output_preview: "upstream_status=200 streaming=true",
                  metadata: { upstream_route: "/threads/thread-a/runs/stream" },
                  scores_count: 0,
                }),
              ]
          : [
              traceSummary(),
              traceSummary({
                trace_id: "33333333-3333-4333-8333-333333333333",
                status: "failed",
                run_id: "run-b",
                request_id: "request-b",
                total_latency_ms: 1610,
                scores_count: 0,
                output_preview: "tool failed with password=[redacted]",
                metadata: {
                  mode: "streaming_first",
                  transcript_locator: {
                    turn_index: 2,
                    turn_id: "session-a:turn:2",
                    current_message_preview: "unrelated account lookup",
                    transcript_excerpt: "user: unrelated account lookup",
                  },
                },
              }),
            ];
      const transcriptQuery = (url.searchParams.get("transcript_query") || "").toLowerCase();
      const requestId = url.searchParams.get("request_id") || "";
      const turnIndex = url.searchParams.get("turn_index") || "";
      const filtered = traces.filter((trace) => {
        const haystack = JSON.stringify(trace).toLowerCase();
        if (transcriptQuery && !haystack.includes(transcriptQuery)) return false;
        if (requestId && trace.request_id !== requestId) return false;
        if (
          turnIndex &&
          String((trace.metadata.transcript_locator as { turn_index?: unknown }).turn_index || "") !== turnIndex
        ) {
          return false;
        }
        return true;
      });
      await route.fulfill(
        jsonResponse({
          traces: filtered,
          total: filtered.length,
          limit: 100,
          offset: 0,
        })
      );
      return;
    }

    if (
      request.method() === "GET" &&
      url.pathname.endsWith("/api/v1/eval/traces/66666666-6666-4666-8666-666666666666")
    ) {
      if (url.searchParams.get("trace_family") !== "langgraph_proxy") {
        await route.fulfill(jsonResponse({ detail: "wrong trace family" }, 404));
        return;
      }
      await route.fulfill(
        jsonResponse({
          trace: traceSummary({
            trace_id: "66666666-6666-4666-8666-666666666666",
            trace_family: "langgraph_proxy",
            workflow_kind: "langgraph_agent_run",
            session_id: "thread-a",
            run_id: "lg-run-a",
            request_id: "lg-request-a",
            model_id: null,
            provider: "langgraph",
            output_preview: "upstream_status=200 streaming=true",
            metadata: { upstream_route: "/threads/thread-a/runs/stream" },
            scores_count: 0,
          }),
          spans: [
            {
              span_id: "77777777-7777-4777-8777-777777777777",
              trace_id: "66666666-6666-4666-8666-666666666666",
              parent_span_id: null,
              span_kind: "gateway_proxy",
              name: "upstream_request",
              status: "succeeded",
              sequence_no: 1,
              started_at: nowIso(),
              ended_at: nowIso(),
              duration_ms: 320,
              input_preview: "POST /threads/thread-a/runs/stream",
              output_preview: "streaming response",
              attributes: { thread_id: "thread-a", run_id: "lg-run-a" },
              error_type: null,
              error_message: null,
              created_at: nowIso(),
            },
          ],
          events: [
            {
              event_id: "88888888-8888-4888-8888-888888888888",
              trace_id: "66666666-6666-4666-8666-666666666666",
              span_id: null,
              event_type: "proxy_request_finished",
              sequence_no: 2,
              occurred_at: nowIso(),
              payload: { data: { upstream_status: 200, streaming: true } },
              payload_size_bytes: 70,
              redacted: true,
              created_at: nowIso(),
            },
          ],
          scores: [],
        })
      );
      return;
    }

    if (
      request.method() === "GET" &&
      url.pathname.endsWith("/api/v1/eval/traces/55555555-5555-4555-8555-555555555555")
    ) {
      if (url.searchParams.get("trace_family") !== "rag") {
        await route.fulfill(jsonResponse({ detail: "wrong trace family" }, 404));
        return;
      }
      await route.fulfill(
        jsonResponse({
          trace: traceSummary({
            trace_id: "55555555-5555-4555-8555-555555555555",
            trace_family: "rag",
            workflow_kind: "rag_retrieval",
            run_id: "rag-run-a",
            request_id: "rag-request-a",
            model_id: null,
            provider: "knowledge-service",
            output_preview: "2 retrieved documents",
            metadata: {
              dataset_id: "support-kb",
              "gen_ai.retrieval.query.text": "refund policy",
              retrieval: { dataset_ids: ["support-kb"], document_count: 2 },
            },
            input_preview: "refund policy",
            scores_count: 0,
          }),
          spans: [
            {
              span_id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
              trace_id: "55555555-5555-4555-8555-555555555555",
              parent_span_id: null,
              span_kind: "retriever",
              name: "rag_retrieval",
              status: "succeeded",
              sequence_no: 2,
              started_at: nowIso(),
              ended_at: nowIso(),
              duration_ms: 64,
              input_preview: "refund policy",
              output_preview: "2 retrieved documents",
              attributes: {
                "openinference.span.kind": "RETRIEVER",
                "gen_ai.retrieval.query.text": "refund policy",
                "retrieval.document_count": 2,
                retrieval: {
                  documents: [
                    { content_eval: "Refunds are allowed within 30 days." },
                    { content_eval: "Contact support for exceptions." },
                  ],
                },
              },
              error_type: null,
              error_message: null,
              created_at: nowIso(),
            },
          ],
          events: [
            {
              event_id: "ffffffff-ffff-4fff-8fff-ffffffffffff",
              trace_id: "55555555-5555-4555-8555-555555555555",
              span_id: null,
              event_type: "rag_retrieval_completed",
              sequence_no: 2,
              occurred_at: nowIso(),
              payload: { data: { dataset_ids: ["support-kb"], document_count: 2 } },
              payload_size_bytes: 72,
              redacted: true,
              created_at: nowIso(),
            },
          ],
          scores: [],
        })
      );
      return;
    }

    if (
      request.method() === "GET" &&
      url.pathname.endsWith("/api/v1/eval/traces/11111111-1111-4111-8111-111111111111")
    ) {
      await route.fulfill(
        jsonResponse({
          trace: traceSummary({ scores_count: scores.length }),
          spans: [
            {
              span_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
              trace_id: "11111111-1111-4111-8111-111111111111",
              parent_span_id: null,
              span_kind: "lifecycle",
              name: "assistant_run",
              status: "succeeded",
              sequence_no: 0,
              started_at: nowIso(),
              ended_at: nowIso(),
              duration_ms: 980,
              input_preview: "hello Authorization: Bearer [redacted]",
              output_preview: "safe assistant answer",
              attributes: { mode: "streaming_first" },
              error_type: null,
              error_message: null,
              created_at: nowIso(),
            },
            {
              span_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
              trace_id: "11111111-1111-4111-8111-111111111111",
              parent_span_id: null,
              span_kind: "model_invocation",
              name: "streaming_first_generation",
              status: "succeeded",
              sequence_no: 2,
              started_at: nowIso(),
              ended_at: nowIso(),
              duration_ms: 740,
              input_preview: "hello",
              output_preview: "safe assistant answer",
              attributes: { usage: { input_tokens: 10, output_tokens: 20 } },
              error_type: null,
              error_message: null,
              created_at: nowIso(),
            },
          ],
          events: [
            {
              event_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
              trace_id: "11111111-1111-4111-8111-111111111111",
              span_id: null,
              event_type: "run_started",
              sequence_no: 1,
              occurred_at: nowIso(),
              payload: { data: { run_id: "run-a", request_id: "request-a" } },
              payload_size_bytes: 82,
              redacted: true,
              created_at: nowIso(),
            },
            {
              event_id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
              trace_id: "11111111-1111-4111-8111-111111111111",
              span_id: null,
              event_type: "run_finished",
              sequence_no: 9,
              occurred_at: nowIso(),
              payload: { data: { status: "succeeded" } },
              payload_size_bytes: 42,
              redacted: true,
              created_at: nowIso(),
            },
          ],
          scores,
        })
      );
      return;
    }

    if (
      request.method() === "GET" &&
      url.pathname.endsWith("/api/v1/eval/traces/11111111-1111-4111-8111-111111111111/export")
    ) {
      await route.fulfill(
        jsonResponse({
          trace_id: "11111111-1111-4111-8111-111111111111",
          format: url.searchParams.get("format") || "openinference",
          payload: {
            trace_id: "11111111-1111-4111-8111-111111111111",
            root: { "openinference.span.kind": "AGENT" },
          },
        })
      );
      return;
    }

    if (
      request.method() === "POST" &&
      url.pathname.endsWith("/api/v1/eval/traces/11111111-1111-4111-8111-111111111111/scores")
    ) {
      const payload = request.postDataJSON();
      expect(payload.tenant_id).toBeUndefined();
      const score = {
        score_id: "44444444-4444-4444-8444-444444444444",
        trace_id: "11111111-1111-4111-8111-111111111111",
        span_id: null,
        score_name: payload.score_name,
        score_type: payload.score_type,
        numeric_value: payload.numeric_value,
        boolean_value: payload.boolean_value,
        categorical_value: payload.categorical_value,
        text_value: payload.text_value,
        label: payload.label,
        explanation: payload.explanation,
        scorer_type: "human",
        evaluator_version: null,
        created_by: "eval-user",
        metadata: payload.metadata || {},
        created_at: nowIso(),
      };
      scores = [score, ...scores];
      await route.fulfill(jsonResponse(score, 201));
      return;
    }

    if (request.method() === "POST" && url.pathname.endsWith("/api/v1/eval/datasets")) {
      const payload = request.postDataJSON();
      await route.fulfill(
        jsonResponse(
          {
            dataset_id: datasetId,
            tenant_id: "tenant-a",
            name: payload.name,
            description: payload.description || "",
            version: payload.version || "v1",
            schema: payload.schema || {},
            metadata: payload.metadata || {},
            created_by: "eval-user",
            created_at: nowIso(),
            updated_at: nowIso(),
          },
          201
        )
      );
      return;
    }

    if (
      request.method() === "POST" &&
      url.pathname.endsWith(`/api/v1/eval/datasets/${datasetId}/examples:import`)
    ) {
      const payload = request.postDataJSON() as {
        examples?: unknown[];
        mode?: "skip_duplicates" | "append";
      };
      const examples = Array.isArray(payload.examples) ? payload.examples : [];
      const mode = payload.mode ?? "skip_duplicates";
      let imported = 0;
      let skipped = 0;
      const createdExamples: Record<string, unknown>[] = [];
      for (const example of examples) {
        const row = example as Record<string, unknown>;
        const caseId = typeof row.case_id === "string" ? row.case_id.trim() : "";
        if (caseId === "assistant.11111111") {
          const input = row.input as Record<string, unknown>;
          const trajectory = row.expected_trajectory as Record<string, unknown>;
          const tools = trajectory.tools as Array<Record<string, unknown>>;
          const assertions = row.assertions as Array<Record<string, unknown>>;
          expect(input.message).toContain("hello Authorization: Bearer [redacted]");
          expect(tools[0]).toMatchObject({
            name: "lookup_policy",
            required: true,
            arguments_subset: { account_id: "known" },
            max_calls: 1,
            status: "succeeded",
          });
          expect(assertions.map((assertion) => assertion.type)).toEqual(
            expect.arrayContaining(["tool_called", "no_sensitive_output"])
          );
        }
        if (mode === "skip_duplicates" && caseId && importedCaseIds.has(caseId)) {
          skipped += 1;
          continue;
        }
        imported += 1;
        if (caseId) importedCaseIds.add(caseId);
        createdExamples.push({
          example_id: `import-${importedCaseIds.size}`,
          dataset_id: datasetId,
          tenant_id: "tenant-a",
          split: row.split || "regression",
          input: row.input || {},
          expected_output: row.expected_output || {},
          metadata: row.metadata || { case_id: row.case_id },
          source_trace_id: row.source_trace_id || null,
          source_span_id: row.source_span_id || null,
          created_by: "eval-user",
          created_at: nowIso(),
        });
      }
      await route.fulfill(
        jsonResponse(
          {
            imported,
            skipped,
            examples: createdExamples,
          },
          201
        )
      );
      return;
    }

    if (
      request.method() === "POST" &&
      url.pathname.endsWith(`/api/v1/eval/datasets/${datasetId}/examples:from-trace`)
    ) {
      const payload = request.postDataJSON();
      expect(payload.trace_family).toBe("assistant");
      await route.fulfill(
        jsonResponse(
          {
            example_id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
            dataset_id: datasetId,
            tenant_id: "tenant-a",
            split: payload.split || "regression",
            input: { input_preview: "hello" },
            expected_output: { output_preview: "safe assistant answer" },
            metadata: payload.metadata || {},
            source_trace_id: payload.source_trace_id,
            source_span_id: payload.source_span_id || null,
            created_by: "eval-user",
            created_at: nowIso(),
          },
          201
        )
      );
      return;
    }

    if (request.method() === "POST" && url.pathname.endsWith("/api/v1/eval/evaluators")) {
      const payload = request.postDataJSON();
      await route.fulfill(
        jsonResponse(
          {
            evaluator_id: evaluatorId,
            tenant_id: "tenant-a",
            name: payload.name,
            evaluator_type: payload.evaluator_type || "human",
            rubric: payload.rubric || "",
            version: payload.version || "v1",
            sampling_config: payload.sampling_config || {},
            filter_config: payload.filter_config || {},
            metadata: payload.metadata || {},
            created_by: "eval-user",
            created_at: nowIso(),
            updated_at: nowIso(),
          },
          201
        )
      );
      return;
    }

    if (request.method() === "POST" && url.pathname.endsWith("/api/v1/eval/experiments")) {
      const payload = request.postDataJSON();
      await route.fulfill(
        jsonResponse(
          {
            experiment_id: experimentId,
            tenant_id: "tenant-a",
            dataset_id: payload.dataset_id || null,
            name: payload.name,
            description: payload.description || "",
            target_config: payload.target_config || {},
            metadata: payload.metadata || {},
            created_by: "eval-user",
            created_at: nowIso(),
            updated_at: nowIso(),
            runs: [],
          },
          201
        )
      );
      return;
    }

    if (
      request.method() === "POST" &&
      url.pathname.endsWith(`/api/v1/eval/evaluators/${evaluatorId}:run-async`)
    ) {
      await route.fulfill(
        jsonResponse(
          {
            job_id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
            status: "queued",
            run_id: "ffffffff-ffff-4fff-8fff-ffffffffffff",
          },
          202
        )
      );
      return;
    }

    await route.fulfill(jsonResponse({ detail: "not found" }, 404));
  });

  return { observedTraceFamilies };
}

function watchRuntimeFailures(page: Page) {
  const consoleErrors: string[] = [];
  const pageErrors: string[] = [];
  const badResponses: string[] = [];

  page.on("console", (message) => {
    if (message.type() === "error" && !/favicon|NO_COLOR/i.test(message.text())) {
      consoleErrors.push(message.text());
    }
  });
  page.on("pageerror", (error) => {
    pageErrors.push(String(error));
  });
  page.on("response", (response) => {
    const url = response.url();
    if (response.status() >= 400 && /\/api\/v1\//.test(url)) {
      badResponses.push(`${response.status()} ${url}`);
    }
  });

  return () => {
    expect(pageErrors, `Page runtime errors:\n${pageErrors.join("\n")}`).toEqual([]);
    expect(consoleErrors, `Console errors:\n${consoleErrors.join("\n")}`).toEqual([]);
    expect([...new Set(badResponses)], `API responses >= 400:\n${badResponses.join("\n")}`).toEqual([]);
  };
}

async function expectNoHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(() => {
    const doc = document.documentElement;
    return doc.scrollWidth - doc.clientWidth;
  });
  expect(overflow).toBeLessThanOrEqual(2);
}

test.describe("Eval trace console", () => {
  test("blocks authenticated users without Eval permission", async ({ page }) => {
    const assertNoRuntimeFailures = watchRuntimeFailures(page);
    await installClientAuth(page, {
      user_id: "viewer-user",
      email: "viewer@example.com",
      display_name: "Viewer User",
      permissions: ["console:dashboard:view"],
      effective_permissions: ["console:dashboard:view"],
    });

    await page.goto("/eval", { waitUntil: "domcontentloaded" });
    await expect(page).toHaveURL(/\/403$/);
    await expect(page.getByRole("heading", { name: "403" })).toBeVisible();

    assertNoRuntimeFailures();
  });

  test("shows the supported RAGAS metric set", async ({ page }) => {
    await installEvalHarness(page);
    await page.goto("/eval", { waitUntil: "domcontentloaded" });
    await expect(page.getByRole("heading", { name: "Eval Console" })).toBeVisible();
    await page.getByRole("tab", { name: "Run & Results", exact: true }).click();
    await page.getByRole("tab", { name: "KB RAGAS", exact: true }).click();

    await expect(page.getByText("Faithfulness").first()).toBeVisible();
    await expect(page.getByText("Response relevancy").first()).toBeVisible();
    await expect(page.getByText("Context recall").first()).toBeVisible();
  });

  test("renders assistant traces, family tabs, focus path, and score submission", async ({
    page,
  }) => {
    const assertNoRuntimeFailures = watchRuntimeFailures(page);
    const harness = await installEvalHarness(page);
    page.setDefaultTimeout(15_000);
    await fs.mkdir(".playwright", { recursive: true });

    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto("/eval", { waitUntil: "domcontentloaded" });

    await expect(page.getByRole("heading", { name: "Eval Console" })).toBeVisible();
    await expect(page.getByText("Golden cases")).toBeVisible();
    await page.getByRole("tab", { name: "Traces" }).click();
    await expect(page.getByRole("cell", { name: /11111111/ })).toBeVisible();
    await expect(page.getByText("Active selection")).toBeVisible();
    await expect(page.getByRole("button", { name: "Open Run Detail" })).toBeVisible();
    await expectNoHorizontalOverflow(page);
    await page.screenshot({ path: ".playwright/eval-desktop.png", fullPage: true });

    await page.getByLabel("Transcript, request, or message text").fill("refund transcript anchor");
    await expect(page.getByRole("row", { name: /11111111/ })).toBeVisible();
    await expect(page.getByRole("row", { name: /33333333/ })).toHaveCount(0);
    await page.getByText("Advanced filters").click();
    await page.getByLabel("Turn").fill("4");
    await expect(page.getByRole("row", { name: /11111111/ })).toBeVisible();
    await page.getByRole("button", { name: "Export OpenInference" }).first().click();
    await expect(page.getByText("Trace export payload ready")).toBeVisible();

    const firstTraceRow = page.getByRole("row", { name: /11111111/ }).first();
    await firstTraceRow.focus();
    await expect(firstTraceRow).toBeFocused();
    await page.keyboard.press("Enter");

    await page.getByRole("button", { name: "Open Run Detail" }).click();
    await expect(page.getByText("Redacted trace preview")).toBeVisible();
    await expect(page.getByText("Transcript locator")).toBeVisible();
    await expect(page.getByLabel("hello refund transcript anchor", { exact: true })).toBeVisible();
    await expect(page.getByText("run_started")).toBeVisible();
    await expect(page.getByText("Grounded answer")).toBeVisible();
    await page.getByRole("button", { name: "Add score" }).click();
    await page.getByLabel("Explanation").fill("Useful trace review");
    await page.getByRole("button", { name: "Submit score" }).click();
    await expect(page.getByText("Trace score submitted")).toBeVisible();

    await page.getByText("Thread View").click();
    await expect(page.getByRole("heading", { name: "Thread View" })).toBeVisible();
    await expect(page.locator(".eval-thread-view").getByText("hello refund transcript anchor").first()).toBeVisible();
    await expectNoHorizontalOverflow(page);
    await page.screenshot({ path: ".playwright/eval-thread.png", fullPage: true });

    await page.getByRole("tab", { name: "Assets", exact: true }).click();
    await page.getByRole("tab", { name: "Golden Sets", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Behavior contract" })).toBeVisible();
    await expect(page.getByText("Expected tools", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Create dataset" }).click();
    await expect(page.getByText("Dataset created")).toBeVisible();
    await page.getByRole("button", { name: "Add trace to dataset" }).click();
    await expect(page.getByText("Trace evidence is observed behavior, not the expectation. Review every field before saving.")).toBeVisible();
    await page.getByRole("button", { name: "Add tool expectation" }).click();
    await page.getByLabel("Tool name").fill("lookup_policy");
    await page.getByLabel("Argument subset JSON").fill('{"account_id":"known"}');
    await page.getByLabel("Max calls").fill("1");
    await page.getByLabel("Expected status").fill("succeeded");
    await page.getByRole("checkbox", { name: "I reviewed and confirmed this expected behavior" }).check();
    await page.getByRole("button", { name: "Save behavior contract" }).click();
    await expect(page.getByText("Behavior contract saved")).toBeVisible();

    const goldenFixture = path.resolve(
      path.dirname(fileURLToPath(import.meta.url)),
      "../../tests/fixtures/eval/golden/assistant_regression_v1.jsonl"
    );
    await page.locator('[data-testid="golden-jsonl-import"] input[type="file"]').setInputFiles(goldenFixture);
    await expect(page.getByText("18 case(s) passed validation")).toBeVisible();
    await page.getByTestId("golden-jsonl-import-submit").click();
    await expect(
      page.getByTestId("golden-jsonl-import").getByText("Imported 18 case(s) across 1 batch(es). Skipped 0.")
    ).toBeVisible();
    await page.getByTestId("golden-jsonl-import-submit").click();
    await expect(
      page.getByTestId("golden-jsonl-import").getByText("Imported 0 case(s) across 1 batch(es). Skipped 18.")
    ).toBeVisible();

    await page.getByRole("tab", { name: "Run & Results", exact: true }).click();
    await expect(page.getByRole("button", { name: "Run current Agent" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Baseline comparison" })).toBeVisible();
    await page.getByText("Create or configure an experiment").click();
    await page.getByRole("button", { name: "Create experiment" }).click();
    await expect(page.getByText("Experiment created")).toBeVisible();

    await page.getByRole("tab", { name: "Assets", exact: true }).click();
    await page.getByRole("tab", { name: "Evaluators", exact: true }).click();
    await page.getByRole("button", { name: "Create evaluator" }).click();
    await expect(page.getByText("Evaluator created")).toBeVisible();
    await page.getByRole("button", { name: "Queue evaluator" }).click();
    await expect(page.getByText("Evaluator run queued").last()).toBeVisible();

    await page.getByRole("tab", { name: "Traces" }).click();
    await page.locator(".ant-segmented-item").filter({ hasText: "LangGraph Proxy" }).click();
    await expect(page.getByRole("heading", { name: "LangGraph Proxy traces" })).toBeVisible();
    await expect(page.getByRole("row", { name: /66666666/ })).toBeVisible();
    await page.getByRole("row", { name: /66666666/ }).click();
    await page.getByRole("button", { name: "Open Run Detail" }).click();
    await expect(
      page.getByText("proxy_request_finished", { exact: true })
    ).toBeVisible();
    expect(harness.observedTraceFamilies).toContain("langgraph_proxy");
    await page.locator(".ant-segmented-item").filter({ hasText: "RAG" }).click();
    await expect(page.getByRole("heading", { name: "RAG traces" })).toBeVisible();
    await expect(page.getByRole("row", { name: /55555555/ })).toBeVisible();
    await page.getByRole("row", { name: /55555555/ }).click();
    await page.getByRole("button", { name: "Open Run Detail" }).click();
    await expect(
      page.getByText("rag_retrieval_completed", { exact: true })
    ).toBeVisible();
    expect(harness.observedTraceFamilies).toContain("rag");

    await page.getByRole("tab", { name: "Run & Results", exact: true }).click();
    await page.getByRole("tab", { name: "KB RAGAS", exact: true }).click();
    await expect(page.getByTestId("kb-ragas-panel")).toBeVisible();
    await expect(page.getByRole("heading", { name: "Knowledge-base RAGAS" })).toBeVisible();
    await expect(page.getByTestId("kb-ragas-panel").getByText("refund policy")).toBeVisible();
    await page.getByRole("button", { name: "Create KB RAGAS evaluator" }).click();
    await expect(page.getByText("KB RAGAS evaluator created")).toBeVisible();
    await page.getByRole("button", { name: "Score selected trace" }).click();
    await expect(page.getByText("Evaluator run queued").last()).toBeVisible();
    await page.getByRole("button", { name: "Batch score dataset traces" }).click();
    await expect(page.getByText("Queued 1 trace(s), skipped 0")).toBeVisible();

    await page.evaluate(() => {
      localStorage.setItem(
        "agent-gateway-storage",
        JSON.stringify({
          state: { themeMode: "dark", resolvedTheme: "dark", darkMode: true },
          version: 3,
        })
      );
      localStorage.setItem("i18nextLng", "zh-CN");
    });
    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(page.getByRole("heading", { name: "评测控制台" })).toBeVisible();
    await page.getByRole("tab", { name: "运行与结果", exact: true }).click();
    await expect(page.getByRole("tab", { name: "知识库 RAGAS", exact: true })).toBeVisible();
    await expect(page.getByText("Trace 持久化为异步路径")).toBeVisible();
    await expectNoHorizontalOverflow(page);
    await page.screenshot({ path: ".playwright/eval-dark-zh.png", fullPage: true });

    await page.setViewportSize({ width: 390, height: 844 });
    const mobileTraceTab = page.getByRole("tab", { name: /Trace$/ });
    await mobileTraceTab.focus();
    await page.keyboard.press("Enter");
    await expect(mobileTraceTab).toHaveAttribute("aria-selected", "true");
    await expect(page.getByRole("tabpanel", { name: /Trace$/ })).toBeVisible();
    await page.locator(".ant-segmented-item").filter({ hasText: "Assistant" }).click();
    await expect(page.getByRole("heading", { name: "Assistant Trace" })).toBeVisible();
    await expectNoHorizontalOverflow(page);
    await page.screenshot({ path: ".playwright/eval-mobile.png", fullPage: true });
    await page.getByRole("button", { name: "打开 Run Detail" }).click();
    await expect(page.getByRole("heading", { name: "Trace 详情" })).toBeVisible();

    assertNoRuntimeFailures();
  });
});
