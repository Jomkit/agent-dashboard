from pathlib import Path
import unittest


class TestWebUIContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.web_dir = Path(__file__).resolve().parent.parent / "tracker" / "interfaces" / "web"
        cls.js_dir = cls.web_dir / "static" / "js"

    def test_web_files_exist(self):
        required_files = [
            self.web_dir / "index.html",
            self.web_dir / "static" / "css" / "style.css",
            self.js_dir / "store.js",
            self.js_dir / "sse.js",
            self.js_dir / "app.js",
            self.js_dir / "components" / "session-list.js",
            self.js_dir / "components" / "transcript-view.js",
        ]
        for f in required_files:
            self.assertTrue(f.is_file(), f"Required web file missing: {f}")

    def test_security_no_inner_html_or_eval(self):
        forbidden_patterns = [
            ".innerHTML", ".outerHTML", "insertAdjacentHTML(",
            "eval(", "new Function(", "document.write(",
            "setTimeout(", "setInterval(",
        ]
        js_files = list(self.js_dir.rglob("*.js")) + list(self.js_dir.rglob("*.mjs"))
        self.assertGreater(len(js_files), 0, "No JavaScript files found to verify")
        for js_file in js_files:
            content = js_file.read_text(encoding="utf-8")
            for pattern in forbidden_patterns:
                self.assertNotIn(
                    pattern,
                    content,
                    f"Security violation: Found '{pattern}' in {js_file.name}",
                )

    def test_security_uses_text_content(self):
        for component_name in ["session-list.js", "transcript-view.js"]:
            comp_file = self.js_dir / "components" / component_name
            self.assertTrue(comp_file.is_file(), f"Component file missing: {comp_file}")
            content = comp_file.read_text(encoding="utf-8")
            self.assertIn(
                "textContent",
                content,
                f"Component {component_name} must use textContent for safe text insertion",
            )

    def test_web_payload_budget(self):
        total_bytes = 0
        for f in self.web_dir.rglob("*"):
            if f.is_file() and f.suffix in (".html", ".css", ".js", ".mjs"):
                total_bytes += f.stat().st_size

        # Budget: under 30KB uncompressed
        self.assertLess(
            total_bytes,
            30 * 1024,
            f"Web assets exceeded 30KB budget: {total_bytes} bytes",
        )

    def test_index_html_structure(self):
        index_file = self.web_dir / "index.html"
        if index_file.is_file():
            content = index_file.read_text(encoding="utf-8")
            self.assertIn("<session-list", content)
            self.assertIn("<transcript-view", content)
            self.assertIn("app.js", content)


if __name__ == "__main__":
    unittest.main()
