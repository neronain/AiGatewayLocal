#!/usr/bin/env bash
# Back up everything a LiteGate needs to be rebuilt.
#
# Three things matter here, and only one of them is the database.
#
#   * The **pepper** (`GW_API_KEY_PEPPER`). Every API key ever issued is a hash
#     under it. Restore a database with a different pepper and every key in the
#     wild stops working, silently, with no way to recover them - members have
#     to be re-issued keys one by one. The pepper lives in `.env`, so `.env` is
#     part of the backup, and that makes the archive a secret.
#   * The **registry** (`config/`). Plain YAML that is probably in git, but a
#     restore that needs someone to remember which branch is a restore that
#     happens badly at 3am.
#   * The **database**. Members, keys, quota policies and usage history.
#
# Usage:
#   ./scripts/backup.sh                       # into ./backups
#   ./scripts/backup.sh --out /srv/backups    # somewhere else
#   ./scripts/backup.sh --keep 14             # prune archives older than 14
#
# Reads GW_DATABASE_URL from the environment or from .env, and handles both
# SQLite and PostgreSQL. Writes one timestamped .tar.gz, then verifies it can be
# listed - a tar that cannot be read is not a backup, and finding that out here
# costs nothing.
set -euo pipefail

OUT_DIR="./backups"
KEEP=30
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
    cat <<'USAGE'
Usage: backup.sh [--out DIR] [--keep N]

  --out   Where to write the archive (default ./backups)
  --keep  Delete archives older than N days (default 30, 0 to keep everything)

Restore with scripts/restore.sh. Test that at least once before you need it -
an untested backup is a guess.
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --out)  OUT_DIR="$2"; shift 2 ;;
        --keep) KEEP="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

cd "$ROOT"

# .env is not exported by sourcing it in a subshell, so read the one value we
# need without pulling the whole file into this shell's environment.
if [[ -z "${GW_DATABASE_URL:-}" && -f .env ]]; then
    GW_DATABASE_URL="$(grep -E '^GW_DATABASE_URL=' .env | tail -1 | cut -d= -f2- || true)"
fi
GW_DATABASE_URL="${GW_DATABASE_URL:-sqlite+aiosqlite:///./data/gateway.db}"

stamp="$(date +%Y%m%d-%H%M%S)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
staging="$work/litegate-$stamp"
mkdir -p "$staging"

echo "Backing up LiteGate ($stamp)"

# --- the database ---------------------------------------------------------
case "$GW_DATABASE_URL" in
    *sqlite*)
        db_path="${GW_DATABASE_URL#*:///}"
        db_path="${db_path%%\?*}"
        [[ -f "$db_path" ]] || { echo "ERROR: no database at $db_path" >&2; exit 1; }
        # `.backup` rather than cp: it takes a consistent snapshot of a database
        # that is being written to. Copying the file while the gateway is
        # serving can capture a torn write-ahead log.
        command -v sqlite3 >/dev/null || { echo "ERROR: sqlite3 is not installed." >&2; exit 1; }
        sqlite3 "$db_path" ".backup '$staging/database.sqlite'"
        echo "  database   sqlite  $(du -h "$staging/database.sqlite" | cut -f1)"
        echo "sqlite" > "$staging/DATABASE_KIND"
        ;;
    *postgresql*|*postgres*)
        command -v pg_dump >/dev/null || { echo "ERROR: pg_dump is not installed." >&2; exit 1; }
        # Strip the SQLAlchemy driver suffix; libpq does not understand it.
        pg_url="${GW_DATABASE_URL/+asyncpg/}"
        pg_url="${pg_url/+psycopg/}"
        # Custom format: compressed, and restorable table by table if only one
        # thing needs putting back.
        pg_dump --format=custom --no-owner --no-privileges --file="$staging/database.dump" "$pg_url"
        echo "  database   postgres  $(du -h "$staging/database.dump" | cut -f1)"
        echo "postgres" > "$staging/DATABASE_KIND"
        ;;
    *)
        echo "ERROR: cannot back up this database URL." >&2
        exit 1
        ;;
esac

# --- the registry ---------------------------------------------------------
if [[ -d config ]]; then
    cp -R config "$staging/config"
    echo "  registry   $(find config -name '*.yaml' | wc -l | tr -d ' ') file(s)"
fi

# --- the secret that makes the database useful ----------------------------
if [[ -f .env ]]; then
    cp .env "$staging/env"
    echo "  .env       included — THIS ARCHIVE IS A SECRET"
else
    cat > "$staging/NO_ENV_WARNING" <<'WARN'
No .env was found next to this backup.

If GW_API_KEY_PEPPER is set some other way - a systemd unit, a secret manager,
compose environment - make sure it is backed up there. Restoring this database
under a different pepper invalidates every API key ever issued, and they cannot
be recovered: every member has to be given a new one.
WARN
    echo "  .env       NOT FOUND — see NO_ENV_WARNING in the archive"
fi

cat > "$staging/MANIFEST" <<MANIFEST
litegate-backup
created:  $(date -u +%Y-%m-%dT%H:%M:%SZ)
host:     $(hostname)
database: ${GW_DATABASE_URL%%://*}://…
restore:  scripts/restore.sh $stamp.tar.gz
MANIFEST

mkdir -p "$OUT_DIR"
archive="$OUT_DIR/litegate-$stamp.tar.gz"
tar -czf "$archive" -C "$work" "litegate-$stamp"
# The archive holds the pepper. Anyone who can read it can forge API keys.
chmod 600 "$archive"

# Read it back. A tar that cannot be listed is not a backup, and the cheapest
# moment to learn that is now rather than during an incident.
tar -tzf "$archive" >/dev/null || { echo "ERROR: archive is unreadable." >&2; exit 1; }

echo
echo "Wrote $archive ($(du -h "$archive" | cut -f1), mode 600)"

if [[ "$KEEP" -gt 0 ]]; then
    pruned="$(find "$OUT_DIR" -name 'litegate-*.tar.gz' -mtime "+$KEEP" -print -delete | wc -l | tr -d ' ')"
    [[ "$pruned" -gt 0 ]] && echo "Pruned $pruned archive(s) older than $KEEP days"
fi

cat <<'NEXT'

Two things this does not do for you:

  * Copy the archive off this machine. A backup on the disk that fails is a
    file, not a backup.
  * Prove it restores. Run scripts/restore.sh against a scratch database once,
    now, while nothing is wrong.
NEXT
