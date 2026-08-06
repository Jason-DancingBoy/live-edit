"""Unified-diff helpers for previewing write operations before approval."""

import difflib
import os

from .builtin_tools.edit_file import apply_edit


def diff_text(old: str, new: str, filename: str = "") -> str:
    """Return a unified diff between old and new content (empty when identical)."""
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=filename,
            tofile=filename,
        )
    )


def _read_or_empty(path: str) -> str:
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as f:
        return f.read()


def compute_write_diff(tool_name: str, args: dict, project_root: str) -> str:
    """Compute a preview unified diff for a write tool without applying it.

    Returns "" for non-write tools, missing paths, or edits that would fail
    (e.g. edit_file's old_string not found).
    """
    path = (args.get("path") or "").strip()
    if not path:
        return ""
    abs_path = os.path.join(project_root, path)
    current = _read_or_empty(abs_path)

    if tool_name == "edit_file":
        result = apply_edit(current, args.get("old_string", ""), args.get("new_string", ""))
        if not result.get("ok"):
            return ""
        return diff_text(current, result["content"], path)

    if tool_name == "write_file":
        return diff_text(current, args.get("content", ""), path)

    return ""
