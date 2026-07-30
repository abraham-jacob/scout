"""Tests for agent/claude.py — the Claude-CLI-calling layer."""

import pytest
from unittest.mock import MagicMock

import agent.claude as claude


class TestClaudeExecutable:
    """Test cross-platform resolution of the claude CLI."""

    def test_resolves_via_which(self, monkeypatch):
        """Returns the absolute path shutil.which finds."""
        claude.claude_executable.cache_clear()
        monkeypatch.setattr(claude.shutil, "which", lambda name: "/usr/local/bin/claude")
        try:
            assert claude.claude_executable() == "/usr/local/bin/claude"
        finally:
            claude.claude_executable.cache_clear()

    def test_raises_when_missing(self, monkeypatch):
        """Raises FileNotFoundError when claude isn't on PATH."""
        claude.claude_executable.cache_clear()
        monkeypatch.setattr(claude.shutil, "which", lambda name: None)
        try:
            with pytest.raises(FileNotFoundError, match="claude"):
                claude.claude_executable()
        finally:
            claude.claude_executable.cache_clear()


class TestKillProcessTree:
    """Test cross-platform subprocess-tree kill."""

    def test_posix_kills_process_group(self, monkeypatch):
        """On POSIX, SIGKILL is sent to the child's process group."""
        killed = {}
        monkeypatch.setattr(claude.os, "name", "posix")
        monkeypatch.setattr(claude.os, "getpgid", lambda pid: 4242)
        monkeypatch.setattr(claude.os, "killpg",
                            lambda pgid, sig: killed.update(pgid=pgid, sig=sig))
        proc = MagicMock()
        proc.pid = 999

        claude._kill_process_tree(proc)

        assert killed == {"pgid": 4242, "sig": claude.signal.SIGKILL}

    def test_posix_swallows_already_dead(self, monkeypatch):
        """A process that already exited doesn't raise out of the kill."""
        monkeypatch.setattr(claude.os, "name", "posix")

        def _gone(pid):
            raise ProcessLookupError()

        monkeypatch.setattr(claude.os, "getpgid", _gone)
        proc = MagicMock()
        proc.pid = 1

        claude._kill_process_tree(proc)  # must not raise

    def test_windows_uses_taskkill(self, monkeypatch):
        """On Windows, taskkill /T is invoked to walk the tree."""
        calls = {}
        monkeypatch.setattr(claude.os, "name", "nt")
        monkeypatch.setattr(claude.subprocess, "run",
                            lambda cmd, **kw: calls.update(cmd=cmd))
        proc = MagicMock()
        proc.pid = 4321

        claude._kill_process_tree(proc)

        assert calls["cmd"] == ["taskkill", "/F", "/T", "/PID", "4321"]
