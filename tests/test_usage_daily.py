"""The dashboard's usage-over-time chart is backed by /admin/usage/daily.

The endpoint must return one point per day, zero-filled, so an idle day reads as
a dip in the line rather than a missing bucket that shifts the axis.
"""

from __future__ import annotations


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def test_daily_series_is_zero_filled_to_the_window(client):
    body = client.get("/admin/usage/daily?days=14", headers=auth(client.admin_key)).json()
    assert body["window_days"] == 14
    assert len(body["series"]) == 14
    # ascending, unique days; every bucket present even with no traffic
    dates = [row["date"] for row in body["series"]]
    assert dates == sorted(dates)
    assert len(set(dates)) == 14
    for row in body["series"]:
        assert row["requests"] == 0
        assert row["input_tokens"] == 0
        assert row["output_tokens"] == 0


def test_daily_requires_a_manager(client):
    assert client.get("/admin/usage/daily").status_code in (401, 403)
