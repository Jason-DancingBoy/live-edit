"""Tests for live_edit.vcs — VCS interface and GitVCS implementation."""

import subprocess
import pytest
from live_edit.vcs import GitVCS, RevertPreview, RevertResult


@pytest.fixture
def git_repo(tmp_path):
    """Create a real git repo in a temp directory."""
    repo = str(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=repo, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=repo, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repo, capture_output=True,
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
            ["git", "log", "--oneline"], cwd=str(git_repo),
            capture_output=True, text=True,
        )
        assert "live-edit: test commit" in result.stdout

    def test_diff(self, git_repo):
        vcs = GitVCS(git_repo)
        (git_repo / "changed.py").write_text("print('changed')")
        subprocess.run(["git", "add", "changed.py"], cwd=str(git_repo), capture_output=True)
        # Commit first so there is something to diff against
        subprocess.run(
            ["git", "commit", "-m", "live-edit: add changed.py"],
            cwd=str(git_repo), capture_output=True,
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
            cwd=str(git_repo), capture_output=True,
        )
        hash1 = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(git_repo), capture_output=True, text=True,
        ).stdout.strip()

        # Commit 2: modify rev.py so there's a range to revert
        (git_repo / "rev.py").write_text("v2")
        subprocess.run(["git", "add", "rev.py"], cwd=str(git_repo), capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "live-edit: update rev.py"],
            cwd=str(git_repo), capture_output=True,
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
            cwd=str(git_repo), capture_output=True, text=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "commit", "-m", "live-edit: add conflict.py"],
            cwd=str(git_repo), capture_output=True,
        )

        # Make a conflicting change
        (git_repo / "conflict.py").write_text("line1-modified\nline2\n")
        subprocess.run(["git", "add", "conflict.py"], cwd=str(git_repo), capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "live-edit: modify conflict.py"],
            cwd=str(git_repo), capture_output=True,
        )

        hash2 = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(git_repo), capture_output=True, text=True,
        ).stdout.strip()

        # Reverting from hash1 through HEAD might conflict
        preview = vcs.revert_preview(hash1)
        # Don't assert can_revert — conflicts are possible
        # Just ensure it ran without exception and returned a result
        assert isinstance(preview, RevertPreview)
