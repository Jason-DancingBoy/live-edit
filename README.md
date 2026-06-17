# live-edit

Natural-language-driven live code editing, powered by an AI agent loop — not a thin LLM wrapper.

It watches you code. Then your users change it. Then it commits. All through natural language, with full git isolation.

## It's not a chatbot strapped to your codebase.

A pure LLM wrapper does one thing: prompt → response. live-edit runs a full agent loop — the model reads files, searches code, makes edits, observes results, retries on failure, and commits. Your users see a friendly chat; your git log sees clean commits.

Three modes, one engine:

| Mode | Who | How |
|------|-----|-----|
| **quick** | Non-technical users | Each write waits for approval, errors translated to plain language |
| **deep** | Developers | Agent works autonomously, final diff gets approved as a batch |
| **qa** | Anyone learning | Read-only tools, code analysis only |

## Install

```bash
pip install live-edit
cd your-project/
live-edit init
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

## Customize

Swap any component — LLM provider, storage backend, version control:

```python
from live_edit import Provider, Storage, VCS, setup_live_edit

app.include_router(setup_live_edit(
    provider=MyProvider(),
    storage=MyStorage(),
    vcs=MyVCS(),
))
```

## Docs

[USER_MANUAL.md](USER_MANUAL.md) — architecture, agent loop, config reference, API endpoints, security model.
