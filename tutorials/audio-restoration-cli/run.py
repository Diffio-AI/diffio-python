import argparse
import os
import sys
import time

from diffio import DiffioClient


def parse_args():
    parser = argparse.ArgumentParser(
        description="Restore audio with the Diffio API using the Python SDK."
    )
    parser.add_argument(
        "--api-key",
        dest="api_key",
        help="Diffio API key. Defaults to DIFFIO_API_KEY.",
    )
    parser.add_argument(
        "--input",
        dest="input_path",
        help="Path to the input audio file. Defaults to DIFFIO_INPUT_FILE.",
    )
    parser.add_argument(
        "--output",
        dest="output_path",
        default="restored.mp3",
        help="Output file path. Defaults to restored.mp3.",
    )
    parser.add_argument(
        "--base-url",
        dest="base_url",
        help="Override API base URL. Defaults to DIFFIO_API_BASE_URL.",
    )
    parser.add_argument(
        "--model",
        dest="model",
        default="diffio-3",
        help="Model key. Defaults to diffio-3.",
    )
    parser.add_argument(
        "--poll-interval",
        dest="poll_interval",
        type=float,
        default=5.0,
        help="Seconds between progress checks. Defaults to 5.",
    )
    return parser.parse_args()


def resolve_inputs(args):
    api_key = args.api_key or os.environ.get("DIFFIO_API_KEY")
    input_path = args.input_path or os.environ.get("DIFFIO_INPUT_FILE")
    if not api_key:
        raise ValueError("API key is required. Use --api-key or DIFFIO_API_KEY.")
    if not input_path:
        raise ValueError("Input file is required. Use --input or DIFFIO_INPUT_FILE.")
    if not os.path.isfile(input_path):
        raise FileNotFoundError(f"Input file not found: {input_path}")
    return api_key, input_path


def main():
    args = parse_args()
    try:
        api_key, input_path = resolve_inputs(args)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    client_kwargs = {"apiKey": api_key}
    if args.base_url:
        client_kwargs["baseUrl"] = args.base_url

    with DiffioClient(**client_kwargs) as client:
        print("Creating project and uploading audio...")
        project = client.create_project(filePath=input_path)

        print("Creating generation...")
        generation = client.create_generation(
            apiProjectId=project.apiProjectId,
            model=args.model,
        )

        status = "queued"
        while status not in ("complete", "failed"):
            progress = client.generations.get_progress(
                generationId=generation.generationId,
                apiProjectId=project.apiProjectId,
            )
            status = progress.status
            print(f"Status: {status}")
            if status not in ("complete", "failed"):
                time.sleep(args.poll_interval)

        if status != "complete":
            print("Generation failed.", file=sys.stderr)
            return 1

        print("Downloading output...")
        client.generations.download(
            generationId=generation.generationId,
            apiProjectId=project.apiProjectId,
            downloadType="mp3",
            downloadFilePath=args.output_path,
        )

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
