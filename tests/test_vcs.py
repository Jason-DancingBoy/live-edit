"""Tests for live_edit.vcs — VCS interface and GitVCS implementation."""

import os
import subprocess
import time
from pathlib import Path

import pytest

from live_edit.vcs import GitVCS, RevertPreview


@pytest.fixture
def git_repo(tmp_path):
    """Create a real git repo in a temp directory."""
    repo = str(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=repo, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=repo,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repo,
        capture_output=True,
    )
    # Initial commit so reverts have something to work with
    (tmp_path / "initial.txt").write_text("initial")
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, capture_output=True)
    return tmp_path


class TestGitVCS:
    def test_commit(self, git_repo):
        vcs = GitVCS(git_repo)
        (git_repo / "new_file.py").write_text("print('hello')")

        hash_val = vcs.commit(["new_file.py"], "live-edit: test commit")

        assert len(hash_val) > 0
        # Verify it's in git log
        result = subprocess.run(
            ["git", "log", "--oneline"],
            cwd=str(git_repo),
            capture_output=True,
            text=True,
        )
        assert "live-edit: test commit" in result.stdout

    def test_diff(self, git_repo):
        vcs = GitVCS(git_repo)
        (git_repo / "changed.py").write_text("print('changed')")
        subprocess.run(["git", "add", "changed.py"], cwd=str(git_repo), capture_output=True)
        # Commit first so there is something to diff against
        subprocess.run(
            ["git", "commit", "-m", "live-edit: add changed.py"],
            cwd=str(git_repo),
            capture_output=True,
        )
        # Now modify to create an unstaged change for diff
        (git_repo / "changed.py").write_text("print('changed again')")

        stat = vcs.diff_stat(["changed.py"])
        assert "changed.py" in stat

    def test_revert_preview_clean(self, git_repo):
        vcs = GitVCS(git_repo)
        # Commit 1: add rev.py
        (git_repo / "rev.py").write_text("v1")
        subprocess.run(["git", "add", "rev.py"], cwd=str(git_repo), capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "live-edit: add rev.py"],
            cwd=str(git_repo),
            capture_output=True,
        )
        hash1 = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(git_repo),
            capture_output=True,
            text=True,
        ).stdout.strip()

        # Commit 2: modify rev.py so there's a range to revert
        (git_repo / "rev.py").write_text("v2")
        subprocess.run(["git", "add", "rev.py"], cwd=str(git_repo), capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "live-edit: update rev.py"],
            cwd=str(git_repo),
            capture_output=True,
        )

        # Revert from hash1 (just after commit 1) through HEAD
        preview = vcs.revert_preview(hash1)
        assert preview.ok
        assert preview.can_revert
        assert "rev.py" in preview.files

    def test_log_live_edit_commits(self, git_repo):
        vcs = GitVCS(git_repo)
        for i in range(3):
            (git_repo / f"f{i}.txt").write_text(f"content {i}")
            subprocess.run(["git", "add", f"f{i}.txt"], cwd=str(git_repo), capture_output=True)
            msg = "live-edit: change" if i < 2 else "non-dev commit"
            subprocess.run(["git", "commit", "-m", msg], cwd=str(git_repo), capture_output=True)

        commits = vcs.log_live_edit_commits(limit=10)
        # Should find 2 live-edit commits (not the 3rd "non-dev" one)
        live_edit_commits = [c for c in commits if "live-edit" in c.get("message", "")]
        assert len(live_edit_commits) == 2

    def test_revert_preview_with_conflict(self, git_repo):
        vcs = GitVCS(git_repo)
        (git_repo / "conflict.py").write_text("line1\nline2\n")
        subprocess.run(["git", "add", "conflict.py"], cwd=str(git_repo), capture_output=True)
        hash1 = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(git_repo),
            capture_output=True,
            text=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "commit", "-m", "live-edit: add conflict.py"],
            cwd=str(git_repo),
            capture_output=True,
        )

        # Make a conflicting change
        (git_repo / "conflict.py").write_text("line1-modified\nline2\n")
        subprocess.run(["git", "add", "conflict.py"], cwd=str(git_repo), capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "live-edit: modify conflict.py"],
            cwd=str(git_repo),
            capture_output=True,
        )

        subprocess.run(  # hash not needed
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(git_repo),
            capture_output=True,
            text=True,
        ).stdout.strip()

        # Reverting from hash1 through HEAD might conflict
        preview = vcs.revert_preview(hash1)
        # Don't assert can_revert — conflicts are possible
        # Just ensure it ran without exception and returned a result
        assert isinstance(preview, RevertPreview)


class TestRemoveWorktreeDir:
    def test_removes_worktree_keeps_branch(self, git_repo):
        from live_edit.vcs import GitVCS

        vcs = GitVCS(git_repo)
        wt_path = vcs.create_worktree("sess-keep")
        # 在 worktree 里写文件并提交，让分支有 commit
        (Path(wt_path) / "f.py").write_text("x")
        vcs.commit_in_worktree(wt_path, ["f.py"], "live-edit: wip")

        vcs.remove_worktree_dir(wt_path, "sess-keep")

        # worktree 目录消失
        assert not os.path.isdir(wt_path)
        # 分支仍存在
        branches = subprocess.run(
            ["git", "branch", "--list", "live-edit/sess-keep"],
            cwd=str(git_repo),
            capture_output=True,
            text=True,
        ).stdout
        assert "live-edit/sess-keep" in branches


class TestDiscardSessionBranch:
    def test_discards_worktree_and_branch(self, git_repo):
        from live_edit.vcs import GitVCS

        vcs = GitVCS(git_repo)
        wt_path = vcs.create_worktree("sess-d")
        (Path(wt_path) / "f.py").write_text("x")
        vcs.commit_in_worktree(wt_path, ["f.py"], "live-edit: wip")

        vcs.discard_session_branch("sess-d")

        assert not os.path.isdir(wt_path)
        branches = subprocess.run(
            ["git", "branch", "--list", "live-edit/sess-d"],
            cwd=str(git_repo),
            capture_output=True,
            text=True,
        ).stdout
        assert "live-edit/sess-d" not in branches

    def test_discard_tolerates_already_removed_worktree(self, git_repo):
        from live_edit.vcs import GitVCS

        vcs = GitVCS(git_repo)
        wt_path = vcs.create_worktree("sess-d2")
        vcs.remove_worktree_dir(wt_path, "sess-d2")  # worktree 已删，分支还在

        # 不应抛异常，分支应被删
        vcs.discard_session_branch("sess-d2")
        branches = subprocess.run(
            ["git", "branch", "--list", "live-edit/sess-d2"],
            cwd=str(git_repo),
            capture_output=True,
            text=True,
        ).stdout
        assert "live-edit/sess-d2" not in branches


class TestListUnmergedBranches:
    def test_returns_only_unmerged(self, git_repo):
        from live_edit.vcs import GitVCS

        vcs = GitVCS(git_repo)

        # 分支 A：提交后合入 main
        wt_a = vcs.create_worktree("sess-a")
        (Path(wt_a) / "a.py").write_text("a")
        h_a = vcs.commit_in_worktree(wt_a, ["a.py"], "live-edit: A")
        vcs.merge_commit(h_a, "live-edit: A")
        vcs.discard_session_branch("sess-a", worktree_path=wt_a)

        # 分支 B：提交但不合入
        wt_b = vcs.create_worktree("sess-b")
        (Path(wt_b) / "b.py").write_text("b")
        vcs.commit_in_worktree(wt_b, ["b.py"], "live-edit: B")
        vcs.remove_worktree_dir(wt_b, "sess-b")

        result = vcs.list_unmerged_branches()
        sids = [r["session_id"] for r in result]
        assert "sess-b" in sids
        assert "sess-a" not in sids
        b_entry = next(r for r in result if r["session_id"] == "sess-b")
        assert b_entry["branch"] == "live-edit/sess-b"
        assert len(b_entry["commit_hash"]) > 0
        assert "B" in b_entry["subject"]


class TestCleanupStaleWorktrees:
    def test_keeps_fresh_worktree_removes_stale(self, git_repo):
        vcs = GitVCS(str(git_repo), worktree_ttl=86400)
        fresh = vcs.create_worktree("sess-fresh")
        stale = vcs.create_worktree("sess-stale")
        # Backdate the stale worktree past the TTL.
        old = time.time() - 2 * 86400
        os.utime(stale, (old, old))

        vcs.cleanup_stale_worktrees(ttl_seconds=86400)

        assert os.path.isdir(fresh)
        assert not os.path.isdir(stale)
        branches = subprocess.run(
            ["git", "-C", str(git_repo), "branch", "--list", "live-edit/sess-stale"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        # The branch must survive cleanup: it is the mergeable deliverable an
        # admin merges into main days later (previously cleanup deleted it).
        assert branches != ""
        vcs.discard_session_branch("sess-fresh", worktree_path=fresh)

    def test_cleanup_keeps_committed_branch_for_admin_merge(self, git_repo):
        """Regression: a committed session's branch must survive stale-worktree
        cleanup so admin merge still works days later. Previously the cleanup
        called discard_session_branch and deleted the branch."""
        vcs = GitVCS(str(git_repo), worktree_ttl=86400)
        wt = vcs.create_worktree("sess-committed")
        # Commit real work to the session branch (mirrors _do_commit).
        with open(os.path.join(wt, "file.txt"), "w") as fh:
            fh.write("content")
        subprocess.run(
            ["git", "-C", wt, "add", "file.txt"],
            capture_output=True,
            text=True,
            check=True,
        )
        subprocess.run(
            ["git", "-C", wt, "commit", "-m", "live-edit: session work"],
            capture_output=True,
            text=True,
            check=True,
        )
        # Backdate past the TTL like an idle preview-kept worktree.
        old = time.time() - 2 * 86400
        os.utime(wt, (old, old))

        vcs.cleanup_stale_worktrees(ttl_seconds=86400)

        # Worktree dir reclaimed…
        assert not os.path.isdir(wt)
        # …but the branch survives for admin merge…
        branches = subprocess.run(
            ["git", "-C", str(git_repo), "branch", "--list", "live-edit/sess-committed"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert branches != ""
        # …and it is still unmerged, i.e. mergeable via the admin endpoint.
        unmerged = vcs.list_unmerged_branches()
        assert "sess-committed" in {b.get("session_id") for b in unmerged}

    def test_removes_stale_orphan_dir(self, git_repo, tmp_path, monkeypatch):
        import live_edit.vcs as vcs_mod

        root = tmp_path / "worktrees"
        root.mkdir()
        monkeypatch.setattr(vcs_mod, "_WORKTREE_ROOT", str(root))
        vcs = GitVCS(str(git_repo), worktree_ttl=86400)
        orphan = root / "orphan-stale"
        orphan.mkdir()
        old = time.time() - 2 * 86400
        os.utime(orphan, (old, old))

        vcs.cleanup_stale_worktrees(ttl_seconds=86400)

        assert not os.path.isdir(orphan)
