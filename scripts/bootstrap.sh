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

# This script installs a systemd unit and reads the first-run credentials back
# out of the journal. Without a running systemd both of those are impossible.
#
# It used to find that out at `systemctl daemon-reload` near the very end, under
# `set -e` - by which point it had already created a system user, copied the
# tree, built a venv, written .env and dropped a unit file into
# /etc/systemd/system. The operator was left with a half-installed machine and
# an error about D-Bus. Check it before touching anything.
if ! [[ -d /run/systemd/system ]]; then
    die "no running systemd (this is normal in Docker, and in an LXC container
     started without an init).

  · Docker: use the image instead - docker/docker-compose.yml. This script
    has no equivalent there and is not needed.
  · LXC: start the container with systemd as init (the default for a
    distro container; 'lxc.init.cmd' must not be overridden), then re-run.
  · Anything else: install by hand with ./install.sh, which needs no systemd.

  See docs/DEPLOYMENT.md § 'Containers: LXC and Docker'."
fi

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

# โมเดลตัวอย่างชี้ไปที่ dgx01/02/03 — ชื่อเครื่องของเรา ไม่ใช่ของผู้ติดตั้ง
#
# ปล่อยไว้ = ภายในหนึ่งนาทีแรก log มี ERROR สิบกว่าบรรทัดว่า "Name or service not
# known" ทั้งที่ไม่มีอะไรเสียเลย · ผู้ติดตั้งคนแรกที่เปิด journalctl ดูจะสรุปว่าติดตั้ง
# ไม่สำเร็จ · ปิดไว้ก่อนแล้วให้เขาเพิ่มเครื่องจริงผ่านคอนโซล ซึ่งเป็นทางที่ควรเป็นอยู่แล้ว
log "Disabling the sample models (they point at machines that are not yours)"
python3 - "$INSTALL_DIR/config/models" <<'PY'
import pathlib, re, sys

# ปิดที่ระดับ *โมเดล* (spec.enabled) ไม่ใช่ระดับ endpoint
#
# ปิดทุก endpoint ไม่ได้ — สคีมาบังคับว่าต้องเปิดอย่างน้อยหนึ่งอัน ไฟล์จะกลายเป็น
# invalid แล้ว registry โหลดได้ 0 โมเดลพร้อม validation error (ลองมาแล้ว)
#
# ตัวตรวจสุขภาพเคารพ spec.enabled ตั้งแต่รุ่นนี้ ไฟล์จึงยังถูกต้อง อ่านเป็นตัวอย่างได้
# และไม่มีใครยิงไปหา dgx01/02/03 ที่ไม่ใช่เครื่องของผู้ติดตั้ง
changed = []
for path in sorted(pathlib.Path(sys.argv[1]).glob("*.y*ml")):
    if path.name.startswith("."):
        continue
    text = path.read_text(encoding="utf-8")
    if "dgx0" not in text:
        continue          # ของจริงที่ใครตั้งไว้เอง — ห้ามแตะ
    if re.search(r"^  enabled:", text, re.M):
        text = re.sub(r"^  enabled:.*$", "  enabled: false", text, count=1, flags=re.M)
    else:
        text = re.sub(r"^spec:$", "spec:\n  enabled: false", text, count=1, flags=re.M)
    path.write_text(text, encoding="utf-8")
    changed.append(path.name)
print("  ปิดไว้: " + (", ".join(changed) if changed else "(ไม่มี)"))
PY

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

# ทะเบียนที่ดูแลด้วย git — คอนโซลจะเสนอ YAML ให้ก็อปไปคอมมิตแทนการเขียนไฟล์เอง
#
# ค่าเริ่มต้นคือเขียนได้ เพราะบรรทัดสุดท้ายของสคริปต์นี้บอกให้ไปเพิ่มโมเดลที่คอนโซล ·
# ถ้าปิดไว้เป็นค่าเริ่มต้น ผู้ติดตั้งคนแรกจะเจอปุ่ม Save ที่กดไม่ได้ทันทีที่ทำตามคำแนะนำ
if [[ "${REGISTRY_READONLY:-0}" == "1" ]]; then
    sed -i "s#^ReadWritePaths=.*#ReadWritePaths=${INSTALL_DIR}/data ${INSTALL_DIR}/logs#" \
        "/etc/systemd/system/${SERVICE_NAME}.service"
    warn "REGISTRY_READONLY=1 — คอนโซลจะเพิ่ม/แก้โมเดลเองไม่ได้ ใช้ Preview YAML แล้วคอมมิตลง git"
fi

systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"

# `hostname -I` lists the addresses of this machine's own interfaces. On bare
# metal the first one is the LAN address an operator can actually reach. Inside
# an LXC container it is the veth address, and there is no guarantee anyone
# outside the host can route to it. Print it, but say what it is rather than
# handing over a URL that may not work.
advertise_addr() { hostname -I 2>/dev/null | awk '{print $1}'; }
ADDR="$(advertise_addr)"
if [[ -z "$ADDR" ]]; then
    ADDR="<this-host>"
fi

log "Waiting for the service to become healthy"
for _ in {1..30}; do
    if curl -fsS http://localhost:8080/healthz >/dev/null 2>&1; then
        echo
        log "Gateway is up: http://${ADDR}:8080"
        echo
        warn "Bootstrap admin key (shown once) - copy it now:"
        journalctl -u "$SERVICE_NAME" --no-pager | grep -A1 "BOOTSTRAP ADMIN KEY" | tail -2 || true
        echo
        # คอนโซลเข้าด้วย username/password ไม่ใช่ API key · เดิมพิมพ์แต่คีย์ ส่วนรหัสผ่าน
        # ไปอยู่ใน journal เฉย ๆ โดยไม่มีอะไรบอกให้ไปหา — คนติดตั้งจึงเข้าหน้าเว็บไม่ได้
        warn "Console sign-in (shown once):"
        journalctl -u "$SERVICE_NAME" --no-pager \
            | grep -A2 "CONSOLE SIGN-IN" | grep -E "username:|password:" | tail -2 || true
        echo
        echo "  ยังไม่มีโมเดลให้เรียก — โมเดลตัวอย่างถูกปิดไว้เพราะชี้ไปที่เครื่องของเรา"
        echo "  เพิ่มเครื่องจริงที่หน้า Models ในคอนโซล แล้วกด Detect"
        echo
        echo "  Console : http://${ADDR}:8080/console"
        echo "  Docs    : http://${ADDR}:8080/docs"
        echo "  Logs    : journalctl -u ${SERVICE_NAME} -f"
        echo
        if [[ -f /run/.containerenv || -f /.dockerenv ]] || grep -qa container=lxc /proc/1/environ 2>/dev/null; then
            warn "This is a container: ${ADDR} is its internal address."
            warn "Reach the console from outside via the host's port forward, not this URL."
        fi

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
