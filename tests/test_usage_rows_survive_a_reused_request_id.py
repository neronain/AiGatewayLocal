"""แถว usage เป็นของเกตเวย์ ไม่ใช่ของค่าที่ client เลือกเอง

`usage_logs.request_id` มี UNIQUE · ตรวจ 2026-10-05 พบว่าค่านั้นรับมาจาก header
`x-request-id` ของ client ตรง ๆ สองเรื่องจึงเกิดพร้อมกัน:

1. **ส่งค่าเดิมซ้ำ = แถวที่สองชน UNIQUE** · client ทำให้ usage ของตัวเองหายได้ด้วยการ
   ใช้ id เดิม (โควตายังนับ เพราะบันทึกคนละทาง — ที่หายคือแถวที่รายงานกับบิลอ่าน)
2. **ทั้ง batch หายไปด้วย** · flush ใส่ทุกแถวใน transaction เดียว แถวเดียวที่ชนจึงพาแถว
   ของสมาชิกคนอื่นที่บังเอิญอยู่รอบเดียวกันทิ้งไปหมด

เป็นปัญหาชนิดเดียวกับใบจองช่องใน 2eb489a (tests/test_slots_follow_the_backend.py ข้อ 4)

เทสในไฟล์นี้ยิงคำขอจริงเข้าแอป แล้วนับแถวผ่าน admin API ตัวเดียวกับที่รายงานใช้ —
ไม่ได้เปิดฐานข้อมูลดูเอง ยกเว้นส่วนท้ายที่ทดสอบ `UsageRecorder` ตรง ๆ
"""

from __future__ import annotations

import logging

import httpx
import pytest
import respx
from sqlalchemy import func, select

CODING = "http://dgx03:8000"
CHAT = f"{CODING}/v1/chat/completions"


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def reply() -> dict:
    return {
        "id": "chatcmpl-1", "object": "chat.completion", "model": "upstream-name",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "ok"}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
    }


def chat(client, key: str, **headers: str):
    return client.post(
        "/v1/chat/completions",
        headers={**auth(key), **headers},
        json={"model": "coding", "messages": [{"role": "user", "content": "hi"}]},
    )


def requests_logged(client, alias: str = "coding") -> int:
    """จำนวนคำขอของ alias นี้ตามที่รายงานของแอดมินเห็น"""
    summary = client.get("/admin/usage/summary?days=1", headers=auth(client.admin_key))
    assert summary.status_code == 200, summary.text
    return sum(row["requests"] for row in summary.json()["by_model"] if row["model"] == alias)


def requests_by_key(client) -> dict[str, int]:
    rows = client.get("/admin/usage/by-key?days=1", headers=auth(client.admin_key))
    assert rows.status_code == 200, rows.text
    return {row["api_key_id"]: row["requests"] for row in rows.json()["data"]}


# ---------------------------------------------------------------------------
# 1. ค่าซ้ำจาก client ต้องไม่ทำให้แถวหาย
# ---------------------------------------------------------------------------
@respx.mock
def test_two_requests_with_the_same_x_request_id_are_both_logged(client, member_key, caplog):
    respx.post(CHAT).mock(return_value=httpx.Response(200, json=reply()))

    with caplog.at_level(logging.WARNING, logger="app.core.usage"):
        answers = [chat(client, member_key, **{"x-request-id": "always-the-same"})
                   for _ in range(2)]
        assert [a.status_code for a in answers] == [200, 200]
        logged = requests_logged(client)

    assert logged == 2, f"สองคำขอสำเร็จ แต่รายงานเห็น {logged} แถว"
    assert not [r for r in caplog.records if r.name == "app.core.usage"], (
        "id ซ้ำจาก client ไม่ควรไปถึงฐานข้อมูลเลย: "
        + "; ".join(r.getMessage() for r in caplog.records)
    )


@respx.mock
def test_a_reused_id_does_not_take_another_members_row_with_it(client, member_key):
    """แถวของคนอื่นที่อยู่ flush รอบเดียวกัน — ตัวที่เสียหายโดยไม่ได้ทำอะไรผิด"""
    respx.post(CHAT).mock(return_value=httpx.Response(200, json=reply()))

    assert chat(client, member_key, **{"x-request-id": "dup"}).status_code == 200
    assert chat(client, client.admin_key).status_code == 200
    assert chat(client, member_key, **{"x-request-id": "dup"}).status_code == 200

    counts = sorted(requests_by_key(client).values())
    assert counts == [1, 2], f"คาดว่าแอดมิน 1 แถว สมาชิก 2 แถว ได้ {counts}"


@respx.mock
def test_the_client_still_gets_its_own_id_back_and_the_gateway_id_beside_it(client, member_key):
    """ค่าที่ client ส่งมายัง echo กลับเหมือนเดิม · id ของเกตเวย์มาใน header อีกตัว"""
    respx.post(CHAT).mock(return_value=httpx.Response(200, json=reply()))

    first = chat(client, member_key, **{"x-request-id": "trace-42"})
    second = chat(client, member_key, **{"x-request-id": "trace-42"})
    bare = chat(client, member_key)

    assert first.headers["x-request-id"] == second.headers["x-request-id"] == "trace-42"
    ours = [r.headers["x-litegate-request-id"] for r in (first, second, bare)]
    assert len(set(ours)) == 3, f"id ของเกตเวย์ต้องไม่ซ้ำกันเลย: {ours}"
    assert "trace-42" not in ours
    # ไม่ส่งมา = ไม่มีอะไรให้ echo · ทั้งสอง header จึงเป็น id ของเกตเวย์ตัวเดียวกัน
    assert bare.headers["x-request-id"] == bare.headers["x-litegate-request-id"]


@respx.mock
def test_an_error_body_quotes_the_id_the_logs_use(client, member_key):
    """`request_id` ใน error คือตัวที่ใช้ grep log — ต้องเป็นของเกตเวย์ ไม่ใช่ค่าที่ซ้ำได้"""
    answer = client.post(
        "/v1/chat/completions",
        headers={**auth(member_key), "x-request-id": "trace-42"},
        json={"model": "no-such-model", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert answer.status_code >= 400
    assert answer.headers["x-request-id"] == "trace-42"
    assert answer.json()["error"]["request_id"] == answer.headers["x-litegate-request-id"]


# ---------------------------------------------------------------------------
# 2. ค่าของ client ยังตามหาได้
# ---------------------------------------------------------------------------
@respx.mock
def test_a_row_can_be_found_by_either_id(client, member_key):
    respx.post(CHAT).mock(return_value=httpx.Response(200, json=reply()))
    answers = [chat(client, member_key, **{"x-request-id": "trace-42"}) for _ in range(2)]
    chat(client, member_key, **{"x-request-id": "someone-else"})
    admin = auth(client.admin_key)

    by_client_id = client.get("/admin/usage/requests?id=trace-42", headers=admin)
    assert by_client_id.status_code == 200, by_client_id.text
    found = by_client_id.json()["data"]
    assert len(found) == 2
    assert {row["client_request_id"] for row in found} == {"trace-42"}
    assert {row["request_id"] for row in found} == {
        a.headers["x-litegate-request-id"] for a in answers
    }

    ours = answers[0].headers["x-litegate-request-id"]
    by_gateway_id = client.get(f"/admin/usage/requests?id={ours}", headers=admin).json()["data"]
    assert [row["request_id"] for row in by_gateway_id] == [ours]
    assert by_gateway_id[0]["model"] == "coding"
    assert by_gateway_id[0]["status"] == "success"

    assert client.get("/admin/usage/requests?id=never-sent", headers=admin).json()["data"] == []


@respx.mock
def test_a_member_cannot_look_up_usage_rows(client, member_key):
    answer = client.get("/admin/usage/requests?id=trace-42", headers=auth(member_key))
    assert answer.status_code == 403


@respx.mock
def test_an_oversized_x_request_id_cannot_break_the_row(client, member_key):
    """Postgres ปฏิเสธค่าที่ยาวเกินคอลัมน์ (SQLite ไม่) — ตัดก่อนถึงฐานข้อมูลเสมอ"""
    respx.post(CHAT).mock(return_value=httpx.Response(200, json=reply()))
    huge = "x" * 4000

    answer = chat(client, member_key, **{"x-request-id": huge})

    assert answer.status_code == 200
    assert answer.headers["x-request-id"] == huge, "ที่ echo กลับคือค่าของเขาทั้งก้อน เหมือนเดิม"
    assert requests_logged(client) == 1
    rows = client.get(
        f"/admin/usage/requests?id={answer.headers['x-litegate-request-id']}",
        headers=auth(client.admin_key),
    ).json()["data"]
    assert rows[0]["client_request_id"] == "x" * 128


# ---------------------------------------------------------------------------
# 3. แถวเสียหนึ่งแถวต้องไม่พาทั้ง batch ไปด้วย — ไม่ว่ามันจะเสียเพราะอะไร
# ---------------------------------------------------------------------------
def _record(request_id: str, **extra):
    from app.core.usage import UsageRecord

    return UsageRecord(request_id=request_id, model_alias="coding", protocol="openai", **extra)


async def _saved_ids() -> set[str]:
    from app.db.models import UsageLog
    from app.db.session import session_scope

    async with session_scope() as session:
        return set((await session.execute(select(UsageLog.request_id))).scalars())


@pytest.fixture
async def recorder(temp_db):
    from app.core.usage import UsageRecorder
    from app.db.session import dispose_db, init_db

    await init_db()
    yield UsageRecorder()
    await dispose_db()


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param({"request_id": "already-there"}, id="duplicate id"),
        pytest.param({"request_id": "no-model", "model_alias": None}, id="missing required value"),
    ],
)
async def test_one_rejected_row_does_not_cost_the_rest_of_the_batch(recorder, caplog, bad):
    from dataclasses import replace

    await recorder.submit(_record("already-there"))
    assert await recorder.flush() == 1

    for record in (_record("a"), replace(_record("x"), **bad), _record("b")):
        await recorder.submit(record)
    with caplog.at_level(logging.ERROR, logger="app.core.usage"):
        saved = await recorder.flush()

    assert saved == 2
    assert await _saved_ids() == {"already-there", "a", "b"}
    assert recorder.rejected == 1
    assert recorder.pending == 0, "แถวที่ฐานข้อมูลไม่รับต้องไม่ถูกเก็บไว้ลองใหม่ไม่รู้จบ"
    dropped = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(dropped) == 1 and bad["request_id"] in dropped[0], dropped


async def test_rows_wait_for_a_database_that_is_down_instead_of_being_dropped(
    recorder, monkeypatch
):
    """ฐานข้อมูลล็อก/ต่อไม่ได้ ไม่ใช่ความผิดของแถวไหน — เก็บไว้ลองรอบหน้า ไม่ลองทีละแถว"""
    from contextlib import asynccontextmanager

    from sqlalchemy.exc import OperationalError

    from app.core import usage as usage_mod

    real_scope = usage_mod.session_scope
    attempts = 0

    @asynccontextmanager
    async def down():
        nonlocal attempts
        attempts += 1
        raise OperationalError("INSERT", {}, Exception("database is locked"))
        yield  # pragma: no cover

    for request_id in ("a", "b", "c"):
        await recorder.submit(_record(request_id))

    monkeypatch.setattr(usage_mod, "session_scope", down)
    assert await recorder.flush() == 0
    assert attempts == 1, "ฐานข้อมูลล่มทั้งตัว ลองทีละแถวคือรอ timeout เดิมซ้ำอีก N รอบ"
    assert await recorder.flush() == 0

    monkeypatch.setattr(usage_mod, "session_scope", real_scope)
    await recorder.submit(_record("d"))
    assert await recorder.flush() == 4
    assert await _saved_ids() == {"a", "b", "c", "d"}
    assert recorder.rejected == 0


async def test_a_long_outage_cannot_grow_the_buffer_without_bound(recorder, monkeypatch):
    from contextlib import asynccontextmanager

    from sqlalchemy.exc import OperationalError

    from app.core import usage as usage_mod

    @asynccontextmanager
    async def down():
        raise OperationalError("INSERT", {}, Exception("database is locked"))
        yield  # pragma: no cover

    monkeypatch.setattr(usage_mod, "MAX_BUFFER", 3)
    monkeypatch.setattr(usage_mod, "session_scope", down)

    for request_id in ("a", "b", "c"):
        await recorder.submit(_record(request_id))
    await recorder.flush()
    for request_id in ("d", "e"):
        await recorder.submit(_record(request_id))
    await recorder.flush()

    assert recorder.pending == 3


async def _column_order() -> list[str]:
    from sqlalchemy import inspect

    from app.db.session import get_engine

    async with get_engine().connect() as conn:
        return await conn.run_sync(
            lambda sync: [c["name"] for c in inspect(sync).get_columns("usage_logs")]
        )


async def test_an_existing_database_gains_the_new_column_on_startup(temp_db):
    """เครื่องที่รันอยู่มีตาราง usage_logs แบบเก่า — ต้องเติมคอลัมน์เอง ไม่ใช่ล้มตอน INSERT

    และต้องได้ **ลำดับคอลัมน์เดียวกับเครื่องติดตั้งใหม่** · วิธีย้าย usage_logs ไป Postgres
    ในคู่มือจับคู่คอลัมน์ตามตำแหน่ง ลำดับต่างกัน = ค่าลงผิดช่องโดยไม่มี error
    """
    from sqlalchemy import text

    from app.core.usage import UsageRecorder
    from app.db.models import UsageLog
    from app.db.session import dispose_db, get_engine, init_db, session_scope

    await init_db()
    fresh = await _column_order()
    # ฐานของรุ่นก่อน client_request_id ไม่มีคอลัมน์ที่ประกาศหลังจากนั้นด้วย (คอลัมน์ใหม่ต่อท้าย
    # เสมอ) จึงถอดออกทั้งช่วงท้าย ไม่ใช่ตัวเดียว · ถอดเฉพาะตัวกลางแล้วเติมกลับ มันจะไปต่อท้ายตัวที่
    # ใหม่กว่า ซึ่งเป็นฐานข้อมูลที่ไม่เคยมีอยู่จริง — เทสนี้ล้มแบบนั้นตอนเพิ่ม cache_hit (2026-10-09)
    newer = fresh[fresh.index("client_request_id"):]
    async with get_engine().begin() as conn:
        for name in reversed(newer):
            await conn.execute(text(f"ALTER TABLE usage_logs DROP COLUMN {name}"))
    await dispose_db()

    await init_db()
    assert await _column_order() == fresh
    recorder = UsageRecorder()
    await recorder.submit(_record("after-upgrade", client_request_id="trace-42"))
    assert await recorder.flush() == 1
    async with session_scope() as session:
        assert (
            await session.execute(
                select(func.count()).where(UsageLog.client_request_id == "trace-42")
            )
        ).scalar_one() == 1
    await dispose_db()
