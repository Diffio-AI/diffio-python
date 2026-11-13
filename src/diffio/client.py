import json
import mimetypes
import os
import time
import uuid
from urllib.parse import quote

import httpx

from .errors import DiffioApiError, DiffioError
from .types import ApiObject, RestoreResult

DEFAULT_BASE_URL = "https://api.diffio.ai"
API_VERSION_PATH = "/v2"
DEFAULT_MODEL = "diffio-flash"
DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_RETRY_BACKOFF = 0.5
MAX_RETRY_DELAY = 30.0
RETRY_STATUS_CODES = (408, 429, 500, 502, 503, 504)
# Completing an upload probes the file inside the request; the API allows it up to 180 seconds.
COMPLETE_UPLOAD_TIMEOUT = 180.0
END_USER_HEADER = "Diffio-End-User"
IDEMPOTENCY_HEADER = "Idempotency-Key"

# Code for an answer that carries no error.code of its own, by HTTP status (same table as the API).
_STATUS_ERROR_CODES = {
    400: "invalid_request",
    401: "unauthenticated",
    402: "payment_required",
    403: "permission_denied",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    411: "length_required",
    413: "payload_too_large",
    415: "unsupported_media_type",
    429: "rate_limited",
    500: "internal_error",
    502: "internal_error",
    503: "unavailable",
    504: "unavailable",
}

_NO_BODY = object()


class RequestOptions:
    """Per-call overrides: extra headers, timeout (seconds), max_retries, retry_backoff, api_key."""

    def __init__(self, *, headers=None, timeout=None, max_retries=None, retry_backoff=None, api_key=None):
        self.headers = dict(headers or {})
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.api_key = api_key


def _request_options(value):
    if value is None:
        return RequestOptions()
    if isinstance(value, RequestOptions):
        return value
    if isinstance(value, dict):
        known = {"headers", "timeout", "max_retries", "retry_backoff", "api_key"}
        unknown = sorted(set(value) - known)
        if unknown:
            raise ValueError(f"Unknown request option(s): {', '.join(unknown)}")
        return RequestOptions(**value)
    raise ValueError("request_options must be a RequestOptions or a dict")


def sdk_identity_headers():
    """Headers that name this SDK and its version to the Diffio API."""
    from . import __version__

    return {
        "User-Agent": f"diffio-python/{__version__}",
        "X-Diffio-SDK-Language": "Python",
        "X-Diffio-SDK-Name": "diffio",
        "X-Diffio-SDK-Version": __version__,
    }


def resolve_base_url(base_url=None):
    """The v2 base URL: base_url, else DIFFIO_API_BASE_URL, else https://api.diffio.ai, ending in /v2."""
    resolved = (base_url or os.environ.get("DIFFIO_API_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/")
    if resolved.endswith(API_VERSION_PATH):
        return resolved
    # Older setups pointed DIFFIO_API_BASE_URL at .../v1; this release speaks /v2 only.
    if resolved.endswith("/v1"):
        resolved = resolved[: -len("/v1")]
    return resolved + API_VERSION_PATH


def path_id(value, name):
    """The id of value (an id string or an object with .id), percent-encoded for a URL path."""
    if isinstance(value, ApiObject):
        value = value.get("id")
    if value is None or not str(value).strip():
        raise ValueError(f"{name} is required")
    return quote(str(value), safe="")


def drop_none(values):
    return {key: value for key, value in values.items() if value is not None}


def _retry_delay(attempt, backoff, response=None):
    if response is not None:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return min(max(float(retry_after), 0.0), MAX_RETRY_DELAY)
            except ValueError:
                pass
    if not backoff:
        return 0.0
    return min(backoff * (2 ** attempt), MAX_RETRY_DELAY)


def _sleep(seconds):
    if seconds > 0:
        time.sleep(seconds)


def api_error_from_response(response):
    """Build the DiffioApiError for a non-2xx answer of the API or the media host."""
    status = response.status_code
    request_id = response.headers.get("X-Request-Id")
    body = None
    code = None
    message = None
    details = None
    try:
        body = response.json() if response.content else None
    except ValueError:
        body = response.text or None
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            message = error.get("message")
            details = error.get("details")
            request_id = error.get("request_id") or request_id
        elif isinstance(error, str):
            message = error
    code = code or _STATUS_ERROR_CODES.get(status) or ("internal_error" if status >= 500 else "invalid_request")
    message = message or f"Request failed with status {status}"
    return DiffioApiError(
        message,
        status=status,
        code=code,
        request_id=request_id,
        details=details,
        body=body,
        headers=dict(response.headers),
    )


def connection_error(exc):
    error = DiffioApiError(f"Could not reach Diffio: {exc}", code="connection_error")
    error.__cause__ = exc
    return error


def parse_json_response(response):
    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError:
        raise DiffioApiError(
            "The answer was not JSON",
            status=response.status_code,
            code="invalid_response",
            request_id=response.headers.get("X-Request-Id"),
            body=response.text,
        ) from None


class DiffioClient:
    """Client of the Diffio API v2.

    api_key defaults to DIFFIO_API_KEY. base_url defaults to DIFFIO_API_BASE_URL, else https://api.diffio.ai;
    /v2 is added. end_user, when set, is sent as Diffio-End-User on every call (a call's own end_user wins).
    Calls that are safe to repeat are retried on 408, 429, 5xx and network errors, max_retries times, with
    exponential backoff from retry_backoff seconds (Retry-After is honoured).
    """

    def __init__(
        self,
        api_key=None,
        *,
        base_url=None,
        end_user=None,
        timeout=DEFAULT_TIMEOUT,
        max_retries=DEFAULT_MAX_RETRIES,
        retry_backoff=DEFAULT_RETRY_BACKOFF,
        headers=None,
        http_client=None,
    ):
        from .resources import (
            AssistantResource,
            EditsResource,
            ExportsResource,
            GenerationsResource,
            MixResource,
            ModelsResource,
            ProjectsResource,
            SharesResource,
            TranscriptsResource,
            UsageResource,
            WebhooksResource,
        )

        self.api_key = api_key or os.environ.get("DIFFIO_API_KEY") or None
        self.base_url = resolve_base_url(base_url)
        self.end_user = end_user
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.headers = dict(headers or {})
        if http_client is None:
            self._http = httpx.Client(timeout=timeout)
            self._owns_http = True
        else:
            self._http = http_client
            self._owns_http = False

        self.models = ModelsResource(self)
        self.projects = ProjectsResource(self)
        self.generations = GenerationsResource(self)
        self.edits = EditsResource(self)
        self.mix = MixResource(self)
        self.exports = ExportsResource(self)
        self.transcripts = TranscriptsResource(self)
        self.assistant = AssistantResource(self)
        self.shares = SharesResource(self)
        self.usage = UsageResource(self)
        self.webhooks = WebhooksResource(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def close(self):
        if self._owns_http:
            self._http.close()

    def restore_audio(
        self,
        file_path,
        *,
        model=DEFAULT_MODEL,
        output_path=None,
        file="audio",
        file_name=None,
        content_type=None,
        label=None,
        sampling=None,
        metadata=None,
        end_user=None,
        idempotency_key=None,
        timeout=3600.0,
        poll_interval=2.0,
        on_progress=None,
        request_options=None,
    ):
        """Restore one recording: create the project, upload, generate, wait, download.

        file_path is a path, bytes or a binary file object (bytes and file objects need file_name). The
        generation is requested with the project, so it starts as soon as the upload is ready. The result
        file (file="audio" is the restored MP3; any name of GET .../media files works) is written to
        output_path, or returned as RestoreResult.content when output_path is None. Raises
        GenerationFailedError when the restore fails and DiffioTimeoutError after timeout seconds.
        """
        resolved_name = file_name
        size_bytes = None
        if isinstance(file_path, (str, os.PathLike)):
            resolved_path = os.fspath(file_path)
            resolved_name = resolved_name or os.path.basename(resolved_path)
            size_bytes = os.path.getsize(resolved_path)
        elif isinstance(file_path, (bytes, bytearray, memoryview)):
            size_bytes = len(file_path)
        if not resolved_name:
            raise ValueError("file_name is required when file_path is bytes or a file object")
        guessed_type = content_type or mimetypes.guess_type(resolved_name)[0]

        generation = {"model": model}
        if label is not None:
            generation["label"] = label
        if sampling is not None:
            generation["sampling"] = sampling

        project = self.projects.create(
            file_name=resolved_name,
            size_bytes=size_bytes,
            content_type=guessed_type,
            metadata=metadata,
            generation=generation,
            end_user=end_user,
            idempotency_key=idempotency_key,
            request_options=request_options,
        )
        if project.latest_generation is None:
            raise DiffioError("The project answer named no generation")
        uploaded = self.projects.upload(
            project, file_path, end_user=end_user, request_options=request_options
        )
        finished = self.generations.wait(
            project.id,
            project.latest_generation.id,
            timeout=timeout,
            poll_interval=poll_interval,
            on_progress=on_progress,
            end_user=end_user,
            request_options=request_options,
        )
        media = self.generations.media(project.id, finished.id, end_user=end_user, request_options=request_options)
        from .transfers import download_url, media_file_url

        url = media_file_url(media, file)
        if output_path is not None:
            saved = download_url(self, url, path=output_path, request_options=request_options)
            return RestoreResult(uploaded or project, finished, media, output_path=saved)
        content = download_url(self, url, request_options=request_options)
        return RestoreResult(uploaded or project, finished, media, content=content)

    # ---- transport ------------------------------------------------------------------------------------

    def _headers(self, options, *, auth, end_user, idempotency_key):
        headers = sdk_identity_headers()
        headers["Accept"] = "application/json"
        headers.update(self.headers)
        api_key = options.api_key or self.api_key
        if auth and api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        if end_user is None:
            end_user = self.end_user
        if end_user is not None and end_user is not False:
            headers[END_USER_HEADER] = str(end_user)
        if idempotency_key is not None:
            headers[IDEMPOTENCY_HEADER] = str(idempotency_key)
        headers.update(options.headers)
        return headers

    def _send(self, method, url, *, headers, retry_safe, options, timeout=None, **kwargs):
        """Send one request with the retry policy; return the final httpx.Response (any status).

        kwargs go to httpx build_request (params, content); stream=True leaves the body unread.
        """
        max_retries = options.max_retries if options.max_retries is not None else self.max_retries
        if not retry_safe:
            max_retries = 0
        backoff = options.retry_backoff if options.retry_backoff is not None else self.retry_backoff
        resolved_timeout = options.timeout if options.timeout is not None else timeout
        if resolved_timeout is not None:
            kwargs["timeout"] = resolved_timeout
        stream = kwargs.pop("stream", False)
        attempt = 0
        while True:
            try:
                request = self._http.build_request(method, url, headers=headers, **kwargs)
                response = self._http.send(request, stream=stream)
            except httpx.TransportError as exc:
                if attempt >= max_retries:
                    raise connection_error(exc)
                _sleep(_retry_delay(attempt, backoff))
                attempt += 1
                continue
            if response.status_code in RETRY_STATUS_CODES and attempt < max_retries:
                delay = _retry_delay(attempt, backoff, response)
                response.close()
                _sleep(delay)
                attempt += 1
                continue
            return response

    def _request(
        self,
        method,
        path,
        *,
        query=None,
        body=_NO_BODY,
        auth=True,
        end_user=None,
        idempotency_key=None,
        retry_safe=None,
        timeout=None,
        request_options=None,
    ):
        options = _request_options(request_options)
        headers = self._headers(options, auth=auth, end_user=end_user, idempotency_key=idempotency_key)
        if retry_safe is None:
            retry_safe = method in ("GET", "PUT", "DELETE") or idempotency_key is not None
        kwargs = {}
        if query:
            kwargs["params"] = drop_none(query)
        if body is not _NO_BODY:
            headers["Content-Type"] = "application/json"
            kwargs["content"] = json.dumps(body).encode("utf-8")
        response = self._send(
            method,
            self.base_url + path,
            headers=headers,
            retry_safe=retry_safe,
            options=options,
            timeout=timeout,
            **kwargs,
        )
        try:
            if not 200 <= response.status_code < 300:
                raise api_error_from_response(response)
            return parse_json_response(response)
        finally:
            response.close()

    @staticmethod
    def _new_idempotency_key():
        return f"diffio-python-{uuid.uuid4().hex}"
