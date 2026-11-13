"""Webhook signature verification (Svix scheme), with no network access and no extra dependency."""

import base64
import binascii
import hashlib
import hmac
import json
import time

from .errors import WebhookVerificationError
from .types import WebhookEvent

DEFAULT_TOLERANCE_SECONDS = 300
_SECRET_PREFIX = "whsec_"
# Svix sends svix-*; the unbranded Standard Webhooks names carry the same values.
_HEADER_NAMES = {
    "id": ("svix-id", "webhook-id"),
    "timestamp": ("svix-timestamp", "webhook-timestamp"),
    "signature": ("svix-signature", "webhook-signature"),
}


def _lower_headers(headers):
    if headers is None or not hasattr(headers, "items"):
        raise WebhookVerificationError("headers must be a mapping of the request's headers")
    lowered = {}
    for key, value in headers.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            value = " ".join(str(item) for item in value)
        lowered[str(key).lower()] = str(value)
    return lowered


def _header(lowered, kind):
    for name in _HEADER_NAMES[kind]:
        value = lowered.get(name)
        if value:
            return value
    raise WebhookVerificationError(f"Missing webhook header {_HEADER_NAMES[kind][0]}")


def _secret_bytes(secret):
    if isinstance(secret, bytes):
        return secret
    if not isinstance(secret, str) or not secret:
        raise WebhookVerificationError("secret is required (the endpoint's whsec_... signing secret)")
    encoded = secret[len(_SECRET_PREFIX):] if secret.startswith(_SECRET_PREFIX) else secret
    try:
        return base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise WebhookVerificationError("secret is not a valid whsec_ signing secret") from None


def sign_webhook(payload, *, msg_id, timestamp, secret):
    """Return the v1,<signature> value Svix would send for payload (useful for tests)."""
    body = payload.decode("utf-8") if isinstance(payload, (bytes, bytearray)) else str(payload)
    signed = f"{msg_id}.{int(timestamp)}.{body}".encode("utf-8")
    digest = hmac.new(_secret_bytes(secret), signed, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode("ascii")


def verify_webhook_signature(payload, headers, secret, *, tolerance_seconds=DEFAULT_TOLERANCE_SECONDS, now=None):
    """Check a webhook's svix-signature against the endpoint secret and return the parsed WebhookEvent.

    payload is the raw request body (bytes or str) exactly as received; do not parse and re-serialize it.
    headers is the request's headers (any mapping; names are matched case-insensitively). secret is the
    endpoint's signing secret from the webhook portal (whsec_...). Raises WebhookVerificationError when the
    headers are missing, the timestamp is more than tolerance_seconds from now, or no signature matches.
    """
    if payload is None or not isinstance(payload, (bytes, bytearray, str)):
        raise WebhookVerificationError("payload must be the raw request body as bytes or str")
    lowered = _lower_headers(headers)
    msg_id = _header(lowered, "id")
    timestamp_text = _header(lowered, "timestamp")
    signature_header = _header(lowered, "signature")

    try:
        timestamp = int(timestamp_text)
    except ValueError:
        raise WebhookVerificationError("Invalid webhook timestamp") from None
    current = int(time.time() if now is None else now)
    if timestamp < current - tolerance_seconds:
        raise WebhookVerificationError("Webhook timestamp is too old")
    if timestamp > current + tolerance_seconds:
        raise WebhookVerificationError("Webhook timestamp is in the future")

    expected = sign_webhook(payload, msg_id=msg_id, timestamp=timestamp, secret=secret).split(",", 1)[1]
    matched = False
    for candidate in signature_header.split(" "):
        version, _, signature = candidate.partition(",")
        if version == "v1" and hmac.compare_digest(signature.encode("utf-8"), expected.encode("utf-8")):
            matched = True
            break
    if not matched:
        raise WebhookVerificationError("No matching webhook signature")

    body = payload.decode("utf-8") if isinstance(payload, (bytes, bytearray)) else payload
    try:
        event = json.loads(body)
    except ValueError:
        raise WebhookVerificationError("Webhook body is not JSON") from None
    if not isinstance(event, dict):
        raise WebhookVerificationError("Webhook body must be a JSON object")
    return WebhookEvent.from_dict(event)
