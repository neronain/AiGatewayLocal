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
   │ request parameters    │ types and ranges; response_format → 400
   │                       │ repaired in place, or refused     │
   ├───────────────────────┤
   │ workspace policy      │ workspace_models allow-list       → 403
   ├───────────────────────┤
   │ resolve alias         │ registry snapshot + visibility    → 404
   ├───────────────────────┤
   │ parse content blocks  │ text / image / tool detection     → 400
   │                       │ magic-byte MIME, pre-decode size  → 413/415
   ├───────────────────────┤
   │ routing rules         │ overflow / small-prompt → another │  never rejects
   │                       │ *model*; falls back to the one    │
   │                       │ that was asked for                │
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

**Parameters are checked once, first, and in place.** `core/params.py` runs
straight after authentication, because every later step *uses* these values and
was written assuming they are well-typed. Values that are right but not in the
expected form are rewritten in the body itself (`max_tokens: 100.0` becomes
`100`), so the capability gate, the cache key and the payload that is forwarded
all read one corrected body rather than each normalising for itself. Fields the
gateway neither reads nor translates are passed through for the backend to
judge. The one exception is chat's `response_format`
([below](#a-malformed-response_format-is-repaired-or-refused-never-forwarded)).
Anything the gateway changed on the caller's behalf is reported in a response
header (`core/notices.py`): `x-litegate-output-cap`, `x-litegate-ignored`,
`x-litegate-adjusted`.

`/v1/messages` and `/v1/responses` enter the same pipeline after translation.
`/v1/embeddings` and `/v1/rerank` run the same order with the steps that do not
apply removed — no content blocks, no image policy, no streaming, no routing
rules — and two additions: a batch-size ceiling checked before quota, and a
context budget checked **per item** rather than against the batch total.

### Two layers of routing

They answer different questions and must not be collapsed into one:

| | question | inputs |
|---|---|---|
| **routing rules** (`core/rules.py`) | which *model* should answer a request shaped like this | estimated prompt size, requested output size |
| **Router** (`core/routing.py`) | which *machine* serves this alias | health, capability, concurrency, priority |

Routing rules run **after** the request has been profiled (they need its size) and
**before** the capability gate and context budget, so those gates check the model
that will actually run. They never reject: a rule that cannot be honoured — target
missing, target too small, target unable to serve the request's modality — degrades
to the model that was asked for, so a misconfiguration is a log line, not an outage.

**Permission and quota are deliberately not re-evaluated after routing.** Both are
checked against the alias the member asked for, before the rules run. Re-checking
against the resolved model would let a routing rule silently widen or narrow what
someone may use; charging against it would make a member's bill depend on an
admin's internal plumbing. The response body, the `x-litegate-model` header and the
usage row all carry the requested alias — routing is an admin decision of the same
kind as repointing an alias (PRD §6), and the member asked for `coding` and gets
`coding`.

Rules are declared on the model that is asked for, and there are three:

```yaml
spec:
  routing:
    overflow: coding-long          # the prompt does not fit this model's window
    small_prompt:                  # housekeeping calls should not occupy a big model
      under_tokens: 2000
      max_output_tokens: 512       # skip the rule when a long answer is requested
      target: quick
    fallback: [coding-backup]      # every machine for this alias is down
```

| Rule | The problem it solves |
|---|---|
| `overflow` | A prompt over the window is thrown away with a `400` while a long-context machine sits idle. Claude Code produces these routinely. |
| `small_prompt` | Naming a session or summarising a topic used to take a slot on the same model as the real work. |
| `fallback` | Endpoint failover runs out when **every machine for the alias** is down. That was a `503`. |

Decisions are made on **request size only** — never on guessed intent — and every
threshold is a number in a file that can be read back and checked.

The guard rails are load-time wherever they can be. An `overflow` target whose
window is not actually wider is rejected when the registry loads, as are cycles
and targets that do not exist; cross-alias checks (target exists, no
self-reference, no cycle, overflow target actually wider) run once at load, not
per request. At request time the resolved target must clear the *same* capability
gate: an image bound for a text-only target keeps the request where it started,
so the error names the alias the member actually asked for.

`fallback` targets clear the same gates as the model that was asked for — API
surface, capabilities, and context window, each measured with the target's own
tokenizer rate. A target that cannot take the request is skipped, and the caller
gets the original failure (the machine was down, or full), which is the true
one, instead of a `400` from a model they never chose. The output ceiling is
recomputed for whichever model ends up serving, because a fallback can have a
narrower window.

**What follows the served model, and what follows the requested alias.** The
member's side — response `model`, `x-litegate-model`, usage row, quota — stays
with the alias they asked for. The machine's side — the in-flight slot, success
and failure reports, `max_output_tokens`, the tokenizer rate — follows the model
that actually serves, because the endpoint belongs to that model. Counting a
rerouted request under the requested alias gave a 1-slot backend two counters.

### `model: "auto"` — a choice made before the pipeline, by the same gates

`auto` is not a third routing layer. It is resolved to a real alias at the top
of `/v1/chat/completions`, and from there the request runs the pipeline above
like any other. `core/auto.py` does it in three steps whose order is the design:

1. **Permission.** The caller hands in only the models this key may use. The
   module knows nothing about members and cannot widen anything.
2. **Facts.** Candidates are filtered with the *same* checks the pipeline will
   apply next — the alias is enabled, the surface is on, the capabilities the
   request needs are declared (`rules.can_serve`), some endpoint can take the
   request's modalities, and the prompt fits the window when counted at that
   model's own tokenizer rate. A ranker that picked a model the next gate
   refuses would be worse than no `auto`.
3. **Preference.** Only what survives is ranked, by one of four strategies
   (`fastest`, `roomiest`, `quality`, `balanced`). No score, however high, gets a
   model past step 2.

Two inputs feed the ranking, and they live in different places on purpose:

| Input | Where it lives | Why there |
|---|---|---|
| Speed (output tok/s, TTFT) | `core/perf.py`, in each worker's memory, fed from the same numbers that go into the usage row | Ranking must not touch the database on the request path |
| Quality score | `spec.quality_score` in the model's YAML | It is a statement about the model, reviewed and versioned with the rest of its definition |
| Strategy | one row in `gateway_settings`, read on each `auto` request | It is decided on screen and every worker must follow it at once. Not `gateway.yaml`: an older schema rejects a file with a key it does not know, and a downgrade would silently lose the vision, quota and permission policy in the same file |

Missing data never removes a candidate; it decides where the candidate sorts,
and the two strategies that use scores chose opposite defaults deliberately.
Without speed samples a model sorts **last** under `fastest`. Without a quality
score it sorts last under `quality` and `balanced`. But under `balanced` a
scored model without speed samples is weighed as if it were as **fast** as the
fastest candidate: the statistics are per worker and gone on restart, so if "no
samples" meant "loses", whichever model happened to be measured first would win
for good — the loser is never chosen and so never measured.

`balanced` is `(2 × quality + 100 × speed) ÷ 3` with speed as a fraction of the
fastest candidate, not min-max normalisation. With two candidates min-max always
yields 0 and 1 whether they differ by 2% or five-fold, which turns "balanced"
into "the heavier axis wins". The 2:1 weight is a constant in the code.

The ranking is computed once and returned as a list of frozen candidates, copies
of the numbers at that moment. `GET /admin/auto/preview` renders that list
rather than re-reading the statistics, so the explanation cannot drift from the
decision it explains.

*Cost:* speed statistics are per worker. With several workers, `fastest` and
`balanced` can disagree between workers while their numbers are close. Answers
served from the response cache are kept out of the statistics, or a model that
is asked the same thing repeatedly would rank by the speed of the cache.

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
│   ├── writer.py         comment-preserving writes from the console
│   └── store.py          snapshot loading, atomic hot reload
│
├── core/
│   ├── auth.py           API keys, Principal, workspace permission, privilege
│   ├── params.py         request-parameter types and ranges, checked first
│   ├── responseformat.py structured-output shape: repair or refuse
│   ├── multimodal.py     content-block parsing, image policy
│   ├── capability.py     the two capability gates
│   ├── tokens.py         token estimation + the visual/text split
│   ├── quota.py          policy resolution, counters (Redis or DB)
│   ├── routing.py        endpoint selection, health with hysteresis
│   ├── rules.py          model-level routing rules
│   ├── auto.py           model="auto": filter, then rank by strategy
│   ├── perf.py           per-worker speed statistics that feed auto
│   ├── retrieval.py      embeddings/rerank shapes, batch ceiling, costing
│   ├── codexcatalog.py   the model catalogue in the shape Codex decodes
│   ├── inflight.py       concurrency counters (Redis or per-process)
│   ├── modeltest.py      the capability probe behind Verify
│   ├── lmds.py           deploy-tool findings that can be applied
│   ├── release.py        version comparison for the console's update check
│   ├── usage.py          buffered usage recording
│   ├── latency.py        latency / TTFT percentiles read from usage rows
│   ├── keyvault.py       sealing and opening one API-key copy
│   ├── keyrotation.py    survey and re-seal of every copy, across secrets
│   └── errors.py         error taxonomy
│
├── upstream/
│   ├── client.py         pooled httpx, header sanitising
│   ├── sse.py            SSE parse/format
│   └── protocol/
│       ├── anthropic.py  Anthropic ⇄ OpenAI, unary and streaming
│       └── responses.py  Responses ⇄ OpenAI, unary and streaming
│
├── api/
│   ├── openai.py         /v1/models, /v1/chat/completions
│   ├── anthropic.py      /v1/messages, /v1/messages/count_tokens
│   ├── responses.py      /v1/responses  (Codex)
│   ├── retrieval.py      /v1/embeddings, /v1/rerank
│   ├── assistant.py      /v1/assistant/*
│   ├── catalog.py        /v1/catalog, /v1/me
│   ├── auth.py           console sign-in, sessions
│   ├── tools.py          the on-prem client-tool mirror
│   ├── admin.py          /admin/*
│   └── health.py         /healthz, /readyz, /metrics
│
├── db/
│   ├── models.py         SQLAlchemy schema
│   └── session.py        async engine/session
│
└── vendor/
    └── codex/            Codex's own system prompt (Apache-2.0), served in its catalogue
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
that the member cannot act on.

### Check-then-record quota, not reserve-then-settle

Reserving tokens would mean holding a reservation across a generation that can
run for minutes, and refunding on every failure path — including client
disconnects mid-stream. The chosen model allows a bounded overrun (in-flight
requests × per-request cost) that self-corrects on the next check. Recorded as
NFR-Q1 rather than hidden.

### Buffered usage writes

A usage row is bookkeeping; an inference response is the product. Writes are
buffered and flushed every 2 seconds so a slow or briefly unavailable database
adds no latency to a member's request. The trade is losing at most one flush
window of rows on an unclean shutdown — acceptable for capacity planning data,
and the buffer is drained on graceful shutdown.

A failed flush costs as little as it can. A row the database refuses is dropped
alone and named in the log (`usage record dropped: …`) — the batch is retried one
row at a time, so it never takes other members' rows with it. A database that is
locked or unreachable is not any row's fault: the batch goes back in the buffer
(up to 5,000 rows) and is tried again on the next flush. Retrying is safe because
`usage_logs.request_id` is unique and generated by the gateway, never taken from
the caller's `x-request-id` — that value is stored beside it in
`client_request_id`, where it may repeat.

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

### Privilege belongs to the credential; the role belongs to the person

`Principal.role` is the owner's role and decides what the caller can *reach* —
which models they see, whether workspaces narrow them. Whether this credential
carries the owner's power over other people and over the gateway is a separate
question, answered in exactly one place: `Principal.is_admin` and
`Principal.is_manager` in `core/auth.py`. Both are false for a key that has any
limit written on it (`limits_on_key`: a model list, access groups, a workspace,
or a quota policy of its own that is still in force). `require_admin`,
`require_manager` and every `if actor.is_admin` in `app/api` read those two
properties, so a route added later cannot forget the rule. A console session
has no key and therefore no limits.

The reasoning is that a limit on a key is a statement about what the key is
*for*. While privilege came from the role alone, a key limited to one model
could call the admin route that removes its own limit, so the limit was advisory
and one leaked script key was the whole gateway.

What counts was chosen so that the rule could ship without a switch. `scopes`
does not count: it has never been enforced anywhere, and the bootstrap key of
every install carries `["admin"]` — counting it would have removed admin rights
from the one key every operator holds, on upgrade day, over a field the console
neither shows nor edits.

The same function is imported by `scripts/restricted_key_report.py`, so the
pre-upgrade report and the request path cannot disagree about what "limited"
means.

Making "no limit" mean "carries the owner's rights" has a second half: lifting
a limit now hands rights out, and limiting or revoking takes them away. That is
decided in one place too — `_assert_may_decide_key` in `api/admin.py`, called by
the three routes that issue, amend and revoke a key. An administrator's key is
an administrator's to decide; a manager's key that carries their rights, before
the change or after it, is that manager's or an administrator's; everything else
is governed by the workspace and model checks that were already there. Revoking
is held to the same rule as narrowing because it is the same act done
irreversibly, and so is extending an expiry, because reviving an expired key
hands the same rights out again.

One step earlier in the same chain is closed by `_assert_may_enrol`: a manager
cannot add an administrator to a workspace. Membership gives an administrator
nothing, since they are not scoped by it, but it is what makes their user row
and their keys appear in that manager's lists. The check is applied to the
people a request would actually add, so it needs no migration — memberships
that exist are untouched, and removing stays open to managers. A manager
enrolling another manager is left alone deliberately: it is how a co-teacher is
added, and it gives nothing of the other manager's rights, which only a key
could carry.

*Cost:* a per-key quota lives in another table, so knowing about it costs a
query. `authenticate` asks only when the answer can change the *decision* — the
owner is a manager or an admin and the key has no other limit — so member
traffic, which is nearly all of it, pays nothing. `Principal.key_limits` is
therefore enough to decide privilege and is not the full list. Anything that
*shows* the limits to a person asks for the full list instead
(`key_limits_in_full`): one more query on `GET /v1/me/key` and on the refusal
path of the admin and manager gates, none on the request path. A field with one
name answers the same wherever it is read.

### An empty result of a written limit is an empty allow-list

A key limited to access groups is expanded to the models those groups name. When
every group is switched off the expansion is empty — and "the limit expanded to
nothing" used to be read as "no limit was written", handing the key the whole
catalogue. The check is now whether a limit was *written*
(`key_models or key_access_groups`), not whether it produced anything. It is the
same distinction the membership rule already drew between "in no workspace" and
"in workspaces that allow nothing".

### A malformed `response_format` is repaired or refused, never forwarded

Everywhere else the gateway lets the backend judge a field it does not use
itself. Chat's `response_format` is the exception because the backends do not
judge it: llama.cpp answers `200` to a schema in the wrong place and generates
unconstrained text. The caller then fails parsing a reply against a schema the
model never saw, with nothing to say why.

So `core/responseformat.py` normalises the *envelope* — keys beside `type` moved
into `json_schema`, `parameters` renamed to `schema`, a missing `type` or `name`
filled in — reports every change in `x-litegate-adjusted`, and refuses with a
`400` naming the field when there is no schema to move. It does **not** touch
the JSON Schema itself. The reference implementation this was adapted from adds
`additionalProperties: false` and rewrites `required` under `strict`; measured
against llama.cpp, an unspecified object is already closed, `strict` has no
effect, and rewriting `required` changes the answer, so that part was left out.

The same failure existed one layer up, in our own translators: a
`text.format` or `output_config.format` they could not express as a
chat-completions `response_format` was dropped. Those are now reported through
the existing "cannot be translated" path (`untranslatable` → the capability
gate), which already refuses before quota and before a slot is taken, and
already lets a backend that speaks the protocol natively take the request whole.

Because the repair happens in `core/params.py`, before anything else reads the
body, the response-cache key is built from the repaired form.

*Cost:* behaviour was measured on one llama.cpp build. vLLM was read, not run.
And the chat path no longer counts the schema towards the prompt estimate
(llama.cpp turns it into a sampling grammar, not prompt tokens) while the two
translated paths still do — the estimates differ by the size of the schema.

### Percentiles are computed in Python, from bounded samples

`core/latency.py` reads `usage_logs`; nothing in it runs on the request path.
The database is asked only for `WHERE`, `ORDER BY ts`, `LIMIT` and `COUNT`. The
sorting and the rank are done in Python with integer arithmetic.

SQL would be the obvious place, and is not used because SQLite has no percentile
function, and the gateway has to give the same answer on both databases it
supports. Nearest-rank was chosen over interpolation for the same reason it is
`percentile_disc` in PostgreSQL: the result is always a request that happened,
and with few samples it cannot report something better than what was seen.

Three decisions keep the report from flattering the system. Only successful,
backend-answered requests count towards latency; a stream that never produced a
first token is counted beside the TTFT figures instead of vanishing from them;
and below a minimum sample size (20 for p50 and p95, 100 for p99) a percentile
is `null` rather than a number. 20 and 100 are the smallest samples in which at
least one request is still slower than the p95 and the p99; under that, the
"percentile" is the slowest request with a statistical name. Whatever is
excluded is counted in the same answer.

The work is bounded whatever the table size: at most the newest 10,000 samples
per group per measure, at most 50 groups, and the answer says when either limit
was reached and what period the sample actually covers. A group known to be
under the cap is fetched without `ORDER BY`, because ordering by `ts` makes the
database walk the time index to find a small group.

Cache hits are told apart by a column, `usage_logs.cache_hit`, because nothing
else distinguishes them: a cached answer is a successful row with a latency of
about a millisecond. It is nullable on purpose — an upgraded database gets it by
`ADD COLUMN`, which is nullable anyway, so fresh and upgraded schemas match, and
the previous version can still write rows after a downgrade.

*Cost:* hits recorded before the column existed cannot be separated. Timing was
measured on SQLite only.

### Sealed key copies: two secrets and a deliberate re-seal

By default an API key is stored as a keyed hash and cannot be shown again. With
`GW_KEY_REVEAL_SECRET` set, a second copy is kept, sealed with AES-GCM under a
key derived from that secret, which must live outside the database.

A secret that cannot be changed cannot be recovered from once it leaks, so
there are two: the current one seals everything new and is tried first when
opening; `GW_KEY_REVEAL_SECRET_PREVIOUS` is tried second, never seals, and
never switches the feature on by itself.

The stored form is `v2:<key id>:<base64(nonce + ciphertext)>`. The key id is
eight hex characters from a second key-derivation pass over the secret with a
different salt, so it shares nothing with the encryption key, and it is bound
into the ciphertext as associated data — editing it in the database makes the
copy fail to open rather than point at another secret. It exists to answer the
question the older `v1:<base64>` form could not: *why* does this copy not open —
the wrong secret, or a damaged copy — and which secret would. `v1` copies still
open and are not rewritten on upgrade, because the previous version cannot read
`v2` and converting a row that already opens would only make a downgrade
harder.

State is always decided by **actually opening** the copy, never by reading the
label: `current`, `previous`, `lost`, `off`, or `none`. A `lost` copy carries
the reason, because the remedies differ: a missing secret can be supplied, a
damaged copy cannot be repaired, a newer format needs an upgrade.

Moving copies from the previous secret to the current one (`core/keyrotation.py`)
is an explicit action — console, API or command line — and is not done at
startup, for three reasons: a multi-worker gateway would run it once per worker
at the moment the system is trying to come up; it changes which secret opens the
data, which should have a name and a time in the audit log; and a typo in `.env`
should not have a permanent effect the instant the service restarts. Startup
only surveys and logs.

The re-seal is written to survive being repeated, interrupted and run
concurrently: one commit per row; a row is updated only if it still holds the
value that was read (compare-and-swap on the sealed value itself, which changes
on every seal because the nonce does); the new value is opened before it is
written; rows already current, and rows nothing opens, are not touched.

Opening is not the last check. The seal authenticates a copy against a secret,
not against the row it is stored in, so a sealed value moved from one row to
another would open — and Reveal would hand out one person's key under another
key's name, with the audit log recording the wrong key. Whatever opens is
therefore hashed and compared with the row's own `key_hash` before it is shown,
counted as revealable or re-sealed; a mismatch is `lost` with the reason
`not_this_key`, and the re-seal leaves it untouched rather than carrying a wrong
copy forward under the current secret.

*Cost:* one previous secret, not a chain. Changing `GW_API_KEY_PEPPER` turns
every copy into `not_this_key`, since the hash they are checked against is
computed under the pepper. And none of this covers `data/secrets.json`, the
upstream provider keys, which are stored unsealed.

---

## Concurrency and state

Per-process, not shared:

- **Registry snapshot** — immutable, swapped wholesale on reload
- **Endpoint health** — in-flight counts, failure streaks
- **Usage buffer** — flushed to the shared database
- **Speed statistics** — what `model: "auto"` ranks with; empty after a restart

Shared across processes:

- **Database** — identity, permission, usage, audit, and the settings changed
  from the console (the assistant's model, the `auto` strategy)
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
