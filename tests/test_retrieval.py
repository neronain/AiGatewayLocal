"""`/v1/embeddings` and `/v1/rerank`: the front door for the RAG half of the fleet.

Everything here runs against a real registry loaded from YAML, with the model
servers mocked at the HTTP boundary — because the thing worth proving is that
these two surfaces obey the *same* gates as chat (key, capability, quota, usage),
not that a handler returns 200.

The bodies mirror what `bundles/qwen3-embedding-8b` and `bundles/qwen3-reranker-4b`
say their vLLM instances actually serve.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import httpx
import pytest
import respx

from app.core.retrieval import profile_embeddings_request, profile_rerank_request
from app.core.tokens import estimate_text_tokens

REPO_ROOT = Path(__file__).resolve().parent.parent

EMBED_PRIMARY = "http://dgx05:8000/v1/embeddings"
EMBED_STANDBY = "http://dgx06:8000/v1/embeddings"
EMBED_ALT = "http://dgx07:8000/v1/embeddings"
RERANK_URL = "http://dgx08:8000/v1/rerank"
CHAT_URL = "http://dgx03:8000/v1/chat/completions"

EMBED_YAML = """
apiVersion: litegate.dev/v1
kind: Model
metadata:
  alias: embed
  display_name: Qwen3 Embedding 8B
  visibility: member
spec:
  upstream_model: Qwen/Qwen3-Embedding-8B
  purpose: [embedding]
  limits:
    context_tokens: 32768
    max_output_tokens: 16
  capabilities:
    chat: false
    streaming: false
    embedding: true
  protocols:
    openai: false
    embeddings: true
  routing:
    fallback: [embed-alt]
  endpoints:
    - name: primary
      server_type: vllm
      base_url: http://dgx05:8000
      priority: 200
      protocols: {openai: false, embeddings: true}
    - name: standby
      server_type: vllm
      base_url: http://dgx06:8000
      priority: 100
      protocols: {openai: false, embeddings: true}
"""

EMBED_ALT_YAML = """
apiVersion: litegate.dev/v1
kind: Model
metadata:
  alias: embed-alt
  display_name: Another Embedding Model
  visibility: member
spec:
  upstream_model: some-other/Embedding-Model
  purpose: [embedding]
  limits:
    context_tokens: 8192
    max_output_tokens: 16
  capabilities:
    chat: false
    streaming: false
    embedding: true
  protocols:
    openai: false
    embeddings: true
  endpoints:
    - name: alt
      server_type: vllm
      base_url: http://dgx07:8000
      protocols: {openai: false, embeddings: true}
"""

RERANK_YAML = """
apiVersion: litegate.dev/v1
kind: Model
metadata:
  alias: rerank
  display_name: Qwen3 Reranker 4B
  visibility: member
spec:
  upstream_model: Qwen/Qwen3-Reranker-4B
  purpose: [rerank]
  limits:
    context_tokens: 32768
    max_output_tokens: 16
  capabilities:
    chat: false
    streaming: false
    rerank: true
  protocols:
    openai: false
    rerank: true
  endpoints:
    - name: scorer
      server_type: vllm
      base_url: http://dgx08:8000
      protocols: {openai: false, rerank: true}
"""

EMBED_REPLY = {
    "object": "list",
    "model": "Qwen/Qwen3-Embedding-8B",
    "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2, 0.3]}],
    "usage": {"prompt_tokens": 4242, "total_tokens": 4242},
}

# vLLM ตอบ /v1/rerank ด้วย usage ที่มีแต่ total_tokens — ไม่มี prompt_tokens
RERANK_REPLY = {
    "id": "rerank-1",
    "model": "Qwen/Qwen3-Reranker-4B",
    "usage": {"total_tokens": 777},
    "results": [
        {"index": 1, "document": {"text": "the matching one"}, "relevance_score": 0.98},
        {"index": 0, "document": {"text": "the other one"}, "relevance_score": 0.10},
    ],
}


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture
def retrieval_config(temp_db, monkeypatch, tmp_path):
    """A copy of config/ with the two retrieval models added.

    Written before the `client` fixture builds the app, so it must sit *before*
    `client` in a test's parameter list — the same rule `writable_config` has.
    Kept out of the shipped `config/` on purpose: those files are the example
    registry every new install starts from, and neither of these backends exists
    on an install that has not deployed them.
    """
    from app import config as config_mod

    target = tmp_path / "config"
    shutil.copytree(REPO_ROOT / "config", target)
    (target / "models" / "embed.yaml").write_text(EMBED_YAML, encoding="utf-8")
    (target / "models" / "embed-alt.yaml").write_text(EMBED_ALT_YAML, encoding="utf-8")
    (target / "models" / "rerank.yaml").write_text(RERANK_YAML, encoding="utf-8")
    monkeypatch.setenv("GW_CONFIG_DIR", str(target))
    config_mod.get_settings.cache_clear()
    yield target
    config_mod.get_settings.cache_clear()


@pytest.fixture
def backends():
    with respx.mock:
        yield respx


def usage_rows(client) -> list[dict]:
    """The `usage_logs` rows as written, including columns no report exposes."""
    from sqlalchemy import select

    from app.db.models import UsageLog
    from app.db.session import session_scope

    async def _read() -> list[dict]:
        await client.app.state.services.usage.flush()
        async with session_scope() as session:
            rows = (await session.execute(select(UsageLog))).scalars().all()
            return [
                {
                    "model_alias": r.model_alias,
                    "protocol": r.protocol,
                    "request_modality": r.request_modality,
                    "text_input_tokens": r.text_input_tokens,
                    "visual_input_tokens": r.visual_input_tokens,
                    "output_tokens": r.output_tokens,
                    "total_tokens": r.total_tokens,
                    "image_count": r.image_count,
                    "token_accounting": r.token_accounting,
                    "ttft_ms": r.ttft_ms,
                    "stream": r.stream,
                    "status": r.status,
                    "endpoint_name": r.endpoint_name,
                }
                for r in rows
            ]

    return client.portal.call(_read)


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------
def test_an_embeddings_request_reaches_the_pooling_backend(
    retrieval_config, client, member_key, backends
):
    route = backends.post(EMBED_PRIMARY).mock(
        return_value=httpx.Response(200, json=EMBED_REPLY)
    )
    response = client.post(
        "/v1/embeddings",
        headers=auth(member_key),
        json={"model": "embed", "input": ["หนึ่ง", "สอง"]},
    )

    assert response.status_code == 200
    assert route.called
    sent = json.loads(route.calls[0].request.content)
    # The backend is addressed by the name *it* knows; the member never sees it.
    assert sent["model"] == "Qwen/Qwen3-Embedding-8B"
    assert sent["input"] == ["หนึ่ง", "สอง"]
    assert response.json()["model"] == "embed"
    assert "Qwen3-Embedding" not in response.text
    assert response.headers["x-litegate-served-by"] == "embed"


def test_a_rerank_request_is_passed_through_in_the_shape_vllm_serves(
    retrieval_config, client, member_key, backends
):
    """เราเป็นทางผ่าน ไม่ใช่ตัวแปล — ผลลัพธ์ต้องหน้าตาเหมือนยิงตรงเข้า backend"""
    route = backends.post(RERANK_URL).mock(
        return_value=httpx.Response(200, json=RERANK_REPLY)
    )
    response = client.post(
        "/v1/rerank",
        headers=auth(member_key),
        json={
            "model": "rerank",
            "query": "ใครเป็นคนเขียน",
            "documents": ["the other one", "the matching one"],
            "top_n": 2,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["model"] == "rerank"
    assert [r["index"] for r in body["results"]] == [1, 0]
    assert body["results"][0]["relevance_score"] == 0.98
    sent = json.loads(route.calls[0].request.content)
    assert sent["query"] == "ใครเป็นคนเขียน"
    assert sent["top_n"] == 2


# ---------------------------------------------------------------------------
# Capability routing: an embedding request must never land on a chat server
# ---------------------------------------------------------------------------
def test_an_embeddings_request_to_a_chat_model_is_refused_before_any_backend_is_called(
    retrieval_config, client, member_key, backends
):
    chat = backends.post(CHAT_URL).mock(return_value=httpx.Response(200, json={}))

    response = client.post(
        "/v1/embeddings",
        headers=auth(member_key),
        json={"model": "coding", "input": "hello"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "PROTOCOL_NOT_SUPPORTED"
    assert not chat.called, "คำขอ embedding ต้องไม่หลุดไปถึง backend ที่เป็น chat"


def test_a_chat_request_to_an_embedding_model_is_refused(
    retrieval_config, client, member_key
):
    response = client.post(
        "/v1/chat/completions",
        headers=auth(member_key),
        json={"model": "embed", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "PROTOCOL_NOT_SUPPORTED"
    # ข้อความต้องบอกว่าไปทางไหนได้ ไม่ใช่บอกว่า "none"
    assert "embeddings" in response.json()["error"]["message"]


def test_a_rerank_model_does_not_answer_embeddings(
    retrieval_config, client, member_key, backends
):
    """สองความสามารถนี้แยกกัน · reranker ไม่คืนเวกเตอร์ และ bundle ก็บอกไว้แบบนั้น"""
    scorer = backends.post(RERANK_URL).mock(return_value=httpx.Response(200, json={}))
    response = client.post(
        "/v1/embeddings",
        headers=auth(member_key),
        json={"model": "rerank", "input": "hello"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "PROTOCOL_NOT_SUPPORTED"
    assert not scorer.called


def test_the_registry_refuses_a_surface_no_backend_can_serve():
    """ประกาศ protocols.embeddings โดยไม่มี endpoint ที่เสิร์ฟ = ล้มตั้งแต่โหลด"""
    from pydantic import ValidationError

    from app.registry.schema import ModelDefinition

    document = {
        "metadata": {"alias": "broken", "display_name": "Broken"},
        "spec": {
            "upstream_model": "x/y",
            "limits": {"context_tokens": 1024},
            "capabilities": {"chat": False, "embedding": True},
            "protocols": {"openai": False, "embeddings": True},
            "endpoints": [
                {
                    "name": "chat-only",
                    "server_type": "vllm",
                    "base_url": "http://dgx03:8000",
                    "protocols": {"openai": True},
                }
            ],
        },
    }
    with pytest.raises(ValidationError, match="no enabled endpoint serves /v1/embeddings"):
        ModelDefinition.model_validate(document)


def test_the_registry_refuses_an_embedding_surface_on_a_model_that_cannot_embed():
    from pydantic import ValidationError

    from app.registry.schema import ModelDefinition

    document = {
        "metadata": {"alias": "broken", "display_name": "Broken"},
        "spec": {
            "upstream_model": "x/y",
            "limits": {"context_tokens": 1024},
            "protocols": {"openai": False, "embeddings": True},
            "endpoints": [
                {
                    "name": "e",
                    "server_type": "vllm",
                    "base_url": "http://dgx05:8000",
                    "protocols": {"openai": False, "embeddings": True},
                }
            ],
        },
    }
    with pytest.raises(ValidationError, match="requires capabilities.embedding=true"):
        ModelDefinition.model_validate(document)


# ---------------------------------------------------------------------------
# The console must be able to edit these models without erasing them
# ---------------------------------------------------------------------------
def test_the_admin_registry_view_exposes_the_retrieval_flags(retrieval_config, client):
    """คอนโซลอ่านสี่ช่องนี้เพื่อส่งกลับตอน Save — ไม่มีให้อ่าน = ส่งกลับไม่ได้"""
    data = client.get("/admin/models", headers=auth(client.admin_key)).json()["data"]
    embed = next(m for m in data if m["alias"] == "embed")

    assert embed["capabilities"]["embedding"] is True
    assert embed["protocols"]["embeddings"] is True
    assert embed["endpoints"][0]["protocols"]["embeddings"] is True
    assert "rerank" in embed["capabilities"]


def _console_document(model: dict, *, protocols: dict, capabilities: dict) -> dict:
    """เอกสารแบบที่คอนโซลประกอบส่งกลับ (ดู editorValues ใน app/static/app.js)"""
    return {
        "apiVersion": "litegate.dev/v1",
        "kind": "Model",
        "metadata": {
            "alias": model["alias"],
            "display_name": "Renamed In The Console",
            "description": model["description"],
            "visibility": model["visibility"],
            "tags": model["tags"],
        },
        "spec": {
            "upstream_model": model["upstream_model"],
            "purpose": model["purpose"],
            "limits": model["limits"],
            "modalities": {"input": ["text"], "output": ["text"]},
            "capabilities": capabilities,
            "protocols": protocols,
            "endpoints": [
                {
                    "name": e["name"],
                    "server_type": e["server_type"],
                    "base_url": e["base_url"],
                    "priority": e["priority"],
                    "weight": e["weight"],
                    "max_concurrency": e["max_concurrency"],
                    "health_path": e["health_path"],
                    "protocols": e["protocols"],
                    "modalities": e["modalities"],
                    "enabled": e["enabled"],
                }
                for e in model["endpoints"]
            ],
            "enabled": True,
        },
    }


def test_a_console_save_that_drops_the_surface_is_refused_not_written(
    retrieval_config, client
):
    """ฟอร์มที่ส่ง protocols กลับมาไม่ครบเคยลบ surface ทิ้งเงียบ ๆ — ต้องดังแทน

    เอกสารนี้คือสิ่งที่คอนโซลเคยส่งหลังคนแก้แค่ชื่อที่แสดง: ช่องที่ฟอร์มไม่มี
    (embeddings) หายไป และช่องที่มี (openai/anthropic) เป็น false อยู่แล้วสำหรับ
    โมเดล embedding · ผลคือ alias ที่ไม่เหลือ surface ให้เรียกเลย
    """
    admin = auth(client.admin_key)
    model = next(
        m for m in client.get("/admin/models", headers=admin).json()["data"]
        if m["alias"] == "embed"
    )
    stripped = _console_document(
        model,
        protocols={"openai": False, "anthropic": False},
        capabilities={
            "chat": False, "vision": False, "tools": False, "streaming": False,
            "coding": False, "reasoning": False, "agentic": False,
        },
    )

    response = client.post("/admin/models", headers=admin, json=stripped)

    assert response.status_code == 400
    assert "no protocol is enabled" in json.dumps(response.json())


def test_a_console_save_that_carries_the_surface_through_keeps_it(
    retrieval_config, client, member_key, backends
):
    """เอกสารที่พา flag ที่ฟอร์มไม่แสดงไปด้วย ต้องบันทึกแล้วยังเสิร์ฟได้เหมือนเดิม"""
    admin = auth(client.admin_key)
    model = next(
        m for m in client.get("/admin/models", headers=admin).json()["data"]
        if m["alias"] == "embed"
    )
    carried = _console_document(
        model,
        protocols={**model["protocols"], "openai": False, "anthropic": False},
        capabilities={
            **model["capabilities"],
            "chat": False, "vision": False, "tools": False, "streaming": False,
            "coding": False, "reasoning": False, "agentic": False,
        },
    )

    assert client.post("/admin/models", headers=admin, json=carried).status_code == 201
    client.post("/admin/registry/reload", headers=admin)

    backends.post(EMBED_PRIMARY).mock(return_value=httpx.Response(200, json=EMBED_REPLY))
    served = client.post(
        "/v1/embeddings",
        headers=auth(member_key),
        json={"model": "embed", "input": "still here"},
    )
    assert served.status_code == 200


# ---------------------------------------------------------------------------
# Token accounting
# ---------------------------------------------------------------------------
def test_rerank_charges_the_query_once_per_document():
    """cross-encoder รัน (query + doc) ใหม่ทุกชิ้น — นับ query ครั้งเดียวคือคิดขาด"""
    query = "ก" * 320
    documents = ["ข" * 320] * 4

    profile = profile_rerank_request(
        {"model": "rerank", "query": query, "documents": documents}
    )

    # 320 ตัวอักษรของ query × 4 เอกสาร + 4×320 ของเอกสารเอง = 2,560 ตัวอักษร
    assert profile.text_chars == 2560
    assert profile.batch_items == 4
    assert estimate_text_tokens(profile) == 800
    # การนับแบบ "prompt เดียว" จะได้ (320 + 1280)/3.2 = 500 — ต่ำกว่าของจริง 37%
    assert estimate_text_tokens(profile) > (320 + 4 * 320) / 3.2


def test_pre_tokenized_input_is_counted_exactly_not_as_zero():
    """ส่ง token id มาแทนข้อความ = ไม่มีอักขระให้ประมาณ · ไม่นับ = ใช้ GPU ฟรี"""
    profile = profile_embeddings_request({"model": "embed", "input": list(range(1000))})

    assert profile.text_chars == 0
    assert profile.pretokenized_tokens == 1000
    assert profile.batch_items == 1
    assert estimate_text_tokens(profile) == 1000


def test_a_batch_of_pre_tokenized_sequences_is_counted_per_sequence():
    profile = profile_embeddings_request(
        {"model": "embed", "input": [[1, 2, 3], [4, 5], [6]]}
    )
    assert profile.batch_items == 3
    assert profile.pretokenized_tokens == 6
    assert profile.largest_item_tokens == 3


def test_an_embedding_usage_row_records_the_backend_figure_and_no_output_tokens(
    retrieval_config, client, member_key, backends
):
    backends.post(EMBED_PRIMARY).mock(return_value=httpx.Response(200, json=EMBED_REPLY))
    client.post(
        "/v1/embeddings",
        headers=auth(member_key),
        json={"model": "embed", "input": ["a", "b", "c"]},
    )

    row = next(r for r in usage_rows(client) if r["model_alias"] == "embed")
    assert row["text_input_tokens"] == 4242      # what the backend measured
    assert row["visual_input_tokens"] == 0       # structurally impossible here
    assert row["output_tokens"] == 0             # there is no output to bill
    assert row["total_tokens"] == 4242
    assert row["token_accounting"] == "upstream"
    assert row["protocol"] == "embeddings"
    assert row["request_modality"] == "text"
    assert row["stream"] is False
    assert row["ttft_ms"] is None                # no first token to time
    assert row["endpoint_name"] == "primary"


def test_a_rerank_usage_row_keeps_the_backend_total_instead_of_our_estimate(
    retrieval_config, client, member_key, backends
):
    """vLLM ส่ง usage มาแต่ `total_tokens` · การอ่านแบบ chat จะทิ้งตัวเลขนั้นไปเงียบ ๆ"""
    backends.post(RERANK_URL).mock(return_value=httpx.Response(200, json=RERANK_REPLY))
    client.post(
        "/v1/rerank",
        headers=auth(member_key),
        json={"model": "rerank", "query": "q", "documents": ["a", "b"]},
    )

    row = next(r for r in usage_rows(client) if r["model_alias"] == "rerank")
    assert row["text_input_tokens"] == 777
    assert row["token_accounting"] == "upstream"
    assert row["protocol"] == "rerank"
    assert row["output_tokens"] == 0


def test_a_backend_that_reports_no_usage_falls_back_to_our_estimate(
    retrieval_config, client, member_key, backends
):
    silent = dict(EMBED_REPLY)
    silent.pop("usage")
    backends.post(EMBED_PRIMARY).mock(return_value=httpx.Response(200, json=silent))

    client.post(
        "/v1/embeddings",
        headers=auth(member_key),
        json={"model": "embed", "input": list(range(5000))},
    )

    row = next(r for r in usage_rows(client) if r["model_alias"] == "embed")
    assert row["text_input_tokens"] == 5000
    assert row["token_accounting"] == "estimated"


def test_retrieval_traffic_does_not_distort_the_chat_numbers_in_reports(
    retrieval_config, client, member_key, backends
):
    """`output_tokens: 0` ต้องไม่ไปถ่วงค่าเฉลี่ยของโมเดล chat

    เป็นไปได้เพราะรายงานจัดกลุ่มด้วย `model_alias` และโมเดลค้นคืนเป็น alias ของ
    ตัวเอง · ค่า `avg(ttft_ms)` ก็ไม่โดนเพราะ SQL ข้าม NULL ให้อยู่แล้ว —
    นี่คือเหตุผลที่ `usage_logs` ไม่ต้องเพิ่มคอลัมน์ไหนเลย
    """
    backends.post(EMBED_PRIMARY).mock(return_value=httpx.Response(200, json=EMBED_REPLY))
    backends.post(CHAT_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "c1", "object": "chat.completion", "model": "m",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )
    )
    client.post(
        "/v1/chat/completions",
        headers=auth(member_key),
        json={"model": "coding", "messages": [{"role": "user", "content": "hi"}]},
    )
    client.post(
        "/v1/embeddings", headers=auth(member_key), json={"model": "embed", "input": "x"}
    )

    summary = client.get(
        "/admin/usage/summary?days=1", headers=auth(client.admin_key)
    ).json()["by_model"]
    by_alias = {row["model"]: row for row in summary}

    assert by_alias["coding"]["output_tokens"] == 5
    assert by_alias["embed"]["output_tokens"] == 0
    assert by_alias["embed"]["text_input_tokens"] == 4242
    assert by_alias["embed"]["images"] == 0
    # ไม่มี first token ให้จับเวลา — ต้องเป็น None ไม่ใช่ 0 ที่ดูเหมือนวัดได้ว่าเร็วมาก
    assert by_alias["embed"]["avg_ttft_ms"] is None


# ---------------------------------------------------------------------------
# Quota
# ---------------------------------------------------------------------------
def test_an_embeddings_call_spends_the_caller_s_quota(
    retrieval_config, client, member_key, backends
):
    backends.post(EMBED_PRIMARY).mock(return_value=httpx.Response(200, json=EMBED_REPLY))
    client.post(
        "/v1/embeddings",
        headers=auth(member_key),
        json={"model": "embed", "input": ["a", "b", "c", "d", "e"]},
    )

    used = client.get("/v1/me", headers=auth(member_key)).json()["quota"]["used"]
    # ห้าสตริงในคำขอเดียว = **หนึ่ง** คำขอ · มิติ token คือตัวที่สะท้อนขนาดของงาน
    # นับเป็นห้าเมื่อไหร่ max_requests จะไร้ความหมายสำหรับการ index เอกสารทั้งกอง
    assert used["requests"] == 1
    assert used["text_input_tokens"] == 4242
    assert used["output_tokens"] == 0
    assert used["images"] == 0


def test_an_exhausted_quota_refuses_embeddings_before_the_backend_is_touched(
    retrieval_config, client, member_key, backends
):
    route = backends.post(EMBED_PRIMARY).mock(
        return_value=httpx.Response(200, json=EMBED_REPLY)
    )
    admin = auth(client.admin_key)
    users = client.get("/admin/users", headers=admin).json()["data"]
    member = next(u for u in users if u["role"] == "member")
    client.post(
        "/admin/quota-policies",
        headers=admin,
        json={
            "name": "tiny", "user_id": member["id"], "window": "day",
            "max_requests": 1, "max_input_tokens": 1_000_000,
            "max_output_tokens": 1_000_000, "max_images": 100,
        },
    )

    body = {"model": "embed", "input": "หนึ่ง"}
    assert client.post("/v1/embeddings", headers=auth(member_key), json=body).status_code == 200
    second = client.post("/v1/embeddings", headers=auth(member_key), json=body)

    assert second.status_code == 429
    assert second.json()["error"]["code"] == "QUOTA_EXCEEDED"
    assert route.call_count == 1, "คำขอที่สองต้องถูกปฏิเสธก่อนถึง backend"


# ---------------------------------------------------------------------------
# Context window: per item, never the sum
# ---------------------------------------------------------------------------
def test_a_large_batch_of_short_items_is_accepted(
    retrieval_config, client, member_key, backends
):
    """ผลรวมของ batch เกิน window ได้ตามปกติ — backend รันทีละชิ้น"""
    backends.post(EMBED_PRIMARY).mock(return_value=httpx.Response(200, json=EMBED_REPLY))
    # 500 ชิ้น × ~312 token = ~156,000 token รวม เทียบกับ window 32,768
    response = client.post(
        "/v1/embeddings",
        headers=auth(member_key),
        json={"model": "embed", "input": ["ก" * 1000] * 500},
    )
    assert response.status_code == 200


def test_one_item_longer_than_the_window_is_refused_without_quoting_it(
    retrieval_config, client, member_key, backends
):
    route = backends.post(EMBED_PRIMARY).mock(
        return_value=httpx.Response(200, json=EMBED_REPLY)
    )
    secret = "PRIVATEPHRASE"
    response = client.post(
        "/v1/embeddings",
        headers=auth(member_key),
        json={"model": "embed", "input": ["ok", secret * 20_000]},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "CONTEXT_LENGTH_EXCEEDED"
    assert not route.called
    # PDPA (PRD FR-28): ข้อความของผู้ใช้ต้องไม่โผล่ในคำตอบที่เป็น error
    assert secret not in response.text
    details = response.json()["error"]["details"]
    assert details["context_tokens"] == 32768
    assert details["items"] == 2


# ---------------------------------------------------------------------------
# Failover: across machines yes, across models never
# ---------------------------------------------------------------------------
def test_a_sick_machine_fails_over_to_another_holding_the_same_weights(
    retrieval_config, client, member_key, backends
):
    backends.post(EMBED_PRIMARY).mock(return_value=httpx.Response(503, text="down"))
    standby = backends.post(EMBED_STANDBY).mock(
        return_value=httpx.Response(200, json=EMBED_REPLY)
    )

    response = client.post(
        "/v1/embeddings",
        headers=auth(member_key),
        json={"model": "embed", "input": "hello"},
    )

    assert response.status_code == 200
    assert standby.called
    assert "primary" in response.headers["x-litegate-failed-over"]


def test_embeddings_never_fall_back_to_a_different_model(
    retrieval_config, client, member_key, backends
):
    """เวกเตอร์จากคนละโมเดลอยู่คนละปริภูมิ · "สำเร็จ" ด้วยรุ่นอื่นคือทำดัชนีพังเงียบ ๆ

    `embed` ประกาศ routing.fallback: [embed-alt] ไว้จริง ๆ และ chat จะใช้มัน —
    เส้นทางนี้ต้องไม่ใช้ แล้วตอบ error ให้ผู้เรียกรู้ตัวแทน
    """
    backends.post(EMBED_PRIMARY).mock(return_value=httpx.Response(503, text="down"))
    backends.post(EMBED_STANDBY).mock(return_value=httpx.Response(503, text="down"))
    alt = backends.post(EMBED_ALT).mock(return_value=httpx.Response(200, json=EMBED_REPLY))

    response = client.post(
        "/v1/embeddings",
        headers=auth(member_key),
        json={"model": "embed", "input": "hello"},
    )

    assert response.status_code >= 500
    assert not alt.called, "ห้ามเปลี่ยนโมเดลเพื่อให้คำขอสำเร็จ"


def test_a_failed_request_is_still_recorded(
    retrieval_config, client, member_key, backends
):
    backends.post(EMBED_PRIMARY).mock(return_value=httpx.Response(503, text="down"))
    backends.post(EMBED_STANDBY).mock(return_value=httpx.Response(503, text="down"))
    client.post(
        "/v1/embeddings",
        headers=auth(member_key),
        json={"model": "embed", "input": "hello"},
    )

    row = next(r for r in usage_rows(client) if r["model_alias"] == "embed")
    assert row["status"] == "error"
    assert row["output_tokens"] == 0


# ---------------------------------------------------------------------------
# Request validation — messages name positions, never content
# ---------------------------------------------------------------------------
def test_embeddings_requires_an_input(retrieval_config, client, member_key):
    response = client.post(
        "/v1/embeddings", headers=auth(member_key), json={"model": "embed"}
    )
    assert response.status_code == 400
    assert response.json()["error"]["param"] == "input"


def test_embeddings_rejects_an_unusable_item_by_position_only(
    retrieval_config, client, member_key
):
    response = client.post(
        "/v1/embeddings",
        headers=auth(member_key),
        json={"model": "embed", "input": ["fine", {"nested": "CONFIDENTIAL"}]},
    )
    assert response.status_code == 400
    assert response.json()["error"]["param"] == "input[1]"
    assert "CONFIDENTIAL" not in response.text


def test_rerank_requires_a_query_and_documents(retrieval_config, client, member_key):
    missing_query = client.post(
        "/v1/rerank",
        headers=auth(member_key),
        json={"model": "rerank", "documents": ["a"]},
    )
    assert missing_query.status_code == 400
    assert missing_query.json()["error"]["param"] == "query"

    empty_documents = client.post(
        "/v1/rerank",
        headers=auth(member_key),
        json={"model": "rerank", "query": "q", "documents": []},
    )
    assert empty_documents.status_code == 400
    assert empty_documents.json()["error"]["param"] == "documents"


def test_rerank_rejects_document_objects_that_the_backend_cannot_read(
    retrieval_config, client, member_key
):
    """Cohere ยอมให้ส่ง object ได้ แต่ vLLM รับแค่สตริง — ปฏิเสธเองดีกว่าปล่อยไปตาย"""
    response = client.post(
        "/v1/rerank",
        headers=auth(member_key),
        json={
            "model": "rerank",
            "query": "q",
            "documents": ["ok", {"text": "SENSITIVE"}],
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["param"] == "documents[1]"
    assert "SENSITIVE" not in response.text


# ---------------------------------------------------------------------------
# Access control is the same one that gates chat
# ---------------------------------------------------------------------------
def test_embeddings_needs_a_key(retrieval_config, client):
    response = client.post("/v1/embeddings", json={"model": "embed", "input": "x"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "MISSING_API_KEY"


def test_a_key_scoped_to_other_models_cannot_call_embeddings(
    retrieval_config, client, backends
):
    admin = auth(client.admin_key)
    user = client.post(
        "/admin/users",
        headers=admin,
        json={"external_id": "6499999999", "display_name": "Scoped", "role": "member"},
    ).json()
    scoped = client.post(
        "/admin/api-keys",
        headers=admin,
        json={"user_id": user["id"], "name": "chat only", "models": ["coding"]},
    ).json()["api_key"]

    route = backends.post(EMBED_PRIMARY).mock(
        return_value=httpx.Response(200, json=EMBED_REPLY)
    )
    response = client.post(
        "/v1/embeddings",
        headers=auth(scoped),
        json={"model": "embed", "input": "x"},
    )

    assert response.status_code == 403
    assert not route.called


# ---------------------------------------------------------------------------
# Discoverability
# ---------------------------------------------------------------------------
def test_the_catalogue_says_which_surface_a_retrieval_model_answers_on(
    retrieval_config, client, member_key
):
    models = client.get("/v1/models", headers=auth(member_key)).json()["data"]
    embed = next(m for m in models if m["id"] == "embed")
    rerank = next(m for m in models if m["id"] == "rerank")

    assert embed["protocols"] == ["embeddings"]
    assert rerank["protocols"] == ["rerank"]
    assert "Embedding" in embed["badges"]
    assert "Rerank" in rerank["badges"]

    catalog = client.get("/v1/catalog", headers=auth(member_key)).json()
    titles = {section["title"] for section in catalog["sections"]}
    assert {"Embedding", "Rerank"} <= titles


# ---------------------------------------------------------------------------
# เพดานของ batch — โควตาตรวจก่อนแล้วค่อยบันทึก คำขอเดียวจึงทะลุได้ถ้าไม่มีเพดาน
# ---------------------------------------------------------------------------
def test_an_oversized_batch_is_refused_before_it_can_spend_the_whole_quota(
    retrieval_config, client, member_key, backends, monkeypatch
):
    """เส้นทาง chat ถูกคุมขนาดคำขอเดียวโดยโครงสร้างด้วย `context_tokens` แต่ batch ไม่มี ·
    โควตาเป็นแบบตรวจ *ก่อน* แล้วค่อยบันทึก คำขอเดียวที่มีแสนเอกสารจึงใช้โควตาทั้งเดือน
    หมดในนัดเดียว แล้วค่อยโดนกันที่คำขอ *ถัดไป* ซึ่งสายไปแล้ว"""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "max_batch_items", 3, raising=False)
    route = backends.post(EMBED_PRIMARY).mock(
        return_value=httpx.Response(200, json=EMBED_REPLY)
    )

    response = client.post(
        "/v1/embeddings",
        headers=auth(member_key),
        json={"model": "embed", "input": ["a", "b", "c", "d"]},
    )

    assert response.status_code == 400
    assert not route.called, "ต้องปฏิเสธก่อนถึง backend ไม่ใช่ให้เครื่องทำงานแล้วค่อยเสียใจ"
    body = response.json()["error"]
    # ข้อความต้องบอกทั้งจำนวนที่ส่งมา เพดาน และทางออก — ไม่ใช่แค่ "invalid request"
    assert "4" in body["message"] and "3" in body["message"]
    assert "split" in body["message"].lower()
    assert body.get("param") == "input"


def test_a_batch_at_the_limit_still_goes_through(
    retrieval_config, client, member_key, backends, monkeypatch
):
    """เพดานที่กันของที่ *เท่ากับ* เพดานคือเพดานที่ตั้งผิดไปหนึ่ง — เคสคลาสสิก off-by-one"""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "max_batch_items", 3, raising=False)
    route = backends.post(EMBED_PRIMARY).mock(
        return_value=httpx.Response(200, json=EMBED_REPLY)
    )

    response = client.post(
        "/v1/embeddings",
        headers=auth(member_key),
        json={"model": "embed", "input": ["a", "b", "c"]},
    )

    assert response.status_code == 200 and route.called


def test_the_rerank_document_list_has_the_same_ceiling(
    retrieval_config, client, member_key, backends, monkeypatch
):
    """rerank คูณ query ด้วยจำนวนเอกสาร — batch ใหญ่ที่นี่แพงกว่า embeddings ต่อชิ้นอีก"""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "max_batch_items", 2, raising=False)
    route = backends.post(RERANK_URL).mock(
        return_value=httpx.Response(200, json=RERANK_REPLY)
    )

    response = client.post(
        "/v1/rerank",
        headers=auth(member_key),
        json={"model": "rerank", "query": "q", "documents": ["a", "b", "c"]},
    )

    assert response.status_code == 400 and not route.called
    assert response.json()["error"].get("param") == "documents"


def test_the_ceiling_can_be_switched_off_for_a_site_that_wants_no_limit(
    retrieval_config, client, member_key, backends, monkeypatch
):
    """ไซต์ที่รันคนเดียวและรู้ว่าตัวเองทำอะไรอยู่ต้องปิดได้ · 0 = ไม่จำกัด
    ไม่ใช่ 'เพดานเป็นศูนย์' ซึ่งจะแปลว่าปฏิเสธทุกคำขอ"""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "max_batch_items", 0, raising=False)
    route = backends.post(EMBED_PRIMARY).mock(
        return_value=httpx.Response(200, json=EMBED_REPLY)
    )

    response = client.post(
        "/v1/embeddings",
        headers=auth(member_key),
        json={"model": "embed", "input": [f"doc {i}" for i in range(5000)]},
    )

    assert response.status_code == 200 and route.called
