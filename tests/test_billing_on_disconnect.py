"""พิสูจน์ช่องโหว่ 4.10 — ตัด connection กลาง stream แล้วไม่ถูกหักโควตา"""
from __future__ import annotations

import asyncio

import pytest

from app.api import openai as openai_api


@pytest.mark.anyio
async def test_finalize_survives_a_cancelled_stream():
    """finally: await ctx.finalize(...) ตายใต้ CancelledError ก่อนบันทึกอะไรเลย

    Starlette ยกเลิก task เมื่อ client หลุด → `finally` รันใต้ CancelledError →
    `await` ตัวแรกใน finalize โยนทิ้งทันที → quota.record() ไม่เคยรัน
    → ตัด connection = ใช้ฟรี
    """
    recorded: list[str] = []

    async def fake_finalize() -> None:
        await asyncio.sleep(0)          # จุด await แรก — ตรงนี้คือที่ที่มันตาย
        recorded.append("quota")

    async def request_handler() -> None:
        try:
            await asyncio.sleep(3600)   # แทน stream ที่กำลังไหล
        finally:
            await openai_api.finalize_even_if_cancelled(fake_finalize())

    task = asyncio.create_task(request_handler())
    await asyncio.sleep(0.05)
    task.cancel()                       # client หลุด
    with pytest.raises(asyncio.CancelledError):
        await task

    await openai_api.drain_pending_finalizers(timeout=2.0)
    assert recorded == ["quota"], "โควตาต้องถูกบันทึกแม้ client จะหลุดกลางทาง"


@pytest.mark.anyio
async def test_a_backlog_of_finalizers_is_bounded():
    """backend ล่มแล้ว finalize ค้างสะสม ต้องไม่กินหน่วยความจำไม่รู้จบ

    ทิ้งการบันทึกทิ้งไปพร้อม log error ดีกว่าให้ gateway ตายทั้งตัว — แต่ต้องดังพอ
    ให้เห็นว่ากำลังเสียรายได้อยู่
    """
    started = asyncio.Event()

    async def never_finishes() -> None:
        started.set()
        await asyncio.sleep(3600)

    held = []
    try:
        for _ in range(openai_api.MAX_PENDING_FINALIZERS):
            held.append(asyncio.create_task(
                openai_api.finalize_even_if_cancelled(never_finishes())))
        await started.wait()
        await asyncio.sleep(0.05)
        assert len(openai_api._PENDING) <= openai_api.MAX_PENDING_FINALIZERS

        # ตัวถัดไปต้องถูกปฏิเสธ ไม่ใช่ยัดเพิ่มเรื่อย ๆ
        before = len(openai_api._PENDING)
        await openai_api.finalize_even_if_cancelled(never_finishes())
        assert len(openai_api._PENDING) <= before
    finally:
        for task in held:
            task.cancel()
        for task in list(openai_api._PENDING):
            task.cancel()
        openai_api._PENDING.clear()


@pytest.mark.anyio
async def test_recording_twice_does_not_bill_twice():
    """finalize บอกใน docstring ว่า "exactly once" แต่เดิมไม่มีอะไรบังคับ

    พอห่อ shield แล้ว การเรียกซ้ำจะกลายเป็นการคิดเงินซ้ำจริง ๆ ไม่ใช่แค่เขียน log ซ้ำ
    """
    class _Ctx:
        request_id = "req-1"
        calls = 0

        async def _record_usage(self, *a, **kw):
            self.calls += 1

    ctx = _Ctx()
    ctx.finalize = openai_api._RequestContext.finalize.__get__(ctx, _Ctx)

    await ctx.finalize(None)
    await ctx.finalize(None)
    await openai_api.drain_pending_finalizers(timeout=2.0)
    assert ctx.calls == 1, "เรียกซ้ำต้องไม่บันทึกซ้ำ"
