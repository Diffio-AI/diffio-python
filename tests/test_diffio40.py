import json

import httpx
import pytest

from diffio import DiffioClient, DiffioApiError, GenerationExportPendingResponse, ModelKey


def client_for(handler):
    return DiffioClient(apiKey="test", httpClient=httpx.Client(
        base_url="https://api.test", transport=httpx.MockTransport(handler),
    ))


def ready():
    return dict(generationId="g", apiProjectId="p", downloadType="audio",
                downloadUrl="https://files.test/audio", fileName="mix.flac",
                storagePath="mix.flac", bucket="test", mimeType="audio/flac")


def test_create_v40():
    def handler(request):
        assert request.url.path == "/v1/diffio-4.0-generation"
        assert json.loads(request.content) == {"apiProjectId": "p", "idempotencyKey": "once"}
        return httpx.Response(200, json=dict(generationId="g", apiProjectId="p",
                                           modelKey="diffio-4.0", status="queued"))
    assert "diffio-4.0" in ModelKey
    assert client_for(handler).generations.create(apiProjectId="p", model="diffio-4.0",
                                                  idempotencyKey="once").generationId == "g"


@pytest.mark.parametrize("format", ["mp3", "flac", "mp4"])
@pytest.mark.parametrize("artifact", ["mix", "speech", "background"])
def test_download_options(format, artifact):
    def handler(request):
        assert json.loads(request.content) == dict(
            generationId="g", apiProjectId="p", format=format, artifact=artifact, backgroundGain=0.25)
        return httpx.Response(200, json=ready())
    assert client_for(handler).generations.get_download(
        generationId="g", apiProjectId="p", format=format, artifact=artifact,
        backgroundGain=0.25).downloadUrl == ready()["downloadUrl"]


def test_pending_then_ready(monkeypatch):
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(202, json=dict(status="pending", exportId="e", retryAfterSeconds=2))
        return httpx.Response(200, json=ready())
    monkeypatch.setattr("diffio.client.time.sleep", lambda _: None)
    result = client_for(handler).generations.wait_for_download(
        generationId="g", apiProjectId="p", artifact="mix", format="flac", backgroundGain=0.2)
    assert result.downloadUrl
    assert requests[0] == requests[1]


def test_pending_response():
    client = client_for(lambda _: httpx.Response(202, json=dict(status="pending", exportId="e",
                                                              retryAfterSeconds=2)))
    response = client.generations.get_download(generationId="g", apiProjectId="p")
    assert isinstance(response, GenerationExportPendingResponse)
    assert response.exportId == "e"


def test_pending_timeout(monkeypatch):
    ticks = iter([0, 0, 0, 2, 2])
    monkeypatch.setattr("diffio.client.time.monotonic", lambda: next(ticks))
    client = client_for(lambda _: httpx.Response(202, json=dict(status="pending", exportId="e")))
    with pytest.raises(TimeoutError):
        client.generations.wait_for_download(generationId="g", apiProjectId="p", timeout=1)


def test_mix_and_playback():
    def handler(request):
        body = json.loads(request.content)
        if request.url.path.endswith("update_generation_mix"):
            assert body == dict(generationId="g", apiProjectId="p", backgroundGain=0.4, expectedRevision=0)
            return httpx.Response(200, json=dict(generationId="g", mix=dict(backgroundGain=0.4, revision=1)))
        assert request.url.path.endswith("get_generation_playback")
        assert body == dict(generationId="g", apiProjectId="p", startChunk=0, chunkCount=8)
        return httpx.Response(200, json=dict(generationId="g", manifest={"chunks": [{"url": "signed"}]}))
    client = client_for(handler)
    assert client.generations.update_mix(apiProjectId="p", generationId="g",
                                         backgroundGain=0.4, expectedRevision=0).mix.revision == 1
    assert client.generations.get_playback(apiProjectId="p", generationId="g").manifest["chunks"]


def test_stale_mix_not_retried():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(409, json={"error": "Stale revision"})
    with pytest.raises(DiffioApiError) as error:
        client_for(handler).generations.update_mix(apiProjectId="p", generationId="g",
            backgroundGain=0, expectedRevision=0, requestOptions={"maxRetries": 3, "retryStatusCodes": [409]})
    assert error.value.statusCode == 409
    assert len(calls) == 1


@pytest.mark.parametrize("gain", [True, float("nan"), float("inf"), -0.1, 1.1, "0.5"])
def test_invalid_gain(gain):
    client = client_for(lambda _: pytest.fail("must not send request"))
    with pytest.raises(ValueError):
        client.generations.get_download(apiProjectId="p", generationId="g", backgroundGain=gain)
    with pytest.raises(ValueError):
        client.generations.update_mix(apiProjectId="p", generationId="g", backgroundGain=gain, expectedRevision=0)


@pytest.mark.parametrize("body", [
    {}, [], {"status": "pending"}, {"status": "pending", "exportId": ""},
    {"status": "pending", "exportId": "e", "retryAfterSeconds": None},
    {"status": "pending", "exportId": "e", "retryAfterSeconds": "2"},
    {"status": "pending", "exportId": "e", "retryAfterSeconds": -1},
    {"status": "pending", "exportId": "e", "retryAfterSeconds": True},
])
def test_malformed_pending(body):
    client = client_for(lambda _: httpx.Response(202, json=body))
    with pytest.raises(DiffioApiError):
        client.generations.wait_for_download(apiProjectId="p", generationId="g")


@pytest.mark.parametrize("delay", ["NaN", "Infinity"])
def test_nonfinite_pending_delay(delay):
    body = '{"status":"pending","exportId":"e","retryAfterSeconds":' + delay + '}'
    client = client_for(lambda _: httpx.Response(202, content=body))
    with pytest.raises(DiffioApiError):
        client.generations.wait_for_download(apiProjectId="p", generationId="g")


def test_playback_pagination():
    def handler(request):
        assert json.loads(request.content) == dict(
            apiProjectId="p", generationId="g", startChunk=16, chunkCount=4)
        return httpx.Response(200, json=dict(generationId="g",
            manifest=dict(startChunk=16, chunkCount=30, chunks=[{"index":16}], mix={"revision":0})))
    result = client_for(handler).generations.get_playback(
        apiProjectId="p", generationId="g", startChunk=16, chunkCount=4)
    assert result.manifest["chunkCount"] == 30
    assert len(result.manifest["chunks"]) == 1


@pytest.mark.parametrize("options", [
    {"startChunk": -1}, {"startChunk": True}, {"startChunk": 0.1},
    {"chunkCount": 0}, {"chunkCount": 17}, {"chunkCount": True}, {"chunkCount": 2.5},
])
def test_invalid_playback_page(options):
    client = client_for(lambda _: pytest.fail("must not request"))
    with pytest.raises(ValueError):
        client.get_generation_playback(apiProjectId="p", generationId="g", **options)


def test_polling_disables_transport_retries_without_mutating_defaults():
    calls = []
    def handler(request):
        calls.append(request)
        assert request.extensions["timeout"]["read"] <= 1
        return httpx.Response(503, json={"error": "Unavailable"})
    client = client_for(handler)
    client._default_request_options.maxRetries = 3
    with pytest.raises(DiffioApiError):
        client.generations.wait_for_download(apiProjectId="p", generationId="g", timeout=1)
    assert len(calls) == 1
    assert client._default_request_options.maxRetries == 3
    assert client._default_request_options.timeout is None
