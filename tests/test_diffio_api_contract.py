import json

import pytest

from diffio import DiffioApiError, DiffioClient, RequestOptions
from diffio_api_contract_fake import (
    FAKE_RESTORED_AUDIO_BYTES,
    FakeDiffioApiContract,
    read_stored_upload_bytes,
)


def _build_contract_client(fake, requestOptions=None):
    return DiffioClient(apiKey=fake.apiKey, httpClient=fake.build_http_client(), requestOptions=requestOptions)


def _edge_upload_requests(fake):
    return [request for request in fake.requests if request.url.host == "media.test"
            and request.url.path.startswith("/v1/uploads/")]


def test_create_project_uploads_through_edge_session_and_confirms_upload(tmp_path):
    fake = FakeDiffioApiContract()
    audio_path = tmp_path / "sample.mp3"
    audio_path.write_bytes(b"ID3-sample-audio")
    client = _build_contract_client(fake)

    project = client.create_project(filePath=str(audio_path))

    assert project.upload.edgeBaseUrl == "https://media.test"
    assert project.upload.partSizeBytes == 32 * 1024 * 1024
    assert project.objectPath == project.upload.objectKey
    assert read_stored_upload_bytes(fake, project.apiProjectId) == b"ID3-sample-audio"
    assert project.uploadCompletion.status == "uploaded"
    assert project.uploadCompletion.sizeBytes == len(b"ID3-sample-audio")

    edge_paths = [(request.method, request.url.path) for request in _edge_upload_requests(fake)]
    assert edge_paths == [
        ("POST", "/v1/uploads/start"),
        ("PUT", "/v1/uploads/parts/1"),
        ("POST", "/v1/uploads/complete"),
    ]
    for request in _edge_upload_requests(fake):
        assert request.headers["Authorization"] == f"Bearer {project.upload.uploadToken}"
        assert fake.apiKey not in request.headers["Authorization"]
    part_request = _edge_upload_requests(fake)[1]
    assert part_request.headers["Content-Type"] == "application/octet-stream"
    assert part_request.headers["Content-Length"] == str(len(b"ID3-sample-audio"))

    completion_requests = fake.requests_for_path("/v1/complete_project_upload")
    assert len(completion_requests) == 1
    assert json.loads(completion_requests[0].content) == {"apiProjectId": project.apiProjectId}
    assert fake.requests.index(completion_requests[0]) > fake.requests.index(_edge_upload_requests(fake)[-1])


def test_create_project_splits_file_into_session_sized_parts(tmp_path):
    fake = FakeDiffioApiContract(partSizeBytes=4)
    audio_path = tmp_path / "sample.wav"
    audio_path.write_bytes(b"0123456789")
    client = _build_contract_client(fake)

    project = client.create_project(filePath=str(audio_path))

    part_requests = [request for request in _edge_upload_requests(fake) if request.method == "PUT"]
    assert [request.url.path for request in part_requests] == [
        "/v1/uploads/parts/1",
        "/v1/uploads/parts/2",
        "/v1/uploads/parts/3",
    ]
    assert [request.content for request in part_requests] == [b"0123", b"4567", b"89"]
    complete_request = [request for request in _edge_upload_requests(fake)
                        if request.url.path == "/v1/uploads/complete"][0]
    completion_body = json.loads(complete_request.content)
    assert [part["partNumber"] for part in completion_body["parts"]] == [1, 2, 3]
    assert read_stored_upload_bytes(fake, project.apiProjectId) == b"0123456789"


def test_create_project_uploads_empty_file_as_one_empty_part(tmp_path):
    fake = FakeDiffioApiContract()
    audio_path = tmp_path / "silence.wav"
    audio_path.write_bytes(b"")
    client = _build_contract_client(fake)

    project = client.create_project(filePath=str(audio_path))

    part_requests = [request for request in _edge_upload_requests(fake) if request.method == "PUT"]
    assert len(part_requests) == 1
    assert part_requests[0].headers["Content-Length"] == "0"
    assert read_stored_upload_bytes(fake, project.apiProjectId) == b""


def test_create_project_retries_only_the_failed_part(tmp_path, monkeypatch):
    monkeypatch.setattr("diffio.client.time.sleep", lambda _seconds: None)
    fake = FakeDiffioApiContract(partSizeBytes=4)
    fake.fail_part_once(2, statusCode=503)
    audio_path = tmp_path / "sample.wav"
    audio_path.write_bytes(b"0123456789")
    client = _build_contract_client(fake, requestOptions=RequestOptions(maxRetries=2))

    project = client.create_project(filePath=str(audio_path))

    part_paths = [request.url.path for request in _edge_upload_requests(fake) if request.method == "PUT"]
    assert part_paths == [
        "/v1/uploads/parts/1",
        "/v1/uploads/parts/2",
        "/v1/uploads/parts/2",
        "/v1/uploads/parts/3",
    ]
    assert len(fake.requests_for_path("/v1/uploads/start")) == 1
    assert read_stored_upload_bytes(fake, project.apiProjectId) == b"0123456789"


def test_create_project_aborts_edge_upload_when_a_part_fails(tmp_path):
    fake = FakeDiffioApiContract(partSizeBytes=4)
    fake.fail_part_once(2, statusCode=503)
    audio_path = tmp_path / "sample.wav"
    audio_path.write_bytes(b"0123456789")
    client = _build_contract_client(fake)

    with pytest.raises(DiffioApiError) as caught:
        client.create_project(filePath=str(audio_path))

    assert caught.value.statusCode == 503
    assert caught.value.message == "Injected part failure"
    assert caught.value.responseBody["error"]["code"] == "internal_error"
    abort_requests = fake.requests_for_path("/v1/uploads/abort")
    assert len(abort_requests) == 1
    assert json.loads(abort_requests[0].content) == {"uploadId": "upload-1"}
    assert fake.requests_for_path("/v1/uploads/complete") == []
    assert fake.requests_for_path("/v1/complete_project_upload") == []


def test_create_project_refuses_file_larger_than_session_limit(tmp_path):
    fake = FakeDiffioApiContract(maxUploadBytes=8)
    audio_path = tmp_path / "sample.wav"
    audio_path.write_bytes(b"0123456789")
    client = _build_contract_client(fake)

    with pytest.raises(ValueError, match="upload limit"):
        client.create_project(filePath=str(audio_path), contentLength=4)

    assert _edge_upload_requests(fake) == []


def test_create_generation_defaults_to_diffio_4_5_flash(tmp_path):
    fake = FakeDiffioApiContract()
    audio_path = tmp_path / "sample.mp3"
    audio_path.write_bytes(b"ID3")
    client = _build_contract_client(fake)
    project = client.create_project(filePath=str(audio_path))

    generation = client.create_generation(apiProjectId=project.apiProjectId)
    pro_generation = client.generations.create(apiProjectId=project.apiProjectId, model="diffio-4.5-pro")

    assert generation.modelKey == "diffio-4.5-flash"
    assert pro_generation.modelKey == "diffio-4.5-pro"
    assert len(fake.requests_for_path("/v1/diffio-4.5-flash-generation")) == 1
    assert len(fake.requests_for_path("/v1/diffio-4.5-pro-generation")) == 1


@pytest.mark.parametrize("retiredModel", [
    "diffio-2", "diffio-2-flash", "diffio-3.4", "diffio-3.5", "diffio-4.0-flash", "diffio-4.0-pro",
])
def test_create_generation_rejects_retired_model_before_sending(retiredModel):
    fake = FakeDiffioApiContract()
    client = _build_contract_client(fake)

    with pytest.raises(ValueError, match="diffio-4.5-flash"):
        client.create_generation(apiProjectId="proj", model=retiredModel)

    assert fake.requests == []


def test_get_generation_download_parses_edge_media_response(tmp_path):
    fake = FakeDiffioApiContract()
    fake.generations["gen-1"] = {"apiProjectId": "proj-1", "modelKey": "diffio-4.5-flash"}
    client = _build_contract_client(fake)

    download = client.get_generation_download(generationId="gen-1", apiProjectId="proj-1")
    download_path = tmp_path / "restored.mp3"
    client.generations.download(generationId="gen-1", apiProjectId="proj-1",
                                downloadFilePath=str(download_path))

    assert download_path.read_bytes() == FAKE_RESTORED_AUDIO_BYTES
    assert download.downloadType == "audio"
    assert download.downloadUrl.startswith("https://media.test/m/")
    assert download.fileName == "diffio_ai_sample.mp3"
    assert download.storagePath.endswith("/restored.mp3")
    assert download.mimeType == "audio/mpeg"


def test_restore_audio_runs_against_current_api_contract(tmp_path):
    fake = FakeDiffioApiContract()
    audio_path = tmp_path / "sample.mp3"
    audio_path.write_bytes(b"ID3-sample-audio")
    client = _build_contract_client(fake)

    content, info = client.restore_audio(filePath=str(audio_path), pollInterval=0)

    assert info["error"] is None
    assert info["ok"] is True
    assert content == FAKE_RESTORED_AUDIO_BYTES
    assert info["generation"].modelKey == "diffio-4.5-flash"
    assert read_stored_upload_bytes(fake, info["apiProjectId"]) == b"ID3-sample-audio"
