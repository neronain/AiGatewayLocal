# API Reference

Base URL: `https://gateway.university.ac.th`
Interactive docs: `/docs` · OpenAPI: `/openapi.json`

## Authentication

Both header styles are accepted everywhere, so the OpenAI and Anthropic SDKs work
unmodified:

```http
Authorization: Bearer edu_sk_...
x-api-key: edu_sk_...
```

## Error envelope

OpenAI routes:

```json
{
  "error": {
    "code": "MODEL_CAPABILITY_NOT_SUPPORTED",
    "message": "Model 'coding' does not support image input. Choose a model whose badge shows 'Image', for example a vision model.",
    "type": "invalid_request_error",
    "param": null,
    "details": { "model": "coding", "required_capability": "image input" },
    "request_id": "c18dc378623d4e26bc72f6b009f3041d"
  }
}
```

`/v1/messages` returns Anthropic's shape instead:

```json
{ "type": "error", "error": { "type": "invalid_request_error", "message": "...", "code": "..." } }
```

Branch on `error.code` — it is stable. See [PRD §13.1](PRD.md#131-error-taxonomy)
for the full table.

Every response carries `x-request-id`. Quote it when reporting a problem.

---

## Student endpoints

### `GET /v1/models`

OpenAI-shaped catalogue, filtered by the caller's role.

```json
{
  "object": "list",
  "data": [{
    "id": "coding",
    "object": "model",
    "owned_by": "edullm-gateway",
    "display_name": "Local Coder",
    "purpose": ["coding", "agent"],
    "capabilities": { "chat": true, "vision": false, "tools": true, "streaming": true,
                      "agentic": true, "coding": true, "reasoning": false,
                      "audio": false, "embedding": false },
    "modalities": { "input": ["text"], "output": ["text"] },
    "context_window": 262144,
    "max_output_tokens": 16384,
    "badges": ["Text", "Code", "Tools", "Agent"]
  }]
}
```

`upstream_model` and `endpoints` appear **only** for `role=admin`.

### `GET /v1/catalog`

The same models grouped by purpose, for a student-facing UI.

```json
{
  "user": { "display_name": "Somchai", "role": "student" },
  "sections": [{
    "purpose": "coding",
    "title": "Coding AI",
    "models": [{
      "id": "coding",
      "name": "Local Coder",
      "badges": ["Text", "Code", "Tools", "Agent"],
      "context": "256K Context",
      "claude_code_ready": true,
      "supports_images": false
    }]
  }]
}
```

### `GET /v1/me`

Identity plus remaining quota.

```json
{
  "user_id": "…", "external_id": "6412345678", "role": "student",
  "quota": {
    "window": "day",
    "window_end": "2026-08-13T00:00:00+00:00",
    "limits": { "max_requests": 300, "max_input_tokens": 1000000,
                "max_output_tokens": 200000, "max_images": 50 },
    "used":   { "requests": 12, "text_input_tokens": 4310, "visual_input_tokens": 1105,
                "input_tokens": 5415, "output_tokens": 2200, "images": 1 }
  }
}
```

---

### `POST /v1/chat/completions`

OpenAI Chat Completions. Standard parameters (`temperature`, `top_p`, `stop`,
`tools`, `tool_choice`, `stream`, `stream_options`) are forwarded as sent.

**Text**

```bash
curl -X POST $GW/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"coding","messages":[{"role":"user","content":"เขียน bubble sort"}]}'
```

**Text + image** — base64 data URL (remote URLs are disabled by default):

```json
{
  "model": "gemma-vision",
  "messages": [{
    "role": "user",
    "content": [
      { "type": "text", "text": "อธิบายภาพนี้" },
      { "type": "image_url", "image_url": { "url": "data:image/png;base64,iVBORw0KG..." } }
    ]
  }]
}
```

**Response** — `model` is always the alias, never the repository name:

```json
{
  "id": "chatcmpl-…", "object": "chat.completion", "model": "gemma-vision",
  "choices": [{ "index": 0, "message": { "role": "assistant", "content": "…" },
                "finish_reason": "stop" }],
  "usage": {
    "prompt_tokens": 1465, "completion_tokens": 210, "total_tokens": 1675,
    "edullm": { "text_input_tokens": 360, "visual_input_tokens": 1105,
                "accounting": "upstream" }
  }
}
```

`usage.edullm` is a gateway addition; OpenAI SDKs ignore unknown fields.
`accounting` is `upstream` (backend-reported) or `estimated` — see
[PRD §8.3](PRD.md#fr-37--visual-token-accounting--p1).

**Streaming** — set `"stream": true`. Standard OpenAI SSE. The gateway always
asks the backend for a final usage chunk so accounting stays accurate; if you did
not set `stream_options.include_usage`, that chunk is stripped before it reaches
you, so the stream matches exactly what you asked for.

**Response headers**

| Header | Meaning |
|---|---|
| `x-request-id` | Correlates with logs and usage rows |
| `x-edullm-model` | The alias that served the request |
| `x-edullm-endpoint` | Which backend was chosen |

---

### `POST /v1/messages`

Anthropic Messages API — what Claude Code speaks. Available for any alias whose
`protocols.anthropic` is true, **including when the backend only speaks OpenAI**;
the gateway translates both directions.

```bash
curl -X POST $GW/v1/messages \
  -H "x-api-key: $KEY" -H "anthropic-version: 2023-06-01" \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "coding",
    "max_tokens": 1024,
    "system": "You are a helpful coding assistant.",
    "messages": [{"role":"user","content":"Write hello world in Rust"}],
    "tools": [{"name":"read_file","description":"Read a file",
               "input_schema":{"type":"object","properties":{"path":{"type":"string"}}}}]
  }'
```

Supported: text and image blocks, `system` (string or blocks), `tools`,
`tool_choice`, `tool_use` / `tool_result` (including images inside a tool result),
`stop_sequences`, `temperature`, `top_p`, `stream`.

Streaming emits the full Anthropic event sequence:

```
message_start → content_block_start → content_block_delta* → content_block_stop
              → message_delta → message_stop
```

Anthropic-only features with no OpenAI equivalent (extended thinking, citations,
cache control) are dropped when translating and never fabricated on the way back.

`x-edullm-protocol` tells you which path served the request:
`anthropic-native` or `anthropic-via-openai`.

### `POST /v1/messages/count_tokens`

Pre-flight estimate. Uses the gateway's estimator, not the model's tokenizer —
treat it as approximate.

```json
{ "input_tokens": 1465,
  "edullm": { "text_input_tokens": 360, "visual_input_tokens": 1105,
              "accounting": "estimated" } }
```

---

## Admin endpoints

`instructor` or `admin` required as noted. Restrict `/admin/*` to the management
network at the proxy (SEC-5).

| Method | Path | Role | Purpose |
|---|---|---|---|
| POST | `/admin/users` | admin | Create a user |
| GET | `/admin/users?role=` | instructor | List users |
| PATCH | `/admin/users/{id}` | admin | Update role / status |
| POST | `/admin/courses` | admin | Create a course |
| GET | `/admin/courses` | instructor | List courses |
| POST | `/admin/courses/{id}/models` | instructor | Replace the allowed alias list |
| POST | `/admin/courses/{id}/enroll` | instructor | Enroll a user |
| POST | `/admin/api-keys` | instructor | Issue a key (**plaintext returned once**) |
| GET | `/admin/api-keys?user_id=` | instructor | List keys (prefix only) |
| DELETE | `/admin/api-keys/{id}` | instructor | Revoke |
| POST | `/admin/quota-policies` | admin | Create a policy |
| GET | `/admin/quota-policies` | instructor | List policies |
| GET | `/admin/models` | admin | Registry incl. upstream names, endpoints, health |
| POST | `/admin/registry/reload` | admin | Reload YAML (see the worker caveat below) |
| POST | `/admin/models/{alias}/compatibility` | admin | Record a test result |
| GET | `/admin/models/{alias}/compatibility` | instructor | READY / DEGRADED roll-up |
| GET | `/admin/usage/summary?days=` | instructor | Per-model totals |
| GET | `/admin/usage/top-users?days=` | instructor | Heaviest users |

> **`POST /admin/registry/reload` reloads only the worker that handled the
> request.** With multiple uvicorn workers, the file-watcher
> (`GW_REGISTRY_RELOAD_SECONDS`) is what propagates a change to all of them.

### `POST /admin/api-keys`

```json
{ "user_id": "…", "course_id": "…", "name": "CS101 key", "expires_in_days": 180 }
```

```json
{ "id": "…", "api_key": "edu_sk_…", "key_prefix": "edu_sk_jYPu",
  "expires_at": "2027-02-08T…",
  "warning": "Store this key now. It cannot be retrieved again." }
```

The plaintext is stored nowhere. A lost key must be revoked and re-issued.

### `GET /admin/usage/summary?days=7`

```json
{
  "window_days": 7,
  "by_model": [{
    "model": "gemma-vision", "requests": 3,
    "text_input_tokens": 1350, "visual_input_tokens": 510, "output_tokens": 35,
    "images": 2, "avg_latency_ms": 1.0, "avg_ttft_ms": null
  }],
  "errors": [{ "code": "UPSTREAM_UNAVAILABLE", "count": 1 }]
}
```

No prompt, response, or image content appears here or in any other response —
the schema has no column for it (PRD §11).

---

## Health and metrics

| Path | Auth | Purpose |
|---|---|---|
| `GET /healthz` | none | Liveness. Always 200 while the process runs |
| `GET /readyz` | none | Readiness. 503 until the registry is loaded, the DB answers, and ≥1 backend is healthy |
| `GET /metrics` | network-restricted | Prometheus |
| `GET /v1/health/endpoints` | admin | Per-endpoint health, in-flight, failure counts |
| `POST /v1/health/probe` | admin | Probe every backend immediately |

```json
{ "ready": true, "database": "ok",
  "models_loaded": 3, "endpoints_healthy": 2, "endpoints_total": 3,
  "registry_errors": [] }
```
