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

`$ADMIN` in the commands below is an administrator's API key **with no limit of
its own**. A key that was given a model list, an access group, a workspace or a
quota of its own is refused on every `/admin` and `/v1/health` route, whoever
owns it — see [An admin's key is refused on `/admin`](#an-admins-key-is-refused-on-admin).

> **Check the unit name once, now, not at 3am.** A gateway upgraded from EduLLM
> Gateway still answers to `edullm-gateway` in `/opt/edullm-gateway`, and every
> `litegate` command below will report success while doing nothing at all.
> `systemctl show <unit> -p FragmentPath -p WorkingDirectory --value` settles it.
> Renaming it once beats substituting names at 3am:
> [Renaming a legacy install](DEPLOYMENT.md#renaming-a-legacy-install-to-litegate).

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
| `INTERNAL_ERROR` | The gateway itself. Get a `request_id` from a member (the one in the error body, or the `x-litegate-request-id` header) and grep for it. If all they have is the `x-request-id` their own program sent, **Dashboard → Find a request** turns it into the gateway's id |

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

If the complaint is that a **model** is slow rather than the console, this alert
is the wrong instrument — it deliberately excludes generation. Open
**Dashboard → Latency percentiles**: p50 / p95 / p99 and the slowest request per
model, and per machine for an administrator, from requests a backend actually
answered. A p95 far above the p50 on one machine and not on its twin is that
machine; a column of dashes means too few requests to say, not that all is well.

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
3. **Is somebody indexing?** One `/v1/embeddings` or `/v1/rerank` call can carry
   up to `GW_MAX_BATCH_ITEMS` (2048) items, and rerank charges the query once
   per document — a 50-document call with a 200-token query is 10,000 tokens,
   not 200. A RAG job that looks like a handful of requests in
   `/admin/usage/top-users` can be most of the month's tokens. Sort by tokens,
   not by request count.
4. Is the window shorter than intended? A `day` policy meant as `term` will
   look exactly like this every afternoon. Note that `term` currently resolves
   against the hardcoded default months `(1, 6, 8)` —
   `quota_defaults.term_start_months` is not wired up (DEPLOYMENT.md §10) — so a
   `term` window may also be shorter than whoever wrote the policy expected.

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
read the message before changing anything, because the other causes look
identical to the user and are not fixed on the key's model list:

| The message says | Cause |
|---|---|
| `not available to you. Allowed by the model list on this key` | The key's own scope |
| the workspace's models | The workspace, or it is suspended |
| `this key is limited to an access group that is switched off or no longer exists` | Every access group the key names is off, deleted or empty, and the key has no model list. It calls nothing until a group is switched back on (**Access & Keys → Access groups**) or the key is given a model list. A caller that sends `model: "auto"` gets the same answer. Before 1.13.0 such a key called everything instead, so this can appear right after an upgrade |
| unknown model | The alias is not in the registry — a typo, or the file failed validation (`/readyz`) |

One more case looks the same to the caller and is not about permission at all:
`PROTOCOL_NOT_SUPPORTED` — *"Model 'x' is not available over the embeddings API.
Available: openai, anthropic."* The key is fine and the alias exists; it is the
**surface** that is not enabled for it. Fix it in the model's `spec.protocols`,
not on the key. `GET /v1/models` lists the surfaces each alias exposes.

Only the first row is fixed on the key's model list, and it no longer needs
reissuing:

```bash
curl -s -X PATCH https://gateway/admin/api-keys/<id> -H "Authorization: Bearer $ADMIN" \
  -H 'Content-Type: application/json' -d '{"models":["alias-a","alias-b"]}'
```

Send the whole list you want. `[]` removes the restriction entirely, which
widens the key — rarely what is wanted during an incident.

## An admin's key is refused on `/admin`

*No alert. A script that worked yesterday gets `403 INSUFFICIENT_SCOPE`.*

Nothing is broken, and the person is still an administrator. The **key** carries
a limit — a model list, an access group, a workspace, or a quota of its own —
and a key with a limit does not carry its owner's manager or admin rights. The
error says which:

```json
"details": { "reason_code": "restricted_key", "limited_by": ["workspace"], "owner_role": "admin" }
```

This is the rule doing its job, and it appears on the first start of the version
that introduced it (1.13.0) for keys issued long before. The key can report on
itself, with nothing but itself:

```bash
curl -s https://gateway/v1/me/key -H "Authorization: Bearer $KEY" | jq '.key | {limited_by, admin_access}'
```

Pick by what the key is for:

| The key is used for | Do |
|---|---|
| calling models only | Nothing. It still calls them |
| admin calls from a script | Issue that script a key with **no** model list, access group, workspace or quota, from the console, and replace it in the script |
| both, and the limit is `models` or `cap` | Take the limit off from the console — the key's `model` button under **Access & Keys**, or the policy in the **Quota** tab. It cannot be done with the limited key itself, and for an administrator's key only an administrator can do it |
| both, and the limit is `access_groups` or `workspace` | Those cannot be removed from an issued key. Issue a new one |

`limited_by` lists every limit on the key, so one look says whether taking a
single limit off would be enough.

Do not "fix" it by widening a key that a job only uses to call one model. A
people-shaped task — adding a member, changing a quota — belongs in the console,
where the session is not limited.

To find every such key at once rather than one `403` at a time: the key list
under **Access & Keys → API keys** marks them `no admin access`, and
`scripts/restricted_key_report.py` prints them from the database
([DEPLOYMENT.md](DEPLOYMENT.md#before-upgrading-keys-that-lose-admin-rights)).

## A manager cannot change, extend or revoke a key

*No alert. A manager gets `403 INSUFFICIENT_SCOPE` on a key that is in their own
key list.*

Seeing a key and deciding it are different things. Two kinds of key are not a
manager's to issue, amend, extend or revoke, even for people in their own
workspaces:

| The message says | The key is | Who can |
|---|---|---|
| `Only an admin can change an admin key.` (or `issue`, `revoke`) | an administrator's | an administrator |
| `A key of <owner>'s with no limit on it carries their manager rights …` (`reason_code: key_carries_rights`) | another manager's, with no limit on it — or it would have none after the change | an administrator, or that manager from their own console |

Nothing is broken. A key with no limit carries its owner's rights, so letting
the manager next door lift a limit, or issue one without, would hand them
rights over workspaces they do not manage. The fix is the one the message
names: ask the owner or an administrator. A manager can still issue another
manager a key *with* a model list or a workspace, and everything about members'
keys is as it was.

A third refusal belongs to the same family: a manager adding an administrator to
their workspace gets `403` with `reason_code: enrol_administrator` (from
1.13.0), and in a bulk enrolment the whole list is refused — nobody is added.
Take the administrators out of the list and send it again; an administrator who
should be in the workspace adds themselves, or another administrator does. An
administrator who was already a member before the upgrade is not affected, and
is not removed either — **Access & Keys → People** shows them as a row with role
`admin` and a non-empty Workspaces column.

## Reveal fails for a key

*No alert. An administrator presses Reveal and is refused.*

The key itself still works in every case below — only showing it again is
affected. The console replaces the Reveal button with the reason; the API puts
it in `details.seal_state`:

| State | What happened | Do |
|---|---|---|
| `none` | No copy was ever kept — the key was issued while reveal was off | Issue a replacement |
| `off` | A copy is stored, but `GW_KEY_REVEAL_SECRET` is no longer set | Put the secret back in `.env` and restart |
| `lost`, reason `unknown_secret` or `unreadable` | The copy was sealed under a secret that is not configured — the secret was changed without the old one being kept as `GW_KEY_REVEAL_SECRET_PREVIOUS`, or an older backup was restored | [Restoring a backup made before the secret was changed](#restoring-a-backup-made-before-the-secret-was-changed) — the same steps apply |
| `lost`, reason `damaged` | Sealed under a configured secret and the contents no longer open | No secret helps. Issue a replacement |
| `lost`, reason `unknown_format` | Written by a newer version than the one running | Upgrade |
| `lost`, reason `not_this_key` | The copy opens, but it is a copy of a **different** key, so it is not shown. A row of `api_keys` was changed outside the gateway — a restore or merge that mixed rows, or tampering — or `GW_API_KEY_PEPPER` changed after the key was issued | No secret helps and a re-seal leaves it alone. Issue a replacement, and find out how the row changed before trusting that database. If *every* copy says this, look at the pepper first |

The whole picture at once — how many copies are in each state, which keys are
lost, and what in the configuration is wrong:

```bash
cd /opt/litegate && sudo -u litegate .venv/bin/python -m app.tools keyvault status
```

or the **Sealed key copies** panel under **Access & Keys → API keys**, which
appears when something needs attention, or `GET /admin/key-vault`. The reason is
also on each row of the key list (`seal_reason`).

If the gateway is running a version **older** than the one that issued the key —
after a rollback to 1.12.1 or earlier — Reveal answers *"Only this key's hash
was stored … Issue a replacement instead."* for keys whose copies are perfectly
good. Do not issue replacements on that message; the copies open again after
upgrading back.

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

"Confirm it works" means an admin call, not a chat completion: issue it with no
model list, access group, workspace or quota of its own, and check
`GET /v1/me/key` answers `"admin_access": true`. A key that carries any of those
calls its models and is refused on `/admin`.

Both halves need an administrator: an administrator's key can be issued,
amended and revoked only by an administrator, from the console or with an
unlimited admin key.

### Change `GW_KEY_REVEAL_SECRET`

For when the secret that seals the API-key copies has to change — it leaked, or
policy says it is time. Done this way every key stays revealable throughout, and
you can stop after any step and pick up later.

Commands are for the native install (`/opt/litegate`, unit `litegate`). On
Docker Compose the two variables are not passed to the container as shipped —
see [DEPLOYMENT.md §10](DEPLOYMENT.md#configuration-and-deployment).

1. **Back up, and take the archive off the machine.**

   ```bash
   ./scripts/backup.sh --out /srv/backups
   ```

   The archive contains `.env`, and so the secret you are about to retire. It
   was a secret before and it still is. If the secret is set somewhere other
   than `.env`, record the old value separately.

2. **Edit `.env`: the old value moves down, the new one goes in its place.**

   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # the new secret
   ```

   ```
   GW_KEY_REVEAL_SECRET=<the new value>
   GW_KEY_REVEAL_SECRET_PREVIOUS=<the value GW_KEY_REVEAL_SECRET had>
   ```

   They must differ. The same value in both does nothing, and the gateway says
   so.

3. **Restart, and read what it found.**

   ```bash
   sudo systemctl restart litegate
   journalctl -u litegate -n 200 --no-pager | grep 'key vault'
   ```

   Expect `current=0 previous=N lost=0` and a warning that N copies still open
   only under the previous secret. Every key can still be revealed. If `lost` is
   not 0, stop and read [Reveal fails for a key](#reveal-fails-for-a-key) before
   going on — a re-seal will not fix those.

4. **Re-seal.** Either in the console — **Access & Keys → API keys → Re-seal N
   keys** — or:

   ```bash
   cd /opt/litegate && sudo -u litegate .venv/bin/python -m app.tools keyvault reseal
   ```

   The command line names the key ids it is moving from and to and asks you to
   type `reseal`; compare them with the ones the console shows, because the
   command uses the secrets of the shell it runs in, not necessarily the
   gateway's (`--yes` skips the question, for scripts). Either way it is safe to
   run twice, to interrupt, and to run while the gateway serves traffic.

   If it stops on an error part-way — a locked database, a dropped connection —
   it says how many copies it had moved, records that in the audit log, and
   exits with status `3`. What was moved stays moved and every copy still opens
   while both secrets are set; run it again to finish.

   **Not if you might still roll back.** Every copy a re-seal moves is written
   in the new format, which 1.12.1 and earlier cannot read. If this is the first
   rotation after upgrading and going back is still on the table, stop after
   step 3 — everything is revealable there — and come back to this step later.

5. **Check.**

   ```bash
   cd /opt/litegate && sudo -u litegate .venv/bin/python -m app.tools keyvault status; echo "exit $?"
   ```

   `previous=0 lost=0`, a note that the previous secret is no longer needed, and
   exit status `0`. Status `1` means copies are still waiting or cannot be
   opened; `2` means the command was refused; `3` (from `reseal` only) means a
   re-seal was stopped by an error part-way.

6. **Remove `GW_KEY_REVEAL_SECRET_PREVIOUS` from `.env` and restart.** The log
   line now reads `current=N previous=0 lost=0` with no warning. Do not skip
   this: a secret that is no longer needed should not stay on the machine.

7. **Take a new backup.** It is the first one whose database and `.env` agree on
   the new secret.

**Keep the old secret — or the archive from step 1 — for as long as you keep any
backup made before step 4.** Those databases are sealed under it.

What this does not cover: only one previous secret is supported, so finish one
change before starting the next. The count line is logged once per worker, so
with several workers you will see it several times. And the provider keys in
`data/secrets.json` are a different store that this secret does not protect.

### Restoring a backup made before the secret was changed

After a restore of an older backup, every key works and every sealed copy is
`lost`: the restored database is sealed under the secret that was current when
the backup was made, and an in-place restore leaves the live `.env` alone.
`scripts/restore.sh --in-place` warns about exactly this before it writes.

1. Get the backup's secret. It is in the archive's copy of `.env`.
   `restore.sh --in-place` prints the command for the archive it was given, with
   the real path and member name filled in; by hand it is:

   ```bash
   ARCHIVE=/srv/backups/litegate-20260813-020000.tar.gz
   tar -xzOf "$ARCHIVE" "$(basename "$ARCHIVE" .tar.gz)/env" | grep '^GW_KEY_REVEAL_SECRET='
   ```

   That prints a secret on your terminal. The member is named exactly, not with
   a `*/env` pattern: GNU tar, which is what the gateway's hosts have, does not
   expand patterns when extracting. If the archive was renamed, list it
   (`tar -tzf "$ARCHIVE"`) and use the path of its `env` entry instead.
2. Put that value in the live `.env` as `GW_KEY_REVEAL_SECRET_PREVIOUS`. Leave
   `GW_KEY_REVEAL_SECRET` as it is.
3. Start the gateway. The log line shows the copies under `previous=`.
4. Re-seal, check, then remove the variable and restart — steps 4 to 6 above.

The same steps recover from a secret that was changed by overwriting it, if the
old value can still be found: a `lost` copy says which secret it needs by key
id, and `python -m app.tools keyvault key-id` prints the key id of a secret you
type in, without echoing it.

### Before an upgrade

```bash
./scripts/backup.sh --out /srv/backups
```

Then read [DEPLOYMENT.md §6.1](DEPLOYMENT.md#61-restoring--rehearse-it-now).
If you have never restored one, that is the thing to do this week rather than
during an incident.

Then ask which keys the upgrade changes. From the directory holding the **new**
version, against the database of the one still running:

```bash
python scripts/restricted_key_report.py --db /opt/litegate/data/gateway.db
```

On a host updated with the console's **Update now** button the new version is
the folder `GW_UPDATE_SOURCE` names — pull it by hand first, because the button
only pulls when it runs. The button also leaves `scripts/` alone: after
upgrading to 1.13.0 that way, copy the fixed `backup.sh` and `restore.sh` (and
the report itself) into `/opt/litegate/scripts/` with the commands in
[DEPLOYMENT.md](DEPLOYMENT.md#the-update-button-and-why-it-is-built-this-way),
or the next restore on that host runs the old script.

It writes nothing. Exit `0` means no key changes; `1` means at least one does —
read the list; `2` means the report could not be produced (no such file, not a
gateway database, no driver, no permission), with the reason on stderr. What the
two lists mean and what to do about each key is in
[DEPLOYMENT.md §9](DEPLOYMENT.md#before-upgrading-keys-that-lose-admin-rights),
with what the upgrade changes on disk and what to undo before rolling back.
