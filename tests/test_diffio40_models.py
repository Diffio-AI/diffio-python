import json

import httpx
import pytest

from diffio import DiffioClient


@pytest.mark.parametrize(
    ("model", "path"),
    [
        ("diffio-4.0-flash", "/v1/diffio-4.0-flash-generation"),
        ("diffio-4.0-pro", "/v1/diffio-4.0-pro-generation"),
    ],
)
def test_create_generation_routes_each_diffio_4_0_model_to_its_endpoint(model, path):
    received = {}

    def handler(request: httpx.Request) -> httpx.Response:
        received["path"] = request.url.path
        received["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={"generationId": "gen_40", "apiProjectId": "proj_40", "modelKey": model, "status": "queued"},
        )

    http_client = httpx.Client(base_url="https://api.test", transport=httpx.MockTransport(handler))
    client = DiffioClient(apiKey="diffio_live_test", baseUrl="https://api.test", httpClient=http_client)

    response = client.create_generation(apiProjectId="proj_40", model=model)

    assert received["path"] == path
    assert received["payload"]["apiProjectId"] == "proj_40"
    assert response.modelKey == model


def test_creating_a_generation_without_a_model_uses_diffio_40_flash():
    """The published SDK defaulted to the deprecated diffio-2, so docs examples broke."""
    import inspect

    from diffio.client import DiffioClient

    for name in ("create_generation", "restore"):
        method = getattr(DiffioClient, name, None)
        if method is None:
            continue
        default = inspect.signature(method).parameters["model"].default
        assert default == "diffio-4.0-flash", f"{name} still defaults to {default}"
