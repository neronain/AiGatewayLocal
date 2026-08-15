# Runbook

What to do when an alert fires. One section per alert in
[`deploy/prometheus/litegate.rules.yml`](../deploy/prometheus/litegate.rules.yml);
an alert without a written response is a page that wakes somebody who then has
to work out what to do at 3am.

Every section says what members are experiencing first, because that decides
whether to fix it or to buy time.

**The two commands worth knowing before anything else:**

```bash
curl -s https://gateway/readyz | jq        # ready, database, backends, registry errors
journalctl -u litegate -n 100 --no-pager   # or: docker compose logs --tail=100 gateway
```

`/readyz` answers most of what follows without reading a log.

> **Check the unit name once, now, not at 3am.** A gateway upgraded from EduLLM
> Gateway still answers to `edullm-gateway` in `/opt/edullm-gateway`, and every
> `litegate` command below will report success while doing nothing at all.
> `systemctl show <unit> -p FragmentPath -p WorkingDirectory --value` settles it.

---

## The gateway is down

*`LiteGateDown` — nobody can reach any model.*

```bash
systemctl status litegate            # or: docker compose ps
journalctl -u litegate -n 100 --no-pager
```

Most first starts that fail, fail on configuration, and the log says which:

| In the log | What happened |
|---|---|
| `GW_API_KEY_PEPPER` missing | The one variable with no default. It cannot be invented — see below |
| `could not initialise the database` | Postgres unreachable or credentials changed |
| YAML parse errors | A bad registry edit. The gateway keeps the last good snapshot in memory, but a **restart** has no memory to keep |

That last row is the trap: a broken model file is survivable until someone
restarts, and then it is not. `git diff config/` is usually the whole answer.

> **Never "fix" a missing pepper by generating a new one.** Every API key is a
> hash under it; a new pepper silently invalidates every key ever issued and
> they cannot be recovered. Find the old one — `.env`, your secret manager, a
> backup (`scripts/restore.sh`). A gateway that is down for an hour is an
> incident; one that comes back having locked out every member is a much longer
> one.

## Running but not ready

*`LiteGateNotReady` — the process is alive and `/readyz` says no.*

```bash
curl -s http://127.0.0.1:8080/readyz | jq
```

* `"database": "unavailable"` → Postgres. Check it is up and reachable *from the
  gateway*, which on Docker is not the same as from your shell.
* `"models_loaded": 0` → the registry loaded nothing. `registry_errors` says
  why; the usual cause is a YAML file that fails validation.
* `"endpoints_healthy": 0` → the next section.

## No backend is healthy

*`LiteGateAllBackendsUnhealthy` — the gateway is fine and has nowhere to route.*

This is nearly always the model servers, not the gateway.

```bash
curl -s https://gateway/v1/health/endpoints -H "Authorization: Bearer $ADMIN" | jq
curl -sf http://<model-host>:8000/health    # straight at the backend
```

If the backend answers directly but the gateway calls it unhealthy, the
difference is the network between them — a firewall, a container that cannot
see the host, a hostname that resolves differently inside Docker.

Model servers are restarted with whatever deployed them, not from here. With
LMDS connected, the *Models → Verify* button will also tell you what a backend
is refusing to do and offer to fix the parser-shaped causes.

## A backend dropped out

*`LiteGateBackendDegraded` — one of several is gone. Members are still working.*

Not urgent, and do not treat it as urgent: routing has already shifted, and
capacity is what you lost. Find out which and why:

```bash
curl -s https://gateway/v1/health/endpoints -H "Authorization: Bearer $ADMIN" | jq '.[] | select(.healthy==false)'
```

Health recovers on its own with hysteresis, so a backend that is genuinely back
will clear the alert without anyone intervening. If it flaps in and out, the
model server is restarting in a loop — look there, not here.

## The error rate is up

*`LiteGateErrorRateHigh` — more than 5% of requests failing server-side.*

```bash
curl -s https://gateway/metrics | grep litegate_errors_total
```

The `code` label names the cause without needing the logs:

| Code | Where the problem is |
|---|---|
| `UPSTREAM_*` | The model backends. See below |
| `MODEL_NOT_FOUND` | An alias was removed or renamed while clients still use it |
| `INTERNAL_ERROR` | The gateway itself. Get a `request_id` from a member and grep for it |

A spike right after a registry change is the registry change. `git log config/`.

## Upstream errors

*`LiteGateUpstreamErrors` — the gateway reaches the backends and dislikes the answers.*

Almost always something changed on the model server: a different image, a
different flag, a model swapped behind the same alias.

```bash
python scripts/model_test_suite.py --base-url $GW --admin-key $ADMIN --model <alias>
```

That says what the backend can *actually* do now, as opposed to what the
registry claims. Where they disagree, the registry is wrong until proven
otherwise — the backend is the thing that is running.

## A backend is serving a different model

*No alert. That is the whole problem.*

An operator reloads a node with different weights and the registry still points
at it. What happens next depends entirely on the server:

| Server | Unknown `model` in the request | How you find out |
|---|---|---|
| vLLM, SGLang | `404 The model X does not exist` | `UPSTREAM_*` errors, alert fires |
| llama.cpp | Serves whatever is loaded, `200` | Nobody tells you |

llama.cpp has one model in memory and ignores the field, so an alias pointed at
it keeps answering — fluently, plausibly, and from the wrong model. A fallback
endpoint is the worst place for this: it is silent until the primary dies, and
then it is silent while it is wrong.

Ask every backend what it is actually serving and compare:

```bash
for url in $(grep -h base_url config/models/*.yaml | awk '{print $2}' | sort -u); do
  echo "$url -> $(curl -s -m 5 "$url/v1/models" | jq -r '.data[].id' | paste -sd,)"
done
```

Anything that disagrees with the alias's `upstream_model` is a live incident
even when the dashboards are green. Disable the endpoint (`enabled: false`)
rather than editing it to match: an alias whose name promises a coding model
must not quietly become a general one, and a fallback that serves the wrong
weights is worse than having no fallback at all.

When the node has genuinely become a new model, give it its own alias, and
build the capability block from measurement instead of from the model's name:

```bash
curl -X POST https://gateway/admin/models/detect -H "Authorization: Bearer $ADMIN" \
     -H 'Content-Type: application/json' -d '{"base_url": "http://node:8000"}'
curl -s https://gateway/admin/models/<alias>/advice -H "Authorization: Bearer $ADMIN" | jq '.backends[].drift'
```

`detect` probes the backend and fills the flags in; `advice` reports every place
the registry and the running server still disagree. An empty `drift` array is
the thing to merge on. Re-run it after any deploy on the model servers — nothing
runs it for you.

## Everything feels slow

*`LiteGateSlowNonStreaming` — p95 above 2s on endpoints that do no generation.*

Endpoints that do not call a model should be fast, so this points at the
database rather than the models.

* Check the database machine for load and disk.
* On SQLite, this is the signal to move to Postgres — one writer, and it does
  not care how many workers you configured.
* Watch out for a usage dashboard being left open on a large window; it is the
  most expensive read in the system.

## The gateway is saturated

*`LiteGateSaturated` — in-flight requests near the tested ceiling of 200.*

```bash
curl -s https://gateway/metrics | grep litegate_requests_in_flight
```

Genuine demand, in which case add an instance behind the proxy — the gateway
holds no per-process state that matters, so instances are interchangeable.

Or one client looping. `/admin/usage/top-users` finds them in a few seconds, and
a quota policy is a better answer than a conversation.

## Redis is down

*`LiteGateQuotaFallbackActive` — quota counting fell back to the database.*

**Members are unaffected.** This is the designed behaviour (NFR-A3): requests
keep succeeding, counters go to the database, and Redis is retried
periodically. Fix it in the morning.

```bash
redis-cli ping
systemctl status redis-server        # or: docker compose ps redis
```

Two things to know about the failover:

* Counts recorded during the outage are in the database. If Redis comes back
  **empty**, the gateway refills it from there — without that, everybody's quota
  would reset to zero.
* If Redis comes back still holding a partial count, the two ledgers stay
  separate and usage is **under-reported** by whatever was spent during the
  outage. That is deliberate; the alternative needs a distributed lock, and
  getting it wrong double-counts and blocks members who did nothing wrong.

## A lot of quota rejections

*`LiteGateManyQuotaRejections` — members being refused at an unusual rate.*

Thirty people rarely hit a limit at the same moment. Check, in this order:

1. Did a quota policy change? `/admin/quota-policies`, and the audit log says
   who changed it and when.
2. Is one client looping? `/admin/usage/top-users` — a single member with
   thousands of requests is a script, not a person.
3. Is the window shorter than intended? A `day` policy meant as `term` will
   look exactly like this every afternoon.

Once the cause is understood, deal with the person and the rule separately. If
one runaway loop spent somebody's allowance, hand it back rather than raising
the limit — the limit was not the problem, and a limit raised in an incident
stays raised:

```bash
curl -s -X POST https://gateway/admin/users/<id>/quota/reset \
  -H "Authorization: Bearer $ADMIN"
```

It returns what it cleared, leaves the usage records intact, and writes the
reset to the audit log. Fix the loop too, or you will be back within the hour.

---

## Somebody's key cannot reach a model

Nothing is broken. `MODEL_NOT_PERMITTED` names the models the key does allow —
read the message before changing anything, because the other three causes look
identical to the user and none of them is the key:

| The message says | Cause |
|---|---|
| `not available to you. Allowed by the model list on this key` | The key's own scope |
| the workspace's models | The workspace, or it is suspended |
| unknown model | The alias is not in the registry — a typo, or the file failed validation (`/readyz`) |

Only the first is fixed on the key, and it no longer needs reissuing:

```bash
curl -s -X PATCH https://gateway/admin/api-keys/<id> -H "Authorization: Bearer $ADMIN" \
  -H 'Content-Type: application/json' -d '{"models":["alias-a","alias-b"]}'
```

Send the whole list you want. `[]` removes the restriction entirely, which
widens the key — rarely what is wanted during an incident.

---

## Routine work

### Restart

```bash
sudo systemctl restart litegate      # or: docker compose restart gateway
```

In-flight streams are dropped. There is no drain, so restart when it is quiet —
or run two instances behind the proxy and restart one at a time.

### Change the registry without a restart

Model files reload on their own within `GW_REGISTRY_RELOAD_SECONDS` (30s), and
a file that fails validation is rejected while the previous snapshot keeps
serving. To apply one immediately:

```bash
curl -X POST https://gateway/admin/registry/reload -H "Authorization: Bearer $ADMIN"
```

### Rotate an admin key

Issue the new one, confirm it works, then revoke the old one. In that order —
revoking first locks you out of the plane you need to issue the replacement
from.

### Before an upgrade

```bash
./scripts/backup.sh --out /srv/backups
```

Then read [DEPLOYMENT.md §6.1](DEPLOYMENT.md#61-restoring--rehearse-it-now).
If you have never restored one, that is the thing to do this week rather than
during an incident.
