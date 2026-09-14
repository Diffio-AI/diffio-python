import base64
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from svix.webhooks import Webhook

from diffio import DiffioApiError, DiffioClient, GenerationTranscription, TranscriptionStatus


def completed_progress(transcription_status):
    payload = {
        "generationId": "gen_123",
        "apiProjectId": "proj_123",
        "status": "complete",
        "hasVideo": False,
        "preProcessing": {"status": "complete", "progress": 100},
        "inference": {"status": "complete", "progress": 100},
    }
    if transcription_status is not None:
        payload["transcription"] = {"status": transcription_status}
    return payload


@pytest.mark.parametrize("transcription_status", [None, *TranscriptionStatus])
@pytest.mark.parametrize("method", ["wait_for_generation", "wait_for_complete"])
def test_media_completion_does_not_wait_for_transcription(transcription_status, method):
    requests = []
    progress_updates = []

    def handler(request):
        requests.append(request)
        assert request.url.path == "/v1/get_generation_progress"
        return httpx.Response(200, json=completed_progress(transcription_status))

    with httpx.Client(base_url="https://api.test", transport=httpx.MockTransport(handler)) as http_client:
        client = DiffioClient(apiKey="diffio_test", httpClient=http_client)
        wait = client.wait_for_generation if method == "wait_for_generation" else client.generations.wait_for_complete
        progress = wait(
            generationId="gen_123",
            apiProjectId="proj_123",
            pollInterval=0,
            timeout=5,
            onProgress=progress_updates.append,
        )

    assert len(requests) == 1
    assert progress_updates == [progress]
    assert progress.status == "complete"
    if transcription_status is None:
        assert progress.transcription is None
    else:
        assert isinstance(progress.transcription, GenerationTranscription)
        assert progress.transcription.status == transcription_status


@pytest.mark.parametrize("transcription_status", [None, *TranscriptionStatus])
def test_verified_webhook_preserves_transcription_state(transcription_status):
    payload = {
        "eventType": "generation.completed",
        "eventId": "evt_123",
        "createdAt": "2026-09-14T00:00:00Z",
        "apiKeyId": "key_123",
        "apiProjectId": "proj_123",
        "generationId": "gen_123",
        "status": "complete",
        "modelKey": "diffio-2",
        "hasVideo": False,
    }
    if transcription_status is not None:
        payload["transcription"] = {"status": transcription_status}
    body = json.dumps(payload)
    secret = "whsec_" + base64.b64encode(b"test-webhook-secret").decode("ascii")
    timestamp = datetime.now(timezone.utc)
    headers = {
        "svix-id": "msg_123",
        "svix-timestamp": str(int(timestamp.timestamp())),
        "svix-signature": Webhook(secret).sign("msg_123", timestamp, body),
    }

    with DiffioClient(apiKey="diffio_test") as client:
        event = client.webhooks.verify_signature(payload=body.encode("utf-8"), headers=headers, secret=secret)

    assert event.status == "complete"
    assert event.eventType == "generation.completed"
    if transcription_status is None:
        assert event.transcription is None
    else:
        assert isinstance(event.transcription, GenerationTranscription)
        assert event.transcription.status == transcription_status


@pytest.fixture(params=[
    (409, "TRANSCRIPT_PENDING", "pending", "Transcript is not ready yet."),
    (404, "TRANSCRIPT_UNAVAILABLE", "unavailable", "Transcript is unavailable."),
])
def transcript_error(request):
    status_code, code, status, message = request.param
    return status_code, {
        "error": message,
        "code": code,
        "transcription": {"status": status},
    }


@pytest.mark.parametrize("method", ["get_download", "download"])
def test_transcript_download_preserves_availability_errors(tmp_path, transcript_error, method):
    status_code, body = transcript_error
    requests = []
    output_path = tmp_path / "word_timestamps.json"
    output_path.write_text("existing transcript", encoding="utf-8")

    def handler(request):
        requests.append(request)
        assert request.url.path == "/v1/get_generation_download"
        assert json.loads(request.content)["downloadType"] == "transcript"
        return httpx.Response(status_code, json=body)

    with httpx.Client(base_url="https://api.test", transport=httpx.MockTransport(handler)) as http_client:
        client = DiffioClient(apiKey="diffio_test", httpClient=http_client)
        arguments = {
            "generationId": "gen_123",
            "apiProjectId": "proj_123",
            "downloadType": "transcript",
        }
        if method == "download":
            arguments["downloadFilePath"] = output_path
        with pytest.raises(DiffioApiError) as raised:
            getattr(client.generations, method)(**arguments)

    assert len(requests) == 1
    assert raised.value.message == body["error"]
    assert raised.value.statusCode == status_code
    assert raised.value.responseBody == body
    assert output_path.read_text(encoding="utf-8") == "existing transcript"


@pytest.mark.parametrize("raise_on_error", [False, True])
def test_restore_transcript_preserves_errors_after_media_completion(monkeypatch, transcript_error, raise_on_error):
    status_code, body = transcript_error
    requests = []

    def handler(request):
        requests.append(request.url.path)
        if request.url.path == "/v1/get_generation_progress":
            return httpx.Response(200, json=completed_progress("pending"))
        assert request.url.path == "/v1/get_generation_download"
        assert json.loads(request.content)["downloadType"] == "transcript"
        return httpx.Response(status_code, json=body)

    with httpx.Client(base_url="https://api.test", transport=httpx.MockTransport(handler)) as http_client:
        client = DiffioClient(apiKey="diffio_test", httpClient=http_client)
        monkeypatch.setattr(client.audio_isolation, "isolate", lambda **kwargs: SimpleNamespace(
            project=SimpleNamespace(apiProjectId="proj_123"),
            generation=SimpleNamespace(generationId="gen_123"),
        ))
        arguments = {
            "filePath": "sample.wav",
            "model": "diffio-2",
            "downloadType": "transcript",
            "raiseOnError": raise_on_error,
        }
        if raise_on_error:
            with pytest.raises(DiffioApiError) as raised:
                client.restore_audio(**arguments)
            assert raised.value.statusCode == status_code
            assert raised.value.responseBody == body
            info = raised.value.restoreInfo
        else:
            content, info = client.restore_audio(**arguments)
            assert content is None

    assert requests == ["/v1/get_generation_progress", "/v1/get_generation_download"]
    assert info["status"] == "complete"
    assert info["progress"].transcription.status == "pending"
    assert info["stage"] == "download_info"
    assert info["ok"] is False
    assert info["exceptionType"] == "DiffioApiError"
    assert info["statusCode"] == status_code
    assert info["responseBody"] == body
