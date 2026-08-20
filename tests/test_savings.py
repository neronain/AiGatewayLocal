"""รายงาน "ถ้าไม่ได้รันเอง จะจ่ายเท่าไร"

โรงเรียนที่ลงทุนซื้อเครื่องมารันเองต้องตอบผู้บริหารให้ได้ว่าคุ้มไหม · เรานับ token
ครบอยู่แล้ว ขาดแค่ตารางราคา
"""

from app.core import pricing


def test_prices_are_static_so_the_report_works_offline():
    """โรงเรียนหลายแห่งอยู่หลัง proxy — รายงานที่ต้องต่อเน็ตคือรายงานที่ใช้ไม่ได้"""
    import inspect

    source = inspect.getsource(pricing)
    for network in ("requests.", "httpx.", "urllib", "aiohttp"):
        assert network not in source, f"ตารางราคาต้องไม่ดึงจากเน็ต: {network}"


def test_estimate_charges_input_and_output_separately():
    """output แพงกว่า input หลายเท่าในทุกเจ้า — คิดรวมเป็นก้อนเดียวคือผิดสาระ"""
    only_in = pricing.estimate("gpt-4o", 1_000_000, 0)
    only_out = pricing.estimate("gpt-4o", 0, 1_000_000)
    assert only_out > only_in
    assert pricing.estimate("gpt-4o", 1_000_000, 1_000_000) == only_in + only_out


def test_unknown_baseline_falls_back_instead_of_crashing():
    """หน้าเว็บส่งค่าเพี้ยนมาไม่ควรทำให้รายงานล่ม"""
    assert pricing.estimate("ไม่มีอยู่", 1000, 1000) == pricing.estimate(
        pricing.DEFAULT_BASELINE, 1000, 1000)


def test_zero_traffic_costs_nothing():
    assert pricing.estimate("gpt-4o", 0, 0) == 0


def test_catalogue_feeds_the_dropdown_without_duplicating_prices():
    entries = pricing.catalogue()
    assert len(entries) == len(pricing.BASELINES)
    for entry in entries:
        assert entry["id"] in pricing.BASELINES
        assert entry["input_per_1m"] > 0 and entry["output_per_1m"] > 0


def test_the_report_says_how_old_the_prices_are():
    """ตัวเลขที่ไม่บอกวันที่ = คนอ่านต้องเดาเองว่าเก่าแค่ไหน"""
    assert pricing.PRICES_UPDATED
    admin = __import__("pathlib").Path("app/api/admin.py").read_text(encoding="utf-8")
    assert '"prices_updated"' in admin
    # ต้องบอกด้วยว่าเป็นประมาณการ ไม่ใช่ใบแจ้งหนี้
    assert '"caveat"' in admin


# ── endpoint ────────────────────────────────────────────────────────────────
import httpx
import respx

from tests.test_api import OPENAI_REPLY, UPSTREAM_CHAT, auth


@respx.mock
def test_the_report_prices_real_traffic(client, member_key):
    respx.post(UPSTREAM_CHAT).mock(return_value=httpx.Response(200, json=OPENAI_REPLY))
    client.post(
        "/v1/chat/completions",
        headers=auth(member_key),
        json={"model": "coding", "messages": [{"role": "user", "content": "hi"}]},
    )
    report = client.get(
        "/admin/usage/savings?days=1&baseline=gpt-4o", headers=auth(client.admin_key)
    ).json()

    assert report["baseline"]["id"] == "gpt-4o"
    assert report["requests"] >= 1
    assert report["output_tokens"] > 0
    # มีทราฟฟิกแล้วต้องได้ตัวเลขเงิน ไม่ใช่ศูนย์
    assert report["would_have_cost_usd"] > 0
    coding = next(r for r in report["by_model"] if r["model"] == "coding")
    assert coding["would_have_cost_usd"] > 0
    # ต้องบอกว่าเป็นประมาณการและราคาลงวันที่ไว้เมื่อไร
    assert report["caveat"] and report["prices_updated"]


def test_baselines_endpoint_feeds_the_dropdown(client):
    d = client.get("/admin/usage/savings/baselines", headers=auth(client.admin_key)).json()
    assert d["default"] in {b["id"] for b in d["data"]}


@respx.mock
def test_a_bad_baseline_still_returns_a_report(client, member_key):
    """หน้าเว็บส่งค่าเพี้ยนมาไม่ควรทำให้รายงานล่ม"""
    report = client.get(
        "/admin/usage/savings?days=1&baseline=ไม่มีอยู่", headers=auth(client.admin_key)
    ).json()
    assert report["baseline"]["id"] == pricing.DEFAULT_BASELINE


def test_tiny_traffic_is_not_rounded_away():
    """ปัดเป็น 2 ตำแหน่งทำให้โรงเรียนทราฟฟิกน้อยเห็น $0.00 แล้วคิดว่ารายงานพัง"""
    from pathlib import Path

    admin = Path("app/api/admin.py").read_text(encoding="utf-8")
    assert "round(pricing.estimate(chosen, total_in, total_out), 6)" in admin
    js = Path("app/static/app.js").read_text(encoding="utf-8")
    assert "'<$0.01'" in js, "หน้าเว็บต้องบอกว่าน้อยกว่าหนึ่งเซนต์ ไม่ใช่โชว์ศูนย์"
