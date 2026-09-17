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

Set the API key with `DIFFIO_API_KEY`. If you need to set the base URL explicitly, use the production endpoint with `DIFFIO_API_BASE_URL`.

```bash
export DIFFIO_API_KEY="diffio_live_..."
export DIFFIO_API_BASE_URL="https://api.diffio.ai/v1"
```

## Request options

Use request options to override headers, timeouts, retries, or the API key per request.
You can also pass `timeoutInSeconds` as an alias for `timeout`.

Generation creation is retried only when you supply a non-empty `idempotencyKey`,
even when `maxRetries` is configured globally or per request. Without a key, a
timeout or lost response can hide an accepted generation, so the SDK returns the
error after the first attempt. With a key, retries send the same key and payload.
Reuse that key when manually retrying the same operation; use a new key for a new
generation. The audio isolation and restore helpers do not supply a key and do
not automatically retry their generation-creation step.

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
    model="diffio-3.5",
    sampling={"steps": 12, "guidance": 1.5},
    idempotencyKey="restore-job-2026-001",
    requestOptions={"maxRetries": 2},
)

print(generation.generationId)
print(generation.idempotentReplay)
```

Use one stable `idempotencyKey` for every retry of the same logical generation request.
The response's optional `idempotentReplay` value is `True` when the API returns the
result of an earlier request with that key. Use a new key for a different generation.

## Audio isolation helper

```py
from diffio import DiffioClient

client = DiffioClient(apiKey="diffio_live_...")
result = client.audio_isolation.isolate(
    filePath="sample.wav",
    model="diffio-3.5",
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
    model="diffio-3.5",
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

`wait_for_generation` and `generations.wait_for_complete` wait for the overall
`status` to become `complete`. Individual stages can reach 100% while video
restoration or final settlement is still pending; stage progress alone does not
indicate overall completion.

For Diffio 2.0, `complete` means restored media is ready. Transcription can still
be `pending`, become `available` later, or finish as `unavailable`. Read
`progress.transcription.status` independently; `progress.transcription` is `None`
for older responses that do not report availability. A completed generation
remains successful if transcription is unavailable. Completion webhooks expose
the same optional `event.transcription` object; a later transcript does not emit
another `generation.completed` event.

```py
from diffio import DiffioClient

client = DiffioClient(apiKey="diffio_live_...")
progress = client.generations.get_progress(
    generationId="gen_123",
    apiProjectId="proj_123",
)

print(progress.status)
if progress.transcription is not None:
    print(progress.transcription.status)
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

Set `downloadType="transcript"` to download the transcript JSON artifact when the generation has one.
Pending transcripts return `DiffioApiError` with `statusCode == 409` and
`responseBody["code"] == "TRANSCRIPT_PENDING"`. Unavailable transcripts return
`statusCode == 404` and `responseBody["code"] == "TRANSCRIPT_UNAVAILABLE"`. These
responses include `responseBody["transcription"]["status"]`. Check the error code
to distinguish them from other 409 or 404 errors.

```py
from diffio import DiffioApiError

try:
    transcript = client.generations.download(
        generationId="gen_123",
        apiProjectId="proj_123",
        downloadType="transcript",
        downloadFilePath="word_timestamps.json",
    )
except DiffioApiError as exc:
    body = exc.responseBody if isinstance(exc.responseBody, dict) else {}
    if exc.statusCode == 409 and body.get("code") == "TRANSCRIPT_PENDING":
        print("Transcript is pending; check progress and retry later.")
    elif exc.statusCode == 404 and body.get("code") == "TRANSCRIPT_UNAVAILABLE":
        print("Transcript is unavailable; restored media remains available.")
    else:
        raise
```

`restore_audio(downloadType="transcript")` also makes one download request after
media completion. It does not wait for a pending transcript. With its default
`raiseOnError=False`, it returns `(None, info)` and preserves the API error in
`info["statusCode"]` and `info["responseBody"]`; `info["status"]` can still be
`complete` because media restoration succeeded. With `raiseOnError=True`, it raises
the same `DiffioApiError` and attaches the metadata as `exc.restoreInfo`. Callers
can poll progress and retry the transcript download explicitly. Audio and video
downloads proceed independently of transcription availability.

## Account, keys, usage, and webhook configuration

Agent keys can manage account settings, scoped keys, usage, and webhook endpoints.

```py
settings = client.account.get_settings()
key = client.api_keys.create(
    label="Backend worker",
    scopes=["projects:read", "projects:write", "generations:read", "generations:write", "artifacts:read"],
)
usage = client.usage.summary(apiKeyId=key.keyId)
webhook = client.webhooks.configure(
    mode="live",
    url="https://example.com/webhooks/diffio",
    eventTypes=["generation.completed", "generation.failed"],
    apiKeyId=key.keyId,
)
```

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

## Send a test webhook event

```py
from diffio import DiffioClient

client = DiffioClient(apiKey="diffio_live_...")
event = client.webhooks.send_test_event(
    eventType="generation.completed",
    mode="live",
    samplePayload={"apiProjectId": "proj_123"},
)

print(event.svixMessageId)
```

## Verify webhook signatures

Use the raw request body (bytes) plus the `svix-*` headers and your webhook signing secret.

```py
from fastapi import FastAPI, Request, HTTPException
from diffio import DiffioClient
import os

app = FastAPI()
client = DiffioClient(apiKey=os.environ["DIFFIO_API_KEY"])

@app.post("/webhooks/diffio")
async def diffio_webhook(request: Request):
    payload = await request.body()
    headers = request.headers
    try:
        event = client.webhooks.verify_signature(
            payload=payload,
            headers=headers,
            secret=os.environ["DIFFIO_WEBHOOK_SECRET"],
        )
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid signature")
    print("Webhook received", event.eventType)
    return {"ok": True}
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

## Diffio 4.0 stems and mixing

These methods target the Diffio 4.0 API implementation. Use a deployment that
has enabled Diffio 4.0; SDK support does not imply production availability.

Create a generation using `model="diffio-4.0"`. Existing models and defaults
are unchanged. While processing, fetch bounded pages of the playback manifest:

```python
generation = client.generations.create(
    apiProjectId="proj_123",
    model="diffio-4.0",
    idempotencyKey="unique-request-id",
)

page = client.generations.get_playback(
    apiProjectId="proj_123",
    generationId=generation.generationId,
    startChunk=0,
    chunkCount=8,
)
manifest = page.manifest
# manifest["chunks"] contains this page with signed audio URLs.
# manifest["chunkCount"] is the TOTAL currently available chunk count,
# not the size of this page. startChunk is a zero-based index.
```

`startChunk` must be a nonnegative integer; `chunkCount` defaults to 8 and
accepts 1–16. Refetch pages as processing advances or signed URLs expire.

Save one background level, from 0 (speech only) to 1, using optimistic concurrency:

```python
updated = client.generations.update_mix(
    apiProjectId="proj_123",
    generationId=generation.generationId,
    backgroundGain=0.25,
    expectedRevision=manifest["mix"]["revision"],
)
print(updated.mix.backgroundGain, updated.mix.revision)
```

A stale revision raises `DiffioApiError` with `statusCode == 409`; fetch the
current playback manifest before deciding whether to resubmit. The SDK does
not automatically retry mix updates.

Request MP3 or FLAC for `artifact="mix"`, `"speech"`, or `"background"`.
MP4 is for a video project's current mix. Exports require completed processing
and the account's download permissions. The optional `backgroundGain` selects
the mix for that export; it does not save the generation's mix setting.

```python
from diffio import GenerationExportPendingResponse

result = client.generations.get_download(
    apiProjectId="proj_123",
    generationId=generation.generationId,
    artifact="mix",
    format="flac",
    backgroundGain=0.25,
)
if isinstance(result, GenerationExportPendingResponse):
    print(result.exportId, result.retryAfterSeconds)
    # Repeat the same request after this delay to poll the export.
else:
    print(result.downloadUrl)

# Or poll and save the finished export automatically:
client.generations.download(
    apiProjectId="proj_123",
    generationId=generation.generationId,
    artifact="mix",
    format="mp3",
    backgroundGain=0.25,
    downloadFilePath="restored.mp3",
    exportTimeout=600,
)
```

`generations.wait_for_download(..., timeout=600)` polls without saving a file.
It raises `TimeoutError` when its polling deadline expires. Pending responses
are not download URLs. Polling disables transport retries and bounds numeric
request timeouts by the remaining deadline; an in-flight HTTP operation can
still finish after that deadline. This SDK uses synchronous HTTP calls.

Flat methods are also available: `get_generation_playback`,
`update_generation_mix`, and `get_generation_download`.
