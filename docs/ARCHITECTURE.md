# Architecture

## The invariant

```
Gateway owns          Model server owns
─────────────────     ─────────────────
Identity              Inference
Permission            Tokenizer
Capability            Vision encoder
Quota                 Tool parser
Routing               KV cache
Usage                 GPU scheduling
Protocol
```

**The test for any proposed feature:** if it requires the gateway to interpret
model *content* — decode an image, tokenize text, classify a prompt — it belongs
on the right. This is what keeps the gateway small enough for a small team to
maintain, even as vision and agentic workloads land.

Two consequences worth stating plainly:

- The gateway never runs a tokenizer, so token counts are either backend-reported
  or estimated, and every usage row records which (`token_accounting`).
- The gateway never decodes an image, so image validation works from magic bytes
  and header geometry only.

---

## Request pipeline

```
POST /v1/chat/completions
        │
   ┌────▼──────────────────┐
   │ authenticate          │ HMAC-SHA256 key lookup            → 401
   ├───────────────────────┤
   │ course policy         │ course_models allow-list          → 403
   ├───────────────────────┤
   │ resolve alias         │ registry snapshot + visibility    → 404
   ├───────────────────────┤
   │ parse content blocks  │ text / image / tool detection     → 400
   │                       │ magic-byte MIME, pre-decode size  → 413/415
   ├───────────────────────┤
   │ model capability gate │ vision? tools? streaming?         → 400  ◀ no backend call
   ├───────────────────────┤
   │ context budget        │ estimate vs context_tokens        → 400
   ├───────────────────────┤
   │ quota                 │ counters vs resolved limits       → 429
   ├───────────────────────┤
   │ select endpoint       │ protocol ∧ modality ∧ healthy     → 503
   │                       │ ∧ capacity                        → 429
   ├───────────────────────┤
   │ forward               │ stream or unary, alias masked     → 502/504
   ├───────────────────────┤
   │ record                │ usage row + quota increment       (always)
   └───────────────────────┘
```

The order is not arbitrary. Cheap, local checks run before expensive ones, and
every check that can reject does so before a backend connection is opened. A
capability rejection costs a few hundred microseconds and zero GPU time.

---

## Modules

```
app/
├── main.py               app factory, middleware, error handlers, lifespan
├── config.py             env settings
├── state.py              process-wide services (registry, router, quota, usage)
│
├── registry/
│   ├── schema.py         canonical YAML schema + load-time consistency rules
│   └── store.py          snapshot loading, atomic hot reload
│
├── core/
│   ├── auth.py           API keys, Principal, course permission
│   ├── multimodal.py     content-block parsing, image policy
│   ├── capability.py     the two capability gates
│   ├── tokens.py         token estimation + the visual/text split
│   ├── quota.py          policy resolution, counters (Redis or DB)
│   ├── routing.py        endpoint selection, health with hysteresis
│   ├── usage.py          buffered usage recording
│   └── errors.py         error taxonomy
│
├── upstream/
│   ├── client.py         pooled httpx, header sanitising
│   ├── sse.py            SSE parse/format
│   └── protocol/
│       └── anthropic.py  Anthropic ⇄ OpenAI, unary and streaming
│
├── api/
│   ├── openai.py         /v1/models, /v1/chat/completions
│   ├── anthropic.py      /v1/messages, /v1/messages/count_tokens
│   ├── catalog.py        /v1/catalog, /v1/me
│   ├── admin.py          /admin/*
│   └── health.py         /healthz, /readyz, /metrics
│
└── db/
    ├── models.py         SQLAlchemy schema
    └── session.py        async engine/session
```

---

## Design decisions

### Registry in YAML, not in the database

The set of models is deployment configuration, not runtime state. Keeping it in
YAML means it is version-controlled, reviewable in a pull request, diffable, and
rollback-able. The `models` table is a *projection* that exists only so usage
rows and compatibility results have something to reference.

Reload is atomic: a snapshot that fails validation is discarded and the previous
one is kept, with the error surfaced on `/readyz`. A typo in a YAML file cannot
take the gateway down.

*Cost:* `POST /admin/registry/reload` only reloads the worker that served it.
Multi-worker deployments rely on the file-watcher. Documented in DEPLOYMENT §4.1.

### Two capability gates, not one

A model declaring `vision: true` is necessary but not sufficient — the specific
endpoint chosen must also serve images. Checking only the model would let a
vision request route to a text-only backend and fail with a backend-shaped 500
that the student cannot act on.

### Check-then-record quota, not reserve-then-settle

Reserving tokens would mean holding a reservation across a generation that can
run for minutes, and refunding on every failure path — including client
disconnects mid-stream. The chosen model allows a bounded overrun (in-flight
requests × per-request cost) that self-corrects on the next check. Recorded as
NFR-Q1 rather than hidden.

### Buffered usage writes

A usage row is bookkeeping; an inference response is the product. Writes are
buffered and flushed every 2 seconds so a slow or briefly unavailable database
adds no latency to a student's request. The trade is losing at most one flush
window of rows on an unclean shutdown — acceptable for capacity planning data,
and the buffer is drained on graceful shutdown.

### Anthropic translated rather than required

Requiring every backend to speak Anthropic would exclude vLLM, which is what the
DGX nodes actually run. Translating in the gateway means Claude Code works
against an OpenAI-only backend today. The translation is the single most
fragile part of the system — it tracks two evolving API surfaces — which is why
MODEL-009 exists and belongs in CI.

### Fail closed on capabilities

Absent capability flags default to `false`. A model that forgets to declare
`tools: true` will reject tool requests with a clear message, rather than
forwarding them and producing confusing partial behaviour.

---

## Concurrency and state

Per-process, not shared:

- **Registry snapshot** — immutable, swapped wholesale on reload
- **Endpoint health** — in-flight counts, failure streaks
- **Usage buffer** — flushed to the shared database

Shared across processes:

- **Database** — identity, permission, usage, audit
- **Redis** (optional) — quota counters. Without it, counters live in the
  database, which is correct for a single worker and slightly lossy in ordering
  under many workers.

This means health state is per-worker: with 4 workers, an endpoint may be
ejected by one worker before the others notice. Each converges within
`unhealthy_threshold × health_check_interval_seconds` (45 s by default).

---

## Failure behaviour

| Failure | Behaviour |
|---|---|
| One backend down | Ejected after 3 failed probes; traffic shifts to remaining endpoints |
| All backends for a model down | Requests still attempted (a stale probe must not take a model offline); a warning is logged |
| No compatible endpoint | `503 NO_HEALTHY_ENDPOINT` naming the modality and protocol |
| Backend at capacity | `429 CONCURRENCY_LIMIT_EXCEEDED` with `Retry-After` |
| Redis down | Falls back to DB counters; no request fails |
| Database down | Auth fails (correctly — permission cannot be verified); `/readyz` reports it |
| Bad registry edit | Previous snapshot retained; error on `/readyz` |
| Client disconnects mid-stream | Usage recorded with `status=aborted`; upstream connection closed |

---

## Scaling

Vertical first: uvicorn workers on one host handle a few hundred concurrent
streams, because the gateway is I/O bound and each stream is mostly idle.

Horizontal when needed:

```
            Load balancer
          ┌───────┴───────┐
      gateway-1       gateway-2
          └───────┬───────┘
        PostgreSQL + Redis      ← Redis becomes mandatory here
```

Redis stops being optional the moment there is more than one process enforcing
quota, otherwise each instance counts independently and effective limits
multiply by the instance count.

The gateway is stateless apart from per-worker health and the usage buffer, so
instances can be added or removed without coordination.
