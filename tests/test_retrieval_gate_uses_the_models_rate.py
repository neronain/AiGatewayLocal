"""ด่าน context ของ /v1/embeddings กับ /v1/rerank ต้องนับด้วยอัตราเดียวกับบิล

ตรวจ 2026-10-06: โมเดล embedding ที่วัด tokenizer ได้ 3.86 อักขระไทยต่อ token หน้าต่าง 8,192
ได้เอกสารไทย 16,000 อักขระ

    ยอดที่จะบันทึก (อัตราของโมเดล)   4,145 token   — อยู่ในหน้าต่างสบาย
    ด่าน context (ค่ากลาง 1.6)        10,000 token  — 400 CONTEXT_LENGTH_EXCEEDED

ด่านปฏิเสธเอกสารที่โมเดลรับได้ · และกลับกัน โมเดลที่อัตราต่ำกว่า 1.6 (tokenizer ฉีกสระ) ถูก
ปล่อยผ่านด่านไปให้ backend ตอบ 400 เอง · สองตัวเลขนี้ต้องเป็นตัวเดียวกัน — เทสยิงผ่าน HTTP
แล้วเทียบ **คำตัดสินของด่าน** กับ **ยอดที่บันทึกจริง** ของคำขอเดียวกัน
"""

from __future__ import annotations

import httpx
import pytest
import respx
import yaml

EMBED_URL = "http://dgx05:8000/v1/embeddings"
RERANK_URL = "http://dgx08:8000/v1/rerank"
WINDOW = 8192

THAI_DOC = "ก" * 16_000


def _model(alias: str, surface: str, base_url: str, **spec) -> str:
    return yaml.safe_dump({
        "apiVersion": "litegate.dev/v1", "kind": "Model",
        "metadata": {"alias": alias, "display_name": alias, "visibility": "member"},
        "spec": {
            "upstream_model": f"org/{alias}",
            "purpose": ["embedding" if surface == "embeddings" else "rerank"],
            "limits": {"context_tokens": WINDOW, "max_output_tokens": 16},
            "capabilities": {"chat": False, "streaming": False,
                             "embedding": surface == "embeddings",
                             "rerank": surface == "rerank"},
            "protocols": {"openai": False, surface: True},
            "endpoints": [{"name": "e1", "server_type": "vllm", "base_url": base_url,
                           "protocols": {"openai": False, surface: True}}],
            **spec,
        }}, sort_keys=False, allow_unicode=True)


@pytest.fixture
def retrieval_models(writable_config):
    """โมเดล embedding สามตัวที่ต่างกันแค่อัตรา tokenizer และ reranker หนึ่งตัว"""
    models = writable_config / "models"
    base = "http://dgx05:8000"
    (models / "embed-good.yaml").write_text(
        _model("embed-good", "embeddings", base, wide_chars_per_token=3.86))
    (models / "embed-torn.yaml").write_text(
        _model("embed-torn", "embeddings", base, wide_chars_per_token=1.0))
    (models / "embed-plain.yaml").write_text(_model("embed-plain", "embeddings", base))
    (models / "rerank-good.yaml").write_text(
        _model("rerank-good", "rerank", "http://dgx08:8000", wide_chars_per_token=3.86))
    return writable_config


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture
def backends():
    """backend ที่ไม่รายงาน usage — ยอดที่บันทึกจึงเป็นค่าประมาณของเกตเวย์เอง ซึ่งคือตัวเลข
    ที่ต้องตรงกับที่ด่านใช้"""
    with respx.mock:
        yield {
            "embed": respx.post(EMBED_URL).mock(return_value=httpx.Response(200, json={
                "object": "list", "model": "up",
                "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}]})),
            "rerank": respx.post(RERANK_URL).mock(return_value=httpx.Response(200, json={
                "model": "up", "results": [{"index": 0, "relevance_score": 0.9}]})),
        }


def _embed(client, key, alias: str, text: str):
    return client.post("/v1/embeddings", headers=auth(key),
                       json={"model": alias, "input": [text]})


def test_a_document_the_model_can_hold_is_not_refused_by_the_fallback_rate(
        retrieval_models, backends, client, member_key):
    response = _embed(client, member_key, "embed-good", THAI_DOC)

    assert response.status_code == 200, response.text
    billed = response.json()["usage"]["litegate"]
    assert billed["accounting"] == "estimated"
    assert billed["text_input_tokens"] == int(16_000 / 3.86) == 4145
    assert billed["text_input_tokens"] < WINDOW, "ด่านปล่อยผ่าน และบิลก็บอกว่าอยู่ในหน้าต่าง"


def test_a_document_too_long_for_a_poor_tokenizer_is_refused_by_the_gate_not_the_backend(
        retrieval_models, backends, client, member_key):
    """ทิศกลับ: 12,000 อักขระบนโมเดล 1.0 อักขระ/token = 12,000 token > 8,192 × 1.15

    ค่ากลาง 1.6 ให้ 7,500 — เดิมผ่านด่านแล้วไปพังที่ backend
    """
    response = _embed(client, member_key, "embed-torn", "ก" * 12_000)

    assert response.status_code == 400, response.text
    error = response.json()["error"]
    assert error["code"] == "CONTEXT_LENGTH_EXCEEDED"
    assert error["details"]["estimated_item_tokens"] == 12_000
    assert not backends["embed"].called


def test_a_model_without_a_measured_rate_is_judged_as_before(
        retrieval_models, backends, client, member_key):
    """ไม่ตั้งอัตรา = ค่ากลาง 1.6 ทั้งด่านและบิล — พฤติกรรมเดิม"""
    refused = _embed(client, member_key, "embed-plain", THAI_DOC)          # 10,000 > 9,420
    assert refused.status_code == 400
    assert refused.json()["error"]["details"]["estimated_item_tokens"] == 10_000

    served = _embed(client, member_key, "embed-plain", "ก" * 12_000)       # 7,500
    assert served.status_code == 200, served.text
    assert served.json()["usage"]["litegate"]["text_input_tokens"] == 7_500


def test_rerank_judges_the_largest_pair_at_the_models_rate(
        retrieval_models, backends, client, member_key):
    query = "ข" * 386                    # 100 token ที่ 3.86
    response = client.post("/v1/rerank", headers=auth(member_key), json={
        "model": "rerank-good", "query": query, "documents": [THAI_DOC, "สั้น"]})

    assert response.status_code == 200, response.text
    # query ถูกนับซ้ำต่อเอกสาร: (386 + 16,000 + 386 + 4) อักขระไทย ที่ 3.86
    assert response.json()["usage"]["litegate"]["text_input_tokens"] == int(16_776 / 3.86)


@pytest.mark.parametrize("rate, verdict", [(3.86, "fits"), (None, "too long"), (1.0, "too long")])
def test_the_gate_and_the_bill_never_disagree_about_one_item(rate, verdict):
    """กติกา: ชิ้นเดียว → `largest_item_tokens` ที่ด่านอ่าน == token ที่บิลจะคิด"""
    from app.core.retrieval import profile_embeddings_request, profile_rerank_request
    from app.core.tokens import TokenRates, estimate_text_tokens

    rates = TokenRates(wide=rate)
    embedding = profile_embeddings_request({"input": [THAI_DOC]}, rates)
    assert embedding.largest_item_tokens == estimate_text_tokens(embedding, rates)
    assert (embedding.largest_item_tokens <= WINDOW) == (verdict == "fits")

    pair = profile_rerank_request({"query": "ข" * 400, "documents": [THAI_DOC]}, rates)
    assert abs(pair.largest_item_tokens - estimate_text_tokens(pair, rates)) <= 1
