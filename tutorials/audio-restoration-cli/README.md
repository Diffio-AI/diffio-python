# Audio restoration CLI tutorial

This tutorial restores speech in one recording with the Diffio Python SDK and the Diffio API v2. `run.py`
uploads the file, restores it, shows progress, and saves the restored MP3.

## Prerequisites

- Python 3.8 or later
- A Diffio API key with Write permission (https://diffio.ai/developers/api-keys)
- A recording with speech, for example `interview.mp3` (audio or video; up to 2 GB)

## Setup

From the diffio-python folder:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r tutorials/audio-restoration-cli/requirements.txt
export DIFFIO_API_KEY="diffio_live_..."
```

## Run it

```bash
python tutorials/audio-restoration-cli/run.py --input interview.mp3 --output interview-restored.mp3
```

Output of a real run (a 32 second clip; IDs shortened):

```
Uploading interview.mp3 and restoring with diffio-flash...
processing (preparing)
processing (restoring) 10%
processing (finalizing)
complete (complete)
Project 3dff68a2-..., generation 8a0d959d-... (diffio-4.7-flash)
Saved interview-restored.mp3
```

Options:

- `--model diffio-pro` uses Diffio Pro (results can then be edited by transcript). `diffio-flash` is the default.
- `--end-user customer-42` creates the project for one of your own users (sent as `Diffio-End-User`).
- `--poll-interval 5` checks progress less often.
- `--base-url` points at another API host.

## How it works

`run.py` calls `client.restore_audio(...)`, which:

1. creates a project with `POST /v2/projects`, asking for the restore at the same time,
2. uploads the file to Diffio's media host in 32 MiB parts, then calls `POST /v2/projects/{id}/upload/complete`,
3. polls `GET /v2/projects/{id}/generations/{generation_id}` until the status is `complete`,
4. gets the file URLs with `GET .../media` and downloads `audio` (the restored MP3).

See the step by step section of the main `README.md` to run those steps yourself.

## Troubleshooting

- `missing_api_key` or `invalid_api_key`: set `DIFFIO_API_KEY` or pass `--api-key`.
- `permission_denied`: the key needs Write permission to create projects.
- `insufficient_credit`, `needs_payment_method` or another 402 code: add credit or a card on the developer
  dashboard.
- `unsupported_media_type`: the file name or type is not audio or video Diffio can read.
- The restore failed: the message says why; `retryable` says whether trying again can help.
