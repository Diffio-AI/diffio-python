import argparse
import os
import sys

from diffio import DiffioApiError, DiffioClient, GenerationFailedError


def parse_args():
    parser = argparse.ArgumentParser(description="Restore speech in a recording with the Diffio API v2.")
    parser.add_argument("--api-key", dest="api_key", help="Diffio API key. Defaults to DIFFIO_API_KEY.")
    parser.add_argument(
        "--input",
        dest="input_path",
        help="Path to the recording. Defaults to DIFFIO_INPUT_FILE.",
    )
    parser.add_argument(
        "--output",
        dest="output_path",
        default="restored.mp3",
        help="Where to write the restored MP3. Defaults to restored.mp3.",
    )
    parser.add_argument(
        "--model",
        dest="model",
        default="diffio-flash",
        help="Model name, for example diffio-flash (default) or diffio-pro.",
    )
    parser.add_argument(
        "--end-user",
        dest="end_user",
        help="Optional end user id, sent as Diffio-End-User.",
    )
    parser.add_argument(
        "--base-url",
        dest="base_url",
        help="API host. Defaults to DIFFIO_API_BASE_URL, else https://api.diffio.ai.",
    )
    parser.add_argument(
        "--poll-interval",
        dest="poll_interval",
        type=float,
        default=2.0,
        help="Seconds between status checks. Defaults to 2.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    api_key = args.api_key or os.environ.get("DIFFIO_API_KEY")
    input_path = args.input_path or os.environ.get("DIFFIO_INPUT_FILE")
    if not api_key:
        print("Error: set DIFFIO_API_KEY or pass --api-key.", file=sys.stderr)
        return 1
    if not input_path or not os.path.isfile(input_path):
        print(f"Error: input file not found: {input_path}", file=sys.stderr)
        return 1

    last_line = None

    def show(generation):
        nonlocal last_line
        line = f"{generation.status} ({generation.stage})"
        if generation.stage_progress is not None and generation.stage_progress.overall_percent is not None:
            line += f" {generation.stage_progress.overall_percent:.0f}%"
        if generation.queue is not None and generation.queue.message:
            line += f" - {generation.queue.message}"
        if line != last_line:
            print(line)
            last_line = line

    with DiffioClient(api_key, base_url=args.base_url, end_user=args.end_user) as client:
        print(f"Uploading {input_path} and restoring with {args.model}...")
        try:
            result = client.restore_audio(
                input_path,
                model=args.model,
                output_path=args.output_path,
                poll_interval=args.poll_interval,
                on_progress=show,
            )
        except GenerationFailedError as error:
            print(f"The restore failed: {error.message} (retryable: {error.retryable})", file=sys.stderr)
            return 1
        except DiffioApiError as error:
            print(f"Diffio refused the request: {error.code}: {error.message} (request {error.request_id})", file=sys.stderr)
            return 1

    print(f"Project {result.project.id}, generation {result.generation.id} ({result.generation.model})")
    print(f"Saved {result.output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
