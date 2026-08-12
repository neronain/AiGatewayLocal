#!/usr/bin/env bash
# Restore a LiteGate from an archive made by scripts/backup.sh.
#
# This exists as a script rather than a paragraph in a runbook because a restore
# procedure nobody has executed is a guess. Run it once against a scratch
# database while everything is fine; the first time should not be during an
# incident.
#
# Usage:
#   ./scripts/restore.sh backups/litegate-20260813-020000.tar.gz --into ./restored
#   ./scripts/restore.sh backups/litegate-20260813-020000.tar.gz --in-place
#
# `--into DIR` unpacks and restores into a scratch directory, touching nothing
# that is running. That is the mode to rehearse with, and the default: this
# script will not overwrite a live deployment unless told to in so many words.
set -euo pipefail

ARCHIVE=""
TARGET=""
IN_PLACE=0
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
    cat <<'USAGE'
Usage: restore.sh ARCHIVE (--into DIR | --in-place)

  --into DIR   Restore into a scratch directory. Nothing running is touched.
  --in-place   Restore over this deployment. Stop the gateway first.

The archive contains the API key pepper. Restoring a database under a
different pepper invalidates every key ever issued and they cannot be
recovered - every member has to be given a new one. This script checks for
that and refuses rather than discovering it later.
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --into) TARGET="$2"; IN_PLACE=0; shift 2 ;;
        --in-place) IN_PLACE=1; TARGET="$ROOT"; shift ;;
        -h|--help) usage; exit 0 ;;
        -*) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
        *) ARCHIVE="$1"; shift ;;
    esac
done

[[ -n "$ARCHIVE" && -f "$ARCHIVE" ]] || { echo "ERROR: give a readable archive." >&2; usage >&2; exit 2; }
[[ -n "$TARGET" ]] || { echo "ERROR: choose --into DIR or --in-place." >&2; usage >&2; exit 2; }

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
tar -xzf "$ARCHIVE" -C "$work"
unpacked="$(find "$work" -maxdepth 1 -type d -name 'litegate-*' | head -1)"
[[ -n "$unpacked" ]] || { echo "ERROR: this does not look like a litegate backup." >&2; exit 1; }

echo "Restoring from $ARCHIVE"
[[ -f "$unpacked/MANIFEST" ]] && sed 's/^/  /' "$unpacked/MANIFEST"
echo

# --- the pepper check, before anything is written -------------------------
# Doing this first is the point. Restoring the data and then finding out the
# keys are dead is the failure this whole script exists to prevent.
backup_pepper=""
[[ -f "$unpacked/env" ]] && backup_pepper="$(grep -E '^GW_API_KEY_PEPPER=' "$unpacked/env" | tail -1 | cut -d= -f2- || true)"

if [[ "$IN_PLACE" -eq 1 ]]; then
    live_pepper="${GW_API_KEY_PEPPER:-}"
    if [[ -z "$live_pepper" && -f "$ROOT/.env" ]]; then
        live_pepper="$(grep -E '^GW_API_KEY_PEPPER=' "$ROOT/.env" | tail -1 | cut -d= -f2- || true)"
    fi
    if [[ -n "$live_pepper" && -n "$backup_pepper" && "$live_pepper" != "$backup_pepper" ]]; then
        cat >&2 <<'STOP'
REFUSING: this deployment's GW_API_KEY_PEPPER is not the one in the backup.

Every API key in the database is a hash under the pepper it was issued with.
Restoring across a change means every key ever issued stops working, with no
way to recover them - every member needs a new one.

If that is genuinely what you want, set the pepper from the backup first:
  grep GW_API_KEY_PEPPER <(tar -xzOf ARCHIVE '*/env')
STOP
        exit 1
    fi
    read -rp "Restore over $ROOT? Stop the gateway first. Type 'restore' to go on: " reply
    [[ "$reply" == "restore" ]] || { echo "Nothing was changed."; exit 1; }
fi

mkdir -p "$TARGET"
kind="$(cat "$unpacked/DATABASE_KIND" 2>/dev/null || echo unknown)"

case "$kind" in
    sqlite)
        mkdir -p "$TARGET/data"
        dest="$TARGET/data/gateway.db"
        [[ -f "$dest" ]] && cp "$dest" "$dest.before-restore-$(date +%s)"
        cp "$unpacked/database.sqlite" "$dest"
        # Prove it opens and holds what a gateway expects, rather than trusting
        # that a file of the right size is a database.
        users="$(sqlite3 "$dest" 'select count(*) from users;' 2>/dev/null || echo ERROR)"
        keys="$(sqlite3 "$dest" 'select count(*) from api_keys;' 2>/dev/null || echo ERROR)"
        [[ "$users" == "ERROR" ]] && { echo "ERROR: restored file is not a readable database." >&2; exit 1; }
        echo "  database   sqlite -> $dest ($users users, $keys keys)"
        ;;
    postgres)
        : "${GW_DATABASE_URL:?set GW_DATABASE_URL to the database to restore into}"
        command -v pg_restore >/dev/null || { echo "ERROR: pg_restore is not installed." >&2; exit 1; }
        pg_url="${GW_DATABASE_URL/+asyncpg/}"
        pg_url="${pg_url/+psycopg/}"
        # --clean drops what it is replacing; without it a restore onto a
        # populated database fails halfway and leaves a mixture of both.
        pg_restore --clean --if-exists --no-owner --no-privileges \
                   --dbname="$pg_url" "$unpacked/database.dump"
        echo "  database   postgres -> ${pg_url%%://*}://…"
        ;;
    *)
        echo "ERROR: archive does not say which kind of database it holds." >&2
        exit 1
        ;;
esac

if [[ -d "$unpacked/config" ]]; then
    [[ -d "$TARGET/config" ]] && mv "$TARGET/config" "$TARGET/config.before-restore-$(date +%s)"
    cp -R "$unpacked/config" "$TARGET/config"
    echo "  registry   $(find "$TARGET/config" -name '*.yaml' | wc -l | tr -d ' ') file(s)"
fi

if [[ -f "$unpacked/env" && "$IN_PLACE" -eq 0 ]]; then
    cp "$unpacked/env" "$TARGET/.env"
    chmod 600 "$TARGET/.env"
    echo "  .env       restored (mode 600)"
elif [[ -f "$unpacked/env" ]]; then
    echo "  .env       left alone — the live one already matches the backup"
fi

# --- ownership -------------------------------------------------------------
# A restore is usually run as root, which leaves every file owned by root. The
# gateway then runs as its own service user, can read the database but not write
# it, and fails at the first request with an error that says nothing about
# permissions. Found by doing exactly that.
owner=""
if [[ "$IN_PLACE" -eq 1 ]]; then
    # Match whatever already owns the deployment rather than guessing a name.
    owner="$(stat -c '%U:%G' "$ROOT" 2>/dev/null || stat -f '%Su:%Sg' "$ROOT" 2>/dev/null || true)"
elif [[ -n "${SUDO_USER:-}" ]]; then
    owner="$SUDO_USER"
fi
if [[ -n "$owner" && "$(id -u)" -eq 0 ]]; then
    chown -R "$owner" "$TARGET"
    echo "  ownership  $owner"
elif [[ "$(id -u)" -eq 0 ]]; then
    echo "  ownership  root — the gateway's service user must be able to WRITE"
    echo "             the database, not just read it:  chown -R <user> $TARGET"
fi
chmod 600 "$TARGET/data/gateway.db" 2>/dev/null || true

echo
if [[ "$IN_PLACE" -eq 1 ]]; then
    echo "Restored. Start the gateway, then check /readyz and that one existing"
    echo "API key still authenticates - that is what proves the pepper survived."
else
    echo "Restored into $TARGET without touching anything that is running."
    echo "To try it: cd $TARGET && uvicorn app.main:app --port 8099"
fi
