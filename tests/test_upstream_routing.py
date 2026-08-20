

def test_cloud_base_urls_do_not_get_a_second_v1():
    """ทุกเจ้าเขียน base URL ในเอกสารพร้อม prefix มาแล้ว — ต่อ /v1 ทับลงไปคือ 404

    เจอตอนลูกค้าเพิ่ม MiniMax จริง: เกตเวย์ยิง /v1/v1/models แล้วได้ 404 กลับมา
    """
    from app.core.providers import CLOUD
    from app.registry.schema import Endpoint
    from app.upstream.client import upstream_url

    for provider in CLOUD.values():
        endpoint = Endpoint(name=provider.id, server_type="vllm", base_url=provider.base_url)
        path = "/v1/messages" if not provider.speaks_openai else "/v1/chat/completions"
        url = upstream_url(endpoint, path)
        assert "/v1/v1/" not in url, provider.id
        assert url.startswith(provider.base_url), provider.id


def test_a_path_prefix_that_is_not_an_api_prefix_still_gets_v1():
    """เกตเวย์หลัง reverse proxy มี path นำหน้าได้ และมันไม่ใช่ prefix ของ API"""
    from app.registry.schema import Endpoint
    from app.upstream.client import upstream_url

    endpoint = Endpoint(name="proxied", server_type="vllm", base_url="https://proxy.example/edullm")
    assert upstream_url(endpoint, "/v1/chat/completions") == (
        "https://proxy.example/edullm/v1/chat/completions"
    )


def test_a_local_server_written_with_v1_is_forgiven():
    from app.registry.schema import Endpoint
    from app.upstream.client import upstream_url

    endpoint = Endpoint(name="local", server_type="vllm", base_url="http://dgx03:8000/v1")
    assert upstream_url(endpoint, "/v1/models") == "http://dgx03:8000/v1/models"


def test_every_cloud_provider_is_a_valid_server_type():
    """เพิ่มเจ้าใหม่ในตาราง CLOUD แล้วลืมเติมใน ServerType = เลือกในหน้าเว็บได้แต่บันทึกไม่ผ่าน"""
    from app.core.providers import CLOUD
    from app.registry.schema import ServerType

    known = {t.value for t in ServerType}
    assert set(CLOUD) <= known, set(CLOUD) - known


def test_minimax_regions_are_separate_providers():
    """คีย์ของฝั่งหนึ่งใช้กับอีกฝั่งไม่ได้จริง — ยิงทดสอบแล้วได้ invalid api key กลับมา

    ตอนแรกเป็นตัวเลือกเดียวพร้อมหมายเหตุให้ไปแก้ URL เอง ซึ่งไม่มีใครทำ
    """
    from app.core.providers import CLOUD

    assert CLOUD["minimax"].base_url == "https://api.minimax.io/v1"
    assert CLOUD["minimax-cn"].base_url == "https://api.minimaxi.com/v1"
    assert CLOUD["minimax"].base_url != CLOUD["minimax-cn"].base_url
