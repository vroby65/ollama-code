import importlib.util
import json
import subprocess
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

    def test_collect_project_context_prioritizes_requested_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("r" * 220, encoding="utf-8")
            (root / "index.html").write_text("i" * 180, encoding="utf-8")

            context, stats = self.module.collect_project_context(
                root,
                max_chars=260,
                priority_files=["index.html"],
            )

        self.assertEqual(stats.included_files, ["index.html"])
        self.assertIn("===FILE: index.html===", context)

    def test_resolve_model_context_info_uses_model_info_when_runtime_context_is_missing(self):
        with mock.patch.object(
            self.module,
            "resolve_active_model",
            return_value="test-model",
        ), mock.patch.object(
            self.module,
            "discover_running_model_context_length",
            return_value=None,
        ), mock.patch.object(
            self.module,
            "fetch_model_details",
            return_value={
                "parameters": "",
                "model_info": {"llama.context_length": 131072},
            },
        ):
            info = self.module.resolve_model_context_info("test-model", "http://127.0.0.1:11434")

        self.assertEqual(info.context_window_tokens, 131072)
        self.assertEqual(info.source, "api-show-model-info")
        self.assertEqual(info.max_context_tokens, 131072)

    def test_resolve_model_context_info_prefers_running_context(self):
        with mock.patch.object(
            self.module,
            "resolve_active_model",
            return_value="test-model",
        ), mock.patch.object(
            self.module,
            "discover_running_model_context_length",
            return_value=8192,
        ), mock.patch.object(
            self.module,
            "fetch_model_details",
            return_value={
                "parameters": "num_ctx 2048",
                "model_info": {"llama.context_length": 131072},
            },
        ):
            info = self.module.resolve_model_context_info("test-model", "http://127.0.0.1:11434")

        self.assertEqual(info.model, "test-model")
        self.assertEqual(info.context_window_tokens, 8192)
        self.assertEqual(info.max_context_tokens, 131072)
        self.assertEqual(info.source, "api-ps")
        self.assertGreater(info.prompt_budget_chars, 0)

    def test_call_ollama_sends_num_ctx_option(self):
        captured = {}

        def fake_request_json(url, payload=None, headers=None, timeout=None):
            captured["payload"] = payload
            return {"response": "ok"}, None

        context_info = self.module.ModelContextInfo(
            model="test-model",
            context_window_tokens=8192,
            max_context_tokens=131072,
            source="api-show-model-info",
            prompt_budget_chars=24000,
        )

        with mock.patch.object(
            self.module,
            "resolve_active_model",
            return_value="test-model",
        ), mock.patch.object(
            self.module,
            "request_json",
            side_effect=fake_request_json,
        ), mock.patch.object(
            self.module,
            "record_response_usage",
        ):
            response = self.module.call_ollama(
                "x" * 12000,
                "http://127.0.0.1:11434",
                "test-model",
                context_info=context_info,
                response_format=self.module.CODE_RESPONSE_SCHEMA,
            )

        self.assertEqual(response.strip(), "ok")
        self.assertIn("options", captured["payload"])
        self.assertGreater(captured["payload"]["options"]["num_ctx"], 4096)
        self.assertEqual(captured["payload"]["format"], self.module.CODE_RESPONSE_SCHEMA)

    def test_run_review_workflow_retries_until_review_ok(self):
        apply_calls = []

        def fake_apply_pass(**kwargs):
            apply_calls.append(kwargs)
            return self.module.AppliedPassResult(
                response="draft",
                valid_response=True,
                written=["main.py"],
                changed_files=["main.py"],
                validation_ok=True,
            )

        context_info = self.module.ModelContextInfo(
            model="test-model",
            context_window_tokens=8192,
            prompt_budget_chars=24000,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(self.module, "apply_pass", side_effect=fake_apply_pass), mock.patch.object(
                self.module,
                "call_ollama",
                side_effect=[
                    "VERDICT: FIX\nISSUES:\n- edge case missing\nFIXES:\n- handle edge case\nFILES:\n- main.py",
                    "VERDICT: OK\nISSUES:\n- none\nFIXES:\n- none\nFILES:\n- none",
                ],
            ), mock.patch.object(
                self.module,
                "collect_context_for_prompt",
                return_value=("", self.module.ProjectContextStats(), context_info),
            ), mock.patch.object(
                self.module,
                "compute_changed_files",
                return_value=["main.py"],
            ):
                ok, written, plan_text = self.module.run_review_workflow(
                    root=root,
                    request="make a small change",
                    ollama_url="http://127.0.0.1:11434",
                    main_model="main-model",
                    review_model="review-model",
                    request_history="",
                    plan_mode=False,
                    backup={},
                )

        self.assertTrue(ok)
        self.assertEqual(written, ["main.py"])
        self.assertEqual(plan_text, "")
        self.assertEqual(len(apply_calls), 2)
        self.assertEqual(apply_calls[1]["review_text"], "VERDICT: FIX\nISSUES:\n- edge case missing\nFIXES:\n- handle edge case\nFILES:\n- main.py")

    def test_apply_pass_rolls_back_when_project_tests_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "demo.py"
            target.write_text("print('old')\n", encoding="utf-8")

            def fake_call_ollama(prompt, ollama_url, model, plan_mode=False, context_info=None, response_format=None):
                return json.dumps(
                    {
                        "operations": [
                            {
                                "action": "write",
                                "path": "demo.py",
                                "content": "print('new')\n",
                            }
                        ]
                    }
                )

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

    def test_check_unified_patch_detects_invalid_patch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)

            target = root / "demo.py"
            target.write_text("print('old')\n", encoding="utf-8")

            patch_text = (
                "--- a/demo.py\n"
                "+++ b/demo.py\n"
                "@@ -1 +1 @@\n"
                "-print('missing old line')\n"
                "+print('new')\n"
            )

            ok, error = self.module.check_unified_patch(root, patch_text)

        self.assertFalse(ok)
        self.assertTrue(error)

    def test_apply_unified_patch_updates_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)

            target = root / "demo.py"
            target.write_text("print('old')\n", encoding="utf-8")

            patch_text = (
                "--- a/demo.py\n"
                "+++ b/demo.py\n"
                "@@ -1 +1 @@\n"
                "-print('old')\n"
                "+print('new')\n"
            )

            ok, error = self.module.apply_unified_patch(root, patch_text)
            self.assertTrue(ok, error)
            self.assertEqual(target.read_text(encoding="utf-8"), "print('new')\n")

    def test_apply_pass_rejects_patch_response_when_json_is_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)

            def fake_call_ollama(prompt, ollama_url, model, plan_mode=False, context_info=None, response_format=None):
                return (
                    "===PATCH===\n"
                    "--- a/demo.py\n"
                    "+++ b/demo.py\n"
                    "@@ -1 +1 @@\n"
                    "-print('old')\n"
                    "+print('new')\n"
                    "===END===\n"
                )

            with mock.patch.object(self.module, "call_ollama", side_effect=fake_call_ollama), mock.patch.object(
                self.module,
                "check_unified_patch",
            ) as check_mock, mock.patch.object(
                self.module,
                "apply_unified_patch",
            ) as apply_mock:
                result = self.module.apply_pass(
                    root=root,
                    project_text="",
                    request="change demo.py",
                    ollama_url="http://127.0.0.1:11434",
                    model="test-model",
                    backup={},
                )

            self.assertFalse(result.valid_response)
            self.assertIn("structured JSON object", result.retry_feedback)
            check_mock.assert_not_called()
            apply_mock.assert_not_called()

    def test_apply_pass_applies_json_write_and_delete_operations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            keep = root / "keep.py"
            keep.write_text("print('old')\n", encoding="utf-8")
            old = root / "old.txt"
            old.write_text("obsolete\n", encoding="utf-8")

            def fake_call_ollama(prompt, ollama_url, model, plan_mode=False, context_info=None, response_format=None):
                return json.dumps(
                    {
                        "operations": [
                            {
                                "action": "write",
                                "path": "keep.py",
                                "content": "print('new')\n",
                            },
                            {
                                "action": "delete",
                                "path": "old.txt",
                            },
                        ]
                    }
                )

            with mock.patch.object(self.module, "call_ollama", side_effect=fake_call_ollama), mock.patch.object(
                self.module,
                "validate_written_files",
                return_value=(True, ["keep.py (python): ok"], []),
            ), mock.patch.object(
                self.module,
                "run_project_tests",
                return_value=(True, [], []),
            ):
                result = self.module.apply_pass(
                    root=root,
                    project_text="",
                    request="change and delete files",
                    ollama_url="http://127.0.0.1:11434",
                    model="test-model",
                    backup={},
                )

            self.assertTrue(result.validation_ok)
            self.assertEqual(result.changed_files, ["keep.py", "old.txt"])
            self.assertEqual(keep.read_text(encoding="utf-8"), "print('new')\n")
            self.assertFalse(old.exists())

    def test_apply_pass_applies_json_operations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "demo.py"
            target.write_text("print('old')\n", encoding="utf-8")

            def fake_call_ollama(prompt, ollama_url, model, plan_mode=False, context_info=None, response_format=None):
                return json.dumps(
                    {
                        "operations": [
                            {
                                "action": "write",
                                "path": "demo.py",
                                "content": "print('new')\n",
                            }
                        ]
                    }
                )

            with mock.patch.object(self.module, "call_ollama", side_effect=fake_call_ollama), mock.patch.object(
                self.module,
                "validate_written_files",
                return_value=(True, ["demo.py (python): ok"], []),
            ), mock.patch.object(
                self.module,
                "run_project_tests",
                return_value=(True, [], []),
            ):
                result = self.module.apply_pass(
                    root=root,
                    project_text="",
                    request="change demo.py",
                    ollama_url="http://127.0.0.1:11434",
                    model="test-model",
                    backup={},
                )

            self.assertTrue(result.validation_ok)
            self.assertEqual(result.changed_files, ["demo.py"])
            self.assertEqual(target.read_text(encoding="utf-8"), "print('new')\n")

    def test_apply_pass_rejects_invalid_json_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)

            def fake_call_ollama(prompt, ollama_url, model, plan_mode=False, context_info=None, response_format=None):
                return json.dumps(
                    {
                        "operations": [
                            {
                                "action": "write",
                                "path": "../evil.py",
                                "content": "print('x')\n",
                            }
                        ]
                    }
                )

            result = None
            with mock.patch.object(self.module, "call_ollama", side_effect=fake_call_ollama):
                result = self.module.apply_pass(
                    root=root,
                    project_text="",
                    request="attempt invalid path",
                    ollama_url="http://127.0.0.1:11434",
                    model="test-model",
                    backup={},
                )

        self.assertFalse(result.valid_response)
        self.assertIn("../evil.py", result.rejected)
        self.assertIn("Invalid file paths", result.retry_feedback)

    def test_apply_pass_rejects_invalid_json_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def fake_call_ollama(prompt, ollama_url, model, plan_mode=False, context_info=None, response_format=None):
                return json.dumps(
                    {
                        "operations": [
                            {
                                "action": "write",
                                "path": "demo.py",
                            }
                        ]
                    }
                )

            with mock.patch.object(self.module, "call_ollama", side_effect=fake_call_ollama):
                result = self.module.apply_pass(
                    root=root,
                    project_text="",
                    request="change demo.py",
                    ollama_url="http://127.0.0.1:11434",
                    model="test-model",
                    backup={},
                )

        self.assertFalse(result.valid_response)
        self.assertIn("missing content", result.retry_feedback.lower())

    def test_apply_pass_saves_last_response_for_last_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            with mock.patch.object(self.module, "call_ollama", return_value="NO_CHANGES"):
                result = self.module.apply_pass(
                    root=root,
                    project_text="",
                    request="no-op request",
                    ollama_url="http://127.0.0.1:11434",
                    model="test-model",
                    backup={},
                )

            saved = (root / self.module.LAST_RESPONSE_FILE).read_text(encoding="utf-8")

        self.assertTrue(result.no_changes)
        self.assertEqual(saved, "NO_CHANGES\n")

    def test_validate_written_files_skips_deleted_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            existing = root / "keep.py"
            existing.write_text("print('ok')\n", encoding="utf-8")

            with mock.patch.object(
                self.module,
                "validate_file",
                return_value=("python", True, "ok"),
            ) as validate_mock:
                ok, reports, failures = self.module.validate_written_files(
                    root,
                    ["deleted.py", "keep.py"],
                )

        self.assertTrue(ok)
        self.assertEqual(reports, ["keep.py (python): ok"])
        self.assertEqual(failures, [])
        validate_mock.assert_called_once_with(existing)

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
            self.assertIn(
                "Original request: recorded in `.ollama-code-last-request.txt` and request history.",
                readme,
            )

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

    def test_load_config_file_migrates_legacy_model_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".ollama-code"
            config_path.write_text(
                json.dumps(
                    {
                        "model": "qwen2.5-coder:latest",
                        "fallback_model": "mistral:latest",
                    }
                ),
                encoding="utf-8",
            )

            loaded = self.module.load_config_file(config_path)

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["main_model"], "qwen2.5-coder:latest")
        self.assertEqual(loaded["review_model"], "mistral:latest")

    def test_load_config_file_reads_runtime_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".ollama-code"
            config_path.write_text(
                json.dumps(
                    {
                        "main_model": "qwen2.5-coder:latest",
                        "review_model": "mistral:latest",
                        "ollama_url": "127.0.0.1:11434",
                        "plan_mode": True,
                        "dry_run": "true",
                        "auto_commit": "false",
                        "auto_readme": 0,
                    }
                ),
                encoding="utf-8",
            )

            loaded = self.module.load_config_file(config_path)

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["ollama_url"], "http://127.0.0.1:11434")
        self.assertTrue(loaded["plan_mode"])
        self.assertTrue(loaded["dry_run"])
        self.assertFalse(loaded["auto_commit"])
        self.assertFalse(loaded["auto_readme"])

    def test_default_ollama_url_keeps_explicit_protocol(self):
        self.assertEqual(
            self.module.default_ollama_url("https://ollama.com/api"),
            "https://ollama.com/api",
        )
        self.assertEqual(
            self.module.default_ollama_url("http://127.0.0.1:11434"),
            "http://127.0.0.1:11434",
        )

    def test_default_ollama_url_uses_https_for_cloud_host_without_protocol(self):
        self.assertEqual(
            self.module.default_ollama_url("ollama.com"),
            "https://ollama.com",
        )
        self.assertEqual(
            self.module.default_ollama_url("api.ollama.com/api"),
            "https://api.ollama.com/api",
        )

    def test_discover_ollama_models_does_not_fallback_to_local_for_cloud_endpoint(self):
        with mock.patch.object(
            self.module,
            "fetch_json",
            return_value=None,
        ), mock.patch.object(
            self.module,
            "tool_exists",
            return_value=True,
        ) as tool_exists_mock, mock.patch.object(
            self.module,
            "run",
        ) as run_mock:
            models = self.module.discover_ollama_models("https://ollama.com")

        self.assertEqual(models, [])
        tool_exists_mock.assert_not_called()
        run_mock.assert_not_called()

    def test_discover_ollama_models_falls_back_to_local_for_non_cloud_endpoint(self):
        fake_cp = mock.Mock(
            returncode=0,
            stdout="NAME               ID           SIZE      MODIFIED\nqwen3.5:latest     abc123       4.7 GB    now\n",
            stderr="",
        )
        with mock.patch.object(
            self.module,
            "fetch_json",
            return_value=None,
        ), mock.patch.object(
            self.module,
            "tool_exists",
            return_value=True,
        ), mock.patch.object(
            self.module,
            "run",
            return_value=fake_cp,
        ) as run_mock:
            models = self.module.discover_ollama_models("http://127.0.0.1:11434")

        self.assertEqual(models, ["qwen3.5:latest"])
        run_mock.assert_called_once()

    def test_slash_menu_includes_set_endpoint_command(self):
        self.assertIn("/set-endpoint", self.module.SLASH_COMMANDS)
        self.assertEqual(
            self.module.SLASH_COMMAND_DESCRIPTIONS.get("/set-endpoint"),
            "change Ollama endpoint",
        )

    def test_slash_menu_includes_review_model_command(self):
        self.assertIn("/review-model", self.module.SLASH_COMMANDS)
        self.assertEqual(
            self.module.SLASH_COMMAND_DESCRIPTIONS.get("/review-model"),
            "change review model",
        )

    def test_model_completion_offers_choices_without_trailing_space(self):
        if self.module.Completion is None:
            self.skipTest("prompt_toolkit Completion unavailable")

        config = self.module.build_runtime_config()
        completer = self.module.SlashCommandCompleter(config, "http://127.0.0.1:11434")
        document = type("Doc", (), {"text_before_cursor": "/mainmodel"})()

        with mock.patch.object(
            self.module,
            "discover_model_choices",
            return_value=(["qwen3.5:latest"], None, True),
        ):
            completions = list(completer.get_completions(document, None))

        texts = [completion.text for completion in completions]
        self.assertIn(" default", texts)
        self.assertIn(" qwen3.5:latest", texts)

    def test_review_model_completion_offers_choices_without_trailing_space(self):
        if self.module.Completion is None:
            self.skipTest("prompt_toolkit Completion unavailable")

        config = self.module.build_runtime_config()
        completer = self.module.SlashCommandCompleter(config, "http://127.0.0.1:11434")
        document = type("Doc", (), {"text_before_cursor": "/review-model"})()

        with mock.patch.object(
            self.module,
            "discover_model_choices",
            return_value=(["mistral:latest"], None, True),
        ):
            completions = list(completer.get_completions(document, None))

        texts = [completion.text for completion in completions]
        self.assertIn(" default", texts)
        self.assertIn(" mistral:latest", texts)

    def test_model_alias_completion_offers_choices_without_trailing_space(self):
        if self.module.Completion is None:
            self.skipTest("prompt_toolkit Completion unavailable")

        config = self.module.build_runtime_config()
        completer = self.module.SlashCommandCompleter(config, "http://127.0.0.1:11434")
        document = type("Doc", (), {"text_before_cursor": "/model"})()

        with mock.patch.object(
            self.module,
            "discover_model_choices",
            return_value=(["mistral:latest"], None, True),
        ):
            completions = list(completer.get_completions(document, None))

        texts = [completion.text for completion in completions]
        self.assertIn(" default", texts)
        self.assertIn(" mistral:latest", texts)

    def test_prompt_model_selection_uses_prompt_input_with_completer(self):
        if self.module.Completion is None:
            self.skipTest("prompt_toolkit Completion unavailable")

        with mock.patch.object(
            self.module,
            "discover_model_choices",
            return_value=(["qwen3.5:latest"], None, True),
        ), mock.patch.object(
            self.module,
            "get_prompt_session",
            return_value=mock.Mock(),
        ), mock.patch.object(
            self.module,
            "prompt_input",
            return_value="qwen3.5:latest",
        ) as prompt_input_mock:
            ok, selected = self.module.prompt_model_selection(
                ollama_url="http://127.0.0.1:11434",
                current_model=None,
            )

        self.assertTrue(ok)
        self.assertEqual(selected, "qwen3.5:latest")
        self.assertIsNotNone(prompt_input_mock.call_args.kwargs.get("completer"))
        self.assertTrue(prompt_input_mock.call_args.kwargs.get("open_completion_menu"))

    def test_prompt_model_selection_falls_back_to_numeric_when_menu_unavailable(self):
        with mock.patch.object(
            self.module,
            "discover_model_choices",
            return_value=(["qwen3.5:latest"], None, True),
        ), mock.patch.object(
            self.module,
            "get_prompt_session",
            return_value=None,
        ), mock.patch.object(
            self.module,
            "prompt_input",
        ) as prompt_input_mock, mock.patch(
            "builtins.input",
            return_value="1",
        ):
            ok, selected = self.module.prompt_model_selection(
                ollama_url="http://127.0.0.1:11434",
                current_model=None,
            )

        self.assertTrue(ok)
        self.assertEqual(selected, "qwen3.5:latest")
        prompt_input_mock.assert_not_called()

    def test_prompt_toggle_selection_uses_prompt_input_with_auto_menu(self):
        if self.module.Completion is None:
            self.skipTest("prompt_toolkit Completion unavailable")

        with mock.patch.object(
            self.module,
            "get_prompt_session",
            return_value=mock.Mock(),
        ), mock.patch.object(
            self.module,
            "prompt_input",
            return_value="off",
        ) as prompt_input_mock:
            ok, selected = self.module.prompt_toggle_selection("/plan", True)

        self.assertTrue(ok)
        self.assertFalse(selected)
        self.assertTrue(prompt_input_mock.call_args.kwargs.get("open_completion_menu"))

    def test_prompt_toggle_selection_falls_back_to_numeric_when_menu_unavailable(self):
        with mock.patch.object(
            self.module,
            "get_prompt_session",
            return_value=None,
        ), mock.patch.object(
            self.module,
            "prompt_input",
        ) as prompt_input_mock, mock.patch(
            "builtins.input",
            return_value="0",
        ):
            ok, selected = self.module.prompt_toggle_selection("/plan", True)

        self.assertTrue(ok)
        self.assertFalse(selected)
        prompt_input_mock.assert_not_called()

    def test_save_config_writes_all_runtime_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp)
            with mock.patch.object(self.module.Path, "home", return_value=fake_home):
                self.module.save_config(
                    fake_home,
                    {
                        "main_model": "qwen2.5-coder:latest",
                        "review_model": "mistral:latest",
                        "ollama_url": "http://127.0.0.1:11434",
                        "plan_mode": True,
                        "dry_run": True,
                        "auto_commit": False,
                        "auto_readme": False,
                    },
                )

            saved_path = fake_home / ".ollama-code" / "config.json"
            self.assertTrue(saved_path.exists())
            saved = json.loads(saved_path.read_text(encoding="utf-8"))

        self.assertEqual(saved["main_model"], "qwen2.5-coder:latest")
        self.assertEqual(saved["review_model"], "mistral:latest")
        self.assertEqual(saved["ollama_url"], "http://127.0.0.1:11434")
        self.assertTrue(saved["plan_mode"])
        self.assertTrue(saved["dry_run"])
        self.assertFalse(saved["auto_commit"])
        self.assertFalse(saved["auto_readme"])

    def test_pick_auto_model_prefers_preferred_local_models(self):
        selected = self.module.pick_auto_model(["qwen3.5:latest", "mistral:latest"])
        self.assertEqual(selected, "qwen3.5:latest")

    def test_summarize_request_for_history_uses_model_response(self):
        with mock.patch.object(
            self.module,
            "call_ollama",
            return_value="SUMMARY: aggiorna autenticazione e aggiungi test",
        ):
            summary = self.module.summarize_request_for_history(
                "sistema login e aggiungi test",
                "http://127.0.0.1:11434",
                "test-model",
            )

        self.assertEqual(summary, "aggiorna autenticazione e aggiungi test")

    def test_summarize_request_for_history_falls_back_to_request(self):
        request = "aggiorna endpoint API per includere controllo token e log strutturato"
        with mock.patch.object(self.module, "call_ollama", return_value=""):
            summary = self.module.summarize_request_for_history(
                request,
                "http://127.0.0.1:11434",
                "test-model",
            )

        self.assertEqual(summary, self.module.one_line_summary(request))

    def test_make_commit_message_uses_runtime_endpoint_and_model(self):
        fake_cp = mock.Mock(stdout="diff --git a/demo.py b/demo.py\n", stderr="", returncode=0)
        with mock.patch.object(self.module, "run", return_value=fake_cp), mock.patch.object(
            self.module,
            "call_ollama",
            return_value="SUMMARY: update demo behavior\nDETAILS:\n- changed demo.py",
        ) as call_mock:
            message = self.module.make_commit_message(
                root=Path("."),
                request="update demo behavior",
                written=["demo.py"],
                ollama_url="http://127.0.0.1:11434",
                model="qwen3.5:latest",
            )

        self.assertTrue(message.startswith("update demo behavior"))
        call_mock.assert_called_once_with(
            mock.ANY,
            "http://127.0.0.1:11434",
            "qwen3.5:latest",
            plan_mode=False,
        )

    def test_process_request_saves_summary_in_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            with mock.patch.object(
                self.module,
                "build_request_history_context",
                return_value="",
            ), mock.patch.object(
                self.module,
                "summarize_request_for_history",
                return_value="sintesi breve",
            ) as summary_mock, mock.patch.object(
                self.module,
                "append_request_history",
            ) as append_mock, mock.patch.object(
                self.module,
                "run_review_workflow",
                return_value=(True, [], ""),
            ):
                self.module.process_request(
                    root=root,
                    request="richiesta completa da riassumere",
                    ollama_url="http://127.0.0.1:11434",
                    main_model="test-model",
                    review_model="review-model",
                    plan_mode=False,
                    dry_run=False,
                    auto_commit=False,
                    auto_readme=False,
                )

            saved_last_request = (root / self.module.LAST_REQUEST_FILE).read_text(encoding="utf-8")
            self.assertEqual(saved_last_request, "richiesta completa da riassumere\n")

        summary_mock.assert_called_once_with(
            "richiesta completa da riassumere",
            "http://127.0.0.1:11434",
            "test-model",
        )
        append_mock.assert_called_once_with(root, "sintesi breve")

    def test_load_last_request_reads_saved_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.module.save_last_request(root, "esegui refactor parser")
            loaded = self.module.load_last_request(root)

        self.assertEqual(loaded, "esegui refactor parser")

    def test_run_review_workflow_rolls_back_when_unresolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context_info = self.module.ModelContextInfo(
                model="test-model",
                context_window_tokens=8192,
                prompt_budget_chars=24000,
            )

            with mock.patch.object(
                self.module,
                "collect_context_for_prompt",
                return_value=("", self.module.ProjectContextStats(), context_info),
            ), mock.patch.object(
                self.module,
                "call_ollama",
                return_value="VERDICT: FIX\nISSUES:\n- mismatch\nFIXES:\n- adjust\nFILES:\n- app.py",
            ), mock.patch.object(
                self.module,
                "apply_pass",
                return_value=self.module.AppliedPassResult(
                    response="===FILE: app.py===\nprint('ok')\n===END===\n",
                    valid_response=True,
                    validation_ok=True,
                    changed_files=["app.py"],
                ),
            ) as apply_mock, mock.patch.object(
                self.module,
                "compute_changed_files",
                side_effect=[["app.py"]] * (self.module.WORKFLOW_MAX_ATTEMPTS * 3) + [[]],
            ), mock.patch.object(
                self.module,
                "restore_files",
            ) as restore_mock:
                ok, written, _ = self.module.run_review_workflow(
                    root=root,
                    request="sistema endpoint",
                    ollama_url="http://127.0.0.1:11434",
                    main_model="main-model",
                    review_model="review-model",
                    request_history="",
                    plan_mode=False,
                    backup={},
                )

        self.assertFalse(ok)
        self.assertEqual(written, [])
        self.assertEqual(apply_mock.call_count, self.module.WORKFLOW_MAX_ATTEMPTS)
        restore_mock.assert_called_once()

    def test_main_does_not_save_config_on_startup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime_config = self.module.build_runtime_config()
            runtime_config["ollama_url"] = None

            with mock.patch.object(self.module, "ensure_git_repo", return_value=root), mock.patch.object(
                self.module,
                "load_config",
                return_value=runtime_config,
            ), mock.patch.object(
                self.module,
                "setup_readline",
            ), mock.patch.object(
                self.module,
                "show_model_status",
            ), mock.patch.object(
                self.module,
                "prompt_input",
                side_effect=EOFError,
            ), mock.patch.object(
                self.module,
                "save_config",
            ) as save_config_mock, mock.patch.object(
                sys,
                "argv",
                ["ollama-code"],
            ):
                self.module.main()

        save_config_mock.assert_not_called()

    def test_main_set_endpoint_reloads_models_and_resets_model_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime_config = self.module.build_runtime_config()
            runtime_config["main_model"] = "old-model:latest"
            runtime_config["review_model"] = "old-review:latest"

            with mock.patch.object(self.module, "ensure_git_repo", return_value=root), mock.patch.object(
                self.module,
                "load_config",
                return_value=runtime_config,
            ), mock.patch.object(
                self.module,
                "setup_readline",
            ), mock.patch.object(
                self.module,
                "show_model_status",
            ) as show_model_status_mock, mock.patch.object(
                self.module,
                "discover_ollama_models",
                return_value=["qwen3.5:latest"],
            ) as discover_models_mock, mock.patch.object(
                self.module,
                "prompt_input",
                side_effect=["/set-endpoint", "https://ollama.com", EOFError],
            ), mock.patch.object(
                self.module,
                "save_config",
            ) as save_config_mock, mock.patch.object(
                sys,
                "argv",
                ["ollama-code"],
            ):
                self.module.main()

        discover_models_mock.assert_called_once_with("https://ollama.com")
        save_config_mock.assert_called_once()
        saved_config = save_config_mock.call_args.args[1]
        self.assertEqual(saved_config["ollama_url"], "https://ollama.com")
        self.assertIsNone(saved_config["main_model"])
        self.assertIsNone(saved_config["review_model"])
        self.assertGreaterEqual(show_model_status_mock.call_count, 2)
        self.assertEqual(show_model_status_mock.call_args_list[-1], mock.call(None, None, "https://ollama.com"))

    def test_main_set_endpoint_keeps_current_when_input_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime_config = self.module.build_runtime_config()
            runtime_config["ollama_url"] = "http://127.0.0.1:11434"

            with mock.patch.object(self.module, "ensure_git_repo", return_value=root), mock.patch.object(
                self.module,
                "load_config",
                return_value=runtime_config,
            ), mock.patch.object(
                self.module,
                "setup_readline",
            ), mock.patch.object(
                self.module,
                "show_model_status",
            ), mock.patch.object(
                self.module,
                "discover_ollama_models",
            ) as discover_models_mock, mock.patch.object(
                self.module,
                "prompt_input",
                side_effect=["/set-endpoint", "", EOFError],
            ), mock.patch.object(
                self.module,
                "save_config",
            ) as save_config_mock, mock.patch.object(
                sys,
                "argv",
                ["ollama-code"],
            ):
                self.module.main()

        discover_models_mock.assert_not_called()
        save_config_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
