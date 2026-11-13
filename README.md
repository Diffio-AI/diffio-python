# Diffio Python SDK

Python client for the Diffio API v2 (`https://api.diffio.ai/v2`): upload a recording, restore its speech, and
download the result. Requires Python 3.8 or later. Its only dependency is `httpx`.

Version 1.0 talks to `/v2` only. Code written for the 0.x SDK (the `/v1` API) keeps working on `pip install "diffio<1"`;
see [Upgrading from 0.x](#upgrading-from-0x).

## Install

```bash
pip install diffio
```

## Restore a recording in one call

```py
from diffio import DiffioClient

client = DiffioClient()  # reads DIFFIO_API_KEY

result = client.restore_audio(
    "interview.mp3",
    model="diffio-flash",
    output_path="interview-restored.mp3",
    on_progress=lambda generation: print(generation.status, generation.stage),
)
print(result.project.id, result.generation.id, result.output_path)
```

`restore_audio` creates the project with the restore already requested, uploads the file, waits for the
generation, and downloads the restored MP3 (`file="audio"`; any name from the generation's media files works).
Without `output_path` the bytes are on `result.content`. A failed restore raises `GenerationFailedError`.
There is no length limit on API uploads; files can be up to 2 GB.

## Configuration

| Setting | Argument | Environment | Default |
|---|---|---|---|
| API key | `DiffioClient(api_key=...)` | `DIFFIO_API_KEY` | none |
| API host | `base_url=` | `DIFFIO_API_BASE_URL` | `https://api.diffio.ai` (`/v2` is added) |
| End user | `end_user=` | | none |
| Timeout (seconds) | `timeout=` | | `60` |
| Retries | `max_retries=`, `retry_backoff=` | | `2`, `0.5` |

Create keys on the developer dashboard at https://diffio.ai/developers/api-keys. The key is sent as
`Authorization: Bearer <key>`.

Every method also takes `request_options={"headers": ..., "timeout": ..., "max_retries": ..., "api_key": ...}`
to change one call.

### Retries and idempotency

Calls that are safe to repeat (GET, PUT, DELETE, completing an upload, creating an export, edit estimates, and
every upload part) are retried on HTTP 408, 429, 500, 502, 503, 504 and network errors, with exponential
backoff that honours `Retry-After`.

`projects.create`, `generations.create` and `edits.create` send an `Idempotency-Key` header. Pass your own
`idempotency_key=` to make a repeat of the same request (from any process) return the first result; without one
the SDK sends a fresh key per call, so its own retries never create a second project or start a second restore.
Reusing a key with a different request is refused with `idempotency_key_conflict`.

### End users

If one API key serves many people, set `end_user` (on the client or per call). It is sent as the
`Diffio-End-User` header: projects are created for that end user, lists show only their projects, any other
project answers `project_not_found`, and `usage.get` reports only their usage.

```py
client = DiffioClient(end_user="customer-42")
client.projects.list(end_user="customer-7")  # one call for someone else
```

## Step by step

```py
import os

from diffio import DiffioClient

client = DiffioClient(end_user="customer-42")  # every call acts for this end user

project = client.projects.create(
    file_name="interview.mp3",
    size_bytes=os.path.getsize("interview.mp3"),
    content_type="audio/mpeg",
    metadata={"order_id": "1042"},
)
project = client.projects.upload(project, "interview.mp3")  # media host upload, then upload/complete
print(project.id, project.status)  # preparing or ready

generation = client.generations.create(project.id, model="diffio-pro", idempotency_key="order-1042-restore")
generation = client.generations.wait(
    project.id,
    generation.id,
    on_progress=lambda g: print(g.status, g.stage),
)
media = client.generations.media(project.id, generation.id)
print(sorted(media.files), media.expires_at)

path = client.generations.download(project.id, generation.id, file="audio", path="interview-pro.mp3")
print(path, generation.model, generation.quality_score)
```

- `projects.create` answers the project plus `project.upload`: the media host's upload requests and the token
  they carry. Pass `generation={"model": "diffio-flash"}` (or just `generation="diffio-flash"`) to request the
  restore with the project; it starts by itself once the upload is ready.
- `projects.upload(project, file)` follows `project.upload.requests` (start, one PUT per 32 MiB part, complete).
  `file` is a path, bytes or a binary file object. Each part is retried on its own; a failed upload is aborted.
  Your API key is never sent to the media host. It then calls `projects.complete_upload` so the file is checked
  now (pass `complete=False` to skip; the project then becomes ready by itself about 30 seconds later). Pass a
  project id instead of a project to fetch fresh instructions with `projects.reopen_upload`.
- `generations.create` works before the upload is ready too; the generation waits for it.
- `generations.wait` polls every 2 seconds until `status` is `complete`. It raises `GenerationFailedError`
  (with `.generation`, `.code`, `.retryable`) when the generation fails and `DiffioTimeoutError` after `timeout`
  seconds (default 3600; `None` waits without limit).
- A generation's `status` is `queued`, `processing`, `complete` or `failed`; `stage`, `stage_progress` and
  `queue` describe a running one. `files_available` names its files.
- `generations.media` hands out URLs for those files, valid for 6 hours: `audio` (restored MP3), `audio_mixed`,
  `original`, `transcript`, `hls_master` and more. `generations.download(..., file=..., path=...)` streams one
  of them to a file (or returns bytes without `path`).

## Lists and pagination

```py
from diffio import DiffioClient

client = DiffioClient()

page = client.projects.list(limit=1)
for project in page.data:
    print(project.id, project.status, project.file_name)
if page.next_cursor:
    page = client.projects.list(limit=1, cursor=page.next_cursor)
    print("next page:", [project.id for project in page.data])

for project in client.projects.iter(limit=50):  # fetches every page as needed
    latest = project.latest_generation
    print(project.id, project.file_name, latest.status if latest else "no generation")
```

Lists are newest first. `limit` is 1 to 100 (default 20). `generations.list(project_id)` and
`generations.iter(project_id)` work the same way.

## Errors

Every refusal of the API raises `DiffioApiError` with:

| Attribute | Meaning |
|---|---|
| `status` | HTTP status (`None` when no answer arrived) |
| `code` | stable snake_case code, for example `project_not_found`, `unknown_model`, `insufficient_credit`, `upload_missing`; `connection_error` when Diffio could not be reached |
| `message` | a sentence to show or log |
| `request_id` | quote it to Diffio support |
| `details` | extra fields, for example `{"field": "model", "models": [...]}` |

```py
from diffio import DiffioApiError

try:
    client.generations.create(project.id, model="diffio-9")
except DiffioApiError as error:
    print(error.status, error.code, error.message, error.request_id, error.details)
```

All SDK errors derive from `DiffioError`. Others: `GenerationFailedError`, `ExportFailedError`,
`AssistantTurnFailedError` (from the wait helpers), `DiffioTimeoutError`, `WebhookVerificationError`.

## Response objects

Responses are objects with the API's snake_case field names as attributes: `project.latest_generation.status`,
`generation.error.code`, `media.files["audio"]`. `obj["field"]` and `obj.get("field")` also work, fields added by
the API later are kept, and `obj.to_dict()` returns the answer as received.

## More of the API

```py
models = client.models.list()                                   # no key needed
client.projects.get(project_id)
client.projects.delete(project_id)                              # refused while a generation runs
client.generations.list(project_id)

transcript = client.transcripts.get(project_id, generation_id)  # text and word timings
draft = client.transcripts.get_draft(project_id, generation_id)
saved = client.transcripts.save_draft(project_id, generation_id, data={"cursor": 12}, revision=draft.revision)
client.transcripts.delete_draft(project_id, generation_id, revision=saved.revision)

# Transcript edits and the edit assistant need a finished Diffio Pro result (generation.transcript_editable).
estimate = client.edits.estimate(project_id, generation_id, words=words)
edit = client.edits.create(project_id, generation_id, words=words)          # a new generation, kind "edit"
turn = client.assistant.create_turn(project_id, generation_id, messages=[{"role": "user", "content": "Remove the ums"}])
turn = client.assistant.wait_turn(project_id, generation_id, turn.id)

mix = client.mix.get(project_id, generation_id)                 # balance of speech and background
client.mix.save(project_id, generation_id, background_gain=0.3, revision=mix.revision)
export = client.exports.create(project_id, generation_id, artifact="mix", format="mp3")
export = client.exports.wait(project_id, generation_id, export.id)
print(export.url)

share = client.shares.publish(project_id)                      # a link anyone can play
client.shares.get_public(share.id)                              # no key needed
client.shares.revoke(project_id)

usage = client.usage.get(start="2026-11-01")                    # generations, billable seconds, list price
```

## Webhooks

`client.webhooks.portal()` returns a short-lived link to the webhook portal, where you add endpoint URLs, pick
event types and copy each endpoint's signing secret (`whsec_...`). It needs a key with the `webhooks:write`
scope. Events are signed by Svix (`svix-id`, `svix-timestamp`, `svix-signature`); check them with the raw
request body:

```py
import os

from diffio import DiffioClient, WebhookVerificationError

client = DiffioClient()
secret = os.environ["DIFFIO_WEBHOOK_SECRET"]  # the endpoint's whsec_... signing secret from the webhook portal


def handle_webhook(raw_body, headers):
    """raw_body: the request body bytes as received; headers: the request headers (any mapping)."""
    try:
        event = client.webhooks.verify_signature(raw_body, headers, secret)
    except WebhookVerificationError:
        return 400
    if event.type == "generation.completed":
        print("restored", event.data.project_id, event.data.id, event.data.end_user)
    elif event.type == "generation.failed":
        print("failed", event.data.id, event.data.error_code)
    return 200
```

Verification needs no API key and no network. Events older than 5 minutes are refused
(`tolerance_seconds=`). Event ids are the same on every retry, so de-duplicate on `event.id`. An event names the
resource that changed; read it again (for example `generations.get`) for the rest.

## Upgrading from 0.x

1.0 removes the `/v1` methods. Request and response fields are snake_case, as on the wire.

| 0.x | 1.0 |
|---|---|
| `DiffioClient(apiKey=, baseUrl=)` | `DiffioClient(api_key=, base_url=)` |
| `create_project(filePath=)` | `projects.create(file_name=, size_bytes=, ...)` then `projects.upload(project, path)` |
| `complete_project_upload(apiProjectId=)` | `projects.complete_upload(project_id)` |
| `create_generation(apiProjectId=, model="diffio-4.7-flash")` | `generations.create(project_id, model="diffio-flash")` |
| `get_generation_progress`, `wait_for_generation` | `generations.get`, `generations.wait` |
| `get_generation_download`, `generations.download(downloadType=)` | `generations.media`, `generations.download(file="audio")` |
| `list_projects`, `list_project_generations` | `projects.list` / `projects.iter`, `generations.list` / `generations.iter` |
| `restore_audio(filePath=)` returning `(bytes, info)` | `restore_audio(path, output_path=)` returning `RestoreResult`; failures raise |
| `audio_isolation.isolate` | `restore_audio`, or `projects.create` + `projects.upload` + `generations.create` |
| `usage.summary` | `usage.get(start=, end=)` |
| `webhooks.configure`, `webhooks.send_test_event` | `webhooks.portal()` |
| `account`, `api_keys` | no API route; use the developer dashboard |
| `DiffioApiError.statusCode`, `.responseBody` | `.status`, `.code`, `.message`, `.request_id`, `.details`, `.body` |

Model names: `diffio-flash` and `diffio-pro` always mean the current model of each tier; `diffio-4.7-flash` and
`diffio-4.7-pro` name the current models exactly. Retired names still run, on the current model of their tier
(`generation.substituted` is true). `client.models.list()` lists them all.

`DIFFIO_API_BASE_URL` set to `https://api.diffio.ai/v1` (as the 0.x docs showed) is read as the host, and `/v2`
is used.

## Tutorials

* Audio restoration CLI: `tutorials/audio-restoration-cli/README.md`

## Tests

```bash
python -m pip install -r requirements-dev.txt
python -m pip install -e .
python -m pytest
```

The tests run against an in-memory fake of the v2 API (`tests/fake_api.py`); they make no network calls.
