"""แคชคำตอบ — ขอบเขตต้องแคบ · tenant ต้องแยกขาด · hit ต้องยังหักโควตา

เทสที่สำคัญที่สุดในไฟล์นี้คือสองข้อ: คำตอบของ tenant หนึ่งต้องไม่มีทางไปโผล่ที่อีก tenant
และ cache hit ต้องคิดเงินเหมือนไม่ได้แคช · สองข้อนี้พลาดแล้วเจ็บกว่าการที่แคชไม่ทำงานเลย
"""
from __future__ import annotations

import pytest

from app.core import responsecache


class _P:
    def __init__(self, user_id: str, workspace_id: str | None = None) -> None:
        self.user_id = user_id
        self.workspace_id = workspace_id


BASE = {"model": "coder", "temperature": 0, "messages": [{"role": "user", "content": "hi"}]}


def _key(principal, payload=None, **kw):
    args = {"alias": "coder", "upstream_model": "coder", "protocol": "openai"}
    args.update(kw)
    return responsecache.build_key(principal, payload=payload or BASE, **args)


# ── ขอบเขต: อะไรแคชไม่ได้บ้าง ──────────────────────────────────────────────────

@pytest.mark.parametrize("payload,why", [
    ({**BASE, "temperature": 0.7}, "temperature ไม่ใช่ 0"),
    ({k: v for k, v in BASE.items() if k != "temperature"}, "ไม่ได้ระบุ temperature"),
    ({**BASE, "tools": [{"type": "function"}]}, "มี tools"),
    ({**BASE, "tool_choice": "auto"}, "มี tool_choice"),
    ({**BASE, "n": 2}, "n ไม่ใช่ 1"),
])
def test_requests_that_must_never_be_cached(payload, why):
    assert responsecache.cacheable_reason(payload) is not None, why
    assert _key(_P("u1"), payload) is None, why


def test_a_plain_deterministic_request_is_cacheable():
    assert responsecache.cacheable_reason(BASE) is None
    assert _key(_P("u1")) is not None


# ── tenant แยกขาด — ข้อที่พลาดแล้วเจ็บที่สุด ──────────────────────────────────

def test_two_tenants_never_share_a_key():
    """คำถามเดียวกันเป๊ะ แต่คนละ workspace ต้องได้คนละ key"""
    a = _key(_P("u1", "acme"))
    b = _key(_P("u2", "globex"))
    assert a and b and a != b


def test_users_without_a_workspace_are_isolated_per_person():
    a = _key(_P("alice"))
    b = _key(_P("bob"))
    assert a and b and a != b


def test_the_tenant_is_a_prefix_not_a_field_to_filter_on():
    """ต้องอ่าน key แล้วรู้เจ้าของทันที — ไม่ใช่ต้องไปเปิดค่าข้างในดู

    ถ้าเก็บรวมกันแล้วค่อยกรองทีหลัง วันที่ใครลืมกรองคือวันที่คำตอบข้ามองค์กร
    """
    assert _key(_P("u1", "acme")).startswith("rc:w:acme:")
    assert _key(_P("solo")).startswith("rc:u:solo:")


def test_people_in_the_same_workspace_do_share():
    """เหตุผลที่แคชมีประโยชน์จริง — คนในทีมเดียวกันถามซ้ำกันบ่อย"""
    assert _key(_P("alice", "acme")) == _key(_P("bob", "acme"))


# ── key ต้องเปลี่ยนเมื่อสิ่งที่มีผลต่อคำตอบเปลี่ยน ─────────────────────────────

def test_swapping_the_upstream_weights_invalidates_the_cache():
    """alias เดิมชี้ไปคนละ weights ได้เมื่อ routing เปลี่ยน — ไม่งั้นได้ของเก่าไปอีก 5 นาที"""
    assert _key(_P("u1"), upstream_model="coder-v1") != _key(_P("u1"), upstream_model="coder-v2")


def test_a_different_prompt_is_a_different_key():
    other = {**BASE, "messages": [{"role": "user", "content": "ถามอย่างอื่น"}]}
    assert _key(_P("u1")) != _key(_P("u1"), other)


def test_fields_that_cannot_change_the_answer_do_not_split_the_cache():
    """metadata ที่ไม่มีผลต่อเนื้อคำตอบไม่ควรทำให้ hit rate พัง"""
    noisy = {**BASE, "user": "ใครก็ได้", "metadata": {"trace": "abc"}}
    assert _key(_P("u1")) == _key(_P("u1"), noisy)


# ── พฤติกรรมของตัวเก็บ ────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_it_stores_and_returns_what_was_put_in():
    cache = responsecache.ResponseCache()
    await cache.put("k", {"data": {"choices": [1]}})
    assert (await cache.get("k"))["data"] == {"choices": [1]}


@pytest.mark.anyio
async def test_a_miss_is_none_not_an_error():
    assert await responsecache.ResponseCache().get("ไม่เคยเก็บ") is None


@pytest.mark.anyio
async def test_a_broken_backend_degrades_to_a_miss_instead_of_failing_the_request():
    """แคชพังต้องไม่ทำให้คำขอพัง — มันมีไว้เร่งความเร็ว ไม่ใช่สิ่งที่ขาดไม่ได้"""
    class _Broken:
        async def get(self, *a, **k): raise RuntimeError("redis ตาย")
        async def set(self, *a, **k): raise RuntimeError("redis ตาย")

    cache = responsecache.ResponseCache(_Broken())
    assert await cache.get("k") is None
    await cache.put("k", {"data": {}})          # ต้องไม่โยน


@pytest.mark.anyio
async def test_the_local_fallback_does_not_grow_without_bound():
    cache = responsecache.ResponseCache(max_local=8)
    for i in range(40):
        await cache.put(f"k{i}", {"data": {"i": i}})
    assert len(cache._local) <= 8


def test_the_cache_is_off_unless_switched_on():
    """เปลี่ยนสิ่งที่ผู้ใช้ได้รับ จึงต้องเปิดเอง ไม่ใช่ติดมาเงียบ ๆ"""
    from app.config import Settings

    assert Settings().response_cache is False


# ── end-to-end: hit ต้องยังหักโควตา ──────────────────────────────────────────

import httpx  # noqa: E402
import respx  # noqa: E402

from tests.test_streaming_connection_release import (  # noqa: E402
    NON_STREAM_REPLY,
    UPSTREAM_CHAT,
    auth,
    counted,
    member,
)


def _ask(client, key: str, content: str = "hi", **extra):
    return client.post(
        "/v1/chat/completions",
        headers=auth(key),
        json={
            "model": "coding",
            "messages": [{"role": "user", "content": content}],
            "temperature": 0,
            **extra,
        },
    )


@respx.mock
def test_a_cache_hit_still_charges_the_tenant(client):
    """ข้อที่ห้ามพลาด: ไม่หัก = ถามซ้ำได้ฟรีไม่จำกัด

    เป็นช่องโหว่รายได้แบบเดียวกับ "ตัดการเชื่อมต่อ = ใช้ฟรี" ที่ปิดไปใน 1.6.0 แค่คนละทาง
    """
    from app.core.responsecache import ResponseCache

    state = client.app.state.services
    state.response_cache = ResponseCache()
    try:
        user, key = member(client, "cache-billing-01")
        route = respx.post(UPSTREAM_CHAT).mock(
            return_value=httpx.Response(200, json=NON_STREAM_REPLY))

        before = counted(client, state, user["id"])
        first = _ask(client, key)
        assert first.status_code == 200
        assert first.headers.get("x-litegate-cache") == "miss"

        second = _ask(client, key)
        assert second.status_code == 200
        assert second.headers.get("x-litegate-cache") == "hit", "รอบสองต้องมาจากแคช"

        assert route.call_count == 1, "แคชต้องกัน backend ไม่ให้ถูกเรียกซ้ำ"
        assert second.json()["choices"] == first.json()["choices"]

        after = counted(client, state, user["id"])
        assert after.requests == before.requests + 2, (
            "cache hit ต้องนับเป็นคำขอด้วย ไม่งั้นถามซ้ำได้ฟรี")
        assert after.output_tokens > before.output_tokens, "token ต้องถูกหักเหมือนกัน"
    finally:
        state.response_cache = None


@respx.mock
def test_one_tenants_answer_never_reaches_another(client):
    """ถ้าข้อนี้พัง คือคำตอบของลูกค้ารายหนึ่งไปโผล่ที่อีกราย"""
    from app.core.responsecache import ResponseCache

    state = client.app.state.services
    state.response_cache = ResponseCache()
    try:
        _, key_a = member(client, "tenant-a-01")
        _, key_b = member(client, "tenant-b-01")
        route = respx.post(UPSTREAM_CHAT).mock(
            return_value=httpx.Response(200, json=NON_STREAM_REPLY))

        assert _ask(client, key_a).headers.get("x-litegate-cache") == "miss"
        assert _ask(client, key_a).headers.get("x-litegate-cache") == "hit"
        # คนละคน คำถามเดียวกันเป๊ะ — ต้องไม่เห็นของอีกคน
        assert _ask(client, key_b).headers.get("x-litegate-cache") == "miss"
        assert route.call_count == 2, "ต้องไปถาม backend ใหม่ให้ tenant ที่สอง"
    finally:
        state.response_cache = None


@respx.mock
def test_a_request_with_tools_is_never_served_from_cache(client):
    from app.core.responsecache import ResponseCache

    state = client.app.state.services
    state.response_cache = ResponseCache()
    try:
        _, key = member(client, "cache-tools-01")
        route = respx.post(UPSTREAM_CHAT).mock(
            return_value=httpx.Response(200, json=NON_STREAM_REPLY))
        tools = [{"type": "function", "function": {"name": "f", "parameters": {}}}]
        for _ in range(2):
            assert _ask(client, key, tools=tools).headers.get("x-litegate-cache") != "hit"
        assert route.call_count == 2, "คำขอที่มี tools ต้องถึง backend ทุกครั้ง"
    finally:
        state.response_cache = None
