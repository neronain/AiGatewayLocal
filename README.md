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

## The console assistant

Bottom right of the console there is a chat box. It is not a general chatbot —
it answers from *this* deployment's state, which is the only thing a general
model cannot tell you:

> **"ทำไม request ของฉันโดน 400"**
> Your key is on the `default` quota policy and you have 0 requests left in this
> window. It resets on 1 Sep.

> **"which model reads images"**
> `vision` (Vision AI (MSI-5)) — it is the only one in your catalogue with the
> vision capability.

What it can see depends on who is asking. A member's assistant sees their own
quota and the models they may use, and nothing else — not other people's usage,
not backend hostnames, not repository names. An admin's additionally sees
backend health, registry errors and upstream model names.

It is not a way around the rules either: its requests go through the same
capability gate, quota and routing as any other caller, spend the caller's own
quota, and show up in usage. Conversation history stays in the browser tab and
is never written server-side.

The box hides itself when no chat model is available to you — a chat box that
always answers "no backend" is worse than no chat box.

**Choosing the model.** The *Assistant* tab ranks every chat model for this
particular job and says why. The assistant's prompt is mostly system state and
grows with your fleet, while its answers are short and read in a small panel, so
context headroom and *not narrating* matter more here than raw capability:

```
  coder-next   Good fit   131,222-token context — room for state and history.
                          Plain chat model — answers without narrating.
                          Backend is healthy.
  general      Usable     16,384-token context works today, but the state block
                          grows with the fleet. Watch it as you add models.
                          Reasoning model, and nobody has tested whether this
                          backend separates the chain of thought.
  embed-only   Cannot     Does not serve chat, so it cannot hold a conversation.
```

Pick one from the dropdown or leave it on *Automatic*, which uses the same
ranking. A model that cannot serve the role is refused with the failing check
named, rather than accepted into a chat box that visibly does not work.
`GW_ASSISTANT_MODEL` still sets a deploy-time default.

> **Reasoning models** narrate unless the backend was started with vLLM's
> `--reasoning-parser`. The console strips what it recognises, but the real fix
> is the flag — `litegate model-test` reports it as `reasoning_not_separated`
> along with the command to correct it.

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

### LMDS — the deploy side of the pair

**[LMDS (Local Model Deploy Studio)](https://github.com/neronain/AutoDeployDGXProject)**
is the other half: it downloads weights, generates the launch bundle, and runs
the model on your own machines. LiteGate is the serving and verification side.

Neither depends on the other:

| You install | You get |
|---|---|
| LMDS alone | Models deployed and running on your hardware, with its own console and its own assistant |
| LiteGate alone | One endpoint, keys, quota and capability verification in front of backends you started any way you like |
| **Both** | The loop closes — LMDS deploys, LiteGate measures what the running server actually does and names the exact command to fix it, and either console's assistant can answer from the state of both |

The two meet in three places, all optional:

* **`managed_by`** on an endpoint turns LiteGate's advice from a placeholder
  into a command you can paste.
* **LMDS's brain** can point at LiteGate instead of a cloud provider, so model
  planning and both chat panels run on your own hardware:
  ```bash
  lmds config set-provider openai-compat --base-url http://litegate:8080/v1 --model general
  ```
* **The tool and reasoning parsers** LiteGate reports as missing are exactly the
  knobs LMDS exposes with `restart --tool-parser` and `test-tools`.

Nothing here is a dependency. A university that only wants to run models
installs LMDS; a company that already has backends installs LiteGate; the ones
who install both get the parts that only exist between them.

## Documentation

| Document | Contents |
|---|---|
| **[PRD.md](docs/PRD.md)** | Requirements, data model, acceptance criteria, decision log |
| **[ARCHITECTURE.md](docs/ARCHITECTURE.md)** | Pipeline, modules, design decisions and their costs |
| **[API.md](docs/API.md)** | Full endpoint reference with examples |
| **[DEPLOYMENT.md](docs/DEPLOYMENT.md)** | Docker / systemd / staging, operations, troubleshooting |
| [PRD-v1.2-Addendum.md](docs/PRD-v1.2-Addendum.md) | The original v1.2 addendum, preserved verbatim |

**Related project** — [LMDS · AutoDeployDGXProject](https://github.com/neronain/AutoDeployDGXProject),
the deploy side of the pair. Works alone; works better alongside this.

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
