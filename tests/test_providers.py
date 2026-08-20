"""ผู้ให้บริการออนไลน์ — ค่าตั้งต้นที่รู้อยู่แล้ว ไม่ต้องให้ผู้ใช้ไปค้นเอง

LiteGate เกิดมาเพื่อโมเดลบนเครื่องตัวเอง และนั่นยังเป็นเรื่องหลัก · แต่ของจริงคือทีม
ต้องผสม — ใช้ของตัวเองเป็นหลัก แล้วเรียกคลาวด์เฉพาะงานที่เครื่องตัวเองทำไม่ไหว
"""

import os
from pathlib import Path

import pytest

from app.core import providers
from app.registry.schema import Endpoint, Protocols, ServerType
from app.upstream.client import upstream_headers

ROOT = Path(__file__).resolve().parents[1]


def test_the_providers_the_team_asked_for_are_there():
    for wanted in ("gemini", "minimax", "openrouter", "openai", "anthropic"):
        assert wanted in providers.CLOUD, wanted


def test_no_api_key_ever_lives_in_the_table():
    """ตารางนี้ถูก commit ลง git — คีย์จริงต้องอยู่ใน env เท่านั้น"""
    source = (ROOT / "app" / "core" / "providers.py").read_text(encoding="utf-8")
    for leak in ("sk-", "AIza", "api_key=", "secret"):
        assert leak not in source, leak
    for entry in providers.catalogue():
        assert entry["suggested_env"].endswith("_API_KEY")


def test_every_preset_points_somewhere_real():
    for p in providers.CLOUD.values():
        assert p.base_url.startswith("https://"), p.id
        assert not p.base_url.endswith("/"), f"{p.id} มี / ท้าย จะทำให้ต่อ path เป็น //"
        assert p.health_path.startswith("/"), p.id


def _endpoint(server_type: ServerType) -> Endpoint:
    return Endpoint(
        name="t", server_type=server_type, base_url="https://x/v1",
        api_key_env="TEST_PROVIDER_KEY", protocols=Protocols(openai=True),
    )


def test_most_providers_take_a_bearer_token(monkeypatch):
    monkeypatch.setenv("TEST_PROVIDER_KEY", "secret-value")
    headers = upstream_headers(_endpoint(ServerType.GEMINI), {})
    assert headers["authorization"] == "Bearer secret-value"
    assert "x-api-key" not in headers


def test_anthropic_uses_its_own_header(monkeypatch):
    """ส่ง Authorization ให้ Anthropic ได้ 401 ที่อ่านแล้วนึกว่าคีย์ผิด"""
    monkeypatch.setenv("TEST_PROVIDER_KEY", "secret-value")
    headers = upstream_headers(_endpoint(ServerType.ANTHROPIC), {})
    assert headers["x-api-key"] == "secret-value"
    assert "authorization" not in headers, "ต้องไม่ส่งทั้งสองแบบพร้อมกัน"
    assert headers["anthropic-version"] == providers.ANTHROPIC_VERSION


def test_a_client_authorization_header_never_leaks_upstream(monkeypatch):
    """คีย์ของสมาชิกต้องไม่ถูกส่งต่อไปหาผู้ให้บริการ ไม่ว่ากรณีไหน"""
    monkeypatch.setenv("TEST_PROVIDER_KEY", "secret-value")
    incoming = {"authorization": "Bearer lg_sk_ของสมาชิก"}
    for server_type in (ServerType.ANTHROPIC, ServerType.GEMINI, ServerType.VLLM):
        headers = upstream_headers(_endpoint(server_type), incoming)
        assert "lg_sk_" not in str(headers), server_type


def test_local_runtimes_are_unaffected(monkeypatch):
    monkeypatch.setenv("TEST_PROVIDER_KEY", "secret-value")
    headers = upstream_headers(_endpoint(ServerType.VLLM), {})
    assert headers["authorization"] == "Bearer secret-value"


def test_the_console_offers_them_without_hardcoding_urls():
    """base URL อยู่ที่เดียวใน providers.py — ก๊อปไปฝั่ง client แล้วจะเพี้ยนตอนแก้"""
    page = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'optgroup class="ep-cloud"' in page or 'class="ep-cloud"' in page
    js = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    assert "loadProviders" in js and "/admin/providers" in js
    for provider in providers.CLOUD.values():
        assert provider.base_url not in js, f"{provider.id} ถูก hardcode ในหน้าเว็บ"
