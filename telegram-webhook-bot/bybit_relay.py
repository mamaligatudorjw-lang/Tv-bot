"""Closed HTTPS relay for the Bybit Demo API.

Run this small service in a Bybit-permitted region, behind an HTTPS
terminator.  It is intentionally not a generic proxy: the upstream host and
the allowed API path are fixed in code, and callers must present the shared
relay token.
"""

from __future__ import annotations

import hmac
import os
from pathlib import Path
from typing import Any

import requests
from flask import Flask, Response, jsonify, request

BYBIT_RELAY_TARGET = "https://api-demo.bybit.com"
BYBIT_RELAY_TOKEN_ENV = "BYBIT_RELAY_TOKEN"
BYBIT_RELAY_TIMEOUT = 8.0
BYBIT_RELAY_MAX_BODY_BYTES = 128 * 1024
_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def _forward_headers() -> dict[str, str]:
    """Forward only headers required by Bybit, never the relay credential."""
    allowed = {"accept", "content-type", "user-agent"}
    forwarded: dict[str, str] = {}
    for name, value in request.headers.items():
        lower = name.lower()
        if lower.startswith("x-bapi-") or lower in allowed:
            forwarded[name] = value
    return forwarded


def _is_https_request() -> bool:
    forwarded = request.headers.get("X-Forwarded-Proto")
    if forwarded:
        return forwarded.split(",", 1)[0].strip().lower() == "https"
    return request.is_secure


def create_app(
    *,
    shared_token: str | None = None,
    session: Any | None = None,
) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = BYBIT_RELAY_MAX_BODY_BYTES
    if shared_token is not None:
        token = shared_token.strip()
    else:
        token_file = os.environ.get("BYBIT_RELAY_TOKEN_FILE", "").strip()
        if token_file:
            try:
                token = Path(token_file).read_text(encoding="utf-8").strip()
            except OSError:
                token = ""
        else:
            token = os.environ.get(BYBIT_RELAY_TOKEN_ENV, "").strip()
    http = session or requests.Session()

    def _authorized() -> bool:
        supplied = request.headers.get("X-Bybit-Relay-Token", "")
        return bool(token) and hmac.compare_digest(supplied, token)

    @app.get("/healthz")
    def healthz() -> Response:
        if not _authorized():
            return jsonify({"error": "unauthorized"}), 401
        if not _is_https_request():
            return jsonify({"error": "https_required"}), 400
        return jsonify({"ok": True, "upstream": BYBIT_RELAY_TARGET})

    @app.route("/v5/<path:api_path>", methods=["GET", "POST"])
    def forward(api_path: str) -> Response:
        if not _authorized():
            return jsonify({"error": "unauthorized"}), 401
        if not _is_https_request():
            return jsonify({"error": "https_required"}), 400
        if request.content_length and request.content_length > BYBIT_RELAY_MAX_BODY_BYTES:
            return jsonify({"error": "request_too_large"}), 413

        upstream_url = f"{BYBIT_RELAY_TARGET}/v5/{api_path}"
        if request.query_string:
            upstream_url = (
                f"{upstream_url}?{request.query_string.decode('ascii', errors='strict')}"
            )
        try:
            upstream = http.request(
                request.method,
                upstream_url,
                data=request.get_data(cache=True),
                headers=_forward_headers(),
                timeout=BYBIT_RELAY_TIMEOUT,
            )
        except requests.RequestException:
            return jsonify({"error": "upstream_unavailable"}), 502

        response_headers = {
            name: value
            for name, value in upstream.headers.items()
            if name.lower() not in _HOP_BY_HOP_HEADERS
            and name.lower() not in {"content-length", "content-encoding"}
        }
        return Response(
            upstream.content,
            status=upstream.status_code,
            headers=response_headers,
        )

    return app


app = create_app()