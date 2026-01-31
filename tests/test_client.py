import json
import mimetypes
from pathlib import Path

import httpx
import pytest

from diffio import DiffioClient


def test_create_project_payload_and_headers(tmp_path: Path):
    received = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/create_project":
            received["path"] = request.url.path
            received["auth"] = request.headers.get("Authorization")
            payload = json.loads(request.content.decode("utf-8"))
            received["payload"] = payload
            return httpx.Response(
                200,
                json={
                    "apiProjectId": "proj_123",
                    "uploadUrl": "https://upload.test/upload",
                    "uploadMethod": "PUT",
                    "objectPath": "users/u/projects/proj_123/original/sample.txt",
                    "bucket": "diffio_api",
                    "expiresAt": "2026-01-24T00:00:00Z",
                },
            )

        if request.url.host == "upload.test":
            received["upload_method"] = request.method
            received["upload_content_type"] = request.headers.get("Content-Type")
            return httpx.Response(200, json={})

        return httpx.Response(404, json={"error": "not found"})

    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(base_url="https://api.test", transport=transport)
    client = DiffioClient(apiKey="diffio_live_test", baseUrl="https://api.test", httpClient=http_client)

    file_path = tmp_path / "sample.txt"
    file_path.write_text("hello world", encoding="utf-8")
    expected_content_type, _ = mimetypes.guess_type(str(file_path))
    expected_content_type = expected_content_type or "application/octet-stream"

    response = client.create_project(filePath=str(file_path))

    assert received["path"] == "/v1/create_project"
    assert received["auth"] == "Bearer diffio_live_test"
    assert received["payload"]["fileName"] == "sample.txt"
    assert received["payload"]["contentType"] == expected_content_type
    assert received["payload"]["contentLength"] == file_path.stat().st_size
    assert received["upload_method"] == "PUT"
    assert received["upload_content_type"] == expected_content_type
    assert response.apiProjectId == "proj_123"


def test_request_options_override_headers_and_api_key():
    received = {}

    def handler(request: httpx.Request) -> httpx.Response:
        received["auth"] = request.headers.get("Authorization")
        received["custom"] = request.headers.get("X-Test")
        return httpx.Response(200, json={"projects": []})

    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(base_url="https://api.test", transport=transport)
    client = DiffioClient(apiKey="diffio_live_test", baseUrl="https://api.test", httpClient=http_client)

    client.list_projects(
        requestOptions={
            "headers": {"X-Test": "present"},
            "apiKey": "diffio_override",
        }
    )

    assert received["auth"] == "Bearer diffio_override"
    assert received["custom"] == "present"


def test_request_options_retries_on_status():
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(200, json={"projects": []})

    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(base_url="https://api.test", transport=transport)
    client = DiffioClient(apiKey="diffio_live_test", baseUrl="https://api.test", httpClient=http_client)

    client.list_projects(
        requestOptions={
            "maxRetries": 1,
            "retryBackoff": 0,
        }
    )

    assert calls["count"] == 2


def test_upload_file_streams_from_disk(monkeypatch):
    class DummyFile:
        def __init__(self):
            self.read_calls = 0
            self.closed = False

        def read(self, size=-1):
            self.read_calls += 1
            raise AssertionError("upload should not read the file into memory")

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            self.closed = True
            return False

    dummy_file = DummyFile()

    def fake_open(path, mode):
        assert mode == "rb"
        return dummy_file

    monkeypatch.setattr("builtins.open", fake_open)

    class DummyClient:
        def __init__(self):
            self.request_args = None

        def request(self, method, url, headers=None, content=None, json=None):
            self.request_args = {
                "method": method,
                "url": url,
                "headers": headers,
                "content": content,
                "json": json,
            }
            return httpx.Response(200, json={})

        def close(self):
            pass

    http_client = DummyClient()
    client = DiffioClient(apiKey="diffio_live_test", baseUrl="https://api.test", httpClient=http_client)

    client._upload_file(
        uploadUrl="https://upload.test/upload",
        filePath="test.wav",
        contentType="audio/wav",
        requestOptions={"headers": {"X-Upload": "1"}},
    )

    assert dummy_file.read_calls == 0
    assert dummy_file.closed is True
    assert http_client.request_args["content"] is dummy_file
    assert http_client.request_args["headers"]["X-Upload"] == "1"


def test_wait_for_generation_reports_progress(monkeypatch):
    progress_calls = []
    status_sequence = ["queued", "processing", "complete"]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/get_generation_progress":
            status = status_sequence.pop(0)
            return httpx.Response(
                200,
                json={
                    "generationId": "gen_123",
                    "apiProjectId": "proj_123",
                    "status": status,
                    "hasVideo": False,
                    "preProcessing": {
                        "jobId": "job-pre",
                        "jobState": "SUCCEEDED",
                        "status": "complete",
                        "progress": 100,
                        "statusMessage": None,
                        "error": None,
                        "errorDetails": None,
                    },
                    "inference": {
                        "jobId": "job-run",
                        "jobState": "RUNNING",
                        "status": "running",
                        "progress": 50,
                        "statusMessage": None,
                        "error": None,
                        "errorDetails": None,
                    },
                },
            )
        return httpx.Response(404, json={"error": "not found"})

    import diffio.client as client_module

    monkeypatch.setattr(client_module.time, "sleep", lambda *_: None)

    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(base_url="https://api.test", transport=transport)
    client = DiffioClient(apiKey="diffio_live_test", baseUrl="https://api.test", httpClient=http_client)

    def on_progress(progress):
        progress_calls.append(progress.status)

    progress = client.generations.wait_for_complete(
        generationId="gen_123",
        apiProjectId="proj_123",
        pollInterval=0.0,
        timeout=5.0,
        onProgress=on_progress,
    )

    assert progress.status == "complete"
    assert progress_calls == ["queued", "processing", "complete"]


def test_audio_isolation_isolate_runs_full_flow(tmp_path: Path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url)))

        if request.url.path == "/v1/create_project":
            payload = json.loads(request.content.decode("utf-8"))
            assert payload["fileName"] == "input.wav"
            return httpx.Response(
                200,
                json={
                    "apiProjectId": "proj_abc",
                    "uploadUrl": "https://upload.test/upload",
                    "uploadMethod": "PUT",
                    "objectPath": "users/u/projects/proj_abc/original/input.wav",
                    "bucket": "diffio_api",
                    "expiresAt": "2026-01-24T00:00:00Z",
                },
            )

        if request.url.host == "upload.test":
            assert request.headers.get("Authorization") is None
            assert request.headers.get("Content-Type") == "audio/wav"
            return httpx.Response(200)

        if request.url.path == "/v1/diffio-2.0-generation":
            payload = json.loads(request.content.decode("utf-8"))
            assert payload["apiProjectId"] == "proj_abc"
            assert payload["sampling"]["steps"] == 10
            return httpx.Response(
                200,
                json={
                    "generationId": "gen_123",
                    "apiProjectId": "proj_abc",
                    "modelKey": "diffio-2",
                    "status": "queued",
                },
            )

        return httpx.Response(404, json={"error": "not found"})

    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(base_url="https://api.test", transport=transport)
    client = DiffioClient(apiKey="diffio_live_test", baseUrl="https://api.test", httpClient=http_client)

    file_path = tmp_path / "input.wav"
    file_path.write_bytes(b"test data")

    result = client.audio_isolation.isolate(
        filePath=str(file_path),
        contentType="audio/wav",
        model="diffio-2",
        sampling={"steps": 10},
    )

    assert result.project.apiProjectId == "proj_abc"
    assert result.generation.generationId == "gen_123"
    assert calls[0][0] == "POST"
    assert calls[1][0] == "PUT"
    assert calls[2][0] == "POST"


def test_restore_audio_runs_full_flow_and_downloads(tmp_path: Path, monkeypatch):
    status_sequence = ["queued", "processing", "complete"]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/create_project":
            payload = json.loads(request.content.decode("utf-8"))
            assert payload["fileName"] == "input.wav"
            return httpx.Response(
                200,
                json={
                    "apiProjectId": "proj_abc",
                    "uploadUrl": "https://upload.test/upload",
                    "uploadMethod": "PUT",
                    "objectPath": "users/u/projects/proj_abc/original/input.wav",
                    "bucket": "diffio_api",
                    "expiresAt": "2026-01-24T00:00:00Z",
                },
            )

        if request.url.host == "upload.test":
            assert request.headers.get("Authorization") is None
            assert request.headers.get("Content-Type") == "audio/wav"
            return httpx.Response(200)

        if request.url.path == "/v1/diffio-2.0-generation":
            payload = json.loads(request.content.decode("utf-8"))
            assert payload["apiProjectId"] == "proj_abc"
            assert payload["sampling"]["steps"] == 10
            return httpx.Response(
                200,
                json={
                    "generationId": "gen_123",
                    "apiProjectId": "proj_abc",
                    "modelKey": "diffio-2",
                    "status": "queued",
                },
            )

        if request.url.path == "/v1/get_generation_progress":
            status = status_sequence.pop(0)
            return httpx.Response(
                200,
                json={
                    "generationId": "gen_123",
                    "apiProjectId": "proj_abc",
                    "status": status,
                    "hasVideo": False,
                    "preProcessing": {
                        "jobId": "job-pre",
                        "jobState": "SUCCEEDED",
                        "status": "complete",
                        "progress": 100,
                        "statusMessage": None,
                        "error": None,
                        "errorDetails": None,
                    },
                    "inference": {
                        "jobId": "job-run",
                        "jobState": "RUNNING",
                        "status": "running",
                        "progress": 50,
                        "statusMessage": None,
                        "error": None,
                        "errorDetails": None,
                    },
                },
            )

        if request.url.path == "/v1/get_generation_download":
            payload = json.loads(request.content.decode("utf-8"))
            assert payload["apiProjectId"] == "proj_abc"
            assert payload["downloadType"] == "audio"
            return httpx.Response(
                200,
                json={
                    "generationId": "gen_123",
                    "apiProjectId": "proj_abc",
                    "downloadType": "audio",
                    "downloadUrl": "https://download.test/output.mp3",
                    "fileName": "diffio_ai_input.mp3",
                    "storagePath": "users/u/projects/proj_abc/generations/gen_123/restored.mp3",
                    "bucket": "diffio_api",
                    "mimeType": "audio/mpeg",
                },
            )

        if request.url.host == "download.test":
            return httpx.Response(200, content=b"restored-audio", headers={"Content-Type": "audio/mpeg"})

        return httpx.Response(404, json={"error": "not found"})

    import diffio.client as client_module

    monkeypatch.setattr(client_module.time, "sleep", lambda *_: None)

    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(base_url="https://api.test", transport=transport)
    client = DiffioClient(apiKey="diffio_live_test", baseUrl="https://api.test", httpClient=http_client)

    file_path = tmp_path / "input.wav"
    file_path.write_bytes(b"test data")

    content, info = client.restore_audio(
        filePath=str(file_path),
        contentType="audio/wav",
        model="diffio-2",
        sampling={"steps": 10},
        pollInterval=0.0,
    )

    assert content == b"restored-audio"
    assert info["apiProjectId"] == "proj_abc"
    assert info["generationId"] == "gen_123"
    assert info["status"] == "complete"
    assert info["downloadUrl"] == "https://download.test/output.mp3"
    assert info["error"] is None
    assert info["ok"] is True


def test_create_generation_rejects_unknown_model():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    http_client = httpx.Client(base_url="https://api.test", transport=transport)
    client = DiffioClient(apiKey="diffio_live_test", baseUrl="https://api.test", httpClient=http_client)

    with pytest.raises(ValueError):
        client.create_generation(apiProjectId="proj", model="diffio-unknown")


def test_get_generation_progress_payload_and_response():
    received = {}

    def handler(request: httpx.Request) -> httpx.Response:
        received["path"] = request.url.path
        payload = json.loads(request.content.decode("utf-8"))
        received["payload"] = payload
        return httpx.Response(
            200,
            json={
                "generationId": "gen_123",
                "apiProjectId": "proj_123",
                "status": "processing",
                "hasVideo": False,
                "preProcessing": {
                    "jobId": "job-pre",
                    "jobState": "SUCCEEDED",
                    "status": "complete",
                    "progress": 100,
                    "statusMessage": None,
                    "error": None,
                    "errorDetails": None,
                },
                "inference": {
                    "jobId": "job-run",
                    "jobState": "RUNNING",
                    "status": "running",
                    "progress": 40,
                    "statusMessage": "Processing audio",
                    "error": None,
                    "errorDetails": None,
                },
            },
        )

    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(base_url="https://api.test", transport=transport)
    client = DiffioClient(apiKey="diffio_live_test", baseUrl="https://api.test", httpClient=http_client)

    progress = client.generations.get_progress(generationId="gen_123", apiProjectId="proj_123")

    assert received["path"] == "/v1/get_generation_progress"
    assert received["payload"]["generationId"] == "gen_123"
    assert received["payload"]["apiProjectId"] == "proj_123"
    assert progress.status == "processing"
    assert progress.preProcessing.status == "complete"
    assert progress.inference.progress == 40
    assert progress.restoredVideo is None


def test_get_generation_download_payload_and_response():
    received = {}

    def handler(request: httpx.Request) -> httpx.Response:
        received["path"] = request.url.path
        payload = json.loads(request.content.decode("utf-8"))
        received["payload"] = payload
        return httpx.Response(
            200,
            json={
                "generationId": "gen_456",
                "apiProjectId": "proj_456",
                "downloadType": "audio",
                "downloadUrl": "https://storage.test/download",
                "fileName": "diffio_ai_input.wav",
                "storagePath": "users/u/projects/proj_456/generations/gen_456/restored.mp3",
                "bucket": "diffio_api",
                "mimeType": "audio/mpeg",
            },
        )

    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(base_url="https://api.test", transport=transport)
    client = DiffioClient(apiKey="diffio_live_test", baseUrl="https://api.test", httpClient=http_client)

    download = client.generations.get_download(
        generationId="gen_456",
        apiProjectId="proj_456",
        downloadType="mp3",
    )

    assert received["path"] == "/v1/get_generation_download"
    assert received["payload"]["generationId"] == "gen_456"
    assert received["payload"]["apiProjectId"] == "proj_456"
    assert received["payload"]["downloadType"] == "audio"
    assert download.downloadUrl == "https://storage.test/download"
    assert download.fileName == "diffio_ai_input.wav"
    assert download.mimeType == "audio/mpeg"


def test_generation_download_writes_file(tmp_path: Path):
    received = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/get_generation_download":
            payload = json.loads(request.content.decode("utf-8"))
            received["payload"] = payload
            return httpx.Response(
                200,
                json={
                    "generationId": "gen_789",
                    "apiProjectId": "proj_789",
                    "downloadType": "audio",
                    "downloadUrl": "https://download.test/restored.mp3",
                    "fileName": "restored.mp3",
                    "storagePath": "users/u/projects/proj_789/generations/gen_789/restored.mp3",
                    "bucket": "diffio_api",
                    "mimeType": "audio/mpeg",
                },
            )
        if request.url.host == "download.test":
            return httpx.Response(200, content=b"mp3-bytes")
        return httpx.Response(404, json={"error": "not found"})

    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(base_url="https://api.test", transport=transport)
    client = DiffioClient(apiKey="diffio_live_test", baseUrl="https://api.test", httpClient=http_client)

    output_path = tmp_path / "restored.mp3"
    download = client.generations.download(
        generationId="gen_789",
        apiProjectId="proj_789",
        downloadType="audio",
        downloadFilePath=str(output_path),
    )

    assert received["payload"]["downloadType"] == "audio"
    assert download.downloadUrl == "https://download.test/restored.mp3"
    assert output_path.read_bytes() == b"mp3-bytes"


def test_generation_download_warns_on_extension_mismatch(tmp_path: Path):
    received = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/get_generation_download":
            payload = json.loads(request.content.decode("utf-8"))
            received["payload"] = payload
            return httpx.Response(
                200,
                json={
                    "generationId": "gen_111",
                    "apiProjectId": "proj_111",
                    "downloadType": "audio",
                    "downloadUrl": "https://download.test/restored.mp3",
                    "fileName": "restored.mp3",
                    "storagePath": "users/u/projects/proj_111/generations/gen_111/restored.mp3",
                    "bucket": "diffio_api",
                    "mimeType": "audio/mpeg",
                },
            )
        if request.url.host == "download.test":
            return httpx.Response(200, content=b"mp3-bytes")
        return httpx.Response(404, json={"error": "not found"})

    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(base_url="https://api.test", transport=transport)
    client = DiffioClient(apiKey="diffio_live_test", baseUrl="https://api.test", httpClient=http_client)

    output_path = tmp_path / "restored.wav"
    with pytest.warns(UserWarning, match="downloadFilePath should end with"):
        client.generations.download(
            generationId="gen_111",
            apiProjectId="proj_111",
            downloadType="mp3",
            downloadFilePath=str(output_path),
        )

    assert received["payload"]["downloadType"] == "audio"
    assert output_path.read_bytes() == b"mp3-bytes"


def test_list_projects_payload_and_response():
    received = {}

    def handler(request: httpx.Request) -> httpx.Response:
        received["path"] = request.url.path
        received["auth"] = request.headers.get("Authorization")
        payload = json.loads(request.content.decode("utf-8"))
        received["payload"] = payload
        return httpx.Response(
            200,
            json={
                "projects": [
                    {
                        "apiProjectId": "proj_123",
                        "status": "uploaded",
                        "originalFileName": "song.wav",
                        "contentType": "audio/wav",
                        "hasVideo": False,
                        "generationCount": 2,
                        "createdAt": "2026-01-05T12:34:56Z",
                        "updatedAt": "2026-01-05T12:35:10Z",
                    }
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(base_url="https://api.test", transport=transport)
    client = DiffioClient(apiKey="diffio_live_test", baseUrl="https://api.test", httpClient=http_client)

    response = client.projects.list()

    assert received["path"] == "/v1/list_projects"
    assert received["auth"] == "Bearer diffio_live_test"
    assert received["payload"] == {}
    assert response.projects[0].apiProjectId == "proj_123"
    assert response.projects[0].generationCount == 2
    assert response.projects[0].hasVideo is False


def test_list_project_generations_payload_and_response():
    received = {}

    def handler(request: httpx.Request) -> httpx.Response:
        received["path"] = request.url.path
        payload = json.loads(request.content.decode("utf-8"))
        received["payload"] = payload
        return httpx.Response(
            200,
            json={
                "apiProjectId": "proj_123",
                "generations": [
                    {
                        "generationId": "gen_123",
                        "status": "processing",
                        "modelKey": "diffio-2",
                        "progress": None,
                        "createdAt": "2026-01-05T12:40:00Z",
                        "updatedAt": "2026-01-05T12:41:00Z",
                    }
                ],
            },
        )

    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(base_url="https://api.test", transport=transport)
    client = DiffioClient(apiKey="diffio_live_test", baseUrl="https://api.test", httpClient=http_client)

    response = client.projects.list_generations(apiProjectId="proj_123")

    assert received["path"] == "/v1/list_project_generations"
    assert received["payload"]["apiProjectId"] == "proj_123"
    assert response.apiProjectId == "proj_123"
    assert response.generations[0].generationId == "gen_123"
    assert response.generations[0].progress is None


def test_webhooks_send_test_event_payload_and_response():
    received = {}

    def handler(request: httpx.Request) -> httpx.Response:
        received["path"] = request.url.path
        payload = json.loads(request.content.decode("utf-8"))
        received["payload"] = payload
        return httpx.Response(
            200,
            json={
                "svixMessageId": "msg_123",
                "eventId": "evt_123",
                "eventType": "generation.completed",
                "mode": "live",
                "apiKeyId": "key_123",
            },
        )

    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(base_url="https://api.test", transport=transport)
    client = DiffioClient(apiKey="diffio_live_test", baseUrl="https://api.test", httpClient=http_client)

    response = client.webhooks.send_test_event(
        eventType="generation.completed",
        mode="live",
        apiKeyId="key_123",
        samplePayload={"apiProjectId": "proj_123"},
    )

    assert received["path"] == "/v1/webhooks/send_test_event"
    assert received["payload"]["eventType"] == "generation.completed"
    assert received["payload"]["mode"] == "live"
    assert received["payload"]["apiKeyId"] == "key_123"
    assert received["payload"]["samplePayload"]["apiProjectId"] == "proj_123"
    assert response.svixMessageId == "msg_123"
    assert response.eventId == "evt_123"
    assert response.eventType == "generation.completed"


def test_webhooks_send_test_event_rejects_invalid_type():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    http_client = httpx.Client(base_url="https://api.test", transport=transport)
    client = DiffioClient(apiKey="diffio_live_test", baseUrl="https://api.test", httpClient=http_client)

    with pytest.raises(ValueError):
        client.webhooks.send_test_event(
            eventType="generation.unknown",
            mode="test",
        )
