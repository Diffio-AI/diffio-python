"""The resource groups of DiffioClient (client.projects, client.generations, ...), one per part of the v2 API."""

import datetime
import time

from .client import COMPLETE_UPLOAD_TIMEOUT, drop_none, path_id
from .errors import (
    AssistantTurnFailedError,
    DiffioTimeoutError,
    ExportFailedError,
    GenerationFailedError,
)
from .types import (
    Generation,
    GenerationExport,
    GenerationMedia,
    GenerationMix,
    Model,
    Page,
    Project,
    ProjectDeleted,
    ProjectShare,
    ProjectShareRevoked,
    Transcript,
    TranscriptAssistantTurn,
    TranscriptDraft,
    TranscriptDraftDeleted,
    TranscriptEditEstimate,
    Usage,
    WebhookPortal,
)
from .webhooks import DEFAULT_TOLERANCE_SECONDS, verify_webhook_signature


class _Resource:
    def __init__(self, client):
        self._client = client

    def _request(self, *args, **kwargs):
        return self._client._request(*args, **kwargs)


def _generation_path(project_id, generation_id):
    return f"/projects/{path_id(project_id, 'project_id')}/generations/{path_id(generation_id, 'generation_id')}"


def _poll(read, *, done, failed, timeout, poll_interval, on_progress, what):
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        current = read()
        if on_progress is not None:
            on_progress(current)
        if done(current):
            return current
        failure = failed(current)
        if failure is not None:
            raise failure
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DiffioTimeoutError(
                    f"Timed out after {timeout} seconds waiting for {what} (last status {current.status})",
                    last=current,
                )
            time.sleep(min(poll_interval, remaining))
        else:
            time.sleep(poll_interval)


def _iso(value):
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, datetime.datetime):
        if value.tzinfo is not None:
            value = value.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        return value.isoformat() + "Z"
    if isinstance(value, datetime.date):
        return value.isoformat()
    raise ValueError("start and end must be ISO 8601 strings, dates or datetimes")


def _iterate(list_page, *, limit, **kwargs):
    cursor = None
    while True:
        page = list_page(limit=limit, cursor=cursor, **kwargs)
        for item in page.data:
            yield item
        if not page.next_cursor:
            return
        cursor = page.next_cursor


class ModelsResource(_Resource):
    def list(self, *, request_options=None):
        """GET /models: every model name, its status (hosted, alias, retired) and price. Needs no API key."""
        return Page.of(self._request("GET", "/models", auth=False, end_user=False, request_options=request_options), Model)


class ProjectsResource(_Resource):
    def create(
        self,
        *,
        file_name,
        size_bytes=None,
        content_type=None,
        metadata=None,
        generation=None,
        end_user=None,
        idempotency_key=None,
        request_options=None,
    ):
        """POST /projects: create a project and get its upload instructions (project.upload).

        generation ({"model": ..., "label": ..., "sampling": ...}, or just a model name) requests the first
        restore now; it starts by itself once the upload is ready. idempotency_key makes a repeat return the
        same project; without one the SDK sends a fresh key so its own retries never create a second project.
        """
        if not file_name:
            raise ValueError("file_name is required")
        if isinstance(generation, str):
            generation = {"model": generation}
        body = drop_none({
            "file_name": file_name,
            "content_type": content_type,
            "size_bytes": None if size_bytes is None else int(size_bytes),
            "metadata": metadata,
            "generation": generation,
        })
        return Project.from_dict(self._request(
            "POST",
            "/projects",
            body=body,
            end_user=end_user,
            idempotency_key=idempotency_key or self._client._new_idempotency_key(),
            request_options=request_options,
        ))

    def upload(self, project, file, *, complete=True, end_user=None, request_options=None):
        """Send the file's bytes to Diffio's media host using the project's upload instructions.

        project is the Project from create (or reopen_upload), or a project id (new instructions are then
        fetched with reopen_upload). file is a path, bytes or a binary file object. The file goes in parts of
        part_size_bytes (32 MiB); a failed part is retried on its own and a failed upload is aborted. With
        complete=True (the default) it then calls complete_upload and returns that Project (status preparing
        or ready); with complete=False it returns None and the project becomes ready by itself about 30 seconds
        later.
        """
        from .transfers import upload_bytes

        instructions = getattr(project, "upload", None) if isinstance(project, Project) else None
        if instructions is None or _expired(instructions.expires_at):
            project = self.reopen_upload(project, end_user=end_user, request_options=request_options)
            instructions = project.upload
        upload_bytes(self._client, instructions, file, request_options=request_options)
        if not complete:
            return None
        return self.complete_upload(project, end_user=end_user, request_options=request_options)

    def reopen_upload(self, project_id, *, end_user=None, request_options=None):
        """POST /projects/{id}/upload: new upload instructions (new token) while the project awaits its upload."""
        return Project.from_dict(self._request(
            "POST",
            f"/projects/{path_id(project_id, 'project_id')}/upload",
            end_user=end_user,
            retry_safe=True,
            request_options=request_options,
        ))

    def complete_upload(self, project_id, *, end_user=None, request_options=None):
        """POST /projects/{id}/upload/complete: say the upload finished so the file is checked now. Safe to repeat."""
        return Project.from_dict(self._request(
            "POST",
            f"/projects/{path_id(project_id, 'project_id')}/upload/complete",
            end_user=end_user,
            retry_safe=True,
            timeout=COMPLETE_UPLOAD_TIMEOUT,
            request_options=request_options,
        ))

    def list(self, *, limit=None, cursor=None, end_user=None, request_options=None):
        """GET /projects: one page of projects, newest first (limit 1 to 100, default 20)."""
        return Page.of(self._request(
            "GET",
            "/projects",
            query={"limit": limit, "cursor": cursor},
            end_user=end_user,
            request_options=request_options,
        ), Project)

    def iter(self, *, limit=None, end_user=None, request_options=None):
        """Every project, newest first, fetching pages as needed (limit is the page size)."""
        return _iterate(self.list, limit=limit, end_user=end_user, request_options=request_options)

    def get(self, project_id, *, end_user=None, request_options=None):
        """GET /projects/{id}: one project with its latest_generation."""
        return Project.from_dict(self._request(
            "GET", f"/projects/{path_id(project_id, 'project_id')}", end_user=end_user, request_options=request_options
        ))

    def delete(self, project_id, *, end_user=None, request_options=None):
        """DELETE /projects/{id}: delete the project, its upload and every result. Refused while a generation runs."""
        return ProjectDeleted.from_dict(self._request(
            "DELETE", f"/projects/{path_id(project_id, 'project_id')}", end_user=end_user, request_options=request_options
        ))


def _expired(expires_at, margin_seconds=60):
    if not expires_at:
        return False
    try:
        text = str(expires_at).replace("Z", "+00:00")
        moment = datetime.datetime.fromisoformat(text)
    except ValueError:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    now = datetime.datetime.now(datetime.timezone.utc)
    return moment <= now + datetime.timedelta(seconds=margin_seconds)


class GenerationsResource(_Resource):
    def create(
        self,
        project_id,
        *,
        model,
        label=None,
        sampling=None,
        end_user=None,
        idempotency_key=None,
        request_options=None,
    ):
        """POST /projects/{id}/generations: start a restore with model (diffio-flash, diffio-pro, ...).

        Accepted before the upload is ready (it then waits). Without idempotency_key the SDK sends a fresh key
        so its own retries never start a second generation.
        """
        if not model:
            raise ValueError("model is required")
        return Generation.from_dict(self._request(
            "POST",
            f"/projects/{path_id(project_id, 'project_id')}/generations",
            body=drop_none({"model": model, "label": label, "sampling": sampling}),
            end_user=end_user,
            idempotency_key=idempotency_key or self._client._new_idempotency_key(),
            request_options=request_options,
        ))

    def list(self, project_id, *, limit=None, cursor=None, end_user=None, request_options=None):
        """GET /projects/{id}/generations: one page of the project's generations, newest first."""
        return Page.of(self._request(
            "GET",
            f"/projects/{path_id(project_id, 'project_id')}/generations",
            query={"limit": limit, "cursor": cursor},
            end_user=end_user,
            request_options=request_options,
        ), Generation)

    def iter(self, project_id, *, limit=None, end_user=None, request_options=None):
        """Every generation of the project, newest first, fetching pages as needed."""
        return _iterate(
            lambda **kwargs: self.list(project_id, **kwargs),
            limit=limit,
            end_user=end_user,
            request_options=request_options,
        )

    def get(self, project_id, generation_id, *, end_user=None, request_options=None):
        """GET /projects/{id}/generations/{gid}: status, stage, progress, queue and files_available."""
        return Generation.from_dict(self._request(
            "GET", _generation_path(project_id, generation_id), end_user=end_user, request_options=request_options
        ))

    def wait(
        self,
        project_id,
        generation_id,
        *,
        timeout=3600.0,
        poll_interval=2.0,
        on_progress=None,
        end_user=None,
        request_options=None,
    ):
        """Poll the generation until status is complete and return it.

        on_progress(generation) is called after every read. Raises GenerationFailedError when it fails and
        DiffioTimeoutError after timeout seconds (None waits without limit).
        """

        def failed(generation):
            if generation.status != "failed":
                return None
            error = generation.error
            code = getattr(error, "code", None)
            message = getattr(error, "message", None) or "The generation failed"
            return GenerationFailedError(f"{message} ({code})", resource=generation, code=code)

        return _poll(
            lambda: self.get(project_id, generation_id, end_user=end_user, request_options=request_options),
            done=lambda generation: generation.status == "complete",
            failed=failed,
            timeout=timeout,
            poll_interval=poll_interval,
            on_progress=on_progress,
            what="the generation",
        )

    def media(self, project_id, generation_id, *, preview_seconds=None, end_user=None, request_options=None):
        """GET .../media: URLs of the generation's files (audio, audio_mixed, original, hls_master, ...), valid 6 hours."""
        return GenerationMedia.from_dict(self._request(
            "GET",
            f"{_generation_path(project_id, generation_id)}/media",
            query={"preview_seconds": preview_seconds},
            end_user=end_user,
            request_options=request_options,
        ))

    def download(self, project_id, generation_id, *, file="audio", path=None, end_user=None, request_options=None):
        """Download one file of the generation (file="audio" is the restored MP3; see media() for the names).

        With path, the file is streamed there and the path is returned (a directory gets the file's own name);
        without, the bytes are returned.
        """
        from .transfers import download_url, media_file_url

        media = self.media(project_id, generation_id, end_user=end_user, request_options=request_options)
        return download_url(self._client, media_file_url(media, file), path=path, request_options=request_options)


class EditsResource(_Resource):
    def estimate(
        self,
        project_id,
        generation_id,
        *,
        words=None,
        tighten=None,
        request_id=None,
        end_user=None,
        request_options=None,
    ):
        """POST .../edits/estimate: what a transcript edit would restore and bill. Creates nothing."""
        return TranscriptEditEstimate.from_dict(self._request(
            "POST",
            f"{_generation_path(project_id, generation_id)}/edits/estimate",
            body=drop_none({"words": words, "tighten": tighten, "request_id": request_id}),
            end_user=end_user,
            retry_safe=True,
            request_options=request_options,
        ))

    def create(
        self,
        project_id,
        generation_id,
        *,
        words=None,
        tighten=None,
        request_id=None,
        end_user=None,
        idempotency_key=None,
        request_options=None,
    ):
        """POST .../edits: re-restore changed words of a finished Pro result as a new generation (kind edit). Billed."""
        return Generation.from_dict(self._request(
            "POST",
            f"{_generation_path(project_id, generation_id)}/edits",
            body=drop_none({"words": words, "tighten": tighten, "request_id": request_id}),
            end_user=end_user,
            idempotency_key=idempotency_key or self._client._new_idempotency_key(),
            request_options=request_options,
        ))


class MixResource(_Resource):
    def get(self, project_id, generation_id, *, end_user=None, request_options=None):
        """GET .../mix: the saved balance of speech and background (background_gain, leveling, revision)."""
        return GenerationMix.from_dict(self._request(
            "GET", f"{_generation_path(project_id, generation_id)}/mix", end_user=end_user, request_options=request_options
        ))

    def save(
        self,
        project_id,
        generation_id,
        *,
        background_gain,
        leveling=None,
        revision=None,
        end_user=None,
        request_options=None,
    ):
        """PUT .../mix: save background_gain (0 to 1) and leveling; with revision, refused if someone saved since."""
        return GenerationMix.from_dict(self._request(
            "PUT",
            f"{_generation_path(project_id, generation_id)}/mix",
            body=drop_none({"background_gain": background_gain, "leveling": leveling, "revision": revision}),
            end_user=end_user,
            request_options=request_options,
        ))


class ExportsResource(_Resource):
    def create(
        self,
        project_id,
        generation_id,
        *,
        artifact,
        format,
        inline=None,
        background_gain=None,
        leveling=None,
        file_name=None,
        end_user=None,
        request_options=None,
    ):
        """POST .../exports: render speech, background, mix, both (zip) or original as wav, mp3, m4a or mp4.

        The same recipe asked again returns the same export, so this call is retried safely.
        """
        return GenerationExport.from_dict(self._request(
            "POST",
            f"{_generation_path(project_id, generation_id)}/exports",
            body=drop_none({
                "artifact": artifact,
                "format": format,
                "inline": inline,
                "background_gain": background_gain,
                "leveling": leveling,
                "file_name": file_name,
            }),
            end_user=end_user,
            retry_safe=True,
            request_options=request_options,
        ))

    def get(self, project_id, generation_id, export_id, *, file_name=None, end_user=None, request_options=None):
        """GET .../exports/{export_id}: the export's status and, once ready, a fresh url."""
        return GenerationExport.from_dict(self._request(
            "GET",
            f"{_generation_path(project_id, generation_id)}/exports/{path_id(export_id, 'export_id')}",
            query={"file_name": file_name},
            end_user=end_user,
            request_options=request_options,
        ))

    def wait(
        self,
        project_id,
        generation_id,
        export_id,
        *,
        file_name=None,
        timeout=600.0,
        poll_interval=2.0,
        on_progress=None,
        end_user=None,
        request_options=None,
    ):
        """Poll the export until status is ready and return it. Raises ExportFailedError or DiffioTimeoutError."""

        def failed(export):
            if export.status != "failed":
                return None
            code = getattr(export.error, "code", None)
            message = getattr(export.error, "message", None) or "The export failed"
            return ExportFailedError(f"{message} ({code})", resource=export, code=code)

        return _poll(
            lambda: self.get(
                project_id, generation_id, export_id, file_name=file_name, end_user=end_user, request_options=request_options
            ),
            done=lambda export: export.status == "ready",
            failed=failed,
            timeout=timeout,
            poll_interval=poll_interval,
            on_progress=on_progress,
            what="the export",
        )


class TranscriptsResource(_Resource):
    def get(self, project_id, generation_id, *, preview_seconds=None, end_user=None, request_options=None):
        """GET .../transcript: text and word timings of the generation's recording."""
        return Transcript.from_dict(self._request(
            "GET",
            f"{_generation_path(project_id, generation_id)}/transcript",
            query={"preview_seconds": preview_seconds},
            end_user=end_user,
            request_options=request_options,
        ))

    def get_draft(self, project_id, generation_id, *, end_user=None, request_options=None):
        """GET .../transcript/draft: the saved editor state (data) and its revision."""
        return TranscriptDraft.from_dict(self._request(
            "GET",
            f"{_generation_path(project_id, generation_id)}/transcript/draft",
            end_user=end_user,
            request_options=request_options,
        ))

    def save_draft(self, project_id, generation_id, *, data, revision=None, end_user=None, request_options=None):
        """PUT .../transcript/draft: save data (a JSON object); with revision, refused if someone saved since."""
        return TranscriptDraft.from_dict(self._request(
            "PUT",
            f"{_generation_path(project_id, generation_id)}/transcript/draft",
            body=drop_none({"data": data, "revision": revision}),
            end_user=end_user,
            request_options=request_options,
        ))

    def delete_draft(self, project_id, generation_id, *, revision=None, end_user=None, request_options=None):
        """DELETE .../transcript/draft: delete the draft; with revision, refused if someone saved since."""
        return TranscriptDraftDeleted.from_dict(self._request(
            "DELETE",
            f"{_generation_path(project_id, generation_id)}/transcript/draft",
            query={"revision": revision},
            end_user=end_user,
            request_options=request_options,
        ))


class AssistantResource(_Resource):
    def create_turn(
        self,
        project_id,
        generation_id,
        *,
        messages,
        words=None,
        selection=None,
        playhead_seconds=None,
        source=None,
        end_user=None,
        request_options=None,
    ):
        """POST .../transcript/assistant/turns: ask the edit assistant (Pro results); answers status processing."""
        return TranscriptAssistantTurn.from_dict(self._request(
            "POST",
            f"{_generation_path(project_id, generation_id)}/transcript/assistant/turns",
            body=drop_none({
                "messages": messages,
                "words": words,
                "selection": selection,
                "playhead_seconds": playhead_seconds,
                "source": source,
            }),
            end_user=end_user,
            request_options=request_options,
        ))

    def get_turn(self, project_id, generation_id, turn_id, *, end_user=None, request_options=None):
        """GET .../transcript/assistant/turns/{turn_id}: the turn's status and, once complete, its answer."""
        return TranscriptAssistantTurn.from_dict(self._request(
            "GET",
            f"{_generation_path(project_id, generation_id)}/transcript/assistant/turns/{path_id(turn_id, 'turn_id')}",
            end_user=end_user,
            request_options=request_options,
        ))

    def wait_turn(
        self,
        project_id,
        generation_id,
        turn_id,
        *,
        timeout=600.0,
        poll_interval=1.5,
        on_progress=None,
        end_user=None,
        request_options=None,
    ):
        """Poll the turn until status is complete and return it. Raises AssistantTurnFailedError or DiffioTimeoutError."""

        def failed(turn):
            if turn.status != "failed":
                return None
            code = getattr(turn.error, "code", None)
            message = getattr(turn.error, "message", None) or "The assistant turn failed"
            return AssistantTurnFailedError(f"{message} ({code})", resource=turn, code=code)

        return _poll(
            lambda: self.get_turn(project_id, generation_id, turn_id, end_user=end_user, request_options=request_options),
            done=lambda turn: turn.status == "complete",
            failed=failed,
            timeout=timeout,
            poll_interval=poll_interval,
            on_progress=on_progress,
            what="the assistant turn",
        )


class SharesResource(_Resource):
    def publish(self, project_id, *, generation_id=None, preview_seconds=None, end_user=None, request_options=None):
        """PUT /projects/{id}/share: publish a share link (newest finished generation unless generation_id)."""
        return ProjectShare.from_dict(self._request(
            "PUT",
            f"/projects/{path_id(project_id, 'project_id')}/share",
            body=drop_none({"generation_id": generation_id, "preview_seconds": preview_seconds}),
            end_user=end_user,
            request_options=request_options,
        ))

    def get(self, project_id, *, end_user=None, request_options=None):
        """GET /projects/{id}/share: the project's published share."""
        return ProjectShare.from_dict(self._request(
            "GET", f"/projects/{path_id(project_id, 'project_id')}/share", end_user=end_user, request_options=request_options
        ))

    def revoke(self, project_id, *, end_user=None, request_options=None):
        """DELETE /projects/{id}/share: stop sharing; old links stop working within about a minute."""
        return ProjectShareRevoked.from_dict(self._request(
            "DELETE", f"/projects/{path_id(project_id, 'project_id')}/share", end_user=end_user, request_options=request_options
        ))

    def get_public(self, share_id, *, request_options=None):
        """GET /shares/{share_id}: what a share link shows. Needs no API key."""
        return ProjectShare.from_dict(self._request(
            "GET",
            f"/shares/{path_id(share_id, 'share_id')}",
            auth=False,
            end_user=False,
            request_options=request_options,
        ))


class UsageResource(_Resource):
    def get(self, *, start=None, end=None, end_user=None, request_options=None):
        """GET /usage: generations, billable seconds and list price from start to end (default: this month so far)."""
        return Usage.from_dict(self._request(
            "GET",
            "/usage",
            query={"start": _iso(start), "end": _iso(end)},
            end_user=end_user,
            request_options=request_options,
        ))


class WebhooksResource(_Resource):
    def portal(self, *, request_options=None):
        """POST /webhooks/portal: a short-lived link to the webhook portal. Needs a key with webhooks:write."""
        return WebhookPortal.from_dict(self._request(
            "POST", "/webhooks/portal", end_user=False, retry_safe=True, request_options=request_options
        ))

    def verify_signature(self, payload, headers, secret, *, tolerance_seconds=DEFAULT_TOLERANCE_SECONDS):
        """Check a webhook's Svix signature with the endpoint's whsec_ secret and return the WebhookEvent."""
        return verify_webhook_signature(payload, headers, secret, tolerance_seconds=tolerance_seconds)
