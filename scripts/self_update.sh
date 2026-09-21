#!/usr/bin/env bash
# ติดตั้งโค้ดใหม่ทับของเดิมแล้วรีสตาร์ต — ถูกเรียกโดย systemd เท่านั้น ไม่ใช่โดยคน
#
# ## ทำไมเป็นสคริปต์แยก แทนที่จะให้เกตเวย์ทำเอง
#
# เกตเวย์รันเป็น `litegate` ซึ่ง **ไม่มีสิทธิ์ sudo** และ unit ตั้ง `ProtectSystem=strict`
# ให้เขียนได้แค่ `data logs config` — `/opt/litegate/app` จึงเป็น read-only สำหรับตัวมันเอง
#
# **นั่นคือการออกแบบที่ถูกและห้ามรื้อ**: ถ้าเปิดให้แอปเขียนโค้ดตัวเองได้ ช่องโหว่ใด ๆ
# ในเกตเวย์ก็กลายเป็นของถาวรทันที
#
# ทางที่ไม่ต้องแลกอะไรคือให้ `litegate-update.path` เฝ้าไฟล์คำขอใน `data/` (ที่แอปเขียนได้
# อยู่แล้ว) แล้วสั่ง `litegate-update.service` มารันไฟล์นี้ในฐานะ root · แอปไม่ได้สิทธิ์
# เพิ่มแม้แต่นิดเดียว และสิ่งเดียวที่มันสั่งได้คือ "อัปเดต" ไม่ใช่คำสั่งอะไรก็ได้
#
# ## ขอบเขตที่สคริปต์นี้ไม่ข้าม
#
# * **ไม่ดาวน์โหลดอะไรเลย** — โค้ดใหม่ต้องมีอยู่บนเครื่องแล้ว · ไซต์ที่ตัดขาดอินเทอร์เน็ต
#   ต้องอัปเดตได้ และเกตเวย์ที่ดึงโค้ดจากเน็ตมารันเองไม่ใช่ของที่ลูกค้าตกลงเอาไปติดตั้ง
# * **แตะเฉพาะ `app/`** — ไม่แตะ `.env` ไม่แตะ `config/` ไม่แตะ `data/` ไม่แตะ venv
# * **ทุกขั้นตอนเหมือน `docs/DEPLOYMENT.md § When the host has no checkout`** ซึ่งเขียนไว้เอง
#   ว่าทุกขั้น *"exists because skipping it has cost an outage"*
# * ล้มที่ขั้นไหนก็ตามหลังเริ่มเขียนไฟล์ = **กู้คืนของเดิมแล้วรีสตาร์ตกลับ**
set -Eeuo pipefail

INSTALL_DIR="${GW_INSTALL_DIR:-/opt/litegate}"
STATE="${INSTALL_DIR}/data"
REQUEST="${STATE}/update.request"
LOG="${STATE}/update.log"
STATUS="${STATE}/update.status"
FORCE="${STATE}/update.force"
SERVICE="${GW_SERVICE_NAME:-litegate}"
OWNER="$(stat -c '%U' "$INSTALL_DIR" 2>/dev/null || echo litegate)"

mkdir -p "$STATE"
: > "$LOG"
# คอนโซลอ่านสองไฟล์นี้กลับไปแสดง จึงต้องเป็นของผู้ใช้เดียวกับที่เกตเวย์รันอยู่
chown "$OWNER:$OWNER" "$LOG" 2>/dev/null || true
exec > >(tee -a "$LOG") 2>&1

say() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
mark() { printf '%s' "$1" > "$STATUS"; chown "$OWNER:$OWNER" "$STATUS" 2>/dev/null || true; }

# ลบคำขอทิ้งตั้งแต่ต้น — ไม่งั้น path unit จะยิงซ้ำทันทีที่สคริปต์จบ กลายเป็นลูปไม่รู้จบ
# อ่าน flag ก่อนลบ — ทั้งสองไฟล์เป็นคำขอครั้งนี้ ต้องไม่ค้างไปถึงครั้งหน้า
SKIP_DEPS=0; [ -f "$FORCE" ] && SKIP_DEPS=1
rm -f "$REQUEST" "$FORCE"
mark running
# ล้มหลังเริ่มเขียนไฟล์แล้ว = ต้องกู้คืน ไม่ใช่แค่ทำเครื่องหมายว่าล้ม · ก่อนหน้านั้น
# ยังไม่มีอะไรถูกแตะ จึงไม่ต้องทำอะไร (เจอจริง: rsync ไม่มีบนเครื่อง 2026-09-22)
WRITING=0
on_error() {
  say "ล้มกลางทางที่บรรทัด $1"
  if [ "$WRITING" = 1 ]; then restore; mark rolled-back; else mark failed; fi
}
trap 'on_error $LINENO' ERR

# ── แหล่งโค้ดใหม่ ────────────────────────────────────────────────────────────
# อ่านจาก `.env` ด้วย ไม่ใช่จาก environment อย่างเดียว
#
# `.env` ถูกโหลดโดย `litegate.service` ผ่าน `EnvironmentFile` — แต่ unit ของ updater
# เป็นคนละตัวและไม่ได้โหลดมัน · ตอนแรกอ่านแต่ environment แล้วได้ "ไม่มีแหล่งโค้ด"
# ทั้งที่ผู้ดูแลตั้งไว้ใน .env เรียบร้อย (เจอจริงตอนกดปุ่มครั้งแรก 2026-09-22)
env_value() { sed -n "s/^$1=//p" "${INSTALL_DIR}/.env" 2>/dev/null | head -1; }

SOURCE="${GW_UPDATE_SOURCE:-$(env_value GW_UPDATE_SOURCE)}"
[ -z "$SOURCE" ] && [ -d "${INSTALL_DIR}/.git" ] && SOURCE="$INSTALL_DIR"
if [ -z "$SOURCE" ] || [ ! -d "${SOURCE}/app" ]; then
  say "ไม่มีแหล่งโค้ดให้อัปเดตจาก"
  say "ตั้ง GW_UPDATE_SOURCE=<โฟลเดอร์ที่มี app/> ใน .env แล้วกดใหม่"
  say "(เกตเวย์ไม่ดาวน์โหลดโค้ดเอง โดยตั้งใจ — ไซต์ที่ไม่มีเน็ตต้องใช้ได้)"
  mark no-source; exit 1
fi
say "แหล่งโค้ด: $SOURCE"

# `git pull` เป็นของอำนวยความสะดวก **ไม่ใช่เงื่อนไขบังคับ**
#
# งานของสคริปต์นี้คือติดตั้งสิ่งที่อยู่ในโฟลเดอร์ต้นทาง · โฟลเดอร์นั้นอาจถูก rsync มา
# อาจถูก pull ด้วยมือไปแล้ว หรือเครื่องอาจไม่มี git ติดตั้งเลย (เกตเวย์หลายเครื่องไม่มี —
# เจอจริงตอนกดปุ่มครั้งที่สอง 2026-09-22) · ล้มทั้งงานเพราะ pull ไม่ได้คือการปฏิเสธ
# อัปเดตที่ทำได้อยู่แล้ว
if [ -d "${SOURCE}/.git" ]; then
  if ! command -v git >/dev/null 2>&1; then
    say "ไม่มี git บนเครื่องนี้ — ข้าม pull แล้วติดตั้งจากที่มีอยู่ในโฟลเดอร์ต้นทาง"
  elif git -C "$SOURCE" pull --ff-only; then
    say "ดึงโค้ดใหม่แล้ว"
  else
    say "git pull ไม่สำเร็จ — ติดตั้งจากที่มีอยู่ในโฟลเดอร์ต้นทางแทน"
  fi
fi

ver() { sed -n 's/^VERSION = "\(.*\)"/\1/p' "$1/app/config.py" 2>/dev/null | head -1; }
say "รุ่นที่ติดตั้งอยู่ $(ver "$INSTALL_DIR") → ที่จะติดตั้ง $(ver "$SOURCE")"

# ── dependency เปลี่ยน = หยุด ไม่ใช่เดินต่อ ─────────────────────────────────
# โค้ดใหม่บน venv เก่าจะตายตอน import ซึ่ง rollback กู้ได้ แต่เสียเวลาเปล่าและทำให้
# เกตเวย์ดับชั่วคราวโดยไม่จำเป็น · บอกไปตรง ๆ แล้วให้คนลง dependency เองดีกว่า
# เทียบ **เฉพาะส่วนที่เป็น dependency** ไม่ใช่ทั้งไฟล์
#
# เลขเวอร์ชันอยู่ใน pyproject ด้วยและขยับทุกรุ่น — เทียบทั้งไฟล์จึงดังทุกครั้งแล้วบล็อก
# การอัปเดตที่ไม่ได้เปลี่ยน dependency เลย ซึ่งคือกรณีส่วนใหญ่
deps_differ() {
  "${INSTALL_DIR}/.venv/bin/python" - "$1" "$2" <<'PYEOF'
import sys, tomllib

def deps(path):
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except Exception:
        return None          # อ่านไม่ได้ = ไม่รู้ ไม่ใช่ "เหมือนกัน"
    project = data.get("project") or {}
    return (
        sorted(project.get("dependencies") or []),
        {k: sorted(v) for k, v in (project.get("optional-dependencies") or {}).items()},
        project.get("requires-python"),
    )

a, b = deps(sys.argv[1]), deps(sys.argv[2])
sys.exit(0 if (a is None or b is None or a != b) else 1)
PYEOF
}

if [ "$SKIP_DEPS" = 1 ]; then
  say "ข้ามด่าน dependency ตามที่ผู้ดูแลยืนยันว่าลงเองแล้ว"
elif [ -f "${SOURCE}/pyproject.toml" ] && [ -f "${INSTALL_DIR}/pyproject.toml" ] \
   && deps_differ "${INSTALL_DIR}/pyproject.toml" "${SOURCE}/pyproject.toml"; then
  say "dependency ต่างจากที่ติดตั้งไว้ — ปุ่มนี้ไม่แตะ venv ให้"
  say "รันเอง: sudo ${INSTALL_DIR}/.venv/bin/pip install -e '${SOURCE}'"
  say "แล้วกดปุ่มอีกครั้งพร้อมยืนยันว่าลง dependency แล้ว"
  mark needs-deps; exit 1
fi

# ── 1/5 สำรอง ────────────────────────────────────────────────────────────────
BACKUP="$(dirname "$INSTALL_DIR")/gw-backup-$(date +%Y%m%d-%H%M%S)"
say "1/5 สำรอง app/ ไปที่ $BACKUP"
mkdir -p "$BACKUP"
cp -a "${INSTALL_DIR}/app" "$BACKUP/"

# เก็บไว้ย้อนกลับได้จริงไม่กี่ชุดพอ — ชุดละ ~4MB ถ้าไม่ลบเลย เครื่องที่กด Update
# ทุกสัปดาห์จะสะสมจนเต็ม /opt ในปีเดียว (เจอจริง: 19 ชุด 474MB บนเครื่องเดโม)
#
# ลบ **หลังอัปเดตสำเร็จเท่านั้น** — ถ้าล้มกลางทางต้องเหลือทุกชุดไว้ให้ไล่ย้อน
# และคัดเฉพาะชื่อที่สคริปต์นี้ตั้งเอง (gw-backup-<8หลัก>-<6หลัก>) ไม่แตะของที่คนอื่นวางไว้
KEEP_BACKUPS="${GW_KEEP_BACKUPS:-5}"
prune_backups() {
  local parent; parent="$(dirname "$INSTALL_DIR")"
  local old; old=$(find "$parent" -maxdepth 1 -type d \
        -regex '.*/gw-backup-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9]' \
        2>/dev/null | sort -r | tail -n +$((KEEP_BACKUPS + 1)))
  [ -z "$old" ] && return 0
  local n=0
  while IFS= read -r dir; do
    [ -n "$dir" ] || continue
    [ "$dir" = "$BACKUP" ] && continue          # ของรอบนี้ ห้ามลบไม่ว่ากรณีใด
    rm -rf -- "$dir" && n=$((n + 1))
  done <<< "$old"
  [ "$n" -gt 0 ] && say "เก็บกวาด: ลบ backup เก่า $n ชุด (เก็บไว้ $KEEP_BACKUPS ชุดล่าสุด)"
  return 0
}

restore() {
  say "กู้คืนจาก $BACKUP"
  rm -rf "${INSTALL_DIR}/app"
  cp -a "${BACKUP}/app" "${INSTALL_DIR}/app"
  chown -R "$OWNER:$OWNER" "${INSTALL_DIR}/app"
  systemctl restart "$SERVICE" || true
}

# ── 2/5 syntax ───────────────────────────────────────────────────────────────
say "2/5 ตรวจ syntax ของโค้ดใหม่ทั้งชุด"
"${INSTALL_DIR}/.venv/bin/python" -m compileall -q "${SOURCE}/app"

# ── 3/5 ติดตั้ง ──────────────────────────────────────────────────────────────
say "3/5 ติดตั้งไฟล์"
WRITING=1
# ไฟล์ที่ถูก *ลบ* ในรุ่นใหม่ต้องหายไปจริง — ไม่งั้นโมดูลเก่าค้างอยู่แล้วถูก import แทนของใหม่
# ซึ่งเป็นอาการที่หาสาเหตุยากที่สุดแบบหนึ่ง · `rsync --delete` ทำให้ตรงนี้ แต่ **เกตเวย์
# หลายเครื่องไม่มี rsync** (เจอจริง 2026-09-22) จึงต้องมีทางสำรองที่ให้ผลเดียวกัน
if command -v rsync >/dev/null 2>&1; then
  rsync -a --delete --exclude '__pycache__' "${SOURCE}/app/" "${INSTALL_DIR}/app/"
else
  say "   ไม่มี rsync — ใช้วิธีสร้างใหม่ทั้งโฟลเดอร์แทน (ผลเหมือนกัน)"
  rm -rf "${INSTALL_DIR}/app.incoming"
  cp -a "${SOURCE}/app" "${INSTALL_DIR}/app.incoming"
  find "${INSTALL_DIR}/app.incoming" -name '__pycache__' -type d -prune -exec rm -rf {} +
  rm -rf "${INSTALL_DIR}/app"
  mv "${INSTALL_DIR}/app.incoming" "${INSTALL_DIR}/app"
fi
chown -R "$OWNER:$OWNER" "${INSTALL_DIR}/app"
# `pyproject.toml` ที่นี่เป็น **บันทึกว่าติดตั้งอะไรไว้** ซึ่งด่าน dependency ใช้เทียบ ·
# ไม่อัปเดตตามแปลว่าด่านจะดังซ้ำตลอดไปแม้ลง dependency ไปแล้ว (`pip install -e` ติดตั้ง
# จากโฟลเดอร์ต้นทาง ไม่ได้ก๊อปไฟล์นี้มาให้)
[ -f "${SOURCE}/pyproject.toml" ] && install -o "$OWNER" -g "$OWNER" -m 644 \
  "${SOURCE}/pyproject.toml" "${INSTALL_DIR}/pyproject.toml"

# ── 4/5 พิสูจน์ว่า import ได้ ก่อนแตะ service ────────────────────────────────
say "4/5 ลอง import app.main ด้วยผู้ใช้จริง"
if ! runuser -u "$OWNER" -- env PYTHONPATH="$INSTALL_DIR" \
       "${INSTALL_DIR}/.venv/bin/python" -c 'import app.main'; then
  say "import ไม่ผ่าน — ของเดิมยังดีอยู่ กู้คืนกลับ"
  restore; mark rolled-back; exit 1
fi

# ── 5/5 รีสตาร์ตแล้วพิสูจน์ว่ากลับมา ────────────────────────────────────────
say "5/5 restart แล้วรอให้ /healthz ตอบ"
systemctl restart "$SERVICE"
PORT="$(env_value GW_PORT)"; PORT="${PORT:-8080}"
# `--retry-connrefused` จำเป็น ไม่ใช่ของประดับ — ทันทีหลัง restart พอร์ตยังไม่เปิด
# และ `--retry` เฉย ๆ ไม่ retry ให้กับ connection refused (เจอจริง 2026-09-21)
if curl -sf --retry 20 --retry-delay 2 --retry-connrefused \
        -o /dev/null "http://127.0.0.1:${PORT}/healthz"; then
  say "เกตเวย์กลับมาแล้ว · รุ่น $(ver "$INSTALL_DIR")"
  prune_backups
  mark ok
else
  say "เกตเวย์ไม่ตอบหลัง restart — กู้คืนของเดิม"
  restore; mark rolled-back; exit 1
fi
