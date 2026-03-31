# ollama-code

`ollama-code` is a lightweight terminal coding agent that uses local Ollama models to inspect a project, propose code changes, write files, validate syntax, and optionally create a Git commit.

The project is intentionally simple: it is a single Python script designed for local, repo-based workflows without cloud dependencies.

## Features

- Runs directly in your terminal.
- Uses local models served by Ollama.
- Reads the current project and sends relevant file context to the model.
- Writes full-file updates returned by the model.
- Supports automatic planning before code generation.
- Supports dry-run mode to preview changes without keeping them.
- Can automatically commit generated changes with Git.
- Includes an optional multi-pass workflow with draft, review, fix, and polish steps.
- Validates modified files before keeping changes:
  - Python via `python3 -m py_compile`
  - JavaScript via `node --check`
  - PHP via `php -l`
  - Shell scripts via `bash -n`
- Stores request history and the last raw model response for debugging and retries.

## Requirements

- Python 3.9+
- [Ollama](https://ollama.com/) installed and running
- At least one Ollama model available locally

Optional but recommended:

- `prompt_toolkit` for a better interactive prompt and completions
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
4. Validate modified files when supported.
5. Optionally create a Git commit.

If the current directory is not a Git repository, `ollama-code` initializes one automatically.

## CLI Options

```bash
./ollama-code --help
```

Available options:

- `--ollama-url <url>`: set a custom Ollama endpoint. Default: `OLLAMA_HOST` or `http://127.0.0.1:11434`
- `--plan`: ask Ollama for a plan before generating code
- `--dry-run`: show changes but restore the working tree afterward
- `--no-commit`: disable automatic Git commits
- `--no-engine`: disable the five-pass engine

The five-pass engine is currently optional and can also be enabled during a session with `/engine on`.

## Interactive Commands

Inside the prompt you can use:

- `/f`: list project files
- `/s`: open a shell command prompt
- `/s <command>`: run a shell command directly
- `/diff`: show Git diff
- `/model`: choose an Ollama model interactively
- `/model <name|number|default>`: switch model
- `/plan [on|off]`: toggle planning mode
- `/dry [on|off]`: toggle dry-run mode
- `/commit [on|off]`: toggle automatic commit mode
- `/engine [on|off]`: toggle the five-pass engine
- `/retry`: repeat the last request
- `/last`: show the last saved raw response
- `/history`: show recent request history
- `/q`: quit

## How It Works

`ollama-code` scans the current repository, skips internal state files, excluded directories, backup folders, binary files, symlinks, and oversized files, then builds a prompt from the remaining text files.

The model must answer with full file blocks in a strict format. The tool writes those files to disk, validates changed files when possible, and restores the previous state automatically if validation fails.

When enabled, the multi-pass engine runs this flow:

1. Plan
2. Draft
3. Review
4. Fix
5. Polish

## Configuration

Model selection can come from:

1. The current session
2. `OLLAMA_MODEL`
3. Automatic discovery of installed Ollama models
4. Built-in fallback model choices

The Ollama endpoint can be configured with:

- `--ollama-url`
- `OLLAMA_HOST`

User configuration is stored in:

```text
~/.ollama-code
```

Project/session state files include:

```text
.ollama-code-last-response.txt
.ollama-code-requests-history.txt
```

## Safety Notes

- The tool refuses absolute paths, parent-directory traversal, `.git` paths, and its own internal state files.
- Invalid generated code is rolled back automatically when validation fails.
- Dry-run mode restores the original files after showing the changes.
- Automatic commits only include files written by the tool, excluding internal state files.

## Limitations

- The project currently ships as a single script, not a packaged Python module.
- Large repositories are truncated to fit prompt limits.
- Validation depends on external tools being installed for the target language.
- Output quality depends heavily on the selected Ollama model.

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.
