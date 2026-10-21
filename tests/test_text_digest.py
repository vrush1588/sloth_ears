import http.client
import os
import io
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import pytest

from sloth_gist import core, server

HINDI_TEXT = (
    "आज टीम की बैठक हुई और हमने अगले महीने का बजट तय किया है। "
    "रमेश शुक्रवार तक नई रिपोर्ट भेजेगा और ग्राहक से भी बात करेगा। "
    "हम अगले सप्ताह फिर मिलेंगे।"
)
MARATHI_TEXT = (
    "आज संघाची बैठक झाली आणि आम्ही पुढच्या महिन्याचे बजेट ठरवले आहे. "
    "सुरेश शुक्रवारपर्यंत नवीन अहवाल पाठवणार आहे आणि ग्राहकाशी बोलणार आहे. "
    "पुढच्या आठवड्यात आम्ही पुन्हा भेटणार आहोत."
)
# What a well-behaved local model answers when told which language to use.
CANNED = {
    "Hindi": {
        "summary": "टीम ने बजट तय किया।",
        "points": ["बजट पर सहमति", "रिपोर्ट शुक्रवार तक", "अगली मुलाकात"],
        "actions": ["शुक्रवार तक रिपोर्ट"],
    },
    "Marathi": {
        "summary": "संघाने बजेट निश्चित केले.",
        "points": ["बजेटवर एकमत", "अहवाल शुक्रवारी", "पुढील भेट"],
        "actions": ["शुक्रवारी अहवाल"],
    },
    None: {"summary": "The team agreed a budget.", "points": ["Budget"], "actions": []},
}


@pytest.fixture
def multilingual_model():
    """A fake local Ollama that answers in whatever language the prompt asks for."""
    prompts = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            prompts.append(body)
            instructions = body["prompt"].split("\n\nText:\n", 1)[0]
            language = next(
                (name for name in ("Hindi", "Marathi") if f"in {name}" in instructions),
                None,
            )
            response = json.dumps({"response": json.dumps(CANNED[language])}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, format, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", prompts
    finally:
        httpd.shutdown()
        thread.join(timeout=2)
        httpd.server_close()


@pytest.fixture
def digest_dir(tmp_path, monkeypatch):
    folder = tmp_path / "digests"
    monkeypatch.setattr(core, "DIGEST_DIR", folder)
    monkeypatch.setattr(core, "DIGEST", folder / "digest.md")
    monkeypatch.setattr(core, "DIGEST_JSON", folder / "digest.json")
    return folder


@pytest.mark.parametrize(
    ("text", "code", "name"),
    [(HINDI_TEXT, "hi", "Hindi"), (MARATHI_TEXT, "mr", "Marathi")],
)
def test_guess_language_tells_hindi_from_marathi(text, code, name):
    assert core.guess_language(text) == code
    assert core.guess_language("A plain English note.") is None


@pytest.mark.parametrize("language_option", [None, "explicit"])
@pytest.mark.parametrize(
    ("text", "code", "name"),
    [(HINDI_TEXT, "hi", "Hindi"), (MARATHI_TEXT, "mr", "Marathi")],
)
def test_text_digest_answers_in_the_input_language(
    multilingual_model, digest_dir, text, code, name, language_option
):
    host, prompts = multilingual_model
    language = code if language_option else None

    results = core.run_text_digest(text, "both", language, "fake-gemma", None, host=host)

    assert len(prompts) == 1
    assert f"Write every field in {name}" in prompts[0]["prompt"]
    assert prompts[0]["format"] == "json"
    entry = results[0]
    assert entry["summary"] == CANNED[name]["summary"]
    assert entry["points"] == CANNED[name]["points"]
    assert entry["actions"] == CANNED[name]["actions"]
    assert entry["kind"] == "text"
    markdown = (digest_dir / "digest.md").read_text(encoding="utf-8")
    assert CANNED[name]["summary"] in markdown
    assert all(f"  - {point}" in markdown for point in CANNED[name]["points"])
    saved_json = (digest_dir / "digest.json").read_text(encoding="utf-8")
    assert CANNED[name]["summary"] in saved_json  # stored as UTF-8, not \u escapes


def test_text_digest_never_writes_the_input_text(multilingual_model, digest_dir):
    host, _ = multilingual_model
    secret = "The confidential launch codename is BLUE HERON and nobody may know."

    core.run_text_digest(secret, "points", None, "fake-model", None, host=host)

    assert sorted(path.name for path in digest_dir.iterdir()) == ["digest.json", "digest.md"]
    for path in digest_dir.iterdir():
        assert "BLUE HERON" not in path.read_text(encoding="utf-8")


def test_text_digest_reads_txt_and_md_files_from_folder(multilingual_model, digest_dir, tmp_path):
    host, prompts = multilingual_model
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "a.txt").write_text("First note about the budget.", encoding="utf-8")
    (notes / "b.md").write_text("# Second\nPlan the offsite.", encoding="utf-8")
    (notes / "c.pdf").write_bytes(b"%PDF")
    events = []

    results = core.run_text_digest(
        notes, "line", None, "fake-model",
        lambda *event: events.append(event), host=host,
    )

    assert sorted(entry["file"] for entry in results) == [
        str((notes / "a.txt").resolve()), str((notes / "b.md").resolve()),
    ]
    assert len(prompts) == 2
    assert [event[3] for event in events] == ["reading", "summarizing", "done"] * 2
    assert all(entry["points"] == [] for entry in results)


def test_text_digest_rejects_oversized_text_and_wrong_file_type(tmp_path):
    with pytest.raises(ValueError, match="200,000"):
        core.run_text_digest("x" * (core.MAX_TEXT_CHARS + 1), "line", None, "m", None)
    pdf = tmp_path / "file.pdf"
    pdf.write_bytes(b"%PDF")
    with pytest.raises(ValueError, match=".txt or .md"):
        core.run_text_digest(pdf, "line", None, "m", None)


def test_points_style_merges_dedupes_and_caps_chunk_points():
    chunks = [
        core.Digest("", ["Call Asha"], [f"Point {index}" for index in range(start, start + 4)])
        for start in (0, 2, 4, 6)
    ]
    chunks[1] = chunks[1]._replace(points=["point 2.", "POINT 3", "Point 4", "Point 5"])
    with patch.object(core, "_ask", side_effect=chunks) as ask:
        summary, actions, points = core.summarize(
            "y" * (core.CHUNK * 3 + 1), "http://localhost:11434", "model", "points"
        )

    assert ask.call_count == 4  # no reduce call for points-only output
    assert summary == ""
    assert actions == ["Call Asha"]
    assert points == [f"Point {index}" for index in range(8)]


def test_both_style_reduces_summary_and_keeps_points():
    chunks = [core.Digest(f"part {index}", [], [f"idea {index}"]) for index in range(2)]
    final = core.Digest("whole story", [], ["ignored"])
    with patch.object(core, "_ask", side_effect=[*chunks, final]) as ask:
        result = core.summarize("z" * (core.CHUNK + 1), "http://localhost:11434", "m", "both")

    assert result == core.Digest("whole story", [], ["idea 0", "idea 1"])
    assert ask.call_args.args[4] == "line"


def test_summarize_rejects_unknown_style():
    with pytest.raises(ValueError, match="style"):
        core.summarize("text", "http://localhost:11434", "m", "essay")


@pytest.mark.parametrize(
    ("raw", "style", "expected"),
    [
        ('{"summary": "One line", "actions": "Reply to Ravi\\nBook room"}', "line",
         core.Digest("One line", ["Reply to Ravi", "Book room"], [])),
        ('{"summary": ["a", "b"], "actions": [{"task": "x"}]}', "line",
         core.Digest("a; b", ["x"], [])),
        ('["first", "second"]', "points", core.Digest("", [], ["first", "second"])),
        ('{"key_points": "- alpha\\n2. beta"}', "points", core.Digest("", [], ["alpha", "beta"])),
        ('{"summary": "Budget agreed. Report due Friday."}', "points",
         core.Digest("", [], ["Budget agreed.", "Report due Friday."])),
        ('{"points": ["only point"]}', "both", core.Digest("only point", [], ["only point"])),
        ('Sure! {"summary": "Wrapped", "points": ["p"]} hope this helps', "both",
         core.Digest("Wrapped", [], ["p"])),
        ("Not JSON at all.", "line", core.Digest("Not JSON at all.", [], [])),
        ("null", "points", core.Digest("", [], [])),
        ('{"summary": 42, "points": null, "actions": 7}', "both",
         core.Digest("42", [], ["42"])),
    ],
)
def test_parse_answer_falls_back_on_wrong_shapes(raw, style, expected):
    assert core._parse_answer(raw, style) == expected


def test_ask_rejects_ollama_reply_without_response_text():
    with patch.object(core, "open_local_url", return_value=io.BytesIO(b'{"error": "no model"}')):
        with pytest.raises(ValueError, match="unexpected response"):
            core._ask("hi", "ONE short line", "http://localhost:11434", "m")


def test_echo_guard_drops_copied_devanagari_points():
    answer = core.Digest(
        "बजट पर सहमति बनी।",
        [],
        ["रमेश शुक्रवार तक नई रिपोर्ट भेजेगा", "बजट पर सहमति"],
    )
    result = core._without_transcript_echo(answer, HINDI_TEXT)

    assert result.summary == "बजट पर सहमति बनी।"
    assert result.points == ["बजट पर सहमति"]


# --- server -----------------------------------------------------------------

@pytest.fixture
def httpd():
    instance = server.create_server(port=0)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        yield instance
    finally:
        deadline = time.monotonic() + 3
        while instance.state.snapshot()["state"] == "running" and time.monotonic() < deadline:
            time.sleep(0.01)
        instance.shutdown()
        thread.join(timeout=2)
        instance.server_close()


def _post(httpd, body):
    connection = http.client.HTTPConnection(*httpd.server_address, timeout=5)
    connection.request("POST", "/api/run", body=json.dumps(body),
                       headers={"Content-Type": "application/json"})
    response = connection.getresponse()
    result = response.status, json.loads(response.read())
    connection.close()
    return result


def _wait_until_finished(httpd):
    deadline = time.monotonic() + 3
    while httpd.state.snapshot()["state"] == "running" and time.monotonic() < deadline:
        time.sleep(0.01)
    return httpd.state.snapshot()


def test_server_runs_text_mode_with_pasted_text(httpd, monkeypatch):
    calls = []
    monkeypatch.setattr(server.core, "run_text_digest", lambda **kw: calls.append(kw) or [])
    text = "म" * core.MAX_TEXT_CHARS  # largest allowed paste, 3 bytes per character

    status, _ = _post(httpd, {
        "mode": "text", "text": text, "name": r"C:\notes\plan.md",
        "style": "points", "language": "mr", "ai_model": "gemma3:4b",
    })

    assert status == 202
    snapshot = _wait_until_finished(httpd)
    assert snapshot["state"] == "done"
    assert snapshot["mode"] == "text"
    assert calls[0]["text_or_files"] == text
    assert calls[0]["name"] == "plan.md"
    assert calls[0]["style"] == "points"
    assert calls[0]["language"] == "mr"


def test_server_runs_text_mode_with_path_and_media_style(httpd, monkeypatch, tmp_path):
    note = tmp_path / "note.txt"
    note.write_text("hello", encoding="utf-8")
    text_calls, media_calls = [], []
    monkeypatch.setattr(server.core, "run_text_digest", lambda **kw: text_calls.append(kw) or [])
    monkeypatch.setattr(server.core, "run_digest", lambda **kw: media_calls.append(kw) or [])

    assert _post(httpd, {"mode": "text", "path": str(note)})[0] == 202
    _wait_until_finished(httpd)
    assert _post(httpd, {"folder": str(tmp_path), "style": "both"})[0] == 202
    _wait_until_finished(httpd)

    assert text_calls[0]["text_or_files"] == note.resolve()
    assert text_calls[0]["style"] == "line"
    assert media_calls[0]["style"] == "both"
    assert "mode" not in media_calls[0]


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"mode": "text", "text": "x" * (core.MAX_TEXT_CHARS + 1)}, "at most 200,000"),
        ({"mode": "text", "text": "   "}, "non-empty"),
        ({"mode": "text"}, "either 'text' or 'path'"),
        ({"mode": "text", "text": "hi", "path": "."}, "either 'text' or 'path'"),
        ({"mode": "text", "path": "missing-file.txt"}, "existing file or folder"),
        ({"mode": "text", "text": "hi", "style": "essay"}, "'style'"),
        ({"mode": "podcast", "text": "hi"}, "'mode'"),
    ],
)
def test_server_rejects_invalid_text_requests(httpd, body, message):
    status, payload = _post(httpd, body)

    assert status == 400
    assert message in payload["error"]


def test_server_rejects_non_text_file_path(httpd, tmp_path):
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF")

    status, payload = _post(httpd, {"mode": "text", "path": str(pdf)})

    assert status == 400
    assert ".txt or .md" in payload["error"]


def test_models_put_gemma_first_as_recommended():
    models = [
        {"name": "llama3.2:3b"}, {"name": "Gemma3:4b"}, {"name": "qwen2.5"},
        {"name": "gemma2:2b"}, {"name": ""}, "junk", {"name": "codegemma"},
    ]

    ordered = server.order_models(models)

    assert [model["name"] for model in ordered] == [
        "Gemma3:4b", "gemma2:2b", "llama3.2:3b", "qwen2.5", "codegemma",
    ]
    assert [model["recommended"] for model in ordered] == [True, True, False, False, False]


def test_echo_guard_keeps_short_shared_hindi_phrasing():
    # "अगले महीने का बजट" is a natural four-word phrase, not a copied sentence.
    answer = core.Digest("टीम ने अगले महीने का बजट निर्धारित किया।", [], [])

    assert core._without_transcript_echo(answer, HINDI_TEXT).summary == answer.summary


def test_ask_retries_once_when_model_returns_empty_object():
    replies = [
        io.BytesIO(json.dumps({"response": "{}"}).encode()),
        io.BytesIO(json.dumps({"response": '{"summary": "second try"}'}).encode()),
    ]
    with patch.object(core, "open_local_url", side_effect=replies) as open_url:
        answer = core._ask("text", "ONE short line", "http://localhost:11434", "m")

    assert answer.summary == "second try"
    assert open_url.call_count == 2


# --- single media file paths -------------------------------------------------

def test_find_media_accepts_one_file_regardless_of_age(tmp_path):
    video = tmp_path / "Screen capture 2026-10-17.mp4"
    video.write_bytes(b"media")
    old = time.time() - 30 * 24 * 3600
    os.utime(video, (old, old))
    (tmp_path / "notes.txt").write_text("not media", encoding="utf-8")

    assert core.find_media(video, 24) == [video.resolve()]
    assert core.find_media(tmp_path, 24) == []
    with pytest.raises(ValueError, match="not a supported audio or video"):
        core.find_media(tmp_path / "notes.txt", 24)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (r'"C:\Users\you\Videos\clip one.mp4"', r"C:\Users\you\Videos\clip one.mp4"),
        ("  'clip.mp4'  ", "clip.mp4"),
        ("clip.mp4", "clip.mp4"),
        ('"', '"'),
    ],
)
def test_clean_path_strips_copy_as_path_quotes(raw, expected):
    assert core.clean_path(raw) == expected


def test_server_accepts_quoted_media_file_and_rejects_other_files(httpd, monkeypatch, tmp_path):
    video = tmp_path / "Asset Copilot - Chrome.mp4"
    video.write_bytes(b"media")
    notes = tmp_path / "notes.txt"
    notes.write_text("hi", encoding="utf-8")
    calls = []
    monkeypatch.setattr(server.core, "run_digest", lambda **kw: calls.append(kw) or [])

    status, _ = _post(httpd, {"folder": f'"{video}"'})
    assert status == 202
    _wait_until_finished(httpd)
    assert calls[0]["folder"] == video.resolve()

    status, payload = _post(httpd, {"folder": str(notes)})
    assert status == 400
    assert "not a supported audio or video file" in payload["error"]


# --- downloaded Whisper models -----------------------------------------------

def _cache_whisper(cache, size):
    snapshot = cache / f"models--Systran--faster-whisper-{size}" / "snapshots" / "abc123"
    snapshot.mkdir(parents=True)
    (snapshot / "model.bin").write_bytes(b"weights")


def test_downloaded_whisper_models_reads_the_hugging_face_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path))
    _cache_whisper(tmp_path, "base")
    # A started-but-unfinished download has no model.bin yet.
    (tmp_path / "models--Systran--faster-whisper-small" / "snapshots" / "x").mkdir(parents=True)

    assert core.downloaded_whisper_models() == ["base"]


def test_run_digest_explains_a_missing_whisper_model(tmp_path, monkeypatch):
    import types

    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "cache"))
    _cache_whisper(tmp_path / "cache", "base")
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"media")

    def offline_model(*args, **kwargs):
        raise OSError("Cannot find an appropriate cached snapshot folder")

    monkeypatch.setitem(
        __import__("sys").modules, "faster_whisper",
        types.SimpleNamespace(WhisperModel=offline_model),
    )
    with pytest.raises(core.WhisperModelMissing) as error:
        core.run_digest(video, 24, None, "small", "m", False, None)

    assert "'small' speech model isn't downloaded" in str(error.value)
    assert "Settings (base)" in str(error.value)


def test_server_lists_whisper_models_with_download_state(httpd, tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path))
    _cache_whisper(tmp_path, "base")
    connection = http.client.HTTPConnection(*httpd.server_address, timeout=5)
    connection.request("GET", "/api/whisper-models")
    response = connection.getresponse()
    payload = json.loads(response.read())
    connection.close()

    assert response.status == 200
    assert payload == {"models": [
        {"name": "tiny", "downloaded": False},
        {"name": "base", "downloaded": True},
        {"name": "small", "downloaded": False},
        {"name": "medium", "downloaded": False},
    ]}


# --- digest history ----------------------------------------------------------

def test_new_runs_add_to_the_saved_digest_instead_of_replacing_it(
    multilingual_model, digest_dir, tmp_path
):
    host, _ = multilingual_model
    note = tmp_path / "note.txt"
    note.write_text("Budget meeting notes.", encoding="utf-8")

    first = core.run_text_digest("First pasted note.", "line", None, "m", None, host=host)
    core.run_text_digest("Second pasted note.", "line", None, "m", None, host=host)
    core.run_text_digest(note, "line", None, "m", None, host=host)
    again = core.run_text_digest(note, "points", None, "m", None, host=host)

    saved = core.load_digest()
    # Both pastes are kept; the re-digested file replaces its earlier entry.
    assert [entry["file"] for entry in saved] == [
        "Pasted text", "Pasted text", str(note.resolve()),
    ]
    assert saved[0]["id"] == first[0]["id"]
    assert saved[-1]["id"] == again[0]["id"]
    assert len({entry["id"] for entry in saved}) == 3
    markdown = (digest_dir / "digest.md").read_text(encoding="utf-8")
    assert markdown.count("`Pasted text`") == 2
    assert f"**{saved[0]['date']} {saved[0]['time']}**" in markdown


def test_digest_history_is_capped_and_survives_a_corrupt_file(digest_dir, monkeypatch):
    monkeypatch.setattr(core, "MAX_DIGEST_ENTRIES", 3)
    digest_dir.mkdir()
    (digest_dir / "digest.json").write_text("{not json", encoding="utf-8")
    assert core.load_digest() == []

    def entry(number):
        return {"id": str(number), "file": f"note {number}", "date": "2026-10-21",
                "time": "10:00", "duration_seconds": 0.0, "summary": f"s{number}",
                "actions": [], "points": [], "kind": "text"}

    for number in range(5):
        core._save_digest([entry(number)])

    assert [saved["id"] for saved in core.load_digest()] == ["2", "3", "4"]


def test_digests_move_from_the_old_sloth_ears_folder(digest_dir, tmp_path, monkeypatch):
    legacy = tmp_path / "SlothEars" / "Digests"
    legacy.mkdir(parents=True)
    (legacy / "digest.json").write_text('[{"summary": "पुराना सार"}]', encoding="utf-8")
    (legacy / "digest.md").write_text("# old digest\n", encoding="utf-8")
    monkeypatch.setattr(core, "LEGACY_DIGEST_DIR", legacy)

    assert core.migrate_legacy_digests() is True
    assert core.load_digest() == [{"summary": "पुराना सार"}]
    assert (digest_dir / "digest.md").read_text(encoding="utf-8") == "# old digest\n"
    assert not any(legacy.iterdir())
    # Once moved (or if new digests exist), it does nothing.
    assert core.migrate_legacy_digests() is False
