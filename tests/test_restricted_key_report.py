"""รายงานก่อนอัปเกรด: key ใบไหนเสียสิทธิ์ผู้ดูแล — ต้องตอบตรงกับด่านจริงทุกใบ

`scripts/restricted_key_report.py` มีไว้ให้ผู้ดูแลรันกับฐานข้อมูลของเครื่องจริง *ก่อน*
เปิดรุ่นที่ key ซึ่งถูกจำกัดไว้เลิกพกสิทธิ์ admin/manager · รายงานที่ตอบไม่ตรงกับเกตเวย์
แย่กว่าไม่มีรายงาน: มันบอกว่า "ไม่มีใบไหนกระทบ" แล้วสคริปต์สำรองข้อมูลตอนตีสองได้ 403

เทสในไฟล์นี้จึงไม่ตรวจตรรกะของสคริปต์เทียบกับตรรกะที่เขียนซ้ำในเทส — มันสร้าง key จริง
ผ่าน admin API รันสคริปต์จริงเป็น process แยกกับไฟล์ฐานข้อมูลเดียวกัน แล้วเอา key ทุกใบ
ไปยิง /admin จริง ๆ เทียบกัน
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "restricted_key_report.py"


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture(autouse=True)
def _writable(writable_config):
    return writable_config


def admin(client, method, path, **kw):
    return client.request(method, path, headers=auth(client.admin_key), **kw)


def person(client, external_id, role):
    return admin(client, "POST", "/admin/users",
                 json={"external_id": external_id, "role": role}).json()


def key_for(client, who, name, **extra) -> dict:
    made = admin(client, "POST", "/admin/api-keys",
                 json={"user_id": who["id"], "name": name, **extra})
    assert made.status_code == 201, made.text
    return made.json()


def cap(client, key) -> str:
    made = admin(client, "POST", "/admin/quota-policies",
                 json={"scope": "key", "api_key_id": key["id"], "name": "cap",
                       "window": "day", "max_requests": 100})
    assert made.status_code == 201, made.text
    return made.json()["id"]


def run(db_path, *flags) -> subprocess.CompletedProcess:
    """สคริปต์จริง เป็น process แยก — แบบเดียวกับที่ผู้ดูแลจะรันบนเครื่องจริง"""
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--db", str(db_path), *flags],
        capture_output=True, text=True, cwd=REPO, timeout=120,
    )


def report(db_path) -> dict:
    done = run(db_path, "--json")
    assert done.returncode in (0, 1), done.stderr
    return json.loads(done.stdout)


def refused_as_a_limited_key(client, key: str) -> bool:
    client.cookies.clear()
    response = client.get("/admin/users", headers=auth(key))
    if response.status_code != 403:
        return False
    details = response.json()["error"].get("details") or {}
    return details.get("reason_code") == "restricted_key"


@pytest.fixture
def fleet(client, temp_db):
    """key ทุกแบบที่เครื่องจริงมีได้ · คืน {ชื่อ: key} กับ path ของไฟล์ฐานข้อมูล"""
    root, boss, student = (person(client, "root", "admin"),
                           person(client, "lecturer", "manager"),
                           person(client, "s1", "member"))
    cs101 = admin(client, "POST", "/admin/workspaces",
                  json={"code": "CS101", "name": "CS101"}).json()
    admin(client, "POST", f"/admin/workspaces/{cs101['id']}/models",
          json={"models": ["coding", "gemma-vision"]})
    for who in (boss, student):
        admin(client, "POST", f"/admin/workspaces/{cs101['id']}/join",
              json={"user_id": who["id"]})
    bundle = admin(client, "POST", "/admin/access-groups",
                   json={"name": "coding-set", "models": ["coding"]}).json()

    keys = {
        "admin plain": key_for(client, root, "admin plain"),
        "admin scoped": key_for(client, root, "admin scoped", scopes=["admin"]),
        "admin models": key_for(client, root, "admin models", models=["coding"]),
        "admin bundle": key_for(client, root, "admin bundle", access_groups=[bundle["id"]]),
        "admin bound": key_for(client, root, "admin bound", workspace_id=cs101["id"]),
        "admin capped": key_for(client, root, "admin capped"),
        "admin cap off": key_for(client, root, "admin cap off"),
        "admin revoked": key_for(client, root, "admin revoked", models=["coding"]),
        "manager plain": key_for(client, boss, "manager plain"),
        "manager models": key_for(client, boss, "manager models", models=["coding"]),
        "manager bound": key_for(client, boss, "manager bound", workspace_id=cs101["id"]),
        "member plain": key_for(client, student, "member plain"),
        "member models": key_for(client, student, "member models", models=["coding"]),
    }
    cap(client, keys["admin capped"])
    off = cap(client, keys["admin cap off"])

    async def switch_off():
        from app.db.models import QuotaPolicy
        from app.db.session import session_scope

        async with session_scope() as session:
            (await session.get(QuotaPolicy, off)).enabled = False

    client.portal.call(switch_off)
    assert admin(client, "DELETE",
                 f"/admin/api-keys/{keys['admin revoked']['id']}").status_code == 200
    return keys, temp_db


# สคริปต์รายงานอ่านฐานแบบ sync · เทสพวกนี้ป้อนฐานของ client ให้มันตรง ๆ — บน PostgreSQL venv ของเรา
# ไม่มีไดรเวอร์ sync (เกตเวย์ใช้ asyncpg) สคริปต์จึงปฏิเสธด้วยข้อความบอกวิธีแก้ ซึ่งเป็นพฤติกรรมที่ตั้งใจ
# ระบบจริงเป็น SQLite · เจอตอนรันชุดเต็มบน PostgreSQL หลังรวมสาย 2026-10-09 (9 ข้อล้ม) —
# รายงานบน PostgreSQL ยังไม่มีเทสครอบ
on_sqlite = pytest.mark.sqlite_only


@on_sqlite
def test_the_report_and_the_gate_agree_about_every_key(client, fleet):
    """ใบไหนที่รายงานบอกว่าเสียสิทธิ์ ต้องได้ 403 จริง · ใบที่ไม่บอก ต้องไม่ได้ — ทุกใบ"""
    keys, db_path = fleet

    said = {row["id"] for row in report(db_path)["loses_admin_access"]}

    live = {name: key for name, key in keys.items() if name != "admin revoked"}
    really = {key["id"] for key in live.values()
              if refused_as_a_limited_key(client, key["api_key"])}
    assert said == really
    # และไม่ใช่ตรงกันเพราะว่างทั้งคู่
    assert said == {keys[name]["id"] for name in (
        "admin models", "admin bundle", "admin bound", "admin capped",
        "manager models", "manager bound",
    )}


@on_sqlite
def test_a_revoked_key_is_not_in_the_report(client, fleet):
    """เพิกถอนไปแล้วไม่ใช่ใบที่จะพัง — นับรวมทำให้ผู้ดูแลไล่หาสคริปต์ที่ไม่มีอยู่"""
    keys, db_path = fleet
    out = report(db_path)

    everything = out["loses_admin_access"] + out["bundles_grant_nothing"] + out["unchanged"]
    assert keys["admin revoked"]["id"] not in {row["id"] for row in everything}
    assert out["live_keys"] == len(everything)


@on_sqlite
def test_each_row_says_which_key_whose_role_and_why(client, fleet):
    keys, db_path = fleet

    rows = {row["name"]: row for row in report(db_path)["loses_admin_access"]}

    bound = rows["manager bound"]
    assert bound["id"] == keys["manager bound"]["id"]
    assert bound["key_prefix"] == keys["manager bound"]["key_prefix"]
    assert bound["owner_role"] == "manager"
    assert bound["limited_by"] == ["workspace"]
    assert rows["admin capped"]["limited_by"] == ["cap"]
    assert rows["admin bundle"]["limited_by"] == ["access_groups"]
    assert rows["admin models"]["owner_role"] == "admin"


@on_sqlite
def test_the_key_and_its_hash_never_leave_the_database(client, fleet):
    """รายงานนี้จะถูกแปะลงแชตและ ticket — ต้องไม่มีอะไรที่ใช้เป็น credential ได้"""
    keys, db_path = fleet
    with sqlite3.connect(db_path) as raw:
        hashes = [h for (h,) in raw.execute("SELECT key_hash FROM api_keys")]
    assert len(hashes) >= len(keys)

    for flags in ((), ("--json",)):
        done = run(db_path, *flags)
        printed = done.stdout + done.stderr
        # รายงานที่ล้มก็ "ไม่พิมพ์ความลับ" เหมือนกัน — ต้องเป็นรายงานที่พูดถึงใบพวกนี้จริง
        assert done.returncode == 1 and keys["admin models"]["key_prefix"] in printed
        for key in keys.values():
            assert key["api_key"] not in printed
            assert key["api_key"][12:] not in printed, "ส่วนลับของ key หลุดออกมา"
        for digest in hashes:
            assert digest not in printed


@on_sqlite
def test_the_text_a_person_reads_names_the_keys_and_what_to_do(client, fleet):
    keys, db_path = fleet

    done = run(db_path)

    assert done.returncode == 1, "มีใบที่เปลี่ยน — exit status ต้องบอก"
    text = done.stdout
    assert "เสียสิทธิ์ผู้ดูแล 6 ใบ" in text
    for name in ("admin models", "manager bound"):
        assert keys[name]["key_prefix"] in text and keys[name]["id"] in text
    assert "เจ้าของเป็น manager" in text and "เจ้าของเป็น admin" in text
    assert "ผูก workspace" in text and "เพดานโควตาเฉพาะใบ" in text
    assert "ออกใบใหม่" in text, "ต้องบอกว่าทำอะไรต่อ"
    # ใบที่ยังเป็นใบผู้ดูแลเหมือนเดิมถูกเรียกชื่อด้วย — ไม่งั้นคนอ่านไม่รู้ว่าใบไหนใช้แทนได้
    assert keys["admin plain"]["key_prefix"] in text.split("── ไม่เปลี่ยน")[1]


@on_sqlite
def test_nothing_to_report_is_exit_status_zero(client, temp_db):
    who = person(client, "s1", "member")
    key_for(client, who, "narrow", models=["coding"])

    done = run(temp_db)

    assert done.returncode == 0, done.stdout + done.stderr
    assert "ไม่มีใบไหนเสียสิทธิ์ผู้ดูแล" in done.stdout


@on_sqlite
def test_a_key_limited_to_a_dead_bundle_is_listed_and_really_calls_nothing(client, temp_db):
    """การเปลี่ยนอีกข้อของรุ่นเดียวกัน: ใบแบบนี้เคยเรียกได้ทุกโมเดล"""
    bundle = admin(client, "POST", "/admin/access-groups",
                   json={"name": "coding-set", "models": ["coding"]}).json()
    student = person(client, "s1", "member")
    dead = key_for(client, student, "dead bundle", access_groups=[bundle["id"]])
    fine = key_for(client, student, "list too", models=["coding"],
                   access_groups=[bundle["id"]])
    admin(client, "PATCH", f"/admin/access-groups/{bundle['id']}", json={"enabled": False})

    out = report(temp_db)

    assert [row["id"] for row in out["bundles_grant_nothing"]] == [dead["id"]]
    assert out["loses_admin_access"] == []
    seen = client.get("/v1/models", headers=auth(dead["api_key"])).json()["data"]
    assert seen == []
    assert client.get("/v1/models", headers=auth(fine["api_key"])).json()["data"] != []


# ── อ่านอย่างเดียว ─────────────────────────────────────────────────────────────

def _digest(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@on_sqlite
def test_running_it_does_not_change_the_database_file(client, fleet):
    _keys, db_path = fleet
    before = _digest(db_path)

    assert run(db_path).returncode == 1
    assert run(db_path, "--json").returncode == 1

    assert _digest(db_path) == before


@on_sqlite
def test_the_connection_it_opens_cannot_write(client, fleet):
    """ไม่ใช่ "สคริปต์ไม่ได้สั่งเขียน" แต่ "สั่งแล้วไดรเวอร์ปฏิเสธ" — ของที่ตรวจได้"""
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import OperationalError

    _keys, db_path = fleet
    module = _load()
    engine = create_engine(module.read_only_url(str(db_path)))
    try:
        with engine.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM api_keys")).scalar() > 0
            with pytest.raises(OperationalError, match="readonly"):
                connection.execute(text("UPDATE api_keys SET name = 'x'"))
    finally:
        engine.dispose()


def _load():
    import importlib.util

    spec = importlib.util.spec_from_file_location("restricted_key_report", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("form", ["path", "url", "async url", "relative url"])
def test_it_takes_the_database_the_way_an_operator_has_it(tmp_path, monkeypatch, form):
    """path ของไฟล์ · URL · หรือค่า GW_DATABASE_URL จาก .env ตรง ๆ (async + สัมพัทธ์)"""
    (tmp_path / "data").mkdir()
    db = tmp_path / "data" / "gateway.db"
    sqlite3.connect(db).close()
    monkeypatch.chdir(tmp_path)
    given = {
        "path": str(db),
        "url": f"sqlite:///{db}",
        "async url": f"sqlite+aiosqlite:///{db}",
        "relative url": "sqlite+aiosqlite:///./data/gateway.db",
    }[form]

    assert _load().read_only_url(given) == f"sqlite:///file:{db.resolve()}?mode=ro&uri=true"


# ── exit status: 1 แปลว่า "มีใบที่เปลี่ยน" เท่านั้น ─────────────────────────────
#
# ทีมเอกสารรันสคริปต์จริง 2026-10-09: ไฟล์ฐานข้อมูลที่ไม่มีอยู่ และ URL ของ PostgreSQL บน
# เครื่องที่ไม่มีไดรเวอร์ sync ต่างก็จบด้วย status 1 — เลขเดียวกับ "มี key ที่เปลี่ยน" ·
# สคริปต์ deploy ที่แตกกิ่งตาม status จะอ่าน "เปิดฐานไม่ได้" เป็น "มีใบกระทบ ไปดูรายงาน"
# หรือแย่กว่านั้น ตัวครอบที่ถือว่า 1 คือ "ต้องทบทวน ตามปกติ" จะเดินต่อโดยไม่เคยอ่านฐานเลย
# (`sys.exit("ข้อความ")` ของ Python จบด้วย 1 เสมอ) · ทุกกรณีที่รันไม่ได้ต้องเป็น 2
# พร้อมเหตุผลบน stderr และ stdout ต้องว่าง — ไม่มีรายงานครึ่งใบให้ใครเอาไปอ่าน

def _could_not_run(done: subprocess.CompletedProcess) -> str:
    assert done.returncode == 2, f"status {done.returncode}\n{done.stdout}\n{done.stderr}"
    assert done.stdout.strip() == "", "รันไม่ได้แล้วต้องไม่มีรายงานออกมา"
    assert done.stderr.strip(), "ต้องบอกเหตุผล"
    assert "Traceback" not in done.stderr, "เหตุผลสำหรับคนอ่าน ไม่ใช่ stack ของ Python"
    return done.stderr


def test_a_database_that_is_not_there_is_said_plainly(tmp_path):
    for flags in ((), ("--json",)):
        reason = _could_not_run(run(tmp_path / "nope.db", *flags))
        assert "ไม่พบไฟล์ฐานข้อมูล" in reason
    assert not (tmp_path / "nope.db").exists(), "รายงานต้องไม่สร้างไฟล์ฐานข้อมูลเปล่าขึ้นมา"


def test_a_file_that_is_not_a_database_is_exit_status_two(tmp_path):
    garbage = tmp_path / "gateway.db"
    garbage.write_bytes(b"this is a log file somebody pointed --db at\n" * 200)

    reason = _could_not_run(run(garbage, "--json"))

    assert "not a database" in reason
    assert garbage.read_bytes().startswith(b"this is a log file")


def test_a_sqlite_file_that_is_not_a_gateways_is_exit_status_two(tmp_path):
    """ไฟล์ SQLite ที่ถูกต้องแต่ไม่มีตารางของเกตเวย์ — "0 ใบ ไม่มีอะไรเปลี่ยน" คือคำตอบที่ผิด"""
    other = tmp_path / "other.db"
    with sqlite3.connect(other) as raw:
        raw.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")

    reason = _could_not_run(run(other))

    assert "api_keys" in reason


@pytest.mark.skipif(os.geteuid() == 0, reason="root อ่านไฟล์ได้ทุกไฟล์ ไม่ว่าจะตั้งสิทธิ์ไว้อย่างไร")
def test_a_database_it_is_not_allowed_to_read_is_exit_status_two(client, fleet):
    _keys, db_path = fleet
    copy = Path(db_path).parent / "locked.db"
    copy.write_bytes(Path(db_path).read_bytes())
    copy.chmod(0)
    try:
        _could_not_run(run(copy))
    finally:
        copy.chmod(0o600)


def test_a_postgres_url_that_cannot_be_reached_is_exit_status_two():
    """ไม่ต้องมีเซิร์ฟเวอร์: URL ชี้ไปพอร์ตที่ไม่มีใครฟัง · มีไดรเวอร์ sync ก็ต่อไม่ติด ไม่มีก็
    หาไดรเวอร์ไม่เจอ — ทางไหนก็คือ "รันไม่ได้" และรหัสผ่านใน URL ต้องไม่ถูกพิมพ์ออกมา"""
    done = run("postgresql+asyncpg://litegate:not-a-real-password@127.0.0.1:1/litegate")

    reason = _could_not_run(done)
    assert "not-a-real-password" not in reason


def test_a_postgres_url_without_a_sync_driver_is_exit_status_two():
    """เกตเวย์ใช้ asyncpg ซึ่งรายงานนี้ใช้ไม่ได้ — เครื่องที่ไม่ได้ลง psycopg คือกรณีปกติ

    สคริปต์จริง รันเป็น process แยก โดยทำให้ `find_spec` มองไม่เห็นไดรเวอร์ sync สองตัว
    (ค่า None ใน sys.modules คือวิธีมาตรฐานที่ Python ใช้บอกว่า "โมดูลนี้ไม่มี") — เทสจึง
    ตรวจทางนี้ได้ทั้งบนเครื่องที่ลง psycopg ไว้และเครื่องที่ไม่ได้ลง
    """
    wrapper = (
        "import runpy, sys\n"
        "sys.modules['psycopg'] = sys.modules['psycopg2'] = None\n"
        f"sys.argv = [{str(SCRIPT)!r}, '--db', "
        "'postgresql+asyncpg://litegate@127.0.0.1:1/litegate']\n"
        f"runpy.run_path({str(SCRIPT)!r}, run_name='__main__')\n"
    )
    done = subprocess.run([sys.executable, "-c", wrapper],
                          capture_output=True, text=True, cwd=REPO, timeout=120)

    reason = _could_not_run(done)
    assert "psycopg" in reason, "ต้องบอกว่าขาดอะไรและลงอย่างไร"


def test_no_database_named_at_all_is_exit_status_two(monkeypatch):
    monkeypatch.delenv("GW_DATABASE_URL", raising=False)
    done = subprocess.run([sys.executable, str(SCRIPT)],
                          capture_output=True, text=True, cwd=REPO, timeout=120)

    assert "no database" in _could_not_run(done)


# ── ฐานของรุ่นเก่า ────────────────────────────────────────────────────────────

def test_a_database_from_before_bundles_and_key_caps_is_still_read(tmp_path):
    """เครื่องที่ยังไม่ได้อัปเกรดคือเครื่องที่รายงานนี้มีไว้ให้ — ตารางของมันคือของรุ่นเก่า"""
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as raw:
        raw.executescript("""
            CREATE TABLE users (id TEXT PRIMARY KEY, external_id TEXT, email TEXT,
                display_name TEXT, role TEXT, status TEXT, created_at TEXT, updated_at TEXT);
            CREATE TABLE api_keys (id TEXT PRIMARY KEY, user_id TEXT, course_id TEXT,
                name TEXT, key_prefix TEXT, key_hash TEXT, scopes TEXT, models TEXT,
                expires_at TEXT, revoked_at TEXT, last_used_at TEXT,
                created_at TEXT, updated_at TEXT);
            CREATE TABLE quota_policies (id TEXT PRIMARY KEY, scope TEXT, enabled INTEGER);
            INSERT INTO users (id, external_id, role) VALUES ('u1', 'root', 'admin');
            INSERT INTO users (id, external_id, role) VALUES ('u2', 'teach', 'instructor');
            INSERT INTO api_keys (id, user_id, name, key_prefix, key_hash, scopes, models,
                                  created_at)
                VALUES ('k1', 'u1', 'script', 'edu_sk_aaaa', 'h1', '[]', '["coding"]',
                        '2026-01-01 00:00:00'),
                       ('k2', 'u1', 'ops', 'edu_sk_bbbb', 'h2', '["admin"]', '[]',
                        '2026-01-02 00:00:00'),
                       ('k3', 'u2', 'class', 'edu_sk_cccc', 'h3', '[]', '[]',
                        '2026-01-03 00:00:00');
            UPDATE api_keys SET course_id = 'w1' WHERE id = 'k3';
        """)

    done = run(db, "--json")

    assert done.returncode == 1, done.stderr
    out = json.loads(done.stdout)
    losers = {row["id"]: row for row in out["loses_admin_access"]}
    assert set(losers) == {"k1", "k3"}
    # role ที่บันทึกก่อนเปลี่ยนชื่อ ("instructor") ยังเป็น manager — เหมือนที่เกตเวย์อ่าน
    assert losers["k3"]["owner_role"] == "manager" and losers["k3"]["limited_by"] == ["workspace"]
    assert {row["id"] for row in out["unchanged"]} == {"k2"}
    assert "api_keys.access_groups" in out["columns_missing"]
    assert "quota_policies.api_key_id" in out["columns_missing"]
