import sys
from pathlib import Path

import pytest

from lamar.tools import ToolBox, _glob_match


@pytest.fixture
def box(tmp_path: Path) -> ToolBox:
    return ToolBox(tmp_path)


def test_write_read_edit_roundtrip(box: ToolBox, tmp_path: Path):
    out = box.execute("write_file", {"path": "src/app.py", "content": "print('hi')\nx = 1\n"})
    assert not out.is_error, out.content
    assert (tmp_path / "src/app.py").read_text() == "print('hi')\nx = 1\n"

    out = box.execute("read_file", {"path": "src/app.py"})
    assert "     1\tprint('hi')" in out.content
    assert "     2\tx = 1" in out.content

    out = box.execute("edit_file", {"path": "src/app.py", "old_string": "x = 1", "new_string": "x = 2"})
    assert not out.is_error, out.content
    assert (tmp_path / "src/app.py").read_text() == "print('hi')\nx = 2\n"


def test_edit_requires_unique_match(box: ToolBox, tmp_path: Path):
    (tmp_path / "a.txt").write_text("foo\nfoo\n")
    out = box.execute("edit_file", {"path": "a.txt", "old_string": "foo", "new_string": "bar"})
    assert out.is_error and "2-mal" in out.content
    out = box.execute("edit_file", {"path": "a.txt", "old_string": "foo", "new_string": "bar", "replace_all": True})
    assert not out.is_error
    assert (tmp_path / "a.txt").read_text() == "bar\nbar\n"


def test_edit_handles_windows_line_endings(box: ToolBox, tmp_path: Path):
    (tmp_path / "w.txt").write_bytes(b"a\r\nb\r\nc\r\n")
    out = box.execute("edit_file", {"path": "w.txt", "old_string": "a\nb", "new_string": "a\nB"})
    assert not out.is_error, out.content
    assert (tmp_path / "w.txt").read_bytes() == b"a\r\nB\r\nc\r\n"


def test_edit_missing_text(box: ToolBox, tmp_path: Path):
    (tmp_path / "a.txt").write_text("hello")
    out = box.execute("edit_file", {"path": "a.txt", "old_string": "nope", "new_string": "x"})
    assert out.is_error and "nicht gefunden" in out.content


@pytest.mark.parametrize("bad", ["../outside.txt", "/etc/passwd", "sub/../../x"])
def test_paths_outside_workspace_are_rejected(box: ToolBox, bad: str):
    out = box.execute("write_file", {"path": bad, "content": "x"})
    assert out.is_error and "außerhalb" in out.content


def test_symlink_escape_is_rejected(box: ToolBox, tmp_path: Path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (tmp_path / "link").symlink_to(outside)
    out = box.execute("write_file", {"path": "link/evil.txt", "content": "x"})
    assert out.is_error
    assert not (outside / "evil.txt").exists()


def test_invalid_input_is_reported_not_executed(box: ToolBox, tmp_path: Path):
    out = box.execute("write_file", {"path": "a.txt"})  # content fehlt (z. B. abgeschnitten)
    assert out.is_error and "INVALID_JSON" in out.content
    assert not (tmp_path / "a.txt").exists()
    out = box.execute("write_file", {"path": "a.txt", "content": "x", "extra": 1})
    assert out.is_error


def test_unknown_tool(box: ToolBox):
    assert box.execute("nope", {}).is_error


def test_read_image_returns_image_block(box: ToolBox, tmp_path: Path):
    (tmp_path / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 20)
    out = box.execute("read_file", {"path": "pic.png"})
    assert not out.is_error
    assert out.content[0]["type"] == "image"
    assert out.content[0]["source"]["media_type"] == "image/png"


def test_read_binary_and_ranges(box: ToolBox, tmp_path: Path):
    (tmp_path / "bin.dat").write_bytes(b"\x00\x01\x02")
    assert box.execute("read_file", {"path": "bin.dat"}).is_error
    (tmp_path / "long.txt").write_text("\n".join(f"line {i}" for i in range(1, 101)))
    out = box.execute("read_file", {"path": "long.txt", "offset": 10, "limit": 5})
    assert "    10\tline 10" in out.content and "    14\tline 14" in out.content
    assert "line 15" not in out.content.split("[")[0]
    assert "offset=15" in out.content


def test_list_find_search(box: ToolBox, tmp_path: Path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg/mod.py").write_text("def hello():\n    return 42\n")
    (tmp_path / "pkg/readme.md").write_text("Hello world\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules/x.py").write_text("def hello(): pass\n")

    listing = box.execute("list_directory", {}).content
    assert "pkg/" in listing and "mod.py" in listing and "node_modules/ (übersprungen)" in listing

    found = box.execute("find_files", {"pattern": "**/*.py"}).content
    assert "pkg/mod.py" in found and "node_modules" not in found

    hits = box.execute("search_code", {"pattern": r"def hello"}).content
    assert "pkg/mod.py:1:def hello():" in hits and "node_modules" not in hits

    hits = box.execute("search_code", {"pattern": "hello", "case_insensitive": True, "glob": "*.md"}).content
    assert "readme.md" in hits and "mod.py" not in hits


def test_glob_match():
    assert _glob_match("a/b/c.py", "**/*.py")
    assert _glob_match("c.py", "**/*.py")
    assert _glob_match("a/b/c.py", "*.py")  # ohne Ordner: gilt für Dateinamen
    assert not _glob_match("a/b/c.py", "a/*.py")
    assert _glob_match("a/c.py", "a/*.py")
    assert _glob_match("src/x/test_a.ts", "src/**/test_*.ts")


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX-Shell")
def test_run_command_output_exit_code_and_timeout(box: ToolBox):
    out = box.execute("run_command", {"command": "echo hallo && exit 3"})
    assert "hallo" in out.content and "Exit-Code: 3" in out.content

    out = box.execute("run_command", {"command": "sleep 5", "timeout": 1})
    assert out.is_error and "Zeitlimit" in out.content


def test_approval_can_reject(tmp_path: Path):
    calls = []

    def deny(kind, title, detail):
        calls.append((kind, title))
        return False

    box = ToolBox(tmp_path, approver=deny)
    out = box.execute("run_command", {"command": "echo no"})
    assert out.is_error and "abgelehnt" in out.content
    out = box.execute("write_file", {"path": "x.txt", "content": "y"})
    assert out.is_error and not (tmp_path / "x.txt").exists()
    assert [c[0] for c in calls] == ["command", "file"]
