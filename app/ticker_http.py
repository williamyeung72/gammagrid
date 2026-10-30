from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
import sys
import urllib.parse
from dataclasses import dataclass

_BODY_LIMIT = 4096
_READ_CAP = 4097


_ADMITTED = object()


class Grant:
    __slots__ = ()

    def __init__(self, seal: object) -> None:
        if seal is not _ADMITTED:
            raise RuntimeError("call admit")


class Presented:
    __slots__ = ("_digest",)

    def __init__(self, secret: str) -> None:
        self._digest = hashlib.sha256(secret.encode("utf-8")).digest()

    def __repr__(self) -> str:
        return "redacted"

    def __str__(self) -> str:
        return "redacted"


class Gate:
    __slots__ = ("_digest",)

    def __init__(self, digest: bytes) -> None:
        self._digest = digest

    @classmethod
    def from_env(cls) -> Gate | None:
        raw = os.environ.pop("GAMMAGRID_API_TOKEN", None)
        if raw is None or raw.strip() == "":
            return None
        digest = hashlib.sha256(raw.encode("utf-8")).digest()
        return cls(digest)

    def __repr__(self) -> str:
        return "redacted"

    def __str__(self) -> str:
        return "redacted"

    def accepts(self, presented: Presented) -> bool:
        return hmac.compare_digest(self._digest, presented._digest)


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes
    content_type: str


def log_request(method: str, path: str, status: int) -> None:
    print(f"{method} {path} {status}", flush=True)


def serve() -> None:
    gate = Gate.from_env()
    if gate is None:
        sys.stderr.write("GAMMAGRID_API_TOKEN is unset or blank\n")
        raise SystemExit(1)
    from app import db

    conn = db.get_connection()
    conn.close()
    host = os.environ.get("GAMMAGRID_API_BIND", "127.0.0.1")
    with socket.create_server((host, 8741)) as server:
        while True:
            client, _addr = server.accept()
            try:
                _handle(client, gate)
            finally:
                client.close()


def _admit(authorization: str | None, gate: Gate) -> Grant | None:
    if authorization is None or not authorization.startswith("Bearer "):
        return None
    presented = Presented(authorization[len("Bearer "):])
    if not gate.accepts(presented):
        return None
    return Grant(_ADMITTED)


def _handle(client: socket.socket, gate: Gate) -> None:
    client.settimeout(30)
    try:
        read = _read_head(client)
        if read is None:
            _write(client, _error(400, "bad_request", "The request could not be read."))
            return
        head, prefetched = read
        parsed = _split_head(head)
        if parsed is None:
            _write(client, _error(400, "bad_request", "The request could not be read."))
            return
        method, path, headers = parsed
        authorization = headers.pop("authorization", None)
        grant = _admit(authorization, gate)
        del authorization
        if grant is None:
            response = _error(401, "unauthorized")
            log_request(method, path, response.status)
            _write(client, response)
            return
        length = _content_length(headers)
        too_large = length == "bad" or len(prefetched) > _BODY_LIMIT
        if isinstance(length, int) and length > _BODY_LIMIT:
            too_large = True
        if too_large:
            response = _error(400, "bad_request", "The request body is too large.")
            log_request(method, path, response.status)
            _write(client, response)
            # Close with unread bytes becomes a TCP reset, and the 400 is lost.
            _drain(client, len(prefetched))
            return
        body = _collect_body(client, prefetched, length if isinstance(length, int) else None)
        if body is None:
            response = _error(400, "bad_request", "The request body is too large.")
            log_request(method, path, response.status)
            _write(client, response)
            return
        response = _respond(grant, method, path, body)
        log_request(method, path, response.status)
        _write(client, response)
    except (TimeoutError, OSError):
        return


def _read_head(client: socket.socket) -> tuple[bytes, bytes] | None:
    buf = bytearray()
    while b"\r\n\r\n" not in buf and len(buf) < 8192 + _READ_CAP:
        chunk = client.recv(1024)
        if not chunk:
            break
        buf.extend(chunk)
    raw = bytes(buf)
    marker = raw.find(b"\r\n\r\n")
    if marker < 0 or marker > 8192:
        return None
    return raw[:marker], raw[marker + 4:]


def _split_head(head: bytes) -> tuple[str, str, dict[str, str]] | None:
    text = head.decode("iso-8859-1")
    lines = text.split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/"):
        return None
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" not in line:
            return None
        name, value = line.split(":", 1)
        key = name.strip().lower()
        if key == "content-length" and key in headers:
            headers["content-length"] = "bad"
            continue
        headers[key] = value.strip()
    path = parts[1].split("?", 1)[0]
    if not path.startswith("/"):
        return None
    return parts[0].upper(), path, headers


def _content_length(headers: dict[str, str]) -> int | None | str:
    raw = headers.get("content-length")
    if raw is None:
        return None
    if raw == "bad" or not raw.isdigit():
        return "bad"
    return int(raw)


def _collect_body(client: socket.socket, prefetched: bytes, content_length: int | None) -> bytes | None:
    if len(prefetched) > _BODY_LIMIT:
        return None
    buf = bytearray(prefetched)
    while len(buf) < _READ_CAP:
        if content_length is not None and len(buf) >= content_length:
            extra = _pending(client, _READ_CAP - len(buf))
            if not extra:
                break
            buf.extend(extra)
            continue
        if content_length is None:
            extra = _pending(client, _READ_CAP - len(buf))
            if not extra:
                break
            buf.extend(extra)
            continue
        need = min(content_length - len(buf), _READ_CAP - len(buf))
        chunk = client.recv(need)
        if not chunk:
            break
        buf.extend(chunk)
    if len(buf) > _BODY_LIMIT:
        return None
    if content_length is not None and len(buf) < content_length:
        return None
    return bytes(buf)


def _drain(client: socket.socket, already: int) -> None:
    left = _READ_CAP - min(already, _READ_CAP)
    while left > 0:
        try:
            chunk = client.recv(min(left, 1024))
        except (TimeoutError, OSError):
            return
        if not chunk:
            return
        left -= len(chunk)


def _pending(client: socket.socket, count: int) -> bytes:
    if count <= 0:
        return b""
    previous = client.gettimeout()
    client.settimeout(0)
    try:
        return client.recv(count)
    except (BlockingIOError, TimeoutError):
        return b""
    finally:
        client.settimeout(previous)


def _respond(grant: Grant, method: str, path: str, body: bytes) -> Response:
    if not isinstance(grant, Grant):
        return _error(401, "unauthorized")
    if path == "/tickers":
        if method == "GET":
            return _list()
        if method == "POST":
            command = _command_post(body)
            if isinstance(command, Response):
                return command
            return _applied(command)
        return _error(405, "method_not_allowed")
    marker = "/tickers/"
    if path.startswith(marker):
        segment = path[len(marker):]
        if segment == "" or "/" in segment:
            return _error(404, "not_found")
        segment = urllib.parse.unquote(segment)
        if method == "PATCH":
            command = _command_patch(segment, body)
            if isinstance(command, Response):
                return command
            return _applied(command)
        if method == "DELETE":
            from app.watchlist import Remove

            return _applied(Remove(segment))
        return _error(405, "method_not_allowed")
    return _error(404, "not_found")


def _command_post(body: bytes):
    parsed = _object(body)
    if isinstance(parsed, Response):
        return parsed
    ticker = parsed.get("ticker")
    if set(parsed) != {"ticker"} or not isinstance(ticker, str):
        return _error(400, "bad_request", "POST /tickers takes one string field, ticker.")
    from app.watchlist import Add

    return Add(ticker)


def _command_patch(segment: str, body: bytes):
    parsed = _object(body)
    if isinstance(parsed, Response):
        return parsed
    pinned = parsed.get("pinned")
    if set(parsed) != {"pinned"} or not isinstance(pinned, bool):
        return _error(400, "bad_request", "PATCH takes one boolean field, pinned.")
    from app.watchlist import Pin, Unpin

    return Pin(segment) if pinned else Unpin(segment)


def _object(body: bytes) -> dict | Response:
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return _error(400, "bad_request", "The request body is not JSON.")
    if not isinstance(parsed, dict):
        return _error(400, "bad_request", "The request body is not a JSON object.")
    return parsed


def _list() -> Response:
    from app import db
    from app.watchlist import members

    conn = db.get_connection()
    try:
        payload = [_member_json(member) for member in members(conn)]
    finally:
        conn.close()
    return Response(200, _dump(payload), "application/json")


def _applied(command) -> Response:
    from app import db
    from app.watchlist import Missing, Rejected, Removed, Stored, apply

    conn = db.get_connection()
    try:
        result = apply(conn, command)
    finally:
        conn.close()
    if isinstance(result, Stored):
        status = 201 if result.created else 200
        return Response(status, _dump(_member_json(result.member)), "application/json")
    if isinstance(result, Removed):
        return Response(204, b"", "application/json")
    if isinstance(result, Rejected):
        return _error(422, "rejected", result.message)
    if isinstance(result, Missing):
        return _error(404, "missing", "That ticker is not on the watchlist.")
    return _error(400, "bad_request", "The request could not be read.")


def _member_json(member) -> dict:
    stamp = member.added_at
    if stamp.tzinfo is not None:
        stamp = stamp.replace(tzinfo=None)
    return {
        "ticker": member.ticker.value,
        "pinned": member.pinned,
        "added_at": stamp.isoformat(sep="T", timespec="seconds"),
    }


def _dump(payload) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _error(status: int, code: str, message: str | None = None) -> Response:
    payload = {"error": code}
    if message is not None:
        payload["message"] = message
    return Response(status, _dump(payload), "application/json")


_REASONS = {
    200: "OK",
    201: "Created",
    204: "No Content",
    400: "Bad Request",
    401: "Unauthorized",
    404: "Not Found",
    405: "Method Not Allowed",
    422: "Unprocessable Entity",
}


def _write(client: socket.socket, response: Response) -> None:
    reason = _REASONS.get(response.status, "Error")
    lines = [f"HTTP/1.1 {response.status} {reason}"]
    if response.body:
        lines.append(f"Content-Type: {response.content_type}")
    lines.append(f"Content-Length: {len(response.body)}")
    lines.append("Connection: close")
    client.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + response.body)


if __name__ == "__main__":
    serve()
