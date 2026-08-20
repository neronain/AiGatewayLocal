#!/usr/bin/env bash
# LiteGate installer — Linux (Debian/Ubuntu) และ macOS
#
#   ./install.sh                 ติดตั้ง + เตรียมให้ยิงได้จริงในคำสั่งเดียว
#   ./install.sh --demo          เหมือนข้างบน แล้วเปิด backend จำลอง + เกตเวย์ให้เลย
#   ./install.sh --no-start      ติดตั้งอย่างเดียว ไม่รันอะไร
#   ./install.sh --port 9000     เปลี่ยนพอร์ตของเกตเวย์ (default 8080)
#
# ทำไมต้องมีสคริปต์นี้: ก่อนหน้านี้ README ให้ประกอบเองสี่ขั้นข้ามสอง terminal —
# สร้าง venv, แก้ base_url ใน YAML ด้วย sed, เปิด backend จำลอง, แล้วค่อยรัน uvicorn
# ขั้นแก้ YAML ใช้ `sed -i ''` ซึ่งเป็นรูปของ macOS และ **พังบน Linux** ส่วนคนที่ข้าม
# ขั้นนั้นจะเจอ ERROR สามบรรทัดทันทีที่เปิด เพราะ config ตัวอย่างชี้ไปที่ dgx01/02/03
# ซึ่งไม่มีอยู่บนเครื่องเขา · ทั้งหมดนี้เกิดก่อนผู้ประเมินจะได้เห็นหน้าคอนโซลด้วยซ้ำ
set -Eeuo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${LITEGATE_VENV:-${REPO_DIR}/.venv}"
PORT="${LITEGATE_PORT:-8080}"
MOCK_PORT="${LITEGATE_MOCK_PORT:-8000}"
MODE="prepare"        # prepare | demo | none

c_head=$'\033[36m'; c_ok=$'\033[32m'; c_warn=$'\033[33m'; c_bad=$'\033[31m'; c_off=$'\033[0m'
log()  { printf '%s==>%s %s\n' "$c_head" "$c_off" "$*"; }
ok()   { printf '%s ✓%s %s\n'  "$c_ok"   "$c_off" "$*"; }
warn() { printf '%s !!%s %s\n' "$c_warn" "$c_off" "$*"; }
die()  { printf '%sERROR:%s %s\n' "$c_bad" "$c_off" "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --demo)     MODE="demo"; shift ;;
        --no-start) MODE="none"; shift ;;
        --port)     PORT="${2:?--port ต้องมีเลขพอร์ต}"; shift 2 ;;
        --port=*)   PORT="${1#*=}"; shift ;;
        -h|--help)  sed -n '2,9p' "$0"; exit 0 ;;
        *) die "ไม่รู้จักตัวเลือก: $1  (ดู --help)" ;;
    esac
done

# ── python ────────────────────────────────────────────────────────────────
# ต้องการ 3.11+ · เครื่องที่มีหลายเวอร์ชันมักให้ `python3` เป็นตัวเก่าของระบบ
# จึงไล่หาตัวที่ใหม่พอแทนที่จะยอมแพ้ตั้งแต่ตัวแรกที่เจอ
find_python() {
    local candidate
    for candidate in python3.13 python3.12 python3.11 python3; do
        command -v "$candidate" >/dev/null 2>&1 || continue
        if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
            echo "$candidate"; return 0
        fi
    done
    return 1
}

log "ตรวจสิ่งที่ต้องมี"
PY="$(find_python)" || die "ต้องใช้ Python 3.11 ขึ้นไป — ลง: sudo apt install python3.12 python3.12-venv"
ok "$PY ($("$PY" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))'))"

if ! "$PY" -c 'import venv' 2>/dev/null; then
    die "ไม่มีโมดูล venv — ลง: sudo apt install ${PY}-venv"
fi

# ── venv ──────────────────────────────────────────────────────────────────
if [[ -x "$VENV/bin/python" ]]; then
    log "ใช้ venv เดิมที่ $VENV"
else
    log "สร้าง venv ที่ $VENV"
    "$PY" -m venv "$VENV" || die "สร้าง venv ไม่สำเร็จ"
fi

log "ติดตั้งแพ็กเกจ (ครั้งแรกใช้เวลาสักครู่)"
"$VENV/bin/pip" install -q --upgrade pip >/dev/null
"$VENV/bin/pip" install -q -e "$REPO_DIR" || die "pip install ไม่สำเร็จ"
ok "ติดตั้งแล้ว"

# ── .env ──────────────────────────────────────────────────────────────────
# secret ต้องสุ่มตอนติดตั้ง ไม่ใช่ค่าตายตัวใน repo — ทุกที่ที่ลงจะได้ไม่ใช้ค่าเดียวกัน
ENV_FILE="$REPO_DIR/.env"
if [[ -f "$ENV_FILE" ]]; then
    log "ใช้ .env เดิม (ไม่เขียนทับ)"
else
    log "สร้าง .env พร้อม secret ที่สุ่มใหม่"
    umask 077
    {
        echo "# สร้างโดย install.sh — แก้ได้ตามต้องการ"
        echo "GW_ENV=development"
        echo "GW_PORT=${PORT}"
        echo "GW_API_KEY_PEPPER=$("$VENV/bin/python" -c 'import secrets; print(secrets.token_urlsafe(48))')"
        echo "GW_KEY_REVEAL_SECRET=$("$VENV/bin/python" -c 'import secrets; print(secrets.token_urlsafe(48))')"
    } > "$ENV_FILE"
    umask 022
    ok ".env"
fi

# ── config ────────────────────────────────────────────────────────────────
# ตัวอย่างที่ shipped มาชี้ไปที่ dgx01/02/03 ซึ่งเป็นชื่อเครื่องในบ้านเรา ไม่ใช่ของผู้ติดตั้ง
# ปล่อยไว้แบบนั้น = เปิดมาเจอ ERROR สามบรรทัดใน 30 วินาทีแรกและไม่มีอะไรบอกว่าให้แก้ตรงไหน
# แก้ให้ชี้มาที่ backend จำลองในเครื่อง แล้วของจริงค่อยเปลี่ยนทีหลังผ่านคอนโซล
log "ตั้ง endpoint ตัวอย่างให้ชี้มาที่เครื่องนี้"
"$VENV/bin/python" - "$REPO_DIR/config/models" "$MOCK_PORT" <<'PY'
import pathlib, re, sys

models_dir, mock_port = pathlib.Path(sys.argv[1]), sys.argv[2]
changed = []
for path in sorted(models_dir.glob("*.y*ml")):
    if path.name.startswith("."):
        continue          # `._ชื่อไฟล์` ที่ macOS แถมมา ไม่ใช่ config และไม่ใช่ UTF-8
    text = path.read_text(encoding="utf-8")
    # เฉพาะโฮสต์ตัวอย่างของเราเท่านั้น — ถ้าใครตั้งของจริงไว้แล้วห้ามไปแตะ
    new = re.sub(r"http://dgx\d+:\d+", f"http://127.0.0.1:{mock_port}", text)
    if new != text:
        path.write_text(new, encoding="utf-8")
        changed.append(path.name)
print("  แก้แล้ว: " + (", ".join(changed) if changed else "(ไม่มีอะไรต้องแก้)"))
PY
ok "config พร้อม"

# ── ตรวจว่าติดตั้งได้จริง ──────────────────────────────────────────────────
log "ตรวจว่าโหลดแอปขึ้นจริง"
"$VENV/bin/python" -c 'from app.main import create_app; create_app()' >/dev/null 2>&1 \
    || die "แอปโหลดไม่ขึ้น — ส่ง output ข้างบนมาให้ทีมดู"
ok "แอปโหลดได้"

port_busy() { "$VENV/bin/python" - "$1" <<'PY'
import socket, sys
s = socket.socket()
try:
    s.bind(("127.0.0.1", int(sys.argv[1])))
except OSError:
    raise SystemExit(0)   # ไม่ว่าง
finally:
    s.close()
raise SystemExit(1)       # ว่าง
PY
}

for p in "$PORT" "$MOCK_PORT"; do
    if port_busy "$p"; then
        warn "พอร์ต $p มีคนใช้อยู่แล้ว — เปลี่ยนด้วย ./install.sh --port <เลข> หรือหยุดตัวที่ใช้อยู่"
    fi
done

echo
if [[ "$MODE" == "none" ]]; then
    log "ติดตั้งเสร็จ (ยังไม่ได้รันอะไร)"
    echo
    echo "  เปิด backend จำลอง:  $VENV/bin/python scripts/mock_backend.py --port $MOCK_PORT"
    echo "  เปิดเกตเวย์:          $VENV/bin/uvicorn app.main:app --port $PORT"
    exit 0
fi

if [[ "$MODE" == "prepare" ]]; then
    log "ติดตั้งเสร็จ"
    echo
    echo "  รันทั้งชุดเลย:  ./install.sh --demo"
    echo "  หรือทีละตัว:"
    echo "    $VENV/bin/python scripts/mock_backend.py --port $MOCK_PORT   # terminal 1"
    echo "    $VENV/bin/uvicorn app.main:app --port $PORT                  # terminal 2"
    echo
    echo "  ต่อ HTTPS:      sudo scripts/install_tls.sh"
    echo "  ลงจริงเป็น service: sudo scripts/bootstrap.sh   (ดู docs/DEPLOYMENT.md)"
    exit 0
fi

# ── โหมด demo: เปิดให้ครบแล้วพิสูจน์ว่ายิงผ่านจริง ────────────────────────
LOG_DIR="$REPO_DIR/.run"
mkdir -p "$LOG_DIR"
cleanup() {
    [[ -n "${MOCK_PID:-}"    ]] && kill "$MOCK_PID"    2>/dev/null || true
    [[ -n "${GATEWAY_PID:-}" ]] && kill "$GATEWAY_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

log "เปิด backend จำลองที่พอร์ต $MOCK_PORT"
"$VENV/bin/python" "$REPO_DIR/scripts/mock_backend.py" --port "$MOCK_PORT" \
    >"$LOG_DIR/mock.log" 2>&1 &
MOCK_PID=$!

log "เปิดเกตเวย์ที่พอร์ต $PORT"
( cd "$REPO_DIR" && "$VENV/bin/uvicorn" app.main:app --port "$PORT" ) \
    >"$LOG_DIR/gateway.log" 2>&1 &
GATEWAY_PID=$!

log "รอให้พร้อม"
for _ in $(seq 1 60); do
    if curl -fsS -m 2 "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then break; fi
    kill -0 "$GATEWAY_PID" 2>/dev/null || { tail -20 "$LOG_DIR/gateway.log"; die "เกตเวย์ดับระหว่างเปิด — log อยู่ข้างบน"; }
    sleep 1
done
curl -fsS -m 3 "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1 \
    || { tail -20 "$LOG_DIR/gateway.log"; die "เกตเวย์ไม่ตอบ /health — log อยู่ข้างบน"; }
ok "เกตเวย์ตอบแล้ว"

# คีย์แอดมินถูกพิมพ์ครั้งเดียวตอนบูต — ดึงจาก log ให้เลย ผู้ใช้จะได้ไม่ต้องไปหาเอง
ADMIN_KEY="$(grep -oE 'lg_sk_[A-Za-z0-9_-]+' "$LOG_DIR/gateway.log" | head -1 || true)"
CONSOLE_USER="$(grep -A2 'CONSOLE SIGN-IN' "$LOG_DIR/gateway.log" | grep -oE 'username: .*' | awk '{print $2}' || true)"
CONSOLE_PASS="$(grep -A3 'CONSOLE SIGN-IN' "$LOG_DIR/gateway.log" | grep -oE 'password: .*' | awk '{print $2}' || true)"

# ยิงจริงหนึ่งครั้งผ่านทางเดียวกับที่ลูกค้าจะใช้ — "ติดตั้งเสร็จ" ที่ไม่ได้พิสูจน์ ไม่มีค่า
if [[ -n "$ADMIN_KEY" ]]; then
    log "ยิงทดสอบผ่าน /v1/chat/completions"
    reply="$(curl -fsS -m 20 "http://127.0.0.1:${PORT}/v1/chat/completions" \
        -H "Authorization: Bearer $ADMIN_KEY" -H 'Content-Type: application/json' \
        -d '{"model":"coding","messages":[{"role":"user","content":"ping"}]}' 2>/dev/null || true)"
    if [[ -n "$reply" ]]; then ok "ยิงผ่าน — เกตเวย์คุยกับ backend ได้จริง"
    else warn "ยิงไม่ผ่าน — ดู $LOG_DIR/gateway.log"; fi
fi

echo
printf '%s─────────────────────────────────────────────%s\n' "$c_head" "$c_off"
echo "  คอนโซล : http://127.0.0.1:${PORT}/console/"
if [[ -n "$CONSOLE_USER" || -n "$ADMIN_KEY" ]]; then
    [[ -n "$CONSOLE_USER" ]] && echo "  ผู้ใช้   : ${CONSOLE_USER}"
    [[ -n "$CONSOLE_PASS" ]] && echo "  รหัสผ่าน : ${CONSOLE_PASS}"
    [[ -n "$ADMIN_KEY"    ]] && echo "  API key : ${ADMIN_KEY}"
else
    # รอบที่สองเป็นต้นไป ฐานข้อมูลมีบัญชีแล้ว ระบบจึงไม่พิมพ์รหัสซ้ำ · ปล่อยว่างไว้เฉย ๆ
    # อ่านเหมือนติดตั้งไม่ครบ ทั้งที่มันถูกต้อง — บอกไปตรง ๆ ว่าทำไมและถ้าลืมรหัสทำยังไง
    echo "  บัญชี   : ตั้งไว้แล้วตั้งแต่รอบแรก (ระบบไม่พิมพ์รหัสซ้ำ)"
    echo "            ลืมรหัส = ลบ data/gateway.db แล้วรันใหม่ (ล้างข้อมูลทั้งหมด)"
fi
echo "  หน้าแรก : http://127.0.0.1:${PORT}/"
printf '%s─────────────────────────────────────────────%s\n' "$c_head" "$c_off"
echo
echo "  log: $LOG_DIR/gateway.log · $LOG_DIR/mock.log"
echo "  Ctrl-C เพื่อหยุดทั้งสองตัว"
echo
wait "$GATEWAY_PID"
