"""An in-memory fake of the Diffio API v2 and its media host, served through httpx.MockTransport.

It answers with the wire shapes of the v2 OpenAPI document (snake_case, {"error": {...}} refusals) and the
media host's camelCase upload API, records every request, and can fail chosen requests on purpose.
"""

import json
import re
import uuid
from urllib.parse import parse_qs, unquote, urlparse

import httpx

API = "https://api.test"
EDGE = "https://media.test"
MODELS = [
    {"id": "diffio-4.7-flash", "object": "model", "status": "hosted", "label": "Diffio 4.7 Flash",
     "served_by": "diffio-4.7-flash", "price_per_hour_usd": 5},
    {"id": "diffio-4.7-pro", "object": "model", "status": "hosted", "label": "Diffio 4.7 Pro",
     "served_by": "diffio-4.7-pro", "price_per_hour_usd": 10},
    {"id": "diffio-flash", "object": "model", "status": "alias", "label": "Diffio 4.7 Flash",
     "served_by": "diffio-4.7-flash", "price_per_hour_usd": 5},
    {"id": "diffio-pro", "object": "model", "status": "alias", "label": "Diffio 4.7 Pro",
     "served_by": "diffio-4.7-pro", "price_per_hour_usd": 10},
]
SERVED = {"diffio-flash": "diffio-4.7-flash", "diffio-pro": "diffio-4.7-pro",
          "diffio-4.7-flash": "diffio-4.7-flash", "diffio-4.7-pro": "diffio-4.7-pro"}
TS = "2026-11-13T00:33:21.812909Z"


def error(status, code, message, details=None, request_id="req_fake"):
    body = {"code": code, "message": message, "request_id": request_id}
    if details is not None:
        body["details"] = details
    return httpx.Response(status, json={"error": body}, headers={"X-Request-Id": request_id})


class FakeDiffioApi:
    def __init__(self, api_key="diffio_test_key", part_size=32 * 1024 * 1024, polls_to_complete=2):
        self.api_key = api_key
        self.part_size = part_size
        self.polls_to_complete = polls_to_complete
        self.requests = []
        self.projects = {}
        self.generations = {}
        self.uploads = {}
        self.objects = {}
        self.idempotency = {}
        self.failures = []
        self.drafts = {}
        self.mixes = {}
        self.exports = {}
        self.turns = {}
        self.shares = {}
        self.fail_generation_with = None
        self.omit_upload_requests = False

    # ---- test helpers --------------------------------------------------------------------------------

    def http_client(self):
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def fail(self, method, pattern, *, status=503, code="unavailable", times=1, exc=None):
        """Answer the next `times` requests matching method and URL regex with status (or raise exc)."""
        self.failures.append({"method": method, "pattern": re.compile(pattern), "status": status, "code": code,
                              "times": times, "exc": exc})

    def sent(self, method=None, pattern=None):
        return [r for r in self.requests
                if (method is None or r.method == method) and (pattern is None or re.search(pattern, str(r.url)))]

    def last(self, method=None, pattern=None):
        found = self.sent(method, pattern)
        return found[-1] if found else None

    # ---- dispatch ------------------------------------------------------------------------------------

    def handle(self, request):
        request.read()
        self.requests.append(request)
        url = str(request.url)
        for failure in self.failures:
            if failure["times"] > 0 and failure["method"] == request.method and failure["pattern"].search(url):
                failure["times"] -= 1
                if failure["exc"] is not None:
                    raise failure["exc"]
                return error(failure["status"], failure["code"], "Injected failure.")
        parsed = urlparse(url)
        if url.startswith(EDGE):
            return self._edge(request, parsed)
        if not parsed.path.startswith("/v2"):
            return error(404, "route_not_found", "No route.")
        path = parsed.path[len("/v2"):]
        query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        body = json.loads(request.content) if request.content else {}
        return self._api(request, request.method, path, query, body)

    def _authed(self, request):
        return request.headers.get("Authorization") == f"Bearer {self.api_key}"

    def _end_user(self, request):
        return request.headers.get("Diffio-End-User")

    def _api(self, request, method, path, query, body):
        if path == "/models" and method == "GET":
            return httpx.Response(200, json={"object": "list", "data": MODELS, "next_cursor": None})
        share = re.fullmatch(r"/shares/([^/]+)", path)
        if share and method == "GET":
            found = self.shares.get(unquote(share.group(1)))
            if not found:
                return error(404, "share_not_found", "No share has that id, or it is no longer shared.")
            return httpx.Response(200, json=found)
        if not request.headers.get("Authorization"):
            return error(401, "missing_api_key", "Missing API key.")
        if not self._authed(request):
            return error(401, "invalid_api_key", "Invalid API key.")
        if path == "/projects" and method == "POST":
            return self._create_project(request, body)
        if path == "/projects" and method == "GET":
            return self._list(self._visible_projects(request), query, self._project)
        if path == "/usage" and method == "GET":
            return httpx.Response(200, json={
                "object": "usage", "start": query.get("start") or "2026-11-01T00:00:00Z",
                "end": query.get("end") or TS, "end_user": self._end_user(request), "generations": 1,
                "billable_seconds": 60, "list_price_usd": 0.0833,
                "by_model": [{"model": "diffio-4.7-flash", "generations": 1, "billable_seconds": 60,
                              "list_price_usd": 0.0833}]})
        if path == "/webhooks/portal" and method == "POST":
            if self._end_user(request):
                return error(403, "permission_denied", "The webhook portal is the account's.")
            return httpx.Response(200, json={"object": "webhook_portal", "url": "https://portal.test/login#key",
                                             "event_types": ["generation.completed", "generation.failed"]})
        match = re.fullmatch(r"/projects/([^/]+)(/.*)?", path)
        if not match:
            return error(404, "route_not_found", f"No version-two route matches {method} {path}.")
        project_id = unquote(match.group(1))
        project = self.projects.get(project_id)
        if project is None or project["end_user"] != self._end_user(request) and self._end_user(request):
            return error(404, "project_not_found", "No project has that id.", {"project_id": project_id})
        rest = match.group(2) or ""
        return self._project_route(request, method, project, rest, query, body)

    def _project_route(self, request, method, project, rest, query, body):
        pid = project["id"]
        if rest == "" and method == "GET":
            return httpx.Response(200, json=self._project(project))
        if rest == "" and method == "DELETE":
            running = [g for g in self._gens(pid) if g["status"] in ("queued", "processing") and project["status"] != "awaiting_upload"]
            if running:
                return error(409, "generation_in_progress", "A generation is running.",
                             {"generation_ids": [g["id"] for g in running]})
            gens = self._gens(pid)
            for gen in gens:
                self.generations.pop(gen["id"], None)
            self.projects.pop(pid)
            return httpx.Response(200, json={"id": pid, "object": "project", "deleted": True,
                                             "deleted_generations": len(gens), "deleted_media_objects": 3})
        if rest == "/upload" and method == "POST":
            if project["status"] != "awaiting_upload":
                return error(409, "upload_already_received", "The upload already arrived.",
                             {"project_status": project["status"]})
            return httpx.Response(200, json=self._project(project, upload=True))
        if rest == "/upload/complete" and method == "POST":
            if pid not in self.objects:
                return error(409, "upload_missing", "Upload has not completed yet.")
            project["status"] = "ready"
            project["size_bytes"] = len(self.objects[pid])
            project["duration_seconds"] = 31.9
            for gen in self._gens(pid):
                if gen["status"] == "queued":
                    gen["status"], gen["stage"] = "processing", "preparing"
            return httpx.Response(200, json=self._project(project))
        if rest == "/generations" and method == "POST":
            key = request.headers.get("Idempotency-Key")
            fingerprint = json.dumps(body, sort_keys=True)
            if key and ("gen", pid, key) in self.idempotency:
                gid, seen = self.idempotency[("gen", pid, key)]
                if seen != fingerprint:
                    return error(409, "idempotency_key_conflict", "idempotencyKey was already used.")
                return httpx.Response(201, json=self._generation(self.generations[gid]))
            if body.get("model") not in SERVED:
                return error(400, "unknown_model", f"{body.get('model')} is not a Diffio model. See GET /v2/models.",
                             {"field": "model", "models": [m["id"] for m in MODELS]})
            gen = self._new_generation(project, body)
            if key:
                self.idempotency[("gen", pid, key)] = (gen["id"], fingerprint)
            return httpx.Response(201, json=self._generation(gen))
        if rest == "/generations" and method == "GET":
            return self._list(self._gens(pid), query, self._generation)
        if rest == "/share" and method == "PUT":
            share = self._share(project, body.get("generation_id"))
            return httpx.Response(200, json=share)
        if rest == "/share" and method == "GET":
            sid = project.get("share_id")
            if not sid:
                return error(404, "share_not_found", "The project is not shared.")
            return httpx.Response(200, json=self.shares[sid])
        if rest == "/share" and method == "DELETE":
            sid = project.pop("share_id", None)
            self.shares.pop(sid, None)
            return httpx.Response(200, json={"object": "project_share", "id": sid, "project_id": pid, "deleted": True})
        match = re.fullmatch(r"/generations/([^/]+)(/.*)?", rest)
        if not match:
            return error(404, "route_not_found", "No route.")
        gen = self.generations.get(unquote(match.group(1)))
        if gen is None or gen["project_id"] != pid:
            return error(404, "generation_not_found", "No generation has that id.")
        return self._generation_route(request, method, project, gen, match.group(2) or "", query, body)

    def _generation_route(self, request, method, project, gen, rest, query, body):
        pid, gid = project["id"], gen["id"]
        base = {"project_id": pid, "generation_id": gid}
        if rest == "" and method == "GET":
            self._advance(gen)
            return httpx.Response(200, json=self._generation(gen))
        if rest == "/media" and method == "GET":
            files = {"original": f"{EDGE}/m/tok/original/{project['file_name'].replace(' ', '%20')}"}
            if gen["status"] == "complete":
                files["audio"] = f"{EDGE}/m/tok/generations/{gid}/restored.mp3"
                files["transcript"] = f"{EDGE}/m/tok/generations/{gid}/word_timestamps.json"
            return httpx.Response(200, json=dict(base, object="generation_media", status=gen["status"],
                                                 base_url=f"{EDGE}/m/tok/", expires_at="2026-11-13T06:33:47Z",
                                                 preview_seconds=int(query["preview_seconds"]) if "preview_seconds" in query else None,
                                                 files=files))
        if rest == "/edits/estimate" and method == "POST":
            return httpx.Response(200, json=dict(base, object="transcript_edit_estimate", edit_count=1, max_windows=1,
                                                 window_seconds=30, max_billable_seconds=30))
        if rest == "/edits" and method == "POST":
            edit = self._new_generation(project, {"model": gen["requested_model"], "label": "Transcript edit"})
            edit.update(kind="edit", source_generation_id=gid, edit={"edit_count": 1, "max_windows": 1, "windows": 1,
                        "windows_done": 0, "ranges": [{"start": 0.5, "end": 1.0}], "billable_seconds": None,
                        "cut_seconds": 0, "request_id": body.get("request_id"), "language": "en", "tighten": None})
            return httpx.Response(201, json=self._generation(edit))
        if rest == "/mix" and method == "GET":
            return httpx.Response(200, json=self.mixes.get(gid) or dict(base, object="generation_mix", background_gain=0,
                                  leveling=False, revision="rev:0", stems_revision="rev"))
        if rest == "/mix" and method == "PUT":
            current = self.mixes.get(gid) or {"revision": "rev:0"}
            if "revision" in body and body["revision"] != current["revision"]:
                return error(409, "revision_conflict", "This mix changed.", {"current_revision": current["revision"]})
            count = int(current["revision"].split(":")[1]) + 1
            self.mixes[gid] = dict(base, object="generation_mix", background_gain=body["background_gain"],
                                   leveling=body.get("leveling", False), revision=f"rev:{count}", stems_revision="rev")
            return httpx.Response(200, json=self.mixes[gid])
        if rest == "/exports" and method == "POST":
            eid = "d51fd816be75bc302ecc3def3da4f22d" + ("-inline" if body.get("inline") else "")
            self.exports[eid] = dict(base, object="generation_export", id=eid, status="queued",
                                     artifact=body["artifact"], format=body["format"],
                                     background_gain=body.get("background_gain", 0), leveling=bool(body.get("leveling")),
                                     inline=bool(body.get("inline")), url=None, error=None, polls=0)
            return httpx.Response(201, json=self._public(self.exports[eid]))
        match = re.fullmatch(r"/exports/([^/]+)", rest)
        if match and method == "GET":
            export = self.exports.get(unquote(match.group(1)))
            if not export:
                return error(404, "export_not_found", "No export has that id.")
            export["polls"] += 1
            if export["polls"] >= 2 and export["status"] != "failed":
                name = query.get("file_name")
                suffix = f"?download={name}.{export['format']}" if name else ""
                export.update(status="ready", url=f"{EDGE}/m/tok/generations/{gid}/diffio_mix.mp3{suffix}")
            return httpx.Response(200, json=self._public(export))
        if rest == "/transcript" and method == "GET":
            words = [{"id": "word-0", "text": "Hello", "start": 0.6, "end": 0.8, "probability": None, "language": "en"},
                     {"id": "word-1", "text": "there", "start": 0.84, "end": 0.94, "probability": None, "language": "en"}]
            return httpx.Response(200, json=dict(base, object="transcript", text="Hello there", words=words,
                                                 preview_seconds=None, truncated=False))
        if rest == "/transcript/draft":
            draft = self.drafts.setdefault(gid, dict(base, object="transcript_draft", revision="0", data=None,
                                                     created_at=None, updated_at=None))
            if method == "GET":
                return httpx.Response(200, json=draft)
            revision = body.get("revision") if method == "PUT" else query.get("revision")
            if revision is not None and revision != draft["revision"]:
                return error(409, "revision_conflict", "The draft was saved since that revision.",
                             {"current_revision": draft["revision"]})
            draft["revision"] = str(int(draft["revision"]) + 1)
            if method == "PUT":
                draft.update(data=body["data"], created_at=TS, updated_at=TS)
                return httpx.Response(200, json=draft)
            draft.update(data=None)
            return httpx.Response(200, json=dict(base, object="transcript_draft", deleted=True, revision=draft["revision"]))
        if rest == "/transcript/assistant/turns" and method == "POST":
            tid = uuid.uuid4().hex
            self.turns[tid] = dict(base, object="transcript_assistant_turn", id=tid, status="processing",
                                   created_at=TS, completed_at=None, error=None, polls=0)
            return httpx.Response(202, json=self._public(self.turns[tid]))
        match = re.fullmatch(r"/transcript/assistant/turns/([^/]+)", rest)
        if match and method == "GET":
            turn = self.turns.get(match.group(1))
            if not turn:
                return error(404, "transcript_assistant_turn_not_found", "No turn has that id.")
            turn["polls"] += 1
            if turn["polls"] >= 2:
                turn.update(status="complete", completed_at=TS, reply="Removed one filler word.", proposals=[],
                            moments=[], unclear_word_ids=[], speaker_turns=[], shortening=None,
                            feedback_offer=None, notices=[], windows_total=1)
            return httpx.Response(200, json=self._public(turn))
        return error(404, "route_not_found", "No route.")

    # ---- media host ----------------------------------------------------------------------------------

    def _edge(self, request, parsed):
        path = parsed.path
        if path.startswith("/m/"):
            if path.endswith("/restored.mp3") or path.endswith("/diffio_mix.mp3"):
                return httpx.Response(200, content=b"ID3restored-audio", headers={"Content-Type": "audio/mpeg"})
            if path.endswith(".json"):
                return httpx.Response(200, json={"words": []})
            return httpx.Response(404, json={"error": {"code": "not_found", "message": "Not found."}})
        token = (request.headers.get("Authorization") or "").replace("Bearer ", "")
        upload = next((u for u in self.uploads.values() if u["token"] == token), None)
        if upload is None:
            return httpx.Response(401, json={"error": {"code": "invalid_token", "message": "Upload token is not valid."}})
        query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        if path == "/v1/uploads/start":
            upload_id = uuid.uuid4().hex
            upload["multipart"][upload_id] = {}
            return httpx.Response(200, json={"uploadId": upload_id, "partSizeBytes": self.part_size, "maxBytes": 2147483648})
        match = re.fullmatch(r"/v1/uploads/parts/(\d+)", path)
        if match and request.method == "PUT":
            parts = upload["multipart"].get(query.get("uploadId"))
            if parts is None:
                return httpx.Response(404, json={"error": {"code": "not_found", "message": "No upload."}})
            if "content-length" not in request.headers:
                return httpx.Response(411, json={"error": {"code": "bad_request", "message": "Content-Length required."}})
            number = int(match.group(1))
            parts[number] = request.content
            return httpx.Response(200, json={"partNumber": number, "etag": f"etag-{number}"})
        body = json.loads(request.content) if request.content else {}
        if path == "/v1/uploads/complete":
            parts = upload["multipart"].get(body.get("uploadId"))
            listed = body.get("parts") or []
            if parts is None or [p["partNumber"] for p in listed] != sorted(parts):
                return httpx.Response(400, json={"error": {"code": "bad_request", "message": "Parts do not match."}})
            data = b"".join(parts[p["partNumber"]] for p in listed)
            self.objects[upload["project_id"]] = data
            return httpx.Response(200, json={"objectKey": "api/users/u/projects/x/original/f", "sizeBytes": len(data),
                                             "etag": "final-etag"})
        if path == "/v1/uploads/abort":
            upload["multipart"].pop(body.get("uploadId"), None)
            upload["aborted"] = True
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "No route."}})

    # ---- state ---------------------------------------------------------------------------------------

    def _create_project(self, request, body):
        if "end_user" in body:
            return error(400, "invalid_field", "end_user is not a body field: send the Diffio-End-User header.",
                         {"field": "end_user"})
        if not body.get("file_name"):
            return error(400, "missing_field", "file_name is required.", {"field": "file_name"})
        key = request.headers.get("Idempotency-Key")
        fingerprint = json.dumps(body, sort_keys=True)
        scope = ("project", self._end_user(request), key)
        if key and scope in self.idempotency:
            pid, seen = self.idempotency[scope]
            if seen != fingerprint:
                return error(409, "idempotency_key_conflict", "Idempotency-Key was already used for a different request.")
            project = self.projects[pid]
            return httpx.Response(201, json=self._project(project, upload=project["status"] == "awaiting_upload"))
        pid = str(uuid.uuid4())
        project = {"id": pid, "status": "awaiting_upload", "file_name": body["file_name"],
                   "content_type": body.get("content_type") or "application/octet-stream",
                   "size_bytes": body.get("size_bytes"), "duration_seconds": None, "has_video": False,
                   "end_user": self._end_user(request), "metadata": body.get("metadata") or {},
                   "created_at": TS, "seq": len(self.projects)}
        self.projects[pid] = project
        if key:
            self.idempotency[scope] = (pid, fingerprint)
        if body.get("generation"):
            gen = self._new_generation(project, body["generation"])
            gen.update(status="queued", stage="pending")
        return httpx.Response(201, json=self._project(project, upload=True))

    def _new_generation(self, project, body):
        gid = str(uuid.uuid4())
        gen = {"id": gid, "project_id": project["id"], "status": "processing", "stage": "preparing",
               "label": body.get("label"), "requested_model": body["model"], "model": SERVED.get(body["model"]),
               "kind": "restore", "source_generation_id": None, "edit": None, "polls": 0,
               "seq": len(self.generations), "created_at": TS}
        if project["status"] == "awaiting_upload":
            gen.update(status="queued", stage="pending")
        self.generations[gid] = gen
        return gen

    def _advance(self, gen):
        if gen["status"] != "processing":
            return
        gen["polls"] += 1
        if gen["polls"] >= self.polls_to_complete:
            if self.fail_generation_with:
                gen.update(status="failed", stage="failed")
            else:
                gen.update(status="complete", stage="complete")
        else:
            gen.update(stage="restoring")

    def _gens(self, pid):
        return sorted((g for g in self.generations.values() if g["project_id"] == pid), key=lambda g: -g["seq"])

    def _visible_projects(self, request):
        end_user = self._end_user(request)
        items = [p for p in self.projects.values() if not end_user or p["end_user"] == end_user]
        return sorted(items, key=lambda p: -p["seq"])

    def _list(self, items, query, render):
        limit = int(query.get("limit") or 20)
        if not 1 <= limit <= 100:
            return error(400, "invalid_parameter", "limit must be between 1 and 100.", {"parameter": "limit"})
        ids = [item["id"] for item in items]
        start = 0
        if query.get("cursor"):
            if query["cursor"] not in ids:
                return error(400, "invalid_parameter", "cursor is not from this list.", {"parameter": "cursor"})
            start = ids.index(query["cursor"]) + 1
        page = items[start:start + limit]
        more = start + limit < len(items)
        return httpx.Response(200, json={"object": "list", "data": [render(item) for item in page],
                                         "next_cursor": page[-1]["id"] if page and more else None})

    def _project(self, project, upload=False):
        gens = self._gens(project["id"])
        body = {key: value for key, value in project.items() if key != "seq"}
        body.update(object="project", generation_count=len(gens), video_preview_status=None,
                    latest_generation=self._generation(gens[0]) if gens else None,
                    share_id=project.get("share_id"), updated_at=TS, error=None)
        if upload:
            token = f"v1.upload-{project['id']}.{uuid.uuid4().hex[:8]}"
            self.uploads[project["id"]] = {"token": token, "project_id": project["id"], "multipart": {},
                                           "aborted": False}
            body["upload"] = self._upload_instructions(project["id"], token)
        return body

    def _upload_instructions(self, pid, token):
        base = f"{EDGE}/v1/uploads"
        auth = {"Authorization": f"Bearer {token}"}
        json_auth = dict(auth, **{"Content-Type": "application/json"})
        requests = {
            "start": {"method": "POST", "url": f"{base}/start", "headers": auth, "body": None,
                      "returns": ["uploadId", "partSizeBytes", "maxBytes"], "description": "Once, first."},
            "part": {"method": "PUT", "url": base + "/parts/{part_number}?uploadId={upload_id}",
                     "headers": dict(auth, **{"Content-Length": "{part_length}"}), "body": None,
                     "returns": ["partNumber", "etag"], "description": "Once per part."},
            "complete": {"method": "POST", "url": f"{base}/complete", "headers": json_auth,
                         "body": {"uploadId": "{upload_id}", "parts": [{"partNumber": "{part_number}", "etag": "{etag}"}]},
                         "returns": ["sizeBytes", "etag"], "description": "Once, after every part."},
            "abort": {"method": "POST", "url": f"{base}/abort", "headers": json_auth, "body": {"uploadId": "{upload_id}"},
                      "returns": [], "description": "Optional."},
        }
        instructions = {"upload_session_id": f"api-{pid}", "edge_base_url": EDGE, "upload_token": token,
                        "part_size_bytes": self.part_size, "max_bytes": 2147483648,
                        "expires_at": "2099-11-14T00:33:21.812909Z", "requests": requests}
        if self.omit_upload_requests:
            instructions.pop("requests")
        return instructions

    def _generation(self, gen):
        complete = gen["status"] == "complete"
        failed = gen["status"] == "failed"
        return {
            "id": gen["id"], "object": "generation", "project_id": gen["project_id"], "status": gen["status"],
            "stage": gen["stage"],
            "stage_progress": {"overall_percent": 50} if gen["status"] == "processing" else None,
            "queue": None, "warnings": [], "label": gen["label"], "kind": gen["kind"],
            "source_generation_id": gen["source_generation_id"], "edit": gen["edit"],
            "transcript_editable": complete and gen["model"] == "diffio-4.7-pro",
            "duration_seconds": 31.8 if complete else None, "quality_score": 4.57 if complete else None,
            "transcription_status": "ready" if complete else None,
            "files_available": ["audio", "transcript"] if complete else [],
            "error": ({"code": self.fail_generation_with, "message": "The restore failed.", "retryable": False,
                       "explanation": None} if failed else None),
            "model": gen["model"], "requested_model": gen["requested_model"], "substituted": False,
            "price_per_hour_usd": 10 if gen["model"] == "diffio-4.7-pro" else 5,
            "stems_status": "ready" if complete else None, "created_at": gen["created_at"], "updated_at": TS,
        }

    def _share(self, project, generation_id):
        sid = project.get("share_id") or "MUBLudGbyebz5CqCg"
        gid = generation_id or (self._gens(project["id"])[0]["id"] if self._gens(project["id"]) else None)
        share = {"object": "project_share", "id": sid, "project_id": project["id"], "generation_id": gid,
                 "file_name": project["file_name"], "content_type": project["content_type"], "created_at": TS,
                 "has_video": False, "duration_seconds": 31.9, "preview_seconds": None,
                 "base_url": f"{EDGE}/s/tok/", "files": {"audio": f"{EDGE}/s/tok/generations/{gid}/restored.mp3"}}
        project["share_id"] = sid
        self.shares[sid] = share
        return share

    @staticmethod
    def _public(record):
        return {key: value for key, value in record.items() if key != "polls"}
