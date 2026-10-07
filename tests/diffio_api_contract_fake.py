import hashlib
import json
import uuid
from urllib.parse import parse_qs, unquote

import httpx

# Mirrors diffio-ui api/api_projects.py, api/api_generations.py, api/api_model_retirement.py and the
# edge Worker upload and media routes (edge/src/uploadRoutes.ts, specs/MacFleetArchitecture.md).
FAKE_API_ORIGIN = "https://api.test"
FAKE_EDGE_ORIGIN = "https://media.test"
FAKE_EDGE_PART_SIZE_BYTES = 32 * 1024 * 1024
FAKE_API_MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
FAKE_SUPPORTED_MODEL_KEYS = ("diffio-4.5-flash", "diffio-4.5-pro")
FAKE_RESTORED_AUDIO_BYTES = b"ID3-restored-audio"


class FakeDiffioApiContract:
    """In-memory stand-in for the current Diffio API and edge Worker, served through httpx.MockTransport.

    It answers exactly the shapes the production handlers return and enforces the edge upload rules
    (bearer upload token, Content-Length on parts, part size and number limits, completion etags).
    """

    def __init__(self, *, apiKey="diffio_test_key", partSizeBytes=FAKE_EDGE_PART_SIZE_BYTES,
                 maxUploadBytes=FAKE_API_MAX_UPLOAD_BYTES):
        self.apiKey = apiKey
        self.partSizeBytes = partSizeBytes
        self.maxUploadBytes = maxUploadBytes
        self.projects = {}
        self.generations = {}
        self.multipartUploads = {}
        self.storedObjects = {}
        self.requests = []
        self.partFailures = {}
        self.uploadCallbacks = []

    def build_http_client(self):
        return httpx.Client(base_url=FAKE_API_ORIGIN, transport=httpx.MockTransport(self.handle_request))

    def fail_part_once(self, partNumber, statusCode=503):
        self.partFailures[partNumber] = statusCode

    def handle_request(self, request):
        self.requests.append(request)
        if request.url.host == "api.test":
            return self._handle_api_request(request)
        if request.url.host == "media.test":
            return self._handle_edge_request(request)
        return httpx.Response(404, json={"error": "Unknown host."})

    def requests_for_path(self, path):
        return [request for request in self.requests if request.url.path == path]

    def _api_json(self, request):
        try:
            return json.loads(request.content.decode("utf-8") or "{}")
        except ValueError:
            return None

    def _handle_api_request(self, request):
        if request.headers.get("Authorization") != f"Bearer {self.apiKey}":
            return httpx.Response(401, json={"error": "Invalid API key."})
        path = request.url.path
        payload = self._api_json(request)
        if not isinstance(payload, dict):
            return httpx.Response(400, json={"error": "Invalid JSON body."})
        if path == "/v1/create_project":
            return self._create_project(payload)
        if path == "/v1/complete_project_upload":
            return self._complete_project_upload(payload)
        if path.startswith("/v1/diffio-") and path.endswith("-generation"):
            model_key = path[len("/v1/"):-len("-generation")]
            if model_key not in FAKE_SUPPORTED_MODEL_KEYS:
                return httpx.Response(410, json={
                    "error": f"{model_key} has been retired. Use diffio-4.5-flash or diffio-4.5-pro "
                             "(/v1/diffio-4.5-flash-generation or /v1/diffio-4.5-pro-generation).",
                    "code": "model_retired", "retiredModel": model_key,
                    "supportedModels": list(FAKE_SUPPORTED_MODEL_KEYS)})
            return self._create_generation(payload, model_key)
        if path == "/v1/get_generation_progress":
            return self._get_generation_progress(payload)
        if path == "/v1/get_generation_download":
            return self._get_generation_download(payload)
        return httpx.Response(404, json={"error": "Not found."})

    def _create_project(self, payload):
        file_name = payload.get("fileName")
        if not file_name or not isinstance(file_name, str):
            return httpx.Response(400, json={"error": "fileName is required."})
        content_type = payload.get("contentType") or "application/octet-stream"
        content_length = payload.get("contentLength")
        if content_length is not None and int(content_length) > self.maxUploadBytes:
            return httpx.Response(413, json={"error": "contentLength exceeds the 2 GiB upload limit.",
                                             "code": "UPLOAD_TOO_LARGE"})
        api_project_id = str(uuid.uuid4())
        object_key = f"api/users/user-1/projects/{api_project_id}/original/{file_name}"
        upload_session_id = f"api-{api_project_id}"
        upload_token = f"v1.upload-{api_project_id}.sig"
        self.projects[api_project_id] = {
            "storagePath": object_key,
            "uploadSessionId": upload_session_id,
            "uploadToken": upload_token,
            "contentType": content_type,
            "params": payload.get("params"),
            "fileFormat": payload.get("fileFormat"),
            "uploadCompletedAt": None,
        }
        expires_at = "2026-10-08T12:00:00Z"
        return httpx.Response(200, json={
            "apiProjectId": api_project_id,
            "upload": {
                "uploadSessionId": upload_session_id,
                "edgeBaseUrl": FAKE_EDGE_ORIGIN,
                "uploadToken": upload_token,
                "objectKey": object_key,
                "partSizeBytes": self.partSizeBytes,
                "maxBytes": self.maxUploadBytes,
                "expiresAt": expires_at,
            },
            "objectPath": object_key,
            "expiresAt": expires_at,
        })

    def _complete_project_upload(self, payload):
        api_project_id = payload.get("apiProjectId")
        project = self.projects.get(api_project_id)
        if project is None:
            return httpx.Response(404, json={"error": "API project not found."})
        stored = self.storedObjects.get(project["storagePath"])
        if stored is None:
            return httpx.Response(409, json={"error": "Upload has not completed yet.", "code": "UPLOAD_MISSING"})
        project["uploadCompletedAt"] = "now"
        return httpx.Response(200, json={"apiProjectId": api_project_id, "status": "uploaded",
                                         "sizeBytes": len(stored)})

    def _create_generation(self, payload, model_key):
        api_project_id = payload.get("apiProjectId")
        project = self.projects.get(api_project_id)
        if project is None:
            return httpx.Response(404, json={"error": "API project not found."})
        if not project["uploadCompletedAt"] and project["storagePath"] not in self.storedObjects:
            return httpx.Response(409, json={"error": "Upload the project file before creating a generation.",
                                             "code": "UPLOAD_MISSING"})
        generation_id = str(uuid.uuid4())
        self.generations[generation_id] = {"apiProjectId": api_project_id, "modelKey": model_key,
                                           "sampling": payload.get("sampling"), "params": payload.get("params")}
        return httpx.Response(200, json={"generationId": generation_id, "apiProjectId": api_project_id,
                                         "modelKey": model_key, "status": "queued"})

    def _get_generation_progress(self, payload):
        generation_id = payload.get("generationId")
        generation = self.generations.get(generation_id)
        if generation is None:
            return httpx.Response(404, json={"error": "Generation not found."})
        stage = {"jobId": None, "jobState": None, "status": "complete", "progress": 100,
                 "statusMessage": None, "error": None, "errorDetails": None}
        return httpx.Response(200, json={
            "generationId": generation_id,
            "apiProjectId": generation["apiProjectId"],
            "status": "complete",
            "hasVideo": False,
            "preProcessing": stage,
            "inference": dict(stage),
            "stage": "complete",
            "transcription": {"status": "available"},
        })

    def _get_generation_download(self, payload):
        generation_id = payload.get("generationId")
        generation = self.generations.get(generation_id)
        if generation is None:
            return httpx.Response(404, json={"error": "Generation not found."})
        api_project_id = generation["apiProjectId"]
        prefix = f"api/users/user-1/projects/{api_project_id}/"
        object_key = f"{prefix}generations/{generation_id}/restored.mp3"
        self.storedObjects[object_key] = FAKE_RESTORED_AUDIO_BYTES
        relative_key = object_key[len(prefix):]
        return httpx.Response(200, json={
            "generationId": generation_id,
            "apiProjectId": api_project_id,
            "downloadType": "audio",
            "downloadUrl": f"{FAKE_EDGE_ORIGIN}/m/v1.media-{api_project_id}.sig/{relative_key}"
                           "?download=diffio_ai_sample.mp3",
            "fileName": "diffio_ai_sample.mp3",
            "storagePath": object_key,
            "mimeType": "audio/mpeg",
        })

    def _edge_error(self, status, code, message):
        return httpx.Response(status, json={"error": {"code": code, "message": message}})

    def _handle_edge_request(self, request):
        parts = request.url.path.lstrip("/").split("/")
        if parts[0] == "m" and request.method == "GET":
            for object_key, content in self.storedObjects.items():
                if object_key.endswith("/" + "/".join(parts[2:])):
                    return httpx.Response(200, content=content, headers={"Content-Type": "audio/mpeg"})
            return self._edge_error(404, "not_found", "No such media")
        if parts[:2] != ["v1", "uploads"] or len(parts) < 3:
            return self._edge_error(404, "not_found", "Unknown route")
        authorization = request.headers.get("Authorization") or ""
        project = None
        for candidate in self.projects.values():
            if authorization == f"Bearer {candidate['uploadToken']}":
                project = candidate
        if project is None:
            return self._edge_error(401, "invalid_token", "Upload token refused")
        action = parts[2]
        extra = parts[3:]
        if action == "start" and request.method == "POST" and not extra:
            upload_id = f"upload-{len(self.multipartUploads) + 1}"
            self.multipartUploads[upload_id] = {"objectKey": project["storagePath"], "parts": {}}
            return httpx.Response(200, json={"uploadId": upload_id, "partSizeBytes": self.partSizeBytes,
                                             "maxBytes": self.maxUploadBytes})
        if action == "parts" and request.method == "PUT" and len(extra) == 1:
            upload_id = parse_qs(request.url.query.decode("ascii")).get("uploadId", [None])[0]
            if not extra[0].isdigit() or not upload_id or upload_id not in self.multipartUploads:
                return self._edge_error(400, "bad_request", "Need a part number and uploadId")
            part_number = int(extra[0])
            if part_number < 1 or part_number > 10000:
                return self._edge_error(400, "bad_request", "Need a part number and uploadId")
            if part_number * self.partSizeBytes > self.maxUploadBytes + self.partSizeBytes:
                return self._edge_error(413, "upload_too_large", "Part is beyond the upload size limit")
            declared_length = request.headers.get("Content-Length")
            if declared_length is None:
                return self._edge_error(411, "bad_request", "Parts need a Content-Length")
            if int(declared_length) > self.partSizeBytes:
                return self._edge_error(413, "payload_too_large", "Part exceeds 32 MiB")
            failure_status = self.partFailures.pop(part_number, None)
            if failure_status is not None:
                return self._edge_error(failure_status, "internal_error", "Injected part failure")
            body = request.read()
            etag = hashlib.md5(body).hexdigest()
            self.multipartUploads[upload_id]["parts"][part_number] = (etag, body)
            return httpx.Response(200, json={"partNumber": part_number, "etag": etag})
        if action == "complete" and request.method == "POST" and not extra:
            payload = self._api_json(request) or {}
            upload_id = payload.get("uploadId")
            receipts = payload.get("parts")
            upload = self.multipartUploads.get(upload_id)
            if upload is None or not isinstance(receipts, list) or not receipts:
                return self._edge_error(400, "bad_request", "Need uploadId and parts")
            ordered = sorted(receipts, key=lambda receipt: receipt["partNumber"])
            body = b""
            for receipt in ordered:
                stored_part = upload["parts"].get(receipt["partNumber"])
                if stored_part is None or stored_part[0] != receipt["etag"]:
                    return self._edge_error(400, "upload_failed", "Part etag mismatch")
                body += stored_part[1]
            for index, receipt in enumerate(ordered[:-1]):
                if len(upload["parts"][receipt["partNumber"]][1]) != self.partSizeBytes:
                    return self._edge_error(400, "upload_failed", f"Part {index + 1} is not partSizeBytes long")
            if len(body) > self.maxUploadBytes:
                return self._edge_error(413, "upload_too_large", "Upload is too large")
            self.storedObjects[upload["objectKey"]] = body
            self.uploadCallbacks.append(upload["objectKey"])
            return httpx.Response(200, json={"objectKey": upload["objectKey"], "sizeBytes": len(body),
                                             "etag": hashlib.md5(body).hexdigest()})
        if action == "abort" and request.method == "POST" and not extra:
            payload = self._api_json(request) or {}
            self.multipartUploads.pop(payload.get("uploadId"), None)
            return httpx.Response(200, json={})
        return self._edge_error(404, "not_found", "Unknown upload route")


def read_stored_upload_bytes(fake, apiProjectId):
    """Return the bytes the fake edge stored for a project's original upload, or None."""
    project = fake.projects[apiProjectId]
    return fake.storedObjects.get(unquote(project["storagePath"]))
