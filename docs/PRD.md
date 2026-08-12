# LiteGate — Product Requirements Document

| | |
|---|---|
| **Version** | 1.4 (LiteGate — sector-neutral) |
| **Status** | Approved for implementation |
| **Supersedes** | PRD v1.0–v1.3 (published as "EduLLM Gateway") |
| **Last updated** | 2026-08-12 |
| **Reference implementation** | this repository |
| **Author** | neronain — [facebook.com/neronain.minidev](https://www.facebook.com/neronain.minidev) |

> **What changed in v1.3.** v1.2 and its addendum described the system in
> fragments and contradicted themselves in several places. v1.3 merges them into
> one specification, resolves every conflict (§21 Decision log), and adds what
> was missing for a team to actually build from it: a single canonical schema,
> a complete API contract, a full error taxonomy, DDL, algorithms in pseudocode,
> per-requirement acceptance criteria, and a rollout plan.

---

## 1. Summary

LiteGate is a self-hosted API gateway that puts an organisation's own GPU
inference (vLLM / llama.cpp / Ollama / SGLang) behind the standard OpenAI and
Anthropic APIs, under its own identity, policy and quota — without exposing a
raw model server to the network.

**Who it is for.** Any group sharing GPUs they control: a small company, a
school or university department, a research group, an agency serving its own
clients. Education was the first deployment and remains a first-class use case,
but nothing in the design is specific to it — the vocabulary is *member*,
*workspace* and *manager*, which a class, a team and a client project all map
onto without translation.

**The one-sentence architecture:** the gateway owns *identity, permission,
capability, quota, routing, usage and protocol*; the model server owns
*inference, tokenizer, vision encoder, tool parser, KV cache and GPU*. Every
requirement below preserves that split, which is what keeps the gateway small
enough to be maintained by a small team even as vision and agentic workloads are
added.

### 1.1 Problem

| Problem | Consequence today |
|---|---|
| Model servers have no concept of a user | Cannot attribute cost, cannot enforce fair use |
| Repository names leak into member-facing config | Swapping a model breaks every member's setup |
| Capability differences are invisible | A member sends an image to a text-only model and gets an opaque backend 500 |
| No quota | One runaway script starves a whole class |
| No usage record | No basis for capacity planning or grant reporting |
| Every client speaks a different API | Claude Code cannot use an OpenAI-only backend |

### 1.2 Goals

- **G1** — One stable endpoint and one API key per member for every model.
- **G2** — Members address models by **purpose alias** (`coding`, `vision`), never by repository name; admins can re-point an alias with zero member-side change.
- **G3** — Capability-aware validation: an unsupported request fails fast at the gateway with an actionable message, never at the backend.
- **G4** — Multimodal (text + image) as a first-class P0 path, not a retrofit.
- **G5** — Enforceable per-member, per-workspace quota covering text, visual and output tokens.
- **G6** — Works unmodified with Claude Code, the OpenAI SDK, and plain `curl`.
- **G7** — Privacy by default: no prompt, response, or image is ever written to disk.

### 1.3 Non-goals (explicitly out of scope for v1.3)

| Not doing | Why | Revisit |
|---|---|---|
| Model inference, tokenization, vision encoding | Belongs to the model server (§13 of v1.2) | never |
| Image resize / OCR / conversion | Same | never |
| AI-based automatic model selection (`model=auto`) | Adds a classifier, a failure mode, and unpredictable cost | P3, §20 |
| Fine-tuning, training, model hosting | Different product | — |
| A full LMS | Integrate via API instead | — |
| Prompt / response storage for grading | Requires a separate privacy review and consent flow | see §11 |

### 1.4 Personas

| Persona | Needs | Success looks like |
|---|---|---|
| **Member** — the person doing the work (developer, student, analyst) | Something that "just works" in Python or Claude Code | Pastes a base URL + key, and it runs. Never sees a repository name. |
| **Manager** — owns a workspace (team lead, instructor, project owner) | Control and visibility for their group | Enables 2 models for the workspace, sets a quota, sees who is near the limit |
| **Administrator / GPU ops** | Add and swap models safely | Adds a model by writing one YAML file, runs the test suite, sees READY |
| **Power user** (researcher, senior engineer) | Higher limits, agentic workloads | Same API, different quota policy |

---

## 2. Architecture

```
                              STUDENTS
             ┌──────────────────┼──────────────────┐
             ▼                  ▼                  ▼
      Python / OpenAI SDK   Web / App        Claude Code
             │                  │                  │
             └──────────────────┼──────────────────┘
                                │  HTTPS
                                ▼
             ┌────────────────────────────────────────┐
             │            LiteGate              │
             │                                        │
             │  1. Authentication      (§7)           │
             │  2. Workspace policy       (§7)           │
             │  3. Capability registry (§4)           │
             │  4. Modality validation (§5, §6)       │
             │  5. Quota               (§8)           │
             │  6. Routing + health    (§9)           │
             │  7. Protocol translation(§9.4)         │
             │  8. Usage accounting    (§8.4)         │
             └───────────────────┬────────────────────┘
                                 │  OpenAI / Anthropic
          ┌──────────────────────┼──────────────────────┐
          ▼                      ▼                      ▼
       DGX #1                 DGX #2                 DGX #3
        vLLM                   vLLM                   vLLM
          │                      │                      │
    Muse Glimmer 30B       Gemma 4 31B IT        Qwen3 Coder Next
    text+image, tools      text+image,           text, tools,
    agent                  reasoning, tools      agent, 256K
```

### 2.1 Responsibility split (invariant)

| Gateway owns | Model server owns |
|---|---|
| Identity, permission, capability, quota | Inference, tokenizer |
| Routing, health, protocol translation | Vision encoder, tool parser |
| Usage accounting, audit | KV cache, GPU scheduling |

**Rule:** if a proposed feature requires the gateway to interpret model *content*
(decode an image, tokenize text, classify a prompt), it belongs on the right-hand
side. This is the test that keeps the gateway small.

---

## 3. Glossary

| Term | Meaning |
|---|---|
| **Alias** | The public model identifier a member uses (`coding`). Stable across model swaps. |
| **Upstream model** | The real repository path (`ucbye/Qwen3-Coder-Next-NVFP4-GB10`). Admin-visible only. |
| **Capability** | A boolean feature flag on a model (`vision`, `tools`, `agentic`). |
| **Modality** | A content type (`text`, `image`, `audio`, `video`). |
| **Purpose** | A catalogue grouping (`general`, `coding`, `vision`). A model may have several. |
| **Endpoint** | One serving process — one vLLM instance on one node. |
| **Protocol** | An API dialect: `openai` or `anthropic`. |
| **Visual tokens** | The prompt tokens attributable to images. |

---

## 4. Capability registry

### FR-31 — Model registry is a capability registry, not a model list · **P0**

The gateway must not assume models are interchangeable. Each model declares what
it can do; the gateway core never needs changing to add a model.

**There is no `type` enum.** `type = chat | vision | coding` is rejected as a
design because one model is routinely several of these at once. Capabilities are
independent booleans:

```
chat ✓   vision ✓   coding ✓   tools ✓   reasoning ✓   agentic ✓
audio ✗  embedding ✗
```

### 4.1 Canonical schema

One YAML file per model in `config/models/<alias>.yaml`. This is the **single
canonical form** — v1.2 showed three mutually inconsistent shapes (§21 D1).

```yaml
apiVersion: litegate.dev/v1
kind: Model

metadata:
  alias: coding                    # public id; ^[a-z0-9][a-z0-9._-]{1,62}$
  display_name: Local Coder        # shown in the catalogue
  description: Agentic coding model with tool calling and long context.
  visibility: member              # member | manager | admin
  tags: [coding, agent]

spec:
  upstream_model: ucbye/Qwen3-Coder-Next-NVFP4-GB10   # ADMIN-ONLY (§10)

  purpose: [coding, agent]         # catalogue grouping; may be several

  limits:
    context_tokens: 262144
    max_output_tokens: 16384

  modalities:
    input:  [text]                 # text | image | audio | video
    output: [text]

  capabilities:
    chat: true
    vision: false                  # -> images to this alias are 400, not a backend error
    coding: true
    tools: true
    streaming: true
    reasoning: false
    agentic: true
    audio: false
    embedding: false

  protocols:                       # which GATEWAY API surfaces this alias exposes
    openai: true
    anthropic: true

  agent_clients:                   # tested compatibility, never inferred (§9.5)
    claude_code: { enabled: true,  tested: true,  notes: "MODEL-009 passed 2026-08-12" }
    qwen_code:   { enabled: true,  tested: true }
    cline:       { enabled: false, tested: false }

  vision_policy:                   # optional; overrides gateway.yaml for this model
    max_images_per_request: 4

  endpoints:                       # what the BACKEND supports (§9.2)
    - name: dgx03
      server_type: vllm            # vllm | ollama | sglang | llama.cpp | openai_compatible
      base_url: http://dgx03:8000
      api_key_env: DGX03_API_KEY   # env var name; the secret is never in YAML
      priority: 100                # higher wins
      weight: 1                    # load share within a priority tier
      max_concurrency: 16
      health_path: /health
      protocols:  { openai: true, anthropic: false }
      modalities: { text: true, image: false, audio: false, video: false }
      enabled: true

  enabled: true
```

### 4.2 Load-time validation

The registry is rejected at load, not at request time, when it is internally
contradictory. A failed reload keeps the previous good snapshot and surfaces the
error on `/readyz` and in the log.

| Rule | Rationale |
|---|---|
| `capabilities.vision` ⟺ `image ∈ modalities.input` | The two fields cannot disagree |
| `capabilities.audio` ⇒ `audio ∈ modalities.input` | Same |
| ≥ 1 enabled endpoint | A model with no backend is not a model |
| `protocols.openai` ⇒ some endpoint speaks OpenAI | No translation path exists in that direction |
| `protocols.anthropic` ⇒ some endpoint speaks Anthropic **or** OpenAI | Anthropic→OpenAI translation exists (§9.4) |
| `capabilities.vision` ⇒ some endpoint has `modalities.image` | A vision model on text-only backends cannot serve vision |
| Endpoint names unique per model | Health and usage are keyed by `alias:endpoint` |
| Alias unique across the registry | It is the public identifier |

### FR-39 — Capability auto-detection · **P2**

The admin UI may probe a backend and pre-fill the capability checkboxes.
**Detection is a suggestion; the admin must confirm before save.** The gateway
must never silently change a declared capability from a probe result.

---

## 5. Multimodal requests

### FR-30 — Text + image request · **P0**

`POST /v1/chat/completions` accepts OpenAI content blocks:

```json
{
  "model": "gemma-vision",
  "messages": [{
    "role": "user",
    "content": [
      { "type": "text", "text": "อธิบายภาพนี้" },
      { "type": "image_url", "image_url": { "url": "data:image/png;base64,iVBORw0..." } }
    ]
  }]
}
```

`POST /v1/messages` accepts the equivalent Anthropic blocks
(`{"type":"image","source":{"type":"base64","media_type":...,"data":...}}`),
including images nested inside a `tool_result` — a screenshot returned by an
agent's tool is still an image and still consumes image quota.

| Supported at P0 | Deferred |
|---|---|
| Text | Uploaded image (multipart) — P1 |
| Base64 data URL image | PDF — P1 |
| Remote image URL (**off by default**, §6.3) | Audio — P2 |
| | Video — P3 |

### FR-33 — Multimodal streaming · **P0**

Streaming works identically for multimodal requests. SSE chunks are relayed as
they arrive; no buffering of a complete response.

### 5.1 What the gateway does *not* do — FR-13 · **P0**

Per v1.2 §13, in MVP the gateway must **not** resize, re-encode, OCR, run vision
encoding, convert formats, or perform object detection. Original bytes are
forwarded untouched. Verified by test: the base64 payload the backend receives is
byte-identical to what the member sent.

```
Client ──original multimodal request──▶ Gateway ──unchanged──▶ vLLM ──▶ Vision model
```

---

## 6. Validation and policy

### FR-32 — Capability validation · **P0**

Two gates, both mandatory (v1.2 §14):

```
request ──▶ does the MODEL declare it? ──▶ does the ENDPOINT serve it? ──▶ route
```

A request for `model=coding` (`vision: false`) carrying an image returns
**HTTP 400** *before any backend call*:

```json
{
  "error": {
    "code": "MODEL_CAPABILITY_NOT_SUPPORTED",
    "message": "Model 'coding' does not support image input. Choose a model whose badge shows 'Image', for example a vision model.",
    "type": "invalid_request_error",
    "param": null,
    "details": { "model": "coding", "required_capability": "vision" },
    "request_id": "…"
  }
}
```

**Acceptance:** the backend access log shows zero requests for this case.

### FR-34/FR-35 — Image size and type validation · **P0**

Configured in `gateway.yaml`, overridable per model:

```yaml
vision_policy:
  max_images_per_request: 4
  max_image_size_mb: 10
  allowed_types: [image/jpeg, image/png, image/webp]
  remote_image_url:
    enabled: false
    allowed_hosts: []
```

Enforcement rules:

1. **Size is checked before decoding.** The base64 payload length gives the
   decoded size to within 3 bytes; an oversized image is rejected without ever
   allocating the decoded buffer.
2. **Type is determined by magic bytes, not by the client's label.** A GIF
   labelled `image/png` is detected as `image/gif` and rejected. Trusting the
   declared MIME would make `allowed_types` unenforceable.
3. Image count is checked across the whole request, including nested tool results.

### 6.3 Remote image URLs — default off

`remote_image_url.enabled: false` is the default and the recommendation for an
internal deployment. With it enabled the gateway becomes a URL fetcher that an
authenticated member could aim at internal addresses (cloud metadata endpoints,
internal admin panels) — a server-side request forgery primitive. When a site
must enable it, `allowed_hosts` is mandatory.

Members send base64 or (P1) upload.

---

## 7. Identity, permission and access

### 7.1 API keys — FR-01 · **P0**

Format `edu_sk_<43 url-safe base64 chars>` (256 bits of entropy).

- Stored as **HMAC-SHA256(pepper, key)**; the plaintext exists only in the
  response that created it. Because the secret is high-entropy random rather than
  a human password, one HMAC is the correct primitive — a slow KDF would only add
  latency to every request without adding meaningful resistance.
- The pepper lives in `GW_API_KEY_PEPPER`, never in the database. Rotating it
  invalidates every key by design.
- Accepted as `Authorization: Bearer <key>` (OpenAI) **or** `x-api-key: <key>`
  (Anthropic), so both SDKs work unmodified.
- A key may be bound to a workspace; unknown vs. malformed keys return the identical
  message, giving no oracle for key probing.

### 7.2 Roles

| Role | Sees | Can |
|---|---|---|
| `member` | `visibility: member` models | Call permitted models; read own quota |
| `manager` | `member` + `manager` | The above, plus manage own workspace, issue member keys, read usage |
| `admin` | everything, including `upstream_model` | Everything, plus registry reload and quota policy |

### FR-19 — Workspace-scoped model permission · **P0**

A key bound to a workspace may only call aliases that workspace enables. An unlisted
alias returns **403 `MODEL_NOT_PERMITTED`**; the message names the alias but
never reveals whether it exists elsewhere.

---

## 8. Quota and usage

### FR-20..24 — Quota enforcement · **P0**

Quota is tracked over four dimensions, each independently limitable:

```
requests · text_input_tokens · visual_input_tokens · output_tokens · images
```

**Policy resolution** — most specific match wins:

```
user + model  >  user  >  workspace + model  >  workspace  >  global  >  gateway.yaml default
```

**Windows:** `day` | `month` | `term`. The `term` window follows the Thai
academic calendar (Aug–Dec, Jan–May, Jun–Jul summer).

### 8.1 Enforcement model — check-then-record

```
check(counters vs limits) ──▶ forward ──▶ record(actual usage)
```

This is deliberately *not* reserve-then-settle. Reserving would require holding a
reservation across a multi-minute generation, and refunding on every failure
path. The cost is a bounded overrun: under a concurrent burst a member can
exceed the limit by at most (in-flight requests × per-request cost), which
self-corrects on the next check. Accepted as **NFR-Q1**.

Counters live in Redis when configured (shared across workers) and fall back to
the database otherwise (correct for single-worker deployments).

### FR-37 — Visual token accounting · **P1**

The gateway runs no tokenizer (§2.1), so accounting has two tiers, and **every
usage row records which tier produced it** so reports never silently mix measured
and estimated numbers:

| Tier | When | Method |
|---|---|---|
| `upstream` | Backend reported usage | `prompt_tokens` is authoritative; the visual estimate is subtracted to give the text portion |
| `estimated` | Backend reported nothing | Text from character count (3.2 chars/token for mixed Thai/English), visual from image geometry |

**Visual estimate** — header-only, no image decoding:

```
fit to 2048 box, then short side to 768
tiles  = ceil(w/512) × ceil(h/512)
tokens = 85 + 170 × tiles
```

Unknown geometry (e.g. WEBP) → 850. Remote URL (never fetched) → 1105.

### FR-38 — Usage record · **P1**

One row per completed request, **metadata only**:

```
request_id · ts · user_id · workspace_id · api_key_id
model_alias · endpoint_name · protocol · request_modality · stream
text_input_tokens · visual_input_tokens · output_tokens · total_tokens
image_count · token_accounting
latency_ms · ttft_ms · status · http_status · error_code · client_agent
```

There is **no column** for prompt, response, or image (§11). Writes are buffered
and flushed on a 2-second interval so a slow database never adds latency to an
inference response.

---

## 9. Routing

### FR-15 — Capability-aware routing · **P0**

Routing is not `alias → endpoint`. It is:

```
Request
  ├─▶ Authenticate                      → 401
  ├─▶ Workspace policy                     → 403
  ├─▶ Resolve alias                     → 404
  ├─▶ Parse content blocks              → 400
  ├─▶ Model capability gate             → 400  ← never reaches the backend
  ├─▶ Vision policy (count/size/MIME)   → 400/413/415
  ├─▶ Context budget                    → 400
  ├─▶ Quota                             → 429
  ├─▶ Select compatible healthy endpoint→ 503
  ├─▶ Forward (stream or unary)         → 502/504 on failure
  └─▶ Record usage + quota                (always, including on error)
```

### 9.2 Endpoint selection

```
candidates = endpoints where
    enabled
    ∧ protocols[requested_protocol]
    ∧ modalities ⊇ request modalities
    ∧ healthy                (if none healthy: use all — a stale probe must not
                              take a whole model offline; log a warning)
    ∧ in_flight < max_concurrency          (else 429 CONCURRENCY_LIMIT_EXCEEDED)

pick highest priority tier → least in-flight → weighted random tie-break
```

### 9.3 Health with hysteresis — FR-16 · **P0**

Active probe of `health_path` every `health_check_interval_seconds`. An endpoint
is ejected after `unhealthy_threshold` consecutive failures and restored after
`healthy_threshold` consecutive successes. Hysteresis stops a single blip from
flapping an endpoint out of rotation.

### FR-25 — Protocol translation · **P0**

| Client wants | Backend speaks | Behaviour |
|---|---|---|
| OpenAI | OpenAI | passthrough |
| Anthropic | Anthropic | passthrough (model name masked to the alias) |
| Anthropic | OpenAI | **translate both directions**, including the SSE event sequence |
| OpenAI | Anthropic only | not supported → `PROTOCOL_NOT_SUPPORTED` |

Anthropic→OpenAI translation covers: system prompts, text and image blocks, tool
definitions, `tool_use` / `tool_result`, `tool_choice`, stop reasons, usage, and
the streaming sequence `message_start → content_block_start → content_block_delta*
→ content_block_stop → message_delta → message_stop`.

Anthropic-only features with no OpenAI equivalent (extended thinking, citations,
cache hints) are **dropped on the way out and never fabricated on the way back**.

### 9.5 No name-based inference — FR-26 · **P0**

The gateway must never conclude "Qwen ⇒ coding" or "Gemma ⇒ general". Client
compatibility comes from `agent_clients` — which is populated by the test suite
(§14), not by a human guess and not by a regex on the model name.

### FR-40 — Automatic model selection · **P3 — not in MVP**

`model=auto` with a prompt classifier is explicitly deferred. It adds a
classification step, an unpredictable cost profile, and a failure mode members
cannot diagnose. Members choose an alias.

---

## 10. Member-facing model UX

### FR-27 — Repository names are never member-visible · **P0**

| Member sees | Admin also sees |
|---|---|
| `coding` | `ucbye/Qwen3-Coder-Next-NVFP4-GB10` |
| `gemma-vision` | `google/gemma-4-31B-it` |
| `muse-local` | `meta-models/Muse-Glimmer-30B` |

This is enforced by a test asserting that no member-visible response body
contains any configured `upstream_model` string. It is what makes G2 real: an
admin can re-point `coding` at a new model and no member changes anything.

The catalogue groups by purpose with plain-language badges:

```
General AI
────────────────────────────
Muse Local
Text · Image · Tools · Agent
128K Context

Vision AI
────────────────────────────
Gemma Vision
Text · Image · Reasoning
256K Context

Coding AI
────────────────────────────
Local Coder
Text · Code · Tools · Agent
256K Context          [Claude Code Ready]
```

---

## 11. Privacy and security

### FR-28 — No-store default · **P0**

```yaml
privacy:
  store_prompts:   false
  store_responses: false
  store_images:    false
```

Base64 images pass through gateway memory and are discarded when the request
ends. **Nothing is written to disk.** This is enforced structurally: the schema
has no column capable of holding this content, so enabling storage requires a
schema change and a review, not a config flip.

Relevant to Thailand's PDPA: member prompts may contain personal data, and the
lawful basis for retaining them has not been established. The default is
therefore no-collection.

### 11.1 Security requirements

| ID | Requirement |
|---|---|
| SEC-1 | TLS terminated at the reverse proxy; the gateway never binds to a public interface directly |
| SEC-2 | API keys stored as HMAC-SHA256 with a server-side pepper; never logged |
| SEC-3 | The member's gateway key is stripped before forwarding; the backend receives its own credential |
| SEC-4 | Backend credentials come from env vars named in YAML — never the values in YAML |
| SEC-5 | `/admin/*` and `/metrics` restricted to the management network at the proxy |
| SEC-6 | Remote image fetch off by default (§6.3, SSRF) |
| SEC-7 | Upstream error text is truncated and wrapped; backend internals are not relayed verbatim to members |
| SEC-8 | Container runs as non-root, read-only rootfs, all capabilities dropped |
| SEC-9 | Every admin mutation writes an `audit_logs` row (actor, action, target, IP) |
| SEC-10 | Registry mounted read-only into the container |

---

## 12. Data model

```sql
users(id, external_id UNIQUE, email, display_name, role, status, created_at, updated_at)
workspaces(id, code UNIQUE, name, term, status, …)
memberships(id, user_id→users, workspace_id→workspaces, role, UNIQUE(user_id,workspace_id))

api_keys(id, user_id→users, workspace_id→workspaces NULL, name,
         key_prefix, key_hash UNIQUE, scopes JSON,
         expires_at, revoked_at, last_used_at, …)

-- Projection of config/models/*.yaml. YAML remains the source of truth.
models(id, alias UNIQUE, display_name, upstream_model, purpose JSON,
       context_length, max_output_tokens,
       supports_text, supports_image, supports_audio, supports_video,
       supports_streaming, supports_tools, supports_reasoning, supports_agentic,
       supports_openai, supports_anthropic, claude_code_compatible,
       visibility, enabled, …)

workspace_models(id, workspace_id→workspaces, model_alias, enabled,
              UNIQUE(workspace_id, model_alias))

model_compatibility(id, model_id→models, feature, status, tested_at,
                    test_version, latency_ms, notes,
                    UNIQUE(model_id, feature))

quota_policies(id, scope, workspace_id, user_id, model_alias, window,
               max_requests, max_input_tokens, max_output_tokens, max_images, enabled)

quota_counters(id, subject_key, window_start, window_end,
               requests, text_input_tokens, visual_input_tokens,
               output_tokens, images, updated_at,
               UNIQUE(subject_key, window_start))

usage_logs(id, request_id UNIQUE, ts, user_id, workspace_id, api_key_id,
           model_alias, endpoint_name, protocol, request_modality, stream,
           text_input_tokens, visual_input_tokens, output_tokens, total_tokens,
           image_count, token_accounting,
           latency_ms, ttft_ms, status, http_status, error_code,
           client_agent, cost_units)
           -- NO prompt / response / image columns, by design (§11)

audit_logs(id, ts, actor_user_id, action, target_type, target_id, payload JSON, ip)
```

Indexes: `usage_logs(ts,user_id)`, `(ts,workspace_id)`, `(ts,model_alias)`,
`(error_code)`; `quota_counters(window_start,window_end)`.

**Retention:** `usage_logs` 24 months (capacity planning and grant reporting);
`audit_logs` 36 months; `quota_counters` may be pruned once the window closes.

---

## 13. API contract

Full request/response detail in [API.md](API.md). Summary:

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/v1/models` | any | OpenAI-shaped catalogue (alias only for members) |
| GET | `/v1/catalog` | any | Member catalogue grouped by purpose |
| GET | `/v1/me` | any | Identity + remaining quota |
| POST | `/v1/chat/completions` | any | OpenAI chat, streaming and multimodal |
| POST | `/v1/messages` | any | Anthropic Messages (Claude Code) |
| POST | `/v1/messages/count_tokens` | any | Pre-flight estimate |
| GET | `/healthz` `/readyz` `/metrics` | none / none / network-restricted | Ops |
| GET/POST | `/admin/users` `/workspaces` `/api-keys` `/quota-policies` | manager / admin | Admin plane |
| GET | `/admin/models` | admin | Registry incl. upstream names + health |
| POST | `/admin/registry/reload` | admin | Hot reload |
| GET/POST | `/admin/models/{alias}/compatibility` | admin | Test-suite results |
| GET | `/admin/usage/summary` `/usage/top-users` | manager | Reporting |

### 13.1 Error taxonomy

Every gateway-originated rejection carries a stable machine code, so clients can
branch on `error.code` rather than parse prose. The envelope is OpenAI-shaped on
OpenAI routes and Anthropic-shaped on `/v1/messages`.

| Code | HTTP |
|---|---|
| `MISSING_API_KEY` · `INVALID_API_KEY` · `API_KEY_REVOKED` · `API_KEY_EXPIRED` | 401 |
| `ACCOUNT_DISABLED` · `INSUFFICIENT_SCOPE` · `MODEL_NOT_PERMITTED` | 403 |
| `MODEL_NOT_FOUND` · `MODEL_DISABLED` | 404 |
| `MODEL_CAPABILITY_NOT_SUPPORTED` · `PROTOCOL_NOT_SUPPORTED` · `INVALID_REQUEST` · `INVALID_CONTENT_BLOCK` · `TOO_MANY_IMAGES` · `REMOTE_IMAGE_URL_DISABLED` · `CONTEXT_LENGTH_EXCEEDED` | 400 |
| `IMAGE_TOO_LARGE` | 413 |
| `IMAGE_TYPE_NOT_ALLOWED` | 415 |
| `QUOTA_EXCEEDED` · `RATE_LIMIT_EXCEEDED` · `CONCURRENCY_LIMIT_EXCEEDED` | 429 + `Retry-After` |
| `UPSTREAM_ERROR` | 502 |
| `NO_HEALTHY_ENDPOINT` · `UPSTREAM_UNAVAILABLE` | 503 |
| `UPSTREAM_TIMEOUT` | 504 |
| `INTERNAL_ERROR` | 500 |

`404 MODEL_NOT_FOUND` includes `details.available_models`, so a typo is
self-correcting.

---

## 14. Model test suite

### FR-36 — Compatibility testing · **P1**

A model is `READY` because it was **measured**, never because of its name (§9.5).
`scripts/model_test_suite.py` runs these against a live gateway and posts each
result to `/admin/models/{alias}/compatibility`.

| ID | Test | Pass criterion |
|---|---|---|
| MODEL-001 | Basic chat | 200, non-empty content |
| MODEL-002 | Streaming | ≥ 2 SSE chunks; TTFT recorded |
| MODEL-003 | Long context | ~25% of the window accepted |
| MODEL-004 | Tool calling | `tool_calls` present with the right name |
| MODEL-005 | Multi-tool | ≥ 2 tool calls in one turn |
| MODEL-006 | Vision | 200 with an image; `visual_input_tokens > 0` |
| MODEL-007 | Vision + text | Correct answer about image content |
| MODEL-008 | Agent loop | Tool result fed back and accepted |
| MODEL-009 | Claude Code | `/v1/messages` returns a valid Anthropic message |
| MODEL-010 | Concurrent load | N parallel requests all 200 (or cleanly 429) |

Status roll-up: `READY` requires `chat` **and** `streaming` to pass. Any `fail`
⇒ `DEGRADED`. Untested features are `NOT TESTED`, never assumed to pass.

```
Gemma Vision
  Chat         PASS      Streaming   PASS
  Vision       PASS      Tools       PASS
  Claude Code  NOT TESTED
  Status       READY
```

---

## 15. Console assistant

A chat box wired to an LLM is a weekend project and it helps nobody: it answers
about HTTP 400 in general, not about *your* 400. What makes one worth shipping
is that it answers from this deployment's own state — the caller's quota, the
models they may actually use, what the backends were last measured to do. None
of that is knowable to a general model, and all of it is already in the gateway.

The assistant is therefore not a feature bolted on the side. It is a reader of
the same state the console renders, speaking through the same request pipeline
as every other caller.

### 15.1 Requirements

| ID | Requirement |
|---|---|
| FR-50 | Console assistant answers from this deployment's live state, not general knowledge |
| FR-51 | Assistant context is scoped to the caller's role — a member's assistant sees only their own quota and permitted models |
| FR-52 | Assistant requests go through the normal pipeline: capability gate, quota, routing, usage |
| FR-53 | The assistant is hidden, not broken, when no chat model is available to the caller |
| FR-54 | An administrator can change the assistant's model, and see why each candidate does or does not suit the role |

### 15.2 Why it is not a side door

The assistant reaches models through `run_chat()` — the same function that
serves `POST /v1/chat/completions`. It cannot use a model its caller could not
use directly, it spends the caller's quota rather than a hidden pool, and its
requests appear in usage like any other. An assistant that quietly bypassed the
gateway would be a hole in every guarantee in §7 and §8.

Context is built per request and scoped by role:

| Included | member | manager | admin |
|---|---|---|---|
| Own quota and usage | ✅ | ✅ | ✅ |
| Models the caller may use | ✅ | ✅ | ✅ |
| Backend health, last errors | — | — | ✅ |
| Registry load errors | — | — | ✅ |
| Upstream repository names | — | — | ✅ |

The last row matters: FR-27 keeps repository names away from members, and an
assistant that would recite them on request defeats it.

### 15.3 State is data, not instructions

The state block carries text from outside this system — model names from public
repositories, error strings from backend servers. Any of it could be written to
read like an instruction. The system prompt labels the block as data and says
that content inside it is to be reported, never obeyed.

### 15.4 Nothing is stored

Conversation history lives in the browser's `sessionStorage` and is gone when
the tab closes. Server-side the assistant keeps nothing, which is what §11's
no-store default requires. The cost is that history does not follow a user
between devices — the right trade for a tool whose whole subject is the state
of the machine in front of you.

### 15.5 Choosing the model

The assistant asks something unusual of a model. Its prompt is mostly *state* —
the catalogue, the caller's quota, backend health — and grows with the
deployment, while the answers it should give are short and are read in a small
panel beside whatever the operator was already doing.

None of that is visible from a model's name, and most is not visible from its
capability flags either. `app/core/assistant_fit.py` judges each candidate from
the registry entry and the compatibility record the test suite writes:

| Signal | Effect |
|---|---|
| No chat capability, or the suite could not get a reply | Blocker |
| Context below 16K | Blocker — the state block alone can reach ~12K |
| Context at or above 32K | Best score |
| Plain chat model | Strongly preferred |
| Reasoning model, `reasoning_separated` untested or failing | Penalised, with the `--reasoning-parser` fix named |
| Purpose `general`/`fast` | Preferred over a specialist |
| Backend currently unhealthy | Penalised, never disqualified — health changes by the minute |

`GET /admin/assistant` returns every candidate ranked with its reasons;
`PUT /admin/assistant` pins one or clears the pin. **The pin is refused if the
model cannot serve the role**, with the failing check named — accepting it would
produce a visibly broken chat box whose owner is the last to find out.

Candidates are ranked, not filtered. "Why can I not pick that one?" is a
question operators actually ask, and a model missing from a list answers it with
silence.

The same ranking drives the automatic choice, so the console never recommends
one model while quietly running another.

The pin lives in `gateway_settings`, not in the environment: four uvicorn
workers cannot share a variable, and a console setting that needs a file edit
and a restart is not a console setting. `GW_ASSISTANT_MODEL` remains the way to
set a deploy-time default.

### 15.6 Reasoning models

A model started without vLLM's `--reasoning-parser` puts its chain of thought in
`content` instead of `reasoning_content`, so the narration arrives mixed into
the answer. The console strips what it recognises, but stripping is guesswork:
the fix is the launch flag. The probe (§14) therefore detects the condition and
`build_advice()` reports it as `reasoning_not_separated` with the command to
correct it — the same treatment as a missing tool parser.

---

## 16. Non-functional requirements

| ID | Requirement | Target | How verified |
|---|---|---|---|
| NFR-P1 | Gateway overhead (non-streaming) | p95 < 50 ms above backend | `latency_ms` minus backend time |
| NFR-P2 | Added time-to-first-token | p95 < 100 ms | MODEL-002 `ttft_ms` |
| NFR-P3 | Throughput | ≥ 200 concurrent streams / instance | MODEL-010 at scale |
| NFR-P4 | Validation rejection | < 10 ms, zero backend calls | FR-32 test |
| NFR-A1 | Availability (gateway) | 99.5% in term time | `/readyz` monitoring |
| NFR-A2 | One backend down | Traffic shifts within 45 s (3 × 15 s) | Chaos test |
| NFR-A3 | Redis down | Falls back to DB counters, no request failures | Failure test |
| NFR-A4 | Bad registry edit | Previous snapshot retained; error on `/readyz` | Reload test |
| NFR-S1 | Image memory | Bounded by `max_images × max_size` per request | Load test |
| NFR-Q1 | Quota overrun under burst | ≤ in-flight × per-request cost | §8.1 |
| NFR-O1 | Add a model | One YAML file + reload, no restart, no code change | Ops drill |
| NFR-O2 | Deploy | Single command, < 10 min from clean host | DEPLOYMENT.md |

---

## 17. Functional requirement index

**P0 — MVP, must ship together**

| ID | Requirement |
|---|---|
| FR-01 | API-key authentication (Bearer + `x-api-key`) |
| FR-02 | Roles: member / manager / admin |
| FR-10 | User, workspace, membership management |
| FR-11 | API key issue / list / revoke |
| FR-15 | Capability-aware routing |
| FR-16 | Backend health with hysteresis |
| FR-19 | Workspace-scoped model permission |
| FR-20..24 | Quota policy, resolution, enforcement, windows, reporting |
| FR-25 | Anthropic ⇄ OpenAI protocol translation |
| FR-26 | Compatibility from testing, never from model names |
| FR-27 | Repository names never member-visible |
| FR-28 | No-store privacy default |
| **FR-30** | **Text + image request** |
| **FR-31** | **Model capability registry** |
| **FR-32** | **Capability validation** |
| **FR-33** | **Multimodal streaming** |
| **FR-34** | **Image size limit** |
| **FR-35** | **Image type validation** |
| FR-13 | Gateway performs no image processing |

**P1** — FR-36 Vision/compat test suite · FR-37 Visual token accounting ·
FR-38 Multimodal usage dashboard · FR-12 Uploaded image · FR-14 PDF input

**P2** — FR-39 Capability auto-detection · FR-17 Audio input · FR-18 Embeddings

**P3** — FR-40 Automatic model selection (`model=auto`) · FR-29 Video

**Console assistant** — FR-50 Grounded in live state · FR-51 Role-scoped context · FR-52 Same pipeline as any caller · FR-53 Hidden when no model is available · FR-54 Administrator-selectable model with a suitability check

---

## 18. Delivery plan

| Milestone | Contents | Exit criteria |
|---|---|---|
| **M1 — Core** (done) | Registry, capability validation, OpenAI surface, auth, quota, usage, routing, health | 48 tests green; FR-30..35 demonstrable |
| **M2 — Agent** (done) | Anthropic surface, protocol translation, Claude Code profile, test suite | MODEL-001..010 runnable end-to-end |
| **M3 — Pilot** | Deploy to staging, connect one real DGX, one workspace, ~30 members | 2 weeks with no P1 incident |
| **M4 — Production** | Postgres + Redis, TLS, monitoring, backups, runbook | NFR-A1 met for one full term |
| **M5 — P1 features** | Upload endpoint, PDF, richer dashboard, capability probe | Manager sign-off |

### 17.1 Task index (from v1.2 §22, with status)

| Task | Description | Status |
|---|---|---|
| GW-100 | Model capability schema | done |
| GW-101 | Model modality schema | done |
| GW-102 | Multimodal request parser | done |
| GW-103 | Image capability validation | done |
| GW-104 | Image MIME validation (magic-byte based) | done |
| GW-105 | Image size limiter (pre-decode) | done |
| GW-106 | Base64 image passthrough | done |
| GW-107 | OpenAI vision passthrough | done |
| GW-108 | Vision compatibility test (MODEL-006/007) | done |
| GW-109 | Visual token usage | done |
| GW-110 | Capability UI (badges, member + admin) | done |
| GW-111 | Backend capability matrix | done |
| GW-112 | Multimodal load test (MODEL-010) | done |
| GW-113 | Image upload endpoint (multipart) | P1 |
| GW-114 | PDF input | P1 |
| GW-115 | Capability auto-detection probe | P2 |

---

## 19. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Estimated visual tokens diverge from real billing | Quota unfair | `token_accounting` marks every row; prefer upstream numbers; recalibrate against measured usage each term |
| A backend reports usage inconsistently | Silent under-counting | `estimated` fallback; alert when the `estimated` share exceeds 20% |
| Base64 images inflate request bodies | Memory pressure | Pre-decode size check; proxy `client_max_body_size`; NFR-S1 |
| Model swap changes behaviour under a stable alias | Assignments break mid-term | Freeze aliases during a term; announce swaps; keep the old alias for one term |
| Anthropic translation drifts as Claude Code evolves | Claude Code breaks | MODEL-009 in CI; pin a known-good client version per term |
| A single DGX is a single point of failure | Model unavailable | Multiple endpoints per alias; hysteresis; `NO_HEALTHY_ENDPOINT` is explicit |

---

## 20. Open questions

| # | Question | Owner | Needed by |
|---|---|---|---|
| Q1 | Does the university IdP (SSO) integrate at M3, or do managers issue keys manually for the pilot? | Ops | M3 |
| Q2 | Term quota values — is 2M input tokens/day/member right for a 60-member class? | Manager | M3 |
| Q3 | Is per-model cost weighting (`cost_units`) required for internal chargeback? | Finance | M4 |
| Q4 | Retention: is 24 months of usage metadata acceptable to the privacy officer? | DPO | M4 |
| Q5 | Should managers see per-member *prompt counts* only, or is any content view needed for academic-integrity cases? (Would reverse §11.) | Faculty + DPO | M5 |

---

## 21. Decision log

Conflicts in v1.2 + addendum, and how v1.3 resolves them.

| # | Conflict in v1.2 | Resolution |
|---|---|---|
| **D1** | Three different YAML shapes for a model (`model:` root, nested keys varying by example) | One canonical `apiVersion/kind/metadata/spec` document (§4.1) |
| **D2** | `gemma-vision` had no `protocols` block while others did — an undefined default | `protocols` is required and explicit on every model |
| **D3** | `purpose` introduced in §5 but absent from every schema example in §1 | `spec.purpose` is a first-class required field |
| **D4** | §10 named fields `text_input_tokens`/`visual_input_tokens`; §19 named them `image_count`/`visual_input_tokens`/`request_modality` | Single field list (§8.4 / §12), all three present |
| **D5** | §1 declared `protocols.anthropic: false` for `muse-local` yet §7 casts it as `general-agent` | Protocol and purpose are independent; `muse-local` exposes OpenAI only. `/v1/messages` for it returns `PROTOCOL_NOT_SUPPORTED` |
| **D6** | Model-level vs endpoint-level protocol semantics were conflated | `spec.protocols` = gateway surfaces exposed; `endpoint.protocols` = what the backend speaks. Anthropic surface may be served over an OpenAI backend by translation |
| **D7** | "Validate size/policy" with no stated order — decode-then-check is a memory DoS | Size is checked **before** base64 decode (§6) |
| **D8** | MIME validation unspecified — trusting the client label makes `allowed_types` unenforceable | Type from magic bytes; declared type is advisory |
| **D9** | No error taxonomy beyond one example | Full code table with HTTP mapping (§13.1) |
| **D10** | Visual/text token split specified as a requirement with no method | Documented two-tier method + formula, with `token_accounting` provenance on every row (§8.3) |
| **D11** | Quota semantics under concurrency unspecified | Check-then-record, bounded overrun, recorded as NFR-Q1 (§8.1) |

---

## 22. Acceptance criteria (v1.3 sign-off)

The release is accepted when all of the following hold:

1. A member with only an alias and a key can complete a text chat, a streaming
   chat, and a text+image chat, using the stock OpenAI SDK, with no gateway-specific code.
2. Claude Code connects via `ANTHROPIC_BASE_URL` and completes a tool-using
   session against an OpenAI-only backend.
3. Sending an image to `model=coding` returns 400 `MODEL_CAPABILITY_NOT_SUPPORTED`
   and the backend access log shows **zero** corresponding requests.
4. An 11 MB image is rejected 413; a GIF labelled `image/png` is rejected 415.
5. Exhausting a quota returns 429 with `Retry-After` and correct `details`.
6. No member-visible response contains any configured `upstream_model` string.
7. No prompt, response, or image appears anywhere in the database or logs.
8. Adding a model = adding one YAML file + `/admin/registry/reload`, no restart.
9. Killing one backend shifts traffic within 45 s with no member-visible error.
10. `MODEL-001..010` run against each registered model and results appear in the console.
11. Full deployment from a clean host completes in under 10 minutes (§NFR-O2).
