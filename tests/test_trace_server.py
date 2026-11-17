"""Real temporary loopback HTTP servers; no native executable execution."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import http.client
import io
import json
from pathlib import Path
import socket
import threading
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from urllib.parse import urlencode

from byteforge.trace import prepare_trace
from byteforge.trace_model import TraceLimits
from byteforge.trace_server import EXAMPLE_NAMES, MAX_BODY_BYTES, TraceHTTPServer, main


ROOT = Path(__file__).resolve().parents[1]
EXIT = "_start: mov rax, 60\nmov rdi, 7\nsyscall"


class TraceServerTests(unittest.TestCase):
    def setUp(self):
        self.server = TraceHTTPServer(0)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.assertFalse(self.thread.is_alive())

    def request(self, method, path, body=None, headers=None, raw=None):
        request_headers = {"Origin": self.server.origin}
        if method == "POST":
            request_headers.update({"X-ByteForge-Token": self.server.mutation_token,
                                    "Content-Type": "application/json"})
        for name, value in (headers or {}).items():
            if value is None:
                request_headers.pop(name, None)
            else:
                request_headers[name] = value
        payload = raw if raw is not None else json.dumps(body).encode("utf-8") if body is not None else None
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request(method, path, payload, request_headers)
            response = connection.getresponse()
            data = response.read()
            response_headers = dict(response.getheaders())
            if data and response_headers.get("Content-Type", "").startswith("application/json"):
                data = json.loads(data)
            return response.status, data, response_headers
        finally:
            connection.close()

    def context(self):
        status, data, _ = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        return {name: data[name] for name in ("session_id", "revision")}

    def assemble(self, source=EXIT, *, context=None, filename="input.asm"):
        return self.request("POST", "/api/assemble", {**(context or self.context()),
                            "source": source, "filename": filename})

    def trace(self, operation, count=None, context=None):
        body = {**(context or self.context()), "operation": operation}
        if count is not None:
            body["count"] = count
        return self.request("POST", "/api/trace", body)

    def query(self, route, **extra):
        return route + "?" + urlencode({**self.context(), **extra})

    def example_source(self, name):
        return (ROOT / "examples" / (name + ".asm")).read_text(encoding="utf-8")

    def raw_request(self, request):
        with socket.create_connection(self.server.server_address, timeout=3) as connection:
            connection.sendall(request)
            result = bytearray()
            while True:
                chunk = connection.recv(65536)
                if not chunk:
                    break
                result.extend(chunk)
        return bytes(result)

    def test_bootstrap_and_unassembled_state(self):
        status, data, _ = self.request("GET", "/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertEqual(data["schema_version"], 1)
        self.assertGreaterEqual(len(data["mutation_token"]), 32)
        self.assertEqual(data["limits"]["max_continue_steps"], 100)
        self.assertFalse(data["capabilities"]["native_execution"])
        self.assertIsNone(data["session_id"])
        self.assertEqual(data["revision"], 0)
        state = self.request("GET", "/api/state")[1]
        self.assertEqual(state["view"]["execution_status"], "not_assembled")

    def test_bind_is_exclusively_ipv4_loopback(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        self.assertEqual(self.server.address_family, socket.AF_INET)

    def test_examples_are_allowlisted_and_preloaded(self):
        with patch("pathlib.Path.open") as opened:
            status, data, _ = self.request("GET", "/api/examples")
        opened.assert_not_called()
        self.assertEqual(status, 200)
        self.assertEqual([item["name"] for item in data["examples"]], list(EXAMPLE_NAMES))
        for item in data["examples"]:
            self.assertEqual(item["filename"], item["name"] + ".asm")
            self.assertEqual(item["source"], self.example_source(item["name"]))

    def test_assemble_exact_code_symbols_and_elf_metadata(self):
        status, data, _ = self.assemble()
        self.assertEqual(status, 200)
        self.assertEqual(data["view"]["execution_status"], "ready")
        self.assertEqual(data["revision"], 1)
        self.assertTrue(data["session_id"])
        assembly = data["assembly"]
        self.assertEqual(assembly["elf"]["file_size"], 4118)
        self.assertEqual(assembly["elf"]["entry"], "0x0000000000401000")
        self.assertEqual(assembly["elf"]["header"]["e_machine"], 62)
        self.assertEqual(assembly["instructions"][0]["bytes_hex"], "48 B8 3C 00 00 00 00 00 00 00")
        self.assertEqual(assembly["instructions"][0]["file_offset"], 4096)
        self.assertEqual(assembly["symbols"][0]["name"], "_start")

    def test_source_position_diagnostics_and_failed_assembly_preserve_workspace(self):
        self.assemble()
        previous = self.context()
        status, data, _ = self.assemble("; original\n_start: nop", filename="bad.asm")
        self.assertEqual(status, 400)
        self.assertEqual(data["error"]["phase"], "assembly")
        self.assertEqual(data["error"]["location"], {"filename": "bad.asm", "line": 2,
                         "column": 9, "text": "_start: nop"})
        self.assertEqual(self.context(), previous)

    def test_uint64_values_are_strings_through_http(self):
        self.assemble("_start: mov rax, 18446744073709551615\nsyscall")
        status, data, _ = self.trace("next")
        self.assertEqual(status, 200)
        self.assertEqual(data["view"]["cpu"]["registers"]["rax"], "0xffffffffffffffff")
        self.assertEqual(data["view"]["cpu"]["step_count"], "0x0000000000000001")

    def test_next_back_cached_forward_reset(self):
        self.assemble()
        first = self.trace("next")[1]
        self.assertEqual(first["view"]["cpu"]["registers"]["rax"], "0x000000000000003c")
        self.assertEqual(first["transition"]["changed_registers"], ["rax"])
        back = self.trace("back")[1]
        self.assertEqual(back["view"]["cursor"], 0)
        self.assertEqual(back["view"]["history_size"], 1)
        forward = self.trace("next")[1]
        self.assertEqual(forward["view"], first["view"])
        reset = self.trace("reset")[1]
        self.assertEqual(reset["view"]["execution_status"], "ready")
        self.assertEqual(reset["view"]["history_size"], 0)
        self.assertEqual(self.request("GET", self.query("/api/timeline"))[1]["total"], 0)

    def test_hello_continue_output_exit_and_memory(self):
        self.assemble(self.example_source("hello"))
        status, memory, _ = self.request("GET", self.query("/api/memory", address="0x401040", length=14))
        self.assertEqual(status, 200)
        self.assertEqual(memory["memory"]["file_offset"], 0x1040)
        self.assertEqual(bytes.fromhex(memory["memory"]["data"]["hex"]), b"Hello, World!\n")
        status, data, _ = self.trace("continue")
        self.assertEqual(status, 200)
        self.assertEqual(data["view"]["execution_status"], "halted_successfully")
        self.assertEqual(data["view"]["cpu"]["exit_status"], 0)
        self.assertEqual(bytes.fromhex(data["view"]["stdout"]["hex"]), b"Hello, World!\n")

    def test_back_over_write_and_exit_restores_output_and_halt(self):
        self.assemble(self.example_source("hello"))
        written = self.trace("continue", 5)[1]
        self.assertEqual(written["view"]["stdout"]["length"], 14)
        self.assertEqual(self.trace("back")[1]["view"]["stdout"]["length"], 0)
        self.assertEqual(self.trace("next")[1]["view"], written["view"])
        exited = self.trace("continue")[1]
        back = self.trace("back")[1]
        self.assertEqual(back["view"]["cpu"]["status"], "running")
        self.assertIsNone(back["view"]["cpu"]["exit_status"])
        self.assertEqual(self.trace("next")[1]["view"], exited["view"])
        self.assertEqual(self.trace("next")[1]["view"], exited["view"])

    def test_timeline_pages_preserve_cached_future_and_branch_metadata(self):
        self.assemble(self.example_source("loop"))
        self.trace("continue", 4)
        self.trace("back")
        status, page, _ = self.request("GET", self.query("/api/timeline", offset=2, limit=2))
        self.assertEqual(status, 200)
        self.assertEqual((page["total"], page["cursor"]), (4, 3))
        self.assertTrue(page["events"][0]["visible"])
        self.assertFalse(page["events"][1]["visible"])
        branch = page["events"][1]["step"]
        self.assertTrue(branch["branch_taken"])
        self.assertEqual(branch["branch_target"], "0x000000000040100a")
        self.assertEqual(branch["bytes_hex"], "0F 85 EC FF FF FF")
        self.trace("next")
        self.assertEqual(self.request("GET", self.query("/api/timeline"))[1]["total"], 4)

    def test_timeline_pagination_is_bounded(self):
        self.assemble("_start: jmp _start")
        self.trace("continue", 100)
        status, page, _ = self.request("GET", self.query("/api/timeline", limit=3))
        self.assertEqual(status, 200)
        self.assertEqual(len(page["events"]), 3)
        self.assertEqual(page["next_offset"], 3)
        self.assertEqual(self.request("GET", self.query("/api/timeline", limit=101))[0], 400)

    def test_stale_revision_and_missing_context_do_not_mutate(self):
        assembled = self.assemble()[1]
        old = {name: assembled[name] for name in ("session_id", "revision")}
        self.trace("next")
        self.assertEqual(self.trace("next", context=old)[0], 409)
        self.assertEqual(self.request("POST", "/api/trace", {"operation": "next"})[0], 400)
        self.assertEqual(self.request("GET", "/api/state")[1]["view"]["cursor"], 1)

    def test_new_assembly_replaces_id_and_rejects_stale_read_and_mutation(self):
        self.assemble()
        old = self.context()
        self.assemble(self.example_source("hello"))
        current = self.context()
        self.assertNotEqual(old["session_id"], current["session_id"])
        self.assertGreater(current["revision"], old["revision"])
        self.assertEqual(self.trace("next", context=old)[0], 409)
        self.assertEqual(self.assemble(context=old)[0], 409)
        self.assertEqual(self.request("GET", "/api/elf?" + urlencode(old))[0], 409)
        self.assertEqual(self.request("GET", "/api/timeline?" + urlencode(old))[0], 409)

    def test_simultaneous_mutations_are_serialized(self):
        self.assemble()
        context = self.context()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.trace("next", context=context)[0], range(2)))
        self.assertEqual(sorted(results), [200, 409])
        self.assertEqual(self.request("GET", "/api/state")[1]["view"]["cursor"], 1)

    def test_step_and_continue_limits(self):
        self.server.limits = replace(self.server.limits, max_steps=3)
        self.assemble("_start: jmp _start")
        self.assertEqual(self.trace("continue", 101)[0], 400)
        self.assertEqual(self.trace("continue", 0)[0], 400)
        self.assertEqual(self.trace("continue", True)[0], 400)
        data = self.trace("continue")[1]
        self.assertEqual(data["view"]["execution_status"], "step_limit_reached")
        self.assertEqual(data["view"]["cpu"]["step_count"], "0x0000000000000003")
        self.assertEqual(data["view"]["history_size"], 4)

    def test_output_and_history_limits_are_not_bypassed(self):
        self.server.limits = replace(self.server.limits, max_output_bytes=2)
        self.assemble(self.example_source("hello"))
        data = self.trace("continue")[1]
        self.assertEqual(data["view"]["cpu"]["halt_reason"], "output_limit")
        self.assertEqual(data["view"]["stdout"]["length"], 0)
        self.server.limits = replace(self.server.limits, max_history=2)
        self.assemble("_start: jmp _start")
        data = self.trace("continue")[1]
        self.assertEqual(data["view"]["cpu"]["halt_reason"], "history_limit")
        self.assertEqual(data["view"]["history_size"], 3)

    def test_source_and_instruction_limits(self):
        self.server.limits = replace(self.server.limits, max_source_bytes=20, max_instructions=1)
        status, data, _ = self.assemble("_start: syscall ;" + "A" * 20)
        self.assertEqual(status, 400)
        self.assertIn("UTF-8 bytes", data["error"]["message"])
        self.server.limits = replace(self.server.limits, max_source_bytes=65536)
        status, data, _ = self.assemble(EXIT)
        self.assertEqual(status, 400)
        self.assertIn("instructions", data["error"]["message"])

    def test_runtime_unsupported_and_faulted_statuses_are_explicit(self):
        self.assemble("_start: syscall")
        data = self.trace("next")[1]
        self.assertEqual(data["view"]["execution_status"], "unsupported_operation")
        self.assertEqual(data["transition"]["fault"]["location"]["column"], 9)
        self.assemble("_start: mov rax, 1")
        self.assertEqual(self.trace("next")[1]["view"]["execution_status"], "faulted")

    def test_invalid_memory_requests(self):
        self.assemble(self.example_source("hello"))
        for fields in ({"address": "0x401000", "length": 1}, {"address": "0x401040", "length": 257},
                       {"address": "0x40104e", "length": 1}, {"address": "0xffffffffffffffff", "length": 1},
                       {"address": "18446744073709551615", "length": 0}, {"address": "-1", "length": 0}):
            with self.subTest(fields=fields):
                self.assertEqual(self.request("GET", self.query("/api/memory", **fields))[0], 400)
        status, data, _ = self.request("GET", self.query("/api/memory", address="0x40104e", length=0))
        self.assertEqual(status, 200)
        self.assertEqual(data["memory"]["data"]["length"], 0)

    def test_elf_download_is_exact_and_never_executed(self):
        self.assemble(self.example_source("hello"))
        status, data, headers = self.request("GET", self.query("/api/elf"))
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/octet-stream")
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertEqual(data, prepare_trace(self.example_source("hello"), "input.asm", TraceLimits()).elf_image)
        self.assertEqual(len(data), 4174)

    def test_known_examples_deterministic_and_unchanged(self):
        for name in EXAMPLE_NAMES:
            with self.subTest(name=name):
                source = self.example_source(name)
                first = self.assemble(source)[1]["assembly"]
                image = self.request("GET", self.query("/api/elf"))[1]
                original = prepare_trace(source, "input.asm", TraceLimits())
                self.assertEqual(image, original.elf_image)
                if (ROOT / "build" / name).exists():
                    self.assertEqual(image, (ROOT / "build" / name).read_bytes())
                self.assertEqual(self.assemble(source)[1]["assembly"], first)
                view = self.trace("continue")[1]["view"]
                self.assertEqual(view["cpu"]["exit_status"], 0 if name == "hello" else 7)

    def test_no_subprocess_native_execution_or_request_file_io(self):
        source = self.example_source("hello")
        with patch("subprocess.Popen") as process, patch("os.system") as system, \
                patch("pathlib.Path.open") as opened, patch("builtins.open") as builtin_open:
            self.assertEqual(self.assemble(source)[0], 200)
            self.assertEqual(self.trace("continue")[0], 200)
            self.assertEqual(self.request("GET", self.query("/api/elf"))[0], 200)
            self.assertEqual(self.request("GET", "/api/examples")[0], 200)
        for mocked in (process, system, opened, builtin_open):
            mocked.assert_not_called()

    def test_safe_filename_and_no_filesystem_traversal(self):
        for filename in ("../secret.asm", "C:\\secret.asm", "/etc/passwd", ".hidden.asm", "a..asm", "x\n.asm", "x" * 65 + ".asm"):
            with self.subTest(filename=filename):
                self.assertEqual(self.assemble(filename=filename)[0], 400)
        for path in ("/AGENTS.md", "/../README.md", "/%2e%2e/README.md", "/api/examples/../../README.md"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 404)
        self.assertEqual(self.request("GET", "/api/examples?path=../../README.md")[0], 400)

    def test_invalid_json_utf8_and_shape(self):
        for raw in (b"{", b"[]", b"null", b"\xff", b'{"source":NaN}',
                    b'{"source":"a","source":"b"}', b"[" * 2000 + b"]" * 2000):
            with self.subTest(raw=raw[:30]):
                status, data, _ = self.request("POST", "/api/assemble", raw=raw)
                self.assertEqual(status, 400)
                self.assertEqual(data["error"]["code"], "invalid_json")

    def test_surrogate_source_and_wrong_field_types(self):
        for source in (None, 123, "\ud800"):
            with self.subTest(source=repr(source)):
                status, data, _ = self.assemble(source)
                self.assertEqual(status, 400)
                self.assertEqual(data["error"]["code"], "invalid_source")
        self.assertEqual(self.request("POST", "/api/assemble", {"source": EXIT, "revision": True, "session_id": None})[0], 400)

    def test_oversized_request_rejected_before_body_read(self):
        status, data, _ = self.request("POST", "/api/assemble", raw=b"", headers={"Content-Length": str(MAX_BODY_BYTES + 1)})
        self.assertEqual(status, 413)
        self.assertEqual(data["error"]["code"], "body_too_large")

    def test_invalid_content_type_and_transfer_encoding(self):
        self.assertEqual(self.request("POST", "/api/assemble", {}, headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("POST", "/api/assemble", {}, headers={"Content-Encoding": "gzip"})[0], 415)
        self.assertEqual(self.request("POST", "/api/assemble", {}, headers={"Transfer-Encoding": "chunked"})[0], 400)

    def test_host_and_origin_enforcement(self):
        for headers in ({"Host": "evil.example"}, {"Host": "localhost:" + str(self.server.server_port)},
                        {"Origin": "http://evil.example"}, {"Origin": "null"},
                        {"Origin": "http://127.0.0.1:1"}, {"Sec-Fetch-Site": "cross-site"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request("GET", "/api/bootstrap", headers=headers)[0], 403)
        self.assertEqual(self.request("GET", "/api/bootstrap", headers={"Origin": None})[0], 200)

    def test_missing_wrong_and_non_ascii_tokens(self):
        for token in (None, "bad", "é"):
            with self.subTest(token=token):
                status, data, _ = self.request("POST", "/api/assemble", {}, headers={"X-ByteForge-Token": token})
                self.assertEqual(status, 403)
                self.assertEqual(data["error"]["code"], "invalid_token")
        self.assertIsNone(self.context()["session_id"])

    def test_duplicate_security_and_length_headers_are_rejected(self):
        authority = self.server.authority
        token = self.server.mutation_token
        for header, status in ((f"Host: {authority}\r\nHost: {authority}", 403),
                               (f"Host: {authority}\r\nOrigin: {self.server.origin}\r\nOrigin: {self.server.origin}", 403),
                               (f"Host: {authority}\r\nX-ByteForge-Token: {token}", 403),
                               (f"Host: {authority}\r\nContent-Length: 2\r\nContent-Length: 2", 400)):
            raw = (f"POST /api/assemble HTTP/1.0\r\n{header}\r\nX-ByteForge-Token: {token}\r\n"
                   "Content-Type: application/json\r\n\r\n{}").encode("ascii")
            response = self.raw_request(raw)
            self.assertIn(f" {status} ".encode("ascii"), response.split(b"\r\n", 1)[0])

    def test_missing_and_invalid_content_length(self):
        base = (f"POST /api/assemble HTTP/1.0\r\nHost: {self.server.authority}\r\n"
                f"X-ByteForge-Token: {self.server.mutation_token}\r\nContent-Type: application/json\r\n")
        for length, expected in ((None, 411), ("-1", 400), ("abc", 400)):
            suffix = "" if length is None else f"Content-Length: {length}\r\n"
            response = self.raw_request((base + suffix + "\r\n").encode("ascii"))
            self.assertIn(f" {expected} ".encode("ascii"), response.split(b"\r\n", 1)[0])

    def test_header_and_request_line_sizes_are_bounded(self):
        authority = self.server.authority
        response = self.raw_request((f"GET /api/state HTTP/1.0\r\nHost: {authority}\r\nX-Large: " + "A" * 17000 + "\r\n\r\n").encode("ascii"))
        self.assertIn(b" 431 ", response.split(b"\r\n", 1)[0])
        response = self.raw_request(("GET /" + "A" * 2100 + f" HTTP/1.0\r\nHost: {authority}\r\n\r\n").encode("ascii"))
        self.assertIn(b" 414 ", response.split(b"\r\n", 1)[0])

    def test_slow_body_connection_has_absolute_deadline(self):
        self.server.request_timeout = 0.15
        start = time.monotonic()
        raw = (f"POST /api/assemble HTTP/1.0\r\nHost: {self.server.authority}\r\n"
               f"X-ByteForge-Token: {self.server.mutation_token}\r\nContent-Type: application/json\r\n"
               "Content-Length: 20\r\n\r\n{").encode("ascii")
        self.raw_request(raw)
        self.assertLess(time.monotonic() - start, 2)
        self.assertEqual(self.request("GET", "/api/state")[0], 200)

    def test_methods_routes_and_queries_are_rejected_safely(self):
        for method in ("PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"):
            with self.subTest(method=method):
                status, data, _ = self.request(method, "/api/state")
                self.assertEqual(status, 405)
                self.assertEqual(data["error"]["code"], "method_not_allowed")
        self.assertEqual(self.request("HEAD", "/api/state")[0], 405)
        self.assertEqual(self.request("BREW", "/api/state")[0], 501)
        self.assertEqual(self.request("GET", "/api/assemble")[0], 405)
        self.assertEqual(self.request("POST", "/api/state", {})[0], 405)
        self.assertEqual(self.request("GET", "/api/missing")[0], 404)
        self.assertEqual(self.request("POST", "/api/missing", {})[0], 404)
        self.assertEqual(self.request("GET", "/api/state?a=1&a=2")[0], 400)
        self.assertEqual(self.request("GET", "http://evil.example/api/state",
                                      headers={"Host": self.server.authority})[0], 400)

    def test_security_headers_and_no_permissive_cors(self):
        for path in ("/api/bootstrap", "/api/missing"):
            _, _, headers = self.request("GET", path)
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(headers["X-Frame-Options"], "DENY")
            self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
            self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_internal_errors_do_not_disclose_paths_or_replace_workspace(self):
        self.assemble()
        previous = self.context()
        with patch("byteforge.trace_server.prepare_trace", side_effect=RuntimeError("private/path/source.asm")):
            status, data, _ = self.assemble()
        self.assertEqual(status, 500)
        self.assertEqual(data["error"]["phase"], "server")
        self.assertNotIn("private", json.dumps(data))
        self.assertEqual(self.context(), previous)

    def test_empty_workspace_and_invalid_trace_operations(self):
        self.assertEqual(self.trace("next")[0], 409)
        self.assertEqual(self.request("GET", "/api/timeline")[0], 409)
        self.assertEqual(self.request("GET", "/api/elf")[0], 409)
        self.assemble()
        self.assertEqual(self.trace("execute_native")[0], 400)
        self.assertEqual(self.trace("back", 1)[0], 400)

    def test_startup_command_binds_loopback_and_closes_on_interrupt(self):
        output = io.StringIO()
        with patch.object(TraceHTTPServer, "serve_forever", side_effect=KeyboardInterrupt), redirect_stdout(output):
            self.assertEqual(main(["--port", "0"]), 0)
        self.assertIn("http://127.0.0.1:", output.getvalue())
        self.assertIn("SIMULATED execution only", output.getvalue())

    def test_frontend_static_routes_have_exact_content_and_mime(self):
        for route, filename, mime in (("/", "index.html", "text/html"),
                                     ("/index.html", "index.html", "text/html"),
                                     ("/styles.css", "styles.css", "text/css"),
                                     ("/app.js", "app.js", "text/javascript"),
                                     ("/panels.js", "panels.js", "text/javascript")):
            with self.subTest(route=route):
                status, data, headers = self.request("GET", route)
                self.assertEqual(status, 200)
                self.assertEqual(data, (ROOT / "visualizer" / filename).read_bytes())
                self.assertTrue(headers["Content-Type"].startswith(mime))
                self.assertNotIn("Content-Disposition", headers)
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
                self.assertNotIn("unsafe-inline", headers["Content-Security-Policy"])

    def test_static_assets_are_preloaded_and_guarded(self):
        with patch("pathlib.Path.open") as opened, patch("builtins.open") as files:
            self.assertEqual(self.request("GET", "/")[0], 200)
            self.assertEqual(self.request("GET", "/app.js")[0], 200)
        opened.assert_not_called()
        files.assert_not_called()
        self.assertEqual(self.request("GET", "/", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.request("GET", "/app.js", headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.request("POST", "/app.js", {})[0], 405)
        self.assertEqual(self.request("POST", "/app.js", {}, headers={"X-ByteForge-Token": None})[0], 403)

    def test_static_allowlist_rejects_traversal_directories_and_extra_paths(self):
        for route in ("/visualizer/", "/visualizer/index.html", "/README.md", "/trace_server.py",
                      "/../AGENTS.md", "/%2e%2e/AGENTS.md", "/app.js/../README.md", "/favicon.ico"):
            with self.subTest(route=route):
                self.assertEqual(self.request("GET", route)[0], 404)
        self.assertEqual(self.request("GET", "/app.js?path=../AGENTS.md")[0], 400)

    def test_frontend_uses_local_modules_and_safe_rendering(self):
        from html.parser import HTMLParser
        class Assets(HTMLParser):
            def __init__(self):
                super().__init__()
                self.sources = []
                self.handlers = []
            def handle_starttag(self, tag, attrs):
                for name, value in attrs:
                    if name in ("src", "href"):
                        self.sources.append(value)
                    if name.startswith("on"):
                        self.handlers.append(name)
        parser = Assets()
        parser.feed((ROOT / "visualizer" / "index.html").read_text(encoding="utf-8"))
        self.assertTrue(all(value.startswith(("/", "#")) for value in parser.sources))
        self.assertFalse(parser.handlers)
        for name in ("app.js", "panels.js"):
            script = (ROOT / "visualizer" / name).read_text(encoding="utf-8")
            self.assertNotIn(".innerHTML", script)
            self.assertNotIn("insertAdjacentHTML", script)


if __name__ == "__main__":
    unittest.main()
