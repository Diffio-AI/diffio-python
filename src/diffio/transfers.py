"""Moving bytes to and from Diffio's media host: the multipart upload and result downloads."""

import io
import json
import os
import re
import uuid
from urllib.parse import quote, urlparse

import httpx

from .client import (
    _request_options,
    api_error_from_response,
    connection_error,
    parse_json_response,
    sdk_identity_headers,
)
from .errors import DiffioApiError, DiffioError
from .types import UploadInstructions, UploadRequest

_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_EXACT_PLACEHOLDER = re.compile(r"^\{(\w+)\}$")


def _default_upload_requests(instructions):
    """The upload recipe as the API sends it, for instructions that came without upload.requests."""
    base = f"{str(instructions.edge_base_url).rstrip('/')}/v1/uploads"
    auth = {"Authorization": f"Bearer {instructions.upload_token}"}
    json_auth = dict(auth, **{"Content-Type": "application/json"})
    return {
        "start": UploadRequest({"method": "POST", "url": f"{base}/start", "headers": auth, "body": None}),
        "part": UploadRequest({
            "method": "PUT",
            "url": f"{base}/parts/{{part_number}}?uploadId={{upload_id}}",
            "headers": dict(auth, **{"Content-Length": "{part_length}"}),
            "body": None,
        }),
        "complete": UploadRequest({
            "method": "POST",
            "url": f"{base}/complete",
            "headers": json_auth,
            "body": {"uploadId": "{upload_id}", "parts": [{"partNumber": "{part_number}", "etag": "{etag}"}]},
        }),
        "abort": UploadRequest({
            "method": "POST",
            "url": f"{base}/abort",
            "headers": json_auth,
            "body": {"uploadId": "{upload_id}"},
        }),
    }


def _fill_text(text, variables, *, for_url=False):
    def replace(match):
        name = match.group(1)
        if name not in variables:
            return match.group(0)
        value = str(variables[name])
        return quote(value, safe="") if for_url else value

    return _PLACEHOLDER.sub(replace, text)


def _fill(value, variables, parts=None):
    """Fill {placeholders} in a descriptor value; a one-item list holding {part_number} becomes one item per part."""
    if isinstance(value, str):
        exact = _EXACT_PLACEHOLDER.match(value)
        if exact and exact.group(1) in variables:
            return variables[exact.group(1)]
        return _fill_text(value, variables)
    if isinstance(value, dict):
        return {key: _fill(item, variables, parts) for key, item in value.items()}
    if isinstance(value, list):
        if parts is not None and len(value) == 1 and "{part_number}" in json.dumps(value[0]):
            return [_fill(value[0], dict(variables, part_number=number, etag=etag)) for number, etag in parts]
        return [_fill(item, variables, parts) for item in value]
    return value


class _Source:
    """A binary stream over a path, bytes or a file object, with its size when it can be known."""

    def __init__(self, source):
        self._close = False
        if isinstance(source, (str, os.PathLike)):
            path = os.fspath(source)
            self.stream = open(path, "rb")
            self._close = True
            self.size = os.path.getsize(path)
            self.label = path
        elif isinstance(source, (bytes, bytearray, memoryview)):
            data = bytes(source)
            self.stream = io.BytesIO(data)
            self.size = len(data)
            self.label = "bytes"
        elif hasattr(source, "read"):
            self.stream = source
            self.size = None
            self.label = getattr(source, "name", "file object")
            try:
                if source.seekable():
                    start = source.tell()
                    source.seek(0, os.SEEK_END)
                    self.size = source.tell() - start
                    source.seek(start)
            except (AttributeError, OSError, ValueError):
                self.size = None
        else:
            raise ValueError("file must be a path, bytes or a binary file object")

    def read(self, count):
        data = self.stream.read(count)
        if isinstance(data, str):
            raise ValueError("file objects must be opened in binary mode ('rb')")
        return data or b""

    def close(self):
        if self._close:
            self.stream.close()


def _edge_request(client, descriptor, variables, options, *, content=None, retry_safe=True, parts=None):
    url = _fill_text(str(descriptor.url), variables, for_url=True)
    headers = {"User-Agent": sdk_identity_headers()["User-Agent"]}
    for name, value in (descriptor.headers or {}).items():
        if str(name).lower() == "content-length":
            continue  # httpx sets it from the body
        headers[name] = _fill_text(str(value), variables)
    body_template = descriptor.body
    if content is None and body_template is not None:
        content = json.dumps(_fill(body_template, variables, parts)).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    kwargs = {}
    if content is not None:
        kwargs["content"] = content
    response = client._send(
        str(descriptor.method or "POST").upper(),
        url,
        headers=headers,
        retry_safe=retry_safe,
        options=options,
        **kwargs,
    )
    try:
        if not 200 <= response.status_code < 300:
            raise api_error_from_response(response)
        return parse_json_response(response)
    finally:
        response.close()


def upload_bytes(client, instructions, source, request_options=None):
    """Send source to the media host by following the project's upload instructions (start, parts, complete).

    Each part is retried on its own under the client's retry policy; any failure after start aborts the
    multipart upload before the error is raised. Returns the media host's answer to complete
    ({"size_bytes", "etag"}).
    """
    instructions = UploadInstructions.from_dict(instructions)
    if not instructions or not instructions.upload_token:
        raise DiffioError("The project has no upload instructions; call projects.reopen_upload first")
    options = _request_options(request_options)
    requests = dict(_default_upload_requests(instructions))
    for name, descriptor in (instructions.requests or {}).items():
        requests[name] = UploadRequest.from_dict(descriptor)
    max_bytes = instructions.max_bytes

    reader = _Source(source)
    try:
        if reader.size is not None and max_bytes and reader.size > max_bytes:
            raise ValueError(f"{reader.label} is {reader.size} bytes; the upload limit is {max_bytes} bytes")

        started = _edge_request(client, requests["start"], {}, options)
        upload_id = started.get("uploadId") if isinstance(started, dict) else None
        if not isinstance(upload_id, str) or not upload_id:
            raise DiffioApiError("The upload start answer has no uploadId", code="invalid_response", body=started)
        part_size = started.get("partSizeBytes")
        if not isinstance(part_size, int) or isinstance(part_size, bool) or part_size <= 0:
            part_size = int(instructions.part_size_bytes or 32 * 1024 * 1024)
        variables = {"upload_id": upload_id}

        try:
            parts = []
            sent = 0
            number = 1
            while True:
                chunk = reader.read(part_size)
                if not chunk and parts:
                    break
                sent += len(chunk)
                if max_bytes and sent > max_bytes:
                    raise ValueError(f"{reader.label} is larger than the upload limit of {max_bytes} bytes")
                receipt = _edge_request(
                    client,
                    requests["part"],
                    dict(variables, part_number=number, part_length=len(chunk)),
                    options,
                    content=chunk,
                )
                etag = receipt.get("etag") if isinstance(receipt, dict) else None
                if not isinstance(etag, str) or not etag:
                    raise DiffioApiError(
                        f"The upload answer for part {number} has no etag", code="invalid_response", body=receipt
                    )
                parts.append((number, etag))
                number += 1
                if len(chunk) < part_size:
                    break
            completed = _edge_request(client, requests["complete"], variables, options, parts=parts)
        except BaseException:
            try:
                abort = requests.get("abort")
                if abort is not None:
                    _edge_request(client, abort, variables, options, retry_safe=False)
            except Exception:
                pass  # a failed abort never hides the upload error
            raise
    finally:
        reader.close()
    completed = completed if isinstance(completed, dict) else {}
    return {"size_bytes": completed.get("sizeBytes"), "etag": completed.get("etag")}


def media_file_url(media, file):
    files = getattr(media, "files", None) or {}
    url = files.get(file)
    if not url:
        available = ", ".join(sorted(files)) or "none"
        raise DiffioError(
            f"{file} is not available for generation {getattr(media, 'generation_id', '')} "
            f"(status {getattr(media, 'status', None)}; files: {available})"
        )
    return url


def download_url(client, url, *, path=None, request_options=None):
    """GET a media URL. With path, stream it to that file (atomically) and return the path; else return bytes.

    A path that is an existing directory gets the URL's file name inside it.
    """
    options = _request_options(request_options)
    headers = {"User-Agent": sdk_identity_headers()["User-Agent"]}
    if path is None:
        response = client._send("GET", url, headers=headers, retry_safe=True, options=options)
        try:
            if not 200 <= response.status_code < 300:
                raise api_error_from_response(response)
            return response.content
        finally:
            response.close()

    target = os.fspath(path)
    if os.path.isdir(target):
        target = os.path.join(target, os.path.basename(urlparse(url).path) or "download")
    directory = os.path.dirname(os.path.abspath(target))
    max_retries = options.max_retries if options.max_retries is not None else client.max_retries
    attempt = 0
    while True:
        # A plain open() (not mkstemp, which makes the file 0600) so the file gets the usual umask permissions.
        temp_path = os.path.join(directory, f".diffio-download-{uuid.uuid4().hex}")
        try:
            with open(temp_path, "xb") as handle:
                response = client._send("GET", url, headers=headers, retry_safe=True, options=options, stream=True)
                try:
                    if not 200 <= response.status_code < 300:
                        response.read()
                        raise api_error_from_response(response)
                    for chunk in response.iter_bytes():
                        handle.write(chunk)
                finally:
                    response.close()
            os.replace(temp_path, target)
            return target
        except DiffioApiError:
            _remove(temp_path)
            raise
        except httpx.TransportError as exc:
            _remove(temp_path)
            # The connection dropped mid-body: start the file again.
            if attempt < max_retries:
                attempt += 1
                continue
            raise connection_error(exc)
        except BaseException:
            _remove(temp_path)
            raise


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass
