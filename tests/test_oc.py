import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("oc_module", ROOT / "oc.py")
oc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(oc)


class ActionApplicationTests(unittest.TestCase):
    def test_write_file_reports_only_real_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            target = repo_path / "app.py"
            target.write_text("print('old')\n", encoding="utf-8")

            unchanged = oc.apply_actions(
                repo_path,
                [{"type": "write_file", "path": "app.py", "content": "print('old')\n"}],
            )
            changed = oc.apply_actions(
                repo_path,
                [{"type": "write_file", "path": "app.py", "content": "print('new')\n"}],
            )

            self.assertEqual(unchanged, [])
            self.assertEqual(changed, ["app.py"])
            self.assertEqual(target.read_text(encoding="utf-8"), "print('new')\n")

    def test_empty_append_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            changed = oc.apply_actions(
                repo_path,
                [{"type": "append_file", "path": "notes.txt", "content": ""}],
            )

            self.assertEqual(changed, [])
            self.assertFalse((repo_path / "notes.txt").exists())

    def test_protected_and_external_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)

            with self.assertRaisesRegex(RuntimeError, "outside"):
                oc.safe_target_path(repo_path, "../escape.py")
            with self.assertRaisesRegex(RuntimeError, "README.md"):
                oc.safe_target_path(repo_path, "docs/README.md")


class ValidationTests(unittest.TestCase):
    def test_extensionless_python_script_is_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            script = repo_path / "tool"
            script.write_text("#!/usr/bin/env python3\nif True print('bad')\n", encoding="utf-8")

            validation = oc.run_code_checks(repo_path, ["tool"])

            self.assertFalse(validation["ok"])
            self.assertEqual(validation["checks"][0]["name"], "py_compile")

    def test_failed_previous_validation_rechecks_workspace_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            (repo_path / "broken.py").write_text("if True print('bad')\n", encoding="utf-8")

            targets = oc.validation_targets_after_changes(
                repo_path,
                [],
                {"ok": False, "checks": [{"name": "py_compile", "ok": False}]},
            )

            self.assertEqual(targets, ["broken.py"])


if __name__ == "__main__":
    unittest.main()
