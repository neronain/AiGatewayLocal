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

## Member endpoints

### `GET /v1/models`

OpenAI-shaped catalogue, filtered by the caller's role.

```json
{
  "object": "list",
  "data": [{
    "id": "coding",
    "object": "model",
    "owned_by": "litegate",
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

The same models grouped by purpose, for a member-facing UI.

```json
{
  "user": { "display_name": "Somchai", "role": "member" },
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
  "user_id": "…", "external_id": "6412345678", "role": "member",
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
    "litegate": { "text_input_tokens": 360, "visual_input_tokens": 1105,
                "accounting": "upstream" }
  }
}
```

`usage.litegate` is a gateway addition; OpenAI SDKs ignore unknown fields.
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
| `x-litegate-model` | The alias that served the request |
| `x-litegate-endpoint` | Which backend was chosen |

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

`x-litegate-protocol` tells you which path served the request:
`anthropic-native` or `anthropic-via-openai`.

### `POST /v1/messages/count_tokens`

Pre-flight estimate. Uses the gateway's estimator, not the model's tokenizer —
treat it as approximate.

```json
{ "input_tokens": 1465,
  "litegate": { "text_input_tokens": 360, "visual_input_tokens": 1105,
              "accounting": "estimated" } }
```

---

### `GET /v1/assistant/status`

Whether the console assistant has a model to talk to, for this caller.

```json
{ "available": true, "model": "general", "display_name": "General AI", "reason": null }
```

`available: false` carries a `reason` and the console hides the chat box rather
than showing one that cannot answer.

### `POST /v1/assistant/chat`

```json
{ "messages": [{ "role": "user", "content": "why is my request rejected?" }] }
```

Streams back an OpenAI-shaped SSE response. The gateway prepends a system prompt
and a state block describing this deployment as the caller is permitted to see
it; only the last 12 turns are forwarded.

The request is not privileged. It goes through the same capability gate, quota
check, routing and usage recording as `POST /v1/chat/completions`, so it can be
rejected for quota like any other call. Messages over 4000 characters are
refused with `400`.

Nothing is stored: history is the caller's to keep and the console keeps it in
`sessionStorage`.

---

## Admin endpoints

`manager` or `admin` required as noted. Restrict `/admin/*` to the management
network at the proxy (SEC-5).

| Method | Path | Role | Purpose |
|---|---|---|---|
| POST | `/admin/users` | admin | Create a user |
| GET | `/admin/users?role=` | manager | List users |
| PATCH | `/admin/users/{id}` | admin | Update role / status |
| POST | `/admin/workspaces` | admin | Create a workspace |
| GET | `/admin/workspaces` | manager | List workspaces |
| POST | `/admin/workspaces/{id}/models` | manager | Replace the allowed alias list |
| POST | `/admin/workspaces/{id}/join` | manager | Enroll a user |
| POST | `/admin/api-keys` | manager | Issue a key (**plaintext returned once**) |
| GET | `/admin/api-keys?user_id=` | manager | List keys (prefix only) |
| DELETE | `/admin/api-keys/{id}` | manager | Revoke |
| POST | `/admin/quota-policies` | admin | Create a policy |
| GET | `/admin/quota-policies` | manager | List policies |
| GET | `/admin/models` | admin | Registry incl. upstream names, endpoints, health |
| POST | `/admin/registry/reload` | admin | Reload YAML (see the worker caveat below) |
| POST | `/admin/models/{alias}/compatibility` | admin | Record a test result |
| GET | `/admin/models/{alias}/compatibility` | manager | READY / DEGRADED roll-up |
| GET | `/admin/usage/summary?days=` | manager | Per-model totals |
| GET | `/admin/usage/top-users?days=` | manager | Heaviest users |

> **`POST /admin/registry/reload` reloads only the worker that handled the
> request.** With multiple uvicorn workers, the file-watcher
> (`GW_REGISTRY_RELOAD_SECONDS`) is what propagates a change to all of them.

### `GET /admin/assistant`

Which model the console assistant uses, and how well every chat model would suit
the role.

```json
{
  "pinned": "",
  "source": "automatic",
  "effective": "coder-next",
  "automatic_choice": "coder-next",
  "candidates": [
    {
      "alias": "coder-next", "display_name": "Coder Next", "usable": true, "score": 90,
      "reasons": [
        { "kind": "good", "detail": "131,222-token context — room for state and history." },
        { "kind": "good", "detail": "Plain chat model — answers without narrating." }
      ]
    }
  ]
}
```

`source` is `console`, `environment` or `automatic`. Candidates are ranked, not
filtered: an unusable model stays in the list carrying the `blocker` reason that
made it unusable.

### `PUT /admin/assistant`

```json
{ "alias": "coder-next" }
```

An empty `alias` clears the pin and returns to the automatic choice. Returns the
same body as `GET`.

Refuses (`400`) an alias that cannot serve the role, naming the failing check —
no chat capability, a context window too small for the state block, or a chat
test the suite could not pass. `404` for an unknown alias.

---

### `GET /admin/integrations/lmds`

```json
{
  "base_url": "http://192.168.1.92:8600",
  "configured": true,
  "has_token": true,
  "appliable_issues": ["reasoning_not_separated", "tools_flag_missing"]
}
```

The token is never returned, not even to an admin — a console that can display
a token is a console that leaks it into a screenshot.

### `PUT /admin/integrations/lmds`

```json
{ "base_url": "http://192.168.1.92:8600", "token": "..." }
```

Omitting `token` keeps the stored one; sending `""` clears it. An empty
`base_url` disconnects the tool. Returns the same body as `GET`.

### `POST /admin/integrations/lmds/test`

Makes a real authenticated call and reports which fleet answered.

```json
{
  "ok": true, "hostname": "Autodeploy", "ip": "192.168.1.92",
  "version": "0.2.0", "nodes": 6,
  "node_names": ["spark-head", "msi-5", "msi-6"]
}
```

Failure is reported in the body, not as an HTTP error — the request succeeded,
the connection is what did not:

```json
{ "ok": false, "reason": "The deploy tool rejected the token. Copy the one it prints with `lmds web --status`." }
```

### `POST /admin/models/{alias}/apply-fix`

```json
{ "issue": "tools_flag_missing", "endpoint": "msi-6", "parser": "qwen3_coder" }
```

Asks the connected deploy tool to restart that backend's bundle with the parser
set. `endpoint` may be omitted when the model has exactly one.

```json
{
  "alias": "coder-next", "endpoint": "msi-6", "issue": "tools_flag_missing",
  "applied": { "tool_parser": "qwen3_coder" },
  "node": "msi-6", "slug": "coder-next", "job": { "id": "..." },
  "next": "Re-run verification on 'coder-next' to confirm the finding is gone."
}
```

Reports what it sent, not that it worked: the model server is restarting, and
whether the finding is gone is a question only a fresh probe answers.

`400` when no deploy tool is connected, when the endpoint has no `managed_by`
naming an `lmds_node` and `lmds_slug`, when the finding is not one of
`appliable_issues`, or when the parser name is not letters, digits, underscore
and hyphen. Errors from the deploy tool are passed through verbatim.

---

### `POST /admin/api-keys`

```json
{ "user_id": "…", "workspace_id": "…", "name": "CS101 key", "expires_in_days": 180 }
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
