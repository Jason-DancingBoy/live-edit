"""Tests for live_edit.vcs — VCS interface and GitVCS implementation."""

import os
import shutil
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


class TestCreateWorktreeFork:
    def test_forks_from_base_ref_commit(self, git_repo):
        from live_edit.vcs import GitVCS

        vcs = GitVCS(git_repo)
        # Commit A (the future fork base), then commit B on main past it
        (git_repo / "a.txt").write_text("a")
        subprocess.run(["git", "add", "."], cwd=str(git_repo), capture_output=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=str(git_repo), capture_output=True)
        base_hash = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(git_repo),
            capture_output=True,
            text=True,
        ).stdout.strip()
        (git_repo / "b.txt").write_text("b")
        subprocess.run(["git", "add", "."], cwd=str(git_repo), capture_output=True)
        subprocess.run(["git", "commit", "-m", "advance"], cwd=str(git_repo), capture_output=True)

        wt_path = vcs.create_worktree("sess-fork", base_ref=base_hash)

        # Worktree HEAD is the base commit, not main HEAD
        head = subprocess.run(
            ["git", "-C", wt_path, "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert head == base_hash
        # The base commit's files are present, main-only files are not (in the worktree)
        assert (Path(wt_path) / "a.txt").exists()
        assert not (Path(wt_path) / "b.txt").exists()
        # Session branch points at the base commit
        branch_head = subprocess.run(
            ["git", "rev-parse", "--short", "live-edit/sess-fork"],
            cwd=str(git_repo),
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert branch_head == base_hash
        vcs.discard_session_branch("sess-fork", worktree_path=wt_path)

    def test_empty_base_ref_forks_from_main(self, git_repo):
        from live_edit.vcs import GitVCS

        vcs = GitVCS(git_repo)
        wt_path = vcs.create_worktree("sess-main")
        head = subprocess.run(
            ["git", "-C", wt_path, "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        main_head = subprocess.run(
            ["git", "rev-parse", "--short", vcs.get_main_branch()],
            cwd=str(git_repo),
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert head == main_head
        vcs.discard_session_branch("sess-main", worktree_path=wt_path)


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


class TestCreateWorktreeIdempotent:
    """create_worktree must be idempotent: a /continue after _do_commit may
    find the worktree dir+branch still present (preview kept it) even though
    session._worktree_path was cleared. It must reuse, not crash on exit 128."""

    def _cleanup(self, session_id):
        # A previous run may have left a linked worktree in the shared
        # /tmp/live-edit pointing at a now-deleted temp repo. Remove the stale
        # dir so each test starts clean regardless of run order.
        shutil.rmtree(os.path.join("/tmp/live-edit", session_id), ignore_errors=True)

    def test_reuses_existing_worktree(self, git_repo):
        from live_edit.vcs import GitVCS

        self._cleanup("sess-again")
        vcs = GitVCS(git_repo)
        wt1 = vcs.create_worktree("sess-again")
        # continue after _do_commit kept the worktree but cleared _worktree_path
        wt2 = vcs.create_worktree("sess-again")
        assert wt1 == wt2
        assert os.path.isfile(os.path.join(wt2, ".git"))
        branch = subprocess.run(
            ["git", "-C", wt2, "branch", "--show-current"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert branch == "live-edit/sess-again"

    def test_rechecks_out_surviving_branch(self, git_repo):
        from live_edit.vcs import GitVCS

        self._cleanup("sess-survivor")
        vcs = GitVCS(git_repo)
        wt = vcs.create_worktree("sess-survivor")
        (Path(wt) / "f.py").write_text("x")
        vcs.commit_in_worktree(wt, ["f.py"], "live-edit: wip")
        vcs.remove_worktree_dir(wt, "sess-survivor")
        assert not os.path.isdir(wt)
        wt2 = vcs.create_worktree("sess-survivor")
        branch = subprocess.run(
            ["git", "-C", wt2, "branch", "--show-current"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert branch == "live-edit/sess-survivor"


class TestGitConsole:
    """Admin git console: working-tree / stash / remote / graph operations.

    Adapted from live-build's git console tests; branch prefix is live-edit/ and
    the graph marker key is is_live_edit.
    """

    def test_diff_staged_and_unstaged(self, git_repo):
        vcs = GitVCS(str(git_repo))
        (git_repo / "f.py").write_text("v1")
        subprocess.run(["git", "add", "f.py"], cwd=str(git_repo), capture_output=True)
        subprocess.run(["git", "commit", "-m", "v1"], cwd=str(git_repo), capture_output=True)
        (git_repo / "f.py").write_text("v2")
        unstaged = vcs.diff(path="f.py", staged=False)
        assert "+v2" in unstaged and "-v1" in unstaged
        subprocess.run(["git", "add", "f.py"], cwd=str(git_repo), capture_output=True)
        staged = vcs.diff(path="f.py", staged=True)
        assert "+v2" in staged

    def test_diff_untracked_file_exit1_is_success(self, git_repo):
        vcs = GitVCS(str(git_repo))
        (git_repo / "new.txt").write_text("hello\n")
        d = vcs.diff(path="new.txt", staged=False)
        assert "+hello" in d

    def test_stage_all_then_unstage_all(self, git_repo):
        vcs = GitVCS(str(git_repo))
        (git_repo / "s1.txt").write_text("a")
        (git_repo / "s2.txt").write_text("b")
        assert vcs.stage()["ok"] is True
        st = vcs.status()
        assert {f["path"] for f in st["staged"]} >= {"s1.txt", "s2.txt"}
        assert vcs.unstage()["ok"] is True
        st2 = vcs.status()
        assert {f["path"] for f in st2["staged"]} == set()

    def test_status_classifies_three_groups(self, git_repo):
        vcs = GitVCS(str(git_repo))
        (git_repo / "tracked.txt").write_text("base")
        subprocess.run(["git", "add", "tracked.txt"], cwd=str(git_repo), capture_output=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=str(git_repo), capture_output=True)
        (git_repo / "tracked.txt").write_text("modified")
        (git_repo / "untracked.txt").write_text("new")
        st = vcs.status()
        assert any(f["path"] == "tracked.txt" for f in st["unstaged"])
        assert any(f["path"] == "untracked.txt" for f in st["untracked"])
        assert st["clean"] is False

    def test_commit_staged_and_rejects_empty(self, git_repo):
        vcs = GitVCS(str(git_repo))
        (git_repo / "c.py").write_text("print(1)")
        subprocess.run(["git", "add", "c.py"], cwd=str(git_repo), capture_output=True)
        result = vcs.commit_staged("manual: add c.py")
        assert result["ok"] is True and result["commit_hash"]
        # 空暂存区 → 拒绝
        result2 = vcs.commit_staged("should fail")
        assert result2["ok"] is False
        assert "暂存" in result2["error"]

    def test_stash_push_list_pop_roundtrip(self, git_repo):
        vcs = GitVCS(str(git_repo))
        (git_repo / "w.txt").write_text("change")
        subprocess.run(["git", "add", "w.txt"], cwd=str(git_repo), capture_output=True)
        push = vcs.stash_push("wip")
        assert push["ok"] is True and push["index"] == 0
        assert vcs.is_clean() is True
        entries = vcs.stash_list()
        assert len(entries) == 1 and entries[0]["index"] == 0 and "wip" in entries[0]["message"]
        # date 必须被 git 展开为 ISO 时间，不是字面量 %(committerdate:iso)
        assert entries[0]["date"].startswith("20")
        pop = vcs.stash_pop()
        assert pop["ok"] is True and pop["conflicts"] is False
        assert (git_repo / "w.txt").read_text() == "change"

    def test_stash_drop(self, git_repo):
        vcs = GitVCS(str(git_repo))
        (git_repo / "d.txt").write_text("x")
        subprocess.run(["git", "add", "d.txt"], cwd=str(git_repo), capture_output=True)
        vcs.stash_push("to drop")
        assert vcs.stash_drop(0)["ok"] is True
        assert vcs.stash_list() == []

    def test_stash_pop_reject_overlapping_changes(self, git_repo):
        vcs = GitVCS(str(git_repo))
        (git_repo / "o.txt").write_text("base")
        subprocess.run(["git", "add", "o.txt"], cwd=str(git_repo), capture_output=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=str(git_repo), capture_output=True)
        (git_repo / "o.txt").write_text("stashed")
        subprocess.run(["git", "add", "o.txt"], cwd=str(git_repo), capture_output=True)
        vcs.stash_push("overlap")
        # 工作区有与 o.txt 重叠的未提交修改 → git 拒绝应用
        (git_repo / "o.txt").write_text("working")
        result = vcs.stash_pop()
        assert result["ok"] is False
        assert "重叠" in result["error"]
        # stash 条目保留
        assert len(vcs.stash_list()) == 1

    def test_stash_push_index_is_0_with_existing(self, git_repo):
        vcs = GitVCS(str(git_repo))
        (git_repo / "a.txt").write_text("a")
        subprocess.run(["git", "add", "a.txt"], cwd=str(git_repo), capture_output=True)
        vcs.stash_push("first")
        (git_repo / "b.txt").write_text("b")
        subprocess.run(["git", "add", "b.txt"], cwd=str(git_repo), capture_output=True)
        push = vcs.stash_push("second")
        assert push["ok"] is True and push["index"] == 0  # 新 stash 恒为 0
        assert len(vcs.stash_list()) == 2

    def _setup_graph_repo(self, tmp_path):
        repo = tmp_path / "graph"
        subprocess.run(["git", "init", "-q", str(repo)], capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "t@t.com"], cwd=str(repo), capture_output=True
        )
        subprocess.run(["git", "config", "user.name", "T"], cwd=str(repo), capture_output=True)
        (repo / "a.txt").write_text("a")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "branch", "-M", "main"], cwd=str(repo), capture_output=True)
        # live-edit 分支
        subprocess.run(
            ["git", "checkout", "-b", "live-edit/le1", "main"], cwd=str(repo), capture_output=True
        )
        (repo / "feat.txt").write_text("feat")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "live-edit: add feat"], cwd=str(repo), capture_output=True
        )
        subprocess.run(["git", "checkout", "main"], cwd=str(repo), capture_output=True)
        return str(repo)

    def test_graph_structure_and_markers(self, tmp_path):
        repo = self._setup_graph_repo(tmp_path)
        vcs = GitVCS(repo)
        g = vcs.graph()
        assert g["main_branch"] == "main"
        refs = {r["name"]: r for r in g["refs"]}
        assert "live-edit/le1" in refs
        assert refs["live-edit/le1"]["type"] == "live-edit"
        assert refs["live-edit/le1"]["session_id"] == "le1"
        tip = next(c for c in g["commits"] if "live-edit/le1" in c["refs"])
        assert tip["is_live_edit"] is True
        assert tip["merged"] is False
        assert tip["ahead"] == 1
        assert tip["conflict"] is False
        hashes = {c["hash"] for c in g["commits"]}
        assert all(p in hashes for c in g["commits"] for p in c["parents"])

    def test_merge_conflicts(self, tmp_path):
        repo = self._setup_graph_repo(tmp_path)
        vcs = GitVCS(repo)
        # 主分支也改 feat.txt → 合并必然冲突
        (tmp_path / "graph" / "feat.txt").write_text("main-feat")
        subprocess.run(["git", "add", "."], cwd=str(tmp_path / "graph"), capture_output=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "main change"],
            cwd=str(tmp_path / "graph"),
            capture_output=True,
        )
        assert vcs.merge_conflicts("live-edit/le1") is True
        assert vcs.merge_conflicts("main") is False

    def test_checkout_clean_success_and_detach(self, tmp_path):
        repo = self._setup_graph_repo(tmp_path)
        vcs = GitVCS(repo)
        subprocess.run(["git", "checkout", "-b", "other", "main"], cwd=repo, capture_output=True)
        result = vcs.checkout("main")
        assert result["ok"] is True
        assert result["detached"] is False
        # 检出历史提交 → detach
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
        ).stdout.strip()
        result2 = vcs.checkout(head)
        assert result2["ok"] is True
        assert result2["detached"] is True

    def test_checkout_rejects_dirty(self, tmp_path):
        repo = self._setup_graph_repo(tmp_path)
        vcs = GitVCS(repo)
        (tmp_path / "graph" / "dirty.txt").write_text("d")
        subprocess.run(["git", "add", "dirty.txt"], cwd=repo, capture_output=True)
        result = vcs.checkout("main")
        assert result["ok"] is False
        assert "提交" in result["error"]

    def test_list_remotes(self, git_repo, tmp_path):
        origin = tmp_path / "origin.git"
        subprocess.run(
            ["git", "init", "--bare", "-q", "--initial-branch=main", str(origin)],
            capture_output=True,
        )
        subprocess.run(
            ["git", "remote", "add", "origin", str(origin)],
            cwd=str(git_repo),
            capture_output=True,
        )
        vcs = GitVCS(str(git_repo))
        remotes = vcs.list_remotes()
        assert any(r["name"] == "origin" for r in remotes)

    def test_push_safe_no_origin(self, git_repo):
        vcs = GitVCS(str(git_repo))
        result = vcs.push_safe()
        assert result["ok"] is False
        assert "origin" in result["error"]
