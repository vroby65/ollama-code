import importlib.util
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock


def load_module():
    script_path = Path(__file__).resolve().parents[1] / "ollama-code"
    loader = SourceFileLoader("ollama_code_script", str(script_path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise RuntimeError("Unable to load module spec")

    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


class OllamaCodeTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module()

    def test_clean_ollama_output_preserves_blank_lines_in_file_blocks(self):
        raw = (
            "thinking...\n"
            "```text\n"
            "===FILE: app.py===\n"
            "def hello():\n"
            "\n"
            "    return 'ok'\n"
            "===END===\n"
            "```\n"
        )

        cleaned = self.module.clean_ollama_output(raw)

        expected = (
            "===FILE: app.py===\n"
            "def hello():\n"
            "\n"
            "    return 'ok'\n"
            "===END===\n"
        )
        self.assertEqual(cleaned, expected)

    def test_collect_project_context_tracks_truncated_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "big.py"
            target.write_text("x" * 80, encoding="utf-8")

            with mock.patch.object(self.module, "MAX_FILE_CHARS", 20), mock.patch.object(
                self.module, "MAX_PROMPT_CHARS", 2000
            ):
                _, stats = self.module.collect_project_context(root)

        self.assertEqual(stats.truncated_files, ["big.py"])

    def test_run_five_pass_engine_runs_only_three_apply_passes(self):
        apply_calls = []

        def fake_apply_pass(**kwargs):
            apply_calls.append(kwargs)
            idx = len(apply_calls)

            if idx == 1:
                return self.module.AppliedPassResult(
                    response="draft",
                    valid_response=True,
                    written=["main.py"],
                    changed_files=["main.py"],
                    validation_ok=True,
                )

            if idx == 2:
                return self.module.AppliedPassResult(
                    response="fix",
                    valid_response=True,
                    written=[],
                    changed_files=[],
                    validation_ok=True,
                    no_changes=True,
                )

            return self.module.AppliedPassResult(
                response="polish",
                valid_response=True,
                written=[],
                changed_files=[],
                validation_ok=True,
                no_changes=True,
            )

        def fake_call_ollama(prompt, ollama_url, model, plan_mode=False):
            if plan_mode:
                return "PLAN:\n- objective"
            return "VERDICT: OK\nISSUES:\n- none\nFIXES:\n- none\nFILES:\n- none"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(self.module, "apply_pass", side_effect=fake_apply_pass), mock.patch.object(
                self.module, "call_ollama", side_effect=fake_call_ollama
            ), mock.patch.object(
                self.module,
                "collect_project_context",
                return_value=("", self.module.ProjectContextStats()),
            ), mock.patch.object(
                self.module, "build_session_diff", return_value=""
            ):
                ok, written = self.module.run_five_pass_engine(
                    root=root,
                    project_text="",
                    request="make a small change",
                    ollama_url="http://127.0.0.1:11434",
                    model="test-model",
                    request_history="",
                    backup={},
                )

        self.assertTrue(ok)
        self.assertEqual(written, ["main.py"])
        self.assertEqual(len(apply_calls), 3)
        self.assertEqual(apply_calls[0]["plan_text"], "PLAN:\n- objective")

    def test_apply_pass_rolls_back_when_project_tests_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "demo.py"
            target.write_text("print('old')\n", encoding="utf-8")

            def fake_call_ollama(prompt, ollama_url, model, plan_mode=False):
                return "===FILE: demo.py===\nprint('new')\n===END===\n"

            with mock.patch.object(self.module, "call_ollama", side_effect=fake_call_ollama), mock.patch.object(
                self.module,
                "validate_written_files",
                return_value=(True, ["demo.py (python): ok"], []),
            ), mock.patch.object(
                self.module,
                "run_project_tests",
                return_value=(False, ["tests (pytest): failed"], ["tests (pytest): failed"]),
            ):
                result = self.module.apply_pass(
                    root=root,
                    project_text="",
                    request="change demo.py",
                    ollama_url="http://127.0.0.1:11434",
                    model="test-model",
                    backup={},
                )

            self.assertFalse(result.validation_ok)
            self.assertEqual(result.test_failures, ["tests (pytest): failed"])
            self.assertEqual(result.changed_files, [])
            self.assertEqual(target.read_text(encoding="utf-8"), "print('old')\n")

    def test_generate_readme_instructions_for_changes_creates_readme(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            changed = root / "src.py"
            changed.write_text("print('ok')\n", encoding="utf-8")
            backup = {}

            with mock.patch.object(
                self.module,
                "detect_test_command",
                return_value=("python3 -m unittest discover -s tests -p 'test_*.py' -q", None),
            ):
                updated, readme_rel = self.module.generate_readme_instructions_for_changes(
                    root=root,
                    request="aggiungi logging strutturato",
                    changed_files=["src.py"],
                    backup=backup,
                )

            self.assertTrue(updated)
            self.assertEqual(readme_rel, "README.md")
            self.assertIn("README.md", backup)
            readme = (root / "README.md").read_text(encoding="utf-8")
            self.assertIn(self.module.GENERATED_README_START, readme)
            self.assertIn("`src.py`", readme)
            self.assertIn("aggiungi logging strutturato", readme)

    def test_upsert_generated_readme_section_replaces_previous_block(self):
        existing = (
            "# README\n\n"
            f"{self.module.GENERATED_README_START}\n"
            "old section\n"
            f"{self.module.GENERATED_README_END}\n"
        )
        updated = self.module.upsert_generated_readme_section(existing, "new section")

        self.assertIn("new section", updated)
        self.assertNotIn("old section", updated)
        self.assertEqual(updated.count(self.module.GENERATED_README_START), 1)
        self.assertEqual(updated.count(self.module.GENERATED_README_END), 1)


if __name__ == "__main__":
    unittest.main()
