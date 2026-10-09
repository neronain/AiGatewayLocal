"""คะแนนคุณภาพต่อโมเดล (`spec.quality_score`) — ตั้งได้ และอยู่รอดทุกทางที่ไฟล์ทะเบียนถูกเขียน

ที่มา (2026-10-09): `model="auto"` จัดอันดับด้วยความเร็วอย่างเดียว · มีโมเดลเล็กที่เร็วกับ
โมเดลใหญ่ที่เก่งกว่าอยู่ด้วยกัน งานทั้งหมดจึงไปที่ตัวเล็กเสมอ · ผู้ดูแลต้องมีที่บอกเกตเวย์ว่า
"ตัวไหนดีกว่า" — โมเดลของเราเป็น fine-tune กับตัว quantise ที่ไม่มีดัชนีสาธารณะไหนครอบ
ตัวเลขนี้จึงมาจากคนที่วัดเอง ไม่ได้ดึงจากเน็ต

ไฟล์นี้ยืนยันเรื่องเดียว: **ค่าที่ตั้งแล้วไม่หายเอง** — schema → writer → admin API → คอนโซล
ครบวง (เคสจริง `77e5e9c`: กด Save แล้วค่าที่คอนโซลไม่รู้จักหาย) · การจัดอันดับที่ใช้ค่านี้อยู่ใน
tests/test_auto_quality.py
"""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from app.registry.schema import ModelDefinition
from app.registry.writer import render_yaml


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _model(**spec) -> ModelDefinition:
    return ModelDefinition.model_validate({
        "apiVersion": "litegate.dev/v1", "kind": "Model",
        "metadata": {"alias": "probe", "display_name": "Probe"},
        "spec": {"upstream_model": "org/probe",
                 "limits": {"context_tokens": 32768, "max_output_tokens": 4096},
                 "endpoints": [{"name": "e", "server_type": "vllm",
                                "base_url": "http://e:8000"}],
                 **spec}})


def _listed(client, alias: str) -> dict:
    data = client.get("/admin/models", headers=auth(client.admin_key)).json()["data"]
    return next(m for m in data if m["alias"] == alias)


def _on_disk(config, alias: str) -> dict:
    return yaml.safe_load((config / "models" / f"{alias}.yaml").read_text())["spec"]


def _document(got: dict, **spec) -> dict:
    """เอกสารแบบที่คอนโซล *รุ่นก่อนมีฟิลด์นี้* ประกอบจากสิ่งที่ GET คืนมา — ไม่มี quality_score"""
    return {
        "apiVersion": "litegate.dev/v1", "kind": "Model",
        "metadata": {"alias": got["alias"], "display_name": got["display_name"],
                     "description": got["description"], "visibility": got["visibility"],
                     "tags": got["tags"]},
        "spec": {
            "upstream_model": got["upstream_model"], "purpose": got["purpose"],
            "limits": got["limits"], "modalities": got["modalities"],
            "capabilities": got["capabilities"], "protocols": got["protocols"],
            "endpoints": [{k: v for k, v in e.items() if k != "health" and v is not None}
                          for e in got["endpoints"]],
            "enabled": True,
            **spec,
        },
    }


def _save(client, document: dict):
    response = client.post("/admin/models", headers=auth(client.admin_key), json=document)
    assert response.status_code in (200, 201), response.text
    return response


# ---------------------------------------------------------------------------
# schema + writer
# ---------------------------------------------------------------------------
def test_a_model_without_a_score_is_what_every_existing_file_is():
    """ไม่ตั้ง = ไม่มีคะแนน ไม่ใช่ศูนย์ — ทะเบียนที่มีอยู่แล้วทุกไฟล์โหลดได้เหมือนเดิม"""
    assert _model().spec.quality_score is None
    assert "quality_score" not in render_yaml(_model())


@pytest.mark.parametrize("score", [0, 1, 55, 100])
def test_a_score_survives_render_and_reload(score):
    rendered = render_yaml(_model(quality_score=score))
    again = ModelDefinition.model_validate(yaml.safe_load(rendered))
    assert again.spec.quality_score == score, rendered


@pytest.mark.parametrize("bad", [-1, 101, 55.5, "high", True, [80]])
def test_a_score_that_is_not_a_whole_number_from_0_to_100_is_refused_at_load(bad):
    """ผิดตอนโหลด ไม่ใช่ตอนมีคำขอ · `true` ต้องไม่กลายเป็นคะแนน 1 เงียบ ๆ (YAML: `yes`)"""
    with pytest.raises(ValidationError):
        _model(quality_score=bad)


# ---------------------------------------------------------------------------
# admin API
# ---------------------------------------------------------------------------
def test_a_score_set_through_the_admin_api_survives_a_registry_reload(writable_config, client):
    _save(client, _document(_listed(client, "coding"), quality_score=85))

    assert _on_disk(writable_config, "coding")["quality_score"] == 85
    assert _listed(client, "coding")["quality_score"] == 85

    reloaded = client.post("/admin/registry/reload", headers=auth(client.admin_key))
    assert reloaded.status_code == 200, reloaded.text
    assert _listed(client, "coding")["quality_score"] == 85
    assert client.app.state.services.registry.snapshot.models["coding"].spec.quality_score == 85


def test_a_worker_that_starts_later_reads_the_same_score(writable_config, client):
    """ค่าอยู่ในไฟล์ทะเบียน — process ใหม่ (worker อีกตัว · หลังรีสตาร์ต) เห็นค่าเดียวกัน"""
    from fastapi.testclient import TestClient

    from app.main import create_app

    _save(client, _document(_listed(client, "coding"), quality_score=85))
    with TestClient(create_app()) as other:
        assert other.app.state.services.registry.snapshot.models[
            "coding"].spec.quality_score == 85


def test_a_save_that_does_not_mention_the_score_keeps_it(writable_config, client):
    """คอนโซลรุ่นก่อนหน้า (แท็บที่เปิดค้างไว้ข้ามการอัปเดต) หรือสคริปต์ที่ไม่รู้จักฟิลด์นี้

    Save คือเขียนทับทั้งเอกสาร — ไม่พูดถึง = หาย · ภายใต้กลยุทธ์ `quality` คะแนนที่หายคือ
    โมเดลตัวนั้นตกไปท้ายแถวและ `auto` ย้ายงานไปตัวอื่นทั้งหมด โดยที่คนกดแค่แก้ชื่อที่แสดง
    """
    _save(client, _document(_listed(client, "coding"), quality_score=85))

    stale = _document(_listed(client, "coding"))
    stale["metadata"]["display_name"] = "renamed only"
    assert "quality_score" not in stale["spec"]
    _save(client, stale)

    assert _listed(client, "coding")["display_name"] == "renamed only"
    assert _on_disk(writable_config, "coding")["quality_score"] == 85
    assert _listed(client, "coding")["quality_score"] == 85


def test_an_explicit_null_clears_the_score(writable_config, client):
    """ไม่พูดถึง ≠ ขอให้ลบ · `null` คือผู้ดูแลล้างช่องเอง — คอนโซลส่งแบบนี้เมื่อช่องว่าง"""
    _save(client, _document(_listed(client, "coding"), quality_score=85))
    _save(client, _document(_listed(client, "coding"), quality_score=None))

    assert "quality_score" not in _on_disk(writable_config, "coding")
    assert _listed(client, "coding")["quality_score"] is None


def test_preview_shows_the_score_a_save_would_keep(writable_config, client):
    """Preview YAML ต้องตรงกับที่ Save จะเขียน — ทะเบียน read-only ใช้ปุ่มนี้แทน Save"""
    _save(client, _document(_listed(client, "coding"), quality_score=85))
    preview = client.post("/admin/models/preview", headers=auth(client.admin_key),
                          json=_document(_listed(client, "coding")))
    assert preview.status_code == 200, preview.text
    assert yaml.safe_load(preview.json()["yaml"])["spec"]["quality_score"] == 85


def test_an_out_of_range_score_is_a_400_and_nothing_is_written(writable_config, client):
    before = (writable_config / "models" / "coding.yaml").read_text()
    response = client.post("/admin/models", headers=auth(client.admin_key),
                           json=_document(_listed(client, "coding"), quality_score=140))
    assert response.status_code == 400, response.text
    assert "quality_score" in response.text
    assert (writable_config / "models" / "coding.yaml").read_text() == before


def test_switching_a_model_off_and_on_keeps_the_score(writable_config, client):
    """ทางเขียนไฟล์อีกสองทาง (แก้บรรทัดเดียว · เขียนใหม่จาก snapshot) ก็ต้องไม่ทำคะแนนหาย"""
    headers = auth(client.admin_key)
    _save(client, _document(_listed(client, "muse-local"), quality_score=70))
    for wanted in (False, True):
        done = client.patch("/admin/models/muse-local/enabled", headers=headers,
                            json={"enabled": wanted})
        assert done.status_code == 200, done.text
    assert _on_disk(writable_config, "muse-local")["quality_score"] == 70


# ---------------------------------------------------------------------------
# คอนโซล: รันฟังก์ชันของหน้าเว็บจริงใน node แล้วส่งผลเข้า admin API จริง
# ---------------------------------------------------------------------------
def _console_document(score_field: str) -> dict:
    from tests.test_context_per_request import _run_console

    # ฟอร์มใน _run_console เป็นตัวปลอมที่ไม่มีอะไรติ๊กไว้ (ไม่มี DOM) — ติ๊กสองช่องที่ฟอร์มจริงติ๊กมาให้
    # และเติมแถว backend ให้ครบ พอที่ API จะรับเอกสารนี้ได้
    document = _run_console(
        "$('c-chat').checked = true; $('x-openai').checked = true;"
        f"$('m-quality').value = {score_field!r};"
        "console.log(JSON.stringify(editorValues()));")
    for endpoint in document["spec"]["endpoints"]:
        endpoint.setdefault("server_type", "vllm")
    return document


def test_console_sends_the_score_typed_into_the_form():
    assert _console_document("85")["spec"]["quality_score"] == 85
    assert _console_document("0")["spec"]["quality_score"] == 0, "0 คือคะแนน ไม่ใช่ช่องว่าง"


def test_console_sends_an_explicit_null_when_the_field_is_empty():
    """ต้อง *มีคีย์* และเป็น null — ถ้าไม่ส่งคีย์เลย API จะเข้าใจว่าไม่ได้พูดถึงและเก็บค่าเดิมไว้
    ผู้ดูแลจึงล้างคะแนนจากหน้าจอไม่ได้"""
    spec = _console_document("")["spec"]
    assert "quality_score" in spec and spec["quality_score"] is None


@pytest.mark.parametrize("typed", ["140", "-3", "7.5", "good"])
def test_console_refuses_a_score_the_registry_would_refuse(typed):
    from tests.test_context_per_request import _run_console

    problems = _run_console(
        f"$('m-quality').value = {typed!r};"
        "console.log(JSON.stringify(editorProblems()));")
    assert [p for p in problems if p["field"] == "m-quality"], problems


def test_console_set_then_clear_through_the_real_api(writable_config, client):
    """เดินเส้นทางของปุ่ม Save ทั้งเส้น: ฟอร์ม → เอกสารที่ JS ประกอบ → POST → ไฟล์ → GET"""
    _save(client, _console_document("85"))
    assert _on_disk(writable_config, "coding")["quality_score"] == 85
    assert _listed(client, "coding")["quality_score"] == 85

    _save(client, _console_document(""))
    assert "quality_score" not in _on_disk(writable_config, "coding")
    assert _listed(client, "coding")["quality_score"] is None


def test_the_edit_form_has_a_field_for_the_score_and_fills_it_from_the_model():
    """ช่องต้องมีอยู่บนหน้าจริง และ openEditor ต้องเติมค่าเดิมลงไป — ไม่งั้นเปิดฟอร์มแล้วกด Save
    เท่ากับส่ง null = ล้างคะแนนทุกครั้งที่แก้อย่างอื่น"""
    import json
    import re
    import shutil
    import subprocess
    from pathlib import Path

    static = Path(__file__).resolve().parents[1] / "app" / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    assert re.search(r'<input[^>]*id="m-quality"', html), "ฟอร์มแก้โมเดลไม่มีช่อง m-quality"

    node = shutil.which("node")
    if not node:
        pytest.skip("ไม่มี node บนเครื่องนี้ — CI มีให้")
    from tests.test_context_per_request import _js_function

    model = {
        "alias": "coding", "display_name": "Coder", "description": "", "visibility": "member",
        "upstream_model": "org/model", "tags": [], "purpose": ["coding"], "routing": {},
        "limits": {"context_tokens": 262144, "max_output_tokens": 8192},
        "capabilities": {"chat": True}, "protocols": {"openai": True, "anthropic": False},
        "agent_clients": {}, "endpoints": [],
    }
    script = f"""
const fields = {{}};
const $ = (id) => fields[id] || (fields[id] = {{
  value: '', checked: false, hidden: false, disabled: false, innerHTML: '',
  textContent: '', scrollIntoView() {{}} }});
const FORM_PURPOSES = ['general', 'coding'];
const state = {{cache: {{}}}};
const flash = () => {{}};
const renderFallback = () => {{}};
const addEndpointRow = () => {{}};
{_js_function("openEditor")}
openEditor({{...{json.dumps(model)}, quality_score: 85}});
const scored = $('m-quality').value;
openEditor({json.dumps(model)});
const unscored = $('m-quality').value;
openEditor(null);
console.log(JSON.stringify({{scored, unscored, fresh: $('m-quality').value}}));
"""
    done = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    shown = json.loads(done.stdout)
    assert str(shown["scored"]) == "85"
    assert shown["unscored"] == "", "โมเดลที่ไม่มีคะแนนต้องเห็นช่องว่าง ไม่ใช่ค่าของตัวที่เปิดก่อนหน้า"
    assert shown["fresh"] == ""
