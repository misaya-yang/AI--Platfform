# OpenAI Responses ingress

The public compatibility endpoint is `POST /v1/responses`. It authenticates at
the Gateway, then proxies to the Assistant service and projects the existing
`AssistantService.chat_stream` event stream. It does not implement a second
agent or tool loop.

## Supported contract

| Area | Supported |
| --- | --- |
| Authentication | Existing tenant-scoped Gateway JWT or configured platform API-key header |
| Model | Exact, configured model ID that the authenticated tenant/user may access |
| Input | Non-empty string; or ordered `user`/`assistant` message items with text content |
| Stateless function-result continuation | A unique `function_call` followed by exactly one matching `function_call_output`, replayed in the same `input` array |
| Instructions | String up to 500 characters; kept below the platform system prompt |
| Sampling | `temperature` from 0 through 2 |
| Output budget | Positive `max_output_tokens` no greater than the selected model limit |
| Storage | `store` omitted or `false`; conversation messages are not persisted and the response is not addressable later |
| Streaming | Responses SSE events with strictly increasing `sequence_number`, canonical heartbeat comments during producer silence, and one authoritative `response.completed` or `response.failed` terminal |
| Non-streaming | A Responses `response` object, including explicit `status: failed` on a runtime failure |
| Idempotency | Non-stream requests replay an identical body for the same `Idempotency-Key`, route, tenant and user; conflicting bodies return 409 |

`store: false` is not a zero-data-retention claim. Tenant-scoped operational
run/trace receipts can still be retained by platform policy. Each request uses
a fresh session identifier, disables persistent conversation messages, disables
memory retrieval, and removes process-local session state when the stream ends.

## Deliberately unsupported

Unsupported input fails closed with an OpenAI-shaped 4xx error:

- `previous_response_id` (there is no safe response-ID persistence contract yet)
- `store: true`
- non-empty `tools`, including custom function tools and built-in tools such as
  `web_search`, `file_search`, or code interpreter
- generating a first-turn client callback function call
- image, audio, or file input parts
- `system` or `developer` roles inside `input` (use `instructions`)
- background mode, response format controls, reasoning controls, and every
  other unlisted request field
- query parameters and client-supplied Agent runtime fields or headers

The platform AgentLoop executes its registered tools server-side, whereas the
OpenAI custom-function contract returns a call for the client to execute. Those
semantics are intentionally not conflated. A caller may replay a completed
external function call and its output in one stateless request, but this
endpoint does not originate that callback workflow. Use the native Assistant
run/approval APIs for platform tools and human approval.

Streaming `Idempotency-Key` values are forwarded for transport retry safety but
SSE bodies are not cached or replayed after a connection has begun.

## Streaming terminal and recovery semantics

Canonical `run_finished` and `run_error` events are authoritative source
boundaries. The adapter projects the corresponding Responses terminal
immediately, then closes the Assistant source; it does not wait for source
exhaustion or consume producer events after that boundary.

During producer silence, the stream emits the shared canonical
`: heartbeat` SSE comment at the configured interval (15 seconds by default).
Heartbeat comments are transport keepalives, not Responses events, and do not
consume a `sequence_number`.

If a run fails after text deltas were emitted, the terminal response preserves
the received partial text. Its started output message is closed with
`status: incomplete` before `response.failed`; a failed response never contains
an `in_progress` output child.

## Examples

Non-streaming text:

```bash
curl http://127.0.0.1:8081/v1/responses \
  -H "Authorization: Bearer ${AI_GATEWAY_TOKEN}" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: responses-example-1" \
  -d '{
    "model": "qwen3.7-plus",
    "instructions": "Answer with the decision and the calculation.",
    "input": "Baseline Recall@10 is .742 and candidate is .721. May we release?",
    "store": false
  }'
```

Streaming text:

```bash
curl -N http://127.0.0.1:8081/v1/responses \
  -H "Authorization: Bearer ${AI_GATEWAY_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3.7-plus",
    "input": "Explain the release decision in three steps.",
    "stream": true
  }'
```

Stateless continuation after an externally completed function call:

```json
{
  "model": "qwen3.7-plus",
  "input": [
    {"role": "user", "content": "Look up order 42"},
    {
      "type": "function_call",
      "call_id": "call_42",
      "name": "lookup_order",
      "arguments": "{\"order_id\":42}"
    },
    {
      "type": "function_call_output",
      "call_id": "call_42",
      "output": "{\"status\":\"shipped\"}"
    }
  ]
}
```

## Live verification

The opt-in live contract suite exercises both non-streaming and streaming
Responses through the running Gateway and the configured real provider. Load a
dedicated test user's credentials from the operator environment, then run:

```bash
RUN_RESPONSES_API_E2E=1 \
uv run --all-packages --extra test pytest -q --no-cov \
  tests/integration/test_responses_api_e2e_live.py
```

A release receipt must show both cases passing with no skips. A default skipped
run proves only that the opt-in guard works; it is not live-provider evidence.
