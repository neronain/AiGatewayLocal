#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Native (non-Docker) install of the LiteGate on Debian/Ubuntu.
#
#   sudo ./scripts/bootstrap.sh                 # install to /opt/litegate
#   sudo INSTALL_DIR=/srv/litegate ./scripts/bootstrap.sh
#
# Installs into a dedicated venv, creates a system user, writes .env with a
# generated pepper, and installs + starts the systemd unit.
# ---------------------------------------------------------------------------
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/litegate}"
SERVICE_USER="${SERVICE_USER:-litegate}"
SERVICE_NAME="litegate"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!!\033[0m %s\n' "$*"; }
die()  { printf '\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root (sudo $0)"

log "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-dev build-essential curl

if ! id "$SERVICE_USER" &>/dev/null; then
    log "Creating service user '$SERVICE_USER'"
    useradd --system --create-home --home-dir "$INSTALL_DIR" \
            --shell /usr/sbin/nologin "$SERVICE_USER"
fi

log "Copying application to $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
for item in app config scripts pyproject.toml README.md; do
    [[ -e "$REPO_DIR/$item" ]] && cp -r "$REPO_DIR/$item" "$INSTALL_DIR/"
done
mkdir -p "$INSTALL_DIR/data" "$INSTALL_DIR/logs"

log "Creating virtualenv"
python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install --quiet "$INSTALL_DIR"

if [[ ! -f "$INSTALL_DIR/.env" ]]; then
    log "Generating .env with a fresh API key pepper"
    PEPPER="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
    cat > "$INSTALL_DIR/.env" <<EOF
GW_ENV=production
GW_HOST=0.0.0.0
GW_PORT=8080
GW_LOG_LEVEL=INFO
GW_WORKERS=4

# Rotating this invalidates every issued API key.
GW_API_KEY_PEPPER=${PEPPER}

GW_DATABASE_URL=sqlite+aiosqlite:///${INSTALL_DIR}/data/gateway.db
GW_REDIS_URL=
GW_CONFIG_DIR=${INSTALL_DIR}/config
GW_REGISTRY_RELOAD_SECONDS=30
GW_CORS_ORIGINS=

# Upstream backend credentials referenced by config/models/*.yaml
DGX01_API_KEY=
DGX02_API_KEY=
DGX03_API_KEY=
EOF
else
    warn ".env already exists, leaving it untouched"
fi

chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
chmod 600 "$INSTALL_DIR/.env"

log "Installing systemd unit"
sed "s#/opt/litegate#${INSTALL_DIR}#g; s#User=litegate#User=${SERVICE_USER}#; s#Group=litegate#Group=${SERVICE_USER}#" \
    "$REPO_DIR/deploy/systemd/${SERVICE_NAME}.service" > "/etc/systemd/system/${SERVICE_NAME}.service"

systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"

log "Waiting for the service to become healthy"
for _ in {1..30}; do
    if curl -fsS http://localhost:8080/healthz >/dev/null 2>&1; then
        echo
        log "Gateway is up: http://$(hostname -I | awk '{print $1}'):8080"
        echo
        warn "Bootstrap admin key (shown once) - copy it now:"
        journalctl -u "$SERVICE_NAME" --no-pager | grep -A1 "BOOTSTRAP ADMIN KEY" | tail -2 || true
        echo
        echo "  Console : http://$(hostname -I | awk '{print $1}'):8080/console"
        echo "  Docs    : http://$(hostname -I | awk '{print $1}'):8080/docs"
        echo "  Logs    : journalctl -u ${SERVICE_NAME} -f"
        echo

        # HTTPS belongs to the install, not to a follow-up someone gets to
        # later. Plenty of clients refuse http:// outright, and a gateway that
        # only speaks plain HTTP is one those clients simply cannot use - which
        # surfaces weeks in, as "the SDK does not work", not as a TLS problem.
        if [[ "${SKIP_TLS:-}" == "1" ]]; then
            warn "SKIP_TLS=1 - HTTPS not configured. Run scripts/install_tls.sh when ready."
        else
            log "Setting up HTTPS"
            "$REPO_DIR/scripts/install_tls.sh" || {
                warn "HTTPS setup failed. The gateway is running on :8080; fix and re-run"
                warn "  sudo ./scripts/install_tls.sh"
            }
        fi
        exit 0
    fi
    sleep 1
done

die "service did not become healthy; check: journalctl -u ${SERVICE_NAME} -n 60"
