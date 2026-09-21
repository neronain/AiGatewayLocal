# Deployment Guide

**Just evaluating it?** Do not read this yet — `./install.sh --demo` from the
repository root brings the whole thing up on one machine in a couple of minutes,
with a stand-in backend so no GPU is needed. This guide is for the install that
has to survive a reboot.

Three supported paths. Pick one:

| Path | Use when | Time |
|---|---|---|
| [A — Docker Compose](#path-a--docker-compose) | Production, or any host with Docker | ~10 min |
| [B — Native + systemd](#path-b--native--systemd) | No Docker on the host; single-node install | ~5 min |
| [C — Local staging on OrbStack](#path-c--local-staging-on-orbstack) | Validating the whole system on a laptop, no GPU | ~5 min |

Every command below has been run end-to-end on Ubuntu 24.04 (arm64).

---

## Forgot the console password

Changing your own password is in the console: **My account → Change password**.
It asks for the current one, which is no help when that is the thing you have
lost. On the machine running the gateway:

```bash
cd /opt/litegate                                    # wherever it is installed
.venv/bin/python scripts/reset_password.py --list   # which accounts exist
.venv/bin/python scripts/reset_password.py admin    # new password, printed once
```

Pass `--password '…'` to choose one instead of taking a generated one. Either
way every existing session for that account is signed out — somebody resetting a
password usually suspects the old one is known to someone else, and leaving the
sessions alive would undo the point of resetting it.

Run it on the gateway host, not from a laptop: it reads the same `.env` the
service does, and pointed at a different database it will report success against
an account nobody signs in with.

`GW_ADMIN_PASSWORD` does **not** help here. It applies when the administrator is
first created, and to an account that somehow has no password at all — it is not
a way to overwrite one that already exists.

---

## 0. Prerequisites

**Gateway host**

| | Minimum | Recommended |
|---|---|---|
| CPU | 2 cores | 4–8 cores |
| RAM | 2 GB | 8 GB |
| Disk | 10 GB | 50 GB (usage history) |
| OS | Ubuntu 22.04 / 24.04 / 25.04 / 25.10, Debian 12 | Ubuntu 24.04 LTS |
| Python | 3.10 – 3.13 | 3.12 |

The gateway does no inference — it is I/O bound. It needs **no GPU**.

**The Python floor is 3.10 because that is what Ubuntu 22.04 hands you.** Every
supported release is installable with the `python3` already on the box — no PPA,
no `deadsnakes`, no source build:

| Release | `apt install python3` gives | CI |
|---|---|---|
| Ubuntu 22.04 LTS | 3.10 | ✅ tested |
| Debian 12 bookworm | 3.11 | ✅ tested |
| Ubuntu 24.04 LTS | 3.12 | ✅ tested |
| Ubuntu 25.04 | 3.13 | ✅ tested |
| Ubuntu 25.10 | 3.13 | ✅ tested |

`.github/workflows/ci.yml` runs the suite on 3.10, 3.11, 3.12 and 3.13, and a
separate `install-smoke` job does a clean `pip install .` and boots the app on
each — that job is the one that would have caught the 22.04 breakage, where
`requires-python = ">=3.11"` made `pip install` refuse before a single test ran.

3.9 and older are **not** supported, and `pip` now says so before installing
anything. The floor is a hard one, not a policy: the ORM models annotate columns
as `Mapped[str | None]`, and SQLAlchemy resolves those annotations at import
time, so on 3.9 the very first import dies with

```
sqlalchemy.orm.exc.MappedAnnotationError: Could not resolve all types within
mapped annotation: "Mapped[str | None]"
```

PEP 604 unions only became evaluable at runtime in 3.10.

### Optional extras

A plain install is complete: everything below is a trade, not a fix for
something missing. Both are ordinary PyPI extras, so an air-gapped site that
cannot add packages keeps working exactly as it does today.

| Extra | Install | What it buys | What you give up by skipping it |
|---|---|---|---|
| `postgres` | `pip install 'litegate[postgres]'` | The `asyncpg` driver, required by a `postgresql+asyncpg://` `GW_DATABASE_URL` | Nothing, unless you are moving to PostgreSQL (§5) |
| `speed` | `pip install 'litegate[speed]'` | `orjson`. JSON encoding is most of the CPU the gateway spends per request; measured ~25× faster on a 59 KB completion body and ~5× on a streaming chunk | Some CPU. Responses are **byte-for-byte identical** either way — the test suite asserts that in both modes |

Both together:

```bash
pip install 'litegate[postgres,speed]'
```

`orjson` ships as a compiled wheel. On a platform with no wheel it will try to
build; if that is not something you want on the host, leave it out — the gateway
falls back to the standard library on its own and logs nothing, because nothing
is wrong.

**Model servers** must already be running and reachable from the gateway host, e.g.

```bash
vllm serve ucbye/Qwen3-Coder-Next-NVFP4-GB10 \
  --host 0.0.0.0 --port 8000 \
  --served-model-name ucbye/Qwen3-Coder-Next-NVFP4-GB10 \
  --max-model-len 262144 \
  --enable-auto-tool-choice --tool-call-parser qwen3_coder
```

> **The parser has to match the model family.** A wrong one does not error — it
> silently fails to parse, and the tool call arrives as ordinary text, which the
> client reads as the model refusing to use tools. Qwen-family models (this one,
> and Nemotron-3) need `qwen3_coder`; `hermes` expects JSON and will not read
> their XML. Gemma 4 emits `<|tool_call>call:name{…}` and needs `gemma4` —
> `hermes` is the default guess and is wrong for it. Ask the engine for the names
> it accepts rather than guessing from filenames — see
> [LMDS · เลือก parser ตัวไหน][lmds-parsers]:
>
> ```bash
> # the authoritative list, from the engine that will use it
> docker exec <container> python3 -c \
>   "from vllm.tool_parsers import *; import vllm.tool_parsers as p; print(p.__file__)"
> ls $(dirname <that path>)   # one *_tool_parser.py per supported family
> ```
>
> A mismatch is worse than no tools at all: the caller receives the raw call as
> the answer. The model test now catches it — when a backend answers 200 with no
> `tool_calls` but the reply *contains* tool-call syntax, the finding is
> `tool_parser_mismatch` (warning) and names the parser that would have read it,
> instead of the older, vaguer "maybe it has no tool template".

[lmds-parsers]: https://github.com/neronain/AutoDeployDGXProject/blob/main/docs/USAGE.md

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
`DGX*_API_KEY` your backends require.

**Do [§1.1](#11-point-the-registry-at-your-real-backends) before you start it.**
Compose mounts `config/` from the repository read-only, so the sample models go
in exactly as they ship — pointed at `dgx01`/`dgx02`/`dgx03`, which are machine
names on our network, not yours. Started as-is, the health prober reports
connection failures against all three every few seconds and the first thing you
read is a log full of errors about hosts you have never heard of. The native
installer disables them for you; on this path the file is read-only and only you
can fix it. Repoint them, or set `enabled: false` under `spec:` in each file.

Then:

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
export GATEWAY_DOMAIN=gateway.example.com
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
service, and prints both ways in — the console sign-in and the API key.

```
==> Gateway is up: http://192.168.139.92:8080

  Console sign-in (shown once)
    URL      : http://192.168.139.92:8080/console
    username : admin
    password : ...

!!  Bootstrap admin key (shown once) - copy it now:
    BOOTSTRAP ADMIN KEY (shown once): lg_sk_...

  No models are available yet — the samples that ship with the repo point at
  our machines and were disabled. Add yours in the console: Models → Add.
```

**No models on a fresh install, on purpose.** The sample files in
`config/models/` name hosts on our network. Left enabled they are probed every
few seconds, and the first thing a new operator sees is a screenful of
connection errors against machines they have never heard of. `bootstrap.sh`
disables them at the model level (`spec.enabled: false`) after copying, which
also stops the health prober from touching them. To use them as a starting
point, edit the endpoint addresses in `/opt/litegate/config/models/*.yaml` and
set `enabled: true` — no restart needed, the registry reloads on change.

Custom install location:

```bash
sudo INSTALL_DIR=/srv/litegate ./scripts/bootstrap.sh
```

**The console writes the registry, so the unit lets it.** `ProtectSystem=strict`
makes the whole filesystem read-only to the service and opens only what
`ReadWritePaths=` lists — `data/`, `logs/`, and `config/`. `config/` is on that
list because adding a model in the console writes
`config/models/<alias>.yaml`, and the enable/disable switch rewrites the same
file. Drop it and a fresh install shows *"the registry is read-only"* with the
Save button greyed out, which is not a security posture anyone chose — it just
looks like the product is broken. (This never shows up on a dev box: containers
run with systemd's sandboxing disabled, so the same unit behaves differently
there than on a real host.)

If you keep the registry in git and want the file tree immutable at runtime,
say so at install time:

```bash
sudo REGISTRY_READONLY=1 ./scripts/bootstrap.sh
```

The console then offers **Preview YAML** instead of Save, and tells the operator
to commit the file. Everything else about the model editor works the same.

Operating it:

```bash
sudo systemctl status  litegate
sudo systemctl restart litegate
sudo journalctl -u litegate -f
```

By default this path uses SQLite, which is fine up to a few hundred members.
For more, switch `GW_DATABASE_URL` in `/opt/litegate/.env` to PostgreSQL
and restart (see §5).

For TLS, run the installer rather than writing the nginx config by hand:

```bash
sudo scripts/install_tls.sh
```

It renders `deploy/nginx/litegate.conf` — which already carries the SSE-critical
settings (`proxy_buffering off`, 900 s timeouts) — for the nginx version actually
on the host, issues a certificate covering this machine's names, and reloads.
Full detail in [§5c](#5c-tls).

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

## Path D — Containers: LXC and Docker

Many sites run the gateway in an LXC container or in Docker rather than on a
dedicated machine. Both work. What changes is the contract with the host, not
the code.

**The gateway itself is blind to host shape, on purpose.** It never reads
`/proc` or `/sys`, never calls `os.cpu_count()` or `sched_getaffinity`, never
shells out to `nproc`, `free`, `uname` or `hostname`, and has no `psutil`
dependency. So a cgroup CPU or memory limit can never cause it to mis-detect
anything — the classic container failure mode simply does not apply. The flip
side is that **nothing auto-tunes**: the worker count is a constant, and on a
small container it is your job to lower it (see *Sizing* below).

### Which install path works where

| | Docker | LXC with systemd | Unprivileged LXC |
|---|---|---|---|
| `docker/docker-compose.yml` | ✅ the intended path | — | — |
| `sudo scripts/bootstrap.sh` | ❌ no systemd | ✅ | ⚠️ see *Hardening* |
| `./install.sh` (venv, no service) | ✅ | ✅ | ✅ |

`scripts/bootstrap.sh` installs a systemd unit and reads the first-run
credentials back out of the journal, so it needs a running systemd. It now
checks `/run/systemd/system` **before** it does anything and exits with the
alternatives. It used to discover this at `systemctl daemon-reload` on the
second-to-last line, having already created a system user, copied the tree,
built a venv, written `.env` and dropped a unit into `/etc/systemd/system` —
leaving a half-installed machine and a message about D-Bus.

### systemd in LXC

**You do not need `loginctl enable-linger`.** The unit is a *system* service
(`User=litegate`, `WantedBy=multi-user.target`), not a user service, so linger —
which only keeps a user manager alive after logout — has no effect on it. Advice
to enable linger for this gateway is for a different product.

The unit is heavily sandboxed, and several of those directives need privileges
an **unprivileged** LXC container may not have. When they cannot be applied the
service fails to start with `status=226/NAMESPACE`, and the error names the
namespace rather than the directive that caused it:

| Directive | In unprivileged LXC |
|---|---|
| `PrivateDevices=`, `ProtectKernelTunables=`, `ProtectKernelModules=`, `ProtectControlGroups=` | may fail the unit — these remount parts of `/proc` and `/sys` |
| `LimitNOFILE=65535` | fails at exec if the container's hard limit is lower |
| `MemoryMax=4G` | **silently ignored** unless the `memory` controller is delegated into the container; the host OOM killer becomes the real limit |
| `ProtectSystem=strict`, `ProtectHome=`, `NoNewPrivileges=`, `PrivateTmp=` | work normally |

Relax only what your container actually rejects, with a drop-in — never by
editing the shipped unit, which an upgrade overwrites:

```bash
sudo systemctl edit litegate
```

```ini
[Service]
# Only the lines your container actually needs.
PrivateDevices=false
ProtectKernelTunables=false
ProtectKernelModules=false
ProtectControlGroups=false
# Empty value = clear the setting inherited from the unit.
LimitNOFILE=
MemoryMax=
```

Then `sudo systemctl daemon-reload && sudo systemctl restart litegate`, and
confirm with `systemd-analyze security litegate` what you gave up.

If you clear `MemoryMax=`, set the ceiling on the container instead — the
gateway has no internal memory cap of its own.

### Sizing: the worker count is a constant, everywhere

Four processes, hard-coded in four separate places. None of them is derived from
the CPU count, so a 1-vCPU container still starts four:

| Where | Setting |
|---|---|
| `deploy/systemd/litegate.service` | `--workers 4` in `ExecStart` |
| `docker/Dockerfile` | `--workers 4` in `CMD` |
| `docker/docker-compose.prod.yml` | `--workers ${GW_WORKERS:-8}` |
| `app/config.py` (`GW_WORKERS`) | read **only** by the `litegate` console script |

> **`GW_WORKERS` does less than it looks like it does.** It is read by
> `app.main:run` — the `litegate` entry point — and neither the systemd unit nor
> the image uses that entry point; both invoke `uvicorn` directly. Setting
> `GW_WORKERS=16` in `.env` on a systemd install still gives you four. To change
> it there, edit `ExecStart` (via `systemctl edit`). The production compose
> overlay now passes the value through on the command line, so `GW_WORKERS` does
> work under `docker compose -f docker-compose.yml -f docker-compose.prod.yml`.

On a container with 1–2 vCPU, drop to `--workers 2` or `1`. Four uvicorn workers
on one core buys nothing and multiplies the per-process state described next.

### Per-process state: set `GW_REDIS_URL` if you run more than one worker

Two things are counted **per process** unless Redis is configured:

* **`max_concurrency` on an endpoint.** Without Redis each worker keeps its own
  counter, so `max_concurrency: 1` with four workers means **four** concurrent
  requests arriving at a backend that was told to expect one. See the note at
  the top of `app/core/inflight.py`.
* **The response cache** (off by default) — a hit only lands on the worker that
  produced it.

This is the one place where the container path is *better* than bare metal:
`docker/docker-compose.yml` ships a Redis service and sets `GW_REDIS_URL`,
while `scripts/bootstrap.sh` writes `GW_REDIS_URL=` (empty) next to
`GW_WORKERS=4`. **A default native install is the one that oversubscribes its
backends.** Either point `GW_REDIS_URL` at a Redis, or drop to one worker.

Quota counters are *not* affected: they are a single atomic
`UPDATE ... SET n = n + :x` in the database and are correct with any number of
workers, Redis or not.

### Read-only filesystems

Two directories are written at runtime:

| Path | Written when | Must survive a restart |
|---|---|---|
| `data/` | an admin saves a provider API key (`data/secrets.json`), tool mirroring, and the SQLite database if you use one | **yes** |
| `config/models/` | an admin adds, edits, enables or disables a model in the console | yes (or keep the registry in git) |

Under `docker-compose.prod.yml` the container runs with `read_only: true`. A
named volume at `/app/data` is now declared in `docker-compose.yml`, so provider
keys persist and are writable; without it, saving a key failed with nothing but
*"An internal error occurred."* Both write paths now report the real cause and
the fix instead:

```
Cannot save the provider key: the directory is on a read-only mount (/app/data).
· Docker: `read_only: true` needs a writable mount for the data directory —
  add a named volume or a tmpfs at /app/data
· systemd install: add that path to ReadWritePaths= in
  /etc/systemd/system/litegate.service ...
```

**Do not use a `tmpfs` for `data/`** — `secrets.json` holds provider API keys and
must outlive the container.

The registry is mounted `:ro` in `docker-compose.yml` on purpose (a compromised
container cannot rewrite which backends it routes to), so the console's *Save*
is greyed out and *Preview YAML* is the workflow. Note the asymmetry: the
systemd install leaves `config/` **writable** by default and offers
`REGISTRY_READONLY=1` to opt out. Same product, opposite defaults.

### Networking

* **The address printed at the end of `bootstrap.sh` is the container's own.**
  It comes from `hostname -I`; inside a container that is the veth or bridge
  address, which is usually not reachable from anywhere but the host. The script
  now says so when it detects a container. Reach the console through the host's
  port forward.
* **Backends are resolved from inside the container.** The sample registry points
  at `dgx01`/`dgx02`/`dgx03`; a Docker container has its own resolver and will
  not find them. `docker-compose.yml` adds
  `extra_hosts: ["host.docker.internal:host-gateway"]`, which covers backends on
  the Docker host but not LAN-resident DGX nodes — use IP addresses or a
  resolvable FQDN. `bootstrap.sh` disables the samples at install time; the image
  ships them enabled, so a first `docker compose up` logs resolver errors until
  you point them at real machines.
* **Client IPs behind a proxy.** `uvicorn` only honours `X-Forwarded-For` from
  peers it trusts. The systemd unit and the image both pass
  `--forwarded-allow-ips '*'`, which is right when only the proxy can reach the
  port. The `litegate` console script now takes the same setting from
  `GW_FORWARDED_ALLOW_IPS` (default `127.0.0.1`, matching uvicorn). In a
  container the reverse proxy is a *different* container, so its address is on
  the bridge network, not loopback — leave the default there and every admin
  audit row records the proxy instead of the caller.

### Shutdown and streaming

`deploy/systemd/litegate.service` allows `TimeoutStopSec=30` so in-flight
streaming responses can finish. Docker's default grace period is **10 s**, and
the shutdown path spends up to 5 s of it draining billing finalizers, so a long
completion was cut mid-token on every `docker compose restart`.
`docker-compose.yml` now sets `stop_grace_period: 30s` to match. If you run the
image with plain `docker run`, pass `--stop-timeout 30`.

`tini` is the image's entrypoint, which matters because `--workers 4` makes
uvicorn a supervisor with forked children; it reaps them and forwards SIGTERM.
The application installs no signal handlers of its own and makes no assumption
about being PID 1.

### Backups from inside the image

`scripts/backup.sh` and `scripts/restore.sh` are copied into the image but
**cannot run there**: the runtime stage installs only `curl` and `tini`, so
neither `sqlite3` nor `pg_dump` exists. Back up from the host instead — dump
Postgres with a `postgres:16-alpine` sidecar (`docker compose exec postgres
pg_dump ...`) and copy the SQLite file out with `docker cp`. On a native install
both tools are present and the scripts work as documented in §6.

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
  --workspace ACME-PLATFORM --name "Platform team" --term 2026-H1 \
  --members 6412345678,6412345679,6412345680 \
  --models coding,gemma-vision
```

Keys are printed once. Distribute them over a channel members already trust
(a direct message, not a shared spreadsheet).

`--term` fills `Workspace.term`, a free-text label on the workspace — a period,
a cost centre, a contract reference, whatever you sort by. **It is a label and
nothing more:** no expiry, no access decision and no quota window is derived
from it. The name is left over from the product's first deployment; see
[the data-model note](#a-note-on-the-schema-vocabulary).

**Being in a workspace is what decides which models someone may call.** The
rules, in the order they narrow:

| Situation | What they can call |
|---|---|
| Key issued *for* a workspace | that workspace's models — the owner's groups do not apply |
| In one or more workspaces | everything those workspaces allow, added together |
| In no workspace at all | everything their role can see |
| A list written on the key | narrowed to that list, on top of the above |

`manager` is scoped like a member, to the workspaces they are in: someone who
looks after CS101 has no business handing out ART200's models. `admin` is not
scoped — they run the gateway, and the alternative is adding them to every
workspace forever.

**A manager's admin powers are scoped the same way.** They see the people, keys
and usage of their own workspaces, and can add members, set models and issue
keys only there — and only naming models they can use themselves, or enabling a
model for your own workspace would be a way of granting it to yourself.

A manager who is in no workspace manages nothing, which is the opposite default
from model access and deliberate: promoting somebody should not quietly hand
them the whole organisation. Put them in their workspaces and they can work.

**Changing what a key may call, after it is in circulation.** The scope was set
once when the key was issued and could never be revisited, so adding a model
meant revoking a working credential and asking everybody to paste a new one.
People responded rationally, by issuing keys wide enough that they would never
have to — which is the opposite of what the scope is for.

```bash
curl -s -X PATCH $GW/admin/api-keys/<key id> -H "Authorization: Bearer $ADMIN_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"models":["claude-opus-4.8","claude-sonnet-4.8","claude-haiku-4.8"]}'
```

Or **Access & Keys → the key's `model` button**, which lists what is in the
registry as checkboxes — a mistyped alias is a key that reaches nothing and says
nothing until somebody tries to use it.

Send the whole list you want, not a delta. `[]` removes the restriction, which
*widens* the key, so the console spells that out before saving. `days` and
`models` travel in the same request and neither disturbs the other. A manager is
held to the same bar as when issuing: only models they could call themselves.

**Handing the same models to many workspaces.** Ticking four models into
twenty workspaces means eighty clicks, and adding a fifth means visiting all
twenty again.
Name the set once instead:

```bash
curl -s -X POST $GW/admin/access-groups -H "Authorization: Bearer $ADMIN_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"name":"coding-set","models":["coding","coder-next"]}'
```

Then give the bundle to a workspace alongside (or instead of) individual models
— `POST /admin/workspaces/{id}/models` takes `access_groups` in the same call.
Editing the bundle reaches every workspace holding it, which the response counts for
you; disabling it takes its models away everywhere at once, reversibly.

A bundle is a shorter way of *writing* a rule, never a new one: what it expands
to is added to the models ticked on the workspace, and then narrowed by everything
that narrowed before. Only an admin can define one, and a manager can only hand
out bundles whose models they could call themselves — otherwise granting
yourself a bundle would be the way around every other check.

**What each role can do — and why there is no screen to change it.**

Three fixed levels, decided in code so that this gateway means the same thing at
every site that installs it:

| Role | May call | May administer |
|---|---|---|
| `member` | whatever their workspaces allow | themselves only — their own keys and password |
| `manager` | whatever their workspaces allow, *same as a member* | only the workspaces they belong to: add and remove people, set that workspace's models, issue keys to its members, read its usage. **Cannot hand out a model they cannot call themselves.** |
| `admin` | every model in the registry | the whole gateway — registry, bundles, quota, every workspace and person |

There is deliberately no role editor. What you configure is *who is in which
workspace* (People), *what that workspace may call* (Workspaces and Access
groups), *how far a key is narrowed* (Issue an API key) and *how much may be
used* (Quota). Roles decide the shape of someone's authority; those four decide
its extent.

A manager who is in no workspace administers nothing. Put them in their
workspaces first — the opposite default from model access, so that promoting
somebody does not quietly hand them the organisation.

**Defaults for a whole workspace.** These are a different question from the
allowed models, and the console keeps them apart: the checkboxes are *what this
workspace may call*, the row beneath is *what a key issued to a new member
starts as*. They can contradict each other — a default naming something the
workspace cannot call produces a key whose own list and whose workspace have
nothing in common, so it
can call nothing at all and the owner finds out by being refused. The default
can therefore only name models the workspace actually reaches, whether ticked
directly or supplied by a bundle, and the API refuses the rest.

**Setting those defaults.** Set `default_member_models`,
`default_access_groups` and `default_key_days` on the workspace and a key issued
to one of its members starts there, instead of thirty keys being typed by hand
with one of them mistyped. Only blanks are filled: sending `"models": []`
explicitly means "unrestricted" and is left alone. The issue response reports
what was filled in and which workspace it came from, because a default that applies
silently is a setting nobody knows they have.

**Marking service keys.** `"kind": "service"` on a key changes no rule; it exists
so a CI token and a developer's laptop key stop looking identical in a list of two
hundred, which is what turns an audit into an afternoon.

**Putting a workspace on hold.** `PATCH /admin/workspaces/{id}/status` with
`suspended` stops it granting any models; `active` brings it back. Nobody's key
is touched, which is the difference from revoking them — a finished engagement,
a project between phases, or a team under investigation should not destroy
credentials that will be needed again. Somebody who is also in another
workspace keeps that one.

Two consequences worth knowing before you use it:

* **A workspace with no models allows nothing.** Adding someone to an empty one
  takes their access away rather than granting any. The join response says so.
* **In no group means unrestricted, not blocked.** A deployment that has not
  started using workspaces behaves exactly as it did before.

#### Upgrading a gateway that already has keys in circulation

Turning this on re-permissions keys that are already out there. Before you
upgrade, ask who it would affect:

```bash
python scripts/access_change_report.py --db "$GW_DATABASE_URL"
```

It writes nothing and exits 1 if anyone would lose access. If the answer is not
"nobody", set `membership_grants_models: false` in `gateway.yaml`, upgrade,
sort out the workspaces, then switch it on.

### 2.3 Set quota

```bash
curl -s -X POST $GW/admin/quota-policies -H "Authorization: Bearer $ADMIN_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"scope":"workspace","workspace_id":"<workspace id>","window":"day",
       "max_requests":300,"max_input_tokens":1000000,
       "max_output_tokens":200000,"max_images":50}'
```

#### Handing an allowance back

A limit that was right can still leave the wrong person stuck: one runaway agent
loop spends a month's quota in an afternoon. Raising the limit to unblock them
changes the rule for everyone, permanently, because of one accident.

```bash
curl -s -X POST $GW/admin/users/<user id>/quota/reset \
  -H "Authorization: Bearer $ADMIN_KEY"
```

Or **Access & Keys → People → คืนโควตา**, which is where you will actually reach
for it.

It clears the counter for the current window and nothing else. Usage records are
a separate ledger and are what the reports read, so what was spent still shows
in `/admin/usage/summary` afterwards. The reset is written to the audit log with
the figures it cleared.

Admin only. A manager can already relax a limit for their own workspace with a dated
policy; returning an allowance somebody has already spent is a different act and
belongs at the top.

> With Redis configured, the reset clears **both** ledgers. Clearing only Redis
> does nothing lasting: the next read misses, decides an earlier outage may have
> left counts in the database, and reseeds from there — the number returns and
> the button looks broken. If Redis is unreachable the call fails rather than
> reporting a success it cannot deliver.

### 2.3b Stop a burst, not just a spree

A daily quota stops somebody spending a month's worth in a week. It does nothing
about a team of forty pressing send in the same minute — the machines queue and
the last person waits minutes for a first token, while their own daily figure is
barely touched.

```bash
curl -s -X POST $GW/admin/quota-policies -H "Authorization: Bearer $ADMIN_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"scope":"workspace","workspace_id":"<id>","window":"day",
       "max_requests":300,
       "max_requests_per_minute":6,"max_tokens_per_minute":40000}'
```

A policy can carry a `name` — six of them told apart only by reading their
scope and target is six nobody dares touch — and can apply to a whole **bundle**
instead of one alias:

```bash
  -d '{"name":"heavy GPU models","access_group_id":"<id>",
       "window":"day","max_requests_per_minute":10}'
```

A limit meant for four models was four policies to write and four to remember
to change; an access group is already a named set, so that is what a policy
points at. Name one alias or one bundle, not both. Precedence puts a named alias
above a bundle containing it, for the same reason a rule about one person beats
a rule about their team.

A policy can also carry `expires_in_days`. Somebody who needs a bigger
allowance for three days is one policy that removes itself, rather than one
somebody has to remember to delete — and remembering is what does not happen.
An expired policy is skipped, not deleted: the row is the record of what was
granted and when, which is the first thing anyone asks afterwards.

**When the time runs out and the work has not.** An expired key stops working
and is kept, which is right, but re-issuing means everybody pastes a new key
into their configuration again — the credential was never the problem, the date
was. `PATCH /admin/api-keys/{id}` with `{"days": 7}` moves it, counting from
today so a key that lapsed last month gets a full week rather than a date
already gone. `null` removes the expiry. A revoked key cannot be brought back
this way: revoking is meant to be final, and an extend that undid it would make
it something else. Quota policies extend the same way.

Two questions that look alike and are not. *Which models may this person call*
is answered by their workspace and by the list on their key. *How much may they
use* is the quota. A per-person quota normally leaves the model field on **all
models**, so it covers whatever they are allowed to call; name one model or one
bundle only when the limit is really about that model.

Counted **per person**, not per key: ten tabs is still one person, and counting
per key would make issuing yourself another one a way to get more. `0` means
unlimited and is the default — a rate limit nobody chose is one that refuses
somebody mid-lesson for a reason nobody can explain.

The burst is checked before the window, so when both are over the reply is the
useful one: `It clears in 32 seconds`, with `Retry-After` to match, rather than
telling them to come back tomorrow.

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
    base_url="https://gateway.example.com/v1",
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
export ANTHROPIC_BASE_URL=https://gateway.example.com
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
anything. Avoid doing this mid-project while long-running jobs are in flight
(PRD §18).

### 4.3 Health

```bash
curl -s $GW/readyz | jq
curl -s $GW/v1/health/endpoints -H "Authorization: Bearer $ADMIN_KEY" | jq
curl -s -X POST $GW/v1/health/probe -H "Authorization: Bearer $ADMIN_KEY" | jq   # probe now
```

---

## 5. Moving from SQLite to PostgreSQL

SQLite is the default and stays the default. A single-machine deployment needs
no database server, and nothing about it is deprecated — most installs should
never read this section.

Switch when one of these is true:

| Reason | Why SQLite cannot do it |
|---|---|
| More than one gateway **machine** | SQLite is a file. Two hosts cannot share one, and a network filesystem breaks its locking. |
| You need **replication or PITR backups** | SQLite has neither. `.backup` is a full copy, taken at whatever moment you run it. |
| More than a few hundred active members | One writer at a time. Under load this shows up as `database is locked`, not as gradual slowdown. |

Running four uvicorn workers on **one** machine is not on that list — SQLite in
WAL mode handles it, which is what the gateway already configures. Set
`GW_REDIS_URL` for that case (§5f); it matters more than the database choice.

### Installing the driver

`asyncpg` is an **optional** dependency. It is deliberately not installed by
default, so an air-gapped or single-machine customer never has to carry it:

```bash
pip install 'litegate[postgres]'        # or: pip install asyncpg
```

Start with a `postgresql+asyncpg://` URL and no driver and the gateway refuses
to boot with a message naming that command — not a bare `ModuleNotFoundError`.

### Setting it up

```bash
sudo -u postgres createuser litegate --pwprompt
sudo -u postgres createdb  litegate --owner litegate
```

```ini
GW_DATABASE_URL=postgresql+asyncpg://litegate:PASSWORD@localhost:5432/litegate
```

Restart. Tables are created automatically, exactly as on SQLite.

### Moving the data across

**Existing SQLite data is not migrated for you.** Decide per table:

| Table | Carry over? |
|---|---|
| `usage_logs` | Yes, if you want history in the reports. It is the only large one. |
| `quota_counters` | No. Counters rebuild themselves within one window. |
| `users`, `api_keys` | Re-issue. Key material is hashed, so an export is only useful if you copy the rows verbatim — and rotating credentials during a database move is the better habit anyway. |
| model registry | Nothing to do. It lives in `config/*.yaml`, not in the database. |

A `usage_logs` copy, with the gateway **stopped on both ends**:

```bash
sqlite3 -header -csv data/gateway.db \
  "SELECT * FROM usage_logs ORDER BY ts;" > usage_logs.csv

psql "$GW_DATABASE_URL_PSQL" -c "\copy usage_logs FROM 'usage_logs.csv' WITH (FORMAT csv, HEADER true)"
```

`$GW_DATABASE_URL_PSQL` is the same URL with `+asyncpg` removed — `psql` speaks
its own protocol and does not know about SQLAlchemy driver names.

Rehearse it on a copy first, and check the row count matches before pointing
production at it.

### Things that behave differently, and what the gateway does about them

These are the places where the two databases genuinely disagree. They are
handled in the code; the list is here so that a reviewer can check the claim
rather than take it on faith.

| Difference | Where it shows up | What we do |
|---|---|---|
| `date(ts)` cuts the day in the **session's timezone** on PostgreSQL, but SQLite stores UTC and cuts on UTC | Daily usage charts (`/v1/usage/daily`, `/admin/usage/daily`) | `app/db/dialect.py::utc_date` forces UTC on both, so a report means the same thing everywhere |
| PostgreSQL rejects `DEFAULT 0` on a `BOOLEAN` column; SQLite accepts it | The additive schema upgrade run at startup | Defaults are rendered by the dialect's own literal processor: `true`/`false` on PostgreSQL, `1`/`0` on SQLite |
| `WINDOW` is a reserved word in PostgreSQL — `quota_policies.window` | Same schema upgrade | Identifiers are quoted by SQLAlchemy's identifier preparer, per dialect |
| PostgreSQL really does accept concurrent writers, so a read-modify-write counter loses increments | `quota_counters` when Redis is not configured | The increment is now one `UPDATE … SET requests = requests + n` (§5f) |
| Connections cross a network and can be closed by a pooler or firewall while idle | Every query | `pool_pre_ping` and a 300 s `pool_recycle` are enabled for PostgreSQL only |

### Things to watch after the switch

* **Timezone of the server does not matter** for reports (see the table above),
  but it does matter for anything you query by hand. `SET TIME ZONE 'UTC';`
  before running ad-hoc SQL, or your day boundaries will not match the console.
* **`scripts/access_change_report.py` reads the database synchronously** and
  therefore cannot use `asyncpg`. It picks up `psycopg` or `psycopg2` if either
  is installed and tells you to install one if neither is:
  `pip install 'psycopg[binary]'`.
* **Behind pgbouncer**, use session pooling. Transaction pooling breaks
  asyncpg's prepared-statement cache; if you must use it, the gateway needs
  `statement_cache_size=0` passed through the URL query string.
* **Back up before the first start**, not after. The startup schema upgrade
  adds columns; it never drops or retypes anything, but "never" is easier to
  believe with a dump on disk.

### Testing against a real PostgreSQL

The test suite does **not** require PostgreSQL. SQLite is the default, every
test behaves exactly the way it always has, and `pytest` on a fresh checkout
needs no database server at all.

Set `GW_TEST_POSTGRES_URL` and **the whole suite** runs against that server
instead — not just the portability checks in `tests/test_postgres_support.py`:

```bash
GW_TEST_POSTGRES_URL=postgresql+asyncpg://litegate:PASSWORD@localhost:5432/litegate_test \
  .venv/bin/python -m pytest -q
```

Without that variable nothing changes and the PostgreSQL-only tests skip with a
reason. They never fail for the absence of a database. **Use a throwaway
database — every table is emptied before every test, and some tests drop them.**

A few tests are marked `sqlite_only` (WAL mode, the busy timeout, the size of
the local-file pool) and skip on PostgreSQL: they assert things that are about
SQLite, not about the gateway.

How each test is isolated from the next — why it is a TRUNCATE per test rather
than the usual transaction-rolled-back-per-test, and how pooled connections are
kept from crossing event loops — is written down at the top of
`tests/conftest.py`. Read that before changing those fixtures.

---

## 5f. Redis — shared quota counters

```ini
GW_REDIS_URL=redis://127.0.0.1:6379/0
```

Empty is still the default, and still correct for a single worker. **Set it on
any deployment that runs more than one worker**, which is both shipped ones:
`docker/Dockerfile` and `deploy/systemd/litegate.service` each start uvicorn
with `--workers 4`.

### Why it matters with four workers

Without Redis, quota and per-minute rate-limit counters live in the
`quota_counters` table. All four workers do share that table, and the increment
is a single atomic `UPDATE … SET requests = requests + n` — so no worker can
lose another's count. (It used to be a read-modify-write in the ORM: `SELECT`,
add in Python, `UPDATE` with the new total. Two workers reading the same row at
the same moment each wrote their own total and one increment vanished, always
in the member's favour, and worse the busier it got. That is fixed.)

What database counters still cost you is **contention**. On the default SQLite
file, SQLite takes one writer at a time, so every increment from every worker
queues behind the others; under load that surfaces as `database is locked`
rather than as a slow counter. On PostgreSQL it is a round trip and a row lock
per request instead of a file lock — better, but still a database write on the
path of every completed request.

Redis counts with `HINCRBY` — atomic, server-side, one hash per
(subject, window) with a TTL that expires when the window does. Concurrent
workers cannot lose each other's increments.

This does not replace the database. It is not where quota *policy* lives, and
`usage_logs` — the ledger the reports are built from — is untouched by it.
Redis holds only the running counts.

### Falling back is automatic, and safe

If `GW_REDIS_URL` is set but Redis does not answer at startup, the gateway logs
`Redis unavailable (…); using database counters` and serves from the database.
It does not refuse to boot.

If Redis dies *later*, the counter store fails over per call: the first failure
marks Redis down for 30 seconds and those requests go straight to the database,
then it tries again. Requests keep working throughout — losing the cache costs
latency, not availability.

On recovery, counts written to the database during the outage are not lost. The
first Redis miss after a fallback reads the database and seeds Redis from it;
without that, a Redis that restarts empty would hand every member their whole
quota back.

One honest gap: the reseed fires on a *miss*, so if Redis survives the outage
holding a partial count, the two ledgers are disjoint and the total is
under-reported by whatever was spent while Redis was unreachable. That is
deliberate — merging them safely would need a distributed lock, and
double-counting blocks a member who has done nothing wrong. The under-count is
bounded by the length of the outage.

`POST /admin/users/{id}/quota/reset` clears **both** ledgers, and fails loudly
if Redis is unreachable rather than reporting a success that will be undone by
the next reseed.

### Watch `litegate_quota_counters_degraded`

```
litegate_quota_counters_degraded   0 = counting in Redis (or Redis was never configured)
                                   1 = fell back to the database
```

This is the only signal that a fallback happened. Nothing errors, nothing in
the request path logs, and members see no difference — which is how a gateway
ends up running for a week on a Redis that died on Tuesday. The gauge reads `0`
on deployments that never set `GW_REDIS_URL`, so alerting on `== 1` does not
fire on every SQLite install.

Redis needs no other configuration: outbound `6379/tcp` from the gateway host
([NETWORK.md](NETWORK.md)), and nothing in it needs backing up — every key
carries a TTL and the database holds the durable copy.

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
* **Reasoning models need a bigger `max_tokens` than you would guess.** They
  spend the budget thinking first, so too small a number returns
  `stop_reason: end_turn` with an *empty* answer — which reads as a broken
  gateway and is the model running out of room. Measured on Muse-Glimmer with a
  three-line prompt: nothing at 128, the full answer at 256. Give reasoning
  models at least 256, and suspect this first whenever a reply comes back empty
  with no error.

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

**`scripts/bootstrap.sh` does all of this for you.** HTTPS is part of the
install, not a follow-up step: the last thing bootstrap does is run
`scripts/install_tls.sh`, which issues the certificate, writes the nginx site,
checks it, and reloads. Set `SKIP_TLS=1` to opt out.

To set it up on a gateway that is already running, or to change the names:

```bash
sudo ./scripts/install_tls.sh                       # names detected from the host
sudo ./scripts/install_tls.sh litegate.local 192.168.1.10
sudo ./scripts/install_tls.sh --cert /path/fullchain.pem --key /path/key.pem
```

It is safe to re-run: an existing certificate with more than a week left is
kept, so re-running to change a hostname does not churn the certificate or make
anyone reinstall the CA.

What you end up with:

| Port | Serves | For |
|---|---|---|
| 443 | nginx, TLS | the address you hand out |
| 80 | 301 to 443 | so a typed hostname lands somewhere |
| 8080 | the app, plain HTTP | scripts, health checks, LAN clients |

Underneath, `scripts/make_tls_cert.sh` creates a small CA of your own and
issues a server certificate with the right names, including IP addresses as IP
SANs. Call it directly if you want the files without touching nginx.

**What it changes on the host, and why.** Two of these were installs that
failed in the field, so they are worth reading before you run it on someone
else's server:

| Change | Reason |
|---|---|
| Picks `http2 on;` or `listen 443 ssl http2;` by nginx version | The two spellings changed at nginx 1.25.1. Ubuntu 24.04 ships 1.24.0, which reads the modern file, reports `unknown directive "http2"` and refuses to start — taking the whole TLS step down on the most common host there is. |
| Disables nginx's stock `default` site | It holds `default_server` on `:80`, so any Host the script did not detect — a name you put in DNS yourself, a second network card, a new DHCP lease — reaches nginx's empty page instead of the redirect. Your file stays in `sites-available/default`; re-enable with `ln -s`. A default site that has been edited is left alone and reported instead. |
| Restores everything if `nginx -t` fails | Otherwise a rejected render leaves a host with neither the default site nor ours, which is worse than before the command ran. |
| Warns when nothing is listening on `:8080` | nginx will be correct and still answer 502. Saying so up front stops an hour of debugging TLS that is already working. |

Non-interactive installs (config management, a scripted rollout) set
`TLS_ASSUME_YES=1` to accept the detected names without the prompt.

List **every** name the gateway will be reached by. A certificate for the
hostname does not cover the IP, and clients differ in which they send —
`install_tls.sh` defaults to the hostname, every non-loopback address, plus
`localhost` and `127.0.0.1`.

**On a LAN with no domain**, run it with no arguments. It shows the names it
found and asks whether to add any; pressing Enter accepts them. A certificate
over `192.168.1.10` is a real certificate — a domain buys public trust, which is
exactly what a LAN install does not need. What it does need is the CA installed
on the machines that will call the gateway, and the script prints the command
for Ubuntu, macOS, Windows and Firefox when it finishes.

Re-running after the addresses change reissues automatically: the script now
compares the names against the certificate's SANs, not just its expiry date. A
certificate that is still valid but no longer covers the address in use produces
a browser warning, which reads to everyone involved as a broken install.

### One gateway, two addresses, one cookie jar

Cookies are scoped by host — not by port, and not by scheme. `https://host` and
`http://host:8080` therefore share a jar, and the https session cookie carries
`Secure`. A browser will not let an insecure page overwrite a Secure cookie of
the same name: it drops the new one and reports nothing.

The symptom, before this was fixed, was a sign-in that returned 200 and then
answered every following call with `MISSING_API_KEY: No API key provided`,
displayed next to the password box. It reads as a rejected password and is
precisely the one thing it is not.

The session cookie is now named per scheme — `litegate_session` over HTTPS,
`litegate_session_http` over plain HTTP — so neither address can shadow the
other, and either cookie is accepted if the matching one is absent (a
TLS-terminating proxy makes a request look secure while the browser stored the
other name). Signing out clears both.

### HSTS is off unless the certificate is publicly trusted

HSTS tells a browser "this host is HTTPS-only" and it is believed for a year.
Paired with a private CA that costs more than it buys:

* Chrome does not offer the **proceed anyway** link on an HSTS host. The first
  visit — before anyone has installed the CA — becomes a dead end rather than a
  warning you can click past.
* The promise covers the *host*, not the port. Every `http://` URL for that
  machine is upgraded, **including `http://host:8080`**, which then fails
  because nothing is listening for TLS there. This is a confusing failure: the
  browser reports a connection error, so it looks like the app is down.

So `install_tls.sh` emits the header only with `--cert` (a real certificate) or
an explicit `--hsts`.

If a browser already has the pin, removing the header does not release it —
the browser keeps its promise for the full year. Clear it by hand:

* **Chrome / Edge** — `chrome://net-internals/#hsts` → *Delete domain security
  policies* → enter the host → **Delete**
* **Firefox** — History → *Forget About This Site* for that host
* **Safari** — Develop → *Empty Caches*, then remove the site's data in
  Settings → Privacy → Manage Website Data

Then install `certs/ca.crt` on the machines that call the gateway — the script
prints the command for each platform. Until you do, they are right to refuse the
connection. Verify without `--insecure`, which is the whole point:

```bash
curl https://192.168.1.10/healthz
```

Re-running the script reuses an existing CA, so certificates issued later stay
trusted and nobody reinstalls anything.

**One thing to decide once TLS is in front:** how much of the network should
still reach port 8080 directly.

The app binds `0.0.0.0:8080` so scripts, health checks and LAN clients keep
working. That port bypasses TLS, the rate limits, and the `/admin` and
`/metrics` restrictions in the nginx config. On a shared corporate network where members
can route to the gateway host, change the systemd unit to `--host 127.0.0.1`
and hand out only the HTTPS address. On an isolated rack where the only things
that can reach it are yours, leaving it open costs nothing and saves an
argument with every monitoring probe.

**Who may reach `/admin/` and `/metrics` through nginx.** Both are restricted to
private address space — `127.0.0.0/8`, `10/8`, `172.16/12`, `192.168/16`, and
the IPv6 equivalents `::1/128`, `fc00::/7`, `fe80::/10`. Everything else gets
403. Two things follow from that, and both have bitten real installs:

- **The console is on those paths.** Most of its calls begin with `/admin/`, so
  an address outside the list does not lose "the admin API" — it loses the
  console. Sign-in still returns 200 and then every panel fails, which reads as
  a broken product rather than a firewall rule.
- **Add your own range if you have one.** A management VLAN on public address
  space, or a VPN handing out addresses outside these ranges, needs an extra
  `allow` line in both blocks of `/etc/nginx/sites-available/litegate.conf`.
  The rule matches the real peer address; `X-Forwarded-For` neither grants nor
  denies access, so putting another proxy in front means allowing *its* address
  and re-establishing the restriction there.

Nothing else changes. The session cookie already sets `Secure` when the request
arrives over HTTPS, which works behind the proxy because the unit passes
`--proxy-headers`.

If you *do* have a public hostname, use the Caddy config instead and let it
obtain a real certificate — then none of the CA installation applies.

---

## 5e. Monitoring

`/metrics` is Prometheus exposition. Scrape it and load the rules:

```yaml
scrape_configs:
  - job_name: litegate
    static_configs: [{ targets: ["gateway:8080"] }]

rule_files:
  - /etc/prometheus/rules/litegate.rules.yml   # deploy/prometheus/litegate.rules.yml
```

Ten alerts, each tied to a target in PRD §16 and to a section in
[RUNBOOK.md](RUNBOOK.md). An alert with no written response is a page that wakes
somebody who then has to work out what to do.

| Signal | Why it is exported |
|---|---|
| `litegate_ready` | `up` catches a dead process. It cannot see a gateway running happily with an unreachable database or no backend to route to, which is the outage members actually feel |
| `litegate_endpoints_healthy` / `_total` | Losing one of three is a warning; losing all three is a page. The two need different responses |
| `litegate_quota_counters_degraded` | Redis falling back is invisible — requests keep working, so nothing errors and nothing logs. This is how a gateway runs for a week on a Redis that died on Tuesday |
| `litegate_requests_in_flight` | Warns at 80% of the tested 200-stream ceiling, while there is still time to add an instance |
| `litegate_errors_total{code}` | The `code` label usually identifies the cause without opening a log |

Thresholds and `for:` durations are deliberately slack. A gateway in front of
model servers is bursty — one team starting a batch job moves every rate —
and an alert that fires on a two-minute spike is one people learn to ignore.

> **Read [§10 Known limitations](#10-known-limitations) before you trust a
> number here.** In multi-worker mode `/metrics` answers from one worker's
> registry, the duration histogram measures time to first header rather than
> request duration, and the in-flight gauge does not count streams that are
> still sending.

Note what is *not* alerted on: NFR-P1 is about gateway overhead, and the request
histogram includes the model's own generation time, so a slow model would fire
it. The latency alert covers only endpoints that do no generation, where slow
means the gateway or its database.

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
| `litegate_request_duration_seconds{path,model}` | Latency histogram — **time to first header, not request duration** (§10) |
| `litegate_requests_in_flight` | Concurrency — **excludes streams already sending** (§10) |
| `litegate_errors_total{code}` | Errors by gateway error code |
| `litegate_quota_counters_degraded` | `1` when quota counting has fallen back from Redis to the database (§5f) |

With `--workers 4` every absolute number above is one worker's view. See
[§10 Known limitations](#10-known-limitations).

Alerts worth having from day one:

| Alert | Condition |
|---|---|
| Gateway down | `/readyz` != 200 for 2 min |
| Backend ejected | `endpoints_healthy < endpoints_total` for 5 min |
| Error surge | `rate(litegate_errors_total{code="UPSTREAM_ERROR"}[5m]) > 0.1` |
| Quota pressure | `QUOTA_EXCEEDED` rate rising as a billing window or deadline approaches |
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
| `x-edullm-model` response header | `x-litegate-model` | **No longer sent.** A client reading the old name gets nothing — read `x-litegate-model` |
| systemd unit `edullm-gateway` | `litegate` | Rename at your convenience; the old unit keeps running |
| Prometheus `edullm_*` metrics | `litegate_*` | **Not aliased** — update dashboards, this is the one thing that changes immediately |

### A note on the schema vocabulary

Database table and column names are unchanged. They are not part of the product
surface, and renaming them would force a migration for nothing a user can see.

That is a deliberate trade, but it means **the names in the database do not
match the names in the API, the console or this guide.** Anyone writing a
report against the database directly, restoring a backup by hand, or reading
`sqlite3 .schema` needs this table:

| Code / API / docs say | The database actually has |
|---|---|
| `Workspace` | table `courses` |
| `Membership` | table `enrollments` |
| `WorkspaceModel` | table `course_models` |
| `workspace_id` | column `course_id` — on `enrollments`, `api_keys`, `course_models`, `quota_policies`, `usage_logs` |
| `WorkspaceAccessGroup.workspace_id` | column keeps the name `workspace_id`, but its foreign key points at `courses.id` |
| — | constraints and indexes still read `uq_enrollment`, `uq_course_model`, `ix_usage_ts_workspace (ts, course_id)` |

The ORM maps every one of these, so **nothing that goes through the application
is affected**: `Workspace.workspace_id` is the attribute, `course_id` is the
column underneath it. Read through the ORM and the mismatch is invisible.
Read the tables directly and it is not.

Two more names are historical in the same way:

- **`Workspace.term`** (`--term` in `scripts/seed.py`, `term` in the workspace
  API) is a free-text label. It expires nothing and grants nothing.
- **The `term` quota window** is a real multi-month window, not an academic
  one — it simply defaults to months `(1, 6, 8)`, the calendar this first ran
  on. See §10 for the configuration caveat before you rely on it.

Renaming any of this is a schema migration with real risk and is not scheduled.
It is tracked as its own task.

### Routine upgrade

When the host has a checkout of this repository:

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

#### When the host has no checkout

An install that was put there by copying files has no git, no remote and no tag
to roll back to — which the instructions above quietly assume. Copying the
changed files over is fine; doing it without the first step is what takes a
gateway down.

**Check that the target is where you think it is.** Compare every file you are
about to replace against the commit you believe is deployed:

```bash
for f in app/api/admin.py app/core/quota.py app/static/app.js; do
  printf '%-34s %s %s\n' "$f" \
    "$(git show HEAD~1:$f | sha256sum | cut -c1-12)" \
    "$(ssh HOST sudo sha256sum /opt/litegate/$f | cut -c1-12)"
done
```

Every pair must match. If one does not, the running code is not the parent of
what you are shipping, and copying a subset of files onto it produces a tree
that never existed — a module importing a name a sibling file does not have yet.
Four uvicorn workers then crash-loop on `ImportError` and the gateway is down
until you notice.

Then, in order — each step exists because skipping it has cost an outage:

```bash
BACKUP=/opt/gw-backup-$(date +%Y%m%d-%H%M%S)
sudo mkdir -p $BACKUP && sudo cp --parents app/api/admin.py … $BACKUP/   # 1. keep the way back
sudo /opt/litegate/.venv/bin/python -m compileall -q <staged files>      # 2. syntax, before it is live
sudo install -o litegate -g litegate -m 644 <file> /opt/litegate/<file>  # 3. copy
sudo -u litegate /opt/litegate/.venv/bin/python -c 'import app.main'     # 4. imports, before restart
sudo systemctl restart litegate
curl -sf --retry 10 --retry-delay 2 http://127.0.0.1:8080/healthz        # 5. prove it came back
```

Steps 2 and 4 are the point: a syntax error or a missing import found *after*
the restart is an outage, and found before it is a no-op. If any step fails,
restore from `$BACKUP` and restart — decide that in advance rather than while
the console is down.

> **Legacy installs answer to different names.** A gateway that started life as
> EduLLM Gateway lives in `/opt/edullm-gateway` under unit `edullm-gateway`,
> owned by the `edullm` user. Every command in this guide says `litegate`;
> substitute throughout, or the commands will appear to succeed while acting on
> nothing. `systemctl show <unit> -p FragmentPath -p WorkingDirectory --value`
> settles which one you have.

### Renaming a legacy install to `litegate`

Optional, and worth doing once rather than substituting names forever. Roughly a
minute of downtime. Nothing in the database changes, so **issued API keys keep
working** — a key is verified by HMAC against `GW_API_KEY_PEPPER`, which moves
with the `.env` file.

```bash
sudo systemctl stop edullm-gateway && sudo systemctl disable edullm-gateway
sudo mv /opt/edullm-gateway /opt/litegate

# uid/gid stay the same, so every file's ownership follows the rename by itself
sudo usermod -l litegate -d /opt/litegate edullm
sudo groupmod -n litegate edullm

sudo sed -i 's#/opt/edullm-gateway#/opt/litegate#g' /opt/litegate/.env
```

**The venv will not survive the move on its own.** Python writes its absolute
path into every script in `.venv/bin` and into `pyvenv.cfg`, so systemd fails
with `203/EXEC` and `No such file or directory` — pointing at a file that is
plainly there. What is missing is the *interpreter named in its shebang*:

```bash
sudo find /opt/litegate/.venv/bin -maxdepth 1 -type f \
     -exec grep -Il '/opt/edullm-gateway' {} \; |
  sudo xargs -r sed -i 's#/opt/edullm-gateway#/opt/litegate#g'
sudo sed -i 's#/opt/edullm-gateway#/opt/litegate#g' /opt/litegate/.venv/pyvenv.cfg
```

Then install the unit that ships with the repository and start it:

```bash
sudo mkdir -p /opt/litegate/logs && sudo chown litegate:litegate /opt/litegate/logs
sudo install -m 644 deploy/systemd/litegate.service /etc/systemd/system/
sudo rm -f /etc/systemd/system/edullm-gateway.service
sudo systemctl daemon-reload && sudo systemctl enable --now litegate

curl -s http://127.0.0.1:8080/readyz     # models_loaded and endpoints_healthy should match what you had
```

`ReadWritePaths` in the shipped unit names `data/` and `logs/`; `logs/` may not
exist on an install that predates it, and systemd refuses to start a unit whose
`ReadWritePaths` is missing. Creating it first is the whole fix.

An old `.venv/bin/edullm-gateway` may still be lying around — a console script
for an entry point the package no longer declares. It does nothing; delete it.

---

## 10. Known limitations

Everything below is true of the current release. None of it stops the gateway
serving traffic; all of it changes what you can conclude from what you are
looking at. Written down because an operator who does not know these will draw
the wrong conclusion from a dashboard at exactly the wrong moment.

### Metrics

**`/metrics` reports one arbitrary worker, not the process group.**
`PROMETHEUS_MULTIPROC_DIR` is not configured anywhere — not in
`docker/Dockerfile`, not in `deploy/systemd/litegate.service`, not in the code —
and `/metrics` serves `generate_latest()` against the default in-process
registry. With `--workers 4` each worker keeps its own counters, and a scrape is
answered by whichever worker happened to accept the connection. Counters look
like they go backwards between scrapes, and every absolute number is roughly a
quarter of the truth. Treat `/metrics` as indicative in multi-worker mode.

The readiness gauges are worse than merely partial, and this one can page you at
three in the morning for nothing. `litegate_ready`,
`litegate_endpoints_healthy`, `litegate_endpoints_total` and
`litegate_models_loaded` are set **inside the `/readyz` handler** and nowhere
else. A worker that has never answered a `/readyz` probe has never set them, so
they sit at the client library's default of `0`. A scrape that lands on such a
worker therefore reports a gateway that is not ready, with no models and no
backends — while the gateway is serving traffic normally.

Two consequences, in opposite directions:

- `LiteGateNotReady` (`litegate_ready == 0`, `for: 3m`, **severity: page**) in
  `deploy/prometheus/litegate.rules.yml` can fire on a perfectly healthy
  gateway, because consecutive scrapes may keep landing on unprobed workers.
- `LiteGateAllBackendsUnhealthy` is guarded by
  `litegate_endpoints_total > 0`, and `LiteGateBackendDegraded` compares
  `healthy < total`. On an unprobed worker both sides are `0`, so neither
  fires — a real "no backend is healthy" outage can be masked by whichever
  worker answers the scrape.

Until `PROMETHEUS_MULTIPROC_DIR` is configured, the safe reading is: use
`/readyz` itself (which is correct, because it computes state on the worker
answering it) for readiness, and treat the readiness *metrics* as advisory.
Raising `for:` on `LiteGateNotReady` reduces the false pages but does not
remove them.

**`litegate_request_duration_seconds` measures time to first header, not request
duration.** It is observed in the `@app.middleware("http")` block in
`app/main.py`, which Starlette runs as `BaseHTTPMiddleware`: `call_next` returns
as soon as the response *starts*, and for a streaming response that is the first
byte, not the last. Most traffic through this gateway is streaming, so for most
requests this histogram is closer to TTFT than to latency. It is not a lie about
short JSON endpoints — `/readyz`, `/admin/*`, `/v1/models` are measured
correctly — but do not size timeouts or read generation cost from it.

**`litegate_requests_in_flight` decrements before the body drains.** Same block:
the gauge is decremented in a `finally` around `call_next`, which completes when
the headers are out. An active stream that has not sent its last token is not
counted. On a gateway whose whole job is long streams, the gauge is closer to
"requests currently in their header phase" than to concurrency, and it can read
near zero while every backend is saturated. The 80%-of-200-streams alert in §5e
inherits this.

**TTFT is measured but never exported.** `UsageLog.ttft_ms` is populated for
streaming requests on all three protocol surfaces, and `PerfStore` uses it live
to rank models for `model: "auto"`. There is no Prometheus metric for it, so the
number an operator most wants during a slow-model complaint is only reachable by
querying `usage_logs`.

### Configuration and deployment

**`GW_WORKERS` does nothing in either shipped deployment.** `Settings.workers`
is read in exactly one place: the `run()` console-script entrypoint
(`litegate = "app.main:run"`). Neither shipped deployment uses it —
`docker/Dockerfile` and `deploy/systemd/litegate.service` both call uvicorn
directly with `--workers 4` hardcoded. Changing `GW_WORKERS` in `.env` and
restarting produces no change at all, silently. To change the worker count,
edit `ExecStart=` in the unit file (or the `CMD` in the Dockerfile) and reload
systemd. Note also that `run()` would clamp to a single worker outside
production regardless.

**`quota_defaults.term_start_months` in `gateway.yaml` is not wired up.** The
setting is parsed and validated — `QuotaDefaults.term_start_months`, documented
there as the way an organisation states its own fiscal or semester calendar —
but `window_bounds()` is called without it at all nine of its call sites, so the
`term` window always falls back to the hardcoded `DEFAULT_TERM_START_MONTHS =
(1, 6, 8)`. Setting `[1, 4, 7, 10]` for fiscal quarters changes nothing, and
nothing warns. Until that is fixed, a `term` quota window means a period
starting in January, June or August, whatever the file says. `hour`, `day` and
`month` windows are unaffected.

**Port 8080 stays bound on `0.0.0.0` behind any TLS proxy.** Both shipped
deployments pass `--host 0.0.0.0`, and installing nginx or Caddy in front adds
443 — it does not close 8080. Anything that can reach the host on 8080 bypasses
the proxy entirely, and with it the per-IP rate limits *and* the
`allow`/`deny` lists that restrict `/admin/` and `/metrics` to private
networks. API-key authentication still applies, so this is not an open door;
it is the loss of every control that lives in the proxy rather than in the
gateway. If the host has an interface on an untrusted network, firewall 8080
to the proxy, or set `GW_HOST=127.0.0.1` when the proxy is on the same machine.
§5c ("One thing to decide once TLS is in front") covers the same decision where
you first meet it.

**nginx rate limits are per source IP, which is per NAT, not per tenant.**
`deploy/nginx/litegate.conf` keys both zones on `$binary_remote_addr`
(lines 16–17), and applies `limit_req … burst=60 nodelay` and
`limit_conn litegate_conn 20` (lines 44–45). At `rate=120r/m` that is 2
requests/second sustained and 20 concurrent connections **shared by everyone
arriving from one address**. A whole office behind one NAT, or a CI runner pool
on one egress IP, hits a limit that was sized for one person — and because
streaming connections are long-lived, the connection limit bites first. There
is no `set_real_ip_from`/`real_ip_header` in the config, so `X-Forwarded-For`
from a CDN or an upstream proxy does not change the key either. Raise both
numbers, or key the zones on something tenant-shaped, if any of your callers
share an egress address. The gateway's own per-member quota and per-minute
limits are unaffected by this — those are the real policy.

### Access control

**`ApiKey.scopes` is stored but never enforced.** The column exists, the
`POST /admin/api-keys` body accepts `scopes`, the value is persisted and loaded
onto the `Principal` on every request — and `Principal.require_scope()` has zero
call sites. A key issued with narrow scopes is not narrowed by them. What *is*
enforced on a key is `models` and `access_groups` (see §2.2), plus the role on
the user behind it. Do not rely on `scopes` as a boundary; if you have issued
keys assuming it works, re-check them against `models`/`access_groups` instead.

---

## Reading an issued key back (optional)

By default only a digest of each API key is stored, so a lost key can only be
replaced, never recovered. Set `GW_KEY_REVEAL_SECRET` to keep a sealed copy that
an **administrator** can open from the console.

```bash
GW_KEY_REVEAL_SECRET=$(openssl rand -base64 32)
```

- Keep it in the environment or a secrets manager — **never in the database**,
  which is where the sealed values live. Together in one place, the encryption
  buys nothing.
- Include it in the same rotation policy as any other credential. Rotating it
  leaves existing keys working but no longer readable.
- Back it up with the same care as the database. Losing it does not break the
  gateway; it permanently removes the ability to read keys back.
- Every reveal is written to `audit_logs` with action `apikey.reveal`, and shown
  in the console next to the key.

Leaving it unset keeps the original behaviour, which is the stronger posture: a
stolen database dump contains no usable credentials.


## Certificates for clients on other machines

`scripts/install_tls.sh` issues from a private CA when no certificate is given,
and leaves it at `/etc/ssl/litegate-ca/ca.crt`. That file is what makes TLS
verify on the machines that call the gateway; `ca.key` beside it is what issues
certificates they will then trust, so it never leaves the host.

**Name every address before issuing.** A certificate for the hostname does not
cover the IP, and clients differ in which one they send:

```bash
sudo scripts/install_tls.sh --force \
  gateway.example.ac.th 10.0.0.5 localhost 127.0.0.1
```

`--force` matters on the second run. Without it the script keeps a certificate
that has not expired — which says nothing about whether it still covers the
address in use, and is how a working gateway starts failing after a rename.

**Installing the CA on a client:**

```bash
# macOS, system-wide — required for Electron/Chromium apps
sudo security add-trusted-cert -d -r trustRoot \
  -k /Library/Keychains/System.keychain litegate-ca.crt

# Linux
sudo cp litegate-ca.crt /usr/local/share/ca-certificates/ && sudo update-ca-certificates

# Node processes only
export NODE_EXTRA_CA_CERTS=/path/to/litegate-ca.crt

# curl / scripts, without touching any store
curl --cacert litegate-ca.crt https://gateway.example.ac.th/healthz
```

Desktop applications usually need the system store rather than the Node
variable: they make some requests from Node and others from Chromium, and only
the first reads `NODE_EXTRA_CA_CERTS`. The symptom of getting that wrong is
`net::ERR_CERT_AUTHORITY_INVALID` from one half of an app while `curl` on the
same machine is happy.

**When not to bother.** A client on the gateway host itself can use
`http://127.0.0.1:8080` and skip certificates entirely — that is the reason the
plain port stays open. And for a client that cannot be given the CA, a tunnel
(`cloudflared tunnel --url http://localhost:8080`) supplies a publicly trusted
name at the cost of publishing the endpoint while it runs.
