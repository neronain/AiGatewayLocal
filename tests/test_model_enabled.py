"""Taking a model out of service without deleting it.

`enabled` was honoured everywhere already — the registry hides a disabled model
from the catalogue, routing skips a disabled endpoint — and the console had no
way to set it. Taking a model down meant deleting its file and rebuilding it
afterwards, losing every setting tuned on the way in.

The case worth guarding is the last endpoint: turning it off leaves the alias
listed and unable to answer, which looks like a broken gateway rather than a
deliberate change.
"""

from __future__ import annotations

import pytest


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _catalogue(client, key):
    return {m["id"] for m in client.get("/v1/models", headers=auth(key)).json()["data"]}


def _set(client, alias, enabled, endpoint=None):
    body = {"enabled": enabled}
    if endpoint:
        body["endpoint"] = endpoint
    return client.patch(f"/admin/models/{alias}/enabled",
                        headers=auth(client.admin_key), json=body)


@pytest.fixture(autouse=True)
def _writable(writable_config):
    """เขียนไฟล์ registry จริง — ใช้สำเนาชั่วคราวไม่ใช่ของจริงในโปรเจกต์"""
    return writable_config


def test_disabling_a_model_takes_it_out_of_the_catalogue(client, member_key):
    assert "coding" in _catalogue(client, member_key)

    response = _set(client, "coding", False)
    assert response.status_code == 200
    assert response.json()["enabled"] is False
    assert "coding" not in _catalogue(client, member_key)


def test_enabling_it_again_brings_it_back(client, member_key):
    _set(client, "coding", False)
    assert _set(client, "coding", True).status_code == 200
    assert "coding" in _catalogue(client, member_key)


def test_the_file_survives_being_disabled(client):
    """ต่างจากการลบ — ค่าที่ปรับมาต้องยังอยู่ครบ"""
    before = client.get("/admin/models", headers=auth(client.admin_key)).json()["data"]
    coding = next(m for m in before if m["alias"] == "coding")

    _set(client, "coding", False)

    after = client.get("/admin/models", headers=auth(client.admin_key)).json()["data"]
    still = next((m for m in after if m["alias"] == "coding"), None)
    assert still is not None, "โมเดลต้องยังอยู่ในรายการของ admin"
    assert len(still["endpoints"]) == len(coding["endpoints"])


def _two_endpoint_model(client, config_dir):
    """config ใน repo มี endpoint เดียวต่อโมเดล — เพิ่มเครื่องที่สองสำหรับเคสนี้

    ต้องอ่านจากไฟล์ ไม่ใช่จาก GET /admin/models: หน้ารายการคืนรูปแบน (alias,
    endpoints พร้อม health) ส่วน POST รับรูปซ้อน (metadata/spec) — ส่งกลับตรง ๆ
    ไม่ผ่าน validation.
    """
    import yaml

    document = yaml.safe_load((config_dir / "models" / "coding.yaml").read_text())
    endpoints = document["spec"]["endpoints"]
    spare = dict(endpoints[0])
    spare["name"] = "spare"
    document["spec"]["endpoints"] = [endpoints[0], spare]

    response = client.post("/admin/models", headers=auth(client.admin_key), json=document)
    assert response.status_code in (200, 201), response.text
    return endpoints[0]["name"]


def test_one_endpoint_can_be_taken_out_while_the_alias_keeps_serving(
    client, member_key, writable_config
):
    """เคสที่ต้องใช้จริง: เครื่องหนึ่งกำลังซ่อม แต่ alias ต้องไม่ล่ม"""
    _two_endpoint_model(client, writable_config)
    response = _set(client, "coding", False, endpoint="spare")
    assert response.status_code == 200, response.text
    assert "coding" in _catalogue(client, member_key), "alias ต้องยังอยู่"


def test_the_last_serving_endpoint_cannot_be_switched_off(client, writable_config):
    """ปิดตัวสุดท้าย = alias ยังโชว์อยู่แต่ตอบไม่ได้ ซึ่งดูเหมือน gateway พัง

    กติกานี้เป็นของ schema (`at least one endpoint must be enabled`) — ที่นี่แค่
    ยืนยันว่าเส้นทางนี้ตรวจซ้ำก่อนเขียนไฟล์ ไม่ใช่ปล่อยให้ไปพังตอน reload
    """
    first = _two_endpoint_model(client, writable_config)
    _set(client, "coding", False, endpoint="spare")
    response = _set(client, "coding", False, endpoint=first)
    assert response.status_code == 400
    assert "endpoint" in response.text.lower()

    # ปฏิเสธแล้วต้องไม่เขียนอะไรทิ้งไว้ — ไม่ใช่เขียนก่อนแล้วไปพังตอน reload
    models = client.get("/admin/models", headers=auth(client.admin_key)).json()["data"]
    coding = next(m for m in models if m["alias"] == "coding")
    still_on = {e["name"] for e in coding["endpoints"] if e["enabled"]}
    assert still_on == {first}, "endpoint ตัวสุดท้ายต้องยังเปิดอยู่"


def test_a_toggle_does_not_eat_the_comments_in_the_file(client, writable_config):
    """คอมเมนต์ในไฟล์ registry คือเหตุผลว่าทำไมตั้งค่าแบบนั้น

    เขียนไฟล์ใหม่ทั้งไฟล์จาก object ที่ parse มาแล้วจะกินคอมเมนต์หมด ซึ่งรับได้ตอน
    admin แก้ฟอร์ม แต่รับไม่ได้ตอนกดสวิตช์ — คนกดไม่ได้ตั้งใจแก้ไฟล์
    """
    path = writable_config / "models" / "coding.yaml"
    before = path.read_text()
    assert "# ->" in before, "ไฟล์ตัวอย่างต้องมีคอมเมนต์ ไม่งั้นเทสนี้ไม่ได้ตรวจอะไร"

    _set(client, "coding", False)
    after = path.read_text()

    for line in before.splitlines():
        if line.strip().startswith("#") or "  # " in line:
            assert line in after, f"คอมเมนต์หาย: {line.strip()}"
    assert "enabled: false" in after, "ต้องเขียนสถานะปิดลงไปจริง"


def test_an_unknown_endpoint_name_says_so(client):
    response = _set(client, "coding", False, endpoint="not-a-machine")
    assert response.status_code == 400
    assert "not-a-machine" in response.json()["error"]["message"]


def test_an_unknown_alias_says_so(client):
    assert _set(client, "not-a-model", False).status_code in (400, 404)


def test_a_member_cannot_disable_a_model(client, member_key):
    response = client.patch("/admin/models/coding/enabled",
                            headers=auth(member_key), json={"enabled": False})
    assert response.status_code in (401, 403)
    assert "coding" in _catalogue(client, member_key)
