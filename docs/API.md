# API Reference

Base URL: `https://gateway.example.com`
Interactive docs: `/docs` · OpenAPI: `/openapi.json`

## Every route at a glance

**Member** — any valid key, narrowed by the key's scope and its workspace:

| Method | Path | Purpose |
|---|---|---|
| GET | [`/v1/models`](#get-v1models) | OpenAI-shaped catalogue, filtered by the caller's role |
| GET | [`/v1/catalog`](#get-v1catalog) | The same models grouped by purpose, for a member-facing UI |
| GET | [`/v1/me`](#get-v1me) | Identity plus remaining quota |
| GET | [`/v1/me/key`](#get-v1mekey) | What is written on the key making this call — its limits, its expiry, and whether it carries admin rights |
| POST | [`/v1/chat/completions`](#post-v1chatcompletions) | OpenAI chat, unary or streaming, text and images |
| POST | [`/v1/messages`](#post-v1messages) | Anthropic surface — what Claude Code talks to |
| POST | [`/v1/responses`](#post-v1responses) | Responses API — what Codex talks to |
| POST | [`/v1/embeddings`](#post-v1embeddings) | Vectors, OpenAI-shaped |
| POST | [`/v1/rerank`](#post-v1rerank) | Cohere/Jina-shaped rerank, passed through |
| POST | [`/v1/messages/count_tokens`](#post-v1messagescount_tokens) | Pre-flight estimate — approximate by design |
| GET | [`/v1/assistant/status`](#get-v1assistantstatus) | Whether the console assistant has a model for this caller |
| POST | [`/v1/assistant/chat`](#post-v1assistantchat) | Ask the console assistant, streamed |

**Admin and operations** — the full table is under
[Admin endpoints](#admin-endpoints); health and metrics are under
[Health and metrics](#health-and-metrics).

Which surfaces an alias exposes is per-model (`spec.protocols`), and
`GET /v1/models` reports it — so a client checks instead of guessing.

## Authentication

Both header styles are accepted everywhere, so the OpenAI and Anthropic SDKs work
unmodified:

```http
Authorization: Bearer lg_sk_...
x-api-key: lg_sk_...
```

Keys issued before v1.4 start with `edu_sk_` and keep working — see
[Key format](#post-adminapi-keys).

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

Two `403` codes carry a `details.reason_code` that says *which* rule refused,
for a client or a console that wants to explain it rather than print the
message:

| `code` | `details.reason_code` | Meaning |
|---|---|---|
| `INSUFFICIENT_SCOPE` | `restricted_key` | The key's owner is a manager or an admin, but the key carries a limit of its own and so does not carry those rights. `details.limited_by` and `details.owner_role` say which — see [Keys that carry a limit](#keys-that-carry-a-limit) |
| `INSUFFICIENT_SCOPE` | `key_carries_rights` | A manager tried to issue, change or revoke another manager's key that has no limit on it. Such a key carries its owner's manager rights, so only an administrator or its owner decides it — see [Who may decide a key](#who-may-decide-a-key) |
| `INSUFFICIENT_SCOPE` | `enrol_administrator` | A manager tried to add an administrator to a workspace. `details.administrators` lists them (up to 50, by `external_id`); nobody in the request was added — see [Who may decide a key](#who-may-decide-a-key) |
| `MODEL_NOT_PERMITTED` | `key_bundle_off` | The key is limited to access groups and none of them grants anything now — switched off, deleted or empty. `details.allowed` is `[]`. The key calls nothing until a group is switched back on or the key is given a model list |
| `MODEL_NOT_PERMITTED` | `workspace_left` | The key was issued for a workspace its owner is no longer a member of |

`MODEL_NOT_PERMITTED` carries other `reason_code` values for the ordinary
cases; the message names the models the key does allow. `key_bundle_off` and
`workspace_left` are answered the same way when the request names
`model: "auto"` instead of an alias.

### Request ids

Every response carries two id headers:

| Header | Whose | Unique | Use it for |
|---|---|---|---|
| `x-litegate-request-id` | the gateway's — generated per request, never taken from the caller | always | Reporting a problem. It is the `request_id` in an error body, the id in the gateway's logs, and the key of the usage row |
| `x-request-id` | yours, echoed back unchanged if you sent one; otherwise the same value as `x-litegate-request-id` | only if you make it so | Correlating with your own logs |

A caller-chosen id is never the key of anything: two requests may carry the same
`x-request-id` and each still gets its own usage row. The value you sent is kept
on the row (first 128 characters) so an admin can find the request by either id —
see `GET /admin/usage/requests` under [Admin endpoints](#admin-endpoints).

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
                      "audio": false, "embedding": false, "rerank": false },
    "modalities": { "input": ["text"], "output": ["text"] },
    "context_window": 262144,
    "max_output_tokens": 16384,
    "badges": ["Text", "Code", "Tools", "Agent"],
    "protocols": ["openai", "anthropic", "responses"]
  }]
}
```

`upstream_model` and `endpoints` appear **only** for an administrator — a
console session, or an administrator's key that carries no limit of its own. An
administrator's key that was limited to a model list, an access group, a
workspace or a quota of its own gets the member view; see
[Keys that carry a limit](#keys-that-carry-a-limit).

`protocols` also carries `embeddings` and `rerank` for retrieval models — those
aliases answer on `/v1/embeddings` or `/v1/rerank` and nowhere else, so the list
is what tells a client which door to knock on.

#### The Codex catalogue

Codex does not read this endpoint the way the SDKs do. Its model manager sends
`GET /v1/models?client_version=<its version>` and decodes `{"models": [...]}` —
the OpenAI `data` list alone fails with ``missing field `models` ``, and Codex
falls back to built-in metadata for every alias (a 272,000-token window, image
input assumed).

`client_version` is the only thing that tells the two kinds of client apart; the
path and headers are the same. When it is present — any value, it is not
interpreted — the response carries `models` **next to** the unchanged `data`:

```json
{
  "object": "list",
  "data": [ ... ],
  "models": [{
    "slug": "coding",
    "display_name": "Local Coder",
    "description": "Agentic coding model with tool calling and long context.",
    "context_window": 262144,
    "max_context_window": 262144,
    "input_modalities": ["text"],
    "supported_reasoning_levels": [],
    "shell_type": "default",
    "apply_patch_tool_type": null,
    "truncation_policy": { "mode": "bytes", "limit": 10000 },
    "support_verbosity": false,
    "default_verbosity": null,
    "supports_image_detail_original": false,
    "experimental_supported_tools": [],
    "base_instructions": "You are a coding agent running in the Codex CLI, ...",
    "visibility": "list",
    "supported_in_api": true,
    "priority": 0,
    "availability_nux": null,
    "upgrade": null
  }]
}
```

Without the parameter the response is what it always was — each Codex entry
carries a ~21 KB system prompt, which has no business reaching a client that
did not ask for it.

| Field | Where the value comes from |
|---|---|
| `context_window`, `max_context_window` | `spec.limits.context_tokens`. The second is the ceiling for `model_context_window` in the user's Codex config: it can be lowered, not raised past the real window. |
| `input_modalities` | `text` always; `image` only when the alias has `capabilities.vision` **and** `image` in `modalities.input` — the same condition the gateway enforces on the request. Never `video`: Codex's enum has no such value and an unknown one fails the decode of the whole response. |
| `slug`, `display_name`, `description` | `metadata.alias`, `metadata.display_name`, `metadata.description`. |
| `priority` | Position in the list: coding-purpose aliases first, then by alias. Codex picks the first when no model is configured. |
| `supported_reasoning_levels` | Always empty. The Responses translator does not forward `reasoning.effort` to the backend, so offering levels would offer a control that does nothing. |
| `shell_type`, `apply_patch_tool_type`, `truncation_policy`, `base_instructions`, … | Not properties of the model. They describe Codex's own harness, and the registry knows nothing about them, so they are the values Codex's fallback already used — `base_instructions` is Codex's own prompt, vendored under `app/vendor/codex/` (Apache-2.0). Codex sends the same request it sent before; only its picture of the model changes. |

There is no `max_output_tokens`: Codex's schema has no field for it and Codex
does not send one. `spec.limits.max_output_tokens` is enforced by the gateway.

**Which aliases are listed.** The ones this key may call (same rule as `data`)
that Codex can actually use: `protocols.responses`, and `capabilities.chat`,
`tools` and `streaming`. Codex attaches tools to every request, so an alias
without tool calling is refused with `400` however it is listed.

**When Codex asks.** Only when it is signed in with ChatGPT, or the provider is
configured with command auth (`[model_providers.<id>.auth]`). With a plain key
(`env_key`, `experimental_bearer_token`) it never requests the catalogue. Save the response to a file and point `model_catalog_json` at it —
the same body is a valid catalogue file:

```bash
curl -fsS -H "Authorization: Bearer lg_sk_..." \
  "http://127.0.0.1:8080/v1/models?client_version=codex" \
  -o ~/.codex/litegate-models.json
```

```toml
# ~/.codex/config.toml, above any [table] — relative paths resolve against ~/.codex
model_catalog_json = "litegate-models.json"
```

Three things to know about that file. Codex **will not start** if it is set and
missing. It replaces Codex's catalogue rather than adding to it, so it belongs
in a config (or profile) that points at this gateway. And it is a snapshot:
fetch it again after models are added or their limits change.

Verified against Codex CLI 0.149.1. `ModelInfo` is Codex's internal schema and
has changed between releases; if a future version rejects this shape, Codex
prints the decode error and falls back exactly as it did before — the request
path is unaffected.

When Codex fetches the catalogue itself it caches it in
`~/.codex/models_cache.json` for five minutes, and that cache is not keyed by
provider (a `TODO` in Codex's `models-manager/src/manager.rs`). Someone who
switches between this gateway and another provider inside that window can see
the other one's model list until it expires. `model_catalog_json` does not use
the cache.

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
  },
  "quota_policies": [ { "source": "default", "window": "day", "used": { … }, … },
                      { "source": "user", "policy_name": "coding 50/day",
                        "applies_to": { "model_alias": "coding" }, "exhausted": true, … } ]
}
```

`quota` is the limit that binds this key when no particular model is named.
`quota_policies` is every limit that can stop a request made with this key —
that one, any policy aimed at a specific model or bundle, and this key's own
ceilings — each with the usage of its own counter. Same shape as `policies`
under [`GET /admin/users/{id}/quota`](#get-adminusersidquota).

### `GET /v1/me/key`

The facts about the key making this call. Nothing the holder does not already
have: the key itself is theirs, and its limits are what they run into when a
request is refused.

```json
{
  "via": "key",
  "key": {
    "prefix": "lg_sk_jYPu", "label": "nightly report",
    "issued_at": "2026-10-01T02:11:09+00:00",
    "expires_at": "2027-03-30T02:11:09+00:00",
    "last_used_at": "2026-10-09T07:40:00+00:00",
    "limited_to_models": ["coding"],
    "limited_to_groups": [],
    "limited_by": ["models"],
    "admin_access": false
  }
}
```

Never the key and never its hash — only the prefix, to tell which key this is.
Called with a console session instead of a key, the answer is
`{"via": "session", "key": null}`.

| Field | Meaning |
|---|---|
| `limited_to_models`, `limited_to_groups` | The model list and the access groups written on this key. Empty = the key adds no narrowing of its own |
| `limited_by` | Every kind of limit written on this key, in a fixed order: `models`, `access_groups`, `workspace` (the key was issued for one workspace), `cap` (a quota policy of its own that is enabled and not expired). The list is complete — the same one [`GET /admin/api-keys`](#get-adminapi-keys) shows for this key |
| `admin_access` | `true` when this key may call the routes its owner's role allows — the owner is a manager or an admin **and** `limited_by` is empty. A script that got `403` from `/admin` asks here and gets the reason |

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

**`max_tokens`** — the model's `limits.max_output_tokens` (shown as *Max output*
in the catalogue) is sent to the backend on every request. Name a smaller
`max_tokens` (or `max_completion_tokens`) and you get that; name a larger one,
or none at all, and you get the ceiling — reduced further when the prompt
leaves less room than that in the context window. `/v1/messages` and
`/v1/responses` behave the same way. For a reasoning model the tokens it spends
thinking come out of the same allowance.

**Streaming** — set `"stream": true`. Standard OpenAI SSE. The gateway always
asks the backend for a final usage chunk so accounting stays accurate; if you did
not set `stream_options.include_usage`, that chunk is stripped before it reaches
you, so the stream matches exactly what you asked for.

**`response_format`** — structured output

The standard shape is forwarded untouched:

```json
{ "type": "json_schema",
  "json_schema": { "name": "movie_review", "strict": true, "schema": { "type": "object", "...": "..." } } }
```

Anything else is either repaired before it is sent, or refused. It is not passed
on as it came, because the backends here do not refuse a malformed
`response_format` — measured against llama.cpp (build b11046, 2026-10-09), every
shape below except an unknown `type` came back `200` with an answer that
ignored the schema. The caller then fails while parsing a reply against a schema
the model never saw.

*Repaired, and announced in the `x-litegate-adjusted` response header:*

| You sent | The backend receives | Header entry |
|---|---|---|
| `strict`, `name`, `description`, `schema` or `parameters` beside `type` instead of inside `json_schema` | the key moved inside `json_schema` | `response_format.strict->response_format.json_schema.strict` |
| the same key in both places | the one inside `json_schema`; the outer one is dropped | `response_format.strict->(dropped)` |
| `json_schema.parameters` (the function-calling name) | `json_schema.schema` | `response_format.json_schema.parameters->response_format.json_schema.schema` |
| `json_schema` (or `schema` / `parameters`) with no `type` | `"type": "json_schema"` added | `response_format.type="json_schema"` |
| no `json_schema.name`, or an empty one | `"name": "response"` | `response_format.json_schema.name="response"` |

Entries are separated by `, ` in the order they were applied. The header is
sent on streams and on answers served from the response cache, and is absent
when nothing was changed.

*Refused* — `400 INVALID_REQUEST` with `param` naming the field, before any
backend is contacted, before a stream opens, and with no usage row:

| `param` | When |
|---|---|
| `response_format` | not an object (a bare string such as `"json_object"`, an array) |
| `response_format.type` | missing with nothing to infer it from, or not a string |
| `response_format.json_schema` | present and not an object |
| `response_format.json_schema.schema` | missing, `null`, or not an object, when the type is `json_schema` |
| `response_format.json_schema.name` | present and not a string |

*Left alone:* a correct `json_schema`, `{"type": "text"}`, `{"type": "json_object"}`
(including llama.cpp's `json_object` + `schema` extension), `{}`, `null`, and a
`type` the gateway does not know — the backend judges that one itself.

**The schema itself is never rewritten.** The gateway does not add
`additionalProperties: false` and does not rewrite `required`. Measured on the
same llama.cpp build, an object that does not say is already closed, `strict`
changes nothing, and rewriting `required` changes the answer.

The response cache key is built after the repair, so two spellings that reach the
backend as the same bytes share one cached answer.

> **Not verified on vLLM.** The repairs and refusals above were measured against
> llama.cpp only; vLLM's behaviour was read from its source (v0.19.1), not run.

The prompt-size estimate does not count `response_format` on this route: the
llama.cpp build measured reports the same `prompt_tokens` with and without a
schema. The translated `/v1/responses` and `/v1/messages` paths do count the
schema, so the two estimates differ by the size of the schema.

**`model: "auto"`** — let the gateway choose

Send `"model": "auto"` and LiteGate picks a model that can serve the request,
**from the models that key may already use**. It is not a way around
permissions: the gateway does the choosing, but the shortlist is exactly what the
member could have named themselves. `auto` is accepted on this route only.

The shortlist is made from the shape of the request — does it carry images, does
it ask for tools, how long is the prompt — never from guessing intent, the same
rule [`app/core/rules.py`](../app/core/rules.py) follows. Only then is it ranked,
by the strategy an administrator chose:

| Strategy | Ranks by |
|---|---|
| `fastest` (default) | **Speed measured from real traffic** through this gateway (output tok/s, then TTFT, exponentially weighted). Never reads the quality score |
| `roomiest` | Largest context window first |
| `quality` | The administrator's `spec.quality_score` (0–100), highest first; equal scores are settled by speed |
| `balanced` | `(2 × quality_score + 100 × speed) ÷ 3`, where `speed` is the model's output tok/s as a fraction of the fastest candidate's. One point of quality is worth 2% of the fastest model's speed: 90 at 40 tok/s beats 40 at 200 tok/s (66.7 to 60.0); 70 at 40 tok/s loses to it (53.3 to 60.0) |

Nothing is ever dropped for missing data — it sorts last and stays eligible. A
model with too few speed samples sorts last under `fastest`; a model with no
quality score sorts last under `quality` and `balanced`, and with no scores set
anywhere those two rank exactly like `fastest`. Under `balanced` a model that has
a score but no speed samples yet is weighed as if it were as fast as the fastest
candidate, and the preview marks it (`speed_assumed`).

A gateway where nobody has set a score or changed the strategy behaves as it did
before these strategies existed. Answers served from the response cache are not
fed into the speed statistics.

> **More than one worker.** Speed statistics are kept in each worker's memory
> and start empty on restart. `quality` does not depend on them unless scores
> tie; `fastest` and `balanced` can pick differently on different workers while
> their numbers are close. This follows from the code and has not been measured
> on a multi-worker deployment.

`x-litegate-served-by` names what actually ran, and the response `model` field is
a real alias, never the word `auto`.

When nothing can be chosen, the answer says why. A key that can call nothing for
a reason the gateway knows — its access groups are switched off, or its owner
left the workspace it was issued for — gets the same `403 MODEL_NOT_PERMITTED`
with `details.reason_code` that naming a model would give it. Otherwise
`404 MODEL_NOT_FOUND`: either no model is available to the key at all, or none
of the available ones can serve this request, and the message lists what was
considered.

Staff can see the current ranking and the numbers behind it at
[`GET /admin/auto/preview`](#get-adminautopreview), or in the console under
**Dashboard → Available models**; an administrator changes the strategy there or
with [`PUT /admin/auto/strategy`](#put-adminautostrategy).

```bash
curl -X POST $GW/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"auto","messages":[{"role":"user","content":"สวัสดี"}]}'
```

**Response headers**

| Header | Meaning |
|---|---|
| `x-litegate-request-id` | The gateway's own id for this request — correlates with logs and the usage row. See [Request ids](#request-ids) |
| `x-request-id` | Your `x-request-id` echoed back, or the gateway's id if you sent none |
| `x-litegate-model` | The alias the caller asked for — never changes with internal routing |
| `x-litegate-served-by` | The alias that **actually ran**. Differs from the above when a routing rule reroutes (`coding` → `coding-long` for an oversized prompt) or when `model: "auto"` picked for you |
| `x-litegate-endpoint` | Which backend machine answered |
| `x-litegate-failed-over` | Backends tried and skipped before this one |
| `x-litegate-output-cap` | Present **only when the gateway sent the backend a lower output limit than you asked for** (or, if you named none, lower than the model's own limit): `granted=256; requested=4000; reason=context`. `reason` is `model-limit` (above the model's `max_output_tokens`), `n` (the limit is shared between `n` choices) or `context` (the estimated prompt leaves less room in the context window). `requested=default` means you sent no limit. Sent on `/v1/chat/completions`, `/v1/messages` and `/v1/responses`, streaming or not; on a stream it describes the model chosen before the first byte |
| `x-litegate-adjusted` | Present **only when the gateway rewrote part of the request before forwarding it** — today, a `response_format` whose keys were in the wrong place (see [`response_format`](#post-v1chatcompletions) above): `response_format.strict->response_format.json_schema.strict, response_format.json_schema.parameters->response_format.json_schema.schema`. Entries read `from->to`, `from->(dropped)` or `path="value added"`. `/v1/chat/completions` only; sent on streams and on cached answers; exposed to browsers through CORS |
| `x-litegate-ignored` | Present **only on `/v1/responses` served by translation, when the request declared tools the backend cannot run** (OpenAI-hosted tools such as `web_search`): `tools[1]:web_search, tools[2]:image_generation`. Those definitions were not offered to the model; everything else was. A request that *depends* on such a tool (`tool_choice` forcing it, or its calls in `input`) is refused with a 400 instead |
| `x-litegate-by` | Author attribution (present on every response) |

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

Anthropic-only features with no OpenAI equivalent (citations, cache control) are
dropped when translating and never fabricated on the way back.

**Thinking.** A reasoning model behind an OpenAI backend returns its chain of
thought in `reasoning_content` (`reasoning` on newer vLLM). When the request
enables thinking — `"thinking": {"type": "enabled", ...}`, which Claude Code
sends on its own — the gateway returns it as `thinking` blocks ahead of the
text, and as `thinking_delta` events when streaming. Without that field no
thinking block is returned, as with Anthropic's own API, so
`content[0].text` keeps working for callers that never asked. `signature` is
always `""`: there is no Anthropic signature to give, and the gateway does not
invent one. `thinking` blocks sent back in a later request are dropped.

This matters most when a reply runs out of `max_tokens` while still thinking:
the answer is `stop_reason: "max_tokens"` with a thinking block and no text. A
caller that did not ask for thinking sees an empty text block — raise
`max_tokens` (and the model's `limits.max_output_tokens`).

**Structured output.** `output_config.format` (or `output_format`, the beta
name) of type `json_schema` with a `schema` object is translated to a
chat-completions `response_format` — `name` defaults to `"response"`, `strict` is
set. A format the translator cannot carry is refused with
`400 INVALID_CONTENT_BLOCK` naming the place, instead of being dropped and
answered with free text as it was before:

| `param` | When |
|---|---|
| `output_config.format` / `output_format` | not an object |
| `output_config.format.schema` / `output_format.schema` | type `json_schema` without a `schema` object — including the chat-shaped `{"type": "json_schema", "json_schema": {...}}` |
| `output_config.format.type` / `output_format.type` | any other type, or none. This includes `{"type": "json_object"}`, which Anthropic's API does not have |

This applies when the alias is served by translation. An alias with a backend
that speaks the Anthropic protocol itself receives the request whole and judges
the format itself.

`x-litegate-protocol` tells you which path served the request:
`anthropic-native` or `anthropic-via-openai`.

### `POST /v1/responses`

OpenAI Responses API — what Codex speaks. Available for any alias whose
`protocols.responses` is true, **including when the backend only speaks chat
completions**; the gateway translates both directions.

```bash
curl -X POST $GW/v1/responses \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{
    "model": "coding",
    "instructions": "You are a helpful coding assistant.",
    "input": [
      {"role":"user","content":[{"type":"input_text","text":"list the files"}]},
      {"type":"function_call","call_id":"c1","name":"ls","arguments":"{}"},
      {"type":"function_call_output","call_id":"c1","output":"a.txt"}
    ],
    "tools": [{"type":"function","name":"ls","parameters":{"type":"object"}}],
    "max_output_tokens": 1024
  }'
```

The shapes differ in more than field names:

| chat completions | Responses |
|---|---|
| `messages[]` | `input[]` — messages **and** tool traffic, mixed at the same level |
| system message | `instructions` (top-level string) |
| `max_tokens` | `max_output_tokens` |
| `tools[].function.name` | `tools[].name` (flattened) |
| `choices[].message` | `output[]` — one item per message or tool call |
| `message.reasoning_content` / `.reasoning` | a `reasoning` output item, before the message |
| `usage.prompt_tokens` | `usage.input_tokens` |
| `finish_reason: "length"` | `status: "incomplete"`, `incomplete_details.reason: "max_output_tokens"` |
| `finish_reason: "content_filter"` | `status: "incomplete"`, `incomplete_details.reason: "content_filter"` |

A turn that used tools comes back as `function_call` / `function_call_output`
items sitting **beside** the messages, not nested inside them. Reading only
`{role, content}` would hand the model a conversation where it asked for a tool
and never learned the answer.

Streaming emits the typed event sequence Codex reads. Each output item opens,
streams and closes at its own `output_index` before the next one opens:

```
response.created → response.in_progress
  reasoning      → response.output_item.added → response.reasoning_text.delta*
                 → response.reasoning_text.done → response.output_item.done
  message        → response.output_item.added → response.content_part.added
                 → response.output_text.delta* → response.output_text.done
                 → response.content_part.done → response.output_item.done
  function_call  → response.output_item.added → response.function_call_arguments.delta*
                 → response.function_call_arguments.done → response.output_item.done
response.completed | response.incomplete
```

Every event carries a `sequence_number` that increases by one **across all event
types** — the client uses it to detect a gap. The `output` of the final event
holds the same items (same ids) that were streamed.

**Reasoning models.** A backend that separates its chain of thought
(`reasoning_content`, or `reasoning` on newer vLLM) gets it back as a `reasoning`
output item:

```json
{ "id": "rs_…", "type": "reasoning", "status": "completed", "summary": [],
  "content": [{ "type": "reasoning_text", "text": "…" }] }
```

The raw text goes in `content`; `summary` is empty because nothing summarised it.
It is not opt-in — as on OpenAI, a reasoning model's `output` starts with a
reasoning item, so read the answer from `output_text` or the `message` item, not
from `output[0]`. Reasoning items in the *request* are dropped. Codex hides raw
reasoning unless `show_raw_agent_reasoning = true` is set in its config.

Thinking counts against `max_output_tokens`. A model that spends the whole budget
thinking returns `status: "incomplete"` with a reasoning item and no message —
and **Codex treats `response.incomplete` as a dropped stream and retries it**
(`stream_max_retries`, 5 by default), so one such turn is billed as six. Give
reasoning models a `limits.max_output_tokens` that covers thinking *and* the
answer.

**Structured output.** `text.format` is translated to a chat-completions
`response_format`: `{"type": "json_object"}` as it is, and
`{"type": "json_schema", "name": …, "schema": {…}}` with `name` defaulting to
`"response"` and `strict` / `description` carried over. `{"type": "text"}` and
`{}` mean plain text. Anything else is refused with `400 INVALID_CONTENT_BLOCK`
rather than dropped:

| `param` | When |
|---|---|
| `text.format` | not an object |
| `text.format.schema` | type `json_schema` without a `schema` object — a missing schema, `parameters` in its place, or the chat-shaped `json_schema: {...}` wrapper |
| `text.format.type` | a type with no chat-completions equivalent, or no type at all |

As on `/v1/messages`, an alias with a backend that speaks the Responses API
itself receives the request whole.

`previous_response_id` returns `400`. Codex uses it to have the server keep the
conversation; this gateway stores no prompts and no responses (PRD §12), so there
is no head to continue from. Answering with the tail of a conversation whose head
was silently dropped is worse than saying so.

`x-litegate-protocol` tells you which path served the request:
`responses-native` or `responses-via-openai`.

### `POST /v1/embeddings`

OpenAI-shaped. Available for any alias whose `protocols.embeddings` is true —
which requires `capabilities.embedding` **and** at least one backend declaring
`protocols.embeddings`. There is no translation path: the gateway cannot
synthesise a vector from a chat model, so an alias without a pooling backend is
refused when the registry loads, not when a request arrives.

```bash
curl -X POST $GW/v1/embeddings \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"embed","input":["ประโยคแรก","ประโยคที่สอง"]}'
```

`input` accepts a string, an array of strings, an array of token ids, or an
array of those arrays. Everything else in the body (`encoding_format`,
`dimensions`, `user`) is forwarded untouched; only `model` is rewritten to the
name the backend knows.

At most **2048 items per request** (`GW_MAX_BATCH_ITEMS`; `0` lifts the cap).
The same ceiling applies to `documents` on `/v1/rerank`. It exists because quota
is checked before a request runs and recorded after: without a ceiling, one call
carrying a hundred thousand documents spends a month of allowance in a single
shot, and the limit only bites on the *next* call. 2048 is what OpenAI allows on
its own embeddings endpoint, so a client written against that needs no change.
Over the ceiling the gateway answers `400` naming the count, the limit, and the
remedy — before the backend is touched.

```json
{ "object": "list", "model": "embed",
  "data": [{ "object": "embedding", "index": 0, "embedding": [0.01, -0.02] }],
  "usage": { "prompt_tokens": 4242, "total_tokens": 4242,
             "litegate": { "text_input_tokens": 4242, "output_tokens": 0,
                           "accounting": "upstream" } } }
```

### `POST /v1/rerank`

Cohere/Jina-shaped, because **OpenAI has no rerank endpoint** and this is the
shape vLLM and TEI actually serve. The gateway passes the body through and
rewrites `model`; it does not translate.

```bash
curl -X POST $GW/v1/rerank \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"rerank","query":"ใครเป็นคนเขียน",
       "documents":["เอกสารหนึ่ง","เอกสารสอง"],"top_n":2}'
```

```json
{ "id": "rerank-…", "model": "rerank",
  "results": [{ "index": 1, "document": { "text": "เอกสารสอง" },
                "relevance_score": 0.98 }],
  "usage": { "total_tokens": 777,
             "litegate": { "text_input_tokens": 777, "output_tokens": 0,
                           "accounting": "upstream" } } }
```

`documents` must be an array of strings; Cohere's object form is refused with a
`400` naming the position, because the backend cannot read it. `/v1/score` and
Cohere's `/v2/rerank` are **not** served — v2 returns a different `results`
shape, and answering a v2 path with a v1 body would be a lie.

**How these two are counted.** Neither has output tokens, so `output_tokens` is
`0` on the usage row and in every report; the input dimension carries the whole
cost.

| Request | Counted as |
|---|---|
| `/v1/embeddings`, strings | the characters of every string, summed |
| `/v1/embeddings`, token ids | the number of ids — exact, not estimated |
| `/v1/rerank` | **the query once per document**, plus every document |
| either | one HTTP call = one request against `max_requests` |

The query is charged per document because a cross-encoder runs the pair
(query, document) afresh for each one. That is what the backend bills, and
counting the query once understates a 50-document call by an order of magnitude.

The context window is checked **per item**, never against the batch total: each
string — or each query+document pair — must fit, while the sum routinely will
not. A 500-document batch is ordinary indexing traffic.

Neither route streams, neither is cached, and neither ever fails over to a
*different* model: vectors from two models are not comparable, so a silent
substitution corrupts an index with nothing to show for it. Failover between
machines serving the same alias still happens.

### `POST /v1/messages/count_tokens`

Pre-flight estimate. Uses the gateway's estimator, not the model's tokenizer —
treat it as approximate. It is the same estimate, at the model's own
`wide_chars_per_token`, that the context check applies to the real request.

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

### Keys that carry a limit

The role in the table below is the role of the **person**. Whether a particular
API key carries that role's rights is a second question, and — from 1.13.0; in
1.12.1 and earlier the owner's role alone decided — the answer is no for any key
that has a limit of its own written on it:

| `limited_by` | The limit | Lifted by |
|---|---|---|
| `models` | a model list on the key | `PATCH /admin/api-keys/{id}` with `"models": []` — by someone who [may decide that key](#who-may-decide-a-key) |
| `access_groups` | one or more access groups on the key | cannot be removed from an issued key — issue a new one |
| `workspace` | the key was issued for one workspace | cannot be removed from an issued key — issue a new one |
| `cap` | a quota policy aimed at this key that is enabled and has not expired | `DELETE /admin/quota-policies/{id}`, or let it expire |

One of them is enough. Such a key calls its models exactly as before and is
refused on every route that needs `manager` or `admin` — `/admin/*`,
`/admin/tools/*`, `GET /v1/health/endpoints`, `POST /v1/health/probe` — and on
`GET /v1/models` it gets the member view. A key issued for one job must not be
able to lift its own limits or issue other keys.

```json
{ "error": {
    "code": "INSUFFICIENT_SCOPE",
    "message": "This API key is limited to a model list, so it does not carry its owner's administrator rights: a key issued for one job must not be able to lift its own limits or issue other keys. Sign in to the console to do this, or use a key issued without a model list, access group, workspace or quota of its own.",
    "details": { "reason_code": "restricted_key", "limited_by": ["models"], "owner_role": "admin" } } }
```

`403`. The message and `limited_by` name every limit on the key, not only the
first ("a model list and a quota of its own"). A member's key gets the plain
*"Administrator privileges are required."* / *"Manager privileges are
required."* it always got, without `details`.

Not counted as a limit: `scopes` (stored, never enforced — and the bootstrap
key of every install carries `["admin"]`), the expiry date, and `kind`. Console
sessions are never limited. A key can read all of this about itself at
[`GET /v1/me/key`](#get-v1mekey); the console marks such keys
`no admin access` under **Access & Keys → API keys**.

To do admin work from a script, use a key issued with no model list, no access
group, no workspace and no quota of its own — and guard it accordingly.

### Who may decide a key

Because a key without a limit carries its owner's rights, lifting a limit hands
those rights out, and putting one on — or revoking the key — takes them away.
`POST /admin/api-keys`, `PATCH /admin/api-keys/{id}` and
`DELETE /admin/api-keys/{id}` are open to managers, and all three apply one rule
on top of the workspace scoping described under each:

| The key belongs to | Who may issue, change (models **and** expiry) or revoke it |
|---|---|
| an administrator | an administrator only |
| a manager, and the key has no limit on it — before the change or after it | an administrator, or that manager |
| a manager, and the key is limited and stays limited | as before: an administrator, or a manager of a workspace the owner is in |
| a member | as before |

So a manager who is not the owner cannot: amend, extend or revoke an
administrator's key; issue another manager a key with no limit (a limited one
is still fine); lift the last limit from another manager's key; or limit,
extend or revoke another manager's unlimited key. A quota of its own counts as a
limit here too.

```json
{ "error": { "code": "INSUFFICIENT_SCOPE",
    "message": "Only an admin can change an admin key." } }
```

```json
{ "error": { "code": "INSUFFICIENT_SCOPE",
    "message": "A key of t0001's with no limit on it carries their manager rights, which may reach workspaces you do not manage. Only an administrator or t0001 can change one. t0001 can change it from their own console.",
    "details": { "reason_code": "key_carries_rights", "owner_role": "manager" } } }
```

The verb is `issue`, `change` or `revoke`; `t0001` stands for the owner's
`external_id`.

**An administrator is added to a workspace by an administrator.** Membership
gives an administrator nothing — they are not scoped by it — but it puts them
inside that workspace's manager's views: their user row, the names, prefixes
and limits of their keys, their usage. So from 1.13.0
`POST /admin/workspaces/{id}/join` and `POST /admin/workspaces/{id}/members`
refuse a manager who names an administrator:

```json
{ "error": { "code": "INSUFFICIENT_SCOPE",
    "message": "staff001 is an administrator. Only an administrator can add an administrator to a workspace - they can add themselves, or another administrator can. Nobody was added.",
    "details": { "reason_code": "enrol_administrator", "administrators": ["staff001"] } } }
```

Only people who would actually be *added* are checked. An administrator who is
already a member does not cause a refusal (a roster sent again goes through),
existing memberships are left as they are, a manager can still remove an
administrator from their workspace, and a manager can still enrol another
manager. An administrator who was put into a manager's workspace before 1.13.0
stays there, and stays visible to that manager, until somebody removes them.

| Method | Path | Role | Purpose |
|---|---|---|---|
| POST | `/admin/users` | admin | Create a user |
| GET | `/admin/users?role=` | manager | List users |
| PATCH | `/admin/users/{id}` | admin | Update role / status |
| POST | `/admin/workspaces` | admin | Create a workspace |
| GET | `/admin/workspaces` | manager | List workspaces |
| POST | `/admin/workspaces/{id}/models` | manager | Replace the allowed alias list |
| POST | `/admin/workspaces/{id}/join` | manager | Enroll a user. A manager cannot enrol an administrator — `403`, `reason_code: enrol_administrator` |
| PATCH | `/admin/workspaces/{id}/status` | manager | `active` / `suspended` — reversible |
| DELETE | `/admin/workspaces/{id}` | admin | Refused while it has members or keys |
| POST | `/admin/workspaces/{id}/members` | manager | Enrol several people at once. From a manager, one administrator among the people who would be added refuses the whole list and adds nobody |
| POST | `/admin/access-groups` | admin | Name a bundle of aliases |
| GET | `/admin/access-groups` | manager | List bundles and who holds them |
| PATCH | `/admin/access-groups/{id}` | admin | Edit — reaches every workspace holding it |
| DELETE | `/admin/access-groups/{id}` | admin | Refused (`400`) while a workspace holds it, a quota policy targets it, or an API key that is not revoked names it. `details` carries `used_by`, `quota_policies` and `api_keys` (counts), `api_keys_expired` (how many of those keys have expired) and `keys` (up to 50 of them as `{id, name, key_prefix}`); the message names up to five. An **expired** key still blocks the delete, deliberately — it can be extended, and would then point at a group that no longer exists; a revoked key does not. A group cannot be taken off an issued key, so for keys the way out is to revoke them and issue replacements — or disable the group instead of deleting it, which stops it granting anything and is reversible |
| POST | `/admin/api-keys` | manager | Issue a key (**plaintext returned once**) — `models`, `access_groups`, `kind`; blanks filled from the workspace defaults. An administrator's key, and an unlimited key for another manager, are not a manager's to issue — [Who may decide a key](#who-may-decide-a-key) |
| GET | [`/admin/api-keys?user_id=`](#get-adminapi-keys) | manager | List keys (prefix only), each with what limits it (`limited_by`), its owner's role, and the state of its sealed copy |
| POST | `/admin/api-keys/{id}/reveal` | **admin** | Show an issued key again — only when a sealed copy exists and opens. See [`POST /admin/api-keys`](#post-adminapi-keys) |
| GET | [`/admin/key-vault`](#get-adminkey-vault) | admin | Which secret the sealed key copies open under, which cannot be opened, and what in the configuration needs attention |
| POST | [`/admin/key-vault/reseal`](#post-adminkey-vaultreseal) | admin | Move copies sealed under `GW_KEY_REVEAL_SECRET_PREVIOUS` to the current secret |
| PATCH | `/admin/api-keys/{id}` | manager | Amend a live key — `{"days": n}` from today (`null` removes it) and/or `{"models": [...]}` replacing the scope. Either alone; neither disturbs the other. Subject to [Who may decide a key](#who-may-decide-a-key) |
| DELETE | `/admin/api-keys/{id}` | manager | Revoke. Subject to [Who may decide a key](#who-may-decide-a-key) |
| GET | `/admin/users/{id}/quota` | manager | Every limit that can bind this person's requests, each with its own usage |
| POST | `/admin/users/{id}/quota/reset` | **admin** | Zero every counter that binds this person; reports which were cleared and which were not — usage records untouched |
| POST | `/admin/quota-policies` | admin | Create a policy — `name`, window limits, per-minute limits, `expires_in_days`, and either `model_alias` or `access_group_id`. Validated by the same rules as PATCH: every limit is a whole number ≥ 0 where **0 means unlimited** and `null` is refused (leave the field out instead); `window` is `hour`/`day`/`month`/`term`; `expires_in_days` is ≥ 1 or `null` for no expiry. `scope` must agree with the target sent (`user` needs `user_id`, `workspace` needs `workspace_id`, `key` needs `api_key_id` and nothing else) and is inferred from the target when omitted; a target that does not exist is a 400 (404 `MODEL_NOT_FOUND` for an unknown alias). A second live policy for the same scope, target and window is refused with **409 `CONFLICT`** naming the existing one (PATCH refuses the same collision when it changes `window` or revives an expired policy); the same target with a *different* window is created but answers `effective: false` with `shadowed_by`, because only the older one is used — except ceilings on an API key, which are all enforced |
| GET | `/admin/quota-policies` | manager | List policies in the order they are resolved (oldest first). Each carries `effective`; a policy that is listed but not in force says why — `expired: true`, or `shadowed_by: {id, name}` naming the older policy for the same target that is used instead. A manager sees the global policies and those for the workspaces, people and keys they can see |
| PATCH | `/admin/quota-policies/{id}` | admin | Edit an existing policy in place: send `days` to move the expiry, and/or any of `window`, `max_requests`, `max_input_tokens`, `max_output_tokens`, `max_images`, `max_requests_per_minute`, `max_tokens_per_minute`. Scope and target are fixed — changing who a policy applies to is a new policy, not an edit |
| GET | `/admin/models` | admin | Registry incl. upstream names, endpoints, health |
| POST | `/admin/models/preview` | admin | Validate a draft document and render its YAML — nothing is written to disk |
| POST | `/admin/models` | admin | Create or replace a registry file from a whole model document (`201`) |
| DELETE | `/admin/models/{alias}` | admin | Delete that alias's registry file |
| POST | `/admin/models/detect` | admin | Probe a backend and suggest capabilities — advisory, saved only if you then POST them |
| PATCH | `/admin/models/{alias}/enabled` | admin | Take an alias or one endpoint out of service |
| PATCH | `/admin/models/{alias}/endpoints/{name}` | admin | `priority`, `weight`, `max_concurrency` |
| POST | `/admin/registry/reload` | admin | Reload YAML (see the worker caveat below) |
| POST | `/admin/version/check` | admin | Ask GitHub whether a newer release exists — the only outbound request the gateway ever makes on its own behalf, and only when this is called. Always `200`: an air-gapped host gets `{"ok": false, "reason": …}`, never an error. Falls back to the newest tag when no release is published |
| POST | `/admin/models/{alias}/compatibility` | admin | Record a test result |
| GET | `/admin/models/{alias}/compatibility` | manager | READY / DEGRADED roll-up |
| GET | [`/admin/usage/summary?days=`](#get-adminusagesummarydays7) | manager | Per-model totals and averages |
| GET | [`/admin/usage/latency?days=&workspace_id=`](#get-adminusagelatency) | manager | p50 / p95 / p99 of latency and time to first token, per model and (admin only) per machine |
| GET | [`/admin/auto/preview`](#get-adminautopreview) | manager | What `model: "auto"` would pick right now, and the numbers that decided it |
| PUT | [`/admin/auto/strategy`](#put-adminautostrategy) | **admin** | Choose how `model: "auto"` ranks |
| GET | `/admin/usage/top-users?days=` | manager | Heaviest users |
| GET | `/admin/usage/quota` | manager | Allowance spent, per person, against the limit that resolves for them |
| GET | `/admin/usage/by-key?days=` | manager | Requests and tokens per key — activity, not allowance |
| GET | `/admin/usage/requests?id=&days=` | manager | The usage row(s) for one request. `id` is the gateway's id (`x-litegate-request-id`, or `request_id` from an error body) **or** the `x-request-id` the caller sent — the latter can match several rows, because callers may reuse it. Metadata only; newest first, at most 100; `days` defaults to 7. Each row carries `cache_hit` — `true` when the answer came from the response cache and no backend ran; rows written before that column existed read `false`. A request refused before it reached a model (bad key, quota) has no usage row |

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

### `GET /admin/auto/preview`

Which model `model: "auto"` would pick right now, and why. Uses the same ranker as
the request path — no second implementation to drift.

Query: `prompt_tokens` (default 1000), `vision`, `tools`, `protocol`, and
`strategy` — rank with another strategy **without saving it**, to see what it
would choose before switching. An unknown `strategy` is `400 INVALID_REQUEST`
with `param: "strategy"`.

```json
{
  "asked": {"prompt_tokens": 1000, "vision": false, "tools": false, "protocol": "openai"},
  "strategy": "balanced",
  "configured_strategy": "fastest",
  "strategies": ["fastest", "roomiest", "quality", "balanced"],
  "chosen": "coder-big",
  "reason": "คะแนนรวมสูงสุด 66.7 (คุณภาพ 90/100 · ความเร็ว 20% ของตัวเร็วสุด)",
  "ranked": [
    {"rank": 1, "alias": "coder-big", "context_tokens": 262144, "quality_score": 90,
     "output_tps": 40.0, "ttft_ms": 800, "samples": 41,
     "speed": 0.2, "speed_assumed": false, "combined": 66.7},
    {"rank": 2, "alias": "coder-small", "context_tokens": 131072, "quality_score": 40,
     "output_tps": 200.0, "ttft_ms": 210, "samples": 57,
     "speed": 1.0, "speed_assumed": false, "combined": 60.0}
  ],
  "min_samples": 3
}
```

| Field | Meaning |
|---|---|
| `strategy` | The strategy this answer was ranked with — the `strategy` query parameter if sent, otherwise the configured one |
| `configured_strategy` | The one live requests use |
| `strategies` | Every strategy this version knows |
| `ranked[].quality_score` | `spec.quality_score` of the model; `null` = no score set |
| `ranked[].output_tps`, `ttft_ms` | `null` until the model has been seen `min_samples` times on this worker |
| `ranked[].speed`, `speed_assumed`, `combined` | Filled under `balanced` only. `speed` is 0–1 against the fastest candidate; `speed_assumed: true` means the model has a score but no speed samples yet and was weighed as if as fast as the fastest; `combined` is the 0–100 figure the ranking used, `null` when the model has no quality score |

The numbers shown are the ones the ranking used: the candidates are ranked once
and the table is read from that result. They are this worker's statistics —
another worker may hold different ones.

### `PUT /admin/auto/strategy`

```json
{ "strategy": "quality" }
```

```json
{ "strategy": "quality", "previous": "fastest",
  "strategies": ["fastest", "roomiest", "quality", "balanced"] }
```

Admin only — the preview is open to managers, the switch is not, because one
call moves every `auto` request on the gateway to a different model. The choice
is stored in the database (`gateway_settings`, key `auto_strategy`) and read on
each `auto` request, so every worker follows it from its next request. Written
to the audit log as `auto.strategy` with the previous value. `400
INVALID_REQUEST` (`param: "strategy"`) for a name that is not in `strategies`.

With no row stored the strategy is `fastest`. A stored value this version does
not recognise is treated as `fastest` and logged.

---

### `GET /admin/usage/savings`

What this traffic would have cost on a commercial API. Schools that bought the
hardware need to answer "was it worth it" — the token counts were already being
recorded, so this needs no new collection.

Query: `days` (default 30), `baseline`, `workspace_id`.

```json
{
  "window_days": 30,
  "baseline": {"id": "gpt-4o", "label": "OpenAI GPT-4o"},
  "prices_updated": "2026-08-20",
  "requests": 12840,
  "would_have_cost_usd": 412.83,
  "by_model": [{"model": "coding", "would_have_cost_usd": 301.2, "...": "..."}],
  "caveat": "ประมาณการจากราคา list — ไม่ได้คิดส่วนลดตามปริมาณ..."
}
```

**This is a comparison figure, not an invoice.** It uses published list prices and
does not model volume discounts, prompt caching, or batch APIs — real spend would
be lower. The price table is **static in the source** ([`app/core/pricing.py`](../app/core/pricing.py))
so the report works on sites with no outbound internet; `prices_updated` says how
old it is.

`GET /admin/usage/savings/baselines` lists the available baselines for a dropdown.

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

### Adding a model from the console

Three requests, in the order the wizard makes them. Each is useful on its own —
a script that already knows the document can go straight to `POST /admin/models`.

`POST /admin/models` writes a file under `config/models/`, so the directory has
to be writable. `GET /admin/registry/status` answers that before the Save button
is offered:

```json
{ "config_dir": "/opt/litegate/config", "writable": true,
  "reload_seconds": 30, "errors": [] }
```

Under systemd the directory is writable because `ReadWritePaths` lists it. A
site that keeps the registry in git and installs with `REGISTRY_READONLY=1` gets
`writable: false`, and every write below returns `400` instead.

#### `POST /admin/models/detect` — admin

Asks a backend what it can do. Nothing is saved: the answer is a suggestion the
admin confirms, because a capability the gateway inferred on its own is a
capability nobody measured.

```json
{ "base_url": "http://10.0.0.7:8000/v1",
  "upstream_model": "ucbye/Qwen3-Coder-Next-NVFP4-GB10",
  "api_key_env": "" }
```

`upstream_model` may be omitted — the probe reads the backend's own model list.
`api_key_env` is the **name** of a stored secret, never a key; it is resolved
through the secret store, so no credential travels in this body.

```json
{
  "suggestion": {
    "reachable": true,
    "served_models": ["ucbye/Qwen3-Coder-Next-NVFP4-GB10"],
    "upstream_model": "ucbye/Qwen3-Coder-Next-NVFP4-GB10",
    "context_tokens": 262144,
    "slots": null,
    "wide_chars_per_token": 1.89,
    "capabilities": { "chat": true, "tools": true, "streaming": true, "vision": false },
    "protocols": { "openai": true, "anthropic": false, "responses": false },
    "server_kind": "vllm",
    "notes": ["..."],
    "advice": [{ "issue": "tools_flag_missing", "command": "..." }]
  },
  "confirmed": false
}
```

`confirmed` is always `false` here. It is the reminder that the next two calls
are what make any of this real.

`context_tokens` is what the backend gives **one request**: vLLM's
`max_model_len`, or llama.cpp's per-slot `n_ctx` from `/props` (its
`--ctx-size` divided by `--parallel`). It is the number `limits.context_tokens`
has to match. `slots` is how many requests the backend serves at once
(llama.cpp `total_slots`); `null` means the backend does not say. See
[DEPLOYMENT §4.1a](DEPLOYMENT.md#41a-context-is-per-request--what-to-type-per-backend).

`GET /admin/models/{alias}/advice` runs the same probe against every backend of
a saved model and lists `drift`: capabilities that disagree, plus
`context_tokens (per request)` when the registry declares more than the backend
gives, and `max_concurrency (backend slots)` when an endpoint allows more
concurrent requests than the backend has slots.

**A backend that refuses a prompt for length** answers the client with
`400 CONTEXT_LENGTH_EXCEEDED` and the backend's own message, on every route. It
used to surface as `502 UPSTREAM_ERROR`, which clients retry instead of
compacting. Request-level rejections (400, 413, 422) no longer count towards
marking an endpoint unhealthy.

#### `POST /admin/models/preview` — admin

Validates a draft document and hands back the exact YAML that would be written.
Nothing touches disk, so this is the safe way to see what a form produces.

The body is a whole model document — the same shape as a file in
`config/models/`, as JSON:

```json
{
  "apiVersion": "litegate.dev/v1",
  "kind": "Model",
  "metadata": { "alias": "coding", "display_name": "Local Coder", "visibility": "member" },
  "spec": {
    "upstream_model": "ucbye/Qwen3-Coder-Next-NVFP4-GB10",
    "purpose": ["coding", "agent"],
    "limits": { "context_tokens": 262144, "max_output_tokens": 16384 },
    "capabilities": { "chat": true, "tools": true, "coding": true, "agentic": true },
    "protocols": { "openai": true, "anthropic": true, "responses": true },
    "endpoints": [{ "name": "spark-1", "base_url": "http://10.0.0.7:8000/v1" }]
  }
}
```

`apiVersion` and `kind` default to the values above and may be omitted.

```json
{ "alias": "coding", "filename": "coding.yaml", "yaml": "# Generated by the LiteGate admin console.\n..." }
```

A document that does not validate returns `400 INVALID_REQUEST` with every
problem located, not just the first:

```json
{ "error": { "code": "INVALID_REQUEST", "message": "The model definition is not valid.",
  "details": { "problems": [{ "field": "spec.limits.context_tokens", "message": "Field required" }] } } }
```

#### `POST /admin/models` — admin — `201`

Same body as the preview, validated the same way, then written atomically to
`config/models/<alias>.yaml` (temp file + rename, so a reload landing mid-write
never sees half a file). The registry is reloaded on **this** worker before the
response returns.

```json
{
  "alias": "coding",
  "path": "/opt/litegate/config/models/coding.yaml",
  "created": false,
  "registry_errors": [],
  "propagation_seconds": 30
}
```

`created` is `false` when the alias already existed — the call is an upsert, and
an existing file is replaced in full. Read that literally: **every field you
leave out is cleared**, and the re-render drops the comments in the file. Use
`PATCH .../enabled` or `PATCH .../endpoints/{name}` for a one-line change.

The one exception is `managed_by`, which is carried across a save that never
mentions it: nothing in the request path reads it, so an old console or a
`curl` of yesterday's document could quietly unmanage a fleet and nobody would
see it until the Apply-fix button was missing months later. An explicit
`"managed_by": null` still removes it.

`spec.quality_score` is carried across the same way, for the same reason — a
console tab left open across an upgrade, or yesterday's document, does not know
the field, and losing the score silently moves `model: "auto"` traffic to another
model under the `quality` strategy. Leave the key out and the stored score is
kept; send `"quality_score": null` to clear it. It is a whole number from 0 to
100 (`true`/`false` are refused, not read as 1/0), `GET /admin/models` returns it
for each model, and `POST /admin/models/preview` applies the same rule so the
YAML it shows is the YAML a save would write.

`propagation_seconds` is the honest part of the answer: the other workers pick
the change up from the registry watcher within that many seconds, not at the
moment this returns. `registry_errors` reports files that failed to parse after
the reload — an empty list is the success case.

`400` when the document is invalid or `config/models/` is not writable.

#### `DELETE /admin/models/{alias}` — admin

Removes the registry file and reloads. The same propagation caveat applies: the
other workers stop serving the alias when their watcher next runs.

```json
{ "alias": "coding", "deleted": true }
```

`404 MODEL_NOT_FOUND` when no registry file exists for that alias — including
the case where the alias came from a file the running registry has cached but
someone already deleted from disk. `400` when the directory is read-only.

Deleting is not the way to take a model out of service: it loses the document.
`PATCH /admin/models/{alias}/enabled` is, and it is reversible.

### `PATCH /admin/models/{alias}/enabled`

Takes a model out of service without deleting its file. `enabled` was already
honoured everywhere — the catalogue hides a disabled alias, routing skips a
disabled endpoint — the console just had no way to set it, so the only way down
was to delete the file and rebuild it afterwards.

```json
{ "enabled": false }                              // the whole alias
{ "enabled": false, "endpoint": "spark-worker" }  // one backend, alias keeps serving
```

Turning off the last serving endpoint is refused: the alias would stay listed
and be unable to answer, which reads as a broken gateway rather than a
deliberate change. Disable the model itself instead.

Only that one `enabled:` line in the YAML is rewritten. The full re-render used
by `POST /admin/models` regenerates the document from the parsed model and
drops every comment in it — a fair trade for a form submission, a bad one for a
switch.

### `PATCH /admin/models/{alias}/endpoints/{name}`

How much work one backend takes, changed on its own.

```json
{ "priority": 100, "max_concurrency": 8 }
```

`priority` picks the tier: the highest tier with room takes everything, the
rest are standby. Give two machines the **same** priority and they share — each
request goes to whichever is carrying less, so one being busy does not make the
next person wait. `max_concurrency` is what makes a lower tier useful while the
top one is healthy: once the top tier is full, requests spill down.

Only the lines you send are rewritten, so the comments in the file survive.

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

#### The `managed_by` block

`managed_by` is what turns a finding into a runnable command, and what
`apply-fix` addresses. It sits on an endpoint:

```yaml
endpoints:
  - name: msi-6
    base_url: http://10.0.0.6:8000
    managed_by:
      tool: lmds
      node: ops@10.0.0.6                  # ssh target, for humans
      controller: ~/bundles/coder/coder-single.sh
      lmds_node: msi-6                    # the machine's name in LMDS
      lmds_slug: coder-next               # the bundle LMDS knows it by
```

`lmds_node` is separate from `node` because LMDS addresses machines by the name
in its own registry, which is usually not the ssh target; guessing one from the
other would restart the wrong machine. The field is inert — nothing in the
request path reads it, and every model works without it.

---

### `POST /admin/api-keys`

```json
{ "user_id": "…", "workspace_id": "…", "name": "CS101 key", "expires_in_days": 180 }
```

```json
{ "id": "…", "api_key": "lg_sk_…", "key_prefix": "lg_sk_jYPu",
  "owner_role": "member", "limited_by": ["workspace"],
  "expires_at": "2027-02-08T…", "revealable": false,
  "warning": "Store this key now. It cannot be retrieved again." }
```

`limited_by` lists the limits this key was issued with — `models`,
`access_groups`, `workspace` — **after** the workspace defaults were filled in,
and `owner_role` is the role of the person it belongs to. Together they say at
issue time, rather than at the first `403`, that a key issued to a manager or an
admin will not carry admin rights ([Keys that carry a limit](#keys-that-carry-a-limit)).
A quota of its own is added afterwards as a quota policy, so `cap` never appears
in this response.

By default the plaintext is stored nowhere and a lost key can only be replaced.
Set `GW_KEY_REVEAL_SECRET` and a sealed copy is kept that an **administrator**
can open through `POST /admin/api-keys/{id}/reveal`; `revealable` says which of
the two applies to that key, and it is decided when the key is issued — turning
the setting on later does not make existing keys readable. Every reveal is
recorded and listed by `GET /admin/api-keys/{id}/reveals`.

> The reveal response is **flat** — `{"id", "api_key", "key_prefix"}` — not
> wrapped in `data` like the rest of the admin API.

A reveal that cannot be served is `400 INVALID_REQUEST`. For a key that exists
and is not revoked, `details.seal_state` says which of three situations it is —
they have different remedies:

| `seal_state` | What it means | `details` also carries |
|---|---|---|
| `none` | No copy was kept: the key was issued before reveal was switched on, or reveal is off | — |
| `off` | A sealed copy is stored, but `GW_KEY_REVEAL_SECRET` is unset. Set it back to the secret that sealed the copy | — |
| `lost` | A sealed copy is stored and cannot be shown: no configured secret opens it, or it opens and is not a copy of this key | `reason`, and `sealed_key_id` when the copy records one |

`reason` for a `lost` copy:

| `reason` | Meaning | What helps |
|---|---|---|
| `unknown_secret` | Sealed under a secret that is not configured. `sealed_key_id` is the key id of the secret it needs | Put that secret in `GW_KEY_REVEAL_SECRET_PREVIOUS`, restart, re-seal |
| `damaged` | Sealed under a secret that *is* configured, and the contents no longer open | Nothing — issue a new key |
| `unreadable` | A copy written by 1.12.1 or earlier, which did not record its secret: a wrong secret and a corrupt copy cannot be told apart | Try the old secret as `GW_KEY_REVEAL_SECRET_PREVIOUS` |
| `unknown_format` | Written in a format this version does not read — most likely by a newer LiteGate | Upgrade |
| `not_this_key` | The copy opens, but what is inside is not this key: it does not match the key's stored hash, so it is not shown. The row was changed outside the gateway (a restore or merge that mixed rows, or tampering), or `GW_API_KEY_PEPPER` changed after the key was issued | No reveal secret fixes it. Issue a new key, and find out how the row changed |

In every one of these the key itself still authenticates; only showing it again
is affected. A refused reveal is not written to the reveal log — including a
`not_this_key` one, where the copy did open. A revoked key is never revealed
(`400`, no `seal_state`). The rotation procedure is in
[RUNBOOK.md](RUNBOOK.md#change-gw_key_reveal_secret).

Keys issued before v1.4 carry the `edu_sk_` prefix and keep working: a key is
verified by HMAC over the whole string, so the prefix is only a label.

### `GET /admin/api-keys`

`?user_id=` narrows to one person. A manager sees the keys of people in their
own workspaces.

```json
{ "data": [{
    "id": "…", "user_id": "…", "owner_role": "admin", "workspace_id": null,
    "name": "nightly report", "key_prefix": "lg_sk_jYPu",
    "models": ["coding"], "access_groups": [], "limited_by": ["models"],
    "kind": "service", "revoked": false,
    "expires_at": "2027-03-30T…", "last_used_at": "2026-10-09T…",
    "revealable": true, "seal_state": "current", "seal_reason": ""
}] }
```

| Field | Meaning |
|---|---|
| `limited_by` | Every limit on the key: `models`, `access_groups`, `workspace`, `cap`. Non-empty on a key whose `owner_role` is `manager` or `admin` means the key has no admin rights — the console shows `no admin access` |
| `owner_role` | The role of the person the key belongs to |
| `seal_state` | `none` — no sealed copy · `current` — opens under `GW_KEY_REVEAL_SECRET` · `previous` — opens only under `GW_KEY_REVEAL_SECRET_PREVIOUS`, waiting for a re-seal · `lost` — cannot be shown; `seal_reason` says why · `off` — a copy is stored but reveal is switched off |
| `seal_reason` | Why a `lost` copy cannot be shown — the same values as `details.reason` of a refused reveal (`unknown_secret`, `unreadable`, `damaged`, `unknown_format`, `not_this_key`). Empty for every other state |
| `revealable` | `true` only for `current` and `previous`: the copy was actually opened, and checked against the key's hash, to decide this — not merely found |

### `GET /admin/key-vault`

Admin. Where the sealed key copies stand. No secret, no key and no sealed value
is in the answer — only key ids, which are short one-way labels of a secret.

```json
{
  "enabled": true,
  "current_key_id": "f9a69d99",
  "previous_key_id": "3c1e07ab",
  "sealed": 4,
  "counts": { "current": 1, "previous": 2, "lost": 1, "off": 0 },
  "lost": [{ "id": "…", "name": "ci", "key_prefix": "lg_sk_ab12", "user_id": "…",
             "revoked": false, "reason": "unknown_secret", "sealed_key_id": "77d0c2e4" }],
  "warnings": [{ "code": "rotation_pending", "level": "warning", "message": "2 sealed key copies still open only under GW_KEY_REVEAL_SECRET_PREVIOUS. …" }],
  "last_reseal": { "at": "2026-10-09T08:15:00+00:00", "by": "…", "via": "console", "resealed": 3 }
}
```

`enabled` is whether `GW_KEY_REVEAL_SECRET` is set. `previous_key_id` is `null`
unless a previous secret is set, differs from the current one, and reveal is
enabled. `counts` covers every stored copy, revoked keys included; `lost`
counts every copy that cannot be shown, whatever the reason, and each entry of
the `lost` list carries its `reason`. `last_reseal`
is `null` until a re-seal has been recorded; `via` is `console` or `cli`, and
`by` is `null` for the command line (the audit row carries the operating-system
user instead).

`warnings[].code`:

| Code | Level | Situation |
|---|---|---|
| `rotation_pending` | warning | Copies still open only under the previous secret. Re-seal before removing it |
| `lost` | error | Copies that cannot be revealed because they do not open: sealed under a secret that is not configured, damaged, or in a newer format. The message gives the count of each and what helps for each |
| `sealed_copy_mismatch` | error | Copies that open but are not copies of the key they are stored on (`not_this_key`). Reported apart from `lost` because no secret is missing — a row was changed outside the gateway, or the pepper changed |
| `previous_unused` | info | The previous secret is set and nothing needs it any more — remove it. Also the answer when the only unreadable copies are damaged or mismatched ones, which no secret would open |
| `previous_equals_current` | warning | Both variables hold the same value, so the previous one does nothing |
| `previous_does_not_match` | warning | A previous secret is set but opens none of the copies that are lost for want of a secret — it is not the one that sealed them |
| `sealed_but_disabled` | warning | Copies are stored but `GW_KEY_REVEAL_SECRET` is unset |
| `previous_without_current` | warning | Only the previous secret is set. It never switches reveal on by itself |

The same survey is written to the log once per worker at startup, as one count
line plus the warnings, and printed by `python -m app.tools keyvault status`.

### `POST /admin/key-vault/reseal`

Admin. No body. Re-seals every copy that opens only under
`GW_KEY_REVEAL_SECRET_PREVIOUS` under the current secret.

```json
{ "resealed": 2, "already_current": 1, "lost": 1, "changed_meanwhile": 0,
  "vault": { "enabled": true, "counts": { "current": 3, "previous": 0, "lost": 1, "off": 0 }, "…": "…" } }
```

`vault` is the body of `GET /admin/key-vault` after the run. `lost` counts the
copies this run could not move — the ones no configured secret opens, and the
ones that open but belong to a different key. `changed_meanwhile` counts rows
somebody else re-sealed between this run reading and writing them; they are left
alone.

It is safe to press twice, to interrupt, and to run from two places at once: each
row is committed on its own, and written only if it still holds the value that
was read. Copies already under the current secret are not rewritten, and copies
neither secret opens are not touched. Nor is a copy that opens but is not a copy
of its own key: re-sealing it would dress a wrong copy up as a correctly sealed
one and erase the sign that the row was changed. Copies of revoked keys are
re-sealed too, so the pending count can reach zero. Each run — including one
that failed part way, from the console or from the command line — is written to
the audit log as `keyvault.reseal`.

`400 INVALID_REQUEST` when `GW_KEY_REVEAL_SECRET` is unset: there is no current
secret to re-seal under.

Nothing re-seals on its own at startup, by design: it changes which secret can
open the copies, so it is something an administrator does, with a name and a
time in the audit log, not something a restart does.

### `PATCH /admin/api-keys/{id}`

```json
{ "days": 30, "models": ["claude-opus-4.8", "claude-haiku-4.8"] }
```

```json
{ "id": "…", "expires_at": "2026-09-13T…",
  "models": ["claude-opus-4.8", "claude-haiku-4.8"] }
```

Both fields are optional and independent — send one and the other is left
exactly as it was. Sending neither is refused rather than treated as a no-op.

`models` **replaces** the list; there is no add or remove. `[]` lifts the
restriction, which widens the key rather than narrowing it, so it is a decision
the caller has to make explicitly. Unknown aliases are refused with
`MODEL_NOT_FOUND` and the known list in `details.known_models`, because a key
naming a model that does not exist reaches nothing and says nothing until
somebody tries to use it.

A manager is held to the same bar as at issue: only models they could call
themselves, and only for keys belonging to their own workspaces. A revoked key
cannot be amended — revocation is meant to be final, not a detour.

The call has to come from a console session or a key with no limit of its own:
a limited key cannot lift the list written on itself, nor push its own expiry
out ([Keys that carry a limit](#keys-that-carry-a-limit)). Only `days` and
`models` can be changed here — the access groups and the workspace a key was
issued with are fixed.

Both fields are also subject to [Who may decide a key](#who-may-decide-a-key):
an administrator's key is changed by an administrator only, and a manager's key
that carries their rights — or would after the change — by an administrator or
that manager. Extending the expiry is held to the same rule as lifting the list,
because bringing an expired key back hands the same rights out again.

### `GET /admin/users/{id}/quota`

A person's requests are not all bound by one rule. A request is governed by the
most specific policy for *(person, the workspace its key was issued for, the
model it names)*, and counted in that policy's own counter — so the view lists
every such policy, not one.

```json
{ "user_id": "…", "source": "default", "window": "day",
  "limits": { "max_requests": 500, … }, "used": { "requests": 3, … },
  "policies": [
    { "source": "default", "policy_id": "", "counter": "user:<id>", "window": "day",
      "applies_to": { "workspace_id": null, "model_alias": null,
                      "access_group_id": null, "api_key_id": null },
      "limits": { … }, "used": { "requests": 3, … }, "percent": 1, "exhausted": false },
    { "source": "workspace", "policy_id": "…", "policy_name": "CS101: 1 request a month",
      "counter": "user:<id>:ws:<workspace>", "window": "month",
      "applies_to": { "workspace_id": "…", "workspace_code": "CS101", … },
      "used": { "requests": 1, … }, "percent": 100, "exhausted": true } ],
  "not_bound": [
    { "policy_id": "…", "workspace_code": "CS101",
      "keys": [ { "id": "…", "name": "laptop", "key_prefix": "lg_sk_ab12" } ],
      "reason": "A workspace policy binds only requests made with a key issued for that workspace. …" } ] }
```

* The top-level `source` … `used` fields are the first entry of `policies`: the
  limit that applies with no workspace and no particular model. They are what
  this endpoint has always returned.
* `policies` adds the policy of each workspace one of the person's live keys was
  issued for, each policy aimed at a model or a bundle, and each ceiling on one
  of their keys (`source: "key"`). `exhausted` means a request under that policy
  is being refused now.
* `not_bound` names workspace policies that do **not** reach some of this
  person's keys, although they are a member: a workspace policy binds a key
  issued for that workspace, not membership. A policy that appears only here
  limits none of their requests.

`GET /admin/usage/quota` returns the same `policies` and `not_bound` per person.

### `POST /admin/users/{id}/quota/reset`

Optional body: `{ "include_keys": true }`.

```json
{ "user_id": "…", "window": "day",
  "cleared": { "requests": 151, "input_tokens": 1630767, "output_tokens": 11033, "images": 2 },
  "usage": { "window": "day", "limits": { … }, "used": { "requests": 0, … } },
  "counters_cleared": [
    { "counter": "user:<id>", "window": "day", "source": "default", "used": { "requests": 151, … } },
    { "counter": "user:<id>:ws:<workspace>", "window": "month", "source": "workspace",
      "policy_name": "CS101: 1 request a month", "used": { "requests": 1, … } } ],
  "not_cleared": [
    { "counter": "key:<key id>", "window": "day", "source": "key", "exhausted": true,
      "reason": "A ceiling on one API key is not part of the person's own allowance. …" } ] }
```

Zeroes **every counter that binds this person's requests** — one per entry of
`policies` above — and the per-minute counter of each where a rate limit is in
force. `counters_cleared` lists each with what it held (`used`); `window`,
`cleared` and `usage` are kept for older clients and describe the first of them.

Ceilings on individual API keys are left alone unless `include_keys` is true: a
trial key capped at 50 requests should not get 50 more because its owner's
allowance was handed back. They are always reported in `not_cleared`, with
`exhausted`, because a full ceiling still refuses that person through that key.
Admin only.

**Usage records are a separate ledger and are not touched** — `/admin/usage/*`
reports the same figures afterwards. What was cleared is written to the audit
log, so the reset is a visible decision rather than a way to hand out unmetered
access quietly.

With Redis configured, both stores are cleared. Clearing only Redis achieves
nothing lasting: the next read misses, concludes an earlier outage may have left
counts in the database, and reseeds from there. If Redis cannot be reached the
call fails with `UPSTREAM_ERROR` rather than reporting a success it did not
deliver — the database side is still cleared, so retrying once Redis is back
completes the job.

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

`avg_latency_ms` and `avg_ttft_ms` are plain averages over **every** row of the
model in the window — failed requests and answers served from the response cache
included — so they flatter a model that fails fast or is cached. Use
[`GET /admin/usage/latency`](#get-adminusagelatency) for how long requests
actually took. `workspace_id` narrows `by_model`; the `errors` list is **not**
narrowed by it.

No prompt, response, or image content appears here or in any other response —
the schema has no column for it (PRD §11).

### `GET /admin/usage/latency`

How slow the slow requests are: p50 / p95 / p99 and the slowest request seen,
for total latency and for time to first token, per model and per machine. An
average hides exactly the requests people complain about — 95 requests at 2 s
and 5 stuck at 90 s average 6.4 s, which nobody experienced.

Query: `days` (default 7, 1–365), `workspace_id`. Manager or admin; a manager
sees the requests of people in their own workspaces, the same scoping as
`/admin/usage/summary`.

```json
{
  "window_days": 7,
  "method": "nearest-rank",
  "min_samples": { "p50": 20, "p95": 20, "p99": 100 },
  "sample_cap": 10000,
  "group_cap": 50,
  "by_model": [{
    "model": "coding",
    "requests": 180, "errors": 6, "aborted": 9, "cache_hits": 12,
    "latency": { "population": 153, "samples": 153, "capped": false, "sampled_since": null,
                 "p50_ms": 2100, "p95_ms": 8400, "p99_ms": 31000, "max_ms": 90210 },
    "ttft":    { "population": 61, "samples": 61, "capped": false, "sampled_since": null,
                 "p50_ms": 310, "p95_ms": 1900, "p99_ms": null, "max_ms": 4200 },
    "streams_without_first_token": 2
  }],
  "by_model_truncated": false,
  "by_endpoint": [{ "endpoint": "spark-1", "model": "coding", "requests": 180, "…": "…" }],
  "by_endpoint_truncated": false
}
```

**Which rows are measured.** The defaults are chosen so the report cannot look
better than the system is:

| Measure | Rows counted |
|---|---|
| `latency` | `status = success` and answered by a backend, streaming or not — request received to last byte |
| `ttft` | streamed requests whose first token arrived, however the stream ended. A non-streaming request has no TTFT |
| neither | answers from the response cache (`cache_hit`). They take about a millisecond and no machine ran |

Everything left out is counted beside the figures instead of hidden: `errors`,
`aborted`, `cache_hits`, and `streams_without_first_token` — streams that ended
without a first token ever arriving, which have no TTFT to report and would
otherwise make a model that hangs look fast.

**Per measure:**

| Field | Meaning |
|---|---|
| `population` | Rows in the window that qualify for this measure |
| `samples` | Rows the percentiles were computed from. Equal to `population` unless `capped` |
| `capped`, `sampled_since` | `true` when the group had more than `sample_cap` qualifying rows: the newest `sample_cap` were used, and `sampled_since` is the timestamp of the oldest of them. The figures are then exact for that shorter period, not an estimate for the window asked for |
| `p50_ms`, `p95_ms`, `p99_ms` | `null` when `samples` is below `min_samples` for that percentile — **too few samples is no number, not zero**. Below 20 a "p95" is one slow request with a statistical name |
| `max_ms` | The slowest request in the sample — a fact, reported at any sample size; `null` only when there are no samples |

Percentiles are **nearest-rank**: the value at position ⌈p·n/100⌉ of the sorted
samples. The result is always a request that happened, never a value
interpolated between two, and it is the definition PostgreSQL calls
`percentile_disc`.

**Groups.** `by_model` is one entry per alias; `by_endpoint` is one per
(machine, alias) pair — a machine serving two models is two entries, so a slow
model is not hidden behind a fast one. The alias is the one on the usage row,
which is the alias the caller asked for: a request that a routing rule moved to
another model still counts under the name that was requested. Both are ordered by request count and
limited to `group_cap` groups; `*_truncated` says when groups were left out.

`by_endpoint` is for administrators. A manager gets `"by_endpoint": null` — not
an empty list, which would read as "no traffic" — because machine names stay
behind the admin role everywhere else too.

Cache hits recorded before the `cache_hit` column existed carry no mark and
cannot be separated after the fact; they are counted as backend answers.

**Next to `/metrics`.** The Prometheus histograms are still there and are still
what alerts are built on. This report is for a site that has the console and no
Grafana, works from the filtered rows described above rather than fixed
buckets, and can be narrowed to one workspace.

**Cost.** Measured on SQLite with 1,000,000 synthetic rows on a development
machine: about 1.1–1.2 s for a 7-day window holding 233,000 rows, 2.7–4.1 s
across all million, peak memory 2.2 MB. Not measured on PostgreSQL.

---

## Health and metrics

| Path | Auth | Purpose |
|---|---|---|
| `GET /healthz` | none | Liveness. Always 200 while the process runs |
| `GET /readyz` | none | Readiness. 503 until the registry is loaded, the DB answers, and ≥1 backend is healthy |
| `GET /metrics` | network-restricted | Prometheus |
| `GET /v1/health/endpoints` | admin | Per-endpoint health, in-flight, failure counts. `in_flight` is the count on the **backend**; `shares_slots_with` lists the other `alias:endpoint` entries on the same server and upstream model, which show the same number |
| `POST /v1/health/probe` | admin | Probe every backend immediately |

"admin" here means what it means under [Admin endpoints](#admin-endpoints): a
console session or an administrator's key with no limit of its own
([Keys that carry a limit](#keys-that-carry-a-limit)).

```json
{ "ready": true, "database": "ok",
  "models_loaded": 3, "endpoints_healthy": 2, "endpoints_total": 3,
  "registry_errors": [] }
```
