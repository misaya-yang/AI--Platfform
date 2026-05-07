# AI 生图接口文档 v8.0

> 面向 Java / 后端开发者的生图 API 速查手册。涵盖单轮、多轮编辑、异步队列、预签名下载、会话历史。

---

## 1. 快速开始

### 1.1 基础单轮生图

```bash
POST /api/v1/assistant/generate-image-async
Content-Type: application/json

{
  "prompt": "一只橘猫在樱花树下",
  "model_id": "gemini-3-flash-preview",
  "style": "anime",
  "add_watermark": true,
  "n": 1
}
```

返回：

```json
{
  "task_id": "task_xxx",
  "status": "pending",
  "progress": 0,
  "prompt": "一只橘猫在樱花树下",
  "model_id": "gemini-3-flash-preview",
  "created_at": "2026-05-07T03:28:10.333946+00:00"
}
```

### 1.2 轮询结果

```bash
GET /api/v1/assistant/image-task/{task_id}
```

复用同一组 Cookie（匿名用户身份绑定）。**不同 Cookie = 不同匿名用户 = 404**。

```json
{
  "task_id": "task_xxx",
  "status": "completed",
  "progress": 100,
  "images": [
    {
      "artifact_id": "art_xxx",
      "download_url": "https://s3.../watermarked_xxx.png?...",
      "url": "https://s3.../watermarked_xxx.png?...",
      "width": 1024,
      "height": 1024
    }
  ],
  "output_artifact_id": "art_yyy",
  "turn_id": "itn_xxx",
  "latest_advanced": true
}
```

---

## 2. 核心概念

| 概念 | 说明 |
|---|---|
| **session** | 多轮编辑会话。首次调用不填 `session_id`，后端自动创建。后续编辑传入同一 `session_id` 即可基于最新图片继续修改。 |
| **turn** | 单次生图回合。每轮生成产生一个 `turn_id`，记录 prompt、model、parent、output 等。 |
| **artifact** | 实际图片文件。每个 turn 产出至少 1 个 artifact（raw 未加水印原图）。`add_watermark=true` 时额外产出 display（带水印）。 |
| **variant** | 图片变体：`raw`（原图，多轮编辑的 lineage anchor）、`display`（带水印，面向终端用户）、`thumbnail`（缩略图）。 |
| **owner_scope** | 租户隔离键。格式 `jwt_subject\x1Fapp_tenant_id\x1Fapp_user_id`，由请求头 `X-App-User-Id` / `X-App-Tenant-Id` 自动计算。 |
| **idempotency** | 幂等保护。同一 `client_request_id` + 同一请求内容 = 返回同一 task_id，不会重复扣费。 |
| **CAS** | 乐观锁。多轮编辑时传入 `expected_parent_artifact_id`，若期间有其他请求已修改，返回 `latest_advanced: false`。 |

---

## 3. 端点速查

### 3.1 提交异步生图任务

```
POST /api/v1/assistant/generate-image-async
```

**请求体**：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `prompt` | string | ✅ | 图片描述 |
| `model_id` | string | ✅ | 模型 ID，如 `gemini-3-flash-preview` |
| `style` | string | | `anime` / `watercolor` / `sketch` / `DEFAULT` |
| `n` | int | | 生成数量，默认 1 |
| `aspect_ratio` | string | | `1:1` / `16:9` / `9:16` / `4:3` / `3:4` |
| `add_watermark` | bool | | 是否加水印，默认 `true` |
| `session_id` | string | | 多轮编辑会话 ID（首次不填，后端自动创建） |
| `parent_artifact_id` | string | | 基于指定图片编辑（覆盖 session 自动继承） |
| `expected_parent_artifact_id` | string | | CAS 乐观锁：期望当前最新图片 ID |
| `client_request_id` | string | | 幂等键（UUID），同一 key 不重复计费 |
| `app_user_id` | string | | 多租户用户 ID（需配 `X-App-User-Id` 头） |
| `app_tenant_id` | string | | 多租户组织 ID（需配 `X-App-Tenant-Id` 头） |
| `callback_url` | string | | 完成回调地址（需公网可访问，会被 SSRF guard 校验） |
| `return_variants` | string[] | | 额外返回变体 URL，如 `["thumbnail"]` |

**响应**（提交成功）：

```json
{
  "task_id": "task_xxx",
  "status": "pending",
  "progress": 0,
  "prompt": "...",
  "model_id": "...",
  "created_at": "..."
}
```

---

### 3.2 轮询任务状态

```
GET /api/v1/assistant/image-task/{task_id}
```

**必须复用提交时的 Cookie**。不同 Cookie = 不同匿名用户 → `owner_scope` 不匹配 → 404。

**响应**：

```json
{
  "task_id": "task_xxx",
  "status": "completed",
  "progress": 100,
  "prompt": "一只橘猫",
  "model_id": "gemini-3-flash-preview",
  "provider": "google",
  "images": [
    {
      "artifact_id": "art_xxx",
      "download_url": "https://s3.../watermarked.png?presigned=...",
      "url": "https://s3.../watermarked.png?presigned=...",
      "width": 1024,
      "height": 1024
    }
  ],
  "output_artifact_id": "art_yyy",
  "turn_id": "itn_zzz",
  "session_id": "ses_www",
  "parent_artifact_id": null,
  "client_request_id": null,
  "latest_advanced": true,
  "error": null,
  "error_code": null,
  "duration_ms": 23564,
  "created_at": "...",
  "completed_at": "..."
}
```

字段说明：

| 字段 | 说明 |
|---|---|
| `output_artifact_id` | 本次 turn 产出的 **raw** artifact ID（多轮编辑的 lineage anchor） |
| `turn_id` | 本次 turn 唯一 ID |
| `latest_advanced` | `true` = CAS 成功，session 的 `latest_artifact_id` 已更新为本次 output；`false` = 并发冲突，session 指针未被更新 |
| `error_code` | 错误码：`reference_not_found` / `provider_unavailable` / `request_failed` / `internal_error` 等 |

---

### 3.3 获取预签名下载 URL

任务响应中的 `download_url` / `url` 有过期时间。过期后可通过此端点换取新的：

```
GET /api/v1/assistant/artifacts/{artifact_id}/download-url?variant=display&expires_in=3600
```

| 参数 | 默认值 | 说明 |
|---|---|---|
| `variant` | `display` | `raw` / `display` / `thumbnail`，支持自动 fallback |
| `expires_in` | 3600 | 60~3600 秒 |

**Variant fallback 链**：
- `thumbnail` → `display` → `raw`
- `display` → `raw`
- `raw` → 无 fallback，不存在则 404

**响应**：

```json
{
  "artifact_id": "art_xxx",
  "variant": "display",
  "url": "https://s3.../watermarked.png?presigned=...",
  "expires_at": "2026-05-07T04:29:54.489601+00:00",
  "width": 1024,
  "height": 1024,
  "mime_type": "image/png"
}
```

---

### 3.4 查询会话历史

```
GET /api/v1/assistant/image-sessions/{session_id}?limit=50&cursor=&include_urls=false
```

| 参数 | 默认值 | 说明 |
|---|---|---|
| `limit` | 50 | 每页数量，最大 200 |
| `cursor` | | 分页游标，上一页 `next_cursor` |
| `include_urls` | false | `true` 时为每 turn 填充 `output_url`（会产生额外 S3 请求） |

**响应**：

```json
{
  "session_id": "ses_xxx",
  "latest_artifact_id": "art_xxx",
  "locked_style": "anime",
  "created_at": "...",
  "updated_at": "...",
  "turns": [
    {
      "turn_id": "itn_xxx",
      "task_id": "task_xxx",
      "prompt": "一只橘猫",
      "model_id": "gemini-3-flash-preview",
      "style": "anime",
      "add_watermark": true,
      "parent_artifact_id": null,
      "output_artifact_id": "art_xxx",
      "status": "completed",
      "error": null,
      "error_code": null,
      "created_at": "...",
      "completed_at": "...",
      "output_url": "https://s3..."
    }
  ],
  "next_cursor": null
}
```

---

## 4. 多轮编辑流程

### 4.1 首次生成（创建 session）

```bash
curl -c cookie.txt -b cookie.txt -X POST .../generate-image-async \
  -d '{"prompt":"一只橘猫","model_id":"gemini-3-flash-preview","style":"anime"}'
```

返回中 `session_id` 为本次会话 ID。保存此 ID。

### 4.2 后续编辑（基于 session 最新图片）

```bash
curl -c cookie.txt -b cookie.txt -X POST .../generate-image-async \
  -d '{
    "prompt":"让猫戴上帽子",
    "model_id":"gemini-3-flash-preview",
    "style":"anime",
    "session_id": "ses_xxx"
  }'
```

后端自动读取 session 的 `latest_artifact_id` 作为参考图，传给 Gemini 进行编辑。

### 4.3 基于指定图片编辑（覆盖 session 继承）

```bash
curl -c cookie.txt -b cookie.txt -X POST .../generate-image-async \
  -d '{
    "prompt":"加上背景",
    "model_id":"gemini-3-flash-preview",
    "session_id": "ses_xxx",
    "parent_artifact_id": "art_xxx"
  }'
```

### 4.4 CAS 乐观锁（防并发覆盖）

```bash
curl -c cookie.txt -b cookie.txt -X POST .../generate-image-async \
  -d '{
    "prompt":"加上背景",
    "model_id":"gemini-3-flash-preview",
    "session_id": "ses_xxx",
    "expected_parent_artifact_id": "art_xxx"
  }'
```

若 `latest_advanced: false`，说明有其他请求已修改了 session，本次基于的参考图已不是最新。Java 端应重新拉取 session 历史，基于最新图片重试。

### 4.5 Style Lock

- 首次传入非 `DEFAULT` 的 style → session 记录 `locked_style`
- 后续请求不填 style → 自动继承 `locked_style`
- 传入 `DEFAULT` → 清除 lock
- 传入其他 style → 覆盖 lock

---

## 5. 幂等性

```bash
curl -c cookie.txt -b cookie.txt -X POST .../generate-image-async \
  -d '{
    "prompt":"一只橘猫",
    "model_id":"gemini-3-flash-preview",
    "client_request_id": "uuid-001"
  }'
```

- 同一 `client_request_id` + **相同请求内容** → 返回同一 `task_id`（不重复计费）
- 同一 `client_request_id` + **不同请求内容** → 409 `duplicate_request_in_flight`
- 不填 `client_request_id` → 不启用幂等保护

---

## 6. 错误码

| 错误码 | HTTP | 说明 |
|---|---|---|
| `reference_not_found` | 404 | 参考图片不存在或无权访问 |
| `reference_too_large` | 413 | 参考图片超过大小限制 |
| `provider_unavailable` | 503 | Gemini / DashScope 服务不可用或未配置 |
| `provider_busy` | 429 | 生成并发过高，稍后重试 |
| `persistence_busy` | 503 | 图片持久化并发饱和 |
| `storage_unavailable` | 503 | S3 存储未配置 |
| `db_unavailable` | 503 | 数据库连接失败 |
| `validation_error` | 422 | 参数校验失败 |
| `request_failed` | 4xx/5xx | 请求被拒绝 |
| `internal_error` | 500 | 内部错误 |
| `duplicate_request_in_flight` | 409 | 同一 `client_request_id` 但内容不同 |
| `callback_ssrf_denied` | 403 | callback_url 为内网地址，被 SSRF guard 拒绝 |

---

## 7. Java 集成示例

```java
// 1. 首次生成
String clientRequestId = UUID.randomUUID().toString();
String response = api.post("/api/v1/assistant/generate-image-async", Map.of(
    "prompt", "一只橘猫在樱花树下",
    "model_id", "gemini-3-flash-preview",
    "style", "anime",
    "add_watermark", true,
    "client_request_id", clientRequestId,
    "app_user_id", currentUser.id,
    "app_tenant_id", currentUser.tenantId
));

// 2. 轮询结果（复用同一 CookieStore）
JsonNode task = objectMapper.readTree(response);
String taskId = task.get("task_id").asText();

for (int i = 0; i < 40; i++) {
    JsonNode status = api.get("/api/v1/assistant/image-task/" + taskId);
    if ("completed".equals(status.get("status").asText())) {
        String imageUrl = status.get("images").get(0).get("url").asText();
        String sessionId = status.has("session_id") ? status.get("session_id").asText() : null;
        String outputArtifactId = status.get("output_artifact_id").asText();
        break;
    }
    Thread.sleep(3000);
}

// 3. 基于 session 继续编辑
api.post("/api/v1/assistant/generate-image-async", Map.of(
    "prompt", "让猫戴上帽子",
    "model_id", "gemini-3-flash-preview",
    "session_id", sessionId
));

// 4. URL 过期后刷新
JsonNode urlResp = api.get("/api/v1/assistant/artifacts/" + artifactId + "/download-url?variant=display");
String freshUrl = urlResp.get("url").asText();

// 5. 查询历史
JsonNode history = api.get("/api/v1/assistant/image-sessions/" + sessionId);
```

---

## 8. 已知限制与注意事项

1. **Cookie 复用**：轮询必须使用与提交相同的 Cookie，否则 `owner_scope` 不匹配 → 404。
2. **SSRF Guard**：`callback_url` 必须是公网可访问地址，内网 IP / 私有域名会被拒绝。
3. **S3 GetObject 权限**：当前 IAM 用户缺少 `s3:GetObject`，直接下载 presigned URL 会报 `AccessDenied`。不影响**生成**（PutObject 正常），只影响**下载**。需在 AWS IAM 控制台给 `ai-gateway-s3` 用户添加 `s3:GetObject`。
4. **水印**：`add_watermark=true` 时返回 watermarked display URL，`output_artifact_id` 始终指向 raw 原图（多轮编辑的 lineage anchor）。
5. **下载变体**：`/artifacts/{id}/download-url?variant=raw` 获取未加水印原图，用于多轮编辑参考。
6. **Presigned URL 过期**：默认 1 小时，过期后通过 download-url 端点重新获取。
