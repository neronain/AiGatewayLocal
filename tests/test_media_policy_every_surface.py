"""นโยบายภาพต้องบังคับเหมือนกันทั้งสาม surface — ไม่ใช่สองในสาม

ตรวจ 2026-10-06: `vision_policy.max_images_per_request: 4` · ส่ง 9 ภาพ

    /v1/chat/completions  -> 400 TOO_MANY_IMAGES
    /v1/messages          -> 400 TOO_MANY_IMAGES
    /v1/responses         -> 200 และ backend ได้รับทั้ง 9 ใบ

เพดานที่ข้ามได้ด้วยการเปลี่ยน path ไม่ใช่เพดาน · ไฟล์นี้ยิงคำขอ *รูปเดียวกัน* เข้าทั้งสามทาง
แล้วยืนยันว่าได้คำตอบเดียวกัน และ backend ไม่ถูกเรียกเลยเมื่อถูกปฏิเสธ — ครอบนโยบายต่อคำขอ
(จำนวน) และนโยบายต่อภาพ (ขนาด · ชนิด · URL ภายนอก) ไปพร้อมกัน เพื่อให้ทางที่สี่ที่เพิ่มมา
วันหน้ามีที่ให้เติมบรรทัดเดียวแล้วรู้ทันทีว่าลืมอะไร
"""

from __future__ import annotations

import base64

import httpx
import pytest
import respx
import yaml

from tests.conftest import png_data_url

GEMMA = "http://dgx02:8000"     # gemma-vision · vLLM · endpoint พูด openai อย่างเดียว

REPLY = {
    "id": "c", "object": "chat.completion", "model": "up",
    "choices": [{"index": 0, "finish_reason": "stop",
                 "message": {"role": "assistant", "content": "ok"}}],
    "usage": {"prompt_tokens": 50, "completion_tokens": 2, "total_tokens": 52},
}

SURFACES = ["chat", "messages", "responses"]


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture
def vision_everywhere(writable_config):
    """gemma-vision เปิดทั้งสาม surface — สองทางหลังเสิร์ฟผ่านตัวแปล ตามที่โมเดลในบ้านเป็นจริง"""
    path = writable_config / "models" / "gemma-vision.yaml"
    document = yaml.safe_load(path.read_text())
    document["spec"]["protocols"].update({"anthropic": True, "responses": True})
    path.write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=True))
    return writable_config


@pytest.fixture
def backend():
    with respx.mock:
        yield respx.post(f"{GEMMA}/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=REPLY))


def _send(client, key: str, surface: str, urls: list[str]):
    """คำขอเดียวกัน — ข้อความหนึ่งท่อนกับภาพตาม `urls` — ในรูปของแต่ละ surface"""
    if surface == "chat":
        return client.post("/v1/chat/completions", headers=auth(key), json={
            "model": "gemma-vision",
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "what is this?"},
                *({"type": "image_url", "image_url": {"url": u}} for u in urls)]}]})
    if surface == "messages":
        def block(url: str) -> dict:
            if url.startswith("data:"):
                head, data = url.split(",", 1)
                media_type = head[len("data:"):].split(";", 1)[0]
                return {"type": "image", "source": {
                    "type": "base64", "media_type": media_type, "data": data}}
            return {"type": "image", "source": {"type": "url", "url": url}}
        return client.post("/v1/messages", headers=auth(key), json={
            "model": "gemma-vision", "max_tokens": 16,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "what is this?"}, *(block(u) for u in urls)]}]})
    return client.post("/v1/responses", headers=auth(key), json={
        "model": "gemma-vision",
        "input": [{"role": "user", "content": [
            {"type": "input_text", "text": "what is this?"},
            *({"type": "input_image", "image_url": u} for u in urls)]}]})


def _code(response) -> str:
    return (response.json().get("error") or {}).get("code", "")


@pytest.mark.parametrize("surface", SURFACES)
def test_images_within_the_limit_reach_the_backend(
        vision_everywhere, backend, client, member_key, surface):
    """ตัวควบคุม: ถ้าทางนี้ไม่ผ่าน เทสข้างล่างที่คาดหวัง 400 ก็ไม่ได้พิสูจน์อะไร"""
    response = _send(client, member_key, surface, [png_data_url(8, 8)] * 4)
    assert response.status_code == 200, response.text
    assert backend.call_count == 1


@pytest.mark.parametrize("surface", SURFACES)
def test_too_many_images_is_refused_on_every_surface(
        vision_everywhere, backend, client, member_key, surface):
    response = _send(client, member_key, surface, [png_data_url(8, 8)] * 9)
    assert response.status_code == 400, response.text
    assert _code(response) == "TOO_MANY_IMAGES"
    assert not backend.called, "คำขอที่เกินเพดานต้องไม่ไปถึง backend"


@pytest.mark.parametrize("surface", SURFACES)
def test_an_oversized_image_is_refused_on_every_surface(
        vision_everywhere, backend, client, member_key, surface):
    # gateway.yaml: max_image_size_mb: 10 · 11 MB ของไบต์อะไรก็ได้ที่ขึ้นต้นด้วยหัว PNG
    blob = b"\x89PNG\r\n\x1a\n" + b"\x00" * (11 * 1024 * 1024)
    url = "data:image/png;base64," + base64.b64encode(blob).decode()
    response = _send(client, member_key, surface, [url])
    assert response.status_code == 413, response.text
    assert _code(response) == "IMAGE_TOO_LARGE"
    assert not backend.called


@pytest.mark.parametrize("surface", SURFACES)
def test_a_disallowed_image_type_is_refused_on_every_surface(
        vision_everywhere, backend, client, member_key, surface):
    # ป้ายบอกว่า PNG แต่ไบต์จริงเป็น GIF — ตัดสินจากไบต์ และ GIF ไม่อยู่ใน allowed_types
    gif = b"GIF89a" + b"\x00" * 64
    url = "data:image/png;base64," + base64.b64encode(gif).decode()
    response = _send(client, member_key, surface, [url])
    assert response.status_code == 415, response.text
    assert _code(response) == "IMAGE_TYPE_NOT_ALLOWED"
    assert not backend.called


@pytest.mark.parametrize("surface", SURFACES)
def test_a_remote_image_url_is_refused_on_every_surface(
        vision_everywhere, backend, client, member_key, surface):
    response = _send(client, member_key, surface, ["https://example.com/cat.png"])
    assert response.status_code == 400, response.text
    assert _code(response) == "REMOTE_IMAGE_URL_DISABLED"
    assert not backend.called
