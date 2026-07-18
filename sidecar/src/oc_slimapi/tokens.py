"""Persistent-secret, stateless route tokens for mutation routing."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Any

import orjson


class RouteTokenError(ValueError):
    pass


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def issue_route_token(
    secret: bytes,
    *,
    kind: str,
    request_id: str,
    session_id: str | None,
    directory: str,
    now: int | None = None,
) -> str:
    issued = int(time.time()) if now is None else now
    payload = orjson.dumps({
        "v": 1,
        "kind": kind,
        "requestID": request_id,
        "sessionID": session_id,
        "directory": directory,
        "iat": issued,
        "exp": issued + 3600,
    }, option=orjson.OPT_SORT_KEYS)
    signature = hmac.new(secret, payload, hashlib.sha256).digest()
    return f"{_b64(payload)}.{_b64(signature)}"


def verify_route_token(
    token: str,
    secret: bytes,
    *,
    kind: str,
    request_id: str,
    session_id: str | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    try:
        payload_part, signature_part = token.split(".", 1)
        payload_bytes = _unb64(payload_part)
        signature = _unb64(signature_part)
        expected = hmac.new(secret, payload_bytes, hashlib.sha256).digest()
        payload = orjson.loads(payload_bytes)
    except Exception as exc:
        raise RouteTokenError("malformed route token") from exc
    if not hmac.compare_digest(signature, expected):
        raise RouteTokenError("invalid route token signature")
    current = int(time.time()) if now is None else now
    if payload.get("v") != 1 or payload.get("exp", 0) < current or payload.get("iat", current + 1) > current + 60:
        raise RouteTokenError("expired or invalid route token")
    if payload.get("kind") != kind or payload.get("requestID") != request_id:
        raise RouteTokenError("route token does not match request")
    if session_id is not None and payload.get("sessionID") != session_id:
        raise RouteTokenError("route token does not match session")
    if not isinstance(payload.get("directory"), str):
        raise RouteTokenError("route token has no directory")
    return payload
