import re
import unittest
from pathlib import Path


WEB_ROOT = Path(__file__).resolve().parents[1] / "web"


class FrontendTests(unittest.TestCase):
    def test_assets_use_no_remote_fonts_or_cdn(self):
        for filename in ("index.html", "style.css", "app.js"):
            with self.subTest(filename=filename):
                content = (WEB_ROOT / filename).read_text(encoding="utf-8")
                self.assertIsNone(
                    re.search(
                        r"https?://|@import|fonts\.googleapis|cdn\.tailwind|material-symbols",
                        content,
                        re.IGNORECASE,
                    )
                )

    def test_markup_has_required_controls_and_real_empty_state(self):
        markup = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="folder-path"', markup)
        self.assertIn('id="time-range"', markup)
        self.assertIn('id="language"', markup)
        self.assertIn('id="offline-toggle"', markup)
        self.assertIn("No digest yet. Take a nap.", markup)
        self.assertNotIn("Weekly_Team_Sync_May18.m4a", markup)

    def test_drop_zone_has_text_tab_output_choice_and_model_select(self):
        markup = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
        for snippet in (
            'id="tab-media"', ">\n            Audio / Video", 'id="tab-text"',
            'id="text-input"', 'id="text-file"', 'accept=".txt,.md',
            'id="output-style"', 'value="line">One line', 'value="points">Key points',
            'value="both">Both', '<select class="control" id="ai-model">',
        ):
            with self.subTest(snippet=snippet):
                self.assertIn(snippet, markup)
        self.assertNotIn("<datalist", markup)

    def test_app_sends_mode_and_style_and_renders_key_points(self):
        script = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
        for snippet in (
            "mode: state.mode", 'style: $("#output-style").value',
            "Recommended for multilingual", "point-list", "MAX_TEXT_CHARS = 200000",
        ):
            with self.subTest(snippet=snippet):
                self.assertIn(snippet, script)

    def test_app_uses_only_same_origin_api_paths(self):
        script = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
        for endpoint in ("/api/run", "/api/status", "/api/digest", "/api/models"):
            with self.subTest(endpoint=endpoint):
                self.assertIn(endpoint, script)


if __name__ == "__main__":
    unittest.main()
