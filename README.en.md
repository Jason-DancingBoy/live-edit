# live-edit

Edit code by talking to it. An AI agent reads your code, makes changes, runs tests and commits — all inside an isolated git branch, so the main branch only sees changes you approve.

live-edit is the library form: two lines of Python embed it into an existing FastAPI app, plus one line in your page. No login, no roles, no standalone service — if you need those, use live-build (the same engine, as a standalone service).

## Install

```bash
pip install live-edit
cd your-project/
live-edit intake    # generates .live-edit.toml automatically (see "Onboarding a new repo")
```

## Wire it up (2 lines)

```python
from live_edit import setup_live_edit

app.include_router(setup_live_edit())
```

```html
<script src="/live-edit/static/live-edit.js"></script>
```

Press `Ctrl+Shift+D` to open the editing panel.

## Modes

| Mode | For | Behavior |
|---|---|---|
| quick | Non-technical users | Every write waits for approval; errors translated to plain language |
| deep | Developers | Works autonomously; the final diff is approved as a batch |
| qa | Reading code | Read-only by intent, no approvals |

## How it works

1. Create an isolated git worktree, branched off the main history
2. The agent loop runs: read files → search → edit → observe → retry
3. When done, a `[verify]` gate runs: tests, health check, diff-safety scan
4. On pass, commit to the session branch

Each session's changes land as a single commit and can be reverted. Sessions run in separate worktrees, so they don't interfere with each other.

## Onboarding a new repo

The `extra_context` field in `.live-edit.toml` controls how well the agent understands your project before it edits — and it's the part people most often have to hand-write. `live-edit intake` automates it:

- Scans the codebase and writes a factual extra_context
- Provisions the `[verify]` test command and health check; generates a minimal smoke test when there are no tests
- Runs the generated test command to confirm the config works

```bash
live-edit intake --dry-run    # preview only, writes nothing
live-edit intake --force      # overwrite an existing config
```

For a bare config without the analysis, use `live-edit init`.

Requirements: the repo must be a git repo with at least one commit (worktree isolation depends on it).

## Configuration

`.live-edit.toml` controls the LLM provider, mode prompts, timeouts, safety, the verify gate and preview. Run `live-edit check` to validate it.

## Docs

- [USER_MANUAL.md](USER_MANUAL.md) — architecture, agent loop, config reference, API, security model

## live-edit vs live-build

Same engine, two forms:

- **live-edit**: library. Embed into an existing app, no login or roles.
- **live-build**: standalone service. Login, admin / business_user roles and live preview, for teams that need collaboration and merge approval.
