from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

ALLOWED_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}
DEFAULT_PROJECT_ID = "diffioai"
DEFAULT_AUTH_HOST = "127.0.0.1:9099"
DEFAULT_FUNCTIONS_HOST = "127.0.0.1:5001"
DEFAULT_WEB_API_KEY = "fake-api-key"
DEFAULT_REGION = "us-central1"


@dataclass
class EmulatorApiKeyResult:
    api_key: str
    key_id: str
    key_prefix: str
    label: str
    user_id: str
    email: str
    password: str
    id_token: str


class EmulatorApiKeyError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, payload: Any = None, code: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload
        self.code = code


def _normalize_origin(raw_host: str, default_port: int, label: str) -> tuple[str, str]:
    host = (raw_host or "").strip()
    if not host:
        raise ValueError(f"Missing {label} host.")
    if not host.startswith(("http://", "https://")):
        host = f"http://{host}"
    parsed = urlparse(host)
    hostname = parsed.hostname
    if not hostname:
        raise ValueError(f"Invalid {label} host {raw_host}.")
    if hostname not in ALLOWED_LOCAL_HOSTS:
        raise ValueError(f"Refusing to contact non-local {label} host {hostname}.")
    port = parsed.port or default_port
    origin = f"{parsed.scheme}://{hostname}:{port}"
    return origin, hostname


def _parse_error(payload: Any) -> tuple[str | None, dict | None]:
    if not isinstance(payload, dict):
        return None, None
    error = payload.get("error")
    if not isinstance(error, dict):
        return None, None
    message = error.get("message") or error.get("status")
    return message, error


def _request_json(
    client: httpx.Client,
    *,
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    json_body: dict | None = None,
) -> dict:
    response = client.request(method, url, headers=headers, json=json_body)
    payload: Any = None
    try:
        payload = response.json()
    except ValueError:
        payload = None
    error_code, _ = _parse_error(payload)
    if response.status_code >= 400 or error_code:
        message = error_code or response.text or f"Request failed with {response.status_code}."
        raise EmulatorApiKeyError(message, status_code=response.status_code, payload=payload, code=error_code)
    if not isinstance(payload, dict):
        raise EmulatorApiKeyError("Unexpected response payload.", status_code=response.status_code, payload=payload)
    return payload


def _create_or_sign_in_user(
    client: httpx.Client,
    *,
    identity_base: str,
    email: str,
    password: str,
    web_api_key: str,
    allow_existing: bool,
) -> tuple[str, str]:
    signup_url = f"{identity_base}/accounts:signUp?key={web_api_key}"
    payload = {"email": email, "password": password, "returnSecureToken": True}
    try:
        data = _request_json(client, method="POST", url=signup_url, json_body=payload)
    except EmulatorApiKeyError as exc:
        if exc.code == "EMAIL_EXISTS":
            if not allow_existing:
                raise EmulatorApiKeyError(
                    "Email already exists. Provide the correct password to sign in.",
                    status_code=exc.status_code,
                    payload=exc.payload,
                    code=exc.code,
                ) from exc
            signin_url = f"{identity_base}/accounts:signInWithPassword?key={web_api_key}"
            data = _request_json(client, method="POST", url=signin_url, json_body=payload)
        else:
            raise
    uid = data.get("localId")
    id_token = data.get("idToken")
    if not uid or not id_token:
        raise EmulatorApiKeyError("Auth emulator response missing uid or idToken.", payload=data)
    return uid, id_token


def create_emulator_api_key(
    *,
    label: str | None = None,
    email: str | None = None,
    password: str | None = None,
    project_id: str | None = None,
    auth_emulator_host: str | None = None,
    functions_emulator_host: str | None = None,
    web_api_key: str | None = None,
    region: str | None = None,
    is_restricted: bool = False,
    spend_limit: float | None = None,
    permissions: dict | None = None,
    http_client: httpx.Client | None = None,
) -> EmulatorApiKeyResult:
    project_id = project_id or os.environ.get("FIREBASE_PROJECT_ID", DEFAULT_PROJECT_ID)
    region = region or DEFAULT_REGION
    auth_host = auth_emulator_host or os.environ.get("FIREBASE_AUTH_EMULATOR_HOST", DEFAULT_AUTH_HOST)
    functions_host = functions_emulator_host or os.environ.get(
        "FUNCTIONS_EMULATOR_HOST",
        os.environ.get("FIREBASE_FUNCTIONS_EMULATOR_HOST", DEFAULT_FUNCTIONS_HOST),
    )
    web_api_key = web_api_key or os.environ.get("FIREBASE_WEB_API_KEY", DEFAULT_WEB_API_KEY)

    auth_origin, _ = _normalize_origin(auth_host, 9099, "Auth emulator")
    functions_origin, _ = _normalize_origin(functions_host, 5001, "Functions emulator")

    label = label or f"test-key-{secrets.token_hex(4)}"
    email = email or f"test-{secrets.token_hex(4)}@example.com"
    password_provided = password is not None
    password = password or secrets.token_urlsafe(12)

    if is_restricted or spend_limit is not None:
        if spend_limit is None:
            raise ValueError("spendLimit is required when restricted.")
        is_restricted = True

    owns_client = http_client is None
    client = http_client or httpx.Client(timeout=10.0)

    try:
        identity_base = f"{auth_origin}/identitytoolkit.googleapis.com/v1"
        uid, id_token = _create_or_sign_in_user(
            client,
            identity_base=identity_base,
            email=email,
            password=password,
            web_api_key=web_api_key,
            allow_existing=password_provided,
        )

        data_payload: dict[str, Any] = {"label": label}
        if is_restricted:
            data_payload["isRestricted"] = True
            data_payload["spendLimit"] = spend_limit
        if permissions is not None:
            data_payload["permissions"] = permissions

        function_url = f"{functions_origin}/{project_id}/{region}/create_api_key"
        response = _request_json(
            client,
            method="POST",
            url=function_url,
            headers={"Authorization": f"Bearer {id_token}"},
            json_body={"data": data_payload},
        )
        result = response.get("result") or response.get("data") or response
        if not isinstance(result, dict):
            raise EmulatorApiKeyError("Callable response missing result data.", payload=response)
        api_key = result.get("key")
        key_id = result.get("keyId")
        key_prefix = result.get("keyPrefix")
        if not api_key or not key_id:
            raise EmulatorApiKeyError("Callable response missing key data.", payload=result)

        return EmulatorApiKeyResult(
            api_key=api_key,
            key_id=key_id,
            key_prefix=key_prefix or "",
            label=str(result.get("label") or label),
            user_id=uid,
            email=email,
            password=password,
            id_token=id_token,
        )
    finally:
        if owns_client:
            client.close()
