import http.client
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from sloth_gist import server


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.web_root = self.root / "web"
        self.web_root.mkdir()
        self.web_patch = patch.object(server, "WEB_ROOT", self.web_root)
        self.web_patch.start()
        self.httpd = server.create_server(port=0)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.thread.join(timeout=2)
        self.httpd.server_close()
        self.web_patch.stop()
        self.temporary.cleanup()

    def request(self, method, path, body=None):
        connection = http.client.HTTPConnection(*self.httpd.server_address, timeout=2)
        payload = None if body is None else json.dumps(body)
        headers = {"Content-Type": "application/json"} if payload else {}
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        result = response.read()
        connection.close()
        return response.status, result

    def test_server_refuses_non_loopback_bind_address(self):
        with self.assertRaises(ValueError):
            server.create_server("0.0.0.0", 0)

    def test_serves_static_files_and_rejects_traversal(self):
        (self.web_root / "index.html").write_text("frontend", encoding="utf-8")
        (self.web_root / "style.css").write_text("body { color: green; }", encoding="utf-8")
        (self.web_root / "app.js").write_text("const ready = true;", encoding="utf-8")
        (self.root / "secret.txt").write_text("secret", encoding="utf-8")

        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"frontend")

        status, body = self.request("GET", "/..%2Fsecret.txt")
        self.assertEqual(status, 404)
        self.assertNotIn(b"secret", body)

        for asset, expected in (("/style.css", b"color: green"), ("/app.js", b"ready")):
            status, body = self.request("GET", asset)
            self.assertEqual(status, 200)
            self.assertIn(expected, body)

    def test_run_validates_folder_and_allows_only_one_active_worker(self):
        status, body = self.request("POST", "/api/run", {"folder": str(self.root / "missing")})
        self.assertEqual(status, 400)
        self.assertIn(b"existing folder or audio/video file", body)

        entered = threading.Event()
        release = threading.Event()

        def blocked_run(**kwargs):
            entered.set()
            release.wait(timeout=2)
            return []

        options = {
            "folder": str(self.root),
            "since_hours": 24,
            "language": None,
            "whisper_model": "small",
            "ai_model": "llama3.2:3b",
            "offline": False,
        }
        with patch.object(server.core, "run_digest", side_effect=blocked_run):
            status, _ = self.request("POST", "/api/run", options)
            self.assertEqual(status, 202)
            self.assertTrue(entered.wait(timeout=2))

            status, _ = self.request("POST", "/api/run", options)
            self.assertEqual(status, 409)
            release.set()

            deadline = time.monotonic() + 2
            while self.httpd.state.snapshot()["state"] == "running" and time.monotonic() < deadline:
                time.sleep(0.01)

        self.assertEqual(self.httpd.state.snapshot()["state"], "done")

    def test_status_and_digest_endpoints(self):
        status, body = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body),
            {
                "state": "idle",
                "mode": "media",
                "done": 0,
                "total": 0,
                "current_file": "",
                "stage": "",
                "error": None,
                "storage_path": str(server.core.DIGEST_DIR),
            },
        )

        missing_digest = self.root / "missing.json"
        with patch.object(server.core, "DIGEST_JSON", missing_digest):
            status, body = self.request("GET", "/api/digest")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), [])

        digest = self.root / "digest.json"
        digest.write_text('[{"summary":"ready"}]', encoding="utf-8")
        with patch.object(server.core, "DIGEST_JSON", digest), patch.object(
            server.core, "DIGEST", self.root / "digest.md"
        ):
            status, body = self.request("GET", "/api/digest")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), [{"summary": "ready"}])

            status, body = self.request("DELETE", "/api/digest")
            self.assertEqual(status, 200)
            self.assertFalse(digest.exists())

    def test_models_endpoint_queries_only_local_ollama(self):
        with patch.object(
            server.core,
            "open_local_url",
            return_value=io.BytesIO(b'{"models":[{"name":"small"}]}'),
        ) as open_url:
            status, body = self.request("GET", "/api/models")

        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body), {"models": [{"name": "small", "recommended": False}]}
        )
        self.assertEqual(open_url.call_args.args[0].full_url, server.OLLAMA_TAGS_URL)

    def test_run_rejects_overflowing_since_hours(self):
        status, body = self.request(
            "POST",
            "/api/run",
            {"folder": str(self.root), "since_hours": 10**400},
        )
        self.assertEqual(status, 400)
        self.assertIn(b"finite non-negative", body)


if __name__ == "__main__":
    unittest.main()
