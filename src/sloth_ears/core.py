#!/usr/bin/env python3
"""Local audio/video transcription and text digest generation."""

import argparse
import datetime as dt
import json
import os
import re
import stat
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from pathlib import Path
from typing import Callable, NamedTuple, NotRequired, TypedDict

AUDIO_EXT = {
    ".opus", ".ogg", ".m4a", ".mp3", ".wav", ".aac", ".amr", ".flac",
    ".wma", ".m4b", ".3gp", ".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v",
}
TEXT_EXT = {".txt", ".md"}
CHUNK = 5000
MAX_TEXT_CHARS = 200_000
MAX_POINTS = 8
ECHO_WORDS = 6
STYLES = ("line", "points", "both")
DEFAULT_HOST = "http://localhost:11434"
DEFAULT_MODEL = "gemma3:4b"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
DIGEST_DIR = Path.home() / "Documents" / "SlothEars" / "Digests"
DIGEST = DIGEST_DIR / "digest.md"
DIGEST_JSON = DIGEST_DIR / "digest.json"
LANGUAGE_NAMES = {"en": "English", "hi": "Hindi (हिन्दी)", "mr": "Marathi (मराठी)"}
# Common function words that tell Hindi and Marathi apart in Devanagari text.
_HINDI_WORDS = {
    "है", "हैं", "था", "थे", "थी", "और", "नहीं", "में", "के", "का", "को",
    "से", "पर", "हम", "यह", "वह", "भी", "कि", "लिए", "गया",
}
_MARATHI_WORDS = {
    "आहे", "आहेत", "होते", "होता", "आणि", "नाही", "मध्ये", "च्या", "ला",
    "आम्ही", "आपण", "हे", "ते", "पण", "म्हणून", "साठी", "केले", "झाले",
}


class DigestEntry(TypedDict):
    file: str
    time: str
    duration_seconds: float
    summary: str
    actions: list[str]
    points: list[str]
    kind: str
    words: NotRequired[int]


class Digest(NamedTuple):
    summary: str
    actions: list[str]
    points: list[str]


ProgressCallback = Callable[[int, int, str, str], None]


class LocalModelError(RuntimeError):
    """The local Ollama request failed while creating a digest."""


class WhisperModelMissing(RuntimeError):
    """The chosen Whisper model has not been downloaded to this computer."""


class _LocalRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        redirected = urllib.parse.urlparse(
            urllib.parse.urljoin(request.full_url, new_url)
        )
        if (
            redirected.scheme not in {"http", "https"}
            or redirected.hostname not in LOCAL_HOSTS
        ):
            raise urllib.error.HTTPError(
                new_url, code, "Refusing redirect away from localhost.", headers, file_pointer
            )
        return super().redirect_request(
            request, file_pointer, code, message, headers, new_url
        )


def open_local_url(request, timeout: float):
    if urllib.parse.urlparse(request.full_url).scheme not in {"http", "https"}:
        raise ValueError("Only local HTTP requests are permitted.")
    require_local(request.full_url)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _LocalRedirectHandler(),
    )
    return opener.open(request, timeout=timeout)


def _protect_digest_file(path: Path) -> None:
    if os.name == "nt":
        result = subprocess.run(
            [
                "icacls",
                str(path),
                "/inheritance:r",
                "/grant:r",
                f"*{_current_windows_user_sid()}:(F)",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            raise OSError(f"Could not restrict digest permissions for {path}.")
        return
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def _current_windows_user_sid() -> str:
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HANDLE]
    kernel32.LocalFree.restype = wintypes.HANDLE
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [
        wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        required = wintypes.DWORD()
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(required))
        token_user = ctypes.create_string_buffer(required.value)
        if not advapi32.GetTokenInformation(
            token, 1, token_user, required.value, ctypes.byref(required)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        sid_pointer = ctypes.cast(
            token_user, ctypes.POINTER(ctypes.c_void_p)
        )[0]
        sid_text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(
            ctypes.cast(sid_pointer, wintypes.LPVOID), ctypes.byref(sid_text)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return sid_text.value
        finally:
            kernel32.LocalFree(sid_text)
    finally:
        kernel32.CloseHandle(token)


def _write_private_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(descriptor)
    _protect_digest_file(path)
    with path.open("w", encoding="utf-8", newline="\n") as output:
        output.write(content)


def require_local(host: str) -> None:
    if urllib.parse.urlparse(host).hostname not in LOCAL_HOSTS:
        sys.exit(
            f"Refusing to run: {host} is not this machine. "
            "sloth-ears never sends audio or text off-device."
        )


def _walk_files(folder: str | Path, extensions: set[str]) -> list[Path]:
    """Files under folder with the given suffixes, never following links out."""
    folder_path = Path(folder).expanduser().resolve()
    files = []
    def is_link_or_junction(path: Path) -> bool:
        is_junction = getattr(os.path, "isjunction", lambda _: False)
        return path.is_symlink() or is_junction(path)

    for current, directories, filenames in os.walk(folder_path, followlinks=False):
        current_path = Path(current)
        directories[:] = [
            name for name in directories
            if not is_link_or_junction(current_path / name)
        ]
        for filename in filenames:
            path = current_path / filename
            if is_link_or_junction(path) or path.suffix.lower() not in extensions:
                continue
            resolved = path.resolve()
            if folder_path not in resolved.parents or not resolved.is_file():
                continue
            files.append(resolved)
    return sorted(files, key=lambda path: path.stat().st_mtime)


WHISPER_SIZES = ("tiny", "base", "small", "medium")


def _hf_hub_cache() -> Path:
    # Same lookup order as huggingface_hub, which faster-whisper downloads with.
    if os.environ.get("HF_HUB_CACHE"):
        return Path(os.environ["HF_HUB_CACHE"]).expanduser()
    if os.environ.get("HF_HOME"):
        return Path(os.environ["HF_HOME"]).expanduser() / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def downloaded_whisper_models() -> list[str]:
    """Standard Whisper sizes already in the local cache, smallest first."""
    cache = _hf_hub_cache()
    return [
        size for size in WHISPER_SIZES
        if any(
            (snapshot / "model.bin").is_file()
            for snapshot in (
                cache / f"models--Systran--faster-whisper-{size}" / "snapshots"
            ).glob("*")
        )
    ]


def missing_whisper_message(size: str) -> str:
    available = downloaded_whisper_models()
    choice = (
        f"Choose a downloaded one in Settings ({', '.join(available)}), or download"
        if available else "Download"
    )
    return (
        f"The Whisper '{size}' speech model isn't downloaded yet. {choice} it once "
        "while online: python -c \"from faster_whisper import WhisperModel; "
        f"WhisperModel('{size}', device='cpu', compute_type='int8')\""
    )


def clean_path(value: str) -> str:
    """Trim spaces and the quotes Windows adds with "Copy as path"."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value


def find_media(folder: str | Path, since_hours: float) -> list[Path]:
    """Recent media in a folder, or a single media file whatever its age."""
    path = Path(folder).expanduser().resolve()
    if path.is_file():
        if path.suffix.lower() not in AUDIO_EXT:
            raise ValueError(f"{path.name} is not a supported audio or video file.")
        return [path]
    cutoff = time.time() - since_hours * 3600
    return [
        media for media in _walk_files(path, AUDIO_EXT)
        if media.stat().st_mtime >= cutoff
    ]


def find_text_files(path: str | Path) -> list[Path]:
    """A single .txt/.md file, or every .txt/.md file inside a folder."""
    resolved = Path(path).expanduser().resolve()
    if resolved.is_dir():
        return _walk_files(resolved, TEXT_EXT)
    if not resolved.is_file():
        raise ValueError(f"{path} does not exist.")
    if resolved.suffix.lower() not in TEXT_EXT:
        raise ValueError(f"{resolved.name} is not a .txt or .md file.")
    return [resolved]


def read_text_file(path: Path) -> str:
    # UTF-8 uses at most 4 bytes per character, so this rejects huge files
    # before reading them into memory.
    if path.stat().st_size > MAX_TEXT_CHARS * 4:
        raise ValueError(f"{path.name} is longer than {MAX_TEXT_CHARS:,} characters.")
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError(f"{path.name} is longer than {MAX_TEXT_CHARS:,} characters.")
    return text


def transcribe(file: str | Path, whisper_model, language: str | None) -> tuple[str, float]:
    segments, info = whisper_model.transcribe(
        str(file),
        vad_filter=True,
        language=language,
    )
    text = " ".join(segment.text.strip() for segment in segments).strip()
    return text, float(info.duration)


def guess_language(text: str) -> str | None:
    """Best-effort guess of Hindi or Marathi; None means "same as the text"."""
    letters = [character for character in text if character.isalpha()]
    if not letters:
        return None
    devanagari = sum("ऀ" <= character <= "ॿ" for character in letters)
    if devanagari / len(letters) < 0.5:
        return None
    words = [word.strip(".,;:!?।॥\"'()[]-") for word in text.split()]
    hindi = sum(word in _HINDI_WORDS for word in words)
    marathi = sum(word in _MARATHI_WORDS for word in words)
    if marathi > hindi:
        return "mr"
    if hindi > marathi:
        return "hi"
    return None


def _language_rule(language: str | None) -> str:
    name = LANGUAGE_NAMES.get(language or "")
    if name:
        return (
            f"The text is in {name}. Write every field in {name}; "
            "do not translate it into any other language. "
        )
    return (
        "Write every field in the same language and script as the text; "
        "do not translate it. "
    )


_SHAPES = {
    "line": '{"summary": str, "actions": [str]}',
    "points": '{"points": [str], "actions": [str]}',
    "both": '{"summary": str, "points": [str], "actions": [str]}',
}


def _ask(
    text: str,
    size: str,
    host: str,
    model: str,
    style: str = "line",
    language: str | None = None,
) -> Digest:
    prompt = ""
    if style != "points":
        prompt += f"Summarize this text in {size}. "
    if style != "line":
        prompt += "List 3 to 6 short key points, each one short phrase or sentence. "
    prompt += (
        _language_rule(language)
        + "List any concrete to-dos, dates or questions that need a reply. "
        "Paraphrase; never quote or reproduce any phrase verbatim from the text. "
        f"Reply with JSON only: {_SHAPES[style]}\n\nText:\n" + text
    )
    body = json.dumps(
        {"model": model, "prompt": prompt, "stream": False, "format": "json"}
    ).encode()
    request = urllib.request.Request(
        f"{host}/api/generate",
        body,
        {"Content-Type": "application/json"},
    )
    # Small models occasionally reply with an empty object; ask once more.
    for _ in range(2):
        with open_local_url(request, timeout=180) as response:
            payload = json.load(response)
        if not isinstance(payload, dict) or not isinstance(payload.get("response"), str):
            raise ValueError("Ollama returned an unexpected response.")
        answer = _parse_answer(payload["response"], style)
        if answer.summary or answer.points or answer.actions:
            break
    return answer


def _as_text(value) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, list):
        return "; ".join(text for text in map(_as_text, value) if text)
    return ""


def _as_list(value) -> list[str]:
    if isinstance(value, str):
        items = value.splitlines()
    elif isinstance(value, dict):
        items = [_as_text(item) for item in value.values()]
    elif isinstance(value, list):
        items = [_as_text(item) for item in value]
    else:
        return []
    cleaned = (
        re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", item).strip() for item in items
    )
    return [item for item in cleaned if item]


def _split_sentences(text: str) -> list[str]:
    return [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?।])\s+|\n+", text)
        if sentence.strip()
    ]


def _first(output: dict, *keys: str):
    for key in keys:
        if output.get(key):
            return output[key]
    return None


def _parse_answer(raw: str, style: str) -> Digest:
    """Read the model's JSON, tolerating prose, lists and renamed keys."""
    try:
        output = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        try:
            output = json.loads(match.group()) if match else {"summary": raw}
        except json.JSONDecodeError:
            output = {"summary": raw}
    if isinstance(output, list):
        output = {"points": output}
    if not isinstance(output, dict):
        output = {}

    summary = _as_text(_first(output, "summary", "short_summary", "line"))
    points = _as_list(_first(output, "points", "key_points", "keyPoints", "bullets"))
    actions = _as_list(_first(output, "actions", "action_items", "todos"))

    if style == "line":
        return Digest(summary or (points[0] if points else ""), actions, [])
    points = points or _as_list(_split_sentences(summary))
    if style == "points":
        return Digest("", actions, points)
    return Digest(summary or (points[0] if points else ""), actions, points)


def _unique(items: Iterable[str]) -> list[str]:
    """Drop repeats that differ only in case, spacing or end punctuation."""
    seen = set()
    unique = []
    for item in items:
        key = " ".join(item.casefold().split()).strip(" .,;:!?।-")
        if key and key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def _normalize(value: str) -> str:
    # Keep letters, combining marks and digits so Devanagari vowel signs survive.
    return "".join(
        character for character in value.casefold()
        if unicodedata.category(character)[0] in "LMN"
    )


def _without_transcript_echo(answer: Digest, source_text: str) -> Digest:
    source = _normalize(source_text)

    def is_echo(value: str) -> bool:
        normalized = _normalize(value)
        if not normalized:
            return False
        if source in normalized:
            return True
        # Short shared phrases are normal (Hindi and Marathi especially lean on
        # short function words), so only a copied run of ECHO_WORDS counts.
        output_words = [word for word in map(_normalize, value.split()) if word]
        for width in range(min(len(output_words), 12), ECHO_WORDS - 1, -1):
            for start in range(len(output_words) - width + 1):
                copied_phrase = "".join(output_words[start:start + width])
                if copied_phrase in source:
                    return True
        return False

    summary = answer.summary
    if is_echo(summary):
        summary = "Summary withheld to avoid repeating the source text."
    return Digest(
        summary,
        [action for action in answer.actions if not is_echo(action)],
        [point for point in answer.points if not is_echo(point)],
    )


def summarize(
    text: str,
    host: str,
    model: str,
    style: str = "line",
    language: str | None = None,
) -> Digest:
    """Summarize short text once and longer text in chunks (map-reduce)."""
    if style not in STYLES:
        raise ValueError(f"style must be one of {', '.join(STYLES)}.")
    language = language or guess_language(text)
    if len(text) <= CHUNK:
        answer = _ask(text, "ONE short line", host, model, style, language)
        answer = _without_transcript_echo(answer, text)
        return Digest(
            answer.summary, _unique(answer.actions)[:5], _unique(answer.points)[:6]
        )
    parts = [text[index:index + CHUNK] for index in range(0, len(text), CHUNK)]
    partials = [
        _without_transcript_echo(
            _ask(part, "2 short sentences", host, model, style, language), part
        )
        for part in parts
    ]
    summary = ""
    partial_summaries = [partial.summary for partial in partials if partial.summary]
    if style != "points" and partial_summaries:
        combined = _ask(
            "\n".join(partial_summaries),
            "3 to 4 short sentences covering the whole text",
            host,
            model,
            "line",
            language,
        )
        summary = _without_transcript_echo(combined, text).summary
    actions = _unique(action for partial in partials for action in partial.actions)
    points = _unique(point for partial in partials for point in partial.points)
    return Digest(summary, actions[:8], points[:MAX_POINTS])


def format_digest(results: list[DigestEntry]) -> str:
    lines = [f"# Sloth Ears digest ({dt.datetime.now():%d %b %Y, %H:%M})", ""]
    for result in results:
        label = f"- **{result['time']}** `{Path(result['file']).name}`:"
        points = result.get("points", [])
        if result["summary"] or points:
            lines.append(f"{label} {result['summary']}".rstrip())
            lines.extend(f"  - {point}" for point in points)
            lines.extend(f"  - [ ] {action}" for action in result["actions"])
        elif result.get("kind") == "text":
            lines.append(f"{label} (no text found)")
        else:
            lines.append(f"{label} (no speech detected)")
    return "\n".join(lines) + "\n"


def _save_digest(results: list[DigestEntry]) -> None:
    _write_private_file(DIGEST, format_digest(results))
    _write_private_file(
        DIGEST_JSON,
        json.dumps(results, ensure_ascii=False, indent=2) + "\n",
    )


def _summarize_locally(
    text: str, host: str, model: str, style: str, language: str | None
) -> Digest:
    try:
        return summarize(text, host, model, style, language)
    except Exception as error:
        raise LocalModelError(
            f"Could not reach the local model at {host} ({error}). "
            "Is `ollama serve` running?"
        ) from error


def run_digest(
    folder: str | Path,
    since_hours: float,
    language: str | None,
    whisper_model: str,
    ai_model: str,
    offline: bool,
    on_progress: ProgressCallback | None,
    host: str = DEFAULT_HOST,
    style: str = "line",
) -> list[DigestEntry]:
    require_local(host)
    if style not in STYLES:
        raise ValueError(f"style must be one of {', '.join(STYLES)}.")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"

    files = find_media(folder, since_hours)
    if not files:
        return []

    from faster_whisper import WhisperModel

    try:
        model = WhisperModel(whisper_model, device="cpu", compute_type="int8")
    except Exception as error:
        if (
            whisper_model in WHISPER_SIZES
            and whisper_model not in downloaded_whisper_models()
        ):
            raise WhisperModelMissing(missing_whisper_message(whisper_model)) from error
        raise
    results: list[DigestEntry] = []
    total = len(files)
    for index, media_file in enumerate(files):
        completed = index
        filename = media_file.name
        if on_progress:
            on_progress(completed, total, filename, "listening")
        text, duration = transcribe(media_file, model, language)

        if text:
            if on_progress:
                on_progress(completed, total, filename, "summarizing")
            answer = _summarize_locally(text, host, ai_model, style, language)
        else:
            answer = Digest("", [], [])

        timestamp = dt.datetime.fromtimestamp(media_file.stat().st_mtime)
        results.append({
            "file": str(media_file),
            "time": timestamp.strftime("%H:%M"),
            "duration_seconds": duration,
            "summary": answer.summary,
            "actions": answer.actions,
            "points": answer.points,
            "kind": "media",
        })
        if on_progress:
            on_progress(index + 1, total, filename, "done")

    _save_digest(results)
    return results


def run_text_digest(
    text_or_files: str | os.PathLike | Iterable[str | os.PathLike],
    style: str,
    language: str | None,
    ai_model: str,
    on_progress: ProgressCallback | None,
    host: str = DEFAULT_HOST,
    name: str = "Pasted text",
) -> list[DigestEntry]:
    """Summarize pasted text (a str) or .txt/.md files (paths) without Whisper.

    The input text is only held in memory; only the digest is written to disk.
    """
    require_local(host)
    if style not in STYLES:
        raise ValueError(f"style must be one of {', '.join(STYLES)}.")
    if isinstance(text_or_files, str):
        if len(text_or_files) > MAX_TEXT_CHARS:
            raise ValueError(f"Text is longer than {MAX_TEXT_CHARS:,} characters.")
        sources: list[Path | None] = [None]
    else:
        items = (
            [text_or_files] if isinstance(text_or_files, os.PathLike)
            else list(text_or_files)
        )
        sources = [path for item in items for path in find_text_files(item)]
    if not sources:
        return []

    results: list[DigestEntry] = []
    total = len(sources)
    for index, path in enumerate(sources):
        label = name if path is None else path.name
        if on_progress:
            on_progress(index, total, label, "reading")
        text = (text_or_files if path is None else read_text_file(path)).strip()
        if text:
            if on_progress:
                on_progress(index, total, label, "summarizing")
            answer = _summarize_locally(text, host, ai_model, style, language)
        else:
            answer = Digest("", [], [])

        timestamp = (
            dt.datetime.now() if path is None
            else dt.datetime.fromtimestamp(path.stat().st_mtime)
        )
        results.append({
            "file": label if path is None else str(path),
            "time": timestamp.strftime("%H:%M"),
            "duration_seconds": 0.0,
            "summary": answer.summary,
            "actions": answer.actions,
            "points": answer.points,
            "kind": "text",
            "words": len(text.split()),
        })
        del text
        if on_progress:
            on_progress(index + 1, total, label, "done")

    _save_digest(results)
    return results


def wipe_digests() -> None:
    DIGEST.unlink(missing_ok=True)
    DIGEST_JSON.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("folder", nargs="?",
                        help="a recording or folder of recordings; "
                             "a .txt/.md file or folder with --text")
    parser.add_argument(
        "--since", type=float, default=24,
        help="only audio files from the last N hours",
    )
    parser.add_argument(
        "--whisper", default="small",
        help="tiny/base/small/medium (small handles Hindi/Marathi OK)",
    )
    parser.add_argument("--language", choices=("en", "hi", "mr"), default=None,
                        help="input language (default: auto-detect)")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--style", choices=STYLES, default="line",
                        help="line: one short line; points: key points; both")
    parser.add_argument("--text", action="store_true",
                        help="summarize .txt/.md files instead of audio/video")
    parser.add_argument(
        "--offline", action="store_true",
        help="retained for compatibility; model downloads are always disabled",
    )
    parser.add_argument("--wipe", action="store_true",
                        help="delete saved digests and exit")
    args = parser.parse_args()

    if args.wipe:
        wipe_digests()
        print("Digest deleted.")
        return
    if not args.folder:
        parser.error("folder is required")
    args.folder = clean_path(args.folder)
    require_local(args.host)

    try:
        if args.text:
            results = run_text_digest(
                Path(args.folder), args.style, args.language, args.model,
                on_progress=None, host=args.host,
            )
        else:
            results = run_digest(
                args.folder,
                args.since,
                args.language,
                args.whisper,
                args.model,
                args.offline,
                on_progress=None,
                host=args.host,
                style=args.style,
            )
    except (LocalModelError, WhisperModelMissing, ValueError) as error:
        sys.exit(str(error))

    if not results:
        print("No new files. Naptime.")
        return
    print(format_digest(results), end="")
    print(f"\nSaved to {DIGEST} and {DIGEST_JSON} (restricted permissions).")


if __name__ == "__main__":
    main()
