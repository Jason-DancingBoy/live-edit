"""Version Control interface and default Git implementation with worktree isolation."""

import logging
import os
import shutil
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

logger = logging.getLogger("live-edit.vcs")

_WORKTREE_ROOT = "/tmp/live-edit"


def session_worktree_path(session_id: str) -> str:
    """Deterministic worktree path for a session (create + recovery)."""
    return os.path.join(_WORKTREE_ROOT, session_id)


def _symlink_config(repo_path: str, worktree_path: str, filename: str):
    """Symlink a config file from repo to worktree if it exists and isn't tracked by git.

    Some config files (like .live-edit.toml) are gitignored and don't appear in
    worktrees, but are needed by the preview server.
    """
    import contextlib
    import os as _os

    src = _os.path.join(repo_path, filename)
    src = _os.path.abspath(src)
    dst = _os.path.join(worktree_path, filename)
    if _os.path.exists(src) and not _os.path.exists(dst):
        with contextlib.suppress(OSError):
            _os.symlink(src, dst)


@dataclass
class RevertPreview:
    ok: bool
    can_revert: bool
    files: list[str]

    diff_summary: str = ""
    conflicts: list[str] = field(default_factory=list)
    error: str = ""

    def __post_init__(self):
        if not isinstance(self.conflicts, (list, tuple)):
            self.conflicts = []


@dataclass
class RevertResult:
    ok: bool
    new_commit_hash: str = ""
    message: str = ""
    error: str = ""


class VCS(ABC):
    """Version control interface — two-phase revert."""

    @abstractmethod
    def commit(self, files: list[str], message: str) -> str:
        """Commit changes, returns hash."""
        ...

    @abstractmethod
    def diff_stat(self, files: list[str]) -> str:
        """Short stat summary for given files."""
        ...

    @abstractmethod
    def diff_full(self, files: list[str]) -> str:
        """Full unified diff for given files."""
        ...

    @abstractmethod
    def revert_preview(self, commit_hash: str) -> RevertPreview:
        """Dry-run revert to check for conflicts."""
        ...

    @abstractmethod
    def revert_execute(self, commit_hash: str) -> RevertResult:
        """Execute revert, returns result with new commit hash."""
        ...

    @abstractmethod
    def show_commit(self, commit_hash: str) -> dict:
        """Return {ok: bool, diff: str} for a commit's full diff."""
        ...

    @abstractmethod
    def log_live_edit_commits(self, limit: int = 30) -> list[dict]:
        """Return recent live-edit commits."""
        ...

    # ── Worktree / branch isolation (new) ──

    @abstractmethod
    def create_worktree(self, session_id: str) -> str:
        """Create an isolated worktree for a session. Returns the worktree path."""
        ...

    @abstractmethod
    def discard_session_branch(self, session_id: str, worktree_path: str = ""):
        """Remove the worktree (if still present) and delete branch live-edit/<session_id>."""
        ...

    @abstractmethod
    def remove_worktree_dir(self, worktree_path: str, session_id: str):
        """Remove the worktree directory only; keep the live-edit/<session_id> branch."""
        ...

    @abstractmethod
    def commit_in_worktree(self, worktree_path: str, files: list[str], message: str) -> str:
        """Commit changes inside a worktree. Returns the commit hash."""
        ...

    @abstractmethod
    def merge_commit(self, commit_hash: str, message: str) -> str:
        """Merge a commit into the main branch (--no-ff). Returns merge commit hash."""
        ...

    @abstractmethod
    def abort_merge(self):
        """Abort an in-progress merge on the main repo."""
        ...

    @abstractmethod
    def list_worktrees(self) -> list[dict]:
        """Return active live-edit worktrees with branch and session info."""
        ...

    @abstractmethod
    def list_unmerged_branches(self) -> list[dict]:
        """Return live-edit/* branches not yet merged into main.

        Each item: {session_id, branch, commit_hash, commit_time, subject}.
        """
        ...

    @abstractmethod
    def get_main_branch(self) -> str:
        """Return the name of the main branch (main or master)."""
        ...


class GitVCS(VCS):
    """Git VCS with worktree support for parallel session isolation."""

    def __init__(self, repo_path, worktree_ttl: int = 86400):
        self.repo_path = str(repo_path)
        self._main_branch: str | None = None
        self._worktree_ttl = worktree_ttl
        self.cleanup_stale_worktrees(self._worktree_ttl)

    # ── Main-branch detection ──

    def get_main_branch(self) -> str:
        if self._main_branch:
            return self._main_branch
        for candidate in ("main", "master"):
            result = subprocess.run(
                ["git", "rev-parse", "--verify", candidate],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=self.repo_path,
            )
            if result.returncode == 0:
                self._main_branch = candidate
                return candidate
        self._main_branch = "main"
        return "main"

    # ── Worktree lifecycle ──

    def cleanup_stale_worktrees(self, ttl_seconds: int | None = None):
        """Remove crashed-session leftovers idle longer than ttl_seconds.

        A freshly-crashed worktree (dir mtime inside the TTL) is kept so the
        session can be recovered via /continue. The engine refreshes the dir
        mtime every round; see engine.run_edit_session.
        """
        ttl = self._worktree_ttl if ttl_seconds is None else ttl_seconds
        if not os.path.isdir(_WORKTREE_ROOT):
            return
        # Resolve repo_path so we can detect when running inside a worktree
        my_path = os.path.abspath(self.repo_path)
        now = time.time()
        # Get list of registered worktrees
        try:
            result = subprocess.run(
                ["git", "worktree", "list", "--porcelain"],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=self.repo_path,
            )
            registered = set()
            for line in result.stdout.split("\n"):
                if line.startswith("worktree "):
                    registered.add(line.split("worktree ", 1)[1].strip())
        except Exception:
            registered = set()

        for name in os.listdir(_WORKTREE_ROOT):
            path = os.path.join(_WORKTREE_ROOT, name)
            if not os.path.isdir(path) or os.path.islink(path):
                continue  # skip symlinks (e.g. live-edit -> package source)
            # Skip the worktree this process is running from (preview server)
            if os.path.abspath(path) == my_path:
                continue
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue  # dir disappeared concurrently — nothing to clean
            if now - mtime < ttl:
                continue  # fresh crash — keep for recovery
            if path in registered:
                try:
                    # Remove the dir only; keep live-edit/<session_id> so a
                    # committed session stays mergeable via the admin UI.
                    self.remove_worktree_dir(path, name)
                    logger.info("Cleaned up stale worktree: %s", path)
                except Exception as e:
                    logger.warning("Failed to remove registered worktree %s: %s", path, e)
            else:
                # Not registered — just delete the directory
                try:
                    shutil.rmtree(path)
                    logger.info("Cleaned up orphan worktree dir: %s", path)
                except Exception as e:
                    logger.warning("Failed to remove orphan dir %s: %s", path, e)

    def _branch_exists(self, branch: str) -> bool:
        """True if a local branch with this name exists in the main repo."""
        result = subprocess.run(
            ["git", "rev-parse", "--verify", f"refs/heads/{branch}"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=self.repo_path,
        )
        return result.returncode == 0

    @staticmethod
    def _is_linked_worktree(path: str) -> bool:
        """True if path is a git-linked worktree (has a .git file marker).

        A linked worktree has a `.git` FILE pointing into the main repo's
        gitdir; the main repo has a `.git` DIRECTORY, so this never matches it.
        """
        return os.path.isfile(os.path.join(path, ".git"))

    def create_worktree(self, session_id: str) -> str:
        worktree_path = session_worktree_path(session_id)
        os.makedirs(_WORKTREE_ROOT, exist_ok=True)

        # Idempotent: a continue after a commit kept the worktree dir+branch
        # but cleared _worktree_path (engine._do_commit). Reuse the existing
        # linked worktree instead of failing on `git worktree add` (the path is
        # already registered).
        if self._is_linked_worktree(worktree_path):
            logger.info("Reusing existing worktree for session %s at %s", session_id, worktree_path)
            return worktree_path

        branch = f"live-edit/{session_id}"
        main = self.get_main_branch()
        if self._branch_exists(branch):
            # Session branch survived a worktree removal — check it out directly.
            subprocess.run(
                ["git", "worktree", "add", worktree_path, branch],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=self.repo_path,
                check=True,
            )
        else:
            subprocess.run(
                ["git", "worktree", "add", "--detach", worktree_path, main],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=self.repo_path,
                check=True,
            )
            subprocess.run(
                ["git", "-C", worktree_path, "checkout", "-b", branch],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
        # Symlink config files that aren't tracked by git but are needed
        # by the preview server (e.g. .live-edit.toml).
        _symlink_config(self.repo_path, worktree_path, ".live-edit.toml")

        logger.info("Created worktree for session %s at %s", session_id, worktree_path)
        return worktree_path

    def discard_session_branch(self, session_id: str, worktree_path: str = ""):
        """Remove worktree (if present) and delete branch live-edit/<session_id>.

        Tolerant: if the worktree dir is already gone, just delete the branch.
        """
        # Resolve worktree path if not provided
        if not worktree_path:
            for wt in self.list_worktrees():
                if wt.get("session_id") == session_id:
                    worktree_path = wt.get("path", "")
                    break
        if worktree_path and os.path.isdir(worktree_path):
            subprocess.run(
                ["git", "worktree", "remove", "--force", worktree_path],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=self.repo_path,
            )
        # Delete the session branch from the main repo
        subprocess.run(
            ["git", "branch", "-D", f"live-edit/{session_id}"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=self.repo_path,
        )
        logger.info("Discarded session branch live-edit/%s", session_id)

    def remove_worktree_dir(self, worktree_path: str, session_id: str):
        """Remove worktree dir only; keep the branch live-edit/<session_id>."""
        args = ["git", "worktree", "remove", "--force", worktree_path]
        subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=10,
            cwd=self.repo_path,
        )
        logger.info("Removed worktree dir (kept branch) for session %s", session_id)

    def list_worktrees(self) -> list[dict]:
        """Return active live-edit worktrees with branch and session info."""
        worktrees = []
        try:
            result = subprocess.run(
                ["git", "worktree", "list", "--porcelain"],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=self.repo_path,
            )
            # Parse porcelain output: each worktree has lines like:
            # worktree /path
            # HEAD <hash>
            # branch refs/heads/<name>
            current: dict[str, str] = {}
            for line in result.stdout.strip().split("\n"):
                line = line.strip()
                if line.startswith("worktree "):
                    if current:
                        worktrees.append(current)
                    current = {"path": line[len("worktree ") :]}
                elif line.startswith("HEAD "):
                    if current is not None:
                        current["commit_hash"] = line[len("HEAD ") :][:8]
                elif line.startswith("branch "):
                    if current is not None:
                        branch_ref = line[len("branch ") :]
                        current["branch"] = branch_ref.replace("refs/heads/", "")
            if current:
                worktrees.append(current)
        except Exception as e:
            logger.warning("list_worktrees error: %s", e)

        # Filter to live-edit worktrees only
        live_edit_wts = []
        for wt in worktrees:
            branch = wt.get("branch", "")
            if branch.startswith("live-edit/"):
                wt["session_id"] = branch[len("live-edit/") :]
                live_edit_wts.append(wt)
        return live_edit_wts

    def list_unmerged_branches(self) -> list[dict]:
        """Return live-edit/* branches not yet merged into main."""
        main = self.get_main_branch()
        result = subprocess.run(
            [
                "git",
                "for-each-ref",
                "--format=%(refname:short)|%(objectname:short)|%(committerdate:iso)|%(subject)",
                "refs/heads/live-edit/*",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=self.repo_path,
        )
        out = []
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            parts = line.split("|", 3)
            if len(parts) < 4:
                continue
            branch, short_hash, ctime, subject = parts
            # Skip if already merged into main
            anc = subprocess.run(
                ["git", "merge-base", "--is-ancestor", branch, main],
                capture_output=True,
                cwd=self.repo_path,
            )
            if anc.returncode == 0:
                continue
            session_id = branch[len("live-edit/") :]
            out.append(
                {
                    "session_id": session_id,
                    "branch": branch,
                    "commit_hash": short_hash,
                    "commit_time": ctime,
                    "subject": subject,
                }
            )
        return out

    # ── Commit / merge (worktree-aware) ──

    def commit_in_worktree(self, worktree_path: str, files: list[str], message: str) -> str:
        subprocess.run(
            ["git", "-C", worktree_path, "add", "--"] + files,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        subprocess.run(
            ["git", "-C", worktree_path, "commit", "-m", message],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        result = subprocess.run(
            ["git", "-C", worktree_path, "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.strip()

    def merge_commit(self, commit_hash: str, message: str) -> str:
        """Merge a worktree commit into the main branch with --no-ff."""
        main = self.get_main_branch()
        # Ensure we're on the main branch
        subprocess.run(
            ["git", "checkout", main],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=self.repo_path,
            check=False,
        )
        result = subprocess.run(
            ["git", "merge", "--no-ff", "-m", message, commit_hash],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=self.repo_path,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Merge conflict:\n{result.stderr[:1000]}")
        hash_result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=self.repo_path,
        )
        return hash_result.stdout.strip()

    def abort_merge(self):
        subprocess.run(
            ["git", "merge", "--abort"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=self.repo_path,
        )

    # ── Original commit (for backward compat — delegates to worktree commit now) ──

    def commit(self, files: list[str], message: str) -> str:
        subprocess.run(
            ["git", "add", "--"] + files,
            capture_output=True,
            text=True,
            timeout=10,
            cwd=self.repo_path,
            check=False,
        )
        subprocess.run(
            ["git", "commit", "-m", message],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=self.repo_path,
            check=False,
        )
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=self.repo_path,
        )
        return result.stdout.strip()

    def diff_stat(self, files: list[str]) -> str:
        result = subprocess.run(
            ["git", "diff", "--stat", "--"] + files,
            capture_output=True,
            text=True,
            timeout=10,
            cwd=self.repo_path,
        )
        return result.stdout.strip() or "(无变更)"

    def diff_full(self, files: list[str]) -> str:
        result = subprocess.run(
            ["git", "diff", "--"] + files,
            capture_output=True,
            text=True,
            timeout=10,
            cwd=self.repo_path,
        )
        return result.stdout.strip()

    def revert_preview(self, commit_hash: str) -> RevertPreview:
        if not commit_hash:
            return RevertPreview(ok=False, can_revert=False, files=[], error="缺少 commit hash")

        result = subprocess.run(
            ["git", "log", "-1", "--format=%s", commit_hash],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=self.repo_path,
        )
        if result.returncode != 0:
            return RevertPreview(
                ok=False, can_revert=False, files=[], error=f"commit {commit_hash} 不存在"
            )
        msg = result.stdout.strip()
        if not (msg.startswith("live-edit:") or msg.startswith("dev-mode:")):
            return RevertPreview(
                ok=False, can_revert=False, files=[], error="只能回滚 LiveEdit 做出的修改"
            )

        # Check for uncommitted changes to tracked files before revert.
        # Untracked files (??) don't block git revert, so exclude them with -uno.
        status = subprocess.run(
            ["git", "status", "--porcelain", "-uno"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=self.repo_path,
        )
        if status.stdout.strip():
            return RevertPreview(
                ok=False,
                can_revert=False,
                files=[],
                error="工作区有未提交的修改，请先提交或放弃后再回滚",
            )

        # Get live-edit commits in the range (from target exclusive to HEAD inclusive)
        rev_result = subprocess.run(
            [
                "git",
                "rev-list",
                "--reverse",
                f"{commit_hash}..HEAD",
                "--grep=live-edit:",
                "--grep=dev-mode:",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=self.repo_path,
        )
        target_commits = [c for c in rev_result.stdout.strip().split("\n") if c]

        if not target_commits:
            return RevertPreview(
                ok=False, can_revert=False, files=[], error="没有可回滚的 LiveEdit 提交"
            )

        # Revert each commit individually: merge commits need -m 1
        for c in target_commits:
            is_merge = (
                subprocess.run(
                    ["git", "rev-list", "--merges", "-1", c],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    cwd=self.repo_path,
                ).returncode
                == 0
            )

            args = ["git", "revert", "--no-commit", "--no-edit"]
            if is_merge:
                args += ["-m", "1"]
            args.append(c)
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=self.repo_path,
            )

        if result.returncode == 0:
            diff = subprocess.run(
                ["git", "diff", "--stat", "--cached"],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=self.repo_path,
            )
            files_result = subprocess.run(
                ["git", "diff", "--name-only", "--cached"],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=self.repo_path,
            )
            files = [f for f in files_result.stdout.strip().split("\n") if f]
            # Abort the dry-run
            subprocess.run(
                ["git", "revert", "--abort"],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=self.repo_path,
            )
            return RevertPreview(
                ok=True,
                can_revert=True,
                files=files,
                diff_summary=diff.stdout.strip(),
            )
        else:
            conflicts = []
            for line in result.stderr.split("\n"):
                if "CONFLICT" in line:
                    conflicts.append(line.strip())
            subprocess.run(
                ["git", "revert", "--abort"],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=self.repo_path,
            )
            return RevertPreview(
                ok=True,
                can_revert=False,
                files=[],
                conflicts=conflicts,
                error="回滚存在冲突，无法自动完成",
            )

    def revert_execute(self, commit_hash: str) -> RevertResult:
        if not commit_hash:
            return RevertResult(ok=False, error="缺少 commit hash")

        # Get live-edit commits in the range
        rev_result = subprocess.run(
            [
                "git",
                "rev-list",
                "--reverse",
                f"{commit_hash}..HEAD",
                "--grep=live-edit:",
                "--grep=dev-mode:",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=self.repo_path,
        )
        target_commits = [c for c in rev_result.stdout.strip().split("\n") if c]

        if not target_commits:
            return RevertResult(ok=False, error="没有可回滚的 LiveEdit 提交")

        # Revert each commit individually
        last_error = ""
        for c in target_commits:
            is_merge = (
                subprocess.run(
                    ["git", "rev-list", "--merges", "-1", c],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    cwd=self.repo_path,
                ).returncode
                == 0
            )

            args = ["git", "revert", "--no-commit"]
            if is_merge:
                args += ["-m", "1"]
            args.append(c)
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=self.repo_path,
            )
            if result.returncode != 0:
                last_error = result.stderr[:1000]
                subprocess.run(
                    ["git", "revert", "--abort"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    cwd=self.repo_path,
                )
                return RevertResult(ok=False, error=f"回滚失败:\n{last_error}")

        subprocess.run(
            ["git", "commit", "-m", f"live-edit: Revert to {commit_hash}"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=self.repo_path,
        )

        hash_result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=self.repo_path,
        )
        new_hash = hash_result.stdout.strip()
        return RevertResult(ok=True, new_commit_hash=new_hash, message=f"已回滚到 {commit_hash}")

    def show_commit(self, commit_hash: str) -> dict:
        try:
            result = subprocess.run(
                ["git", "show", "--stat", "--patch", commit_hash],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=self.repo_path,
            )
            return {"ok": True, "diff": result.stdout.strip()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def log_live_edit_commits(self, limit: int = 30) -> list[dict]:
        """Return recent live-edit merge commits (--first-parent skips worktree internals)."""
        try:
            result = subprocess.run(
                [
                    "git",
                    "log",
                    "--first-parent",
                    "--oneline",
                    "--grep=live-edit:",
                    "--grep=dev-mode:",
                    "--format=%h|%s|%ai",
                    f"-n{limit}",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=self.repo_path,
            )
            commits = []
            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                parts = line.split("|", 2)
                if len(parts) < 3:
                    continue
                commits.append(
                    {
                        "commit_hash": parts[0],
                        "message": parts[1],
                        "date": parts[2],
                    }
                )
            return commits
        except Exception as e:
            logger.warning("log_live_edit_commits error: %s", e)
            return []
