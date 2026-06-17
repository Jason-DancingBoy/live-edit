"""Version Control interface and default Git implementation with worktree isolation."""

import logging
import os
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass


logger = logging.getLogger("live-edit.vcs")

_WORKTREE_ROOT = "/tmp/live-edit"


@dataclass
class RevertPreview:
    ok: bool
    can_revert: bool
    files: list[str]

    diff_summary: str = ""
    conflicts: list[str] = ()
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
    def remove_worktree(self, worktree_path: str, session_id: str, force: bool = False):
        """Remove a session worktree and its branch."""
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
    def get_main_branch(self) -> str:
        """Return the name of the main branch (main or master)."""
        ...


class GitVCS(VCS):
    """Git VCS with worktree support for parallel session isolation."""

    def __init__(self, repo_path):
        self.repo_path = str(repo_path)
        self._main_branch: str | None = None
        self.cleanup_stale_worktrees()

    # ── Main-branch detection ──

    def get_main_branch(self) -> str:
        if self._main_branch:
            return self._main_branch
        for candidate in ("main", "master"):
            result = subprocess.run(
                ["git", "rev-parse", "--verify", candidate],
                capture_output=True, text=True, timeout=10, cwd=self.repo_path,
            )
            if result.returncode == 0:
                self._main_branch = candidate
                return candidate
        self._main_branch = "main"
        return "main"

    # ── Worktree lifecycle ──

    def cleanup_stale_worktrees(self):
        """Remove leftover worktrees from crashed sessions."""
        if not os.path.isdir(_WORKTREE_ROOT):
            return
        # Get list of registered worktrees
        try:
            result = subprocess.run(
                ["git", "worktree", "list", "--porcelain"],
                capture_output=True, text=True, timeout=10, cwd=self.repo_path,
            )
            registered = set()
            for line in result.stdout.split("\n"):
                if line.startswith("worktree "):
                    registered.add(line.split("worktree ", 1)[1].strip())
        except Exception:
            registered = set()

        for name in os.listdir(_WORKTREE_ROOT):
            path = os.path.join(_WORKTREE_ROOT, name)
            if not os.path.isdir(path):
                continue
            if path in registered:
                try:
                    self.remove_worktree(path, name, force=True)
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

    def create_worktree(self, session_id: str) -> str:
        worktree_path = os.path.join(_WORKTREE_ROOT, session_id)
        os.makedirs(_WORKTREE_ROOT, exist_ok=True)

        main = self.get_main_branch()
        subprocess.run(
            ["git", "worktree", "add", "--detach", worktree_path, main],
            capture_output=True, text=True, timeout=30, cwd=self.repo_path,
            check=True,
        )
        subprocess.run(
            ["git", "-C", worktree_path, "checkout", "-b", f"live-edit/{session_id}"],
            capture_output=True, text=True, timeout=10,
            check=True,
        )
        logger.info("Created worktree for session %s at %s", session_id, worktree_path)
        return worktree_path

    def remove_worktree(self, worktree_path: str, session_id: str, force: bool = False):
        args = ["git", "worktree", "remove"]
        if force:
            args.append("--force")
        args.append(worktree_path)
        subprocess.run(
            args,
            capture_output=True, text=True, timeout=10, cwd=self.repo_path,
        )
        # Delete the session branch from the main repo
        subprocess.run(
            ["git", "branch", "-D", f"live-edit/{session_id}"],
            capture_output=True, text=True, timeout=10, cwd=self.repo_path,
        )
        logger.info("Removed worktree for session %s", session_id)

    def list_worktrees(self) -> list[dict]:
        """Return active live-edit worktrees with branch and session info."""
        worktrees = []
        try:
            result = subprocess.run(
                ["git", "worktree", "list", "--porcelain"],
                capture_output=True, text=True, timeout=10, cwd=self.repo_path,
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
                    current = {"path": line[len("worktree "):]}
                elif line.startswith("HEAD "):
                    if current is not None:
                        current["commit_hash"] = line[len("HEAD "):][:8]
                elif line.startswith("branch "):
                    if current is not None:
                        branch_ref = line[len("branch "):]
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
                wt["session_id"] = branch[len("live-edit/"):]
                live_edit_wts.append(wt)
        return live_edit_wts

    # ── Commit / merge (worktree-aware) ──

    def commit_in_worktree(self, worktree_path: str, files: list[str], message: str) -> str:
        subprocess.run(
            ["git", "-C", worktree_path, "add", "--"] + files,
            capture_output=True, text=True, timeout=10,
            check=False,
        )
        subprocess.run(
            ["git", "-C", worktree_path, "commit", "-m", message],
            capture_output=True, text=True, timeout=10,
            check=False,
        )
        result = subprocess.run(
            ["git", "-C", worktree_path, "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        return result.stdout.strip()

    def merge_commit(self, commit_hash: str, message: str) -> str:
        """Merge a worktree commit into the main branch with --no-ff."""
        main = self.get_main_branch()
        # Ensure we're on the main branch
        subprocess.run(
            ["git", "checkout", main],
            capture_output=True, text=True, timeout=10, cwd=self.repo_path,
            check=False,
        )
        result = subprocess.run(
            ["git", "merge", "--no-ff", "-m", message, commit_hash],
            capture_output=True, text=True, timeout=30, cwd=self.repo_path,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Merge conflict:\n{result.stderr[:1000]}")
        hash_result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10, cwd=self.repo_path,
        )
        return hash_result.stdout.strip()

    def abort_merge(self):
        subprocess.run(
            ["git", "merge", "--abort"],
            capture_output=True, text=True, timeout=10, cwd=self.repo_path,
        )

    # ── Original commit (for backward compat — delegates to worktree commit now) ──

    def commit(self, files: list[str], message: str) -> str:
        subprocess.run(
            ["git", "add", "--"] + files,
            capture_output=True, text=True,
            timeout=10, cwd=self.repo_path,
            check=False,
        )
        subprocess.run(
            ["git", "commit", "-m", message],
            capture_output=True, text=True,
            timeout=10, cwd=self.repo_path,
            check=False,
        )
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True,
            timeout=10, cwd=self.repo_path,
        )
        return result.stdout.strip()

    def diff_stat(self, files: list[str]) -> str:
        result = subprocess.run(
            ["git", "diff", "--stat", "--"] + files,
            capture_output=True, text=True,
            timeout=10, cwd=self.repo_path,
        )
        return result.stdout.strip() or "(无变更)"

    def diff_full(self, files: list[str]) -> str:
        result = subprocess.run(
            ["git", "diff", "--"] + files,
            capture_output=True, text=True,
            timeout=10, cwd=self.repo_path,
        )
        return result.stdout.strip()

    def revert_preview(self, commit_hash: str) -> RevertPreview:
        if not commit_hash:
            return RevertPreview(ok=False, can_revert=False, files=[],
                                 error="缺少 commit hash")

        result = subprocess.run(
            ["git", "log", "-1", "--format=%s", commit_hash],
            capture_output=True, text=True, timeout=10, cwd=self.repo_path,
        )
        if result.returncode != 0:
            return RevertPreview(ok=False, can_revert=False, files=[],
                                 error=f"commit {commit_hash} 不存在")
        msg = result.stdout.strip()
        if not (msg.startswith("live-edit:") or msg.startswith("dev-mode:")):
            return RevertPreview(ok=False, can_revert=False, files=[],
                                 error="只能回滚 LiveEdit 做出的修改")

        # Check for uncommitted changes to tracked files before revert.
        # Untracked files (??) don't block git revert, so exclude them with -uno.
        status = subprocess.run(
            ["git", "status", "--porcelain", "-uno"],
            capture_output=True, text=True, timeout=10, cwd=self.repo_path,
        )
        if status.stdout.strip():
            return RevertPreview(ok=False, can_revert=False, files=[],
                                 error="工作区有未提交的修改，请先提交或放弃后再回滚")

        # Get live-edit commits in the range (from target exclusive to HEAD inclusive)
        rev_result = subprocess.run(
            ["git", "rev-list", "--reverse", f"{commit_hash}..HEAD",
             "--grep=live-edit:", "--grep=dev-mode:"],
            capture_output=True, text=True, timeout=10, cwd=self.repo_path,
        )
        target_commits = [c for c in rev_result.stdout.strip().split("\n") if c]

        if not target_commits:
            return RevertPreview(ok=False, can_revert=False, files=[],
                                 error="没有可回滚的 LiveEdit 提交")

        # Revert each commit individually: merge commits need -m 1
        for c in target_commits:
            is_merge = subprocess.run(
                ["git", "rev-list", "--merges", "-1", c],
                capture_output=True, text=True, timeout=10, cwd=self.repo_path,
            ).returncode == 0

            args = ["git", "revert", "--no-commit", "--no-edit"]
            if is_merge:
                args += ["-m", "1"]
            args.append(c)
            result = subprocess.run(
                args, capture_output=True, text=True, timeout=30, cwd=self.repo_path,
            )

        if result.returncode == 0:
            diff = subprocess.run(
                ["git", "diff", "--stat", "--cached"],
                capture_output=True, text=True, timeout=10, cwd=self.repo_path,
            )
            files_result = subprocess.run(
                ["git", "diff", "--name-only", "--cached"],
                capture_output=True, text=True, timeout=10, cwd=self.repo_path,
            )
            files = [f for f in files_result.stdout.strip().split("\n") if f]
            # Abort the dry-run
            subprocess.run(
                ["git", "revert", "--abort"],
                capture_output=True, text=True, timeout=10, cwd=self.repo_path,
            )
            return RevertPreview(
                ok=True, can_revert=True,
                files=files, diff_summary=diff.stdout.strip(),
            )
        else:
            conflicts = []
            for line in result.stderr.split("\n"):
                if "CONFLICT" in line:
                    conflicts.append(line.strip())
            subprocess.run(
                ["git", "revert", "--abort"],
                capture_output=True, text=True, timeout=10, cwd=self.repo_path,
            )
            return RevertPreview(
                ok=True, can_revert=False,
                files=[], conflicts=conflicts,
                error="回滚存在冲突，无法自动完成",
            )

    def revert_execute(self, commit_hash: str) -> RevertResult:
        if not commit_hash:
            return RevertResult(ok=False, error="缺少 commit hash")

        # Get live-edit commits in the range
        rev_result = subprocess.run(
            ["git", "rev-list", "--reverse", f"{commit_hash}..HEAD",
             "--grep=live-edit:", "--grep=dev-mode:"],
            capture_output=True, text=True, timeout=10, cwd=self.repo_path,
        )
        target_commits = [c for c in rev_result.stdout.strip().split("\n") if c]

        if not target_commits:
            return RevertResult(ok=False, error="没有可回滚的 LiveEdit 提交")

        # Revert each commit individually
        last_error = ""
        for c in target_commits:
            is_merge = subprocess.run(
                ["git", "rev-list", "--merges", "-1", c],
                capture_output=True, text=True, timeout=10, cwd=self.repo_path,
            ).returncode == 0

            args = ["git", "revert", "--no-commit"]
            if is_merge:
                args += ["-m", "1"]
            args.append(c)
            result = subprocess.run(
                args, capture_output=True, text=True, timeout=30, cwd=self.repo_path,
            )
            if result.returncode != 0:
                last_error = result.stderr[:1000]
                subprocess.run(
                    ["git", "revert", "--abort"],
                    capture_output=True, text=True, timeout=10, cwd=self.repo_path,
                )
                return RevertResult(ok=False, error=f"回滚失败:\n{last_error}")

        subprocess.run(
            ["git", "commit", "-m", f"live-edit: Revert to {commit_hash}"],
            capture_output=True, text=True, timeout=10, cwd=self.repo_path,
        )

        hash_result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10, cwd=self.repo_path,
        )
        new_hash = hash_result.stdout.strip()
        return RevertResult(ok=True, new_commit_hash=new_hash,
                           message=f"已回滚到 {commit_hash}")

    def show_commit(self, commit_hash: str) -> dict:
        try:
            result = subprocess.run(
                ["git", "show", "--stat", "--patch", commit_hash],
                capture_output=True, text=True, timeout=10, cwd=self.repo_path,
            )
            return {"ok": True, "diff": result.stdout.strip()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def log_live_edit_commits(self, limit: int = 30) -> list[dict]:
        """Return recent live-edit merge commits (--first-parent skips worktree internals)."""
        try:
            result = subprocess.run(
                ["git", "log", "--first-parent", "--oneline",
                 "--grep=live-edit:", "--grep=dev-mode:",
                 "--format=%h|%s|%ai",
                 f"-n{limit}"],
                capture_output=True, text=True, timeout=10, cwd=self.repo_path,
            )
            commits = []
            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                parts = line.split("|", 2)
                if len(parts) < 3:
                    continue
                commits.append({
                    "commit_hash": parts[0],
                    "message": parts[1],
                    "date": parts[2],
                })
            return commits
        except Exception as e:
            logger.warning("log_live_edit_commits error: %s", e)
            return []
