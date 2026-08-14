# Claude Developer Mode → Cloudflare Tunnel → LiteGate

Reaching your own models from Claude's third-party provider settings, from a
machine that is not the one running the gateway.

Everything here was worked out against a running deployment, including the
errors — each one in the troubleshooting table was hit before it was written
down.

```
  Claude (Developer Mode)          Cloudflare                 Your machine
 ┌───────────────────────┐      ┌──────────────┐      ┌────────────────────┐
 │ base URL  → tunnel    │─────▶│ public HTTPS │─────▶│ cloudflared        │
 │ API key   → lg_sk_…   │      │ real cert    │      │   ↓ localhost:8080 │
 │ model     → alias     │      └──────────────┘      │ LiteGate → GPUs    │
 └───────────────────────┘                            └────────────────────┘
```

The tunnel exists to solve one problem: **Claude accepts `https`, or `http` on
loopback, and nothing else.** A LAN address is refused outright with
`baseUrl: must use https (or http on loopback)`. A tunnel supplies a name with a
certificate every client already trusts, so no CA has to be installed anywhere.

If Claude runs on the gateway host itself, skip all of this and use
`http://127.0.0.1:8080` — see the README.

---

## 1 · Give the models names the client expects

Some clients pin Anthropic's model names. An alias is a name pointing at a
model, so both can be true at once:

```yaml
metadata:
  alias: claude-opus-4.8            # what the client asks for
spec:
  upstream_model: Qwen3-Coder-30B-A3B-Instruct   # what actually answers
```

Create them from the console (Models → Add) or `POST /admin/models`. Point them
at models measured as `agentic: true` — Claude Code is a tool loop, and a model
that cannot call tools will look broken rather than limited.

Repointing later is one line, and no client has to change. That is the whole
reason for aliases.

> These are your models under a familiar name, not Anthropic's. Say so in the
> `description` if anyone else uses the gateway, or they will believe the label.

## 2 · Issue a key that can only reach those models

```
models: ['claude-opus-4.8', 'claude-sonnet-4.8']
```

A key scoped this way is the difference between leaking two aliases and leaking
a fleet. It also makes the model picker honest: discovery lists what the key can
actually call, so nothing is offered that would be refused on use.

Keep this key revealable (`GW_KEY_REVEAL_SECRET` set) and it can be read back
from the console later instead of reissued.

## 3 · Install cloudflared

```bash
sudo mkdir -p --mode=0755 /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
  | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" \
  | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt-get update && sudo apt-get install -y cloudflared
```

The key step is the one that gets skipped: adding the source without the keyring
gives `NO_PUBKEY` and `The repository … is not signed`, because `signed-by=`
points at a file that was never created.

## 4 · Open the tunnel

**Quick tunnel** — nothing to configure, nothing to sign into:

```bash
cloudflared tunnel --url http://localhost:8080
```

It prints `https://<random-words>.trycloudflare.com`. That URL is the base URL.

Two properties to plan around rather than discover:

- **The name changes every restart.** Close the terminal and the URL is gone;
  the next run gets another one and every client has to be edited again.
- **It is on the public internet** for as long as it runs. The API key is what
  stands between it and anyone who learns the URL — which is why step 2 matters.

**Named tunnel**, for anything lasting: a fixed hostname on a domain you own,
and Cloudflare Access in front so only the people you list get as far as the
gateway.

```bash
cloudflared tunnel login          # opens a browser for you to authorise
cloudflared tunnel create litegate
cloudflared tunnel route dns litegate gateway.example.com
cloudflared tunnel run --url http://localhost:8080 litegate
```

### Read the connectivity pre-check

cloudflared tests both regions before connecting and prints a table. A tunnel
can come up while half of it failed:

```
UDP Connectivity  region1  PASS    QUIC connection successful
UDP Connectivity  region2  FAIL    QUIC connection failed
TCP Connectivity  region2  FAIL    HTTP/2 blocked or unreachable
SUMMARY: Environment has critical failures
```

That works — on one leg, with nothing to fail over to. The cause is almost
always **outbound UDP 7844 blocked** to one region. Open it, or drop QUIC and
run over HTTP/2 on both:

```bash
cloudflared tunnel --protocol http2 --url http://localhost:8080
```

## 5 · Fill in Developer Mode

| Field | Value |
|---|---|
| Base URL | `https://<name>.trycloudflare.com` |
| API key | the scoped `lg_sk_…` from step 2 |
| Model discovery | on — it reads `/v1/models` through the same URL |
| Model | `claude-opus-4.8` |

Green looks like this:

```
✓ Model discovery — found 2 models
    claude-opus-4.8, claude-sonnet-4.8
✓ Inference — 1-token completion in 260 ms (claude-sonnet-4.8) · via static key
```

Discovery finding exactly the models the key allows is the sign that both halves
agree. If it lists more than the key can call, the key is wider than intended.

---

## Troubleshooting

| What you see | Why | Fix |
|---|---|---|
| `baseUrl: must use https (or http on loopback)` | A LAN address is neither | Tunnel, or `http://127.0.0.1:8080` if Claude is on the gateway host |
| `net::ERR_CERT_AUTHORITY_INVALID` | Private CA, and the check runs in Chromium which reads the **system** store — `NODE_EXTRA_CA_CERTS` does not reach it | Install the CA system-wide and restart the app, or use a tunnel and avoid certificates entirely |
| `Model discovery — Gateway /v1/models was unreachable` | Same certificate or URL problem, surfacing on the discovery call first | Fix the base URL; the inference test cannot pass until discovery does |
| `NO_PUBKEY …` / `repository is not signed` | The apt source was added without the keyring it references | Install the key (step 3) |
| `SUMMARY: Environment has critical failures` but it connects | One region unreachable, usually UDP 7844 | Open the port, or `--protocol http2` |
| Discovery lists models you did not expect | The key is not scoped | Set `models: [...]` on the key |
| Tunnel URL stopped working | Quick tunnels are ephemeral | Named tunnel with your own hostname |

## What this costs

A tunnel publishes an endpoint that reaches your GPUs. The gateway still
enforces the key, the model scope, the quota and the capability checks on every
request — none of that is bypassed by arriving over a tunnel. But the door is
open while cloudflared runs, so:

- scope the key to the aliases that job needs, and no more
- give it an expiry; a key for an afternoon's test should not outlive the test
- close the tunnel when finished — `Ctrl-C` is the whole procedure
- for anything standing, use a named tunnel with Access in front

See also: [README — pointing clients at it](../README.md#pointing-claude-code-and-other-clients-at-it)
· [DEPLOYMENT.md](DEPLOYMENT.md) for certificates when you would rather stay on
your own network.
