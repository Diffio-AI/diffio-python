import datetime
import io
import json
import os
import time

import httpx
import pytest
from svix.webhooks import Webhook

import diffio
from diffio import (
    DiffioApiError,
    DiffioClient,
    DiffioError,
    DiffioTimeoutError,
    ExportFailedError,
    Generation,
    GenerationFailedError,
    Page,
    Project,
    WebhookVerificationError,
)
from diffio.client import resolve_base_url
from fake_api import API, EDGE, FakeDiffioApi

KEY = "diffio_test_key"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(time, "sleep", lambda seconds: slept.append(seconds))
    return slept


@pytest.fixture
def fake():
    return FakeDiffioApi(api_key=KEY)


@pytest.fixture
def client(fake):
    return DiffioClient(KEY, base_url=API, http_client=fake.http_client())


def body(request):
    return json.loads(request.content) if request.content else None


def ready_generation(client, fake, tmp_path, model="diffio-flash"):
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"ID3" + b"x" * 100)
    project = client.projects.create(file_name="clip.mp3", size_bytes=103, content_type="audio/mpeg")
    client.projects.upload(project, audio)
    generation = client.generations.create(project.id, model=model)
    finished = client.generations.wait(project.id, generation.id)
    return project, finished


# ---- configuration -----------------------------------------------------------------------------------


def test_base_url_defaults_to_production_v2(monkeypatch):
    monkeypatch.delenv("DIFFIO_API_BASE_URL", raising=False)
    assert resolve_base_url() == "https://api.diffio.ai/v2"


@pytest.mark.parametrize("value,expected", [
    ("https://diffio-api-dev.web.app", "https://diffio-api-dev.web.app/v2"),
    ("https://diffio-api-dev.web.app/", "https://diffio-api-dev.web.app/v2"),
    ("https://diffio-api-dev.web.app/v2", "https://diffio-api-dev.web.app/v2"),
    ("https://api.diffio.ai/v1", "https://api.diffio.ai/v2"),
])
def test_base_url_env_overrides_host(monkeypatch, value, expected):
    monkeypatch.setenv("DIFFIO_API_BASE_URL", value)
    assert DiffioClient("k").base_url == expected


def test_api_key_comes_from_env(monkeypatch, fake):
    monkeypatch.setenv("DIFFIO_API_KEY", KEY)
    client = DiffioClient(base_url=API, http_client=fake.http_client())
    client.projects.list()
    assert fake.last().headers["Authorization"] == f"Bearer {KEY}"


def test_requests_name_the_sdk(client, fake):
    client.projects.list()
    headers = fake.last().headers
    assert headers["User-Agent"] == f"diffio-python/{diffio.__version__}"
    assert headers["X-Diffio-SDK-Language"] == "Python"
    assert headers["X-Diffio-SDK-Name"] == "diffio"
    assert headers["X-Diffio-SDK-Version"] == diffio.__version__ == "1.0.0"


def test_request_options_add_headers_and_override_key(fake):
    client = DiffioClient("wrong", base_url=API, http_client=fake.http_client())
    client.projects.list(request_options={"headers": {"X-Debug": "1"}, "api_key": KEY})
    assert fake.last().headers["X-Debug"] == "1"
    with pytest.raises(ValueError, match="Unknown request option"):
        client.projects.list(request_options={"maxRetries": 1})


def test_v1_methods_are_gone(client):
    for name in ("create_project", "create_generation", "get_generation_progress", "get_generation_download",
                 "list_projects", "audio_isolation", "account", "api_keys", "wait_for_generation"):
        assert not hasattr(client, name)


# ---- errors and retries --------------------------------------------------------------------------------


def test_api_error_carries_code_message_request_id_and_details(client):
    with pytest.raises(DiffioApiError) as caught:
        client.projects.get("missing")
    error = caught.value
    assert error.status == 404
    assert error.code == "project_not_found"
    assert error.message == "No project has that id."
    assert error.request_id == "req_fake"
    assert error.details == {"project_id": "missing"}
    assert "code=project_not_found" in str(error)
    assert isinstance(error, DiffioError)


def test_missing_and_invalid_keys_are_typed(fake):
    with pytest.raises(DiffioApiError) as missing:
        DiffioClient(base_url=API, http_client=fake.http_client()).projects.list()
    assert (missing.value.status, missing.value.code) == (401, "missing_api_key")
    with pytest.raises(DiffioApiError) as invalid:
        DiffioClient("nope", base_url=API, http_client=fake.http_client()).projects.list()
    assert invalid.value.code == "invalid_api_key"


def test_get_retries_5xx_and_network_errors(client, fake, no_sleep):
    fake.fail("GET", r"/v2/projects$", status=503, times=1)
    fake.fail("GET", r"/v2/projects$", exc=httpx.ConnectError("boom"), times=1)
    page = client.projects.list()
    assert isinstance(page, Page)
    assert len(fake.sent("GET", r"/v2/projects$")) == 3
    assert no_sleep == [0.5, 1.0]


def test_retry_after_is_honoured(client, fake, no_sleep):
    original = fake.handle

    def handle(request):
        if request.method == "GET" and not fake.sent("GET"):
            fake.requests.append(request)
            return httpx.Response(429, headers={"Retry-After": "3"}, json={"error": {"code": "rate_limited", "message": "Slow down."}})
        return original(request)

    client._http = httpx.Client(transport=httpx.MockTransport(handle))
    client.projects.list()
    assert no_sleep == [3.0]


def test_retries_stop_after_max_retries(fake):
    client = DiffioClient(KEY, base_url=API, http_client=fake.http_client(), max_retries=1)
    fake.fail("GET", r"/v2/usage", status=500, code="internal_error", times=5)
    with pytest.raises(DiffioApiError) as caught:
        client.usage.get()
    assert caught.value.code == "internal_error"
    assert len(fake.sent("GET", r"/v2/usage")) == 2


def test_network_error_after_retries_is_a_connection_error(client, fake):
    fake.fail("GET", r"/v2/usage", exc=httpx.ConnectError("down"), times=5)
    with pytest.raises(DiffioApiError) as caught:
        client.usage.get()
    assert caught.value.code == "connection_error"
    assert caught.value.status is None


def test_post_without_idempotency_is_not_retried(client, fake, tmp_path):
    project, generation = ready_generation(client, fake, tmp_path, model="diffio-pro")
    fake.fail("POST", r"/assistant/turns$", status=503, times=1)
    with pytest.raises(DiffioApiError):
        client.assistant.create_turn(project.id, generation.id, messages=[{"role": "user", "content": "Tidy it"}])
    assert len(fake.sent("POST", r"/assistant/turns$")) == 1


# ---- models ------------------------------------------------------------------------------------------


def test_models_list_needs_no_key(fake):
    client = DiffioClient(base_url=API, http_client=fake.http_client(), end_user="customer-1")
    models = client.models.list()
    assert [m.id for m in models][:2] == ["diffio-4.7-flash", "diffio-4.7-pro"]
    assert models.data[2].status == "alias" and models.data[2].served_by == "diffio-4.7-flash"
    assert models.next_cursor is None
    assert "Authorization" not in fake.last().headers
    assert "Diffio-End-User" not in fake.last().headers


# ---- projects ----------------------------------------------------------------------------------------


def test_projects_create_sends_snake_case_body_and_headers(client, fake):
    project = client.projects.create(
        file_name="interview.mp3",
        size_bytes=510957,
        content_type="audio/mpeg",
        metadata={"order_id": "1042"},
        end_user="customer-42",
        idempotency_key="order-1042",
    )
    request = fake.last("POST", r"/v2/projects$")
    assert body(request) == {"file_name": "interview.mp3", "content_type": "audio/mpeg", "size_bytes": 510957,
                             "metadata": {"order_id": "1042"}}
    assert request.headers["Idempotency-Key"] == "order-1042"
    assert request.headers["Diffio-End-User"] == "customer-42"
    assert request.headers["Content-Type"] == "application/json"
    assert isinstance(project, Project)
    assert project.status == "awaiting_upload"
    assert project.end_user == "customer-42"
    assert project.upload.part_size_bytes == 32 * 1024 * 1024
    assert project.upload.requests["part"].method == "PUT"
    replay = client.projects.create(file_name="interview.mp3", size_bytes=510957, content_type="audio/mpeg",
                                    metadata={"order_id": "1042"}, end_user="customer-42", idempotency_key="order-1042")
    assert replay.id == project.id
    with pytest.raises(DiffioApiError) as conflict:
        client.projects.create(file_name="other.mp3", end_user="customer-42", idempotency_key="order-1042")
    assert conflict.value.code == "idempotency_key_conflict"


def test_projects_create_sends_its_own_idempotency_key_and_retries_with_it(client, fake):
    fake.fail("POST", r"/v2/projects$", status=503, times=1)
    project = client.projects.create(file_name="a.wav", generation="diffio-pro")
    sent = fake.sent("POST", r"/v2/projects$")
    assert len(sent) == 2
    assert sent[0].headers["Idempotency-Key"].startswith("diffio-python-")
    assert sent[0].headers["Idempotency-Key"] == sent[1].headers["Idempotency-Key"]
    assert body(sent[1])["generation"] == {"model": "diffio-pro"}
    assert project.latest_generation.status == "queued"
    assert project.generation_count == 1


def test_client_end_user_is_sent_and_a_call_can_override_it(fake):
    client = DiffioClient(KEY, base_url=API, http_client=fake.http_client(), end_user="customer-1")
    client.projects.create(file_name="a.mp3")
    assert fake.last().headers["Diffio-End-User"] == "customer-1"
    client.projects.list(end_user="customer-2")
    assert fake.last().headers["Diffio-End-User"] == "customer-2"


def test_projects_create_requires_file_name(client):
    with pytest.raises(ValueError):
        client.projects.create(file_name="")


def test_upload_follows_the_upload_requests_and_completes(client, fake, tmp_path):
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"ID3" + bytes(range(256)) * 4)
    project = client.projects.create(file_name="clip.mp3", size_bytes=audio.stat().st_size)
    ready = client.projects.upload(project, audio)
    assert fake.objects[project.id] == audio.read_bytes()
    edge = [r for r in fake.requests if str(r.url).startswith(EDGE)]
    assert [(r.method, r.url.path) for r in edge] == [
        ("POST", "/v1/uploads/start"), ("PUT", "/v1/uploads/parts/1"), ("POST", "/v1/uploads/complete")]
    for request in edge:
        assert request.headers["Authorization"] == f"Bearer {project.upload.upload_token}"
        assert KEY not in request.headers["Authorization"]
    assert edge[1].headers["Content-Length"] == str(audio.stat().st_size)
    assert body(edge[2]) == {"uploadId": edge[1].url.params["uploadId"], "parts": [{"partNumber": 1, "etag": "etag-1"}]}
    assert fake.last("POST", r"/upload/complete$") is not None
    assert ready.status == "ready"


def test_upload_splits_into_parts_of_the_announced_size(fake, tmp_path):
    fake.part_size = 10
    client = DiffioClient(KEY, base_url=API, http_client=fake.http_client())
    data = bytes(range(25))
    project = client.projects.create(file_name="clip.wav")
    client.projects.upload(project, data)
    parts = fake.sent("PUT", r"/v1/uploads/parts/")
    assert [len(r.content) for r in parts] == [10, 10, 5]
    assert fake.objects[project.id] == data
    assert [p["partNumber"] for p in body(fake.last("POST", r"/v1/uploads/complete"))["parts"]] == [1, 2, 3]


def test_upload_of_an_exact_multiple_sends_no_empty_tail(fake):
    fake.part_size = 10
    client = DiffioClient(KEY, base_url=API, http_client=fake.http_client())
    project = client.projects.create(file_name="clip.wav")
    client.projects.upload(project, bytes(20))
    assert [len(r.content) for r in fake.sent("PUT", r"/parts/")] == [10, 10]


def test_upload_accepts_a_binary_file_object(client, fake):
    project = client.projects.create(file_name="clip.wav")
    client.projects.upload(project, io.BytesIO(b"RIFF-data"))
    assert fake.objects[project.id] == b"RIFF-data"
    with pytest.raises(ValueError, match="binary"):
        client.projects.upload(client.projects.create(file_name="b.wav"), io.StringIO("text"))


def test_upload_retries_only_the_failed_part(fake):
    fake.part_size = 4
    client = DiffioClient(KEY, base_url=API, http_client=fake.http_client())
    project = client.projects.create(file_name="clip.wav")
    fake.fail("PUT", r"/parts/2\?", status=503, times=1)
    client.projects.upload(project, b"aaaabbbbcc")
    numbers = [r.url.path.rsplit("/", 1)[1] for r in fake.sent("PUT", r"/parts/")]
    assert numbers == ["1", "2", "2", "3"]
    assert fake.objects[project.id] == b"aaaabbbbcc"


def test_upload_aborts_when_a_part_keeps_failing(client, fake):
    project = client.projects.create(file_name="clip.wav")
    fake.fail("PUT", r"/parts/1\?", status=500, code="internal_error", times=10)
    with pytest.raises(DiffioApiError):
        client.projects.upload(project, b"data")
    assert fake.uploads[project.id]["aborted"] is True
    assert fake.last("POST", r"/upload/complete$") is None


def test_upload_refuses_a_file_over_max_bytes_before_sending(client, fake):
    project = client.projects.create(file_name="clip.wav")
    project.upload._data["max_bytes"] = 3
    object.__setattr__(project.upload, "max_bytes", 3)
    with pytest.raises(ValueError, match="upload limit"):
        client.projects.upload(project, b"four")
    assert not fake.sent(pattern=EDGE)


def test_upload_by_project_id_reopens_the_upload(client, fake):
    project = client.projects.create(file_name="clip.wav")
    result = client.projects.upload(project.id, b"bytes", complete=False)
    assert result is None
    assert fake.last("POST", r"/upload$") is not None
    assert fake.last("POST", r"/upload/complete$") is None
    assert fake.objects[project.id] == b"bytes"


def test_upload_without_requests_uses_the_documented_recipe(client, fake):
    fake.omit_upload_requests = True
    project = client.projects.create(file_name="clip.wav")
    assert project.upload.requests is None
    client.projects.upload(project, b"bytes")
    assert fake.objects[project.id] == b"bytes"


def test_reopen_upload_and_its_refusal(client, fake):
    project = client.projects.create(file_name="clip.wav")
    reopened = client.projects.reopen_upload(project.id)
    assert reopened.upload.upload_token != project.upload.upload_token
    client.projects.upload(reopened, b"x")
    with pytest.raises(DiffioApiError) as caught:
        client.projects.reopen_upload(project.id)
    assert caught.value.code == "upload_already_received"
    assert caught.value.details == {"project_status": "ready"}


def test_complete_upload_before_bytes_is_upload_missing(client):
    project = client.projects.create(file_name="clip.wav")
    with pytest.raises(DiffioApiError) as caught:
        client.projects.complete_upload(project.id)
    assert (caught.value.status, caught.value.code) == (409, "upload_missing")


def test_projects_list_iter_get_and_delete(client, fake):
    ids = [client.projects.create(file_name=f"f{i}.mp3").id for i in range(5)]
    page = client.projects.list(limit=2)
    assert [p.id for p in page] == [ids[4], ids[3]]
    assert page.next_cursor == ids[3] and page.has_more
    assert fake.last().url.params["limit"] == "2"
    second = client.projects.list(limit=2, cursor=page.next_cursor)
    assert [p.id for p in second] == [ids[2], ids[1]]
    assert [p.id for p in client.projects.iter(limit=2)] == list(reversed(ids))
    assert len(fake.sent("GET", r"/v2/projects\?")) == 2 + 3
    got = client.projects.get(ids[0])
    assert got.file_name == "f0.mp3" and got.latest_generation is None
    deleted = client.projects.delete(ids[0])
    assert deleted.deleted is True and deleted.id == ids[0]
    with pytest.raises(DiffioApiError) as caught:
        client.projects.get(ids[0])
    assert caught.value.code == "project_not_found"


def test_project_ids_are_path_encoded(client, fake):
    with pytest.raises(DiffioApiError):
        client.projects.get("a/b")
    assert fake.last().url.raw_path == b"/v2/projects/a%2Fb"


# ---- generations ---------------------------------------------------------------------------------------


def test_generations_create_wait_list_get(client, fake, tmp_path):
    project = client.projects.create(file_name="clip.mp3")
    client.projects.upload(project, b"ID3data")
    generation = client.generations.create(project.id, model="diffio-flash", label="Take 1")
    request = fake.last("POST", r"/generations$")
    assert body(request) == {"model": "diffio-flash", "label": "Take 1"}
    assert request.headers["Idempotency-Key"].startswith("diffio-python-")
    assert isinstance(generation, Generation)
    assert generation.model == "diffio-4.7-flash" and generation.requested_model == "diffio-flash"
    seen = []
    finished = client.generations.wait(project.id, generation.id, on_progress=lambda g: seen.append(g.status))
    assert seen == ["processing", "complete"]
    assert finished.status == "complete" and finished.files_available == ["audio", "transcript"]
    assert seen and seen[0] == "processing"
    assert [g.id for g in client.generations.list(project.id)] == [generation.id]
    assert [g.id for g in client.generations.iter(project.id, limit=1)] == [generation.id]
    assert client.generations.get(project, generation).id == generation.id


def test_generations_create_retry_keeps_the_key(client, fake):
    project = client.projects.create(file_name="clip.mp3")
    fake.fail("POST", r"/generations$", exc=httpx.ReadTimeout("lost"), times=1)
    generation = client.generations.create(project.id, model="diffio-pro", idempotency_key="restore-7")
    sent = fake.sent("POST", r"/generations$")
    assert [r.headers["Idempotency-Key"] for r in sent] == ["restore-7", "restore-7"]
    assert generation.status == "queued"


def test_generations_create_unknown_model(client):
    project = client.projects.create(file_name="clip.mp3")
    with pytest.raises(DiffioApiError) as caught:
        client.generations.create(project.id, model="diffio-9-ultra")
    assert caught.value.code == "unknown_model"
    assert "diffio-flash" in caught.value.details["models"]
    with pytest.raises(ValueError):
        client.generations.create(project.id, model="")


def test_wait_raises_generation_failed(client, fake):
    fake.fail_generation_with = "no_speech_detected"
    project = client.projects.create(file_name="clip.mp3")
    client.projects.upload(project, b"x")
    generation = client.generations.create(project.id, model="diffio-flash")
    with pytest.raises(GenerationFailedError) as caught:
        client.generations.wait(project.id, generation.id)
    assert caught.value.code == "no_speech_detected"
    assert caught.value.generation.status == "failed"
    assert caught.value.retryable is False


def test_wait_times_out(client, fake, monkeypatch):
    fake.polls_to_complete = 10 ** 6
    project = client.projects.create(file_name="clip.mp3")
    client.projects.upload(project, b"x")
    generation = client.generations.create(project.id, model="diffio-flash")
    clock = iter(range(0, 10 ** 6, 5))
    monkeypatch.setattr("diffio.resources.time.monotonic", lambda: next(clock))
    with pytest.raises(DiffioTimeoutError) as caught:
        client.generations.wait(project.id, generation.id, timeout=12)
    assert caught.value.last.status == "processing"


def test_media_and_download(client, fake, tmp_path):
    project, generation = ready_generation(client, fake, tmp_path)
    media = client.generations.media(project.id, generation.id, preview_seconds=8)
    assert fake.last().url.params["preview_seconds"] == "8"
    assert media.files["audio"].startswith(media.base_url)
    out = tmp_path / "restored.mp3"
    saved = client.generations.download(project.id, generation.id, path=out)
    assert saved == str(out) and out.read_bytes() == b"ID3restored-audio"
    umask = os.umask(0)
    os.umask(umask)
    assert out.stat().st_mode & 0o777 == 0o666 & ~umask
    assert "Authorization" not in fake.last("GET", r"restored\.mp3").headers
    assert client.generations.download(project.id, generation.id) == b"ID3restored-audio"
    folder = tmp_path / "out"
    folder.mkdir()
    assert client.generations.download(project.id, generation.id, path=folder).endswith("restored.mp3")
    with pytest.raises(DiffioError, match="video is not available"):
        client.generations.download(project.id, generation.id, file="video", path=tmp_path / "v.mp4")
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".diffio-download-")] == []


def test_download_media_host_errors_are_typed(client, fake, tmp_path):
    project, generation = ready_generation(client, fake, tmp_path)
    fake.fail("GET", r"restored\.mp3", status=403, code="preview_limited", times=1)
    with pytest.raises(DiffioApiError) as caught:
        client.generations.download(project.id, generation.id, path=tmp_path / "x.mp3")
    assert (caught.value.status, caught.value.code) == (403, "preview_limited")
    assert not (tmp_path / "x.mp3").exists()


# ---- edits, mix, exports, transcripts, assistant ---------------------------------------------------------


def test_edits_estimate_and_create(client, fake, tmp_path):
    project, generation = ready_generation(client, fake, tmp_path, model="diffio-pro")
    words = [{"id": "word-0", "text": ""}, {"id": "word-1", "text": "there", "start": 0.84, "end": 0.94}]
    estimate = client.edits.estimate(project.id, generation.id, words=words)
    assert body(fake.last()) == {"words": words}
    assert estimate.max_billable_seconds == 30 and estimate.window_seconds == 30
    edit = client.edits.create(project.id, generation.id, words=words, request_id="edit-1",
                               tighten={"close_removed_gaps": True}, idempotency_key="edit-key")
    request = fake.last()
    assert body(request) == {"words": words, "tighten": {"close_removed_gaps": True}, "request_id": "edit-1"}
    assert request.headers["Idempotency-Key"] == "edit-key"
    assert edit.kind == "edit" and edit.source_generation_id == generation.id
    assert edit.edit.request_id == "edit-1"


def test_mix_get_save_and_conflict(client, fake, tmp_path):
    project, generation = ready_generation(client, fake, tmp_path)
    mix = client.mix.get(project.id, generation.id)
    assert mix.background_gain == 0 and mix.revision == "rev:0"
    saved = client.mix.save(project.id, generation.id, background_gain=0.3, revision=mix.revision)
    assert fake.last().method == "PUT"
    assert body(fake.last()) == {"background_gain": 0.3, "revision": "rev:0"}
    assert saved.revision == "rev:1"
    with pytest.raises(DiffioApiError) as caught:
        client.mix.save(project.id, generation.id, background_gain=0.5, revision="rev:0")
    assert caught.value.code == "revision_conflict"
    assert caught.value.details == {"current_revision": "rev:1"}


def test_exports_create_get_wait(client, fake, tmp_path):
    project, generation = ready_generation(client, fake, tmp_path)
    export = client.exports.create(project.id, generation.id, artifact="mix", format="mp3", file_name="interview mix")
    assert body(fake.last()) == {"artifact": "mix", "format": "mp3", "file_name": "interview mix"}
    assert export.status == "queued" and export.url is None
    ready = client.exports.wait(project.id, generation.id, export.id, file_name="interview mix")
    assert ready.status == "ready" and "download=interview" in ready.url
    assert fake.last().url.params["file_name"] == "interview mix"
    again = client.exports.get(project.id, generation.id, export.id)
    assert again.status == "ready"


def test_exports_wait_raises_on_failure(client, fake, tmp_path):
    project, generation = ready_generation(client, fake, tmp_path)
    export = client.exports.create(project.id, generation.id, artifact="speech", format="wav")
    fake.exports[export.id].update(status="failed", error={"code": "export_failed", "message": "Render failed."})
    with pytest.raises(ExportFailedError) as caught:
        client.exports.wait(project.id, generation.id, export.id)
    assert caught.value.code == "export_failed"
    assert caught.value.export.id == export.id


def test_transcripts_and_drafts(client, fake, tmp_path):
    project, generation = ready_generation(client, fake, tmp_path)
    transcript = client.transcripts.get(project.id, generation.id, preview_seconds=8)
    assert fake.last().url.params["preview_seconds"] == "8"
    assert transcript.text == "Hello there"
    assert transcript.words[0].id == "word-0" and transcript.words[0]["language"] == "en"
    draft = client.transcripts.get_draft(project.id, generation.id)
    assert draft.revision == "0" and draft.data is None
    saved = client.transcripts.save_draft(project.id, generation.id, data={"cursor": 1}, revision=draft.revision)
    assert body(fake.last()) == {"data": {"cursor": 1}, "revision": "0"}
    assert saved.revision == "1" and saved.data == {"cursor": 1}
    with pytest.raises(DiffioApiError) as caught:
        client.transcripts.delete_draft(project.id, generation.id, revision="0")
    assert caught.value.code == "revision_conflict"
    deleted = client.transcripts.delete_draft(project.id, generation.id, revision="1")
    assert fake.last().url.params["revision"] == "1"
    assert deleted.deleted is True and deleted.revision == "2"


def test_assistant_turns(client, fake, tmp_path):
    project, generation = ready_generation(client, fake, tmp_path, model="diffio-pro")
    turn = client.assistant.create_turn(project.id, generation.id,
                                        messages=[{"role": "user", "content": "Remove filler words"}],
                                        selection={"word_ids": ["word-0"]}, playhead_seconds=1.5)
    assert body(fake.last()) == {"messages": [{"role": "user", "content": "Remove filler words"}],
                                 "selection": {"word_ids": ["word-0"]}, "playhead_seconds": 1.5}
    assert turn.status == "processing"
    assert client.assistant.get_turn(project.id, generation.id, turn.id).status == "processing"
    done = client.assistant.wait_turn(project.id, generation.id, turn.id)
    assert done.status == "complete" and done.reply == "Removed one filler word."


# ---- shares, usage, webhooks ----------------------------------------------------------------------------


def test_shares_publish_get_public_and_revoke(client, fake, tmp_path):
    project, generation = ready_generation(client, fake, tmp_path)
    share = client.shares.publish(project.id, preview_seconds=8)
    assert body(fake.last()) == {"preview_seconds": 8}
    assert share.generation_id == generation.id and share.files["audio"].startswith(share.base_url)
    assert client.shares.get(project.id).id == share.id
    public = client.shares.get_public(share.id)
    assert "Authorization" not in fake.last().headers
    assert public.id == share.id
    revoked = client.shares.revoke(project.id)
    assert revoked.deleted is True and revoked.id == share.id
    with pytest.raises(DiffioApiError) as caught:
        client.shares.get_public(share.id)
    assert caught.value.code == "share_not_found"


def test_usage_formats_dates(client, fake):
    usage = client.usage.get(start=datetime.date(2026, 11, 1),
                             end=datetime.datetime(2026, 11, 12, 18, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=-7))))
    params = fake.last().url.params
    assert params["start"] == "2026-11-01"
    assert params["end"] == "2026-11-13T01:00:00Z"
    assert usage.billable_seconds == 60 and usage.by_model[0].model == "diffio-4.7-flash"
    client.usage.get()
    assert "start" not in fake.last().url.params


def test_webhooks_portal_never_sends_the_end_user(fake):
    client = DiffioClient(KEY, base_url=API, http_client=fake.http_client(), end_user="customer-1")
    portal = client.webhooks.portal()
    assert "Diffio-End-User" not in fake.last().headers
    assert portal.url.startswith("https://portal.test")


WEBHOOK_SECRET = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"
EVENT = {"type": "generation.completed", "id": "evt_0123456789abcdef0123456789abcdef", "created_at": "2026-11-13T00:33:41Z",
         "data": {"object": "generation", "id": "gen_1", "project_id": "proj_1", "end_user": "customer-42",
                  "status": "complete", "kind": "restore"}}


def _svix_signed(payload, msg_id="msg_1", when=None):
    when = when or datetime.datetime.now(datetime.timezone.utc)
    signature = Webhook(WEBHOOK_SECRET).sign(msg_id, when, payload)
    return {"svix-id": msg_id, "svix-timestamp": str(int(when.timestamp())), "svix-signature": signature}


def test_verify_signature_accepts_svix_signed_events(client):
    payload = json.dumps(EVENT)
    headers = _svix_signed(payload)
    event = client.webhooks.verify_signature(payload.encode(), headers, WEBHOOK_SECRET)
    assert event.type == "generation.completed" and event.data.end_user == "customer-42"
    upper = {key.upper(): value for key, value in headers.items()}
    assert client.webhooks.verify_signature(payload, upper, WEBHOOK_SECRET).id == EVENT["id"]
    rotated = dict(headers, **{"svix-signature": "v1,bm90LXRoaXM= " + headers["svix-signature"]})
    assert diffio.verify_webhook_signature(payload, rotated, WEBHOOK_SECRET).data.id == "gen_1"


@pytest.mark.parametrize("mutate,message", [
    (lambda p, h: (p.replace("complete", "failed"), h), "No matching"),
    (lambda p, h: (p, {k: v for k, v in h.items() if k != "svix-signature"}), "Missing webhook header svix-signature"),
    (lambda p, h: (p, dict(h, **{"svix-timestamp": str(int(time.time()) - 3600)})), "too old"),
    (lambda p, h: (p, dict(h, **{"svix-timestamp": "soon"})), "Invalid webhook timestamp"),
])
def test_verify_signature_refuses_bad_events(client, mutate, message):
    payload = json.dumps(EVENT)
    tampered_payload, tampered_headers = mutate(payload, _svix_signed(payload))
    with pytest.raises(WebhookVerificationError, match=message):
        client.webhooks.verify_signature(tampered_payload, tampered_headers, WEBHOOK_SECRET)


def test_verify_signature_refuses_a_wrong_secret(client):
    payload = json.dumps(EVENT)
    with pytest.raises(WebhookVerificationError):
        client.webhooks.verify_signature(payload, _svix_signed(payload), "whsec_" + "A" * 32)
    with pytest.raises(WebhookVerificationError, match="secret"):
        client.webhooks.verify_signature(payload, _svix_signed(payload), "")


# ---- one call restore -----------------------------------------------------------------------------------


def test_restore_audio_runs_the_whole_flow(client, fake, tmp_path):
    audio = tmp_path / "interview.mp3"
    audio.write_bytes(b"ID3" + b"\0" * 64)
    progress = []
    result = client.restore_audio(audio, model="diffio-pro", output_path=tmp_path / "restored.mp3",
                                  on_progress=lambda g: progress.append(g.status), end_user="customer-9")
    create = fake.sent("POST", r"/v2/projects$")[0]
    assert body(create) == {"file_name": "interview.mp3", "content_type": "audio/mpeg", "size_bytes": 67,
                            "generation": {"model": "diffio-pro"}}
    assert create.headers["Diffio-End-User"] == "customer-9"
    assert not fake.sent("POST", r"/generations$")
    assert fake.objects[result.project.id] == audio.read_bytes()
    assert result.generation.status == "complete" and result.generation.model == "diffio-4.7-pro"
    assert result.output_path == str(tmp_path / "restored.mp3")
    assert (tmp_path / "restored.mp3").read_bytes() == b"ID3restored-audio"
    assert progress[-1] == "complete"
    assert all(r.headers.get("Diffio-End-User") == "customer-9" for r in fake.requests if str(r.url).startswith(API))


def test_restore_audio_returns_bytes_without_output_path(client, fake):
    result = client.restore_audio(b"RIFFdata", file_name="take.wav")
    assert result.content == b"ID3restored-audio" and result.output_path is None
    assert body(fake.sent("POST", r"/v2/projects$")[0])["generation"] == {"model": "diffio-flash"}
    with pytest.raises(ValueError, match="file_name"):
        client.restore_audio(b"RIFFdata")


def test_restore_audio_raises_when_the_generation_fails(client, fake, tmp_path):
    fake.fail_generation_with = "restoration_failed"
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")
    with pytest.raises(GenerationFailedError) as caught:
        client.restore_audio(audio, output_path=tmp_path / "out.mp3")
    assert caught.value.code == "restoration_failed"
    assert not (tmp_path / "out.mp3").exists()


# ---- response objects -----------------------------------------------------------------------------------


def test_response_objects_keep_unknown_fields_and_wire_names():
    project = Project({"id": "p", "status": "ready", "latest_generation": {"id": "g", "status": "failed",
                       "error": {"code": "timed_out", "message": "Too slow.", "retryable": True, "explanation": None}},
                       "brand_new_field": {"x": 1}})
    assert project["brand_new_field"] == {"x": 1}
    assert project.brand_new_field == {"x": 1}
    assert project.latest_generation.error.retryable is True
    assert project.upload is None and project.get("upload", "none") == "none"
    assert project.to_dict()["latest_generation"]["error"]["code"] == "timed_out"
    assert Generation({"id": "g", "is_finished": "wire"}).is_finished is False
    with pytest.raises(KeyError):
        project["nope"]


def test_download_restarts_after_a_dropped_body(client, fake, tmp_path):
    project, generation = ready_generation(client, fake, tmp_path)
    original = fake.handle
    calls = []

    class Broken(httpx.SyncByteStream):
        def __iter__(self):
            yield b"ID3par"
            raise httpx.ReadError("dropped")

    def handle(request):
        if request.url.path.endswith("restored.mp3"):
            calls.append(1)
            if len(calls) == 1:
                return httpx.Response(200, stream=Broken())
        return original(request)

    client._http = httpx.Client(transport=httpx.MockTransport(handle))
    out = tmp_path / "again.mp3"
    client.generations.download(project.id, generation.id, path=out)
    assert out.read_bytes() == b"ID3restored-audio" and len(calls) == 2
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".diffio-download-")] == []
