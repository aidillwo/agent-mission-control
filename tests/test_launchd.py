"""Tests for the launchd LaunchAgent wiring. Run: .venv/bin/pytest -q

Pure/plist-level only — never calls launchctl or touches ~/Library."""
import plistlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import install_hooks as ih


def test_rendered_plist_has_no_placeholders():
    xml = ih.render_launchd_plist(
        "/repo/.venv/bin/python3", "/repo/app.py", "/repo")
    assert "/ABSOLUTE/PATH" not in xml
    assert "/usr/bin/python3" not in xml  # must be the venv interpreter
    assert "/repo/.venv/bin/python3" in xml
    assert "/repo/app.py" in xml


def test_rendered_plist_parses_and_has_keepalive():
    xml = ih.render_launchd_plist(
        "/repo/.venv/bin/python3", "/repo/app.py", "/repo")
    parsed = plistlib.loads(xml.encode())
    assert parsed["Label"] == ih.LAUNCHD_LABEL == "com.aidill.amc"
    assert parsed["RunAtLoad"] is True
    assert parsed["KeepAlive"] is True
    assert parsed["ProgramArguments"] == ["/repo/.venv/bin/python3", "/repo/app.py"]
    assert parsed["WorkingDirectory"] == "/repo"


def test_venv_python_errors_without_venv(monkeypatch, tmp_path):
    """No .venv ⇒ refuse to write a crash-looping plist."""
    monkeypatch.setattr(ih, "HERE", tmp_path)
    with pytest.raises(FileNotFoundError):
        ih.venv_python()


def test_venv_python_found_when_present(monkeypatch, tmp_path):
    py = tmp_path / ".venv" / "bin" / "python3"
    py.parent.mkdir(parents=True)
    py.write_text("")
    monkeypatch.setattr(ih, "HERE", tmp_path)
    assert ih.venv_python() == py
