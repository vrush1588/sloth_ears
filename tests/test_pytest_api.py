import http.client
import json
import os
import stat
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import pytest

from sloth_ears import core, server


@pytest.fixture
def fake_ollama():
    calls = []

    class OllamaHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers["Content-Length"])
            calls.append(json.loads(self.rfile.read(length)))
            call_number = len(calls)
            output = {
                "summary": f"summary {call_number}",
                "actions": ["repeat", f"action {call_number}"],
            }
            response = json.dumps({"response": json.dumps(output)}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, format, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), OllamaHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", calls
    finally:
        httpd.shutdown()
        thread.join(timeout=2)
        httpd.server_close()


@pytest.mark.parametrize("host", ["http://localhost:11434", "http://127.0.0.1:11434"])
def test_require_local_accepts_loopback_hosts(host):
    assert core.require_local(host) is None


@pytest.mark.parametrize(
    "host",
    ["https://example.com", "http://192.168.1.20:11434", "http://localhost.example"],
)
def test_require_local_rejects_non_local_hosts(host):
    with pytest.raises(SystemExit, match="Refusing to run"):
        core.require_local(host)


def test_local_http_helper_refuses_external_target():
    request = urllib.request.Request("https://example.com/")

    with pytest.raises(SystemExit, match="Refusing to run"):
        core.open_local_url(request, timeout=1)


def test_local_http_helper_rejects_non_http_protocol():
    request = urllib.request.Request("ftp://localhost/model")

    with pytest.raises(ValueError, match="Only local HTTP"):
        core.open_local_url(request, timeout=1)


def test_find_media_filters_time_window_and_supported_audio_video(tmp_path):
    now = time.time()
    expected_recent = [
        tmp_path / "recent.wav",
        tmp_path / "recent.mp4",
        tmp_path / "recent.MKV",
    ]
    for path in expected_recent:
        path.write_bytes(b"media")
        os.utime(path, (now, now))

    old_file = tmp_path / "old.mp3"
    old_file.write_bytes(b"media")
    old_time = now - 48 * 60 * 60
    os.utime(old_file, (old_time, old_time))
    unsupported = tmp_path / "recent.txt"
    unsupported.write_text("not media", encoding="utf-8")
    os.utime(unsupported, (now, now))

    assert set(core.find_media(tmp_path, 24)) == {
        path.resolve() for path in expected_recent
    }
    assert set(core.find_media(tmp_path, 72)) == {
        *(path.resolve() for path in expected_recent),
        old_file.resolve(),
    }


def test_summarize_uses_one_model_call_for_short_text(fake_ollama):
    host, calls = fake_ollama

    summary, actions, points = core.summarize("A short recording.", host, "fake-model")

    assert summary == "summary 1"
    assert actions == ["repeat", "action 1"]
    assert points == []
    assert len(calls) == 1


def test_summarize_chunks_long_text_and_deduplicates_actions(fake_ollama):
    host, calls = fake_ollama

    summary, actions, _ = core.summarize("x" * (core.CHUNK * 2 + 1), host, "fake-model")

    assert summary == "summary 4"
    assert len(calls) == 4
    assert actions == ["repeat", "action 1", "action 2", "action 3"]


def test_summarize_withholds_verbatim_transcript_echo(fake_ollama):
    host, _ = fake_ollama
    transcript = "The private recording phrase should not be copied into any digest."
    with patch.object(
        core,
        "_ask",
        return_value=core.Digest(
            transcript, ["The private recording phrase"], []
        ),
    ):
        summary, actions, _ = core.summarize(transcript, host, "fake-model")

    assert "private recording phrase" not in summary
    assert actions == []


def test_digest_file_is_private(tmp_path):
    digest_file = tmp_path / "digest.md"
    core._write_private_file(digest_file, "summary only\n")

    assert digest_file.read_text(encoding="utf-8") == "summary only\n"
    if os.name != "nt":
        assert stat.S_IMODE(digest_file.stat().st_mode) == 0o600


def test_run_digest_disables_model_downloads_even_without_offline_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")

    assert core.run_digest(tmp_path, 24, None, "small", "fake-model", False, None) == []
    assert os.environ["HF_HUB_OFFLINE"] == "1"


def _request(httpd, method, path, body=None):
    connection = http.client.HTTPConnection(*httpd.server_address, timeout=3)
    payload = None if body is None else json.dumps(body)
    headers = {"Content-Type": "application/json"} if payload is not None else {}
    connection.request(method, path, body=payload, headers=headers)
    response = connection.getresponse()
    result = response.read()
    connection.close()
    return response.status, result


def test_server_is_loopback_validates_folder_and_rejects_parallel_runs(tmp_path, monkeypatch):
    httpd = server.create_server(port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    entered = threading.Event()
    release = threading.Event()

    def blocked_digest(**kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return []

    monkeypatch.setattr(server.core, "run_digest", blocked_digest)
    options = {
        "folder": str(tmp_path),
        "since_hours": 24,
        "language": None,
        "whisper_model": "small",
        "ai_model": "fake-model",
        "offline": True,
    }
    try:
        assert httpd.server_address[0] == "127.0.0.1"

        with pytest.raises(ValueError, match="must bind to 127.0.0.1"):
            server.create_server("0.0.0.0", 0)

        status, body = _request(
            httpd, "POST", "/api/run", {"folder": str(tmp_path / "missing")}
        )
        assert status == 400
        assert b"must exist and be a directory" in body

        status, _ = _request(httpd, "POST", "/api/run", options)
        assert status == 202
        assert entered.wait(timeout=3)

        status, body = _request(httpd, "POST", "/api/run", options)
        assert status == 409
        assert b"already active" in body
    finally:
        release.set()
        deadline = time.monotonic() + 3
        while httpd.state.snapshot()["state"] == "running" and time.monotonic() < deadline:
            time.sleep(0.01)
        httpd.shutdown()
        thread.join(timeout=2)
        httpd.server_close()

    assert httpd.state.snapshot()["state"] == "done"


def test_wipe_deletes_markdown_and_json_digests(tmp_path, monkeypatch):
    markdown = tmp_path / "digest.md"
    structured = tmp_path / "digest.json"
    markdown.write_text("digest", encoding="utf-8")
    structured.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(core, "DIGEST", markdown)
    monkeypatch.setattr(core, "DIGEST_JSON", structured)

    core.wipe_digests()

    assert not markdown.exists()
    assert not structured.exists()
