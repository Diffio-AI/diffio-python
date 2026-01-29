# Diffio Python SDK

The Diffio Python SDK helps you call the Diffio API from Python. This version covers project creation, upload, generation, progress checks, and download URLs.
Requires Python 3.8 or later.

## Install

```bash
pip install diffio
```

For local development:

```bash
cd diffio-python
pip install -e .
```

## Configuration

Set the API key with `DIFFIO_API_KEY`. You can also override the base URL with `DIFFIO_API_BASE_URL`.

```bash
export DIFFIO_API_KEY="diffio_live_..."
export DIFFIO_API_BASE_URL="https://us-central1-diffioai.cloudfunctions.net"
```

For emulators, set the base URL to the Functions emulator host.

```bash
export DIFFIO_API_BASE_URL="http://127.0.0.1:5001/diffioai/us-central1"
```

## Request options

Use request options to override headers, timeouts, retries, or the API key per request.
You can also pass `timeoutInSeconds` as an alias for `timeout`.

```py
from diffio import DiffioClient, RequestOptions

client = DiffioClient(apiKey="diffio_live_...")
projects = client.list_projects(
    requestOptions=RequestOptions(
        headers={"X-Debug": "1"},
        timeout=30.0,
        maxRetries=2,
        retryBackoff=0.5,
    )
)
```

## Create a project and generation

`create_project` uploads the file and returns the project metadata.

```py
from diffio import DiffioClient

client = DiffioClient(apiKey="diffio_live_...")
file_path = "sample.wav"
project = client.create_project(
    filePath=file_path,
)

generation = client.create_generation(
    apiProjectId=project.apiProjectId,
    model="diffio-2",
    sampling={"steps": 12, "guidance": 1.5},
)

print(generation.generationId)
```

## Audio isolation helper

```py
from diffio import DiffioClient

client = DiffioClient(apiKey="diffio_live_...")
result = client.audio_isolation.isolate(
    filePath="sample.wav",
    model="diffio-2",
    sampling={"steps": 12, "guidance": 1.5},
)

print(result.generation.generationId)
```

## Restore audio in one call

This helper runs the full flow and returns the downloaded bytes plus a metadata dict.

```py
from diffio import DiffioClient

client = DiffioClient(apiKey="diffio_live_...")
audio_bytes, info = client.restore_audio(
    filePath="sample.wav",
    model="diffio-2",
    sampling={"steps": 12, "guidance": 1.5},
    onProgress=lambda progress: print(progress.status),
)

if info["error"]:
    print(info["error"])
else:
    with open("restored.mp3", "wb") as handle:
        handle.write(audio_bytes)

print(info["apiProjectId"], info["generationId"])
```

## Generation progress

```py
from diffio import DiffioClient

client = DiffioClient(apiKey="diffio_live_...")
progress = client.generations.get_progress(
    generationId="gen_123",
    apiProjectId="proj_123",
)

print(progress.status)
```

## Generation download

```py
from diffio import DiffioClient

client = DiffioClient(apiKey="diffio_live_...")
download = client.generations.download(
    generationId="gen_123",
    apiProjectId="proj_123",
    downloadType="mp3",
    downloadFilePath="restored.mp3",
)

print(download.downloadUrl)
```

If you only need the URL, use `client.generations.get_download`.

## List projects

```py
from diffio import DiffioClient

client = DiffioClient(apiKey="diffio_live_...")
projects = client.projects.list()

for project in projects.projects:
    print(project.apiProjectId, project.status)
```

## List project generations

```py
from diffio import DiffioClient

client = DiffioClient(apiKey="diffio_live_...")
generations = client.projects.list_generations(apiProjectId="proj_123")

for generation in generations.generations:
    print(generation.generationId, generation.status)
```

## Webhooks portal access

```py
from diffio import DiffioClient

client = DiffioClient(apiKey="diffio_live_...")
portal = client.webhooks.get_portal_access(mode="test")
print(portal.portalUrl)
```

## Send a test webhook event

```py
from diffio import DiffioClient

client = DiffioClient(apiKey="diffio_live_...")
event = client.webhooks.send_test_event(
    eventType="generation.completed",
    mode="test",
    samplePayload={"apiProjectId": "proj_123"},
)

print(event.svixMessageId)
```

## Tutorials

* Audio restoration CLI tutorial: `tutorials/audio-restoration-cli/README.md`

## Runtime compatibility

Use Python 3.8 or later.

## Tests

```bash
cd diffio-python
python -m pip install -r requirements-dev.txt
python -m pytest
```
