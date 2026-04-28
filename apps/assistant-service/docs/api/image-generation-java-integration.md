# Image Generation — Java Backend Integration Recipe

How to wire your Java backend to the AI Gateway image generation API. For
the full endpoint reference (every field, every error code), see
[image-generation.md](./image-generation.md).

This document is code-first. All snippets are Java-flavored pseudo-code; no
Spring or HTTP-client boilerplate.

---

## 1. What Java stores

The Java backend is the system-of-record for app-side state. The gateway
owns the artifacts. Keep your schema small.

Minimum columns per generation row:

| Column         | Type      | Note                                                           |
| -------------- | --------- | -------------------------------------------------------------- |
| `session_id`   | string    | From the gateway. Re-used for next-turn edits.                 |
| `task_id`      | string    | From the async submit response. Use to poll.                   |
| `artifact_id`  | string    | The raw artifact ID. The long-term reference. Store this.      |
| `status`       | enum      | `pending` / `running` / `completed` / `failed`.                |
| `prompt`       | text      | What the user typed.                                           |
| `created_at`   | timestamp | For sorting and TTL.                                           |

Optional but useful: `client_request_id` (for retry-safety),
`parent_artifact_id` (to render an edit history tree), `error_code` (for
analytics).

### Do NOT store

- **Base64 image data.** The gateway never returns it; don't encode it
  yourself.
- **Presigned URLs as the long-term reference.** They expire after 1h. Use
  `artifact_id` and re-sign on demand (see §5).
- **Image bytes in your DB / object store.** The gateway already persists
  to S3/MinIO. Storing again wastes space and creates consistency bugs.

---

## 2. Submit a generation

```java
String clientRequestId = UUID.randomUUID().toString();

SubmitReq req = new SubmitReq();
req.prompt = userPrompt;
req.session_id = sessionId;          // optional, for multi-turn
req.client_request_id = clientRequestId;
req.app_user_id = currentAppUser.id;
req.app_tenant_id = currentAppUser.tenantId;
req.add_watermark = true;            // produce display variant for APP

SubmitResp resp = aiClient.post("/api/v1/assistant/generate-image-async", req);

db.save(new ImageTask(
    resp.task_id,
    sessionId,
    "pending",
    userPrompt,
    clientRequestId,
    now()
));

return resp.task_id;
```

Notes:

- Always supply your own `client_request_id`. If your HTTP retry middleware
  retries the submit, the gateway dedupes by this key and you get the same
  `task_id` back with `idempotent_replay=true`.
- Pass `app_user_id` / `app_tenant_id` (or set the
  `X-App-User-Id` / `X-App-Tenant-Id` headers on the HTTP client). Without
  them, scoping falls back to your service's JWT subject only.
- `session_id` is optional for the first turn; include it for subsequent
  turns.

---

## 3. Poll a task

Polling is the primary mechanism. Treat callbacks (if you've enabled them)
as an optimization — always have a poller to catch dropped callbacks.

```java
PollResp poll(String taskId) {
    PollResp resp = aiClient.get("/api/v1/assistant/image-task/" + taskId);

    switch (resp.status) {
        case "completed":
            db.update(taskId, t -> {
                t.status = "completed";
                t.artifact_id = resp.output_artifact_id;
                t.parent_artifact_id = resp.parent_artifact_id;
                t.completed_at = resp.completed_at;
            });
            break;

        case "failed":
            db.update(taskId, t -> {
                t.status = "failed";
                t.error_code = resp.error_code;
                t.error = resp.error;
            });
            break;

        case "pending":
        case "running":
            // schedule next poll, capped backoff (e.g. 1s, 2s, 4s, 8s, 8s, ...)
            break;
    }
    return resp;
}
```

Retry-on-callback fallback: if you accept callbacks but a callback never
arrives within `T` (e.g. 60s), poll once with `taskId`. The gateway response
is authoritative.

When `idempotent_replay=true` is returned on submit, poll exactly once
immediately — the task may already be `completed`.

---

## 4. Edit a previous image

Two flavors. Pick based on whether you want optimistic concurrency.

### 4a. Edit the latest turn (fire-and-forget)

```java
SubmitReq req = new SubmitReq();
req.prompt = "make the sky orange";
req.session_id = sessionId;          // resolves session.latest_artifact_id (raw)
req.client_request_id = UUID.randomUUID().toString();
req.app_user_id = currentAppUser.id;
req.app_tenant_id = currentAppUser.tenantId;
req.add_watermark = true;
SubmitResp resp = aiClient.post("/api/v1/assistant/generate-image-async", req);
```

The gateway uses `session.latest_artifact_id` (raw) as the input. Last write
wins.

### 4b. Edit a specific turn with CAS guard

```java
// User clicked "edit" on a specific image in the history panel.
String parentArtifactId   = chosenTurn.output_artifact_id;
String expectedHead       = currentSessionState.latest_artifact_id;

SubmitReq req = new SubmitReq();
req.prompt = "make the sky orange";
req.session_id = sessionId;
req.parent_artifact_id = parentArtifactId;
req.expected_parent_artifact_id = expectedHead;     // CAS guard
req.client_request_id = UUID.randomUUID().toString();
req.app_user_id = currentAppUser.id;
req.app_tenant_id = currentAppUser.tenantId;
req.add_watermark = true;

try {
    SubmitResp resp = aiClient.post("/api/v1/assistant/generate-image-async", req);
} catch (GatewayConflict e) {
    if ("latest_artifact_conflict".equals(e.error_code)) {
        // Session moved on. See §8 for resolution strategy.
    }
}
```

`parent_artifact_id` lets you branch from any prior turn even when the
session head has moved on. Pair it with
`expected_parent_artifact_id` only when you want to **prevent** a stale
edit; omit it (or set `allow_branch=true`) when you want to deliberately
fork.

---

## 5. Re-sign a stale URL

Presigned URLs returned by submit / poll / list endpoints expire after
their TTL (default 1h). When the APP needs an image whose URL has expired,
the Java backend re-signs:

```java
ResignResp resign(String artifactId, String variant) {
    return aiClient.get(
        "/api/v1/assistant/artifacts/" + artifactId + "/download-url"
        + "?variant=" + variant            // "display" | "raw" | "thumbnail"
        + "&expires_in=3600"
    );
}

// usage
ResignResp r = resign(task.artifact_id, "display");
return r.url;     // hand to APP; valid until r.expires_at
```

Notes:

- Default `variant` is `display`. Falls back to `raw` if no display variant
  exists (i.e. `add_watermark=false` on the original generation).
- `thumbnail` is best-effort; falls back through `display` → `raw`.
- The response includes `width`, `height`, `mime_type` — useful for sizing
  the APP's image element ahead of the byte fetch.

---

## 6. List session history

```java
SessionResp page(String sessionId, String cursor) {
    String url = "/api/v1/assistant/image-sessions/" + sessionId
               + "?limit=50"
               + (cursor != null ? "&cursor=" + cursor : "")
               + "&include_urls=false";    // sign only what the APP actually displays
    return aiClient.get(url);
}

List<Turn> allTurns = new ArrayList<>();
String cursor = null;
do {
    SessionResp s = page(sessionId, cursor);
    allTurns.addAll(s.turns);
    cursor = s.next_cursor;
} while (cursor != null);
```

When rendering, request `include_urls=true` only for the page actually
shown to the user. Each `include_urls=true` page costs N S3 sign operations
(one per image), so don't sign things you won't display.

For the latest image, you can short-circuit: the response includes
`latest_artifact_id` directly — re-sign that one.

---

## 7. APP-side hint

The APP fetches image bytes **directly** from the presigned URL.

```
APP:   <img src="${urlFromJava}"/>
       fetch(urlFromJava)        // CORS to S3/CDN, not to Java
```

Rules for the APP:

1. The URL is short-lived (1h default). **Don't** cache it long-term in
   localStorage / IndexedDB. Cache the `artifact_id` instead.
2. On 403 / SignatureDoesNotMatch from S3 → ask Java for a fresh URL via
   §5. Don't retry the same URL.
3. Treat the URL as opaque — never parse query string, never strip
   parameters.

---

## 8. Error handling

Map every `error_code` to an action. Codes are stable; do not pattern-match
on `error` text.

| `error_code`                | Java action                                                                                          |
| --------------------------- | ---------------------------------------------------------------------------------------------------- |
| `invalid_request`           | Bug in your request builder. Log payload, surface a generic error to the user. Do not retry.        |
| `validation_error`          | Field-level. Surface to the user (e.g. "image too large"). Do not retry.                            |
| `unauthorized`              | Auth header rotation needed. Refresh credentials, retry once.                                        |
| `forbidden`                 | `owner_scope` mismatch — the user is trying to touch someone else's resource. Surface as 403 to APP. |
| `not_found`                 | Stale `task_id` / `artifact_id` / `session_id`. Treat as user-visible 404. Don't retry.              |
| `idempotency_conflict`      | You reused `client_request_id` with a different payload. **Generate a fresh `client_request_id` and resubmit.** |
| `latest_artifact_conflict`  | The session head moved while the user was composing. See below.                                      |
| `internal_error`            | Transient. Retry with the **same** `client_request_id` (gateway will dedupe).                       |

### Resolving `latest_artifact_conflict`

You sent `expected_parent_artifact_id=X`, but `session.latest_artifact_id`
is now `Y`. Two options:

1. **Branch** — the user really wants to edit `X`. Resubmit with
   `parent_artifact_id=X`, `allow_branch=true`, and **omit**
   `expected_parent_artifact_id`. The new turn becomes a fork of `X`.
2. **Refresh** — re-fetch the session via §6, show the user the new head
   `Y`, and let them re-issue their edit on top of `Y`.

### Resolving `idempotency_conflict`

You sent the same `client_request_id` with a different `prompt` /
`session_id` / etc. This is almost always a caller bug (e.g. you reused a
key across two distinct user actions). Generate a fresh UUID and resubmit.
Never keep retrying with the conflicting key — every retry will 409.

---

## 9. Migration tips

If your existing client uses any of these legacy patterns, no changes are
required — they keep working as documented in the
[reference §11 Backward compatibility](./image-generation.md#11-backward-compatibility).

| Legacy pattern                              | Still works? | Recommendation                                                   |
| ------------------------------------------- | ------------ | ---------------------------------------------------------------- |
| `reference_image` (base64)                  | yes          | Migrate to `parent_artifact_id` once you have artifact IDs.      |
| `reference_image_url` (HTTPS fetch)         | yes          | Same — migrate to `parent_artifact_id`.                          |
| `reference_artifact_id`                     | yes          | Equivalent to `parent_artifact_id`. New code should use the new field. |
| Auth with only `X-User-Id` / `X-Tenant-Id`  | yes          | Add `X-App-User-Id` / `X-App-Tenant-Id` to get end-user scoping. |
| No `client_request_id`                      | yes          | Add one. It's the cheapest reliability win available.            |
| Storing the presigned URL in your DB        | works until expiry | Replace with `artifact_id` + on-demand re-sign (§5).         |

### Suggested migration order

1. Add `client_request_id` to every submit. Fire-and-forget upgrade; no
   contract change visible to the user. Immediate retry-safety win.
2. Start sending `app_user_id` / `app_tenant_id`. Resources keep working;
   future resources get tighter scoping.
3. Switch from base64 / URL references to `parent_artifact_id` once you've
   stored your first wave of `artifact_id` values.
4. Adopt `expected_parent_artifact_id` only when you actually need
   optimistic concurrency in the UI.

---

## See also

- [image-generation.md](./image-generation.md) — full endpoint reference.
