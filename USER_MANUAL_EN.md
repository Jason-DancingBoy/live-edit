# live-edit User Manual

## 1. What It Is (and Isn't)

**live-edit is not an LLM wrapper.** It doesn't pass user requests to a model and hand back whatever comes out. A pure LLM wrapper does one thing: prompt → response.

**live-edit is an AI agent framework.** It wraps the LLM in a full agent loop:

```
User request → AI thinks → selects tool → executes → observes result → re-thinks → continues → ... → generates diff → git commit
```

This loop is not scripted. The agent decides when to read files, which files to open, what grep patterns to search, and which lines to edit. Every observation feeds back into the next round of decisions. A real session might go:

1. `search_code` to locate relevant code → 2. `read_file` to get context → 3. `edit_file` to make changes → 4. `old_string` not found → 5. re-`read_file` to see current state → 6. retry `edit_file` with adjusted match → 7. success

### Agent Characteristics

**Autonomous tool use.** Tools aren't menu items — they're capabilities the agent wields. It plans which tool to use, with what arguments, and how to recover when things fail. The entire process is a perceive → act → observe → act cycle.

**Three autonomy levels, three agent paradigms.**

| Mode | Paradigm | Human role |
|------|----------|------------|
| `deep` | Fully autonomous agent | Research → edit → commit; no human in the middle |
| `quick` | Human-in-the-loop agent | Each write waits for approval; friendly for non-technical users |
| `qa` | Read-only agent | Observe only, no side effects; pure code analysis |

**Course correction.** The agent loop has built-in nudges: if `deep` mode spends 3 consecutive rounds reading without writing, the system injects a prompt to break analysis paralysis. That's agent orchestration, not a model capability.

**Environment isolation.** The agent never touches your project directory directly. Every session runs inside a dedicated git worktree (`/tmp/live-edit/{session_id}`). Each agent instance has its own sandbox — parallel sessions don't conflict, and a broken session can be discarded without affecting the main branch.

### Architecture

```
Browser frontend (live-edit.js)
    │  SSE event stream
    ▼
FastAPI Router (/live-edit/*)
    │
    ├── Provider ──→ LLM (Anthropic API / DeepSeek / compatible endpoints)
    ├── VCS      ──→ Git (commit / diff / revert / worktree isolation)
    ├── Storage  ──→ SQLite (session persistence)
    ├── Preview  ──→ Per-session uvicorn process (live preview while editing)
    └── Config   ──→ .live-edit.toml
```

---

## 2. Before You Start — Four Things You Need

### 2.1 An LLM API Key

live-edit defaults to Anthropic-compatible endpoints and works out of the box with DeepSeek:

```bash
export DEEPSEEK_API_KEY="sk-..."
```

To use a different model, change `api_url` and `api_key_env` in `.live-edit.toml`:

```toml
# Claude API
[llm]
api_url = "https://api.anthropic.com/v1/messages"
api_key_env = "ANTHROPIC_API_KEY"
model = "claude-sonnet-4-6"

# Ollama
[llm]
api_url = "http://localhost:11434/v1/messages"
api_key_env = "OLLAMA_API_KEY"
model = "qwen3"
```

### 2.2 A Git Repository

live-edit depends on git — every change is committed, rollback uses `git revert`, and multi-session isolation uses `git worktree`. **Your project must already be tracked by git, with at least one commit** (otherwise `git worktree add` will fail).

```bash
cd your-project/
git init
git add -A && git commit -m "Initial commit"
```

### 2.3 Generate and Edit the Config File

```bash
pip install live-edit
cd your-project/
live-edit init
```

This creates `.live-edit.toml` in your project root, auto-detecting language and framework. **You must review and edit at minimum these fields:**

```toml
[project]
name = "YourApp"           # Required — injected into the system prompt
language = "python"        # Required — auto-detection is usually correct
framework = "fastapi"      # Auto-detected
extra_context = """..."""  # Strongly recommended — tells the agent about your project

[llm]
api_url = "https://api.deepseek.com/anthropic/v1/messages"
api_key_env = "DEEPSEEK_API_KEY"
model = "deepseek-v4-pro"

[preview]
enabled = true             # Required for live preview while editing
command = "uvicorn server:app --host 127.0.0.1 --port {port}"  # Match your startup command
base_url = "http://localhost:8083"  # Your app's main URL, used for reverse proxy
```

**`extra_context` is the single most important factor in how well the agent understands your project.** At minimum, include: file structure, file responsibilities, tech stack notes, and any special warnings (e.g. "static/index.html is ~7000 lines — always search_code to locate relevant sections first, never read_file the entire file"). See the [lyric-muse project](https://github.com/Jason-DancingBoy/lyric-muse) for a complete `extra_context` example.

### 2.4 Two Lines of Code

Wire the router into your FastAPI app:

```python
from live_edit import setup_live_edit

app.include_router(setup_live_edit())
```

If you have an admin panel, pass `admin_key` to enable worktree management and force-cancel:

```python
app.include_router(setup_live_edit(api_key=API_KEY, admin_key=ADMIN_KEY))
```

Add the frontend script before `</body>` in your HTML template:

```html
<script src="/live-edit/static/live-edit.js"></script>
```

Done. Users press `Ctrl+Shift+D` to open the editing panel, or click the "Live Edit" button pinned to the bottom-left corner.

---

## 3. Three Modes

| Mode | Key | Approval | Tool permissions | Use case |
|------|-----|----------|------------------|----------|
| **quick** | `quick` | Per-tool approval for writes | All (dangerous commands auto-blocked) | Non-technical users, small changes |
| **deep** | `deep` | Autonomous execution, final diff review | All | Developers, complex tasks |
| **qa** | `qa` | None | Read-only (read_file, search_code, glob, run_shell) | Code analysis, learning |

### quick mode flow

```
User request → AI analyzes → proposes tool call → user approves/rejects → execute → ... → diff → user approves commit → git commit
```

### deep mode flow

```
User request → AI executes multiple steps autonomously → diff → auto-commit → git commit
```

### qa mode flow

```
User asks question → AI uses read-only tools to analyze → returns answer (no modifications)
```

---

## 4. Configuration Reference (.live-edit.toml)

### 4.1 Complete Example

```toml
[project]
name = "my-app"
language = "python"
framework = "fastapi"
root = "."
extra_context = "An e-commerce admin dashboard using SQLAlchemy ORM."

[llm]
provider = "anthropic_compatible"
api_url = "https://api.deepseek.com/anthropic/v1/messages"
api_key_env = "DEEPSEEK_API_KEY"
model = "deepseek-v4-pro"

[safety]
allowed_dirs = ["."]
overwrite_allowed_dirs = ["static", "public", "assets"]
allow_overwrite_existing = false
blocked_commands = ["rm ", "git push", "git reset --hard", "shutdown", "reboot"]
search_extensions = ["*.py", "*.html", "*.js", "*.css", "*.ts", "*.tsx", "*.md", "*.json", "*.toml"]

[timeouts]
api_request = 180
shell_command = 30
approval = 300
final_approval = 600
session_ttl = 1800
max_rounds = 15

[sessions]
max_active = 10

[hooks]
post_revert = ""
pre_commit = ""

[ui]
default_mode = "quick"

[preview]
enabled = true
port_start = 19000
port_end = 19050
startup_timeout = 30
command = "uvicorn server:app --host 127.0.0.1 --port {port}"
base_url = "http://localhost:8083"

[modes.quick]
label = "Quick Edit"
approval = "per_tool"
tools = "write"
approve_for = ["edit_file", "write_file"]

[modes.quick.prompt]
base = "You are a full-stack web developer AI for my-app."
user_persona = "Non-technical users who describe what they want in plain language."
communication_rules = "Communicate in plain language. Never show code snippets, file paths, or line numbers. Describe changes from the user's perspective."

[modes.deep]
label = "Deep Dev"
approval = "final"
tools = "all"
approve_for = []

[modes.deep.prompt]
base = "You are a developer AI assistant for my-app."
user_persona = "Professional developers who understand code and technical concepts."
communication_rules = "Use technical terminology freely. Show key code snippets. Analyze before acting, but execute within 2-3 research rounds."

[modes.qa]
label = "Code Q&A"
approval = "none"
tools = "readonly"
approve_for = []

[modes.qa.prompt]
base = "You are a code analysis expert for my-app."
user_persona = "Learners who want to understand code."
communication_rules = "Use clear code references (file path + line numbers). Explain both what and why. Read-only tools only."

[errors.quick]
"old_string not found in file" = "The file content has changed — the agent will re-read and adjust."
"path traversal detected" = "Operation blocked (attempted to access files outside the project)."
"dangerous command detected" = "Operation blocked for safety."
```

### 4.2 Configuration Reference

| Section | Field | Type | Description |
|---------|-------|------|-------------|
| `[project]` | `name` | str | Project name |
| | `language` | str | Language: python / typescript / go / unknown |
| | `framework` | str | Framework: fastapi / flask / django |
| | `root` | str | Project root directory, default `"."` |
| | `extra_context` | str | Additional context injected into the system prompt |
| `[llm]` | `provider` | str | Always `"anthropic_compatible"` |
| | `api_url` | str | Anthropic Messages API-compatible endpoint |
| | `api_key_env` | str | Environment variable name for the API key |
| | `model` | str | Model name |
| `[safety]` | `allowed_dirs` | list | Directories the agent may access |
| | `overwrite_allowed_dirs` | list | Directories where write_file may overwrite existing files |
| | `allow_overwrite_existing` | bool | Allow overwriting any existing file |
| | `blocked_commands` | list | Shell command patterns to block |
| | `search_extensions` | list | File extensions searched by search_code |
| `[timeouts]` | `api_request` | int | LLM API request timeout (seconds) |
| | `shell_command` | int | Shell command timeout (seconds) |
| | `approval` | int | Per-tool approval timeout in quick mode (seconds) |
| | `final_approval` | int | Final diff approval timeout in quick mode (seconds) |
| | `session_ttl` | int | Session expiry time (seconds) |
| | `max_rounds` | int | Maximum agent loop iterations per session |
| `[sessions]` | `max_active` | int | Maximum concurrent sessions |
| `[hooks]` | `post_revert` | str | Shell command to run after revert |
| | `pre_commit` | str | Shell command to run before commit (non-zero aborts) |
| `[ui]` | `default_mode` | str | Default editing mode |
| `[preview]` | `enabled` | bool | Enable per-session preview server |
| | `port_start` | int | Start of preview port range |
| | `port_end` | int | End of preview port range |
| | `startup_timeout` | int | Max seconds to wait for preview health check |
| | `command` | str | Shell command to start the preview server (`{port}` is replaced) |
| | `base_url` | str | Main app URL for reverse-proxying preview requests |
| `[modes.<name>]` | `label` | str | Display name for the mode |
| | `approval` | str | per_tool / final / none |
| | `tools` | str | all / write / readonly |
| | `approve_for` | list | Tool names requiring approval |
| `[modes.<name>.prompt]` | `base` | str | System prompt body |
| | `user_persona` | str | Description of the target user |
| | `communication_rules` | str | Interaction style rules |
| `[errors.<mode>]` | — | dict | Error message translation table |

---

## 5. API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/live-edit/stream` | Start a new session, returns SSE event stream |
| `POST` | `/live-edit/continue/{session_id}` | Append a new request to an existing session |
| `POST` | `/live-edit/approve/{session_id}/{tool_id}` | Approve or reject a tool call (quick mode) |
| `GET` | `/live-edit/timeline` | Get change timeline (optional `?diff_for=<hash>`) |
| `GET` | `/live-edit/history` | Get recent session history |
| `GET` | `/live-edit/session/{session_id}` | Get session detail |
| `POST` | `/live-edit/revert/{hash}/preview` | Dry-run revert to check for conflicts |
| `POST` | `/live-edit/revert/{hash}/execute` | Execute revert |
| `GET` | `/live-edit/static/{filename}` | Serve static files (JS/CSS) |
| `GET` | `/live-edit/health` | Health check |

### SSE Event Types

| Event | When | Fields |
|-------|------|--------|
| `thinking` | AI reasoning complete | `text` |
| `text` | AI streaming text | `text` |
| `tool_plan` | AI proposes a tool call | `id`, `tool`, `args`, `summary`, `reason` |
| `tool_result` | Tool execution complete | `id`, `ok`, `error`, `path`, `content` |
| `diff` | Change summary generated | `files`, `summary`, `diff` |
| `done` | Session complete | `committed`, `commit_hash`, `message` |
| `error` | Error occurred | `error` |

---

## 6. Custom Implementations

All three core components of live-edit can be replaced with custom implementations:

### 6.1 Custom Provider

Implement the `live_edit.Provider` abstract class:

```python
from live_edit import Provider, setup_live_edit

class MyProvider(Provider):
    async def call_with_tools(self, messages, tools, on_thinking=None, on_text=None):
        # Call your LLM API of choice
        # Return a list of content_blocks, each in the format:
        #   {"type": "text", "text": "..."}
        #   or {"type": "tool_use", "id": "...", "name": "...", "input": {...}}
        ...

app.include_router(setup_live_edit(provider=MyProvider()))
```

The default `AnthropicCompatibleProvider` supports any service compatible with the Anthropic Messages Streaming API (DeepSeek, Claude API, etc.).

### 6.2 Custom Storage

Implement the `live_edit.Storage` abstract class:

```python
from live_edit import Storage

class MyStorage(Storage):
    def save_session(self, session_id, request, committed, files, commit_hash, messages_json, mode):
        ...

    def get_sessions(self, limit=30):
        ...

    def get_session_detail(self, session_id):
        ...

app.include_router(setup_live_edit(storage=MyStorage()))
```

The default `SQLiteStorage` uses `live_edit.db` in the project directory.

### 6.3 Custom VCS

Implement the `live_edit.VCS` abstract class:

```python
from live_edit import VCS, RevertPreview, RevertResult

class MyVCS(VCS):
    def commit(self, files, message) -> str:
        ...

    def diff_stat(self, files) -> str:
        ...

    def diff_full(self, files) -> str:
        ...

    def revert_preview(self, commit_hash) -> RevertPreview:
        ...

    def revert_execute(self, commit_hash) -> RevertResult:
        ...

    def show_commit(self, commit_hash) -> dict:
        ...

    def log_live_edit_commits(self, limit=30) -> list:
        ...

app.include_router(setup_live_edit(vcs=MyVCS()))
```

The default `GitVCS` invokes git commands via subprocess; commit messages are prefixed with `live-edit:`.

---

## 7. Security Model

### 7.1 Path Traversal Protection

All file operations are validated through `_safe_path()`, ensuring paths stay within the project root. `../` and other traversal attempts are blocked.

### 7.2 Dangerous Command Blocking

`run_shell` uses regex-based blocking. The following patterns are automatically rejected:

- Deletion: `rm`, `unlink`, `git rm`
- Database destruction: `drop table`, `delete from`
- Force push: `git push`, `git reset --hard`
- System operations: `shutdown`, `reboot`, `mkfs`, `dd if=`
- Excessive permissions: `chmod 777`
- Pipe-to-shell: `curl ... | bash`, `:(){ :|:& };:`
- Output redirection outside the project

### 7.3 Write File Controls

- `write_file` can only create new files or overwrite files in `static/public/assets` directories by default
- Adjust via `[safety]` `overwrite_allowed_dirs` and `allow_overwrite_existing`

### 7.4 Concurrency Limiting

`max_active` controls the maximum number of concurrent sessions (default 10). Returns 503 when exceeded.

---

## 8. CLI Commands

```bash
live-edit init [directory]      # Generate .live-edit.toml
live-edit check [config-file]   # Validate a config file
live-edit --help                # Show help
```

Options:
- `--force`: Overwrite an existing config file (for `init`)

---

## 9. Frontend Script

### Usage

```html
<script src="/live-edit/static/live-edit.js"></script>
```

### Interactions

- **Shortcut** `Ctrl+Shift+D`: Toggle the editing panel
- **Panel button** "Live Edit": Pinned to the bottom-left corner
- **Mode switcher**: Dropdown at the top of the panel (Quick Edit / Deep Dev / Code Q&A)
- **History button**: View recent change timeline
- **Approve / Reject**: In quick mode, each write operation shows a confirmation card
- **Final approval**: In quick mode, after the diff is shown, choose "Apply Changes" or "Discard All"

Panel state (open/closed) is persisted in `localStorage`.

---

## 10. Session Lifecycle

```
Create (POST /stream)
  → Generates session_id (le_xxxxxxxxxxxx)
  → Added to in-memory SessionStore (TTL default 1800s)
  → Opens SSE event stream
  → Agent loop (up to 15 tool-call rounds)
  → Diff generated
  → User approves → git commit → persisted to SQLite
  → User rejects → git checkout (discard changes)
  → Session ends
```

Use `POST /continue/{session_id}` to append new requests to the same session, preserving conversation context.

---

## 11. Public API Surface

```python
from live_edit import (
    # Router setup
    setup_live_edit,

    # Provider
    Provider,
    AnthropicCompatibleProvider,

    # Storage
    Storage,
    SQLiteStorage,

    # VCS
    VCS,
    GitVCS,
    RevertPreview,
    RevertResult,

    # Config
    Config,
    parse_config,
    validate_config,
    detect_project,

    # Engine
    EditSession,
    SessionStore,
    build_timeline,
    translate_error,

    # Preview
    PreviewManager,
)
```

---

## 12. FAQ

**Q: Which LLMs are supported?**
Any service compatible with the Anthropic Messages API. Defaults to DeepSeek. Switch to Claude API by setting `api_url` to `https://api.anthropic.com/v1/messages`.

**Q: Is git required?**
The default implementation depends on git. You can adapt other version control systems by implementing a custom `VCS`.

**Q: What about multiple concurrent users?**
`SessionStore` isolates sessions in memory, each SSE connection is independent. `max_active` controls the concurrency cap.

**Q: How do I help the agent understand my project better?**
Set `extra_context` in `[project]`. Its content is injected into the system prompt. The more detailed, the better the agent performs.

**Q: How does revert work?**
Two-phase `git revert`: preview checks for conflicts first, then execute applies the revert. Supports `post_revert` hooks (e.g. to restart a service).
