"""คำขอของ /v1/embeddings และ /v1/rerank — อ่านรูปร่าง ตรวจ แล้วบอกว่าต้องคิดเท่าไร

ทำไมไม่ใช้ `profile_openai_request` ซ้ำ
--------------------------------------
สองเส้นทางนี้ไม่มี `messages` ไม่มี content block ไม่มีภาพ และ **ไม่มี output token**
แต่สิ่งที่ต่างจริง ๆ ไม่ใช่รูปร่าง JSON — คือ *หนึ่งคำขอเท่ากับหลายงานที่ backend รัน
แยกกัน*:

  * embedding: หนึ่ง forward pass ต่อสตริงหนึ่งตัวใน `input`
  * rerank:    หนึ่ง forward pass ต่อเอกสารหนึ่งชิ้น และ **มี query ต่อหัวทุกครั้ง**

ข้อสองคือจุดที่การคัดลอกการนับของ chat มาใช้จะผิดมากที่สุด · คำขอ rerank ที่มี query
200 token กับเอกสาร 50 ชิ้น เผา query ไป 10,000 token ไม่ใช่ 200 — ถ้านับแบบ "prompt
เดียว" ลูกค้าจะจ่ายน้อยกว่าที่เครื่องทำงานจริงหลายสิบเท่า และ quota engine จะกันอะไร
ไม่ได้เลยบนเส้นทางที่หนัก GPU ที่สุดของ RAG

เรื่องความเป็นส่วนตัว (PRD FR-28)
---------------------------------
คำขอ embedding คือข้อความของผู้ใช้ทั้งดุ้น · **ห้ามให้เนื้อหาหลุดออกจากไฟล์นี้**
ข้อผิดพลาดทุกอันในนี้อ้างได้แค่ *ตำแหน่ง* (`input[3]`) กับ *ตัวเลข* — ไม่มีบรรทัดไหน
ใส่เนื้อหาลงใน message, `details`, หรือ log และห้ามมีในอนาคตด้วย
"""

from __future__ import annotations

from typing import Any

from app.core.errors import ErrorCode, GatewayError
from app.core.multimodal import RequestProfile
from app.core.tokens import CHARS_PER_TOKEN

# เส้นทางที่ยิงไปหา backend · ตรงกับที่ bundle ของ LMDS บอกว่าเสิร์ฟจริง
# (bundles/qwen3-embedding-8b, bundles/qwen3-reranker-4b → MODEL_PROFILE.yaml)
UPSTREAM_EMBEDDINGS_PATH = "/v1/embeddings"
UPSTREAM_RERANK_PATH = "/v1/rerank"


def _tokens(chars: int = 0, token_ids: int = 0) -> int:
    """ต้นทุนของงานย่อยหนึ่งชิ้น · token id นับเป๊ะ ส่วนอักขระต้องประมาณ"""
    return int(chars / CHARS_PER_TOKEN) + token_ids


def _is_int(value: Any) -> bool:
    # bool เป็นลูกของ int ใน Python — `[True, False]` ไม่ใช่ token id ของใคร
    return isinstance(value, int) and not isinstance(value, bool)


def profile_embeddings_request(body: dict[str, Any]) -> RequestProfile:
    """ตรวจ body ของ /v1/embeddings · ไม่แก้ body (เกตเวย์ส่งต่อของเดิม)

    `input` รับได้สี่แบบตามสเปกของ OpenAI ซึ่ง vLLM ทำตาม:
    สตริงเดียว · อาร์เรย์ของสตริง · อาร์เรย์ของ token id · อาร์เรย์ของอาร์เรย์ token id

    สองแบบหลังสำคัญกว่าที่เห็น: ผู้เรียกที่ tokenize เองแล้วส่ง id มา ทำให้การนับจาก
    จำนวนอักขระได้ 0 — คือใช้ GPU ฟรีโดยไม่ต้องตั้งใจโกง · จึงนับ id ตรง ๆ
    """
    profile = RequestProfile()
    raw = body.get("input")

    if raw is None:
        raise GatewayError(
            ErrorCode.INVALID_REQUEST, "'input' is required.", param="input"
        )

    # (จำนวนอักขระ, จำนวน token id) ต่อหนึ่งงานย่อย
    items: list[tuple[int, int]] = []

    if isinstance(raw, str):
        if not raw:
            raise GatewayError(
                ErrorCode.INVALID_REQUEST, "'input' must not be empty.", param="input"
            )
        items.append((len(raw), 0))
    elif isinstance(raw, list):
        if not raw:
            raise GatewayError(
                ErrorCode.INVALID_REQUEST,
                "'input' must not be an empty array.",
                param="input",
            )
        # อาร์เรย์ของ int ล้วน = *หนึ่ง* ลำดับที่ tokenize มาแล้ว ไม่ใช่หลายงานย่อย
        if all(_is_int(x) for x in raw):
            items.append((0, len(raw)))
        else:
            for idx, item in enumerate(raw):
                if isinstance(item, str):
                    items.append((len(item), 0))
                elif isinstance(item, list) and all(_is_int(x) for x in item):
                    if not item:
                        raise GatewayError(
                            ErrorCode.INVALID_REQUEST,
                            f"input[{idx}] must not be an empty array.",
                            param=f"input[{idx}]",
                        )
                    items.append((0, len(item)))
                else:
                    raise GatewayError(
                        ErrorCode.INVALID_REQUEST,
                        f"input[{idx}] must be a string or an array of token ids.",
                        param=f"input[{idx}]",
                    )
    else:
        raise GatewayError(
            ErrorCode.INVALID_REQUEST,
            "'input' must be a string, an array of strings, or token ids.",
            param="input",
        )

    profile.text_chars = sum(chars for chars, _ in items)
    profile.pretokenized_tokens = sum(ids for _, ids in items)
    profile.batch_items = len(items)
    profile.largest_item_tokens = max(_tokens(chars, ids) for chars, ids in items)
    return profile


def profile_rerank_request(body: dict[str, Any]) -> RequestProfile:
    """ตรวจ body ของ /v1/rerank (รูปแบบ Cohere/Jina ที่ vLLM เสิร์ฟจริง)

    **query ถูกนับซ้ำตามจำนวนเอกสาร** และนี่คือหัวใจของไฟล์นี้ · cross-encoder ไม่ได้
    อ่าน query ครั้งเดียวแล้วเทียบกับทุกเอกสาร — มันรันใหม่ทั้ง (query + doc_i) ทีละคู่
    ตัวเลขที่ได้จึงตรงกับ `usage.prompt_tokens` ที่ vLLM รายงานกลับมาเอง ไม่ใช่สูตรที่
    เราคิดขึ้น · ผลพลอยได้คือเวลา backend ไม่รายงาน usage มา ค่าประมาณของเราก็ยังอยู่
    ใกล้ของจริง แทนที่จะต่ำกว่าหลายสิบเท่า
    """
    profile = RequestProfile()

    query = body.get("query")
    if not isinstance(query, str) or not query:
        raise GatewayError(
            ErrorCode.INVALID_REQUEST,
            "'query' is required and must be a non-empty string.",
            param="query",
        )

    documents = body.get("documents")
    if not isinstance(documents, list) or not documents:
        raise GatewayError(
            ErrorCode.INVALID_REQUEST,
            "'documents' is required and must be a non-empty array of strings.",
            param="documents",
        )
    for idx, document in enumerate(documents):
        if not isinstance(document, str):
            # Cohere ยอมให้ส่ง object ได้ด้วย แต่ vLLM รับแค่สตริง (ดู docs/API.md)
            # ปฏิเสธที่นี่พร้อมบอกว่าต้องการอะไร ดีกว่าส่งต่อไปให้ backend ตอบ 400
            # ด้วยภาษาของ pydantic ที่อาจพ่นค่าที่ส่งมากลับออกมาด้วย
            raise GatewayError(
                ErrorCode.INVALID_REQUEST,
                f"documents[{idx}] must be a string.",
                param=f"documents[{idx}]",
            )

    top_n = body.get("top_n")
    if top_n is not None and (not _is_int(top_n) or top_n < 1):
        raise GatewayError(
            ErrorCode.INVALID_REQUEST,
            "'top_n' must be a positive integer.",
            param="top_n",
        )

    query_chars = len(query)
    profile.text_chars = query_chars * len(documents) + sum(len(d) for d in documents)
    profile.batch_items = len(documents)
    profile.largest_item_tokens = _tokens(query_chars + max(len(d) for d in documents))
    return profile


def rewrite_model_name(data: dict[str, Any], alias: str) -> None:
    """สมาชิกขอ alias ไหนต้องเห็น alias นั้น — ชื่อ repo ต้นทางไม่เคยหลุด (PRD §6)"""
    if isinstance(data, dict):
        data["model"] = alias
