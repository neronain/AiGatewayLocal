"""health probe ต้องบอกว่ามันเห็นอะไร — ไม่ใช่จบที่เครื่องหมายโคลอน

เครื่องจริง 2026-10-06 18:58 (VM เพิ่งบูต): ทุก endpoint ขึ้น

    marked unhealthy after 3 failures: health probe failed:

แล้วไม่มีอะไรต่อ · `_probe` เขียน `f"health probe failed: {exc}"` และ exception ฝั่งเครือข่าย
ของ httpx (ConnectTimeout · ReadTimeout · PoolTimeout) `str()` แล้วได้สตริงว่าง — คนที่เปิด log
ตอนตีสองแยกไม่ออกว่าเครื่องปิดอยู่ ปฏิเสธการเชื่อมต่อ หรือแค่ช้า
"""

from __future__ import annotations

import logging

import httpx
import pytest
import respx

from tests.realistic_backends import CODING, auth


def _coding(client):  # noqa: ANN001
    services = client.app.state.services
    return services.router, services.registry.snapshot.models["coding"].spec.endpoints[0]


@respx.mock
@pytest.mark.parametrize("failure, expected", [
    (httpx.ConnectTimeout(""), "ConnectTimeout"),
    (httpx.ReadTimeout(""), "ReadTimeout"),
    (httpx.ConnectError("All connection attempts failed"),
     "ConnectError: All connection attempts failed"),
], ids=["connect-timeout", "read-timeout", "refused"])
def test_a_failed_probe_names_the_failure(client, caplog, failure, expected):
    respx.get(f"{CODING}/health").mock(side_effect=failure)
    router, endpoint = _coding(client)
    with caplog.at_level(logging.ERROR, logger="app.core.routing"):
        for _ in range(3):  # unhealthy_threshold
            client.portal.call(router._probe, "coding", endpoint, 1.0)

    row = client.get("/v1/health/endpoints", headers=auth(client.admin_key)).json()["data"][
        "coding:dgx03"]
    assert row["healthy"] is False
    assert row["last_error"] == f"health probe failed: {expected}"
    marked = [r.getMessage() for r in caplog.records if "marked unhealthy" in r.getMessage()]
    assert marked and marked[0].endswith(f"health probe failed: {expected}"), marked


def test_describe_never_returns_an_empty_string():
    from app.core.errors import describe

    assert describe(httpx.PoolTimeout("")) == "PoolTimeout"
    assert describe(TimeoutError()) == "TimeoutError"
    assert describe(ValueError("  bad value ")) == "ValueError: bad value"


@respx.mock
@pytest.mark.parametrize("status", [401, 404])
def test_a_health_path_that_answers_like_another_product_is_visible(client, caplog, status):
    """portainer บน :8000 ตอบ health path ด้วย 404/401 — เครื่องยัง "healthy" แต่ต้องมีที่ให้เห็น

    กติกา "ต่ำกว่า 500 = ถึงแล้ว" คงไว้ (ผู้ให้บริการออนไลน์พึ่ง 401 ของ GET /models) จึงไม่
    เปลี่ยนคำตัดสิน · สิ่งที่เพิ่มคือรายงานสุขภาพบอกว่า probe เห็นอะไร และ log เตือนหนึ่งครั้ง
    """
    respx.get(f"{CODING}/health").mock(return_value=httpx.Response(status))
    router, endpoint = _coding(client)

    with caplog.at_level(logging.WARNING, logger="app.core.routing"):
        client.portal.call(router._probe, "coding", endpoint, 1.0)
        client.portal.call(router._probe, "coding", endpoint, 1.0)

    row = client.get("/v1/health/endpoints", headers=auth(client.admin_key)).json()["data"][
        "coding:dgx03"]
    assert row["healthy"] is True
    assert row["last_probe"] == f"HTTP {status}"
    warned = [r.getMessage() for r in caplog.records if "health path" in r.getMessage()]
    assert len(warned) == 1, "เตือนเมื่อคำตอบเปลี่ยน ไม่ใช่ทุกรอบ probe"
    assert f"HTTP {status}" in warned[0] and "coding:dgx03" in warned[0]


@respx.mock
def test_a_normal_health_answer_is_reported_without_a_warning(client, caplog):
    respx.get(f"{CODING}/health").mock(return_value=httpx.Response(200))
    router, endpoint = _coding(client)

    with caplog.at_level(logging.WARNING, logger="app.core.routing"):
        client.portal.call(router._probe, "coding", endpoint, 1.0)

    row = client.get("/v1/health/endpoints", headers=auth(client.admin_key)).json()["data"][
        "coding:dgx03"]
    assert row["last_probe"] == "HTTP 200"
    assert not [r for r in caplog.records if "health path" in r.getMessage()]
