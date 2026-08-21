"""กติกาของขั้นตอนติดตั้ง — ทุกข้อในนี้มาจากของที่ลูกค้าติดจริงตอนประเมินระบบ

ลูกค้ารายหนึ่งเกือบยกเลิกเพราะติดตั้งไม่ผ่าน ทั้งที่ตัวระบบไม่ได้มีอะไรเสีย
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCS = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]


def test_there_is_one_command_that_installs_everything():
    """LMDS มี ./install.sh ส่วนเกตเวย์เคยให้ประกอบเองสี่ขั้นข้ามสอง terminal"""
    script = ROOT / "install.sh"
    assert script.is_file(), "ต้องมี install.sh ที่ราก repo"
    assert os.stat(script).st_mode & stat.S_IXUSR, "install.sh ต้องรันได้ (chmod +x)"


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_no_bsd_only_sed_in_the_docs(doc: Path):
    """`sed -i ''` เป็นรูปของ macOS · บน Linux มันอ่าน '' เป็นสคริปต์แล้วหาไฟล์ไม่เจอ

    ลูกค้าก๊อปบรรทัดนี้จาก README ไปรันบน Ubuntu แล้วได้
    `sed: can't read s#http://dgx03:8000#...#: No such file or directory`
    """
    assert "sed -i ''" not in doc.read_text(encoding="utf-8"), (
        f"{doc.name}: `sed -i ''` รันบน Linux ไม่ได้ — ใช้ install.sh หรือเขียนแบบพกพาได้"
    )


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_example_paths_do_not_look_like_real_files(doc: Path):
    """ตัวอย่างที่ก๊อปแล้วรันได้ทันทีแต่ล้มเหลว แย่กว่าตัวอย่างที่เห็นชัดว่าเป็นตัวอย่าง

    `--cert full.pem` ถูกก๊อปทั้งบรรทัด แล้วได้ `install: cannot stat 'full.pem'`
    กลางการติดตั้ง หลัง apt ลง nginx ไปแล้ว
    """
    text = doc.read_text(encoding="utf-8")
    for match in re.finditer(r"--cert\s+(\S+)", text):
        path = match.group(1)
        assert "/" in path, f"{doc.name}: --cert {path} ดูเหมือนไฟล์ในโฟลเดอร์ปัจจุบัน"


def test_the_installer_refuses_a_cert_path_that_is_not_there():
    """ตรวจไฟล์ก่อนแตะระบบ ไม่ใช่ปล่อยให้ `install` ล้มกลางทาง"""
    script = (ROOT / "scripts" / "install_tls.sh").read_text(encoding="utf-8")
    assert "ซึ่งไม่มีไฟล์นั้นอยู่" in script
    # ต้องตรวจก่อนถึงขั้นลง nginx
    assert script.index("ซึ่งไม่มีไฟล์นั้นอยู่") < script.index("Installing nginx")


def test_tls_covers_localhost_so_a_lan_install_needs_no_domain():
    """ลูกค้าติดตั้งในวงแลนไม่มีโดเมน · คู่มือเดิมยกตัวอย่างเป็น .ac.th อย่างเดียว"""
    script = (ROOT / "scripts" / "install_tls.sh").read_text(encoding="utf-8")
    assert '"localhost" "127.0.0.1"' in script, "ต้องเติม localhost/127.0.0.1 ให้เสมอ"
    assert "ไม่ต้องมีโดเมน" in script, "ต้องบอกผู้ใช้ว่าวงแลนไม่ต้องมีโดเมน"


def test_a_valid_but_incomplete_certificate_is_reissued():
    """"ยังไม่หมดอายุ" ไม่เท่ากับ "ยังครอบชื่อที่เรากำลังจะประกาศ" """
    script = (ROOT / "scripts" / "install_tls.sh").read_text(encoding="utf-8")
    assert "cert_covers_all" in script
    # เทียบแบบ substring ทำให้ DNS:host ไปตรงกับ DNS:host.example.local
    assert '== "$wanted"' in script, "ต้องเทียบชื่อแบบเต็ม ไม่ใช่ substring"


def test_dotfiles_in_the_config_dir_are_ignored():
    """macOS แถม `._ชื่อไฟล์` มาเวลาแตก zip/tar หรือก๊อปผ่าน USB และมันไม่ใช่ UTF-8"""
    from app.registry.store import load_snapshot

    import shutil
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "config"
        shutil.copytree(ROOT / "config", target)
        (target / "models" / "._coding.yaml").write_bytes(b"\x00\xa3not utf-8 at all")
        snapshot = load_snapshot(target)

    assert "coding" in snapshot.models
    assert not [e for e in snapshot.errors if "._coding" in e], snapshot.errors


def test_the_installer_waits_on_a_path_that_exists(client):
    """install.sh เคยรอ /health ซึ่งไม่มีในแอป — รอจนหมดเวลาแล้วบอกว่าติดตั้งล้มเหลว

    ทั้งที่เกตเวย์ขึ้นเรียบร้อยแล้ว · ยิงจริงผ่าน TestClient แทนการอ่านตาราง route
    เพราะ FastAPI ห่อ router ที่ include เข้ามาจนอ่าน .path ตรง ๆ ไม่ได้
    """
    script = (ROOT / "install.sh").read_text(encoding="utf-8")
    # เก็บพาธเต็ม ไม่ใช่แค่ส่วนแรก — ตัดครึ่งแล้วจะได้ "/v" จาก "/v1/chat/completions"
    waited = set(re.findall(r"http://127\.0\.0\.1:\$\{PORT\}(/[A-Za-z0-9/_-]*)", script))
    assert waited, "install.sh ต้องรอ endpoint สักตัวก่อนบอกว่าพร้อม"

    for path in waited:
        assert client.get(path).status_code != 404, f"install.sh รอ {path} แต่แอปตอบ 404"


def test_the_nginx_template_is_rendered_for_the_installed_nginx():
    """`http2 on;` ต้องใช้ nginx 1.25.1 ขึ้นไป · Ubuntu 24.04 มากับ 1.24.0

    ลูกค้ารัน install_tls.sh บน 24.04 แล้วได้ `unknown directive "http2"` —
    nginx ไม่ยอมสตาร์ต ขั้น TLS ล้มทั้งขั้น · เครื่องที่เราทดสอบกันเองมี 1.28
    จึงไม่เคยเห็น
    """
    script = (ROOT / "scripts" / "install_tls.sh").read_text(encoding="utf-8")
    assert "1.25.1" in script, "ต้องเทียบเวอร์ชันก่อนเลือกวิธีเขียน http2"
    assert "listen 443 ssl http2;" in script, "ต้องมีทางถอยไปรูปเก่า"
    assert "sort -V" in script, "เทียบเวอร์ชันด้วยการเรียงแบบ version ไม่ใช่เทียบสตริง"
    # ต้องเลือกก่อนที่ nginx จะถูกเรียกมาตรวจ ไม่งั้นก็ล้มเหมือนเดิม
    assert script.index("1.25.1") < script.index("nginx -t")


def test_the_shipped_nginx_config_still_targets_modern_nginx():
    """ไฟล์ต้นแบบเขียนแบบใหม่ ส่วนเครื่องเก่าให้ตัวติดตั้งแปลงลงให้

    เขียนแบบเก่าไว้ในไฟล์ต้นแบบจะทำให้ nginx ใหม่ขึ้น warning ทุกครั้งที่ reload
    """
    conf = (ROOT / "deploy" / "nginx" / "litegate.conf").read_text(encoding="utf-8")
    assert "http2 on;" in conf
    assert "listen 443 ssl;" in conf


def test_plain_http_reaches_us_even_under_a_name_we_never_detected():
    """nginx ที่ Ubuntu แจกมา จอง default_server บน :80 ไว้แล้ว

    ผลคือ request ที่ Host ไม่ตรง server_name ของเราสักชื่อ — ลูกค้าตั้งชื่อใน
    DNS เอง เพิ่มการ์ดแลนใบที่สอง หรือ DHCP เปลี่ยน IP หลังเราตรวจชื่อไปแล้ว —
    ไปโผล่ /var/www/html แทนที่จะเด้งขึ้น https · ฝั่ง :443 ไม่เป็นเพราะบล็อก
    ของเราเป็นตัวเดียวที่ฟัง TLS อยู่ อาการเลยโผล่เฉพาะทาง http ซึ่งเป็นทางที่
    คนพิมพ์เข้ามาเองพอดี
    """
    script = (ROOT / "scripts" / "install_tls.sh").read_text(encoding="utf-8")
    assert "listen 80 default_server;" in script
    # ยึดได้ต่อเมื่อไม่มีใครจองไว้ก่อน — จองซ้ำแล้ว nginx ไม่ยอมสตาร์ตเลย
    assert "default_server" in script.split("sites-enabled/*")[0]
    assert "var/www/html" in script, "ต้องเช็คว่ายังเป็นไซต์ default ที่แจกมา ก่อนจะไปปิดของใคร"


def test_a_rejected_config_leaves_nginx_the_way_it_was_found():
    """ถ้า nginx -t ไม่ผ่าน ต้องคืนไซต์ default ก่อนตาย

    ไม่งั้นเครื่องจะเหลือ nginx ที่ทั้งไม่มีไซต์ default และไม่มีไซต์ของเรา
    ซึ่งแย่กว่าตอนก่อนรันคำสั่ง
    """
    script = (ROOT / "scripts" / "install_tls.sh").read_text(encoding="utf-8")
    body = script.split("nginx -t")[1].split("systemctl reload")[0]
    assert "default_restore" in body
    assert "ln -sf ../sites-available/default" in body


def test_the_console_is_reachable_from_the_machine_it_runs_on():
    """`/admin/` เป็นทางที่คอนโซลใช้เอง — ที่ไหนไม่อยู่ในลิสต์ คือคอนโซลใช้ไม่ได้

    ของเดิมอนุญาตแค่ 10/8 กับ 192.168/16 · แปลว่าคนที่นั่งอยู่หน้าเครื่องเกตเวย์
    เอง หรือ curl จาก ssh บนเครื่องนั้น โดน 403 ทั้งที่ทั้งวงแลนรอบตัวเข้าได้
    และเครื่องที่ resolve ไปเจอ AAAA จะเข้ามาทาง IPv6 ซึ่งกฎเดิมเป็น v4 ล้วน
    — เบราว์เซอร์ตัวเดียวกันจึงใช้ได้หรือไม่ได้ ขึ้นกับว่ามันหยิบ record ไหน

    อาการที่เห็นคือ login ผ่าน 200 แล้วทุกแผงพัง ซึ่งดูเหมือนของเสีย ไม่เหมือน
    กฎไฟร์วอลล์
    """
    conf = (ROOT / "deploy" / "nginx" / "litegate.conf").read_text(encoding="utf-8")
    for guarded in ("location /admin/ {", "location /metrics {"):
        block = conf.split(guarded, 1)[1].split("}", 1)[0]
        for cidr in ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12",
                     "192.168.0.0/16", "::1/128", "fc00::/7", "fe80::/10"):
            assert f"allow {cidr};" in block, f"{guarded} ขาด {cidr}"
        assert "deny all;" in block, f"{guarded} ต้องปิดที่เหลือ"
