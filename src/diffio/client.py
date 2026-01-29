import json
import mimetypes
import os
import tempfile
import time
import warnings
from urllib.parse import urlparse

import httpx

from .errors import DiffioApiError
from .types import (
    AudioIsolationResult,
    CreateGenerationResponse,
    CreateProjectResponse,
    DownloadType,
    GenerationDownloadResponse,
    GenerationProgressResponse,
    ListProjectGenerationsResponse,
    ListProjectsResponse,
    ModelKey,
    WebhookEventType,
    WebhookMode,
    WebhookPortalResponse,
    WebhookTestEventResponse,
)

DEFAULT_BASE_URL = "https://us-central1-diffioai.cloudfunctions.net"
API_PREFIX = "v1"
MODEL_ENDPOINTS = {
    "diffio-2": "diffio-2.0-generation",
    "diffio-2-flash": "diffio-2.0-flash-generation",
    "diffio-3": "diffio-3.0-generation",
}
DEFAULT_RETRY_STATUS_CODES = [408, 429, 500, 502, 503, 504]
DEFAULT_RETRY_BACKOFF = 0.5


class RequestOptions:
    def __init__(
        self,
        *,
        headers=None,
        timeout=None,
        timeoutInSeconds=None,
        maxRetries=None,
        retryBackoff=None,
        retryStatusCodes=None,
        apiKey=None,
    ):
        self.headers = headers or {}
        self.timeout = timeoutInSeconds if timeoutInSeconds is not None else timeout
        self.maxRetries = maxRetries
        self.retryBackoff = retryBackoff
        self.retryStatusCodes = retryStatusCodes
        self.apiKey = apiKey


def _normalize_request_options(options):
    if options is None:
        return RequestOptions()
    if isinstance(options, RequestOptions):
        return options
    if isinstance(options, dict):
        timeout = options.get("timeout")
        timeout_in_seconds = options.get("timeoutInSeconds")
        if timeout_in_seconds is not None:
            timeout = timeout_in_seconds
        return RequestOptions(
            headers=options.get("headers"),
            timeout=timeout,
            maxRetries=options.get("maxRetries"),
            retryBackoff=options.get("retryBackoff"),
            retryStatusCodes=options.get("retryStatusCodes"),
            apiKey=options.get("apiKey"),
        )
    raise ValueError("requestOptions must be a RequestOptions or dict")


def _merge_request_options(base_options, override_options):
    if override_options is None:
        return base_options
    override = _normalize_request_options(override_options)
    headers = {}
    if base_options.headers:
        headers.update(base_options.headers)
    if override.headers:
        headers.update(override.headers)
    return RequestOptions(
        headers=headers,
        timeout=override.timeout if override.timeout is not None else base_options.timeout,
        maxRetries=override.maxRetries if override.maxRetries is not None else base_options.maxRetries,
        retryBackoff=override.retryBackoff if override.retryBackoff is not None else base_options.retryBackoff,
        retryStatusCodes=(
            override.retryStatusCodes
            if override.retryStatusCodes is not None
            else base_options.retryStatusCodes
        ),
        apiKey=override.apiKey if override.apiKey is not None else base_options.apiKey,
    )


def _default_request_options():
    return RequestOptions(
        headers={},
        timeout=None,
        maxRetries=0,
        retryBackoff=DEFAULT_RETRY_BACKOFF,
        retryStatusCodes=list(DEFAULT_RETRY_STATUS_CODES),
        apiKey=None,
    )


class DiffioClient:
    def __init__(
        self,
        *,
        apiKey=None,
        baseUrl=None,
        timeout=60.0,
        timeoutInSeconds=None,
        httpClient=None,
        requestOptions=None,
    ):
        resolved_key = apiKey or os.environ.get("DIFFIO_API_KEY")
        if not resolved_key:
            raise ValueError("apiKey is required")

        resolved_base = baseUrl or os.environ.get("DIFFIO_API_BASE_URL") or DEFAULT_BASE_URL
        resolved_base = resolved_base.rstrip("/")
        api_prefix = "" if resolved_base.endswith(f"/{API_PREFIX}") else API_PREFIX

        self.apiKey = resolved_key
        self.baseUrl = resolved_base
        self._api_prefix = api_prefix
        resolved_timeout = timeoutInSeconds if timeoutInSeconds is not None else timeout
        if httpClient is None:
            self._client = httpx.Client(base_url=resolved_base, timeout=resolved_timeout)
            self._owns_client = True
        else:
            self._client = httpClient
            self._owns_client = False
        self._default_request_options = _merge_request_options(_default_request_options(), requestOptions)

        self.audio_isolation = AudioIsolationClient(self)
        self.generations = GenerationsClient(self)
        self.projects = ProjectsClient(self)
        self.webhooks = WebhooksClient(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def close(self):
        if self._owns_client:
            self._client.close()

    def create_project(
        self,
        *,
        filePath,
        contentType=None,
        contentLength=None,
        params=None,
        fileFormat=None,
        requestOptions=None,
    ):
        payload = _build_create_project_payload(
            filePath=filePath,
            contentType=contentType,
            contentLength=contentLength,
            params=params,
            fileFormat=fileFormat,
        )

        response = self._request("POST", "create_project", json_payload=payload, requestOptions=requestOptions)
        project = CreateProjectResponse.from_dict(response)
        self._upload_file(
            uploadUrl=project.uploadUrl,
            uploadMethod=project.uploadMethod,
            filePath=filePath,
            contentType=payload["contentType"],
            requestOptions=requestOptions,
        )
        return project

    def _upload_file(
        self,
        *,
        uploadUrl,
        uploadMethod=None,
        filePath=None,
        data=None,
        contentType=None,
        requestOptions=None,
    ):
        if (filePath is None) == (data is None):
            raise ValueError("Provide filePath or data")

        resolved_content_type = contentType
        if resolved_content_type is None and filePath is not None:
            resolved_content_type = _guess_content_type(filePath)
        if resolved_content_type is None:
            resolved_content_type = "application/octet-stream"

        method = (uploadMethod or "PUT").upper()
        merged_options = _merge_request_options(self._default_request_options, requestOptions)
        headers = {"Content-Type": resolved_content_type}
        if _is_storage_emulator_url(uploadUrl):
            headers["Authorization"] = "Bearer owner"
        headers = _merge_headers(headers, merged_options.headers)
        timeout = merged_options.timeout
        max_retries = merged_options.maxRetries if merged_options.maxRetries is not None else 0
        retry_backoff = (
            merged_options.retryBackoff if merged_options.retryBackoff is not None else DEFAULT_RETRY_BACKOFF
        )
        retry_statuses = (
            merged_options.retryStatusCodes
            if merged_options.retryStatusCodes is not None
            else DEFAULT_RETRY_STATUS_CODES
        )

        attempt = 0
        while True:
            try:
                if filePath is not None:
                    with open(filePath, "rb") as handle:
                        request_kwargs = {
                            "method": method,
                            "url": uploadUrl,
                            "headers": headers,
                            "content": handle,
                        }
                        if timeout is not None:
                            request_kwargs["timeout"] = timeout
                        response = self._client.request(**request_kwargs)
                else:
                    request_kwargs = {
                        "method": method,
                        "url": uploadUrl,
                        "headers": headers,
                        "content": data,
                    }
                    if timeout is not None:
                        request_kwargs["timeout"] = timeout
                    response = self._client.request(**request_kwargs)
            except httpx.RequestError:
                if attempt >= max_retries:
                    raise
                _sleep_retry(attempt, retry_backoff)
                attempt += 1
                continue

            if retry_statuses and response.status_code in retry_statuses and attempt < max_retries:
                response.close()
                _sleep_retry(attempt, retry_backoff)
                attempt += 1
                continue

            _raise_for_error(response)
            return

    def create_generation(
        self,
        *,
        apiProjectId,
        model="diffio-2",
        sampling=None,
        params=None,
        requestOptions=None,
    ):
        endpoint = MODEL_ENDPOINTS.get(model)
        if not endpoint:
            raise ValueError(f"Unsupported model: {model}")

        payload = {"apiProjectId": apiProjectId}
        if sampling is not None:
            payload["sampling"] = sampling
        if params:
            payload["params"] = params

        response = self._request("POST", endpoint, json_payload=payload, requestOptions=requestOptions)
        return CreateGenerationResponse.from_dict(response)

    def list_projects(self, *, requestOptions=None):
        """
        Lists projects owned by the API key.

        Returns
        -------
        ListProjectsResponse
            Project summaries ordered by creation time.

        Examples
        --------
        from diffio import DiffioClient

        client = DiffioClient(apiKey="diffio_live_...")
        projects = client.list_projects()
        print(projects.projects[0].apiProjectId)
        """
        response = self._request("POST", "list_projects", json_payload={}, requestOptions=requestOptions)
        return ListProjectsResponse.from_dict(response)

    def list_project_generations(
        self,
        *,
        apiProjectId,
        requestOptions=None,
    ):
        """
        Lists generations for a project owned by the API key.

        Parameters
        ----------
        apiProjectId : str
            The project id to list generations for.

        Returns
        -------
        ListProjectGenerationsResponse
            Generation summaries ordered by creation time.

        Examples
        --------
        from diffio import DiffioClient

        client = DiffioClient(apiKey="diffio_live_...")
        generations = client.list_project_generations(apiProjectId="proj_123")
        print(generations.generations[0].generationId)
        """
        if not apiProjectId:
            raise ValueError("apiProjectId is required")
        response = self._request(
            "POST",
            "list_project_generations",
            json_payload={"apiProjectId": apiProjectId},
            requestOptions=requestOptions,
        )
        return ListProjectGenerationsResponse.from_dict(response)

    def get_generation_progress(
        self,
        *,
        generationId,
        apiProjectId=None,
        requestOptions=None,
    ):
        """
        Gets progress for a generation.

        Parameters
        ----------
        generationId : str
            The generation id to query.
        apiProjectId : str, optional
            Optional project id, used to speed up lookup.

        Returns
        -------
        GenerationProgressResponse
            Current generation progress.

        Examples
        --------
        from diffio import DiffioClient

        client = DiffioClient(apiKey="diffio_live_...")
        progress = client.get_generation_progress(
            generationId="gen_123",
            apiProjectId="proj_123",
        )
        print(progress.status)
        """
        payload = {"generationId": generationId}
        if apiProjectId is not None:
            payload["apiProjectId"] = apiProjectId

        response = self._request(
            "POST",
            "get_generation_progress",
            json_payload=payload,
            requestOptions=requestOptions,
        )
        return GenerationProgressResponse.from_dict(response)

    def wait_for_generation(
        self,
        *,
        generationId,
        apiProjectId=None,
        pollInterval=2.0,
        timeout=600.0,
        onProgress=None,
        showProgress=False,
        requestOptions=None,
    ):
        """
        Polls generation progress until completion or failure.

        Parameters
        ----------
        generationId : str
            The generation id to wait for.
        apiProjectId : str, optional
            Optional project id, used to speed up lookup.
        pollInterval : float
            Seconds to wait between polls.
        timeout : float
            Maximum seconds to wait before timing out.
        onProgress : callable, optional
            Callback invoked with each GenerationProgressResponse.
        showProgress : bool
            If true, prints progress updates to stdout.
        """
        deadline = time.monotonic() + timeout
        last_progress = None

        while time.monotonic() < deadline:
            progress = self.get_generation_progress(
                generationId=generationId,
                apiProjectId=apiProjectId,
                requestOptions=requestOptions,
            )
            last_progress = progress
            _report_progress(progress, onProgress=onProgress, showProgress=showProgress)

            if progress.status == "complete":
                return progress

            if progress.status == "failed":
                raise RuntimeError(
                    "Generation failed"
                    f" (preProcessing={progress.preProcessing.status},"
                    f" inference={progress.inference.status},"
                    f" error={progress.error},"
                    f" details={progress.errorDetails})"
                )

            time.sleep(pollInterval)

        raise RuntimeError(
            "Timed out waiting for generation completion"
            f" (lastStatus={getattr(last_progress, 'status', None)})"
        )

    def get_generation_download(
        self,
        *,
        generationId,
        apiProjectId,
        downloadType=None,
        requestOptions=None,
    ):
        """
        Gets a signed download URL for a generation.

        Parameters
        ----------
        generationId : str
            The generation id to download.
        apiProjectId : str
            The project id that owns the generation.
        downloadType : str, optional
            Optional download type, audio, mp3, or video.

        Returns
        -------
        GenerationDownloadResponse
            Signed download URL and file metadata.

        Examples
        --------
        from diffio import DiffioClient

        client = DiffioClient(apiKey="diffio_live_...")
        download = client.get_generation_download(
            generationId="gen_123",
            apiProjectId="proj_123",
            downloadType="audio",
        )
        print(download.downloadUrl)
        """
        payload = {"generationId": generationId, "apiProjectId": apiProjectId}
        if downloadType is not None:
            resolved_download_type, _ = _normalize_download_type(downloadType)
            payload["downloadType"] = resolved_download_type

        response = self._request(
            "POST",
            "get_generation_download",
            json_payload=payload,
            requestOptions=requestOptions,
        )
        return GenerationDownloadResponse.from_dict(response)

    def get_webhooks_portal_access(
        self,
        *,
        mode,
        apiKeyId=None,
        requestOptions=None,
    ):
        """
        Gets the Svix App Portal URL for configuring webhooks.

        Parameters
        ----------
        mode : str
            Webhook mode, test or live.
        apiKeyId : str, optional
            Optional API key id to validate access.

        Returns
        -------
        WebhookPortalResponse
            App portal URL and mode.
        """
        if mode not in WebhookMode:
            raise ValueError("mode must be test or live")
        payload = {"mode": mode}
        if apiKeyId is not None:
            payload["apiKeyId"] = apiKeyId

        response = self._request(
            "POST",
            "webhooks/app_portal_access",
            json_payload=payload,
            requestOptions=requestOptions,
        )
        return WebhookPortalResponse.from_dict(response)

    def send_webhook_test_event(
        self,
        *,
        eventType,
        mode,
        apiKeyId=None,
        samplePayload=None,
        requestOptions=None,
    ):
        """
        Sends a test webhook event.

        Parameters
        ----------
        eventType : str
            One of the supported generation webhook event types.
        mode : str
            Webhook mode, test or live.
        apiKeyId : str, optional
            Optional API key id to validate access.
        samplePayload : dict, optional
            Sample payload overrides. Must be a dict.

        Returns
        -------
        WebhookTestEventResponse
            Webhook message metadata.
        """
        if eventType not in WebhookEventType:
            raise ValueError("eventType is not supported")
        if mode not in WebhookMode:
            raise ValueError("mode must be test or live")
        if samplePayload is not None and not isinstance(samplePayload, dict):
            raise ValueError("samplePayload must be an object")

        payload = {"eventType": eventType, "mode": mode}
        if apiKeyId is not None:
            payload["apiKeyId"] = apiKeyId
        if samplePayload is not None:
            payload["samplePayload"] = samplePayload

        response = self._request(
            "POST",
            "webhooks/send_test_event",
            json_payload=payload,
            requestOptions=requestOptions,
        )
        return WebhookTestEventResponse.from_dict(response)

    def restore_audio(
        self,
        *,
        filePath,
        contentType=None,
        contentLength=None,
        fileFormat=None,
        model="diffio-2",
        sampling=None,
        projectParams=None,
        generationParams=None,
        downloadType="audio",
        pollInterval=2.0,
        timeout=600.0,
        onProgress=None,
        showProgress=False,
        requestOptions=None,
        progressRequestOptions=None,
        downloadRequestOptions=None,
        raiseOnError=False,
    ):
        return self.audio_isolation.restore_audio(
            filePath=filePath,
            contentType=contentType,
            contentLength=contentLength,
            fileFormat=fileFormat,
            model=model,
            sampling=sampling,
            projectParams=projectParams,
            generationParams=generationParams,
            downloadType=downloadType,
            pollInterval=pollInterval,
            timeout=timeout,
            onProgress=onProgress,
            showProgress=showProgress,
            requestOptions=requestOptions,
            progressRequestOptions=progressRequestOptions,
            downloadRequestOptions=downloadRequestOptions,
            raiseOnError=raiseOnError,
        )

    def _request(self, method, path, *, json_payload, requestOptions=None):
        request_path = path.lstrip("/")
        if self._api_prefix:
            request_path = f"{self._api_prefix}/{request_path}"
        merged_options = _merge_request_options(self._default_request_options, requestOptions)
        api_key = merged_options.apiKey or self.apiKey
        headers = _merge_headers({"Authorization": f"Bearer {api_key}"}, merged_options.headers)
        timeout = merged_options.timeout
        max_retries = merged_options.maxRetries if merged_options.maxRetries is not None else 0
        retry_backoff = (
            merged_options.retryBackoff if merged_options.retryBackoff is not None else DEFAULT_RETRY_BACKOFF
        )
        retry_statuses = (
            merged_options.retryStatusCodes
            if merged_options.retryStatusCodes is not None
            else DEFAULT_RETRY_STATUS_CODES
        )

        attempt = 0
        while True:
            try:
                request_kwargs = {
                    "method": method,
                    "url": request_path,
                    "headers": headers,
                    "json": json_payload,
                }
                if timeout is not None:
                    request_kwargs["timeout"] = timeout
                response = self._client.request(**request_kwargs)
            except httpx.RequestError:
                if attempt >= max_retries:
                    raise
                _sleep_retry(attempt, retry_backoff)
                attempt += 1
                continue

            if retry_statuses and response.status_code in retry_statuses and attempt < max_retries:
                response.close()
                _sleep_retry(attempt, retry_backoff)
                attempt += 1
                continue

            return _raise_for_error(response)


class GenerationsClient:
    def __init__(self, parent):
        self._parent = parent

    def create(
        self,
        *,
        apiProjectId,
        model="diffio-2",
        sampling=None,
        params=None,
        requestOptions=None,
    ):
        return self._parent.create_generation(
            apiProjectId=apiProjectId,
            model=model,
            sampling=sampling,
            params=params,
            requestOptions=requestOptions,
        )

    def get_progress(
        self,
        *,
        generationId,
        apiProjectId=None,
        requestOptions=None,
    ):
        return self._parent.get_generation_progress(
            generationId=generationId,
            apiProjectId=apiProjectId,
            requestOptions=requestOptions,
        )

    def get_download(
        self,
        *,
        generationId,
        apiProjectId,
        downloadType=None,
        requestOptions=None,
    ):
        return self._parent.get_generation_download(
            generationId=generationId,
            apiProjectId=apiProjectId,
            downloadType=downloadType,
            requestOptions=requestOptions,
        )

    def download(
        self,
        *,
        generationId,
        apiProjectId,
        downloadFilePath,
        downloadType=None,
        requestOptions=None,
    ):
        """
        Downloads a generation result directly to a file path.

        Parameters
        ----------
        generationId : str
            The generation id to download.
        apiProjectId : str
            The project id that owns the generation.
        downloadFilePath : str
            Local file path to write the downloaded media to.
        downloadType : str, optional
            Optional download type, audio, mp3, or video.

        Returns
        -------
        GenerationDownloadResponse
            Download metadata for the saved file.
        """
        resolved_path = os.fspath(downloadFilePath)
        if not resolved_path:
            raise ValueError("downloadFilePath is required")
        if downloadType is not None:
            resolved_download_type, _ = _normalize_download_type(downloadType)
        else:
            resolved_download_type = None

        download = self._parent.get_generation_download(
            generationId=generationId,
            apiProjectId=apiProjectId,
            downloadType=resolved_download_type,
            requestOptions=requestOptions,
        )
        _warn_download_extension_mismatch(download, resolved_path)
        _download_to_file(self._parent, download.downloadUrl, resolved_path, requestOptions=requestOptions)
        return download

    def wait_for_complete(
        self,
        *,
        generationId,
        apiProjectId=None,
        pollInterval=2.0,
        timeout=600.0,
        onProgress=None,
        showProgress=False,
        requestOptions=None,
    ):
        return self._parent.wait_for_generation(
            generationId=generationId,
            apiProjectId=apiProjectId,
            pollInterval=pollInterval,
            timeout=timeout,
            onProgress=onProgress,
            showProgress=showProgress,
            requestOptions=requestOptions,
        )

    def create_and_wait(
        self,
        *,
        apiProjectId,
        model="diffio-2",
        sampling=None,
        params=None,
        pollInterval=2.0,
        timeout=600.0,
        onProgress=None,
        showProgress=False,
        requestOptions=None,
        progressRequestOptions=None,
    ):
        resolved_progress_options = requestOptions if progressRequestOptions is None else progressRequestOptions
        generation = self.create(
            apiProjectId=apiProjectId,
            model=model,
            sampling=sampling,
            params=params,
            requestOptions=requestOptions,
        )
        progress = self.wait_for_complete(
            generationId=generation.generationId,
            apiProjectId=generation.apiProjectId,
            pollInterval=pollInterval,
            timeout=timeout,
            onProgress=onProgress,
            showProgress=showProgress,
            requestOptions=resolved_progress_options,
        )
        return generation, progress


class ProjectsClient:
    def __init__(self, parent):
        self._parent = parent

    def list(self, *, requestOptions=None):
        return self._parent.list_projects(requestOptions=requestOptions)

    def list_generations(
        self,
        *,
        apiProjectId,
        requestOptions=None,
    ):
        return self._parent.list_project_generations(
            apiProjectId=apiProjectId,
            requestOptions=requestOptions,
        )


class WebhooksClient:
    def __init__(self, parent):
        self._parent = parent

    def get_portal_access(
        self,
        *,
        mode,
        apiKeyId=None,
        requestOptions=None,
    ):
        return self._parent.get_webhooks_portal_access(
            mode=mode,
            apiKeyId=apiKeyId,
            requestOptions=requestOptions,
        )

    def send_test_event(
        self,
        *,
        eventType,
        mode,
        apiKeyId=None,
        samplePayload=None,
        requestOptions=None,
    ):
        return self._parent.send_webhook_test_event(
            eventType=eventType,
            mode=mode,
            apiKeyId=apiKeyId,
            samplePayload=samplePayload,
            requestOptions=requestOptions,
        )


class AudioIsolationClient:
    def __init__(self, parent):
        self._parent = parent

    def convert(
        self,
        *,
        filePath,
        contentType=None,
        contentLength=None,
        fileFormat=None,
        model="diffio-2",
        sampling=None,
        projectParams=None,
        generationParams=None,
        requestOptions=None,
    ):
        return self.isolate(
            filePath=filePath,
            contentType=contentType,
            contentLength=contentLength,
            fileFormat=fileFormat,
            model=model,
            sampling=sampling,
            projectParams=projectParams,
            generationParams=generationParams,
            requestOptions=requestOptions,
        )

    def isolate(
        self,
        *,
        filePath,
        contentType=None,
        contentLength=None,
        fileFormat=None,
        model="diffio-2",
        sampling=None,
        projectParams=None,
        generationParams=None,
        requestOptions=None,
    ):
        project = self._parent.create_project(
            filePath=filePath,
            contentType=contentType,
            contentLength=contentLength,
            params=projectParams,
            fileFormat=fileFormat,
            requestOptions=requestOptions,
        )

        generation = self._parent.create_generation(
            apiProjectId=project.apiProjectId,
            model=model,
            sampling=sampling,
            params=generationParams,
            requestOptions=requestOptions,
        )

        return AudioIsolationResult(project=project, generation=generation)

    def restore_audio(
        self,
        *,
        filePath,
        contentType=None,
        contentLength=None,
        fileFormat=None,
        model="diffio-2",
        sampling=None,
        projectParams=None,
        generationParams=None,
        downloadType="audio",
        pollInterval=2.0,
        timeout=600.0,
        onProgress=None,
        showProgress=False,
        requestOptions=None,
        progressRequestOptions=None,
        downloadRequestOptions=None,
        raiseOnError=False,
    ):
        metadata = _init_restore_metadata()
        metadata["downloadType"] = downloadType
        resolved_progress_options = requestOptions if progressRequestOptions is None else progressRequestOptions
        resolved_download_options = requestOptions if downloadRequestOptions is None else downloadRequestOptions

        try:
            result = self.isolate(
                filePath=filePath,
                contentType=contentType,
                contentLength=contentLength,
                fileFormat=fileFormat,
                model=model,
                sampling=sampling,
                projectParams=projectParams,
                generationParams=generationParams,
                requestOptions=requestOptions,
            )
        except Exception as exc:
            metadata["stage"] = "isolate"
            _set_restore_error(metadata, exc)
            if raiseOnError:
                _attach_restore_metadata(exc, metadata)
                raise
            return None, metadata

        metadata["project"] = result.project
        metadata["generation"] = result.generation
        metadata["apiProjectId"] = result.project.apiProjectId
        metadata["generationId"] = result.generation.generationId
        metadata["stage"] = "generation"

        try:
            progress = self._parent.wait_for_generation(
                generationId=result.generation.generationId,
                apiProjectId=result.project.apiProjectId,
                pollInterval=pollInterval,
                timeout=timeout,
                onProgress=onProgress,
                showProgress=showProgress,
                requestOptions=resolved_progress_options,
            )
        except Exception as exc:
            metadata["stage"] = "progress"
            progress = None
            try:
                progress = self._parent.get_generation_progress(
                    generationId=result.generation.generationId,
                    apiProjectId=result.project.apiProjectId,
                    requestOptions=resolved_progress_options,
                )
            except Exception:
                progress = None
            metadata["progress"] = progress
            metadata["status"] = getattr(progress, "status", None)
            _set_restore_error(metadata, exc)
            if progress is not None:
                metadata["error"] = progress.error or str(exc)
                metadata["errorDetails"] = progress.errorDetails
            if raiseOnError:
                _attach_restore_metadata(exc, metadata)
                raise
            return None, metadata

        metadata["progress"] = progress
        metadata["status"] = progress.status
        metadata["error"] = progress.error
        metadata["errorDetails"] = progress.errorDetails

        metadata["stage"] = "download_info"
        try:
            download = self._parent.get_generation_download(
                generationId=result.generation.generationId,
                apiProjectId=result.project.apiProjectId,
                downloadType=downloadType,
                requestOptions=resolved_download_options,
            )
        except Exception as exc:
            _set_restore_error(metadata, exc)
            if raiseOnError:
                _attach_restore_metadata(exc, metadata)
                raise
            return None, metadata

        metadata["download"] = download
        metadata["downloadType"] = download.downloadType
        metadata["downloadUrl"] = download.downloadUrl
        metadata["fileName"] = download.fileName
        metadata["mimeType"] = download.mimeType

        metadata["stage"] = "download"
        try:
            content = _download_binary(self._parent, download.downloadUrl, requestOptions=resolved_download_options)
        except Exception as exc:
            _set_restore_error(metadata, exc)
            if raiseOnError:
                _attach_restore_metadata(exc, metadata)
                raise
            return None, metadata

        metadata["stage"] = "complete"
        metadata["ok"] = True
        return content, metadata

    def restore(
        self,
        *,
        filePath,
        contentType=None,
        contentLength=None,
        fileFormat=None,
        model="diffio-2",
        sampling=None,
        projectParams=None,
        generationParams=None,
        downloadType="audio",
        pollInterval=2.0,
        timeout=600.0,
        onProgress=None,
        showProgress=False,
        requestOptions=None,
        progressRequestOptions=None,
        downloadRequestOptions=None,
        raiseOnError=False,
    ):
        return self.restore_audio(
            filePath=filePath,
            contentType=contentType,
            contentLength=contentLength,
            fileFormat=fileFormat,
            model=model,
            sampling=sampling,
            projectParams=projectParams,
            generationParams=generationParams,
            downloadType=downloadType,
            pollInterval=pollInterval,
            timeout=timeout,
            onProgress=onProgress,
            showProgress=showProgress,
            requestOptions=requestOptions,
            progressRequestOptions=progressRequestOptions,
            downloadRequestOptions=downloadRequestOptions,
            raiseOnError=raiseOnError,
        )



def _guess_content_type(file_path):
    guessed, _ = mimetypes.guess_type(file_path)
    return guessed


def _build_create_project_payload(
    *,
    filePath=None,
    contentType=None,
    contentLength=None,
    params=None,
    fileFormat=None,
):
    if filePath is None:
        raise ValueError("filePath is required")

    resolved_path = os.fspath(filePath)
    if not resolved_path:
        raise ValueError("filePath is required")

    resolved_file_name = os.path.basename(resolved_path)
    if not resolved_file_name:
        raise ValueError("filePath must include a file name")

    resolved_content_type = contentType or _guess_content_type(resolved_path) or "application/octet-stream"
    resolved_content_length = contentLength
    if resolved_content_length is None:
        resolved_content_length = os.path.getsize(resolved_path)

    payload = {
        "fileName": resolved_file_name,
        "contentType": resolved_content_type,
        "contentLength": int(resolved_content_length),
    }
    if params:
        payload["params"] = params
    if fileFormat is not None:
        payload["fileFormat"] = fileFormat

    return payload


def _normalize_download_type(download_type):
    if download_type is None:
        return None, None
    if download_type == "mp3":
        return "audio", "mp3"
    if download_type in {"audio", "video"}:
        return download_type, download_type
    raise ValueError("downloadType must be audio, mp3, or video")


def _extension_from_file_name(file_name):
    if not file_name:
        return None
    extension = os.path.splitext(file_name)[1]
    if extension:
        return extension
    return None


def _extension_from_mime_type(mime_type):
    if not mime_type:
        return None
    extension = mimetypes.guess_extension(mime_type)
    if extension:
        return extension
    return None


def _extension_from_url(download_url):
    if not download_url:
        return None
    try:
        parsed = urlparse(download_url)
    except Exception:
        return None
    extension = os.path.splitext(parsed.path or "")[1]
    if extension:
        return extension
    return None


def _expected_download_extension(download):
    if download.downloadType == "audio":
        return ".mp3"
    if download.downloadType == "video":
        return (
            _extension_from_file_name(download.fileName)
            or _extension_from_mime_type(download.mimeType)
            or _extension_from_url(download.downloadUrl)
        )
    return None


def _warn_download_extension_mismatch(download, download_file_path):
    expected_extension = _expected_download_extension(download)
    if not expected_extension:
        return
    provided_extension = os.path.splitext(download_file_path)[1]
    if provided_extension.lower() != expected_extension.lower():
        warnings.warn(
            "downloadFilePath should end with "
            f"{expected_extension} for {download.downloadType} downloads. "
            f"Received {download_file_path}.",
            UserWarning,
            stacklevel=2,
        )


def _is_storage_emulator_url(upload_url):
    try:
        parsed = urlparse(upload_url)
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
    if (parsed.hostname or "").lower() != (emulator_parsed.hostname or "").lower():
        return False

    parsed_port = parsed.port or (443 if parsed.scheme == "https" else 80)
    emulator_port = emulator_parsed.port or (443 if emulator_parsed.scheme == "https" else 80)
    return parsed_port == emulator_port


def _merge_headers(base_headers, override_headers):
    headers = {}
    if base_headers:
        headers.update(base_headers)
    if override_headers:
        headers.update(override_headers)
    return headers


def _sleep_retry(attempt, retry_backoff):
    if retry_backoff is None:
        return
    delay = retry_backoff * (2 ** attempt)
    if delay > 0:
        time.sleep(delay)


def _raise_for_error(response):
    if 200 <= response.status_code < 300:
        content_type = response.headers.get("Content-Type", "")
        if response.content and "application/json" in content_type:
            return response.json()
        return {}

    message = f"Request failed with status {response.status_code}"
    body = None
    try:
        if response.content:
            body = response.json()
            if isinstance(body, dict) and body.get("error"):
                message = str(body.get("error"))
        else:
            body = None
    except json.JSONDecodeError:
        body = response.text

    raise DiffioApiError(message, statusCode=response.status_code, responseBody=body)


def _init_restore_metadata():
    return {
        "ok": False,
        "stage": "start",
        "apiProjectId": None,
        "generationId": None,
        "project": None,
        "generation": None,
        "progress": None,
        "download": None,
        "downloadType": None,
        "downloadUrl": None,
        "fileName": None,
        "mimeType": None,
        "status": None,
        "error": None,
        "errorDetails": None,
        "exceptionType": None,
        "exceptionMessage": None,
    }


def _set_restore_error(metadata, exc):
    metadata["error"] = str(exc)
    metadata["exceptionType"] = exc.__class__.__name__
    metadata["exceptionMessage"] = str(exc)


def _attach_restore_metadata(exc, metadata):
    try:
        setattr(exc, "restoreInfo", metadata)
    except Exception:
        return


def _download_binary(parent, download_url, *, requestOptions=None):
    merged_options = _merge_request_options(parent._default_request_options, requestOptions)
    headers = {}
    if _is_storage_emulator_url(download_url):
        headers["Authorization"] = "Bearer owner"
    headers = _merge_headers(headers, merged_options.headers)
    timeout = merged_options.timeout
    max_retries = merged_options.maxRetries if merged_options.maxRetries is not None else 0
    retry_backoff = (
        merged_options.retryBackoff if merged_options.retryBackoff is not None else DEFAULT_RETRY_BACKOFF
    )
    retry_statuses = (
        merged_options.retryStatusCodes
        if merged_options.retryStatusCodes is not None
        else DEFAULT_RETRY_STATUS_CODES
    )

    attempt = 0
    while True:
        try:
            request_kwargs = {
                "method": "GET",
                "url": download_url,
                "headers": headers,
            }
            if timeout is not None:
                request_kwargs["timeout"] = timeout
            response = parent._client.request(**request_kwargs)
        except httpx.RequestError:
            if attempt >= max_retries:
                raise
            _sleep_retry(attempt, retry_backoff)
            attempt += 1
            continue

        if retry_statuses and response.status_code in retry_statuses and attempt < max_retries:
            response.close()
            _sleep_retry(attempt, retry_backoff)
            attempt += 1
            continue

        try:
            if 200 <= response.status_code < 300:
                return response.content
            _raise_for_error(response)
        finally:
            response.close()


def _download_to_file(parent, download_url, file_path, *, requestOptions=None):
    merged_options = _merge_request_options(parent._default_request_options, requestOptions)
    headers = {}
    if _is_storage_emulator_url(download_url):
        headers["Authorization"] = "Bearer owner"
    headers = _merge_headers(headers, merged_options.headers)
    timeout = merged_options.timeout
    max_retries = merged_options.maxRetries if merged_options.maxRetries is not None else 0
    retry_backoff = (
        merged_options.retryBackoff if merged_options.retryBackoff is not None else DEFAULT_RETRY_BACKOFF
    )
    retry_statuses = (
        merged_options.retryStatusCodes
        if merged_options.retryStatusCodes is not None
        else DEFAULT_RETRY_STATUS_CODES
    )

    resolved_path = os.fspath(file_path)
    directory = os.path.dirname(resolved_path) or "."
    temp_handle = None
    temp_path = None

    try:
        attempt = 0
        while True:
            try:
                fd, temp_path = tempfile.mkstemp(prefix="diffio-download-", dir=directory)
                temp_handle = os.fdopen(fd, "wb")
                with parent._client.stream("GET", download_url, headers=headers, timeout=timeout) as response:
                    if retry_statuses and response.status_code in retry_statuses and attempt < max_retries:
                        temp_handle.close()
                        try:
                            os.remove(temp_path)
                        except OSError:
                            pass
                        _sleep_retry(attempt, retry_backoff)
                        attempt += 1
                        continue
                    if not (200 <= response.status_code < 300):
                        temp_handle.close()
                        try:
                            os.remove(temp_path)
                        except OSError:
                            pass
                        _raise_for_error(response)
                    for chunk in response.iter_bytes():
                        if chunk:
                            temp_handle.write(chunk)
                temp_handle.close()
                temp_handle = None
                os.replace(temp_path, resolved_path)
                temp_path = None
                return
            except httpx.RequestError:
                if temp_handle is not None:
                    temp_handle.close()
                    temp_handle = None
                if temp_path is not None:
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass
                    temp_path = None
                if attempt >= max_retries:
                    raise
                _sleep_retry(attempt, retry_backoff)
                attempt += 1
                continue
    finally:
        if temp_handle is not None:
            temp_handle.close()
        if temp_path is not None:
            try:
                os.remove(temp_path)
            except OSError:
                pass


def _format_progress(progress):
    parts = []
    if progress.preProcessing is not None:
        parts.append(f"pre={progress.preProcessing.status}:{progress.preProcessing.progress}%")
    if progress.inference is not None:
        parts.append(f"inf={progress.inference.status}:{progress.inference.progress}%")
    if getattr(progress, "restoredVideo", None) is not None:
        parts.append(f"vid={progress.restoredVideo.status}:{progress.restoredVideo.progress}%")
    joined = ", ".join(parts)
    if joined:
        return f"{progress.status} ({joined})"
    return f"{progress.status}"


def _report_progress(progress, *, onProgress, showProgress):
    if onProgress:
        onProgress(progress)
    if showProgress:
        print(_format_progress(progress))
