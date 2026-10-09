"""คำตอบจากแคชต้องบอกตัวเองในแถว usage ว่ามาจากแคช

แถวของ cache hit เคยหน้าตาเหมือนคำขอสำเร็จทั่วไปทุกคอลัมน์: `status=success` · ชื่อเครื่องที่
"ตอบ" (ซึ่งไม่ได้เห็นคำขอนั้นเลย) · token ครบ · ต่างกันแค่ `latency_ms` ที่เป็น ~1 ms · อะไรก็ตาม
ที่อ่านความเร็วจากตารางนี้จึงแยกมันออกไม่ได้ — เปิดแคชเมื่อไร โมเดลนั้น "เร็วขึ้น" เองโดยที่
เครื่องไม่ได้เร็วขึ้นเลย (รายงานเปอร์เซ็นไทล์: tests/test_latency_percentiles.py)

โควตายังถูกหักเหมือนเดิมทุกอย่าง (ดู core/responsecache.py) — ที่เพิ่มคือป้ายบนแถว ไม่ใช่
ทางเดินใหม่ และไม่มี query เพิ่มบนทางเดินของคำขอ
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from tests.test_api import OPENAI_REPLY, UPSTREAM_CHAT, auth

ASK = {"model": "coding", "temperature": 0,
       "messages": [{"role": "user", "content": "refactor this function"}]}


@respx.mock
def test_a_cached_answer_says_so_in_its_usage_row(client, member_key):
    """ของจริงทั้งเส้น: คำขอแรกไปถึง backend · คำขอที่สองได้จากแคช — สองแถว สองป้าย"""
    from app.core.responsecache import ResponseCache

    client.app.state.services.response_cache = ResponseCache()
    route = respx.post(UPSTREAM_CHAT).mock(return_value=httpx.Response(200, json=OPENAI_REPLY))

    first = client.post("/v1/chat/completions", headers=auth(member_key), json=ASK)
    second = client.post("/v1/chat/completions", headers=auth(member_key), json=ASK)
    assert [r.headers["x-litegate-cache"] for r in (first, second)] == ["miss", "hit"]
    assert route.call_count == 1

    def row_of(response) -> dict:
        request_id = response.headers["x-request-id"]
        rows = client.get(f"/admin/usage/requests?id={request_id}",
                          headers=auth(client.admin_key)).json()["data"]
        assert len(rows) == 1
        return rows[0]

    assert row_of(first)["cache_hit"] is False
    assert row_of(second)["cache_hit"] is True
    # ที่เหลือของแถวยังเป็นอย่างเดิม: cache hit ถูกคิดโควตาและนับเป็นคำขอสำเร็จ
    assert (row_of(second)["status"], row_of(second)["total_tokens"]) == (
        "success", row_of(first)["total_tokens"])


@pytest.mark.sqlite_only  # แถวของ "รุ่นก่อน" เขียนด้วย SQL ดิบของ SQLite และอ่านสคีมาด้วย PRAGMA
def test_an_upgraded_database_gets_the_column_and_old_rows_are_not_lost(temp_db):
    """ฐานจริงมีแถว usage อยู่แล้ว · หลังอัปเกรดแถวเก่าต้องอ่านเป็น "ไม่ใช่แคช"

    ผู้อ่านกรองด้วย `cache_hit IS NOT TRUE` · ถ้ากรองด้วย `= false` แถวเก่าที่เป็น NULL (และ
    แถวที่รุ่นก่อนเขียนหลังถอยกลับ) จะหลุดจากทุกรายงานจนกว่าจะมีทราฟฟิกใหม่
    """
    import asyncio

    from sqlalchemy import select, text

    from app.db.models import UsageLog
    from app.db.session import dispose_db, get_engine, init_db, session_scope

    async def columns(conn) -> list[str]:  # noqa: ANN001
        return [c[1] for c in (await conn.execute(text("PRAGMA table_info(usage_logs)"))).all()]

    async def scenario() -> tuple[list[int], list[str], list[str]]:
        await init_db()
        async with get_engine().begin() as conn:
            # ฐานของรุ่นก่อน: ไม่มี cache_hit และไม่มีคอลัมน์ที่ประกาศหลังจากนั้น (คอลัมน์ใหม่ของ
            # ตารางนี้ต่อท้ายเสมอ) · มีแถวอยู่แล้วหนึ่งแถว
            fresh = await columns(conn)
            for name in reversed(fresh[fresh.index("cache_hit"):]):
                await conn.execute(text(f"ALTER TABLE usage_logs DROP COLUMN {name}"))
            await conn.execute(text(
                "INSERT INTO usage_logs (id, request_id, ts, model_alias, endpoint_name, "
                "protocol, request_modality, stream, text_input_tokens, visual_input_tokens, "
                "output_tokens, total_tokens, image_count, token_accounting, latency_ms, "
                "status, http_status, client_agent, cost_units) VALUES ('old', 'old', :ts, "
                "'coding', 'dgx03', 'openai', 'text', 0, 0, 0, 0, 0, 0, 'upstream', 1234, "
                "'success', 200, '', 0)"),
                {"ts": datetime.now(timezone.utc) - timedelta(minutes=5)})
        await init_db()
        async with session_scope() as session:
            kept = (await session.execute(
                select(UsageLog.latency_ms).where(UsageLog.cache_hit.is_not(True)))).scalars().all()
            upgraded = await columns(session)
        await dispose_db()
        return list(kept), fresh, upgraded

    kept, fresh, upgraded = asyncio.run(scenario())
    assert "cache_hit" in fresh
    assert upgraded == fresh, "ฐานที่อัปเกรดต้องได้ลำดับคอลัมน์เดียวกับฐานติดตั้งใหม่ (ดู UsageLog)"
    assert kept == [1234]
