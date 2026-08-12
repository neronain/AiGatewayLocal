# Deployment Guide

Three supported paths. Pick one:

| Path | Use when | Time |
|---|---|---|
| [A — Docker Compose](#path-a--docker-compose) | Production, or any host with Docker | ~10 min |
| [B — Native + systemd](#path-b--native--systemd) | No Docker on the host; single-node install | ~5 min |
| [C — Local staging on OrbStack](#path-c--local-staging-on-orbstack) | Validating the whole system on a laptop, no GPU | ~5 min |

Every command below has been run end-to-end on Ubuntu 24.04 (arm64).

---

## 0. Prerequisites

**Gateway host**

| | Minimum | Recommended |
|---|---|---|
| CPU | 2 cores | 4–8 cores |
| RAM | 2 GB | 8 GB |
| Disk | 10 GB | 50 GB (usage history) |
| OS | Ubuntu 22.04 / 24.04, Debian 12 | Ubuntu 24.04 LTS |
| Python | 3.11+ | 3.12 |

The gateway does no inference — it is I/O bound. It needs **no GPU**.

**Model servers** must already be running and reachable from the gateway host, e.g.

```bash
vllm serve ucbye/Qwen3-Coder-Next-NVFP4-GB10 \
  --host 0.0.0.0 --port 8000 \
  --served-model-name ucbye/Qwen3-Coder-Next-NVFP4-GB10 \
  --max-model-len 262144 \
  --enable-auto-tool-choice --tool-call-parser hermes
```

Verify from the gateway host before going further:

```bash
curl -s http://dgx03:8000/v1/models
```

---

## 1. Configure before you deploy

### 1.1 Point the registry at your real backends

Edit `config/models/*.yaml`. At minimum change `base_url` and `upstream_model`:

```yaml
spec:
  upstream_model: ucbye/Qwen3-Coder-Next-NVFP4-GB10   # must match --served-model-name
  endpoints:
    - name: dgx03
      base_url: http://10.0.0.23:8000                 # your DGX
```

Validate the registry without starting the gateway:

```bash
.venv/bin/python -c "
from pathlib import Path
from app.registry.store import load_snapshot
s = load_snapshot(Path('config'))
print('models:', sorted(s.models))
print('errors:', s.errors or 'none')"
```

A non-empty `errors` list means the gateway will refuse those models — fix before deploying.

### 1.2 Generate secrets

```bash
python3 -c "import secrets; print('GW_API_KEY_PEPPER=' + secrets.token_urlsafe(48))"
python3 -c "import secrets; print('POSTGRES_PASSWORD=' + secrets.token_urlsafe(24))"
```

> **`GW_API_KEY_PEPPER` is not rotatable in place.** Every issued API key is an
> HMAC under it. Changing it invalidates every key at once. Set it before you
> issue keys, then back it up with your other secrets.

---

## Path A — Docker Compose

```bash
git clone https://github.com/neronain/AiGatewayLocal.git
cd AiGatewayLocal
cp .env.example .env
```

Edit `.env` — at minimum `GW_API_KEY_PEPPER`, `POSTGRES_PASSWORD`, and any
`DGX*_API_KEY` your backends require. Then:

```bash
docker compose -f docker/docker-compose.yml up -d --build
```

Capture the bootstrap admin key (printed **once**):

```bash
docker compose -f docker/docker-compose.yml logs gateway | grep -A1 "BOOTSTRAP ADMIN KEY"
```

Verify:

```bash
curl -s http://localhost:8080/healthz
curl -s http://localhost:8080/readyz
```

`readyz` returns 503 until at least one backend passes a health probe — that is
correct behaviour, not a gateway fault. Check `endpoints_healthy` in the body.

### A.1 Production overlay (TLS)

```bash
export GATEWAY_DOMAIN=gateway.university.ac.th
docker compose -f docker/docker-compose.yml -f docker/docker-compose.prod.yml up -d
```

Caddy obtains and renews certificates automatically. For an internal-only
deployment with no public DNS, add `tls internal` inside the site block in
`docker/Caddyfile` to use Caddy's local CA.

The prod overlay also: stops publishing 8080 on the host, makes the container
root filesystem read-only, drops all capabilities, and sets `GW_ENV=production`
(which makes the gateway refuse to start with the default pepper).

---

## Path B — Native + systemd

For a host without Docker. One command:

```bash
git clone https://github.com/neronain/AiGatewayLocal.git
cd AiGatewayLocal
sudo ./scripts/bootstrap.sh
```

It creates the `litegate` system user, installs to `/opt/litegate`, builds a
venv, generates `.env` with a fresh pepper, installs the systemd unit, starts the
service, and prints the bootstrap admin key.

```
==> Gateway is up: http://192.168.139.92:8080
!!  Bootstrap admin key (shown once) - copy it now:
    BOOTSTRAP ADMIN KEY (shown once): edu_sk_...
  Console : http://192.168.139.92:8080/console
  Docs    : http://192.168.139.92:8080/docs
```

Custom install location:

```bash
sudo INSTALL_DIR=/srv/litegate ./scripts/bootstrap.sh
```

Operating it:

```bash
sudo systemctl status  litegate
sudo systemctl restart litegate
sudo journalctl -u litegate -f
```

By default this path uses SQLite, which is fine up to a few hundred members.
For more, switch `GW_DATABASE_URL` in `/opt/litegate/.env` to PostgreSQL
and restart (see §5).

Put nginx in front for TLS — `deploy/nginx/litegate.conf` is ready to use
and already has the SSE-critical settings (`proxy_buffering off`, 900 s timeouts).

---

## Path C — Local staging on OrbStack

Validating the complete system on a Mac, with no GPU and no DGX, using a mock
backend. This is the exact procedure used to verify this release.

```bash
# From the repo on the Mac; the VM sees /Users directly.
orb -m <machine> bash -lc 'sudo /Users/you/AiGatewayLocal/scripts/bootstrap.sh'
```

Start a mock model server inside the VM and point an alias at it:

```bash
orb -m <machine> bash -lc '
  sudo -u litegate bash -c "cd /opt/litegate && \
    nohup .venv/bin/python scripts/mock_backend.py --port 8000 > /tmp/mock.log 2>&1 &"
  sudo sed -i "s#base_url: http://dgx03:8000#base_url: http://127.0.0.1:8000#" \
    /opt/litegate/config/models/coding.yaml'
```

Within `GW_REGISTRY_RELOAD_SECONDS` (30 s default) every worker picks up the
change. Then run the full suite:

```bash
orb -m <machine> bash -lc '
  sudo -u litegate /opt/litegate/.venv/bin/python \
    /opt/litegate/scripts/model_test_suite.py \
    --base-url http://127.0.0.1:8080 --admin-key edu_sk_... --model coding'
```

Expected:

```
  MODEL-001 ... PASS      34 ms  replied 'OK'
  MODEL-002 ... PASS      10 ms  6 chunks
  MODEL-003 ... PASS      13 ms  20 prompt tokens accepted
  MODEL-004 ... PASS      11 ms  called get_weather
  MODEL-005 ... PASS       9 ms  2 call(s)
  MODEL-006 ... SKIP       0 ms  model declares vision=false
  MODEL-007 ... SKIP       0 ms  model declares vision=false
  MODEL-008 ... PASS      18 ms  tool result accepted
  MODEL-009 ... PASS      11 ms  stop_reason=tool_use
  MODEL-010 ... PASS      57 ms  5 ok / 0 throttled / 5 total
```

> **Never leave `mock_backend.py` running on a real deployment.** It returns
> canned text, not inference, and a member cannot tell the difference from the
> response shape.

---

## 2. First-run setup

Export the bootstrap key once:

```bash
export ADMIN_KEY=edu_sk_...
export GW=http://localhost:8080
```

### 2.1 Create a real admin and retire the bootstrap key

```bash
curl -s -X POST $GW/admin/users -H "Authorization: Bearer $ADMIN_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"external_id":"staff001","display_name":"Ajarn Somsak","role":"admin"}'

curl -s -X POST $GW/admin/api-keys -H "Authorization: Bearer $ADMIN_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"<id from above>","name":"admin laptop","expires_in_days":365}'
```

Then revoke the bootstrap key with `DELETE /admin/api-keys/{id}`.

### 2.2 Create a workspace and issue member keys

```bash
python scripts/seed.py \
  --workspace CS101 --name "Intro to Programming" --term 1/2569 \
  --members 6412345678,6412345679,6412345680 \
  --models coding,gemma-vision
```

Keys are printed once. Distribute them over a channel members already trust
(LMS message, not a shared spreadsheet).

### 2.3 Set quota

```bash
curl -s -X POST $GW/admin/quota-policies -H "Authorization: Bearer $ADMIN_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"scope":"workspace","workspace_id":"<workspace id>","window":"day",
       "max_requests":300,"max_input_tokens":1000000,
       "max_output_tokens":200000,"max_images":50}'
```

### 2.4 Certify each model

```bash
python scripts/model_test_suite.py --base-url $GW --admin-key $ADMIN_KEY --model coding
python scripts/model_test_suite.py --base-url $GW --admin-key $ADMIN_KEY --model gemma-vision
```

Results post back automatically; the console shows `READY` / `DEGRADED`.

---

## 3. Member setup

**Python (OpenAI SDK)**

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://gateway.university.ac.th/v1",
    api_key="edu_sk_...",
)

response = client.chat.completions.create(
    model="coding",                       # the alias, never a repository name
    messages=[{"role": "user", "content": "เขียนฟังก์ชัน bubble sort ใน Python"}],
)
print(response.choices[0].message.content)
```

**Claude Code**

```bash
export ANTHROPIC_BASE_URL=https://gateway.university.ac.th
export ANTHROPIC_AUTH_TOKEN=edu_sk_...
export ANTHROPIC_MODEL=coding
claude
```

Only aliases whose `agent_clients.claude_code.enabled` is true will work well;
`/admin/models` shows which.

---

## 4. Operating

### 4.1 Adding a model — no restart, no code change

1. Write `config/models/<alias>.yaml`.
2. Wait for the reload interval, or `POST /admin/registry/reload`.
3. Run the test suite against the new alias.
4. Enable it for a workspace.

> **With multiple uvicorn workers, `POST /admin/registry/reload` only reloads the
> worker that served that request.** The file-watcher is what reloads every
> worker, within `GW_REGISTRY_RELOAD_SECONDS`. Use the endpoint for a fast
> single-worker refresh; rely on the watcher (or restart) for a fleet.

### 4.2 Swapping the model behind an alias

Change `upstream_model` and `base_url`, keep the alias. No member changes
anything. Avoid doing this mid-term while assignments are in flight (PRD §18).

### 4.3 Health

```bash
curl -s $GW/readyz | jq
curl -s $GW/v1/health/endpoints -H "Authorization: Bearer $ADMIN_KEY" | jq
curl -s -X POST $GW/v1/health/probe -H "Authorization: Bearer $ADMIN_KEY" | jq   # probe now
```

---

## 5. Moving from SQLite to PostgreSQL

SQLite is the default for Path B and is adequate for a pilot. Switch when you
have more than a few hundred active members, or more than one gateway instance.

```bash
sudo -u postgres createuser litegate --pwprompt
sudo -u postgres createdb litegate --owner litegate
```

```ini
GW_DATABASE_URL=postgresql+asyncpg://litegate:PASSWORD@localhost:5432/litegate
```

Restart. Tables are created automatically. **Existing SQLite data is not
migrated** — export what you need first (`usage_logs` is the only table worth
carrying over; users and keys should be re-issued).

---

## 5a. Where the models come from

LiteGate serves models; it does not start them. Whatever started the backend —
a shell script, Ansible, or **[LMDS](https://github.com/neronain/AutoDeployDGXProject)**,
the deploy tool built alongside this one — LiteGate needs only a reachable
OpenAI-compatible URL.

If you run LMDS too, three optional links are worth setting up:

1. Put `managed_by` on each endpoint so LiteGate's advice names a runnable
   command instead of `./<controller>.sh`.
2. Point LMDS's brain at this gateway, so its planning and its chat panel run on
   your own models:
   ```bash
   lmds config set-provider openai-compat --base-url http://litegate:8080/v1 --model general
   ```
3. Act on `tools_flag_missing` and `reasoning_not_separated` findings with
   LMDS's `restart --tool-parser` / `--reasoning-parser`, then re-run the suite.

Neither system requires the other. Install one, or both.

---

## 5b. The console assistant

Nothing to install: the assistant uses the models already in the registry, so it
starts working as soon as one chat model is reachable. If none is, the console
hides the chat box instead of showing one that cannot answer.

```ini
# Optional. Empty = pick the best general chat model the caller may use.
GW_ASSISTANT_MODEL=general
```

Pin an alias when the automatic choice is not the one you want — typically to
send assistant traffic to a small fast model rather than the largest one, since
its answers are short and its prompt is not.

Two things to know before promising it to anyone:

* **It spends the caller's quota.** Assistant requests are ordinary requests. A
  member out of quota gets the same rejection from the chat box as from the API,
  which is intentional — a chat box exempt from quota is a quota bypass.
* **Reasoning models narrate.** Unless the backend was started with vLLM's
  `--reasoning-parser`, the chain of thought arrives inside the answer. The
  console strips what it recognises; the real fix is the flag. Run
  `litegate model-test <alias>` and look for `reasoning_not_separated`, which
  carries the command.

---

## 5d. Enrolling a group

A pilot is thirty or so people, each needing a user, a place in a workspace and
a key. By hand that is ninety API calls, and the real risk is not tedium — it is
stopping halfway, retrying, and ending up with duplicate users or members
holding keys nobody can account for.

```bash
export LITEGATE_URL=https://gateway.uni.ac.th
export LITEGATE_ADMIN_KEY=lg_sk_...

python scripts/provision.py members.csv --workspace ai-101 --dry-run
python scripts/provision.py members.csv --workspace ai-101 --out keys.csv
```

```csv
external_id,display_name,email,role
s6412345,Somchai P.,s6412345@uni.ac.th,member
t0001,Dr Anong,anong@uni.ac.th,manager
```

**It is safe to run again.** An existing user is left alone, and a member who
already holds a live key does not get a second one. Add five names to the file,
run it, and five people are enrolled — which is what a pilot actually needs,
because the list changes every week.

The list is validated before anything is created: a duplicate `external_id`, a
missing one, or an unknown role stops the run with nothing changed. Enrolling
the good half leaves nobody able to say who is in and who is not.

**Keys are written as each one is issued**, not gathered up and saved at the
end. The gateway shows a key exactly once, so a failure after issuing thirty of
them would destroy thirty credentials. The output path is opened before the
first key is created, too, so an unwritable path fails while it is still free.

`keys.csv` is created mode 600 and is a file full of credentials. Hand them out,
then delete it. If one goes missing the member is not stuck: revoke it in the
console and run the script again.

It works through the admin API, not the database, so role checks, key format and
the audit log all apply exactly as they would to a human doing it by hand.

---

## 5c. TLS

Some clients refuse plain HTTP outright, and any deployment carrying API keys
should be encrypted regardless. The awkward part of a self-hosted gateway is
that it usually has no public DNS name: it answers on `192.168.x.y` or an
internal hostname, and no public CA will issue a certificate for either.

The tempting shortcut - a one-line `openssl req -x509` - produces a certificate
with no `subjectAltName`, which every current browser and HTTP client rejects.
People then reach for `--insecure` everywhere, which is worse than plain HTTP
because it looks encrypted while verifying nothing.

`scripts/make_tls_cert.sh` creates a small CA of your own and issues a server
certificate from it with the right names, including IP addresses as IP SANs:

```bash
./scripts/make_tls_cert.sh --out ./certs litegate.local 192.168.1.10
sudo install -m 644 certs/litegate.crt /etc/ssl/certs/litegate.crt
sudo install -m 600 certs/litegate.key /etc/ssl/private/litegate.key
sudo cp deploy/nginx/litegate.conf /etc/nginx/sites-available/
sudo ln -s ../sites-available/litegate.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

List **every** name the gateway will be reached by. A certificate for the
hostname does not cover the IP, and clients differ in which they send.

Then install `certs/ca.crt` on the machines that call the gateway — the script
prints the command for each platform. Until you do, they are right to refuse the
connection. Verify without `--insecure`, which is the whole point:

```bash
curl https://192.168.1.10/healthz
```

Re-running the script reuses an existing CA, so certificates issued later stay
trusted and nobody reinstalls anything.

**Two things to change once TLS is in front:**

* **Bind the app to localhost.** With a proxy in front, `--host 0.0.0.0` leaves
  port 8080 reachable directly, which bypasses TLS, the rate limits and the
  `/admin` and `/metrics` restrictions in the nginx config. Change the systemd
  unit to `--host 127.0.0.1`.
* **Nothing else.** The session cookie already sets `Secure` when the request
  arrives over HTTPS, which works behind the proxy because the unit passes
  `--proxy-headers`.

If you *do* have a public hostname, use the Caddy config instead and let it
obtain a real certificate — then none of the CA installation applies.

---

## 6. Backup

```bash
./scripts/backup.sh --out /srv/backups --keep 30
```

One timestamped `.tar.gz` holding the three things a gateway cannot be rebuilt
without. Only one of them is the database:

| In the archive | Why it is there |
|---|---|
| `database.sqlite` / `database.dump` | Members, keys, quota policies, usage history |
| `config/` | The registry. Probably in git — but a restore that needs someone to remember which branch is a restore that goes badly at 3am |
| `.env` | **The pepper.** Every API key is a hash under it |

That last row is the one that matters. Restore a database under a different
`GW_API_KEY_PEPPER` and every key ever issued stops working, silently, with no
way to recover them: every member has to be given a new one. The archive is
therefore a secret — it is written mode 600, and it belongs somewhere with the
same protection as the live `.env`.

SQLite is copied with `.backup`, not `cp`, so a gateway that is serving traffic
cannot produce a torn snapshot. PostgreSQL uses `pg_dump --format=custom`.

**Two things the script deliberately does not do:** copy the archive off the
machine, and prove it restores. Both are yours.

### 6.1 Restoring — rehearse it now

```bash
./scripts/restore.sh /srv/backups/litegate-20260813-020000.tar.gz --into /tmp/rehearsal
```

`--into` restores to a scratch directory and touches nothing that is running,
which is the mode to practise with. `--in-place` overwrites the deployment and
asks you to type `restore` first.

Before writing anything, `--in-place` compares the pepper in the archive with
the one this deployment uses and **refuses** if they differ. Discovering that
mismatch after the data is restored is exactly the failure the script exists to
prevent.

Then prove it, rather than trusting a file of the right size:

```bash
cd /tmp/rehearsal
GW_DATABASE_URL="sqlite+aiosqlite:////tmp/rehearsal/data/gateway.db" GW_API_KEY_PEPPER="$(grep GW_API_KEY_PEPPER .env | cut -d= -f2-)"   uvicorn app.main:app --port 8098

curl -H "Authorization: Bearer <a key that already existed>" http://127.0.0.1:8098/v1/me
```

A key issued **before** the backup authenticating against the restored copy is
the only thing that proves the pepper survived. `/healthz` returning 200 does
not.

Note the four slashes in that SQLite URL. `sqlite:///tmp/x.db` is a *relative*
path; the gateway will happily create an empty database beside it and report
itself healthy while every key is rejected.

**Ownership.** A restore run under `sudo` leaves everything owned by root. The
gateway then reads the database fine and fails on the first write with an error
that says nothing about permissions. The script sets ownership when it can and
says so when it cannot.



| What | Why | How |
|---|---|---|
| `.env` | Losing `GW_API_KEY_PEPPER` invalidates every key | Secret manager, offline copy |
| `config/` | The registry | Git — commit it |
| Database | Users, keys, quota, usage | `pg_dump` nightly, 30-day retention |

```bash
# PostgreSQL
docker compose -f docker/docker-compose.yml exec -T postgres \
  pg_dump -U litegate litegate | gzip > backup-$(date +%F).sql.gz

# SQLite
sudo sqlite3 /opt/litegate/data/gateway.db ".backup '/backup/gateway-$(date +%F).db'"
```

---

## 7. Monitoring

`/metrics` exposes Prometheus data. Restrict it to the management network
(both the nginx and Caddy configs already do).

| Metric | Meaning |
|---|---|
| `litegate_requests_total{path,method,status,model}` | Request counts |
| `litegate_request_duration_seconds{path,model}` | Latency histogram |
| `litegate_requests_in_flight` | Concurrency |
| `litegate_errors_total{code}` | Errors by gateway error code |

Alerts worth having from day one:

| Alert | Condition |
|---|---|
| Gateway down | `/readyz` != 200 for 2 min |
| Backend ejected | `endpoints_healthy < endpoints_total` for 5 min |
| Error surge | `rate(litegate_errors_total{code="UPSTREAM_ERROR"}[5m]) > 0.1` |
| Quota pressure | `QUOTA_EXCEEDED` rate rising near an assignment deadline |
| Estimation drift | share of usage rows with `token_accounting='estimated'` > 20% |

---

## 8. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `readyz` 503, `endpoints_healthy: 0` | Backends unreachable, or `health_path` wrong | `curl <base_url>/health` from the gateway host |
| `MODEL_NOT_FOUND` | Alias typo or registry error | `GET /admin/models` — check `errors[]` |
| `UPSTREAM_ERROR` with `upstream_status: 404` | `upstream_model` ≠ vLLM's `--served-model-name` | Make them identical |
| Streaming arrives all at once | Proxy buffering | `proxy_buffering off` (nginx) / `flush_interval -1` (Caddy) |
| `MODEL_CAPABILITY_NOT_SUPPORTED` on a vision model | `capabilities.vision` or the endpoint's `modalities.image` is false | Both must be true |
| Boot fails: `error parsing value for field "cors_origins"` | Old build; fixed in 1.3.0 | Upgrade |
| Every key rejected after a config change | `GW_API_KEY_PEPPER` changed | Restore the old pepper, or re-issue all keys |
| `413` on a legitimate image | Proxy body limit below `max_image_size_mb × max_images` | Raise `client_max_body_size` |

Diagnostics:

```bash
sudo journalctl -u litegate -n 100 --no-pager     # native
docker compose -f docker/docker-compose.yml logs --tail 100 gateway
```

Every error response carries `request_id`; grep the logs for it.

---

## 9. Upgrading

### Upgrading from EduLLM Gateway (pre-1.4)

The product was renamed to LiteGate and the vocabulary was made
sector-neutral. Nothing in a running deployment has to change on upgrade day:

| Was | Is | On upgrade |
|---|---|---|
| `edu_sk_...` API keys | `lg_sk_...` for newly issued keys | Existing keys keep working — a key is verified by HMAC over the whole string, so the prefix is only a label |
| `apiVersion: edullm.gateway/v1` | `litegate.dev/v1` | Both accepted; no model file needs editing |
| `visibility: student` / role `instructor` | `member` / `manager` | Both accepted; rows are not rewritten |
| `x-edullm-model` response header | `x-litegate-model` | Both sent for one release, then the old one is removed |
| systemd unit `edullm-gateway` | `litegate` | Rename at your convenience; the old unit keeps running |
| Prometheus `edullm_*` metrics | `litegate_*` | **Not aliased** — update dashboards, this is the one thing that changes immediately |

Database table and column names are unchanged. They are not part of the product
surface, and renaming them would force a migration for nothing a user can see.

### Routine upgrade


```bash
cd AiGatewayLocal && git pull

# Docker
docker compose -f docker/docker-compose.yml up -d --build

# Native
sudo ./scripts/bootstrap.sh          # preserves the existing .env
```

`create_all` only adds missing tables; it never alters existing ones. A release
that changes a column will say so in its notes and ship a migration.

Roll back by checking out the previous tag and repeating. The database schema is
additive, so an older gateway runs against a newer database.
