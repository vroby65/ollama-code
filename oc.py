#!/usr/bin/env python3
import json
import os
import re
import shlex
import socket
import shutil
import subprocess
import sys
import textwrap
import threading
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path


CONFIG_FILE = ".config"
SPECS_FILE = "specs.txt"
REPORT_FILE = "report.txt"
README_FILE = "README.md"
LEGACY_REPORT_FILE = "oc-report.md"
APP_NAME = "Ollama Code"
APP_ABBR = "oc"
DEFAULT_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "llama3.1:8b"
DEFAULT_TEMPERATURE = 0.2
MAX_FILE_CHARS = 8000
MAX_TOTAL_CHARS = 30000
MAX_FILES_IN_CONTEXT = 24
MAX_ITERATIONS = 3
MODEL_REQUEST_TIMEOUT = 180
MODEL_STEP_RETRIES = 3
MODEL_RETRY_DELAY_SECONDS = 2
ANSI_PATTERN = re.compile(r"\x1b\[[0-9;]*m")
PRINT_LOCK = threading.Lock()
CODE_EXTENSIONS = {
    ".py",
    ".js",
    ".mjs",
    ".cjs",
    ".jsx",
    ".ts",
    ".tsx",
    ".html",
    ".htm",
    ".css",
    ".scss",
    ".sass",
    ".sh",
    ".bash",
    ".zsh",
    ".json",
    ".toml",
    ".yaml",
    ".yml",
    ".go",
    ".rs",
    ".java",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".php",
    ".rb",
    ".swift",
    ".kt",
    ".sql",
}

TERM_STYLES = {
    "reset": "0",
    "bold": "1",
    "dim": "2",
    "panel": "38;5;60",
    "accent": "38;5;81",
    "prompt": "38;5;151",
    "model": "38;5;213",
    "url": "38;5;75",
    "success": "38;5;120",
    "warning": "38;5;214",
    "error": "38;5;203",
    "muted": "38;5;244",
    "note": "38;5;180",
    "logo": "96;48;5;54",
}

OLLAMA_BANNER = r"""
╭────────╮     ╭──────╮
│ ╭────╮ │   ╭─╯ ╭────╯
│ │    │ │   │   │
│ │    │ │   │   │
│ ╰────╯ │   ╰─╮ ╰────╮
╰────────╯     ╰──────╯
""".strip("\n").splitlines()

SPINNER_FRAMES = [
    "[=     ]",
    "[==    ]",
    "[===   ]",
    "[ ===  ]",
    "[  === ]",
    "[   ===]",
    "[    ==]",
    "[     =]",
]

EDITOR_FALLBACKS = [
    ["nano"],
    ["micro"],
    ["vim"],
    ["vi"],
    ["notepad"],
]


def println(message=""):
    with PRINT_LOCK:
        print(message, flush=True)


def explain_missing_path(exc, fallback=None):
    missing = getattr(exc, "filename", None) or fallback
    if missing:
        return f"file or directory not found: {missing}"
    return "file or directory not found"


def supports_color(stream=None):
    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR") is not None:
        return False
    return bool(getattr(stream, "isatty", lambda: False)()) and os.environ.get("TERM") != "dumb"


def paint(text, *styles):
    if not text:
        return text
    if not supports_color():
        return text
    prefix = "".join(f"\033[{style}m" for style in styles if style)
    return f"{prefix}{text}\033[{TERM_STYLES['reset']}m"


def strip_ansi(text):
    return ANSI_PATTERN.sub("", text)


def terminal_columns():
    return shutil.get_terminal_size(fallback=(80, 24)).columns


def rule(char="-"):
    return char * min(terminal_columns(), 78)


def section_title(title):
    return paint(title, TERM_STYLES["bold"], TERM_STYLES["accent"])


def muted(text):
    return paint(text, TERM_STYLES["dim"], TERM_STYLES["muted"])


def ok_text(text="OK"):
    return paint(text, TERM_STYLES["bold"], TERM_STYLES["success"])


def warning_text(text):
    return paint(text, TERM_STYLES["bold"], TERM_STYLES["warning"])


def error_text(text):
    return paint(text, TERM_STYLES["bold"], TERM_STYLES["error"])


def format_path(path):
    return paint(str(path), TERM_STYLES["bold"], TERM_STYLES["accent"])


def format_model_name(name):
    return paint(str(name), TERM_STYLES["bold"], TERM_STYLES["model"])


def format_url(url):
    return paint(str(url), TERM_STYLES["url"])


def format_status(ok):
    return ok_text("ok") if ok else error_text("error")


def info_text(text):
    return paint(text, TERM_STYLES["note"])


def format_prompt(label, default=None):
    prompt = paint(label, TERM_STYLES["bold"], TERM_STYLES["prompt"])
    if default is not None:
        prompt += " " + muted(f"[{default}]")
    return f"{prompt}: "


def format_input_marker():
    return paint(f"{APP_ABBR}> ", TERM_STYLES["bold"], TERM_STYLES["prompt"])


def format_check_line(check):
    return f"- {paint(check['name'], TERM_STYLES['bold'], TERM_STYLES['note'])}: {format_status(check['ok'])}"


def print_ollama_banner(config):
    println(paint(rule("="), TERM_STYLES["panel"]))
    for line in OLLAMA_BANNER:
        println(paint(line, TERM_STYLES["logo"]))
    println("  " + paint(f"{APP_NAME} ({APP_ABBR})", TERM_STYLES["bold"], TERM_STYLES["prompt"]))
    println(
        "  "
        + paint("model", TERM_STYLES["bold"], TERM_STYLES["note"])
        + f"={format_model_name(config['model'])}  "
        + paint("base", TERM_STYLES["bold"], TERM_STYLES["note"])
        + f"={format_url(config['base_url'])}  "
        + paint("temp", TERM_STYLES["bold"], TERM_STYLES["note"])
        + f"={paint(str(config.get('temperature', DEFAULT_TEMPERATURE)), TERM_STYLES['warning'])}"
    )
    println(paint(rule("="), TERM_STYLES["panel"]))


class AnimatedStatus:
    def __init__(self, label):
        self.label = label
        self.enabled = supports_color() and sys.stdout.isatty()
        self._done = threading.Event()
        self._thread = None
        self._last_width = 0

    def _render(self):
        frame_count = len(SPINNER_FRAMES)
        tick = 0
        while not self._done.is_set():
            frame = SPINNER_FRAMES[tick % frame_count]
            line = (
                paint(frame, TERM_STYLES["panel"])
                + " "
                + paint(APP_NAME.upper(), TERM_STYLES["bold"], TERM_STYLES["model"])
                + " :: "
                + paint(self.label, TERM_STYLES["bold"], TERM_STYLES["accent"])
            )
            plain = strip_ansi(line)
            self._last_width = max(self._last_width, len(plain))
            with PRINT_LOCK:
                sys.stdout.write("\r" + line + " " * max(0, self._last_width - len(plain)))
                sys.stdout.flush()
            tick += 1
            time.sleep(0.12)

    def _finish_line(self, ok):
        status = ok_text("ok") if ok else error_text("error")
        line = (
            paint("[done]", TERM_STYLES["panel"])
            + " "
            + paint(self.label, TERM_STYLES["bold"], TERM_STYLES["accent"])
            + f" :: {status}"
        )
        with PRINT_LOCK:
            sys.stdout.write("\r" + " " * self._last_width + "\r")
            sys.stdout.flush()
        println(line)

    def __enter__(self):
        if self.enabled:
            self._thread = threading.Thread(target=self._render, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        if not self.enabled:
            return False
        self._done.set()
        if self._thread is not None:
            self._thread.join()
        self._finish_line(exc_type is None)
        return False


def parse_args(argv):
    reset_config = False
    for arg in argv[1:]:
        if arg == "-r":
            reset_config = True
            continue
        raise SystemExit(f"Usage: {Path(argv[0]).name} [-r]")
    return {"reset_config": reset_config}


def prompt_input(label, default=None, allow_empty=False):
    while True:
        try:
            value = input(format_prompt(label, default if default else None)).strip()
        except EOFError:
            raise SystemExit("\nInput ended.")
        except KeyboardInterrupt:
            raise SystemExit("\nInterrupted.")
        if value:
            return value
        if default is not None:
            return default
        if allow_empty:
            return ""
        println(warning_text("A value is required."))


def prompt_multiline(label):
    println(section_title(label))
    println(muted("Finish with an empty line."))
    lines = []
    while True:
        try:
            line = input(format_input_marker())
        except EOFError:
            break
        except KeyboardInterrupt:
            raise SystemExit("\nInterrupted.")
        if not line.strip() and lines:
            break
        if not line.strip() and not lines:
            println(warning_text("Enter at least one line."))
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def resolve_editor_command():
    for env_name in ("VISUAL", "EDITOR"):
        raw = os.environ.get(env_name, "").strip()
        if not raw:
            continue
        parts = shlex.split(raw, posix=os.name != "nt")
        if parts and command_exists(parts[0]):
            return parts
    for candidate in EDITOR_FALLBACKS:
        if command_exists(candidate[0]):
            return candidate
    return None


def open_file_in_editor(repo_path, target_path):
    editor_cmd = resolve_editor_command()
    if not editor_cmd:
        raise RuntimeError(
            "No editor available. Set $VISUAL or $EDITOR, or install nano/vim/vi or Notepad on Windows."
        )

    if not target_path.exists():
        target_path.write_text("", encoding="utf-8")

    println(
        info_text("Opening editor:")
        + " "
        + paint(" ".join(editor_cmd), TERM_STYLES["bold"], TERM_STYLES["prompt"])
        + " "
        + format_path(target_path.name)
    )
    try:
        completed = subprocess.run([*editor_cmd, str(target_path)], cwd=str(repo_path))
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Unable to start editor ({' '.join(editor_cmd)}): {explain_missing_path(exc, editor_cmd[0])}"
        ) from exc
    if completed.returncode != 0:
        raise RuntimeError(f"The editor exited with code {completed.returncode}.")


def load_specs_prompt(repo_path):
    specs_path = repo_path / SPECS_FILE
    while True:
        if sys.stdin.isatty() and sys.stdout.isatty():
            if specs_path.exists():
                println(info_text("Opening specs for editing:") + f" {format_path(SPECS_FILE)}")
            else:
                println(warning_text(f"{SPECS_FILE} not found."))
            open_file_in_editor(repo_path, specs_path)

        if not specs_path.exists():
            raise SystemExit(
                f"{SPECS_FILE} not found. Create it in the workspace or run {APP_ABBR} in an interactive terminal."
            )

        try:
            content = specs_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            content = ""
        except OSError as exc:
            raise RuntimeError(f"Unable to read {SPECS_FILE}: {explain_missing_path(exc, specs_path)}") from exc

        if content:
            println(info_text("Specs loaded from") + f" {format_path(SPECS_FILE)}.")
            return content

        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise SystemExit(f"{SPECS_FILE} is empty. Fill it in the workspace or run {APP_ABBR} in an interactive terminal.")

        println(warning_text(f"{SPECS_FILE} is empty."))


def create_readme(repo_path):
    readme_path = repo_path / README_FILE
    content = textwrap.dedent(
        f"""
        # {APP_NAME} ({APP_ABBR})

        {APP_NAME} is a local CLI that uses Ollama to inspect the current workspace,
        build a short execution plan, apply file changes, and run basic validation checks.

        ## Workflow

        1. At startup, the tool opens `{SPECS_FILE}` in a text editor so you can write or update the request.
        2. After you save the file, `{APP_ABBR}` loads the prompt from `{SPECS_FILE}`.
        3. It loads the Ollama settings from `{CONFIG_FILE}`.
        4. It scans the workspace, runs an initial verification when code already exists, and creates a plan.
        5. It asks the model for file actions, applies them, and runs automatic checks again.
        6. It stores the execution report in `{REPORT_FILE}`.

        ## Current capabilities

        - Local Ollama integration with interactive model selection.
        - Spec-driven workflow based on `{SPECS_FILE}`.
        - Colored terminal output, animated status indicators, and an ASCII banner.
        - Bootstrap mode when the workspace does not contain code yet.
        - Automatic retries for model planning and generation steps.
        - Basic validation for Python, JavaScript, JSON, HTML, shell scripts, and pytest-based Python projects.
        - Write protection for reserved files and paths outside the repository.

        ## Main files

        - `{CONFIG_FILE}`: local Ollama configuration.
        - `{SPECS_FILE}`: the request to execute.
        - `{REPORT_FILE}`: text report for the latest iteration.
        - `{README_FILE}`: workspace overview generated by the tool.
        - `oc` / `oc.py` / `oc.bat`: launchers for Unix-like systems and Windows.
        """
    ).strip() + "\n"
    try:
        readme_path.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Unable to write {README_FILE}: {explain_missing_path(exc, readme_path)}") from exc
    return readme_path


def fetch_available_models(base_url):
    request = urllib.request.Request(f"{base_url}/api/tags", method="GET")
    try:
        with AnimatedStatus("Scanning Ollama models"):
            with urllib.request.urlopen(request, timeout=10) as response:
                raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Ollama HTTP error {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Unable to contact Ollama at {base_url}: {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Invalid Ollama response for the model list.") from exc

    models = data.get("models", [])
    if not isinstance(models, list):
        raise RuntimeError("Invalid Ollama response: missing or invalid models field.")

    names = []
    for item in models:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if name and name not in names:
            names.append(name)
    return names


def prompt_model_choice(base_url, default=None):
    default_model = default or DEFAULT_MODEL
    try:
        models = fetch_available_models(base_url)
    except Exception as exc:
        println(
            warning_text("Warning:")
            + f" unable to read available models from {format_url(base_url)}: {exc}"
        )
        return prompt_input("Ollama model", default_model)

    if not models:
        println(warning_text("Warning:") + " Ollama did not return any available models.")
        return prompt_input("Ollama model", default_model)

    println(section_title("Available Ollama models"))
    for idx, name in enumerate(models, start=1):
        entry = f"{paint(str(idx), TERM_STYLES['note'])}. {format_model_name(name)}"
        if name == default_model:
            entry += " " + muted("(default)")
        println(entry)

    default_choice = default_model if default_model in models else models[0]
    default_index = models.index(default_choice) + 1
    while True:
        try:
            value = input(format_prompt("Choose the model by number or name", default_index)).strip()
        except EOFError:
            raise SystemExit("\nInput ended.")
        except KeyboardInterrupt:
            raise SystemExit("\nInterrupted.")
        if not value:
            return default_choice
        if value.isdigit():
            choice = int(value)
            if 1 <= choice <= len(models):
                return models[choice - 1]
            println(warning_text("Invalid number."))
            continue
        if value in models:
            return value
        println(warning_text("Invalid choice.") + " Enter a number or one of the listed names.")


def run(cmd, cwd, check=True, timeout=None):
    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd),
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Unable to run command ({' '.join(cmd)}): {explain_missing_path(exc, cmd[0])}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Command timed out ({' '.join(cmd)}).") from exc
    if check and result.returncode != 0:
        raise RuntimeError(
            f"Command failed ({' '.join(cmd)}):\n{result.stderr.strip() or result.stdout.strip()}"
        )
    return result


def load_or_create_config(repo_path):
    config_path = repo_path / CONFIG_FILE
    created = False
    if not config_path.exists():
        println(warning_text("Configuration not found."))
        base_url = prompt_input("Base URL Ollama", DEFAULT_BASE_URL).rstrip("/")
        model = prompt_model_choice(base_url, DEFAULT_MODEL)
        temperature_raw = prompt_input("Temperature", str(DEFAULT_TEMPERATURE))
        try:
            temperature = float(temperature_raw)
        except ValueError:
            temperature = DEFAULT_TEMPERATURE
        config = {
            "base_url": base_url,
            "model": model,
            "temperature": temperature,
        }
        try:
            config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(f"Unable to save {CONFIG_FILE}: {explain_missing_path(exc, config_path)}") from exc
        created = True
    else:
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise RuntimeError(f"Configuration disappeared while reading it: {explain_missing_path(exc, config_path)}") from exc
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{CONFIG_FILE} is invalid: {exc}")
    config.setdefault("base_url", DEFAULT_BASE_URL)
    config.setdefault("model", DEFAULT_MODEL)
    config.setdefault("temperature", DEFAULT_TEMPERATURE)
    config["base_url"] = str(config["base_url"]).rstrip("/")
    return config, created


def command_exists(name):
    return shutil.which(name) is not None


def reset_config_file(repo_path):
    config_path = repo_path / CONFIG_FILE
    if config_path.exists():
        try:
            config_path.unlink()
        except OSError as exc:
            raise RuntimeError(f"Unable to remove {CONFIG_FILE}: {explain_missing_path(exc, config_path)}") from exc
        println(info_text("Configuration removed:") + f" {format_path(CONFIG_FILE)}")


def is_code_file(path):
    if path.name in {CONFIG_FILE, REPORT_FILE, LEGACY_REPORT_FILE, README_FILE, SPECS_FILE}:
        return False
    if path.suffix.lower() in CODE_EXTENSIONS:
        return True
    if not path.suffix and is_text_file(path):
        try:
            first_line = path.read_text(encoding="utf-8").splitlines()[:1]
        except OSError:
            return False
        return bool(first_line and first_line[0].startswith("#!"))
    return False


def list_workspace_code_files(repo_path):
    files = []
    try:
        paths = sorted(repo_path.rglob("*"))
    except FileNotFoundError:
        return files
    for path in paths:
        if path.is_dir():
            continue
        if ".git" in path.parts or "__pycache__" in path.parts:
            continue
        if is_code_file(path):
            files.append(path.relative_to(repo_path).as_posix())
    return files


def is_text_file(path):
    try:
        with path.open("r", encoding="utf-8") as handle:
            handle.read(1024)
        return True
    except (UnicodeDecodeError, OSError):
        return False


def collect_workspace_context(repo_path):
    source_name = Path(__file__).name
    files = []
    try:
        paths = sorted(repo_path.rglob("*"))
    except FileNotFoundError:
        paths = []
    for path in paths:
        if path.is_dir():
            continue
        if ".git" in path.parts:
            continue
        if "__pycache__" in path.parts:
            continue
        if path.name in {CONFIG_FILE, README_FILE, SPECS_FILE, LEGACY_REPORT_FILE}:
            continue
        rel = path.relative_to(repo_path).as_posix()
        files.append(rel)
    files.sort(key=lambda rel: (0 if rel == source_name else 1 if rel == REPORT_FILE else 2, rel))

    snippets = []
    total_chars = 0
    included_files = 0
    for rel in files:
        if included_files >= MAX_FILES_IN_CONTEXT or total_chars >= MAX_TOTAL_CHARS:
            break
        path = repo_path / rel
        if not is_text_file(path):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        snippet = content[:MAX_FILE_CHARS]
        entry = f"FILE: {rel}\n{snippet}"
        snippets.append(entry)
        total_chars += len(entry)
        included_files += 1

    tree = "\n".join(files) if files else "(empty)"
    excerpts = "\n\n".join(snippets) if snippets else "(no relevant text files)"
    return {
        "tree": tree,
        "excerpts": excerpts,
    }


def ollama_chat(config, system_prompt, user_prompt):
    payload = {
        "model": config["model"],
        "stream": False,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "options": {"temperature": config.get("temperature", DEFAULT_TEMPERATURE)},
    }
    request = urllib.request.Request(
        f"{config['base_url']}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=MODEL_REQUEST_TIMEOUT) as response:
            raw = response.read().decode("utf-8")
    except (TimeoutError, socket.timeout) as exc:
        raise RuntimeError(f"Ollama timed out after {MODEL_REQUEST_TIMEOUT} seconds.") from exc
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Ollama HTTP error {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Unable to contact Ollama at {config['base_url']}: {exc}") from exc

    data = json.loads(raw)
    message = data.get("message", {})
    content = message.get("content", "").strip()
    if not content:
        raise RuntimeError("Empty Ollama response.")
    return content


def extract_json(text):
    candidate = text.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if len(lines) >= 3:
            candidate = "\n".join(lines[1:-1]).strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    start_indexes = [idx for idx, ch in enumerate(candidate) if ch in "[{"]
    for start in start_indexes:
        open_char = candidate[start]
        close_char = "}" if open_char == "{" else "]"
        depth = 0
        in_string = False
        escape = False
        for idx in range(start, len(candidate)):
            ch = candidate[idx]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
                continue
            if ch == open_char:
                depth += 1
            elif ch == close_char:
                depth -= 1
                if depth == 0:
                    fragment = candidate[start : idx + 1]
                    try:
                        return json.loads(fragment)
                    except json.JSONDecodeError:
                        break
    raise RuntimeError(f"Unable to extract JSON from the model response:\n{text}")


def compact_error_message(exc):
    message = str(exc).strip()
    if not message:
        return exc.__class__.__name__
    return " ".join(message.split())


def run_model_step(step_name, operation):
    last_exc = None
    for attempt in range(1, MODEL_STEP_RETRIES + 1):
        try:
            with AnimatedStatus(step_name):
                return operation()
        except Exception as exc:
            last_exc = exc
            if attempt >= MODEL_STEP_RETRIES:
                break
            println(
                error_text(step_name)
                + f": attempt {attempt}/{MODEL_STEP_RETRIES} failed: {compact_error_message(exc)}"
            )
            println(info_text(f"Retrying in {MODEL_RETRY_DELAY_SECONDS} seconds...\n"))
            time.sleep(MODEL_RETRY_DELAY_SECONDS)
    raise RuntimeError(
        f"{step_name} failed after {MODEL_STEP_RETRIES} attempts: {compact_error_message(last_exc)}"
    ) from last_exc


def format_validation_for_prompt(validation):
    if not validation:
        return "No previous check."
    if not validation.get("checks"):
        return "No automatic checks applied in the previous iteration."

    lines = [f"Previous result: {'ok' if validation['ok'] else 'error'}"]
    for check in validation["checks"]:
        status = "ok" if check.get("ok") else "error"
        lines.append(f"{check.get('name', 'check')}: {status}")
        details = str(check.get("details", "")).strip()
        if details:
            lines.append(details)
    return "\n".join(lines)


def build_verification(config, user_prompt, workspace_context, initial_validation):
    system_prompt = textwrap.dedent(
        """
        You are a code analysis agent.
        Read the user prompt, automatic checks, and workspace content.
        Return only valid JSON with this schema:
        {
          "summary": "short summary of the current state",
          "observations": ["observation 1", "observation 2"]
        }
        Rules:
        - First describe what currently happens relative to the prompt.
        - Highlight existing anomalies, errors, or gaps.
        - Do not propose a change plan yet.
        - If the prompt asks to verify anomalies or problems, treat the subsequent fix as implicit.
        - Write in English.
        - No text outside the JSON.
        """
    ).strip()
    user_message = textwrap.dedent(
        f"""
        User prompt:
        {user_prompt}

        Initial automatic checks:
        {format_validation_for_prompt(initial_validation)}

        Workspace tree:
        {workspace_context['tree']}

        File excerpts:
        {workspace_context['excerpts']}
        """
    ).strip()
    response = ollama_chat(config, system_prompt, user_message)
    data = extract_json(response)
    if not isinstance(data, dict):
        raise RuntimeError("The initial verification is not a valid JSON object.")
    observations = data.get("observations", [])
    if not isinstance(observations, list):
        observations = [str(observations)]
    summary = str(data.get("summary", "")).strip() or "No summary available."
    return {
        "summary": summary,
        "observations": [str(item).strip() for item in observations if str(item).strip()],
    }


def build_plan(config, user_prompt, workspace_context, iteration, validation=None, verification=None, bootstrap_mode=False):
    system_prompt = textwrap.dedent(
        """
        You are a software development planning agent.
        Read the user prompt, initial verification, and workspace content.
        Return only valid JSON with this schema:
        {
          "plan": ["item 1", "item 2", "item 3"]
        }
        Rules:
        - Use 3 to 6 items.
        - Each item must be concrete and operational.
        - Write in English.
        - Keep the solution as simple as possible.
        - If the prompt asks to verify anomalies or problems, treat the fix as implicit unless instructed otherwise.
        - If there is a previous report or a failed check, use it to fix the problems first.
        - If the workspace does not contain code yet, do not analyze nonexistent errors: plan the initial build from scratch.
        - No text outside the JSON.
        """
    ).strip()
    verification_text = "No initial verification: workspace has no existing code."
    if verification:
        verification_lines = [verification["summary"]]
        verification_lines.extend(verification["observations"])
        verification_text = "\n".join(verification_lines)
    user_message = textwrap.dedent(
        f"""
        User prompt:
        {user_prompt}

        Iteration:
        {iteration}

        Previous check:
        {format_validation_for_prompt(validation)}

        Initial verification:
        {verification_text}

        Mode:
        {'initial build from scratch' if bootstrap_mode else 'verification, plan, and changes'}

        Workspace tree:
        {workspace_context['tree']}

        File excerpts:
        {workspace_context['excerpts']}
        """
    ).strip()
    response = ollama_chat(config, system_prompt, user_message)
    data = extract_json(response)
    plan = data.get("plan")
    if not isinstance(plan, list) or not plan:
        raise RuntimeError("The generated plan does not contain a valid list of items.")
    return [str(item).strip() for item in plan if str(item).strip()]


def build_actions(config, user_prompt, plan, workspace_context, iteration, validation=None, verification=None):
    plan_text = "\n".join(f"- {item}" for item in plan)
    verification_text = "No initial verification available."
    if verification:
        verification_lines = [verification["summary"]]
        verification_lines.extend(verification["observations"])
        verification_text = "\n".join(verification_lines)
    system_prompt = textwrap.dedent(
        """
        You are a minimal code agent.
        Produce only valid JSON with this schema:
        {
          "summary": "short summary",
          "actions": [
            {
              "type": "write_file",
              "path": "relative/path.ext",
              "content": "complete content"
            }
          ],
          "notes": ["note 1", "note 2"]
        }
        Allowed types:
        - write_file: create or overwrite a file with the complete content.
        - append_file: append text to a file.
        - delete_file: delete a file.
        Rules:
        - Use only relative paths.
        - Keep the solution as simple as possible.
        - Do not touch .config, specs.txt, report.txt, README.md, or paths outside the repository.
        - If you modify an existing file with write_file, return the full content.
        - If no file should be modified, use an empty actions list.
        - If the previous check failed, fix those problems first.
        - If the prompt asks to verify anomalies or problems, treat the fix as implicit unless instructed otherwise.
        - If the workspace does not contain code yet, create the minimum initial files needed to satisfy the prompt.
        - Write notes and summary in English.
        - No text outside the JSON.
        """
    ).strip()
    user_message = textwrap.dedent(
        f"""
        User prompt:
        {user_prompt}

        Iteration:
        {iteration}

        Plan:
        {plan_text}

        Previous check:
        {format_validation_for_prompt(validation)}

        Initial verification:
        {verification_text}

        Workspace tree:
        {workspace_context['tree']}

        File excerpts:
        {workspace_context['excerpts']}
        """
    ).strip()
    response = ollama_chat(config, system_prompt, user_message)
    data = extract_json(response)
    if not isinstance(data, dict):
        raise RuntimeError("The model response for actions is not a JSON object.")
    actions = data.get("actions", [])
    if not isinstance(actions, list):
        raise RuntimeError("The actions field is not a list.")
    notes = data.get("notes", [])
    if not isinstance(notes, list):
        notes = [str(notes)]
    summary = str(data.get("summary", "")).strip() or "No summary provided."
    return {
        "summary": summary,
        "actions": actions,
        "notes": [str(note).strip() for note in notes if str(note).strip()],
    }


def safe_target_path(repo_path, relative_path):
    target = Path(relative_path)
    if target.is_absolute():
        raise RuntimeError(f"Absolute path not allowed: {relative_path}")
    resolved = (repo_path / target).resolve()
    try:
        resolved.relative_to(repo_path.resolve())
    except ValueError as exc:
        raise RuntimeError(f"Path outside the repository is not allowed: {relative_path}") from exc
    if resolved.name == CONFIG_FILE:
        raise RuntimeError(f"Changes to {CONFIG_FILE} are not allowed")
    if resolved.name == REPORT_FILE:
        raise RuntimeError(f"Changes to {REPORT_FILE} are not allowed")
    if resolved.name == LEGACY_REPORT_FILE:
        raise RuntimeError(f"Changes to {LEGACY_REPORT_FILE} are not allowed")
    if resolved.name == SPECS_FILE:
        raise RuntimeError(f"Changes to {SPECS_FILE} are not allowed")
    if resolved.name == README_FILE:
        raise RuntimeError(f"Changes to {README_FILE} are not allowed")
    if ".git" in resolved.parts:
        raise RuntimeError(f"Changes under .git are not allowed: {relative_path}")
    return resolved


def prepare_actions(repo_path, actions):
    prepared_actions = []
    for raw_action in actions:
        if not isinstance(raw_action, dict):
            raise RuntimeError(f"Invalid action: {raw_action}")
        action_type = raw_action.get("type")
        relative_path = raw_action.get("path")
        if not action_type or not relative_path:
            raise RuntimeError(f"Incomplete action: {raw_action}")
        target = safe_target_path(repo_path, str(relative_path))
        if action_type == "write_file":
            content = str(raw_action.get("content", ""))
        elif action_type == "append_file":
            content = str(raw_action.get("content", ""))
        elif action_type == "delete_file":
            content = None
        else:
            raise RuntimeError(f"Unsupported action type: {action_type}")
        prepared_actions.append(
            {
                "type": action_type,
                "target": target,
                "content": content,
            }
        )
    return prepared_actions


def apply_actions(repo_path, actions):
    prepared_actions = prepare_actions(repo_path, actions)
    changed_files = []
    for action in prepared_actions:
        action_type = action["type"]
        target = action["target"]
        if action_type == "write_file":
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(action["content"], encoding="utf-8")
            changed_files.append(str(target.relative_to(repo_path)))
        elif action_type == "append_file":
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as handle:
                handle.write(action["content"])
            changed_files.append(str(target.relative_to(repo_path)))
        elif action_type == "delete_file":
            if target.exists():
                target.unlink()
                changed_files.append(str(target.relative_to(repo_path)))
    return list(dict.fromkeys(changed_files))


def build_and_apply_actions(
    repo_path,
    config,
    user_prompt,
    plan,
    workspace_context,
    iteration,
    validation=None,
    verification=None,
):
    execution = build_actions(
        config,
        user_prompt,
        plan,
        workspace_context,
        iteration,
        validation,
        verification,
    )
    changed_files = apply_actions(repo_path, execution["actions"])
    return execution, changed_files


def create_report(
    repo_path,
    user_prompt,
    iteration,
    total_iterations,
    plan,
    execution,
    changed_files,
    validation,
    verification=None,
    bootstrap_mode=False,
):
    report_path = repo_path / REPORT_FILE
    report = [
        f"REPORT {APP_NAME.upper()} ({APP_ABBR})",
        "=" * 48,
        "",
        "ITERATION",
        f"{iteration}/{total_iterations}",
        "",
        "SPECS",
        "-" * 48,
        user_prompt,
        "",
    ]
    if verification:
        report.extend(
            [
                "INITIAL VERIFICATION",
                "-" * 48,
                verification["summary"],
            ]
        )
        if verification["observations"]:
            report.extend([f"- {item}" for item in verification["observations"]])
        else:
            report.append("- No additional observations.")
    report.append("")
    report.extend(
        [
            "PLAN",
            "-" * 48,
        ]
    )
    report.extend([f"{idx}. {item}" for idx, item in enumerate(plan, start=1)])
    report.extend(
        [
            "",
            "SUMMARY",
            "-" * 48,
            execution["summary"],
            "",
            "NOTE",
            "-" * 48,
        ]
    )
    if execution["notes"]:
        report.extend([f"- {note}" for note in execution["notes"]])
    else:
        report.append("- No additional notes.")
    report.extend(
        [
            "",
            "EFFECTS",
            "-" * 48,
            f"- Mode: {'initial build from scratch' if bootstrap_mode else 'verification, plan, and changes'}",
            f"- Changed files: {', '.join(changed_files) if changed_files else 'none'}",
            "",
            "CODE CHECK",
            "-" * 48,
            f"Result: {'ok' if validation['ok'] else 'error'}",
            "",
        ]
    )
    if validation["checks"]:
        for check in validation["checks"]:
            report.append(f"- {check['name']}: {'ok' if check['ok'] else 'error'}")
            details = str(check.get("details", "")).strip()
            if details:
                report.extend(["  details:", textwrap.indent(details, "    "), ""])
    else:
        report.append("- No automatic checks applicable.")
    try:
        report_path.write_text("\n".join(report).rstrip() + "\n", encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Unable to write {REPORT_FILE}: {explain_missing_path(exc, report_path)}") from exc
    return report_path


def make_check_result(name, ok, details=""):
    return {
        "name": name,
        "ok": ok,
        "details": str(details).strip(),
    }


def validate_json_file(path, repo_path):
    rel = path.relative_to(repo_path).as_posix()
    try:
        json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return make_check_result(f"json {rel}", False, str(exc))
    return make_check_result(f"json {rel}", True, "")


class BasicHTML5Parser(HTMLParser):
    def error(self, message):
        raise RuntimeError(message)


def validate_html_file(path, repo_path):
    rel = path.relative_to(repo_path).as_posix()
    try:
        content = path.read_text(encoding="utf-8")
    except Exception as exc:
        return make_check_result(f"html5 {rel}", False, str(exc))

    if command_exists("tidy"):
        result = run(
            ["tidy", "-qe", str(path)],
            cwd=repo_path,
            check=False,
            timeout=120,
        )
        details = result.stderr.strip() or result.stdout.strip()
        return make_check_result(f"html5 {rel}", result.returncode <= 1, details)

    stripped = content.lstrip().lower()
    if not stripped.startswith("<!doctype html>"):
        return make_check_result(
            f"html5 {rel}",
            False,
            "Missing HTML5 DOCTYPE. Add <!DOCTYPE html> at the beginning of the file.",
        )

    parser = BasicHTML5Parser()
    try:
        parser.feed(content)
        parser.close()
    except Exception as exc:
        return make_check_result(f"html5 {rel}", False, str(exc))

    if "<html" not in stripped:
        return make_check_result(f"html5 {rel}", False, "Missing <html> tag.")

    return make_check_result(f"html5 {rel}", True, "")


def has_pytest_config(repo_path):
    if (repo_path / "tests").exists():
        return True
    for name in ("pytest.ini", "conftest.py"):
        if (repo_path / name).exists():
            return True
    pyproject = repo_path / "pyproject.toml"
    if not pyproject.exists():
        return False
    try:
        content = pyproject.read_text(encoding="utf-8").lower()
    except OSError:
        return False
    return "[tool.pytest" in content


def run_code_checks(repo_path, changed_files):
    existing_paths = []
    for rel in changed_files:
        path = repo_path / rel
        if path.exists() and path.is_file():
            existing_paths.append(path)

    checks = []
    python_files = [path for path in existing_paths if path.suffix == ".py"]
    shell_files = [path for path in existing_paths if path.suffix == ".sh"]
    javascript_files = [path for path in existing_paths if path.suffix in {".js", ".mjs", ".cjs"}]
    json_files = [path for path in existing_paths if path.suffix == ".json"]
    html_files = [path for path in existing_paths if path.suffix in {".html", ".htm"}]

    if python_files:
        result = run(
            [sys.executable, "-m", "py_compile", *[str(path) for path in python_files]],
            cwd=repo_path,
            check=False,
            timeout=120,
        )
        details = result.stderr.strip() or result.stdout.strip()
        checks.append(make_check_result("py_compile", result.returncode == 0, details))

    if javascript_files and command_exists("node"):
        for path in javascript_files:
            rel = path.relative_to(repo_path).as_posix()
            result = run(
                ["node", "--check", str(path)],
                cwd=repo_path,
                check=False,
                timeout=120,
            )
            details = result.stderr.strip() or result.stdout.strip()
            checks.append(make_check_result(f"javascript {rel}", result.returncode == 0, details))
    elif javascript_files:
        checks.append(
            make_check_result(
                "javascript",
                False,
                "Node.js is not available: unable to run the JavaScript syntax check.",
            )
        )

    for path in json_files:
        checks.append(validate_json_file(path, repo_path))

    for path in html_files:
        checks.append(validate_html_file(path, repo_path))

    if shell_files and command_exists("bash"):
        result = run(
            ["bash", "-n", *[str(path) for path in shell_files]],
            cwd=repo_path,
            check=False,
            timeout=120,
        )
        details = result.stderr.strip() or result.stdout.strip()
        checks.append(make_check_result("bash -n", result.returncode == 0, details))

    if (
        python_files
        and command_exists("pytest")
        and has_pytest_config(repo_path)
        and all(check["ok"] for check in checks)
    ):
        result = run(["pytest", "-q"], cwd=repo_path, check=False, timeout=120)
        details = result.stdout.strip() or result.stderr.strip()
        checks.append(make_check_result("pytest -q", result.returncode == 0, details))

    return {
        "ok": all(check["ok"] for check in checks) if checks else True,
        "checks": checks,
    }


def main():
    try:
        repo_path = Path.cwd()
    except FileNotFoundError as exc:
        raise SystemExit(
            "Current workspace not found. "
            "The directory was probably removed or renamed while `oc` was running."
        ) from exc
    println(info_text("Workspace:") + f" {format_path(repo_path)}")

    options = parse_args(sys.argv)
    if options["reset_config"]:
        reset_config_file(repo_path)

    config, config_created = load_or_create_config(repo_path)
    print_ollama_banner(config)
    if config_created:
        println(info_text("Configuration saved to") + f" {format_path(CONFIG_FILE)}.")
    readme_path = create_readme(repo_path)
    println(info_text("README updated at") + f" {format_path(README_FILE)}.")

    user_prompt = load_specs_prompt(repo_path)
    if not user_prompt:
        raise SystemExit(f"{SPECS_FILE} is empty.")

    code_files = list_workspace_code_files(repo_path)
    bootstrap_mode = not bool(code_files)
    verification = None
    initial_validation = None

    workspace_context = collect_workspace_context(repo_path)
    if bootstrap_mode:
        println("")
        println(section_title("Initial Bootstrap"))
        println(info_text("No existing code detected. Planning and building from scratch.\n"))
    else:
        println("")
        println(section_title("Initial Code Verification"))
        println(muted(rule()))
        initial_validation = run_code_checks(repo_path, code_files)
        try:
            verification = run_model_step(
                "Initial verification",
                lambda: build_verification(config, user_prompt, workspace_context, initial_validation),
            )
            println(verification["summary"])
            for item in verification["observations"]:
                println("- " + item)
        except Exception as exc:
            println(warning_text("Initial verification unavailable:") + f" {exc}")
            verification = None

    validation = None
    report_path = repo_path / REPORT_FILE
    execution = {"summary": "", "notes": []}
    changed_files = []
    plan = []
    last_model_error = None

    max_iterations = MAX_ITERATIONS
    for iteration in range(1, max_iterations + 1):
        workspace_context = collect_workspace_context(repo_path)
        println("")
        println(section_title(f"Iteration {iteration}/{max_iterations}"))
        println(muted(rule()))
        current_validation = validation if validation is not None else initial_validation
        try:
            plan = run_model_step(
                "Planning",
                lambda: build_plan(
                    config,
                    user_prompt,
                    workspace_context,
                    iteration,
                    current_validation,
                    verification,
                    bootstrap_mode,
                ),
            )
        except Exception as exc:
            last_model_error = exc
            println(error_text("Model error:") + f" {exc}")
            if iteration < max_iterations:
                println(info_text("\nThe model did not produce a valid plan, starting a new iteration...\n"))
                continue
            break
        for idx, item in enumerate(plan, start=1):
            println(f"{paint(str(idx), TERM_STYLES['note'])}. {item}")

        println("")
        println(section_title("Applying Changes"))
        println(muted(rule()))
        try:
            execution, changed_files = run_model_step(
                "Generating changes",
                lambda: build_and_apply_actions(
                    repo_path,
                    config,
                    user_prompt,
                    plan,
                    workspace_context,
                    iteration,
                    current_validation,
                    verification,
                ),
            )
        except Exception as exc:
            last_model_error = exc
            println(error_text("Model error:") + f" {exc}")
            if iteration < max_iterations:
                println(info_text("\nThe model did not produce valid changes, starting a new iteration...\n"))
                continue
            break
        validation = run_code_checks(repo_path, changed_files)
        last_model_error = None

        report_path = create_report(
            repo_path=repo_path,
            user_prompt=user_prompt,
            iteration=iteration,
            total_iterations=max_iterations,
            plan=plan,
            execution=execution,
            changed_files=changed_files,
            validation=validation,
            verification=verification,
            bootstrap_mode=bootstrap_mode,
        )

        println("")
        println(section_title("Code Check"))
        println(muted(rule()))
        if validation["checks"]:
            for check in validation["checks"]:
                println(format_check_line(check))
        else:
            println("- " + muted("No automatic checks applicable."))

        if validation["ok"]:
            break
        if iteration < MAX_ITERATIONS:
            println("")
            println(warning_text("Check failed, starting a new iteration.\n"))

    if validation is None:
        reason = compact_error_message(last_model_error) if last_model_error else "no details available"
        raise RuntimeError(f"No iteration completed successfully. Last model error: {reason}")

    println("")
    println(section_title("Summary"))
    println(muted(rule()))
    println(execution["summary"])
    println("")
    println(section_title("Saved Files"))
    if changed_files:
        for path in changed_files:
            println(f"- {format_path(path)}")
    else:
        println("- " + muted("No files modified by the model."))
    println("- " + info_text("Specs:") + f" {format_path(SPECS_FILE)}")
    println(
        "- "
        + info_text("Report:")
        + f" {format_path(report_path.name) if report_path.exists() else muted('not generated')}"
    )
    println("- " + info_text("README:") + f" {format_path(readme_path.name)}")
    println("- " + info_text("Code check:") + f" {format_status(bool(validation and validation['ok']))}")
    if last_model_error:
        println("- " + error_text("Last model error:") + f" {compact_error_message(last_model_error)}")
    if validation and not validation["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        println(error_text("Error:") + f" {exc}")
        sys.exit(1)
