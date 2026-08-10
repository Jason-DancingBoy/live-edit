"""Tests for live_edit.diff — preview diff computation."""


def test_diff_text_shows_removed_and_added():
    from live_edit.diff import diff_text

    diff = diff_text("a\nb\n", "a\nB\n")
    assert "-b" in diff
    assert "+B" in diff


def test_diff_text_empty_when_identical():
    from live_edit.diff import diff_text

    assert diff_text("same\n", "same\n") == ""


def test_compute_write_diff_edit_file(tmp_path):
    from live_edit.diff import compute_write_diff

    p = tmp_path / "app.py"
    p.write_text("old\n", encoding="utf-8")
    diff = compute_write_diff(
        "edit_file",
        {"path": "app.py", "old_string": "old", "new_string": "new"},
        str(tmp_path),
    )
    assert "-old" in diff
    assert "+new" in diff


def test_compute_write_diff_write_file_new(tmp_path):
    from live_edit.diff import compute_write_diff

    diff = compute_write_diff(
        "write_file", {"path": "new.py", "content": "print(1)\n"}, str(tmp_path)
    )
    assert "+print(1)" in diff


def test_compute_write_diff_write_file_overwrites_existing(tmp_path):
    from live_edit.diff import compute_write_diff

    p = tmp_path / "app.py"
    p.write_text("old\n", encoding="utf-8")
    diff = compute_write_diff("write_file", {"path": "app.py", "content": "new\n"}, str(tmp_path))
    assert "-old" in diff
    assert "+new" in diff


def test_compute_write_diff_non_write_tool_returns_empty(tmp_path):
    from live_edit.diff import compute_write_diff

    assert compute_write_diff("run_shell", {"cmd": "echo hi"}, str(tmp_path)) == ""


def test_compute_write_diff_edit_failure_returns_empty(tmp_path):
    from live_edit.diff import compute_write_diff

    assert (
        compute_write_diff(
            "edit_file",
            {"path": "nope.py", "old_string": "x", "new_string": "y"},
            str(tmp_path),
        )
        == ""
    )


class TestDeleteFileDiff:
    def test_delete_file_returns_full_removal_diff(self, tmp_path):
        from live_edit.diff import compute_write_diff

        p = tmp_path / "gone.py"
        p.write_text("print('bye')\n")
        d = compute_write_diff("delete_file", {"path": "gone.py"}, str(tmp_path))
        assert "-print('bye')" in d
        assert "+print('bye')" not in d
