# ollama-code

`ollama-code` is a lightweight terminal coding agent that uses local Ollama models to inspect a project, propose code changes, write files, run validation/tests, and optionally create a Git commit.

The project is intentionally simple: it is a single Python script designed for local, repo-based workflows without cloud dependencies.

## Features

- Runs directly in your terminal.
- Uses local models served by Ollama.
- Reads the current project and sends relevant file context to the model.
- Writes full-file updates returned by the model.
- Supports automatic planning before code generation.
- Supports dry-run mode to preview changes without keeping them.
- Can automatically commit generated changes with Git.
- Uses a single adaptive workflow with a `main_model` for code changes and a `review_model` for ratification.
- Uses English as the default CLI and prompt language.
- Keeps auto-generated `README.md` instruction sections in English.
- Shows cloud-model usage in the interactive prompt status bar when `prompt_toolkit` is available, using per-request compute-time metrics returned by Ollama.
- Automatically creates/updates `README.md` instructions for generated code changes.
- Inspects Ollama model metadata to estimate a safe prompt budget and keep project context within the active model limit.
- Validates modified files before keeping changes:
  - Python via `python3 -m py_compile`
  - JavaScript via `node --check`
  - PHP via `php -l`
  - Shell scripts via `bash -n`
- Runs project tests automatically when a runner is detected:
  - Python projects: `python3 -m pytest -q` (or `unittest` fallback)
  - Node projects: `pnpm test`, `npm test`, or `yarn test`
- If validation/tests or review ratification fail, reports the failure and retries with explicit feedback.
- Stores request summaries in history and the last raw model response for debugging and retries.
- Summarizes each new request with the active model, prints the summary, and saves that summary in history.
- Rolls back changes only if the unified workflow cannot converge within the allowed attempts.

## Requirements

- Python 3.9+
- [Ollama](https://ollama.com/) installed and running
- At least one Ollama model available locally

Optional but recommended:

- `prompt_toolkit` for a better interactive prompt and completions
- `prompt_toolkit` is also required for the interactive cloud-usage status bar
- `pytest` for Python test execution
- `node` if you want JavaScript syntax validation
- `php` if you want PHP syntax validation

## Installation

Clone the repository and make the script executable:

```bash
git clone https://github.com/your-name/ollama-code.git
cd ollama-code
chmod +x ollama-code
```

To use it from any repository, install it into your `PATH`:

```bash
install -m 755 ollama-code ~/.local/bin/ollama-code
```

Optional dependency for a richer interactive shell:

```bash
pip install prompt-toolkit
```

Make sure Ollama is running:

```bash
ollama serve
```

Pull a model if needed:

```bash
ollama pull qwen2.5-coder:latest
```

## Quick Start

Run the tool inside the repository you want to work on:

```bash
ollama-code
```

Then type a request such as:

```text
Add a README section that explains the configuration files.
```

The tool will:

1. Inspect the current repository.
2. Send the project context to Ollama.
3. Apply returned file changes.
4. Validate modified files and run project tests when available.
5. If checks fail, run an automatic corrective pass.
6. Generate/update `README.md` instructions for the generated code.
7. Optionally create a Git commit.

If the current directory is not a Git repository, `ollama-code` initializes one automatically.

## CLI Options

```bash
./ollama-code --help
```

Available options:

- `--ollama-url <url>`: set a custom Ollama endpoint (you can pass `http://` or `https://`). Default: `OLLAMA_HOST` or `http://127.0.0.1:11434` (auto-uses `https://` when host is `ollama.com`/`*.ollama.com` and no protocol is provided)
- `--plan`: ask Ollama for a plan before generating code
- `--dry-run`: show changes but restore the working tree afterward
- `--no-commit`: disable automatic Git commits
- `--no-readme`: disable automatic `README.md` instruction generation

## Interactive Commands

Inside the prompt you can use:

- `/f`: list project files
- `/s`: open a shell command prompt
- `/s <command>`: run a shell command directly
- `/set-endpoint`: show the current endpoint, prompt for a new one (empty input keeps current), reset `main_model`/`review_model` to automatic, and reload available models from the new endpoint
- `/diff`: show Git diff
- `/mainmodel`: choose the main Ollama model interactively (same autocomplete menu as `/`, auto-open)
- `/mainmodel <name|number|default>`: switch/reset the main model
- `/model`: alias for `/mainmodel`
- `/review-model`: choose the review model interactively (same autocomplete menu as `/`, auto-open)
- `/review-model <name|number|default>`: switch/reset the review model
- `/plan [on|off]`: set planning mode (interactive selector if omitted)
- `/dry [on|off]`: set dry-run mode (interactive selector if omitted)
- `/commit [on|off]`: set automatic commit mode (interactive selector if omitted)
- `/readme [on|off]`: set automatic `README.md` instruction generation (interactive selector if omitted)
- `/retry`: repeat the last request
- `/last`: show the last saved raw response
- `/history`: show recent request summaries
- `/q`: quit

If the autocomplete menu is not available in your terminal, model and on/off selections fall back to numbered choices.

When you are using an Ollama cloud model, the bottom status bar shows the latest cloud usage estimate and the running session total based on the usage durations returned by Ollama's API responses. This status bar is shown only in the richer `prompt_toolkit` prompt.

If you point the tool directly at `https://ollama.com/api`, set `OLLAMA_API_KEY`. If you use a local Ollama instance on `localhost`, Ollama's own sign-in flow continues to work as usual for cloud models.

## How It Works

`ollama-code` scans the current repository, skips internal state files, excluded directories, backup folders, binary files, symlinks, and oversized files, then builds a prompt from the remaining text files.

Before each generation or review pass, the tool asks Ollama for model details, derives a prompt budget from the available context window, prioritizes the most relevant files first, and requests an appropriate `num_ctx` for the call.

The main model answers with complete file blocks for updated files and delete blocks for removals. The tool writes those file states directly, validates changed files, runs tests when available, asks the review model to ratify the resulting diff, and retries with explicit validation/review feedback when needed.

The workflow is:

1. Optional plan with `main_model`
2. Code generation/fix pass with `main_model`
3. Validation and test execution
4. Diff review and ratification with `review_model`
5. Retry from the current state if validation or review fails
6. Roll back only if the workflow cannot converge

## Configuration

Model selection can come from:

1. The current session
2. `OLLAMA_MODEL`
3. Automatic discovery of installed Ollama models
4. Built-in preferred model choices

The Ollama endpoint can be configured with:

- `--ollama-url`
- `OLLAMA_HOST`

If no protocol is specified, `ollama-code` uses `http://` for local endpoints and `https://` for `ollama.com` cloud endpoints.

Optional test command override:

- `OLLAMA_CODE_TEST_COMMAND` (example: `OLLAMA_CODE_TEST_COMMAND="make test"`)

User configuration is stored in:

```text
~/.ollama-code/config.json
```

Saved settings include:

- `main_model`
- `review_model`
- `ollama_url`
- `plan_mode`
- `dry_run`
- `auto_commit`
- `auto_readme`

Interactive prompt history is stored in:

```text
~/.ollama-code/.ollama-history
```

Project/session state files include:

```text
.ollama-code-last-response.txt
.ollama-code-last-request.txt
.ollama-code-requests-history.txt
```

## Safety Notes

- The tool refuses absolute paths, parent-directory traversal, `.git` paths, and its own internal state files.
- Invalid generated code is retried from validation/review feedback and rolled back only if retries do not converge.
- Dry-run mode restores the original files after showing the changes.
- Automatic commits only include files written by the tool, excluding internal state files.

## Limitations

- The project currently ships as a single script, not a packaged Python module.
- Large repositories are truncated to fit prompt limits.
- Validation depends on external tools being installed for the target language.
- Output quality depends heavily on the selected Ollama model.

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.
