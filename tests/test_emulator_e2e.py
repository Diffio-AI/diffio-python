from __future__ import annotations

import os
import time
from urllib.parse import urlparse
from pathlib import Path

import httpx
import pytest

from diffio import DiffioApiError, DiffioClient
from emulator_api_key import create_emulator_api_key

EMULATOR_BASE_URL = "http://127.0.0.1:5001/diffioai/us-central1"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
SAMPLE_AUDIO = FIXTURES_DIR / "sample-audio.mp3"

POLL_INTERVAL_SECONDS = 2.0
GENERATION_TIMEOUT_SECONDS = 300.0
DOWNLOAD_TIMEOUT_SECONDS = 120.0


@pytest.fixture(scope="session")
def emulator_env() -> None:
    os.environ["DIFFIO_API_BASE_URL"] = EMULATOR_BASE_URL
    os.environ.setdefault("FIREBASE_PROJECT_ID", "diffioai")
    os.environ.setdefault("FIREBASE_AUTH_EMULATOR_HOST", "127.0.0.1:9099")
    os.environ.setdefault("FUNCTIONS_EMULATOR_HOST", "127.0.0.1:5001")
    os.environ.setdefault("FIREBASE_WEB_API_KEY", "fake-api-key")
    os.environ.setdefault("FIREBASE_STORAGE_EMULATOR_HOST", "127.0.0.1:9199")
    os.environ.setdefault("STORAGE_EMULATOR_HOST", "http://127.0.0.1:9199")


@pytest.fixture(scope="session")
def emulator_api_key(emulator_env):
    result = create_emulator_api_key()
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
) -> tuple[object, float]:
    deadline = time.monotonic() + timeout_seconds
    last_progress = None

    while time.monotonic() < deadline:
        progress = client.generations.get_progress(
            generationId=generation_id,
            apiProjectId=api_project_id,
        )
        last_progress = progress

        if progress.status == "complete":
            return progress, time.monotonic()

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


def _needs_storage_emulator_auth(download_url: str) -> bool:
    try:
        parsed = urlparse(download_url)
    except Exception:
        return False

    host = (parsed.hostname or "").lower()
    port = parsed.port
    if host in {"127.0.0.1", "localhost", "0.0.0.0", "::1"} and (port == 9199 or port is None):
        return True

    emulator_host = os.environ.get("STORAGE_EMULATOR_HOST") or os.environ.get("FIREBASE_STORAGE_EMULATOR_HOST")
    if not emulator_host:
        return False
    if not emulator_host.startswith(("http://", "https://")):
        emulator_host = f"http://{emulator_host}"
    try:
        emulator_parsed = urlparse(emulator_host)
    except Exception:
        return False
    if host != (emulator_parsed.hostname or "").lower():
        return False

    parsed_port = parsed.port or (443 if parsed.scheme == "https" else 80)
    emulator_port = emulator_parsed.port or (443 if emulator_parsed.scheme == "https" else 80)
    return parsed_port == emulator_port


def test_emulator_create_project_appears_in_list(client: DiffioClient, sample_audio_path: Path) -> None:
    project = client.create_project(filePath=str(sample_audio_path))

    assert project.uploadMethod in {"POST", "PUT"}
    assert "/v0/b/" in project.uploadUrl

    projects = client.list_projects()
    project_ids = {item.apiProjectId for item in projects.projects}
    assert project.apiProjectId in project_ids


def test_emulator_create_generation_appears_in_list(client: DiffioClient, sample_audio_path: Path) -> None:
    project = client.create_project(filePath=str(sample_audio_path))
    generation = client.create_generation(
        apiProjectId=project.apiProjectId,
        model="diffio-2-flash",
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
        model="diffio-2-flash",
        sampling={"steps": 10},
    )

    progress, _ = _wait_for_generation_complete(
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

    headers = {"Authorization": "Bearer owner"} if _needs_storage_emulator_auth(download.downloadUrl) else None
    response = httpx.get(download.downloadUrl, headers=headers, timeout=30.0)
    assert response.status_code == 200
    assert response.content
    assert download.mimeType.startswith("audio/")
