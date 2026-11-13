"""Response objects of the Diffio API v2.

Every object keeps the wire field names (snake_case) as attributes, for example project.latest_generation.status.
Fields the API adds later are kept too: read them with obj["name"] or obj.get("name"), and obj.to_dict() returns
the answer as received.
"""

import json


class ApiObject:
    """One JSON object from the API with attribute access. Nested objects are converted by _nested/_nested_lists."""

    _fields = ()
    _nested = {}
    _nested_lists = {}

    def __init__(self, data=None):
        raw = dict(data or {})
        object.__setattr__(self, "_data", raw)
        for name in self._fields:
            object.__setattr__(self, name, None)
        for name, value in raw.items():
            if self._is_attribute_name(name):
                object.__setattr__(self, name, self._convert(name, value))

    @classmethod
    def _is_attribute_name(cls, name):
        # A wire field becomes an attribute unless it would hide one of this class's methods or properties.
        if not isinstance(name, str) or not name.isidentifier() or name.startswith("_"):
            return False
        return name in cls._fields or not hasattr(cls, name)

    @classmethod
    def from_dict(cls, data):
        if data is None:
            return None
        if isinstance(data, cls):
            return data
        return cls(data)

    def _convert(self, name, value):
        nested = self._nested.get(name)
        if nested is not None and isinstance(value, dict):
            return nested.from_dict(value)
        nested_item = self._nested_lists.get(name)
        if nested_item is not None and isinstance(value, list):
            return [nested_item.from_dict(item) if isinstance(item, dict) else item for item in value]
        return value

    def __getitem__(self, name):
        if name not in self._data:
            raise KeyError(name)
        if self._is_attribute_name(name):
            return getattr(self, name)
        return self._data[name]

    def __contains__(self, name):
        return name in self._data

    def get(self, name, default=None):
        if name not in self._data:
            return default
        return self[name]

    def to_dict(self):
        """The object exactly as the API sent it (nested objects as plain dicts)."""
        return json.loads(json.dumps(self._data))

    def __eq__(self, other):
        if isinstance(other, ApiObject):
            return type(self) is type(other) and self._data == other._data
        return NotImplemented

    def __hash__(self):
        return id(self)

    def __repr__(self):
        name = type(self).__name__
        shown = []
        for key in ("id", "object", "status"):
            if key in self._data:
                shown.append(f"{key}={self._data[key]!r}")
        if not shown:
            shown = [f"{key}={value!r}" for key, value in list(self._data.items())[:4]]
        return f"<{name} {' '.join(shown)}>"


class Page(ApiObject):
    """One page of a list: data (the items) and next_cursor (None on the last page).

    Pass next_cursor as cursor= to the same list call for the next page, or use the resource's iter() to walk
    every page.
    """

    _fields = ("object", "data", "next_cursor")
    _item = ApiObject

    def __init__(self, data=None, item=None):
        item_type = item or self._item
        raw = dict(data or {})
        super().__init__(raw)
        object.__setattr__(self, "data", [item_type.from_dict(entry) if isinstance(entry, dict) else entry
                                          for entry in raw.get("data") or []])

    @classmethod
    def of(cls, data, item):
        return cls(data, item=item)

    @property
    def has_more(self):
        return self.next_cursor is not None

    def __iter__(self):
        return iter(self.data)

    def __len__(self):
        return len(self.data)

    def __repr__(self):
        return f"<Page {len(self.data)} items next_cursor={self.next_cursor!r}>"


class ResourceError(ApiObject):
    """The error of a failed project, export or assistant turn: code and message."""

    _fields = ("code", "message")


class GenerationError(ResourceError):
    """The error of a failed generation: code, message, retryable and explanation."""

    _fields = ("code", "message", "retryable", "explanation")


class Model(ApiObject):
    """A model name: id, status (hosted, alias or retired), label, served_by, price_per_hour_usd."""

    _fields = ("id", "object", "status", "label", "served_by", "price_per_hour_usd")


class UploadRequest(ApiObject):
    """One request of the upload recipe: method, url, headers, body, returns, description ({placeholders} filled by the SDK)."""

    _fields = ("method", "url", "headers", "body", "returns", "description")


class UploadInstructions(ApiObject):
    """How to send the file: the media host's upload requests and the upload token they carry.

    Fields: upload_session_id, edge_base_url, upload_token, part_size_bytes, max_bytes, expires_at and requests
    (start, part, complete, abort). projects.upload follows them for you.
    """

    _fields = (
        "upload_session_id",
        "edge_base_url",
        "upload_token",
        "part_size_bytes",
        "max_bytes",
        "expires_at",
        "requests",
    )

    def _convert(self, name, value):
        if name == "requests" and isinstance(value, dict):
            return {key: UploadRequest.from_dict(item) if isinstance(item, dict) else item
                    for key, item in value.items()}
        return super()._convert(name, value)


class StageProgress(ApiObject):
    """Progress while a generation runs: overall_percent, stage_percent, bytes_done, bytes_total, available_through_seconds, duration_seconds."""

    _fields = (
        "overall_percent",
        "stage_percent",
        "bytes_done",
        "bytes_total",
        "available_through_seconds",
        "duration_seconds",
    )


class GenerationQueue(ApiObject):
    """Where a generation waits for a worker: position, connected_workers, idle_workers, busy_workers, wait_reason, message."""

    _fields = ("position", "connected_workers", "idle_workers", "busy_workers", "wait_reason", "message")


class GenerationWarning(ApiObject):
    """Something to know about the recording, for example code unsupported_language with languages."""

    _fields = ("code", "languages", "message")


class GenerationEdit(ApiObject):
    """What a transcript edit did: edit_count, max_windows, windows, windows_done, ranges, billable_seconds, cut_seconds, request_id, language, tighten."""

    _fields = (
        "edit_count",
        "max_windows",
        "windows",
        "windows_done",
        "ranges",
        "billable_seconds",
        "cut_seconds",
        "request_id",
        "language",
        "tighten",
    )


class Generation(ApiObject):
    """One restore (kind restore) or transcript edit (kind edit) of a project.

    status is queued, processing, complete or failed. stage, stage_progress and queue describe a running
    generation; files_available names the files GET .../media can hand out.
    """

    _fields = (
        "id",
        "object",
        "project_id",
        "status",
        "stage",
        "stage_progress",
        "queue",
        "warnings",
        "label",
        "kind",
        "source_generation_id",
        "edit",
        "transcript_editable",
        "duration_seconds",
        "quality_score",
        "transcription_status",
        "files_available",
        "error",
        "model",
        "requested_model",
        "substituted",
        "price_per_hour_usd",
        "stems_status",
        "created_at",
        "updated_at",
    )
    _nested = {
        "stage_progress": StageProgress,
        "queue": GenerationQueue,
        "edit": GenerationEdit,
        "error": GenerationError,
    }
    _nested_lists = {"warnings": GenerationWarning}

    @property
    def is_finished(self):
        return self.status in ("complete", "failed")


class Project(ApiObject):
    """A project: one uploaded file and its generations.

    status is awaiting_upload, preparing, ready or failed. upload (create and reopen_upload only) holds the
    upload instructions.
    """

    _fields = (
        "id",
        "object",
        "status",
        "file_name",
        "content_type",
        "size_bytes",
        "duration_seconds",
        "has_video",
        "end_user",
        "metadata",
        "generation_count",
        "video_preview_status",
        "latest_generation",
        "share_id",
        "created_at",
        "updated_at",
        "error",
        "upload",
    )
    _nested = {
        "latest_generation": Generation,
        "error": ResourceError,
        "upload": UploadInstructions,
    }


class ProjectDeleted(ApiObject):
    """Answer of projects.delete: id, deleted, deleted_generations, deleted_media_objects."""

    _fields = ("id", "object", "deleted", "deleted_generations", "deleted_media_objects")


class GenerationMedia(ApiObject):
    """A playback and download grant: files maps a file name (audio, original, hls_master, ...) to a URL.

    Every URL starts with base_url and stops working at expires_at (6 hours); ask again for new URLs.
    """

    _fields = ("object", "project_id", "generation_id", "status", "base_url", "expires_at", "preview_seconds", "files")


class TranscriptEditEstimate(ApiObject):
    """Answer of edits.estimate: edit_count, max_windows, window_seconds, max_billable_seconds."""

    _fields = ("object", "project_id", "generation_id", "edit_count", "max_windows", "window_seconds", "max_billable_seconds")


class GenerationMix(ApiObject):
    """The saved balance of the separated tracks: background_gain (0 to 1), leveling, revision, stems_revision."""

    _fields = ("object", "project_id", "generation_id", "background_gain", "leveling", "revision", "stems_revision")


class GenerationExport(ApiObject):
    """A file rendered from the separated tracks. status is queued, processing, ready or failed; url is set once ready."""

    _fields = (
        "object",
        "id",
        "project_id",
        "generation_id",
        "status",
        "artifact",
        "format",
        "background_gain",
        "leveling",
        "inline",
        "url",
        "error",
    )
    _nested = {"error": ResourceError}


class TranscriptWord(ApiObject):
    """One word: id, text, start, end, probability, plus stored extras such as speaker and language."""

    _fields = ("id", "text", "start", "end", "probability")


class Transcript(ApiObject):
    """The word-timed transcript of a generation: text, words, preview_seconds, truncated."""

    _fields = ("object", "project_id", "generation_id", "text", "words", "preview_seconds", "truncated")
    _nested_lists = {"words": TranscriptWord}


class TranscriptDraft(ApiObject):
    """Opaque editor state saved for a generation: data and its revision."""

    _fields = ("object", "project_id", "generation_id", "revision", "data", "created_at", "updated_at")


class TranscriptDraftDeleted(ApiObject):
    """Answer of transcripts.delete_draft: deleted and the new revision."""

    _fields = ("object", "project_id", "generation_id", "deleted", "revision")


class TranscriptAssistantTurn(ApiObject):
    """One edit-assistant turn. status is processing, complete or failed; a complete turn has reply, proposals and more."""

    _fields = (
        "object",
        "id",
        "project_id",
        "generation_id",
        "status",
        "created_at",
        "completed_at",
        "error",
        "reply",
        "proposals",
        "moments",
        "unclear_word_ids",
        "speaker_turns",
        "shortening",
        "feedback_offer",
        "notices",
        "windows_total",
    )
    _nested = {"error": ResourceError}


class ProjectShare(ApiObject):
    """A published share: id, generation_id, base_url and files (name to URL) anyone with the link can play."""

    _fields = (
        "object",
        "id",
        "project_id",
        "generation_id",
        "file_name",
        "content_type",
        "created_at",
        "has_video",
        "duration_seconds",
        "preview_seconds",
        "base_url",
        "files",
    )


class ProjectShareRevoked(ApiObject):
    """Answer of shares.revoke: id (None if never shared), project_id, deleted."""

    _fields = ("object", "id", "project_id", "deleted")


class UsageByModel(ApiObject):
    """Usage of one model: model, generations, billable_seconds, list_price_usd."""

    _fields = ("model", "generations", "billable_seconds", "list_price_usd")


class Usage(ApiObject):
    """Usage report: start, end, end_user, generations, billable_seconds, list_price_usd, by_model."""

    _fields = ("object", "start", "end", "end_user", "generations", "billable_seconds", "list_price_usd", "by_model")
    _nested_lists = {"by_model": UsageByModel}


class WebhookPortal(ApiObject):
    """A short-lived link to the webhook portal (url) and the event_types it offers."""

    _fields = ("object", "url", "event_types")


class WebhookEventData(ApiObject):
    """The resource a webhook event names: object, id, project_id, end_user, status and per-type extras."""

    _fields = ("object", "id", "project_id", "generation_id", "end_user", "status", "kind", "stems_status", "error_code")


class WebhookEvent(ApiObject):
    """A verified webhook event: type, id (the same on every retry), created_at and data."""

    _fields = ("type", "id", "created_at", "data")
    _nested = {"data": WebhookEventData}


class RestoreResult:
    """What restore_audio made: project, generation (complete), media, output_path (or content when no path was given)."""

    def __init__(self, project, generation, media, output_path=None, content=None):
        self.project = project
        self.generation = generation
        self.media = media
        self.output_path = output_path
        self.content = content

    def __repr__(self):
        target = self.output_path if self.output_path is not None else f"{len(self.content or b'')} bytes"
        return f"<RestoreResult project={self.project.id!r} generation={self.generation.id!r} -> {target}>"


ProjectStatus = ("awaiting_upload", "preparing", "ready", "failed")
GenerationStatus = ("queued", "processing", "complete", "failed")
ExportStatus = ("queued", "processing", "ready", "failed")
WebhookEventType = (
    "export.failed",
    "export.ready",
    "generation.completed",
    "generation.failed",
    "generation.stems_failed",
    "generation.stems_ready",
    "project.failed",
    "project.ready",
    "transcript_assistant_turn.completed",
    "transcript_assistant_turn.failed",
)
