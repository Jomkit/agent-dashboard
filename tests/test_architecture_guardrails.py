import ast
import os
from pathlib import Path
import sys
import unittest

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestArchitectureGuardrails(unittest.TestCase):
    def setUp(self):
        self.tracker_dir = REPO_ROOT / "tracker"
        self.tests_dir = REPO_ROOT / "tests"

    def test_zero_external_dependencies_ast_scan(self):
        """Scans all python files to ensure imports are strictly standard library or internal."""
        stdlib_names = set(sys.stdlib_module_names) if hasattr(sys, "stdlib_module_names") else set()
        # Fallback for Python versions or platforms where stdlib_module_names might omit builtins
        allowed_internal = {"tracker", "tests"}

        py_files = list(self.tracker_dir.rglob("*.py")) + list(self.tests_dir.rglob("*.py"))
        self.assertTrue(len(py_files) > 0, "No python files found to scan!")

        for py_file in py_files:
            with open(py_file, "r", encoding="utf-8") as f:
                try:
                    tree = ast.parse(f.read(), filename=str(py_file))
                except SyntaxError as e:
                    self.fail(f"Syntax error in {py_file}: {e}")

            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        top_pkg = alias.name.split(".")[0]
                        self._assert_allowed_module(top_pkg, stdlib_names, allowed_internal, py_file)
                elif isinstance(node, ast.ImportFrom):
                    if node.level and node.level > 0:
                        # Relative import within package is allowed
                        continue
                    if node.module:
                        top_pkg = node.module.split(".")[0]
                        self._assert_allowed_module(top_pkg, stdlib_names, allowed_internal, py_file)

    def _assert_allowed_module(self, top_pkg, stdlib_names, allowed_internal, file_path):
        if stdlib_names and top_pkg in stdlib_names:
            return
        if top_pkg in allowed_internal:
            return
        # Known standard modules if stdlib_module_names is missing
        known_stdlib = {
            "os", "sys", "json", "sqlite3", "http", "urllib", "queue", "threading",
            "time", "datetime", "dataclasses", "enum", "typing", "pathlib", "ast",
            "unittest", "tempfile", "shutil", "socket", "hashlib", "re", "abc",
        }
        if top_pkg in known_stdlib:
            return
        self.fail(f"Forbidden third-party dependency '{top_pkg}' imported in {file_path}")

    def test_hexagonal_boundary_domain_and_ports(self):
        """Ensures tracker/domain/ and tracker/ports/ never import storage, web, or adapters."""
        forbidden_in_domain_ports = {
            "sqlite3", "http", "socket", "urllib", "tracker.adapters", "tracker.app"
        }

        domain_and_port_files = list((self.tracker_dir / "domain").rglob("*.py")) + list(
            (self.tracker_dir / "ports").rglob("*.py")
        )

        for py_file in domain_and_port_files:
            with open(py_file, "r", encoding="utf-8") as f:
                tree = ast.parse(f.read(), filename=str(py_file))

            for node in ast.walk(tree):
                imported_names = []
                if isinstance(node, ast.Import):
                    imported_names.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        imported_names.append(node.module)

                for name in imported_names:
                    for forbidden in forbidden_in_domain_ports:
                        if name == forbidden or name.startswith(forbidden + "."):
                            self.fail(
                                f"Hexagonal boundary violation: '{name}' imported in pure layer {py_file}"
                            )

    def test_web_budget_and_no_external_cdns(self):
        """Ensures tracker/web has zero external URLs and uncompressed size is under 30KB."""
        web_dir = self.tracker_dir / "web"
        if not web_dir.exists():
            return

        total_bytes = 0
        for root, _, files in os.walk(web_dir):
            for file in files:
                file_path = Path(root) / file
                total_bytes += file_path.stat().st_size
                if file.endswith((".html", ".js", ".css")):
                    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read()
                        self.assertNotIn("http://", content, f"External CDN / URL found in {file_path}")
                        self.assertNotIn("https://", content, f"External CDN / URL found in {file_path}")

        # Total uncompressed web asset budget: strictly < 30KB (30,720 bytes)
        self.assertLess(
            total_bytes,
            30720,
            f"Web assets total {total_bytes} bytes, exceeding 30KB uncompressed budget!"
        )


if __name__ == "__main__":
    unittest.main()
