

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
