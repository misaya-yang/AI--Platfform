# Image Generation API — Reference

Full reference for the AI Gateway image generation endpoints. For the Java
backend integration recipe, see
[image-generation-java-integration.md](./image-generation-java-integration.md).

All endpoints are mounted under `/api/v1/assistant/`.

---

## 1. Overview

### Recommended architecture

```
+--------+      +-----------------+      +-------------+      +----------+
|  APP   | <--> | Java backend    | <--> | AI Gateway  | ---> | Provider |
| (web/  |      | (your service)  |      | (this API)  |      | (Gemini) |
|  ios)  |      +-----------------+      +-------------+
|        |                                     |
|        |                                     v
|        |                              +-------------+
|        |                              | S3 / MinIO  |
|        |                              +-------------+
|        |                                     ^
|        | <-- presigned GET (image bytes) ----+
+--------+        (direct, no proxy)
```

The Java backend is the system-of-record for app-side state (user, session,
business workflow). It calls the AI Gateway to submit and poll generations,
and stores `artifact_id` / `task_id` references. **Image bytes are never
proxied through Java**; the APP fetches them directly from S3/CDN via
presigned URLs returned by the gateway.

### DO

- Store the `artifact_id` returned by the gateway. It is the long-term
  reference for an image.
- Re-sign URLs by calling
  `GET /api/v1/assistant/artifacts/{artifact_id}/download-url` whenever the
  cached presigned URL is stale (URLs expire after `expires_in`, default 1h).
- Pass `session_id` plus the user's new `prompt` for next-turn edits. The
  gateway will resolve the previous turn's raw artifact automatically.
- Use `client_request_id` (a caller-controlled UUID) on every submit so
  retries replay safely.

### DON'T

- Don't store base64 image data anywhere. Responses do not include base64,
  and the gateway will not stream image bytes back through itself.
- Don't store a presigned URL as the long-term reference for an image. URLs
  expire. Store the `artifact_id` and re-sign on demand.
- Don't proxy image bytes through Java. The APP must fetch from S3/CDN
  directly.
- Don't pass `reference_image` (raw base64) for new integrations. It still
  works for backward compatibility but is discouraged.

---

## 2. Identity and `owner_scope`

Every request carries identity via headers. Two layers exist:

| Header              | Required | Purpose                                                  |
| ------------------- | -------- | -------------------------------------------------------- |
| `X-User-Id`         | yes      | JWT subject of the calling backend (your Java service).  |
| `X-Tenant-Id`       | yes      | Tenant of the calling backend.                           |
| `X-App-User-Id`     | optional | End-user identity inside the calling app (new).          |
| `X-App-Tenant-Id`   | optional | End-user tenant inside the calling app (new).            |

The new `app_user_id` / `app_tenant_id` may also be passed as **request body
fields** (`app_user_id`, `app_tenant_id`); the body fields and headers are
equivalent. Body fields take precedence if both are present.

### `owner_scope` formula

```
if app_user_id is present:
    owner_scope = (X-User-Id, app_tenant_id, app_user_id)
else:
    owner_scope = (X-User-Id,)               # legacy fallback
```

Sessions, artifacts, and tasks are scoped by `owner_scope`. A request whose
`owner_scope` does not match the resource's stored `owner_scope` receives
`403 forbidden` (or `404 not_found` for opaque resources).

### Legacy fallback

Callers that do not send `app_user_id` keep working unchanged: their
resources are scoped by the JWT subject only, exactly as before.

---

## 3. Endpoints

### 3.1 `POST /api/v1/assistant/generate-image`

Synchronous generation. Returns when the provider call completes (typical
latency 5-20s). Use the async variant for production.

#### Request body

| Field                          | Type     | Required | Default  | Description |
| ------------------------------ | -------- | -------- | -------- | ----------- |
| `prompt`                       | string   | yes      | —        | Generation or edit prompt. |
| `model_id`                     | string   | no       | provider default | Provider model identifier. |
| `n`                            | int      | no       | 1        | Number of images (1-4). |
| `size`                         | string   | no       | `1024x1024` | `WxH` pixel size. |
| `style`                        | string   | no       | `default`| Style preset. First-turn value is locked into the session (see Style Lock). |
| `session_id`                   | string   | no       | none     | Existing session ID for multi-turn. Omit to start a new session. |
| `reference_artifact_id`        | string   | no       | none     | **Legacy.** Use `parent_artifact_id` instead. |
| `reference_image`              | string   | no       | none     | **Legacy.** Base64 PNG/JPEG. Use S3 upload + `parent_artifact_id` instead. |
| `reference_image_url`          | string   | no       | none     | **Legacy.** HTTPS URL the gateway will fetch (SSRF-safe). |
| `add_watermark`                | bool     | no       | `false`  | If true, also produce a watermarked display variant. |
| `app_user_id`                  | string   | no       | from header | End-user identity inside the calling app. |
| `app_tenant_id`                | string   | no       | from header | End-user tenant inside the calling app. |
| `parent_artifact_id`           | string   | no       | none     | Branch from a specific prior artifact (raw). Overrides `session.latest_artifact_id`. |
| `expected_parent_artifact_id`  | string   | no       | none     | CAS guard. If set, must equal `session.latest_artifact_id` or the request fails with `latest_artifact_conflict`. |
| `client_request_id`            | string   | no       | server-generated | Caller-controlled idempotency key (UUID). |
| `return_variants`              | bool     | no       | `false`  | If true, response includes a `variants` map per image. |
| `allow_branch`                 | bool     | no       | `false`  | If true, generation succeeds even when `parent_artifact_id` is not the current `latest_artifact_id` (creates a branch). |

#### Response body

| Field                  | Type     | Description |
| ---------------------- | -------- | ----------- |
| `success`              | bool     | True on completion. |
| `images`               | array    | One element per generated image. |
| `images[].url`         | string   | Presigned S3/CDN URL (display variant if watermarked, raw otherwise). |
| `images[].width`       | int      | Pixel width. |
| `images[].height`      | int      | Pixel height. |
| `images[].artifact_id` | string   | Long-term artifact reference. Store this. |
| `provider`             | string   | Backing provider (e.g. `gemini`). |
| `duration_ms`          | int      | Wall time. |
| `session_id`           | string   | Session this image belongs to. |
| `error`                | string\|null | Human-readable error if `success=false`. |
| `turn_id`              | string   | Identifier for this turn within the session. |
| `parent_artifact_id`   | string\|null | Raw artifact this turn was edited from, if any. |
| `output_artifact_id`   | string   | Raw artifact this turn produced. Equals `images[0].artifact_id` when `add_watermark=false`; equals the parent of the display variant when `add_watermark=true`. |
| `client_request_id`    | string   | Echo of the supplied or server-generated idempotency key. |
| `error_code`           | string\|null | Stable machine-readable code (see §9). |
| `idempotent_replay`    | bool     | True if this response is a replay of an earlier request with the same `client_request_id`. |
| `variants`             | object   | Present when `return_variants=true`. Maps `display`/`raw`/`thumbnail` → `{artifact_id, url, width, height}`. |

#### Error codes

`invalid_request`, `unauthorized`, `forbidden`, `not_found`,
`idempotency_conflict`, `latest_artifact_conflict`, `validation_error`,
`internal_error`.

---

### 3.2 `POST /api/v1/assistant/generate-image-async`

Submit a generation and return immediately. Recommended for production. All
request fields are identical to §3.1.

#### Response body

| Field                  | Type     | Description |
| ---------------------- | -------- | ----------- |
| `task_id`              | string   | Poll handle. Pass to `GET /image-task/{task_id}`. |
| `status`               | string   | Always `pending` on submit. |
| `message`              | string   | Human-readable status note. |
| `client_request_id`    | string   | Echo of the supplied or server-generated key. |
| `idempotent_replay`    | bool     | True if this submit replayed a prior task. |
| `error_code`           | string\|null | Set if submit itself was rejected (e.g. `idempotency_conflict`, `latest_artifact_conflict`). |

If `idempotent_replay=true`, the returned `task_id` may already be
`completed` — the caller should poll once to drain the result.

---

### 3.3 `GET /api/v1/assistant/image-task/{task_id}`

Poll a previously submitted async task.

#### Query / path

| Param      | In   | Required | Description |
| ---------- | ---- | -------- | ----------- |
| `task_id`  | path | yes      | From submit response. |

#### Response body

| Field                  | Type     | Description |
| ---------------------- | -------- | ----------- |
| `task_id`              | string   | Echo. |
| `status`               | string   | `pending`, `running`, `completed`, `failed`. |
| `progress`             | float    | 0.0-1.0 best-effort. |
| `prompt`               | string   | Original prompt. |
| `model_id`             | string   | Original model. |
| `provider`             | string   | Backing provider. |
| `images`               | array    | Same shape as §3.1. Empty until `status=completed`. |
| `duration_ms`          | int\|null| Set on completion. |
| `error`                | string\|null | Set on failure. |
| `created_at`           | string   | ISO-8601. |
| `completed_at`         | string\|null | ISO-8601. |
| `session_id`           | string   | Session of this turn. |
| `turn_id`              | string   | Turn ID. |
| `parent_artifact_id`   | string\|null | Raw artifact this turn edited from. |
| `output_artifact_id`   | string\|null | Raw artifact produced (set when `completed`). |
| `client_request_id`    | string   | Echo. |
| `error_code`           | string\|null | Stable code on failure. |
| `idempotent_replay`    | bool     | True if the task was a replay. |
| `variants`             | object   | Same shape as §3.1 when populated. |

#### Error codes

`unauthorized`, `forbidden`, `not_found`, `internal_error`.

---

### 3.4 `GET /api/v1/assistant/artifacts/{artifact_id}/download-url`

Re-sign or pick a variant for an artifact. Use this when a cached presigned
URL has expired, or when you want a `thumbnail` or `raw` variant.

#### Query / path

| Param         | In    | Required | Default   | Description |
| ------------- | ----- | -------- | --------- | ----------- |
| `artifact_id` | path  | yes      | —         | Artifact reference returned by a generation. |
| `variant`     | query | no       | `display` | One of `display`, `raw`, `thumbnail`. |
| `expires_in`  | query | no       | `3600`    | URL TTL in seconds (60-86400). |

#### Response body

| Field         | Type   | Description |
| ------------- | ------ | ----------- |
| `artifact_id` | string | Echo of path arg. |
| `variant`     | string | Variant actually returned (see fallback rules in §10). |
| `url`         | string | Presigned URL. |
| `expires_at`  | string | ISO-8601 expiry. |
| `width`       | int    | Pixel width of the chosen variant. |
| `height`      | int    | Pixel height of the chosen variant. |
| `mime_type`   | string | e.g. `image/png`. |

#### Error codes

`unauthorized`, `forbidden`, `not_found`, `validation_error`,
`internal_error`.

---

### 3.5 `GET /api/v1/assistant/image-sessions/{session_id}`

List the turn history of a session, paginated. Use this to render an "edit
history" panel in the APP, or to recover state after a callback miss.

#### Query / path

| Param         | In    | Required | Default | Description |
| ------------- | ----- | -------- | ------- | ----------- |
| `session_id`  | path  | yes      | —       | Session ID. |
| `limit`       | query | no       | `50`    | Page size (1-200). |
| `cursor`      | query | no       | none    | Opaque cursor from previous response. |
| `include_urls`| query | no       | `false` | If true, every turn's images include a fresh presigned URL. Costs one S3 sign per image. |

#### Response body

| Field                 | Type     | Description |
| --------------------- | -------- | ----------- |
| `session_id`          | string   | Echo. |
| `latest_artifact_id`  | string   | Current head (raw). The reference next-turn edits will use. |
| `locked_style`        | string\|null | Style locked into the session, or null if cleared. |
| `created_at`          | string   | ISO-8601. |
| `updated_at`          | string   | ISO-8601. |
| `turns`               | array    | Ordered oldest → newest within the page. |
| `turns[].turn_id`     | string   | |
| `turns[].task_id`     | string   | |
| `turns[].prompt`      | string   | |
| `turns[].parent_artifact_id` | string\|null | |
| `turns[].output_artifact_id` | string | Raw artifact for this turn. |
| `turns[].status`      | string   | Same enum as §3.3. |
| `turns[].created_at`  | string   | |
| `turns[].images`      | array    | Same shape as §3.1; `url` populated only when `include_urls=true`. |
| `next_cursor`         | string\|null | Pass back as `cursor` to fetch next page. Null when exhausted. |

#### Error codes

`unauthorized`, `forbidden`, `not_found`, `validation_error`,
`internal_error`.

---

## 4. Reference precedence

When a generation request needs to resolve "what is the input image?", the
gateway checks sources in this exact order. The first non-empty source wins;
later sources are ignored.

```
def resolve_reference(req, session):
    if req.parent_artifact_id:
        return load_raw(req.parent_artifact_id)              # 1. explicit branch
    if req.session_id and session.latest_artifact_id:
        return load_raw(session.latest_artifact_id)          # 2. multi-turn head
    if req.reference_artifact_id:
        return load_raw(req.reference_artifact_id)           # 3. legacy artifact
    if req.reference_image_url:
        return ssrf_safe_fetch(req.reference_image_url)      # 4. legacy URL
    if req.reference_image:
        return decode_base64(req.reference_image)            # 5. legacy base64
    return None                                              # 6. text-to-image
```

Notes:

- All artifact-based sources resolve to the **raw** variant. The watermarked
  display variant is never used as input to the next turn.
- `parent_artifact_id` always wins. Use it to branch from a prior turn even
  when a session has moved on.
- Sources 3-5 are kept for backward compatibility. New integrations should
  use `parent_artifact_id` or `session_id`.

---

## 5. Watermark: raw vs display rule

```
            +--------------------+
prompt ---> | Provider           |
            | (Gemini etc.)      |
            +---------+----------+
                      |
                      v
                +-----------+    add_watermark=false
                | RAW       |---------------------------------> images[0]
                | artifact  |                                    (url = raw)
                +-----------+
                      |
                      | add_watermark=true
                      v
                +-----------+
                | DISPLAY   |  parent_artifact_id = RAW.id
                | artifact  |
                | (stamped) |
                +-----------+
                      |
                      v
                  images[0]
                  (url = display)
```

1. Every successful generation produces a **raw** artifact with no
   watermark. This is always stored.
2. When `add_watermark=true`, the gateway produces a second **display**
   artifact (watermarked), whose `parent_artifact_id` points at the raw.
3. The image returned in the response (`images[0].url`) is the display
   variant when watermarked, the raw otherwise.
4. `output_artifact_id` always points at the **raw** artifact, regardless of
   watermark setting.
5. The next turn's edit always operates on the raw, never on the
   watermarked copy. This keeps watermark stamps from compounding.

---

## 6. Style lock

The session has a `locked_style` field. Three rules govern it:

1. **First-turn lock.** The first turn's `style` value is written into
   `session.locked_style`.
2. **Inherit by omission.** A later turn that omits the `style` field
   reuses `session.locked_style` for the generation. The lock is unchanged.
3. **Explicit override.** A later turn that sends a non-default `style`
   value uses that value for the turn and updates the lock.
4. **Explicit reset.** A later turn that explicitly sends
   `style="default"` clears the lock (`locked_style → null`); the
   generation uses no style preset.

The `style` value used for each turn is recorded on the turn itself; the
lock only governs the default for future turns.

---

## 7. Idempotency (`client_request_id`)

The caller supplies a UUID (or any unique string) per logical submit. The
gateway uses `(owner_scope, client_request_id)` as a dedupe key.

| Scenario                                                | Behavior |
| ------------------------------------------------------- | -------- |
| Same `client_request_id`, same payload                  | Returns the original task / response. `idempotent_replay=true`. |
| Same `client_request_id`, **different** payload         | `409` with `error_code=idempotency_conflict`. |
| New `client_request_id`                                 | New task. `idempotent_replay=false`. |
| Omitted                                                 | Server generates one. No dedupe across retries — caller should always supply its own. |

### Examples

Replay (network retry):

```
POST /generate-image-async
{ "prompt": "a cat", "client_request_id": "aaaaaaaa-..." }
→ { "task_id": "tsk_A", "idempotent_replay": false, ... }

# network blip; client retries
POST /generate-image-async
{ "prompt": "a cat", "client_request_id": "aaaaaaaa-..." }
→ { "task_id": "tsk_A", "idempotent_replay": true, ... }    # same task
```

Conflict (caller bug — reused key on a new prompt):

```
POST /generate-image-async
{ "prompt": "a cat",  "client_request_id": "aaaaaaaa-..." }
→ 200 OK, task_id=tsk_A

POST /generate-image-async
{ "prompt": "a dog",  "client_request_id": "aaaaaaaa-..." }
→ 409 { "error_code": "idempotency_conflict", ... }
```

The fix on conflict: generate a fresh `client_request_id`.

---

## 8. Concurrency (`expected_parent_artifact_id`)

For multi-turn editing, two clients editing the same session can race. To
prevent silent overwrite, pass `expected_parent_artifact_id`. The gateway
performs a CAS check:

```
if request.expected_parent_artifact_id != session.latest_artifact_id:
    return 409 latest_artifact_conflict
```

### Race-loss example

```
Time   Client A                          Client B                          Session.latest
----   --------------------------------- --------------------------------- ----------------
t0                                                                          art_root
t1     read latest = art_root
t2                                       read latest = art_root
t3     submit edit, expected=art_root
t4                                                                          art_A
t5                                       submit edit, expected=art_root
t6                                                                          409 latest_artifact_conflict
```

On a `latest_artifact_conflict`, Client B has two choices:

- **Branch.** Resubmit with `parent_artifact_id=art_root` (and either
  `allow_branch=true`, or omit `expected_parent_artifact_id`). The new turn
  becomes a fork of `art_root` rather than an edit of the current head. The
  branched turn is still in the same session; `latest_artifact_id` then
  points at the branch.
- **Refresh.** Re-read the session via `GET /image-sessions/{id}`, show
  Client A's new image to the user, and let them re-issue an edit on top of
  `art_A`.

`expected_parent_artifact_id` is opt-in. If omitted, last-write-wins.

---

## 9. Error code reference

All error responses include `error_code` (machine-readable) and `error`
(human-readable). HTTP status follows REST conventions.

| `error_code`                | HTTP | Meaning                                                                              |
| --------------------------- | ---- | ------------------------------------------------------------------------------------ |
| `invalid_request`           | 400  | Malformed body or missing required field.                                            |
| `validation_error`          | 422  | Field present but value invalid (e.g. unknown variant, negative `n`, bad size).      |
| `unauthorized`              | 401  | Missing or invalid auth headers.                                                     |
| `forbidden`                 | 403  | `owner_scope` mismatch on a known resource.                                          |
| `not_found`                 | 404  | Artifact / task / session not found, or hidden by owner scope.                       |
| `idempotency_conflict`      | 409  | Same `client_request_id` reused with a different payload.                            |
| `latest_artifact_conflict`  | 409  | `expected_parent_artifact_id` did not match `session.latest_artifact_id`.            |
| `internal_error`            | 500  | Unhandled server error. Safe to retry with the same `client_request_id`.             |

---

## 10. Variants and `download-url`

Every artifact has up to three variants:

| Variant     | Description                                                  | Always present? |
| ----------- | ------------------------------------------------------------ | --------------- |
| `raw`       | Provider output, no watermark. Authoritative source.         | yes             |
| `display`   | Watermarked copy of `raw`, suitable for app display.         | only when `add_watermark=true` was used |
| `thumbnail` | Small (~256px long edge) preview, derived from `display` if present, else `raw`. | best-effort, generated lazily |

### Fallback rules

`GET /artifacts/{id}/download-url?variant=...` resolves as follows:

```
variant=thumbnail → thumbnail || display || raw
variant=display   → display || raw
variant=raw       → raw || 404 not_found
```

The response's `variant` field reflects what was actually returned, which
may differ from the request when fallback fires.

---

## 11. Backward compatibility

The following request fields and behaviors are guaranteed stable for
existing integrations. They keep working unchanged with no `app_user_id`,
no `client_request_id`, no `parent_artifact_id`.

- Request fields: `prompt`, `model_id`, `n`, `size`, `style`, `session_id`,
  `reference_artifact_id`, `reference_image`, `reference_image_url`,
  `add_watermark`.
- Response fields: `success`, `images[]: {url, width, height, artifact_id}`,
  `provider`, `duration_ms`, `session_id`, `error`.
- Async submit response: `{task_id, status: "pending", message}`.
- Poll response: `{task_id, status, progress, prompt, model_id, provider,
  images[], duration_ms, error, created_at, completed_at}`.
- Auth via `X-User-Id`, `X-Tenant-Id`.
- Reference precedence sources 3-5 (`reference_artifact_id`,
  `reference_image_url`, `reference_image`) keep working.

New fields are all optional and additive. Clients that ignore them see no
behavior change.

---

## 12. Callback payload reference

If the integration uses callbacks (configured out-of-band), the gateway
POSTs the following payload to the configured URL when an async task
finishes. **Existing fields** are preserved; **new fields** are additive.

```json
{
  "task_id": "tsk_...",
  "status": "completed",
  "session_id": "ses_...",
  "prompt": "a cat",
  "model_id": "gemini-2.5-flash-image",
  "provider": "gemini",
  "images": [
    {
      "url": "https://s3.../...png?X-Amz-...",
      "width": 1024,
      "height": 1024,
      "artifact_id": "art_..."
    }
  ],
  "duration_ms": 7421,
  "error": null,
  "created_at": "2026-04-28T01:23:45Z",
  "completed_at": "2026-04-28T01:23:52Z",

  "turn_id": "trn_...",
  "parent_artifact_id": "art_prev_...",
  "output_artifact_id": "art_...",
  "client_request_id": "aaaaaaaa-...",
  "error_code": null,
  "idempotent_replay": false,
  "variants": {
    "raw":       { "artifact_id": "art_raw_...",  "url": "...", "width": 1024, "height": 1024 },
    "display":   { "artifact_id": "art_disp_...", "url": "...", "width": 1024, "height": 1024 },
    "thumbnail": { "artifact_id": "art_thmb_...", "url": "...", "width": 256,  "height": 256  }
  }
}
```

URLs in the callback payload are short-lived. Treat the payload as
ephemeral and store only `artifact_id` / `task_id` for long-term use; re-sign
via §3.4 when needed.
