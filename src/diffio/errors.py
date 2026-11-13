class DiffioError(Exception):
    """Base class of every error this SDK raises on purpose."""

    def __init__(self, message):
        super().__init__(message)
        self.message = message


class DiffioApiError(DiffioError):
    """A request to the Diffio API (or its media host) failed.

    status is the HTTP status (None when no answer arrived), code the stable snake_case error code from the
    answer's error.code (for example project_not_found or unknown_model; connection_error when no answer
    arrived), message the sentence the API sent, request_id the id Diffio support can look up, details the
    optional error.details object, and body the parsed answer.
    """

    def __init__(self, message, *, status=None, code=None, request_id=None, details=None, body=None, headers=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.request_id = request_id
        self.details = details
        self.body = body
        self.headers = headers

    def __str__(self):
        parts = []
        if self.status is not None:
            parts.append(f"status={self.status}")
        if self.code:
            parts.append(f"code={self.code}")
        if self.request_id:
            parts.append(f"request_id={self.request_id}")
        suffix = f" ({', '.join(parts)})" if parts else ""
        return f"{self.message}{suffix}"


class DiffioTimeoutError(DiffioError):
    """A wait helper gave up before the resource finished. last holds the resource as last read."""

    def __init__(self, message, *, last=None):
        super().__init__(message)
        self.last = last


class ResourceFailedError(DiffioError):
    """A generation, export or assistant turn finished as failed.

    resource is the failed object as read, code and message come from its error object.
    """

    def __init__(self, message, *, resource=None, code=None):
        super().__init__(message)
        self.resource = resource
        self.code = code


class GenerationFailedError(ResourceFailedError):
    """generations.wait (or restore_audio) read status failed. generation is the failed Generation."""

    @property
    def generation(self):
        return self.resource

    @property
    def retryable(self):
        error = getattr(self.resource, "error", None)
        return bool(getattr(error, "retryable", False))


class ExportFailedError(ResourceFailedError):
    """exports.wait read status failed. export is the failed GenerationExport."""

    @property
    def export(self):
        return self.resource


class AssistantTurnFailedError(ResourceFailedError):
    """assistant.wait_turn read status failed. turn is the failed TranscriptAssistantTurn."""

    @property
    def turn(self):
        return self.resource


class WebhookVerificationError(DiffioError):
    """A webhook's Svix signature, timestamp or body did not check out. Answer the request with 400."""
