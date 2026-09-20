"""orjson เป็นของเสริม — มีหรือไม่มี ผลลัพธ์ต้องเหมือนกันทุกไบต์

เทสนี้มีไว้กันอาการเดียว: ลูกค้าที่ลง `litegate[speed]` ได้คำตอบหน้าตาไม่เหมือนลูกค้า
ที่ไม่ได้ลง · ถ้าเกิดขึ้นจริงมันจะเป็นบั๊กที่ reproduce ไม่ได้ เพราะเครื่องคนรายงานกับ
เครื่องคนแก้ลง extra ไม่เหมือนกัน

ทุกเคสจึงรันสองรอบ: รอบที่ใช้ orjson (ถ้าลงไว้) และรอบที่บังคับให้ตกไปใช้ stdlib
"""

from __future__ import annotations

import json

import pytest

from app.core import jsonio

# ของที่เกตเวย์ส่งจริง — ไทยปนอังกฤษ, escape, ตัวเลข, null, โครงซ้อน, ของว่าง
CORPUS = [
    {},
    [],
    {"a": 1, "b": [1, 2, 3], "c": None, "d": True, "e": 1.5, "f": -0.0},
    {"content": "ช่วยเขียนฟังก์ชัน Python หน่อยครับ · 「テスト」 · emoji 🙂"},
    {"escaped": 'a"b\\c' + chr(10) + chr(9) + chr(13) + chr(0) + chr(27)},
    {"deep": {"x": {"y": {"z": [{"k": "v"}, 2, None]}}}},
    {"id": "chatcmpl-x", "choices": [{"index": 0, "delta": {"content": "ก"}}]},
    {"usage": {"prompt_tokens": 812, "completion_tokens": 4096}},
    {"big": "ยาว " * 5000},
    {1: "int key", 2.5: "float key"},  # stdlib แปลงเป็นสตริงให้ — orjson ต้องทำเหมือนกัน
]


@pytest.fixture(params=["as-installed", "stdlib-fallback"])
def mode(request, monkeypatch):
    """บังคับให้เดินทั้งสองทางของ jsonio ในเทสชุดเดียวกัน"""
    if request.param == "stdlib-fallback":
        monkeypatch.setattr(jsonio, "HAS_ORJSON", False)
    return request.param


@pytest.mark.parametrize("value", CORPUS, ids=range(len(CORPUS)))
def test_output_is_byte_identical_to_stdlib(value, mode):
    expected = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert jsonio.dumpb(value) == expected
    assert jsonio.dumps(value) == expected.decode("utf-8")


@pytest.mark.parametrize("value", CORPUS, ids=range(len(CORPUS)))
def test_round_trip_preserves_the_value(value, mode):
    assert jsonio.loads(jsonio.dumpb(value)) == json.loads(json.dumps(value))


def test_canonical_sorts_keys_so_the_same_request_gets_the_same_cache_key(mode):
    a = jsonio.canonical({"b": 1, "a": {"d": 4, "c": 3}})
    b = jsonio.canonical({"a": {"c": 3, "d": 4}, "b": 1})
    assert a == b == b'{"a":{"c":3,"d":4},"b":1}'


def test_unserialisable_values_go_through_default(mode):
    class Thing:
        def __repr__(self) -> str:
            return "thing"

    assert jsonio.dumps({"x": Thing()}, default=str) == '{"x":"thing"}'


def test_a_broken_body_raises_the_error_callers_already_catch(mode):
    """โค้ดที่ดัก json.JSONDecodeError อยู่แล้วต้องยังดักได้ทั้งสองทาง

    orjson.JSONDecodeError สืบทอดจาก json.JSONDecodeError — ถ้าวันหนึ่งไม่ใช่แล้ว
    ทุก `except json.JSONDecodeError` ในเส้นทางคำขอจะกลายเป็น 500 เงียบ ๆ
    """
    with pytest.raises(json.JSONDecodeError):
        jsonio.loads('{"not": json')
    with pytest.raises(ValueError):
        jsonio.loads(b"\xff\xfe not json")


def test_a_structure_deeper_than_orjson_allows_still_encodes(mode):
    """orjson หยุดที่ 254 ชั้น · ต้องตกไป stdlib ไม่ใช่ทำให้คำขอพัง"""
    deep: dict = {}
    cursor = deep
    for _ in range(400):
        cursor["n"] = {}
        cursor = cursor["n"]

    assert jsonio.loads(jsonio.dumpb(deep)) == deep


def test_fast_json_response_renders_what_starlette_would(mode):
    from starlette.responses import JSONResponse

    for value in CORPUS:
        assert jsonio.FastJSONResponse(value).body == JSONResponse(value).body


def test_sse_frames_accept_bytes_without_a_round_trip():
    """format_sse รับ bytes ตรง ๆ ได้ — สตรีมหนึ่งเส้นคือหลายพัน frame"""
    from app.upstream.sse import format_json_sse, format_sse

    assert format_sse(b'{"a":1}') == format_sse('{"a":1}') == b'data: {"a":1}\n\n'
    assert format_json_sse({"a": "ก"}, "delta") == (
        'event: delta\ndata: {"a":"ก"}\n\n'.encode()
    )
