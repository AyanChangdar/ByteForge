"""Loopback-only, single-workspace HTTP controller for the existing trace engine."""

import argparse
from dataclasses import asdict
from http import HTTPStatus
from http.client import LineTooLong
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import re
import secrets
import socket
import threading
import time
from urllib.parse import parse_qs, urlsplit

from byteforge.model import ByteForgeError
from byteforge.report import SCHEMA_VERSION, assembly_report, error_report, memory_report, step_report, view_report
from byteforge.trace import TraceSession, prepare_trace, read_memory
from byteforge.trace_model import TraceLimits, TraceStatus


MAX_BODY_BYTES = 512 * 1024
MAX_HEADER_BYTES = 16 * 1024
MAX_TIMELINE_PAGE = 100
EXAMPLE_NAMES = ("exit", "hello", "arithmetic", "loop", "conditional")
PROCESS_TOKEN = secrets.token_urlsafe(32)
GET_ROUTES = ("/api/bootstrap", "/api/examples", "/api/state", "/api/timeline", "/api/memory", "/api/elf")
POST_ROUTES = ("/api/assemble", "/api/trace")


class APIError(Exception):
    def __init__(self, status, phase, code, message, location=None):
        super().__init__(message)
        self.status = status
        self.detail = error_report(phase, code, message, location)


def _integer(value, name, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise APIError(400, "request", "invalid_field", f"{name} must be an integer in {minimum}..{maximum}")
    return value


def _fields(data, allowed, required=()):
    if set(data) - set(allowed) or set(required) - set(data):
        raise APIError(400, "request", "invalid_fields", "Missing or unsupported request fields")


def _unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("Duplicate JSON key")
        result[name] = value
    return result


def _reject_constant(value):
    raise ValueError("Non-finite JSON number")


class TraceHTTPServer(HTTPServer):
    """HTTPServer serializes all requests; no background thread mutates a workspace."""

    def __init__(self, port=8765, *, limits=None, request_timeout=3.0):
        _integer(port, "port", 0, 65535)
        if not 0.05 <= request_timeout <= 10:
            raise ValueError("Request timeout must be in 0.05..10 seconds")
        self.limits = limits or TraceLimits()
        self.request_timeout = request_timeout
        self.mutation_token = PROCESS_TOKEN
        self.session = None
        self.session_id = None
        self.revision = 0
        self.events = []
        self.examples = []
        example_dir = Path(__file__).resolve().parents[1] / "examples"
        for name in EXAMPLE_NAMES:
            path = example_dir / (name + ".asm")
            if path.resolve().parent != example_dir:
                raise ValueError("Example path is outside the fixed example directory")
            with path.open("rb") as source_file:
                raw = source_file.read(65537)
            if len(raw) > 65536:
                raise ValueError("Allowlisted example exceeds source limit")
            self.examples.append({"name": name, "filename": path.name, "source": raw.decode("utf-8")})
        super().__init__(("127.0.0.1", port), TraceHandler)
        actual_port = self.server_address[1]
        self.authority = "127.0.0.1" if actual_port == 80 else f"127.0.0.1:{actual_port}"
        self.origin = f"http://{self.authority}"

    def check_context(self, data):
        _fields(data, data.keys(), ("session_id", "revision"))
        revision = _integer(data["revision"], "revision", 0, (1 << 53) - 1)
        session_id = data["session_id"]
        if session_id is not None and (not isinstance(session_id, str) or len(session_id) > 128):
            raise APIError(400, "session", "invalid_session", "Invalid session ID")
        if session_id != self.session_id or revision != self.revision:
            raise APIError(409, "session", "stale_workspace", "Session ID or revision is stale; fetch /api/state")

    def active(self):
        if self.session is None:
            raise APIError(409, "session", "not_assembled", "Assemble a program first")
        return self.session

    def current(self):
        view = view_report(self.session.view()) if self.session else {
            "execution_status": "not_assembled", "cpu": None, "stdout": None,
            "cursor": 0, "history_size": 0, "last_step": None,
        }
        return {"session_id": self.session_id, "revision": self.revision, "view": view}

    def advance(self, count):
        # Capture public next() results without accessing TraceSession's private
        # history or introducing new CPU semantics. Cached steps are not appended.
        session = self.active()
        transition = None
        for _ in range(count):
            before = session.view()
            if before.state.status is not TraceStatus.RUNNING:
                break
            transition = session.next()
            if session.view().history_size > len(self.events):
                self.events.append(transition)
        return transition


class _HeaderReader:
    def __init__(self, stream):
        self.stream = stream
        self.remaining = MAX_HEADER_BYTES

    def readline(self, limit=-1):
        size = self.remaining + 1 if limit < 0 else min(limit, self.remaining + 1)
        line = self.stream.readline(size)
        self.remaining -= len(line)
        if self.remaining < 0:
            raise LineTooLong("header block")
        return line


class TraceHandler(BaseHTTPRequestHandler):
    server_version = "ByteForge"
    sys_version = ""
    protocol_version = "HTTP/1.0"

    def log_message(self, format, *args):
        # No request URLs, source code, tokens or diagnostic text in access logs.
        pass

    def handle(self):
        self.deadline = time.monotonic() + self.server.request_timeout
        self.connection.settimeout(self.server.request_timeout)
        timer = threading.Timer(self.server.request_timeout, self._expire)
        timer.daemon = True
        timer.start()
        try:
            super().handle()
        except (OSError, TimeoutError):
            pass
        finally:
            timer.cancel()
            timer.join(timeout=0.1)

    def _expire(self):
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def parse_request(self):
        if len(self.raw_requestline) > 2048:
            self.requestline = ""
            self.request_version = "HTTP/1.0"
            self.command = None
            self.send_error(414)
            return False
        stream = self.rfile
        self.rfile = _HeaderReader(stream)
        try:
            return super().parse_request()
        finally:
            self.rfile = stream

    def _check_deadline(self):
        if time.monotonic() >= self.deadline:
            raise APIError(408, "request", "request_timeout", "Request deadline exceeded")

    def _respond(self, status, payload, *, binary=False):
        self.close_connection = True
        if binary:
            data = payload
        else:
            data = json.dumps({"schema_version": SCHEMA_VERSION, **payload},
                              ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream" if binary else "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        if binary:
            self.send_header("Content-Disposition", 'attachment; filename="byteforge.elf"')
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def send_error(self, code, message=None, explain=None):
        phrase = HTTPStatus(code).phrase
        self._respond(code, {"ok": False, "error": error_report("request", f"http_{code}", phrase)})

    def _security(self):
        if self.headers.get_all("Host", []) != [self.server.authority]:
            raise APIError(403, "security", "invalid_host", "Host must match the loopback server authority")
        origins = self.headers.get_all("Origin", [])
        if origins and origins != [self.server.origin]:
            raise APIError(403, "security", "invalid_origin", "Only the server's exact origin is allowed")
        if self.headers.get("Sec-Fetch-Site") in ("cross-site", "same-site"):
            raise APIError(403, "security", "cross_site", "Cross-origin requests are not allowed")
        if self.headers.get_all("Transfer-Encoding", []):
            raise APIError(400, "request", "unsupported_transfer", "Transfer-Encoding is not supported")
        if self.command == "POST":
            tokens = self.headers.get_all("X-ByteForge-Token", [])
            if (len(tokens) != 1 or not tokens[0].isascii()
                    or not secrets.compare_digest(tokens[0], self.server.mutation_token)):
                raise APIError(403, "security", "invalid_token", "A valid mutation token is required")

    def _json_body(self):
        lengths = self.headers.get_all("Content-Length", [])
        if not lengths:
            raise APIError(411, "request", "length_required", "Content-Length is required")
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,9}", lengths[0]):
            raise APIError(400, "request", "invalid_length", "Invalid Content-Length")
        length = int(lengths[0])
        if length > MAX_BODY_BYTES:
            raise APIError(413, "request", "body_too_large", "Request body exceeds the byte limit")
        types = self.headers.get_all("Content-Type", [])
        if len(types) != 1 or types[0].split(";", 1)[0].strip().lower() != "application/json":
            raise APIError(415, "request", "invalid_content_type", "Use application/json encoded as UTF-8")
        if self.headers.get("Content-Encoding", "identity") != "identity":
            raise APIError(415, "request", "unsupported_encoding", "Compressed requests are unsupported")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise APIError(400, "request", "incomplete_body", "Incomplete request body")
        self._check_deadline()
        try:
            body = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        except (ValueError, UnicodeError, RecursionError):
            raise APIError(400, "request", "invalid_json", "Expected a UTF-8 JSON object") from None
        if not isinstance(body, dict):
            raise APIError(400, "request", "invalid_json", "Expected a JSON object")
        return body

    def _query_context(self, query):
        self.server.active()
        if "session_id" not in query or "revision" not in query:
            raise APIError(400, "request", "missing_context", "Supply session_id and revision")
        self.server.check_context({"session_id": query["session_id"],
                                   "revision": self._query_int(query, "revision", 0, (1 << 53) - 1)})

    def _query_int(self, query, name, minimum, maximum, default=None):
        value = query.get(name)
        if value is None and default is not None:
            return default
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,20}", value):
            raise APIError(400, "request", "invalid_query", f"Invalid integer query field: {name}")
        return _integer(int(value), name, minimum, maximum)

    def _get(self, path, query):
        server = self.server
        if path in ("/api/bootstrap", "/api/examples", "/api/state"):
            _fields(query, ())
            if path == "/api/bootstrap":
                return {"mutation_token": server.mutation_token, "session_id": server.session_id,
                        "revision": server.revision, "limits": asdict(server.limits),
                        "http_limits": {"body_bytes": MAX_BODY_BYTES, "header_bytes": MAX_HEADER_BYTES,
                                        "request_seconds": server.request_timeout,
                                        "timeline_page": MAX_TIMELINE_PAGE},
                        "capabilities": {"instructions": ["mov", "add", "sub", "xor", "cmp", "jmp", "je", "jne", "syscall"],
                                         "syscalls": ["write_stdout", "exit"], "native_execution": False,
                                         "one_workspace": True}}
            if path == "/api/examples":
                return {"examples": server.examples}
            return server.current()
        if path not in ("/api/timeline", "/api/memory", "/api/elf"):
            raise APIError(404, "request", "not_found", "Unknown API route")
        extra = ("offset", "limit") if path == "/api/timeline" else ("address", "length") if path == "/api/memory" else ()
        _fields(query, ("session_id", "revision", *extra))
        self._query_context(query)
        session = server.active()
        if path == "/api/elf":
            return session.program.elf_image
        if path == "/api/timeline":
            offset = self._query_int(query, "offset", 0, server.limits.max_history + 1, 0)
            limit = self._query_int(query, "limit", 1, MAX_TIMELINE_PAGE, 50)
            cursor = session.view().cursor
            end = min(offset + limit, len(server.events))
            return {"session_id": server.session_id, "revision": server.revision, "cursor": cursor,
                    "total": len(server.events), "offset": offset,
                    "events": [{"index": index, "visible": index < cursor, "step": step_report(server.events[index])}
                               for index in range(offset, end)],
                    "next_offset": end if end < len(server.events) else None}
        address = query.get("address", "")
        if not re.fullmatch(r"0x[0-9a-fA-F]{1,16}", address):
            raise APIError(400, "memory", "invalid_address", "Supply a hexadecimal uint64 address string")
        address = int(address, 16)
        length = self._query_int(query, "length", 0, server.limits.max_memory_bytes, 16)
        try:
            data = read_memory(session.program, address, length)
        except ValueError as error:
            raise APIError(400, "memory", "invalid_memory", str(error)) from None
        return {"session_id": server.session_id, "revision": server.revision,
                "memory": memory_report(session.program, address, data)}

    def _post(self, path, body):
        server = self.server
        if path not in ("/api/assemble", "/api/trace"):
            raise APIError(404, "request", "not_found", "Unknown API route")
        if path == "/api/assemble":
            _fields(body, ("source", "filename", "session_id", "revision"), ("source", "session_id", "revision"))
        else:
            _fields(body, ("operation", "count", "session_id", "revision"), ("operation", "session_id", "revision"))
        server.check_context(body)
        if path == "/api/assemble":
            source, filename = body["source"], body.get("filename", "input.asm")
            if not isinstance(source, str):
                raise APIError(400, "request", "invalid_source", "Source must be a string")
            try:
                source.encode("utf-8")
            except UnicodeError:
                raise APIError(400, "request", "invalid_source", "Source must contain valid Unicode") from None
            if (not isinstance(filename, str) or len(filename) > 64 or ".." in filename
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.asm", filename)):
                raise APIError(400, "request", "invalid_filename", "Use a display basename ending in .asm (at most 64 ASCII characters)")
            try:
                program = prepare_trace(source, filename, server.limits)
            except ByteForgeError as error:
                raise APIError(400, "assembly", "assembly_error", error.message, error.location) from None
            report = assembly_report(program)
            self._check_deadline()
            server.session = TraceSession(program)
            server.session_id = secrets.token_urlsafe(24)
            server.revision += 1
            server.events.clear()
            return {**server.current(), "assembly": report}
        session = server.active()
        operation = body["operation"]
        if not isinstance(operation, str) or operation not in ("next", "back", "reset", "continue"):
            raise APIError(400, "trace", "invalid_operation", "Use next, back, reset or continue")
        if "count" in body and operation != "continue":
            raise APIError(400, "trace", "invalid_count", "Only continue accepts a count")
        count = _integer(body.get("count", server.limits.max_continue_steps), "count", 1,
                         server.limits.max_continue_steps)
        self._check_deadline()
        transition = None
        if operation in ("next", "continue"):
            transition = server.advance(1 if operation == "next" else count)
        elif operation == "back":
            session.back()
        else:
            session.reset()
            server.events.clear()
        server.revision += 1
        return {**server.current(), "transition": step_report(transition)}

    def _dispatch(self):
        try:
            self._security()
            self._check_deadline()
            parsed = urlsplit(self.path)
            if parsed.scheme or parsed.netloc or parsed.fragment:
                raise APIError(400, "request", "invalid_target", "Use an origin-relative API route")
            try:
                values = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=8)
            except ValueError:
                raise APIError(400, "request", "invalid_query", "Invalid query string") from None
            if any(len(items) != 1 for items in values.values()):
                raise APIError(400, "request", "invalid_query", "Duplicate query fields")
            query = {name: items[0] for name, items in values.items()}
            if parsed.path not in (*GET_ROUTES, *POST_ROUTES):
                raise APIError(404, "request", "not_found", "Unknown API route")
            if ((self.command == "GET" and parsed.path not in GET_ROUTES)
                    or (self.command == "POST" and parsed.path not in POST_ROUTES)):
                raise APIError(405, "request", "method_not_allowed", "Method is not supported for this route")
            if self.command == "GET":
                if self.headers.get_all("Content-Length", []) not in ([], ["0"]):
                    raise APIError(400, "request", "unexpected_body", "GET requests must not have a body")
                result = self._get(parsed.path, query)
            elif self.command == "POST":
                _fields(query, ())
                result = self._post(parsed.path, self._json_body())
            else:
                raise APIError(405, "request", "method_not_allowed", "Only GET and POST are supported")
            self._respond(200, result if isinstance(result, bytes) else {"ok": True, **result},
                          binary=isinstance(result, bytes))
        except APIError as error:
            self._respond(error.status, {"ok": False, "error": error.detail})
        except (OSError, TimeoutError):
            self.close_connection = True
        except Exception:
            self._respond(500, {"ok": False, "error": error_report("server", "internal_error", "Internal server error")})

    do_GET = _dispatch
    do_POST = _dispatch
    do_HEAD = _dispatch
    do_OPTIONS = _dispatch
    do_PUT = _dispatch
    do_PATCH = _dispatch
    do_DELETE = _dispatch
    do_TRACE = _dispatch
    do_CONNECT = _dispatch


def main(argv=None):
    parser = argparse.ArgumentParser(description="ByteForge TraceLab local API (no frontend yet)")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    try:
        with TraceHTTPServer(args.port) as server:
            print(f"ByteForge TraceLab API: {server.origin} (SIMULATED execution only)")
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
    except (OSError, ValueError, APIError) as error:
        print(f"TraceLab API startup failed: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
