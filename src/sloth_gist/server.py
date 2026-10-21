"""Loopback-only web server for the Sloth Gist frontend and API."""

import argparse
import json
import math
import mimetypes
import os
import threading
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from . import core

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
OLLAMA_TAGS_URL = "http://127.0.0.1:11434/api/tags"
WEB_ROOT = Path(__file__).resolve().parents[2] / "web"
# Pasted text may be up to MAX_TEXT_CHARS characters; allow for JSON escapes.
MAX_REQUEST_BYTES = core.MAX_TEXT_CHARS * 6 + 64 * 1024
LANGUAGES = {None, "en", "hi", "mr"}
MODES = ("media", "text")
RECOMMENDED_PREFIX = "gemma"


def _initial_state(mode: str = "media", state: str = "idle") -> dict[str, Any]:
    return {
        "state": state,
        "mode": mode,
        "done": 0,
        "total": 0,
        "current_file": "",
        "stage": "",
        "error": None,
        "storage_path": str(core.DIGEST_DIR),
    }


def order_models(models: list[Any]) -> list[dict[str, Any]]:
    """Gemma models first (flagged as recommended), otherwise Ollama's order."""
    named = [
        model for model in models
        if isinstance(model, dict) and isinstance(model.get("name"), str) and model["name"]
    ]
    flagged = [
        {**model, "recommended": model["name"].lower().startswith(RECOMMENDED_PREFIX)}
        for model in named
    ]
    return sorted(flagged, key=lambda model: not model["recommended"])


class ServerState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.value: dict[str, Any] = _initial_state()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return self.value.copy()

    def update(self, **values: Any) -> None:
        with self.lock:
            self.value.update(values)


class SlothGistHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, request_handler, state: ServerState):
        self.state = state
        super().__init__(server_address, request_handler)


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _make_handler(web_root: Path):
    static_root = web_root.resolve()

    class RequestHandler(BaseHTTPRequestHandler):
        server: SlothGistHTTPServer

        def _send_json(self, status: HTTPStatus, value: Any) -> None:
            payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def _send_error(self, status: HTTPStatus, message: str) -> None:
            self._send_json(status, {"error": message})

        def _read_json(self) -> dict[str, Any]:
            raw_length = self.headers.get("Content-Length")
            try:
                length = int(raw_length) if raw_length is not None else -1
            except ValueError as error:
                raise ValueError("Content-Length must be an integer.") from error
            if length < 0 or length > MAX_REQUEST_BYTES:
                raise ValueError(
                    f"Request body must be between 0 and {MAX_REQUEST_BYTES} bytes."
                )
            try:
                data = json.loads(self.rfile.read(length))
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise ValueError("Request body must contain valid JSON.") from error
            if not isinstance(data, dict):
                raise ValueError("Request body must be a JSON object.")
            return data

        def do_GET(self) -> None:
            route = urlsplit(self.path).path
            if route == "/api/status":
                self._send_json(HTTPStatus.OK, self.server.state.snapshot())
                return
            if route == "/api/digest":
                self._get_digest()
                return
            if route == "/api/models":
                self._get_models()
                return
            if route == "/api/whisper-models":
                downloaded = core.downloaded_whisper_models()
                self._send_json(HTTPStatus.OK, {"models": [
                    {"name": size, "downloaded": size in downloaded}
                    for size in core.WHISPER_SIZES
                ]})
                return
            self._serve_static(route)

        def do_POST(self) -> None:
            if urlsplit(self.path).path != "/api/run":
                self._send_error(HTTPStatus.NOT_FOUND, "Not found.")
                return
            try:
                options = self._validate_run_request(self._read_json())
            except ValueError as error:
                self._send_error(HTTPStatus.BAD_REQUEST, str(error))
                return

            with self.server.state.lock:
                if self.server.state.value["state"] == "running":
                    self._send_error(HTTPStatus.CONFLICT, "A digest run is already active.")
                    return
                self.server.state.value = _initial_state(options["mode"], "running")

            worker = threading.Thread(
                target=self._run_digest,
                args=(options,),
                name="sloth-gist-digest",
                daemon=True,
            )
            try:
                worker.start()
            except RuntimeError as error:
                self.server.state.update(state="error", error=str(error))
                self._send_error(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "Could not start the digest worker.",
                )
                return
            self._send_json(HTTPStatus.ACCEPTED, {"state": "running"})

        def do_DELETE(self) -> None:
            if urlsplit(self.path).path != "/api/digest":
                self._send_error(HTTPStatus.NOT_FOUND, "Not found.")
                return
            with self.server.state.lock:
                if self.server.state.value["state"] == "running":
                    self._send_error(HTTPStatus.CONFLICT, "Cannot wipe a digest while a run is active.")
                    return
                try:
                    core.wipe_digests()
                except OSError as error:
                    self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(error))
                    return
            self._send_json(HTTPStatus.OK, {"deleted": True})

        @staticmethod
        def _validate_run_request(data: dict[str, Any]) -> dict[str, Any]:
            mode = data.get("mode", "media")
            if mode not in MODES:
                raise ValueError("'mode' must be 'media' or 'text'.")
            style = data.get("style", "line")
            if style not in core.STYLES:
                raise ValueError("'style' must be 'line', 'points', or 'both'.")
            language = data.get("language")
            if language is not None and not isinstance(language, str):
                raise ValueError("'language' must be null, 'en', 'hi', or 'mr'.")
            if language not in LANGUAGES:
                raise ValueError("'language' must be null, 'en', 'hi', or 'mr'.")
            ai_model = data.get("ai_model", core.DEFAULT_MODEL)
            if not isinstance(ai_model, str) or not ai_model.strip():
                raise ValueError("'ai_model' must be a non-empty string.")

            source = (
                RequestHandler._validate_text_source(data) if mode == "text"
                else RequestHandler._validate_media_options(data)
            )
            return {
                "mode": mode,
                **source,
                "style": style,
                "language": language,
                "ai_model": ai_model,
            }

        @staticmethod
        def _validate_text_source(data: dict[str, Any]) -> dict[str, Any]:
            text = data.get("text")
            path_value = data.get("path")
            if (text is None) == (path_value is None):
                raise ValueError("Text mode needs either 'text' or 'path', not both.")
            if text is not None:
                if not isinstance(text, str) or not text.strip():
                    raise ValueError("'text' must be a non-empty string.")
                if len(text) > core.MAX_TEXT_CHARS:
                    raise ValueError(
                        f"'text' must be at most {core.MAX_TEXT_CHARS:,} characters."
                    )
                name = data.get("name") or "Pasted text"
                if not isinstance(name, str) or len(name) > 200:
                    raise ValueError("'name' must be a string of at most 200 characters.")
                return {"text_or_files": text, "name": Path(name).name or "Pasted text"}

            if not isinstance(path_value, str) or not core.clean_path(path_value):
                raise ValueError("'path' must be a non-empty path.")
            try:
                path = Path(core.clean_path(path_value)).expanduser().resolve(strict=True)
            except (OSError, RuntimeError, ValueError) as error:
                raise ValueError("'path' must be an existing file or folder.") from error
            if path.is_file() and path.suffix.lower() not in core.TEXT_EXT:
                raise ValueError("'path' must be a .txt or .md file, or a folder.")
            if not path.is_file() and not path.is_dir():
                raise ValueError("'path' must be an existing file or folder.")
            return {"text_or_files": path}

        @staticmethod
        def _validate_media_options(data: dict[str, Any]) -> dict[str, Any]:
            # 'folder' may also name a single audio or video file.
            folder_value = data.get("folder")
            if not isinstance(folder_value, str) or not core.clean_path(folder_value):
                raise ValueError("'folder' must be a non-empty path.")
            missing = "'folder' must be an existing folder or audio/video file."
            try:
                folder = Path(core.clean_path(folder_value)).expanduser().resolve(strict=True)
            except (OSError, RuntimeError, ValueError) as error:
                raise ValueError(missing) from error
            if folder.is_file() and folder.suffix.lower() not in core.AUDIO_EXT:
                raise ValueError(f"{folder.name} is not a supported audio or video file.")
            if not folder.is_file() and not folder.is_dir():
                raise ValueError(missing)

            since_hours = data.get("since_hours", 24)
            if isinstance(since_hours, bool) or not isinstance(since_hours, (int, float)):
                raise ValueError("'since_hours' must be a finite non-negative number.")
            try:
                since_hours = float(since_hours)
            except (OverflowError, ValueError) as error:
                raise ValueError(
                    "'since_hours' must be a finite non-negative number."
                ) from error
            if not math.isfinite(since_hours) or since_hours < 0:
                raise ValueError("'since_hours' must be a finite non-negative number.")

            whisper_model = data.get("whisper_model", "small")
            if not isinstance(whisper_model, str) or not whisper_model.strip():
                raise ValueError("'whisper_model' must be a non-empty string.")

            offline = data.get("offline", False)
            if not isinstance(offline, bool):
                raise ValueError("'offline' must be a boolean.")

            return {
                "folder": folder,
                "since_hours": since_hours,
                "whisper_model": whisper_model,
                "offline": offline,
            }

        def _run_digest(self, options: dict[str, Any]) -> None:
            def on_progress(done: int, total: int, filename: str, stage: str) -> None:
                self.server.state.update(
                    done=done,
                    total=total,
                    current_file=filename,
                    stage=stage,
                )

            try:
                os.environ["HF_HUB_OFFLINE"] = "1"
                options = dict(options)
                if options.pop("mode") == "text":
                    core.run_text_digest(**options, on_progress=on_progress)
                else:
                    core.run_digest(**options, on_progress=on_progress)
            except Exception as error:
                self.server.state.update(
                    state="error",
                    error=str(error),
                )
            else:
                self.server.state.update(state="done", error=None)

        def _get_digest(self) -> None:
            try:
                content = core.DIGEST_JSON.read_bytes()
                json.loads(content)
            except FileNotFoundError:
                self._send_json(HTTPStatus.OK, [])
                return
            except (OSError, json.JSONDecodeError) as error:
                self._send_error(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    f"Could not read the saved digest: {error}",
                )
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)

        def _get_models(self) -> None:
            try:
                request = urllib.request.Request(OLLAMA_TAGS_URL)
                with core.open_local_url(request, timeout=5) as response:
                    payload = json.load(response)
                if not isinstance(payload, dict):
                    raise ValueError("Ollama returned an invalid response.")
                models = payload.get("models")
                if not isinstance(models, list):
                    raise ValueError("Ollama returned an invalid model list.")
            except (OSError, urllib.error.URLError, json.JSONDecodeError, ValueError) as error:
                self._send_error(
                    HTTPStatus.BAD_GATEWAY,
                    f"Could not get models from local Ollama: {error}",
                )
                return
            self._send_json(HTTPStatus.OK, {"models": order_models(models)})

        def _serve_static(self, route: str) -> None:
            if route.startswith("/api/"):
                self._send_error(HTTPStatus.NOT_FOUND, "Not found.")
                return
            try:
                relative_path = unquote(route).lstrip("/")
                if "\x00" in relative_path or "\\" in relative_path:
                    raise ValueError("Invalid path.")
                requested = (static_root / relative_path).resolve()
                if not _is_within(requested, static_root):
                    raise ValueError("Path is outside the web directory.")
                if requested.is_dir():
                    requested = (requested / "index.html").resolve()
                if not _is_within(requested, static_root) or not requested.is_file():
                    raise FileNotFoundError(requested)
                content = requested.read_bytes()
            except (OSError, ValueError):
                self._send_error(HTTPStatus.NOT_FOUND, "Static file not found.")
                return

            content_type = mimetypes.guess_type(requested.name)[0] or "application/octet-stream"
            if content_type.startswith("text/") or content_type in {
                "application/javascript", "application/json", "image/svg+xml",
            }:
                content_type += "; charset=utf-8"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    return RequestHandler


def create_server(host: str = HOST, port: int = DEFAULT_PORT) -> SlothGistHTTPServer:
    if host != HOST:
        raise ValueError(f"Sloth Gist server must bind to {HOST}, not {host}.")
    return SlothGistHTTPServer(
        (HOST, port),
        _make_handler(WEB_ROOT),
        ServerState(),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    try:
        server = create_server(args.host, args.port)
        if core.migrate_legacy_digests():
            print(f"Moved your earlier digests to {core.DIGEST_DIR}", flush=True)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(f"Sloth Gist is available at http://{HOST}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
