# LiteGate

**Capability-aware, multimodal AI gateway for teams that run their own GPUs.**
Put your own model servers behind the standard OpenAI and Anthropic APIs, with
identity, policy, quota and usage accounting in front of them — and without
exposing a raw model server to anyone.

Built for the case where a handful of people share a handful of GPUs: a small
company, a school or university department, a research group, an agency running
inference for its own clients. Nothing in it assumes a particular sector.

[![CI](https://github.com/neronain/AiGatewayLocal/actions/workflows/ci.yml/badge.svg)](https://github.com/neronain/AiGatewayLocal/actions/workflows/ci.yml)

```
    Members                       LiteGate                    Your GPUs
┌─────────────┐          ┌────────────────────────┐        ┌──────────────┐
│ Python SDK  │          │ Auth · Workspace policy│        │ vLLM         │
│ Claude Code │ ─HTTPS─▶ │ Capability · Quota     │ ─────▶ │ llama.cpp    │
│ Web / App   │          │ Routing · Usage        │        │ Ollama/SGLang│
└─────────────┘          └────────────────────────┘        └──────────────┘
      alias only            policy + accounting               inference
```

### Who it is for

| You have | LiteGate gives you |
|---|---|
| One GPU box shared by a small team | Per-person keys and quota instead of an open port |
| Several nodes with different models | One alias per job (`coding`, `vision`) that survives model swaps |
| A class, a department, or client projects | Workspaces, each with its own allowed models and limits |
| Claude Code users and OpenAI-SDK users | Both, against the same backend, without changing the backend |

---

## What it does

| | |
|---|---|
| **Capability registry** | Models declare what they *can do* (`vision`, `tools`, `agentic`), not what they *are*. Adding a model is one YAML file — no code change. |
| **Fails fast, not downstream** | Send an image to a text-only model and you get a `400` with an actionable message. The backend never sees the request. |
| **Multimodal from day one** | Text + image content blocks, streaming, on both the OpenAI and Anthropic surfaces. |
| **Stable aliases** | Members use `coding`. Admins can repoint it from Qwen to something else with zero member-side change. Repository names are never member-visible. |
| **Real quota** | Per member, workspace and model, over requests / text tokens / **visual tokens** / output tokens / images. |
| **Claude Code works** | `/v1/messages` is served even when the backend only speaks OpenAI — the gateway translates both directions, streaming included. |
| **Private by default** | No prompt, no response, no image is ever written to disk. The schema has no column for them. |

---

## Quick start

```bash
git clone https://github.com/neronain/AiGatewayLocal.git
cd AiGatewayLocal

python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

# Terminal 1 — a stand-in model server (no GPU needed)
.venv/bin/python scripts/mock_backend.py --port 8000

# Terminal 2 — point an alias at it, then run the gateway
sed -i '' 's#http://dgx03:8000#http://127.0.0.1:8000#' config/models/coding.yaml
.venv/bin/uvicorn app.main:app --port 8080
```

The log prints a bootstrap admin key once. Then:

```bash
export KEY=edu_sk_...

curl -s -X POST http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"coding","messages":[{"role":"user","content":"Reply with exactly: OK"}]}'
```

Console at <http://localhost:8080/console>, API docs at `/docs`.

For real deployments see **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)** — Docker
Compose, native systemd, and a no-GPU staging path.

---

## Accounts and keys

Two credentials, two audiences — the distinction that keeps production keys out
of browsers:

| | API key (`lg_sk_...`) | Console sign-in |
|---|---|---|
| Authenticates | a **program** — SDK, Claude Code, curl | a **person** in a browser |
| Lives in | a config file or `.env` | the operator's head |
| Lifetime | long (default 180 days) | 8 hours |
| How many | several — one per machine or project | one session |
| If it leaks | revoke that key; other work is unaffected | sign out, change password |

On first start the console asks for a username and password rather than a token,
and creates the first administrator. Set `GW_ADMIN_USER` / `GW_ADMIN_PASSWORD` to
choose them; otherwise a password is generated and printed once.

### What each role can do

| | member | manager | admin |
|---|---|---|---|
| See the models they may use, and their own quota | ✅ | ✅ | ✅ |
| Issue, name and revoke **their own** API keys | ✅ | ✅ | ✅ |
| Manage people in their workspace, issue keys for them | — | ✅ | ✅ |
| Choose which models a workspace may use | — | ✅ | ✅ |
| Set workspace quota, read workspace usage | — | ✅ | ✅ |
| Add, edit or delete models in the registry | — | — | ✅ |
| Verify backends, run the model test suite, reload the registry | — | — | ✅ |

The line is deliberate: **a manager decides who may use what; an admin decides
what exists.** Adding a model touches GPUs and machine configuration, which is
not a people-management decision.

## For members

**Python**

```python
from openai import OpenAI

client = OpenAI(base_url="https://gateway.university.ac.th/v1", api_key="edu_sk_...")

response = client.chat.completions.create(
    model="coding",                       # an alias, not a repository name
    messages=[{"role": "user", "content": "เขียนฟังก์ชัน bubble sort ใน Python"}],
)
```

**Vision**

```python
client.chat.completions.create(
    model="gemma-vision",
    messages=[{"role": "user", "content": [
        {"type": "text", "text": "อธิบายภาพนี้"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
    ]}],
)
```

**Claude Code**

```bash
export ANTHROPIC_BASE_URL=https://gateway.university.ac.th
export ANTHROPIC_AUTH_TOKEN=edu_sk_...
export ANTHROPIC_MODEL=coding
claude
```

---

## Adding a model

Write `config/models/<alias>.yaml` — that is the whole job:

```yaml
apiVersion: litegate.dev/v1
kind: Model
metadata:
  alias: my-model
  display_name: My Model
  visibility: member
spec:
  upstream_model: org/Model-Name-On-The-Backend
  purpose: [general]
  limits: { context_tokens: 131072, max_output_tokens: 8192 }
  modalities: { input: [text], output: [text] }
  capabilities: { chat: true, tools: true, streaming: true }
  protocols: { openai: true, anthropic: false }
  endpoints:
    - name: dgx01
      server_type: vllm
      base_url: http://10.0.0.21:8000
      protocols:  { openai: true, anthropic: false }
      modalities: { text: true, image: false }
```

Contradictions are caught at load, not at request time: declaring
`capabilities.vision: true` without `image` in `modalities.input`, or without any
endpoint that serves images, is rejected with a specific message and the previous
good registry is kept.

Then certify it — a model is `READY` because it was measured, never because of
its name:

```bash
python scripts/model_test_suite.py --base-url $GW --admin-key $KEY --model my-model
```

```
  MODEL-001 ... PASS      34 ms  replied 'OK'
  MODEL-002 ... PASS      10 ms  6 chunks
  MODEL-004 ... PASS      11 ms  called get_weather
  MODEL-009 ... PASS      11 ms  stop_reason=tool_use
```

---

## Working with a deploy tool

LiteGate does not deploy models and does not want to. It measures what a running
server actually does, which is the one thing a deploy tool structurally cannot
check: the tool knows what it *generated*, not what the process is *doing*.

Used together the loop closes. Used alone, each still works.

```
  deploy tool                LiteGate
  (LMDS, Ansible,     ──▶    verifies the running server
   a shell script)           and names the fix
        ▲                              │
        └──────────────────────────────┘
              you apply it
```

**Verify** re-probes every backend of a model and reports what to change:

```
[warning] tools_flag_missing
  vLLM rejected the tool request: it was started without
  --enable-auto-tool-choice and a --tool-call-parser.
  → ./<controller>.sh restart --tool-parser qwen3_coder
```

If a model file says where the backend came from, the command is filled in
rather than left as a placeholder:

```yaml
endpoints:
  - name: msi-6
    base_url: http://10.0.0.6:8000
    managed_by:                       # optional, purely informational
      tool: lmds
      node: ops@10.0.0.6
      controller: ~/bundles/coder/coder-single.sh
```

```
→ ssh ops@10.0.0.6 '~/bundles/coder/coder-single.sh restart --tool-parser qwen3_coder'
```

`managed_by` is inert: LiteGate never contacts the deploy tool, and every model
works without it. It only decides whether a finding names a command you can
paste or one you have to translate.

## Documentation

| Document | Contents |
|---|---|
| **[PRD.md](docs/PRD.md)** | Requirements, data model, acceptance criteria, decision log |
| **[ARCHITECTURE.md](docs/ARCHITECTURE.md)** | Pipeline, modules, design decisions and their costs |
| **[API.md](docs/API.md)** | Full endpoint reference with examples |
| **[DEPLOYMENT.md](docs/DEPLOYMENT.md)** | Docker / systemd / staging, operations, troubleshooting |
| [PRD-v1.2-Addendum.md](docs/PRD-v1.2-Addendum.md) | The original v1.2 addendum, preserved verbatim |

---

## Development

```bash
make dev      # install with dev dependencies
make test     # 48 tests
make lint
make check    # what CI runs
make run      # reload server on :8080
make mock     # mock backend on :8000
```

Tests cover the capability contract, vision policy enforcement (including a GIF
mislabelled as PNG, and the SSRF guard on remote URLs), quota exhaustion,
streaming on both protocols, Anthropic translation, and the guarantee that no
repository name reaches a member-visible response.

---

## Project status

| Milestone | State |
|---|---|
| M1 — Core: registry, capability validation, OpenAI surface, quota, routing | done |
| M2 — Agent: Anthropic surface, translation, test suite | done |
| M3 — Pilot: one real DGX, one workspace | next |
| M4 — Production: Postgres + Redis, TLS, monitoring | planned |
| M5 — P1: image upload, PDF, richer dashboard | planned |

Verified end-to-end on Ubuntu 24.04 against a mock backend: all 10 model tests,
streaming on both protocols, capability rejection with zero backend calls, and
the image-type and SSRF guards.

## Credits

Created and maintained by **neronain** — [facebook.com/neronain.minidev](https://www.facebook.com/neronain.minidev)

## License

MIT — see [LICENSE](LICENSE).
