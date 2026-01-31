# Audio restoration CLI tutorial

This tutorial runs the full audio restoration flow with the Diffio Python SDK. It creates a project with your audio file, starts a generation, polls status, then downloads the restored file.

## Prerequisites

- Python 3.9 or later
- A Diffio API key
- A speech focused WAV file, for example sample.wav

## Setup

From the diffio-python folder:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r tutorials/audio-restoration-cli/requirements.txt
```

Set your API key:

```bash
export DIFFIO_API_KEY="diffio_live_..."
```

## Run the tutorial

```bash
python tutorials/audio-restoration-cli/run.py --input sample.wav --output restored.mp3
```

You can also pass the API key directly:

```bash
python tutorials/audio-restoration-cli/run.py --api-key diffio_live_... --input sample.wav
```

## Notes

- The script infers the content type from the file extension. If the extension is uncommon, update run.py to pass a contentType value.
- The output file defaults to restored.mp3.
- Use --model to switch models, the default is diffio-3.
- Use --poll-interval to control how often progress is checked.

## Troubleshooting

- Missing key: set DIFFIO_API_KEY or pass --api-key.
- File not found: check the --input path.
- Generation failed: check your spend limit and permissions for the API key.
