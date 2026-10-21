import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from sloth_ears import core


class CoreTests(unittest.TestCase):
    def test_transcribe_passes_selected_language_and_returns_duration(self):
        class Model:
            def transcribe(self, filename, **kwargs):
                self.filename = filename
                self.options = kwargs
                return iter([types.SimpleNamespace(text=" hello "), types.SimpleNamespace(text="world")]), (
                    types.SimpleNamespace(duration=12.5)
                )

        model = Model()
        text, duration = core.transcribe("recording.wav", model, "hi")

        self.assertEqual(text, "hello world")
        self.assertEqual(duration, 12.5)
        self.assertEqual(model.options, {"vad_filter": True, "language": "hi"})

    def test_find_media_skips_symlinks_outside_selected_folder(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selected = root / "selected"
            selected.mkdir()
            inside = selected / "inside.wav"
            inside.write_bytes(b"audio")
            outside = root / "outside.wav"
            outside.write_bytes(b"audio")
            try:
                (selected / "linked.wav").symlink_to(outside)
                (selected / "linked-folder").symlink_to(root, target_is_directory=True)
            except OSError:
                self.skipTest("Symlinks are not available in this environment.")

            self.assertEqual(core.find_media(selected, 24), [inside.resolve()])

    def test_summarize_keeps_chunked_map_reduce_and_action_limits(self):
        responses = [
            core.Digest(f"part {part}", [f"action {part}-{index}" for index in range(4)], [])
            for part in range(3)
        ]
        responses.append(core.Digest("combined summary", [], []))
        with patch.object(core, "_ask", side_effect=responses) as ask:
            summary, actions, points = core.summarize(
                "x" * 10001, "http://localhost:11434", "model"
            )

        self.assertEqual(summary, "combined summary")
        self.assertEqual(len(actions), 8)
        self.assertEqual(points, [])
        self.assertEqual(ask.call_count, 4)

    def test_run_digest_returns_and_saves_structured_results_and_progress(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = root / "meeting.wav"
            media.write_bytes(b"not real audio")
            digest = root / "out" / "digest.md"
            digest_json = root / "out" / "digest.json"
            events = []
            whisper_module = types.ModuleType("faster_whisper")
            whisper_module.WhisperModel = lambda *args, **kwargs: object()

            with (
                patch.dict("sys.modules", {"faster_whisper": whisper_module}),
                patch.object(core, "DIGEST_DIR", digest.parent),
                patch.object(core, "DIGEST", digest),
                patch.object(core, "DIGEST_JSON", digest_json),
                patch.object(core, "transcribe", return_value=("private transcript", 42.0)),
                patch.object(
                    core, "summarize",
                    return_value=core.Digest("A short summary.", ["Follow up"], []),
                ),
            ):
                results = core.run_digest(
                    root, 24, "en", "small", "local-model", False,
                    lambda done, total, filename, stage: events.append(
                        (done, total, filename, stage)
                    ),
                )

            self.assertEqual(
                results,
                [{
                    "file": str(media),
                    "time": results[0]["time"],
                    "duration_seconds": 42.0,
                    "summary": "A short summary.",
                    "actions": ["Follow up"],
                    "points": [],
                    "kind": "media",
                }],
            )
            self.assertEqual(
                [event[3] for event in events],
                ["listening", "summarizing", "done"],
            )
            self.assertEqual(events[-1][:2], (1, 1))
            self.assertEqual(json.loads(digest_json.read_text(encoding="utf-8")), results)
            self.assertIn("A short summary.", digest.read_text(encoding="utf-8"))
            self.assertNotIn("private transcript", digest.read_text(encoding="utf-8"))
            self.assertNotIn("private transcript", digest_json.read_text(encoding="utf-8"))

    def test_run_digest_refuses_non_local_model_host(self):
        with self.assertRaises(SystemExit):
            core.run_digest(".", 24, None, "small", "model", False, None,
                            host="https://example.invalid")

    def test_wipe_removes_both_digest_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            digest = root / "digest.md"
            digest_json = root / "digest.json"
            digest.write_text("digest", encoding="utf-8")
            digest_json.write_text("[]", encoding="utf-8")

            with (
                patch.object(core, "DIGEST", digest),
                patch.object(core, "DIGEST_JSON", digest_json),
                patch("sys.argv", ["sloth_ears", "--wipe"]),
            ):
                core.main()

            self.assertFalse(digest.exists())
            self.assertFalse(digest_json.exists())


if __name__ == "__main__":
    unittest.main()
