"""Is this gateway still the current release? — asked only when somebody asks.

ของจริงที่เกิดขึ้น: เครื่อง production รัน 1.10.0 อยู่หลายสัปดาห์ทั้งที่ออก 1.12.1 ไปแล้ว
และไม่มีอะไรบนจอบอกเลยว่ามันตามหลังอยู่สองรุ่น — คนที่ดูแลเครื่องนั้นไม่มีทางรู้จนกว่า
จะไปเปิดหน้า releases เอง · ลูกค้าติดตั้งแบบเครื่องใครเครื่องมัน ไม่มีใครไล่ดูให้

โมดูลนี้จึงตอบคำถามเดียว: **เลขที่รันอยู่ยังเป็นเลขล่าสุดไหม** และตอบเมื่อมีคนถาม

สามข้อบังคับที่กำหนดรูปร่างของทุกอย่างในไฟล์นี้:

* **ไม่ยิงเองเด็ดขาด** ไม่ตอนสตาร์ต ไม่ตอนโหลดหน้า ไม่มี background task ·
  ลูกค้าที่รัน air-gapped มีจริง และเป็นเหตุผลที่คอนโซลนี้ไม่โหลดแม้แต่ฟอนต์จากข้างนอก ·
  ทางเดียวที่โค้ดในนี้จะทำงานคือมีคนกดปุ่มใน ``/admin`` — ไม่มี caller อื่นในทั้ง repo
* **ส่งออกไปเปล่า ๆ** คำขอที่ออกไปคือ GET ไปที่ GitHub API หนึ่งครั้ง (สองครั้ง
  เฉพาะตอนที่ repo ยังไม่มี release เผยแพร่ แล้วต้องถามหาแท็กแทน) ไม่มี query
  ไม่มี body ไม่มี header ที่บอกว่าเครื่องนี้คือใครหรือรันเวอร์ชันอะไร · การเทียบเลข
  ทำที่ฝั่งนี้หลังได้คำตอบมาแล้ว GitHub จึงรู้แค่ว่า "มีคนถามหา release ล่าสุดของ repo
  สาธารณะอันหนึ่ง" ซึ่งเป็นสิ่งเดียวกับที่ใครเปิดหน้าเว็บก็รู้ · ที่นี่ไม่มี telemetry
  และบรรทัดพวกนี้มีไว้เพื่อให้มันยังไม่มีต่อไป
* **ต่อไม่ได้ ไม่ใช่ความผิดพลาด** เครื่องที่ไม่มีทางออกเน็ตคือการตั้งค่าที่ตั้งใจ
  ไม่ใช่อาการเสีย · ทุกทางที่ล้มเหลวคืนค่าปกติพร้อม ``reason`` ที่อ่านรู้เรื่อง
  ไม่มีทางไหน raise ออกไปให้คอนโซลขึ้นแดง

และสิ่งที่โมดูลนี้ **ไม่ทำ** คืออัปเดตให้ · ดู ``how_to_update()`` ท้ายไฟล์
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

# repo สาธารณะที่ปล่อย release ของสินค้านี้ · ตรงกับ [project.urls] ใน pyproject.toml
REPO = "neronain/AiGatewayLocal"
LATEST_RELEASE_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
TAGS_URL = f"https://api.github.com/repos/{REPO}/tags"
RELEASES_URL = f"https://github.com/{REPO}/releases"
TAG_URL = f"https://github.com/{REPO}/releases/tag/"

# สั้นโดยตั้งใจ · คนกดปุ่มนี้แล้วยืนรออยู่หน้าจอ และคำตอบว่า "ตรวจไม่ได้" ที่มาใน
# หกวินาที มีค่ามากกว่าคำตอบที่ถูกต้องที่มาในสามสิบ · เครื่องที่ไม่มี default route
# มักไม่ตอบอะไรเลยจนกว่าจะ timeout ดังนั้นตัวเลข connect คือตัวที่กำหนดว่า
# air-gapped จะเห็นคำตอบเร็วแค่ไหน
_TIMEOUT = httpx.Timeout(connect=3.0, read=5.0, write=3.0, pool=3.0)

# header ชุดเดียวที่ใส่เอง — บอกแค่ว่าอยากได้ JSON รูปแบบของ GitHub API
# ไม่มีที่ว่างสำหรับ User-Agent ที่ระบุตัวสินค้า/เวอร์ชัน: httpx ใส่ของมันเอง
# (`python-httpx/x.y`) ซึ่งไม่ได้บอกอะไรเกี่ยวกับเครื่องนี้ และนั่นคือที่ที่เราอยากอยู่
_HEADERS = {"accept": "application/vnd.github+json"}

# `v1.12.1`, `1.12.1`, `1.12` — ยึดหัวสตริงไว้ ไม่ search กลางทาง เพราะแท็กชื่อแปลก ๆ
# อย่าง `release-2026` ไม่ควรถูกอ่านเป็นเวอร์ชัน 2026 แล้วประกาศว่าตามหลังอยู่พันรุ่น
_TAG = re.compile(r"^v?(\d+(?:\.\d+){0,3})")

# รากของ repo/ที่ติดตั้ง — app/core/release.py จึงขึ้นสองชั้น
REPO_ROOT = Path(__file__).resolve().parents[2]


def parse_version(text: str) -> tuple[int, ...] | None:
    """`"v1.12.1"` → `(1, 12, 1)` · คืน None เมื่ออ่านไม่ออก แทนที่จะเดา"""
    match = _TAG.match((text or "").strip())
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def compare(current: str, latest: str) -> str:
    """behind / current / ahead / unknown

    `ahead` ไม่ใช่เรื่องแปลก: เครื่องที่รันจาก main ระหว่างรอ release ถัดไปจะเห็นค่านี้
    และมันต้องไม่ถูกแสดงว่า "ล้าสมัย" · `unknown` คือตอนที่แท็กฝั่งโน้นไม่ได้อยู่ในรูป
    ตัวเลข — บอกตามตรงดีกว่าเทียบมั่ว
    """
    here, there = parse_version(current), parse_version(latest)
    if here is None or there is None:
        return "unknown"
    width = max(len(here), len(there))
    here += (0,) * (width - len(here))
    there += (0,) * (width - len(there))
    if here < there:
        return "behind"
    if here > there:
        return "ahead"
    return "current"


async def check(current: str) -> dict:
    """ถาม GitHub ว่า release ล่าสุดคือเลขอะไร แล้วเทียบกับเลขที่รันอยู่

    เรียกจากที่เดียวเท่านั้น: endpoint ที่ผูกกับปุ่มในคอนโซล · อย่าเรียกจาก startup,
    lifespan, health check หรือ task เบื้องหลัง — นั่นคือการเปลี่ยนสินค้านี้ให้เป็น
    ของที่โทรกลับบ้านเอง ซึ่งเป็นคนละอย่างกับที่ลูกค้าตกลงเอาไปติดตั้ง
    """
    checked_at = datetime.now(timezone.utc).isoformat()
    tags = None
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.get(LATEST_RELEASE_URL, headers=_HEADERS)
            if response.status_code == 404:
                # repo นี้ตอนนี้มีแท็กครบทุกรุ่นแต่ยังไม่เคยกด Publish release สักครั้ง
                # `/releases/latest` จึงตอบ 404 เสมอ · ถ้าไม่ถามต่อที่ `/tags` ปุ่มนี้
                # จะตอบ "ยังไม่มี release" ไปตลอดกาลและไม่มีวันเตือนใครได้เลย
                # คำขอที่สองเกิดเฉพาะสาขานี้ และยังเป็น GET เปล่า ๆ ชุด header เดิม
                tags = await _try_get(client, TAGS_URL)
    except httpx.TimeoutException:
        return _cannot(
            current, checked_at,
            "GitHub did not answer in time. A gateway with no route to the internet "
            "always looks like this, and nothing is wrong with it.",
        )
    except httpx.HTTPError as exc:
        # ข้อความของ httpx บอกได้ตรงกว่าคำสรุปของเราเอง (DNS, TLS, proxy ปฏิเสธ)
        return _cannot(current, checked_at, f"Could not reach GitHub: {exc}")

    if response.status_code == 404:
        newest = _newest_tag(tags)
        if newest:
            return _answer(current, checked_at, newest, "", TAG_URL + newest)
        return _cannot(
            current, checked_at,
            f"GitHub has no published release or usable tag for {REPO} yet. "
            "Compare by hand until one is published.",
        )
    if response.status_code in (403, 429):
        # GitHub จำกัดคำขอที่ไม่ได้ยืนยันตัวตนไว้ต่อ IP · หลายคนกดพร้อมกันจากออฟฟิศ
        # เดียวกันชนเพดานได้ และมันไม่ใช่อาการของเกตเวย์เสีย จึงต้องพูดให้ตรง
        if response.headers.get("x-ratelimit-remaining") == "0":
            return _cannot(
                current, checked_at,
                "GitHub is rate-limiting this address. It allows a limited number of "
                "anonymous requests per hour; try again later.",
            )
        return _cannot(
            current, checked_at,
            f"GitHub refused the request (HTTP {response.status_code}).",
        )
    if response.status_code >= 400:
        return _cannot(current, checked_at, f"GitHub answered HTTP {response.status_code}.")

    body = _body(response)
    tag = str(body.get("tag_name") or body.get("name") or "").strip()
    if not tag:
        return _cannot(current, checked_at, "GitHub's answer did not name a release.")

    return _answer(
        current, checked_at, tag,
        str(body.get("published_at") or ""),
        str(body.get("html_url") or RELEASES_URL),
    )


def install_shape(root: Path | None = None) -> str:
    """ที่ติดตั้งนี้อัปเดตด้วยวิธีไหน — ตอบจากสิ่งที่อยู่บนดิสก์ ไม่ต้องต่อเน็ต

    สามแบบที่มีจริง และวิธีอัปเดตของแต่ละแบบคนละเรื่องกันโดยสิ้นเชิง (ดู
    docs/DEPLOYMENT.md §Routine upgrade) · คอนเทนเนอร์มาก่อนเสมอ: image ที่ build
    จาก checkout ก็ยังมี .git ติดมา แต่ `git pull` ข้างในนั้นแก้อะไรไม่ได้เลย
    เพราะสิ่งที่ต้องเปลี่ยนคือ image ที่อยู่บน host
    """
    if Path("/.dockerenv").exists() or Path("/run/.containerenv").exists():
        return "container"
    base = root if root is not None else REPO_ROOT
    if (base / ".git").exists():
        return "checkout"
    return "copied"


def how_to_update(shape: str | None = None) -> dict:
    """คำสั่งที่ต้องรัน — เป็นข้อความให้อ่านและก๊อป ไม่ใช่ปุ่มที่รันให้

    **ทำไมไม่มีปุ่ม "อัปเดตให้เลย"** · LMDS ทำได้เพราะมันติดตั้งจาก git checkout
    ทางเดียว · ที่นี่มีสามทางที่ไม่เหมือนกันเลย และทางที่ใช้จริงบนเครื่อง production
    คือแบบก๊อปไฟล์ทับ ซึ่งขั้นตอนที่ปลอดภัยของมันมีห้าขั้น (สำรอง → compileall →
    ติดตั้ง → ลอง import → restart แล้วพิสูจน์ว่ากลับมา) และทุกขั้นมีอยู่เพราะเคย
    ข้ามแล้วเครื่องล่ม · ปุ่มที่ทำสำเร็จบ้างไม่สำเร็จบ้างบนเครื่องลูกค้า แย่กว่าไม่มีปุ่ม
    """
    shape = shape or install_shape()
    if shape == "container":
        return {
            "shape": shape,
            "summary": "This gateway runs in a container. The image on the host is what "
                       "changes; nothing inside the container does.",
            # รันบน host ไม่ใช่ใน container นี้ — เขียนไว้ในบรรทัดแรกเพราะคนก๊อปทั้งก้อน
            "commands": [
                "# run these on the host, not inside this container",
                "cd AiGatewayLocal && git pull",
                "docker compose -f docker/docker-compose.yml up -d --build",
            ],
            "doc": "docs/DEPLOYMENT.md § Routine upgrade",
        }
    if shape == "checkout":
        return {
            "shape": shape,
            "summary": "This install is a git checkout, so it updates in place.",
            "commands": [
                f"cd {REPO_ROOT} && git pull",
                "sudo ./scripts/bootstrap.sh    # keeps the existing .env",
                "sudo systemctl restart litegate",
            ],
            "doc": "docs/DEPLOYMENT.md § Routine upgrade",
        }
    return {
        "shape": shape,
        "summary": "This install has no checkout — the files were copied here. Copying the "
                   "new ones over is fine; doing it without a backup and a syntax check "
                   "first is what takes a gateway down.",
        "commands": [
            "BACKUP=/opt/gw-backup-$(date +%Y%m%d-%H%M%S)",
            f"sudo mkdir -p $BACKUP && sudo cp -a {REPO_ROOT}/app $BACKUP/",
            f"sudo {REPO_ROOT}/.venv/bin/python -m compileall -q <staged files>",
            "sudo install -o litegate -g litegate -m 644 <file> "
            f"{REPO_ROOT}/<file>",
            f"sudo -u litegate {REPO_ROOT}/.venv/bin/python -c 'import app.main'",
            "sudo systemctl restart litegate",
            "curl -sf --retry 10 --retry-delay 2 http://127.0.0.1:8080/healthz",
        ],
        "doc": "docs/DEPLOYMENT.md § When the host has no checkout",
    }


async def _try_get(client: httpx.AsyncClient, url: str) -> httpx.Response | None:
    """คำขอเสริมที่ล้มได้โดยไม่ทำให้คำตอบหลักหาย

    ใช้กับ `/tags` เท่านั้น · ถึงตรงนี้เรารู้คำตอบของคำขอหลักแล้ว การที่คำขอที่สอง
    ล้มจึงไม่ควรกลบมันด้วยข้อความ "ต่อเน็ตไม่ได้" ที่ไม่จริง
    """
    try:
        return await client.get(url, headers=_HEADERS)
    except httpx.HTTPError as exc:
        log.debug("tag lookup failed: %s", exc)
        return None


def _newest_tag(response: httpx.Response | None) -> str:
    """แท็กเลขสูงสุดจากคำตอบของ `/tags` — `""` เมื่อไม่มีตัวไหนอ่านเป็นเวอร์ชันได้

    GitHub ไม่รับประกันว่าลำดับที่ส่งมาเรียงตามเวอร์ชัน (มันเรียงตามที่ ref ถูกสร้าง)
    ตัวแรกในรายการจึงไม่ใช่ตัวล่าสุดเสมอไป — ต้องเทียบเองทุกตัว · แท็กที่อ่านไม่ออก
    ถูกข้าม ไม่ใช่ทำให้ทั้งรายการใช้ไม่ได้
    """
    if response is None or response.status_code != 200:
        return ""
    try:
        items = response.json()
    except ValueError:
        return ""
    if not isinstance(items, list):
        return ""
    best, best_key = "", None
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        parsed = parse_version(name)
        if parsed is None:
            continue
        # เติมศูนย์ให้ยาวเท่ากันก่อนเทียบ ไม่งั้น (1, 12) > (1, 12, 1) ไม่เป็นจริง
        key = parsed + (0,) * (4 - len(parsed))
        if best_key is None or key > best_key:
            best, best_key = name, key
    return best


def _answer(current: str, checked_at: str, tag: str, published_at: str,
            release_url: str) -> dict:
    """คำตอบที่ตรวจได้ · รูปเดียวกันไม่ว่าเลขจะมาจาก release หรือจากแท็ก"""
    status = compare(current, tag)
    if status == "behind":
        log.info("update available: running %s, latest is %s", current, tag)
    return {
        "ok": True,
        "checked_at": checked_at,
        "current": current,
        "latest": tag,
        "status": status,
        "published_at": published_at,
        "release_url": release_url or RELEASES_URL,
        "releases_url": RELEASES_URL,
    }


def _cannot(current: str, checked_at: str, reason: str) -> dict:
    """คำตอบว่า "ตรวจไม่ได้" · รูปเดียวกับคำตอบที่สำเร็จ คอนโซลจะได้ไม่ต้องเดา"""
    return {
        "ok": False,
        "checked_at": checked_at,
        "current": current,
        "status": "unknown",
        "reason": reason,
        "releases_url": RELEASES_URL,
    }


def _body(response: httpx.Response) -> dict:
    try:
        parsed = response.json()
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
