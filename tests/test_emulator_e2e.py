from __future__ import annotations

import os
import socket
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest

from diffio import DiffioApiError, DiffioClient
from emulator_api_key import EmulatorApiKeyError, create_emulator_api_key

# Runs against a local Functions emulator plus edge Worker (diffio-ui `npm run local:start`). Point it at a
# stack with DIFFIO_EMULATOR_API_BASE_URL (for example http://127.0.0.1:21003/demo-name/us-central1),
# FIREBASE_PROJECT_ID, FIREBASE_AUTH_EMULATOR_HOST and FUNCTIONS_EMULATOR_HOST. Skipped when nothing listens.
DEFAULT_EMULATOR_API_BASE_URL = "http://127.0.0.1:5001/diffioai/us-central1"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
SAMPLE_AUDIO = FIXTURES_DIR / "sample-audio.mp3"

POLL_INTERVAL_SECONDS = 2.0
GENERATION_TIMEOUT_SECONDS = 300.0
DOWNLOAD_TIMEOUT_SECONDS = 120.0


def _emulator_api_base_url() -> str:
    return os.environ.get("DIFFIO_EMULATOR_API_BASE_URL") or DEFAULT_EMULATOR_API_BASE_URL


def _is_emulator_listening(base_url: str) -> bool:
    parsed = urlparse(base_url)
    try:
        with socket.create_connection((parsed.hostname or "127.0.0.1", parsed.port or 80), timeout=0.5):
            return True
    except OSError:
        return False


@pytest.fixture(scope="session")
def emulator_env() -> None:
    base_url = _emulator_api_base_url()
    if not _is_emulator_listening(base_url):
        pytest.skip(f"No Functions emulator is listening at {base_url}.")
    os.environ["DIFFIO_API_BASE_URL"] = base_url
    os.environ.setdefault("FIREBASE_PROJECT_ID", "diffioai")
    os.environ.setdefault("FIREBASE_AUTH_EMULATOR_HOST", "127.0.0.1:9099")
    os.environ.setdefault("FUNCTIONS_EMULATOR_HOST", "127.0.0.1:5001")
    os.environ.setdefault("FIREBASE_WEB_API_KEY", "fake-api-key")


@pytest.fixture(scope="session")
def emulator_api_key(emulator_env):
    try:
        result = create_emulator_api_key()
    except EmulatorApiKeyError as exc:
        if "Upgrade to the Developer plan" in str(exc):
            pytest.skip("Local emulator account cannot create API keys without the Developer plan.")
        raise
    os.environ["DIFFIO_API_KEY"] = result.api_key
    return result


@pytest.fixture()
def client(emulator_api_key):
    client = DiffioClient(apiKey=emulator_api_key.api_key)
    try:
        yield client
    finally:
        client.close()


@pytest.fixture(scope="session")
def sample_audio_path() -> Path:
    if not SAMPLE_AUDIO.exists():
        raise FileNotFoundError(f"Missing test audio file at {SAMPLE_AUDIO}")
    return SAMPLE_AUDIO


def _wait_for_generation_complete(
    client: DiffioClient,
    *,
    generation_id: str,
    api_project_id: str,
    timeout_seconds: float = GENERATION_TIMEOUT_SECONDS,
) -> object:
    deadline = time.monotonic() + timeout_seconds
    last_progress = None

    while time.monotonic() < deadline:
        progress = client.generations.get_progress(
            generationId=generation_id,
            apiProjectId=api_project_id,
        )
        last_progress = progress

        if progress.status == "complete":
            return progress

        if progress.status == "failed":
            raise AssertionError(
                "Generation failed"
                f" (preProcessing={progress.preProcessing.status},"
                f" inference={progress.inference.status},"
                f" error={progress.error},"
                f" details={progress.errorDetails})"
            )

        time.sleep(POLL_INTERVAL_SECONDS)

    raise AssertionError(
        "Timed out waiting for generation completion"
        f" (lastStatus={getattr(last_progress, 'status', None)})"
    )


def _wait_for_download(
    client: DiffioClient,
    *,
    generation_id: str,
    api_project_id: str,
    download_type: str,
    timeout_seconds: float = DOWNLOAD_TIMEOUT_SECONDS,
) -> object:
    deadline = time.monotonic() + timeout_seconds
    last_error = None

    while time.monotonic() < deadline:
        try:
            return client.generations.get_download(
                generationId=generation_id,
                apiProjectId=api_project_id,
                downloadType=download_type,
            )
        except DiffioApiError as exc:
            if exc.statusCode in {404, 409}:
                last_error = exc
                time.sleep(POLL_INTERVAL_SECONDS)
                continue
            raise

    message = "Timed out waiting for download"
    if last_error:
        message += f" (lastStatus={last_error.statusCode})"
    raise AssertionError(message)


def test_emulator_create_project_appears_in_list(client: DiffioClient, sample_audio_path: Path) -> None:
    project = client.create_project(filePath=str(sample_audio_path))

    assert project.upload.edgeBaseUrl.startswith(("http://", "https://"))
    assert project.uploadCompletion.status == "uploaded"
    assert project.uploadCompletion.sizeBytes == sample_audio_path.stat().st_size

    projects = client.list_projects()
    project_ids = {item.apiProjectId for item in projects.projects}
    assert project.apiProjectId in project_ids


def test_emulator_create_generation_appears_in_list(client: DiffioClient, sample_audio_path: Path) -> None:
    project = client.create_project(filePath=str(sample_audio_path))
    generation = client.create_generation(
        apiProjectId=project.apiProjectId,
        model="diffio-4.5-flash",
    )

    generations = client.list_project_generations(apiProjectId=project.apiProjectId)
    generation_ids = {item.generationId for item in generations.generations}
    assert generation.generationId in generation_ids


def test_emulator_audio_isolation_full_flow_download(
    client: DiffioClient,
    sample_audio_path: Path,
) -> None:
    result = client.audio_isolation.isolate(
        filePath=str(sample_audio_path),
        contentType="audio/mpeg",
        model="diffio-4.5-flash",
    )

    progress = _wait_for_generation_complete(
        client,
        generation_id=result.generation.generationId,
        api_project_id=result.project.apiProjectId,
    )

    assert progress.preProcessing.status == "complete"
    assert progress.inference.status == "complete"

    download = _wait_for_download(
        client,
        generation_id=result.generation.generationId,
        api_project_id=result.project.apiProjectId,
        download_type="audio",
    )

    response = httpx.get(download.downloadUrl, timeout=30.0)
    assert response.status_code == 200
    assert response.content
    assert download.mimeType.startswith("audio/")
