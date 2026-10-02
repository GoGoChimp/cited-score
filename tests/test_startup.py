"""Tests for startup.py - the start-with-Windows toggle. It writes a tiny .cmd shim into the
user's Startup folder that launches `rubric-tray`. The Startup dir is patchable so tests never
touch the real one."""
import os
import startup


def _patch_dir(monkeypatch, d):
    monkeypatch.setattr(startup, "_startup_dir", lambda: str(d))


def test_is_enabled_false_when_clean(tmp_path, monkeypatch):
    _patch_dir(monkeypatch, tmp_path)
    assert startup.is_enabled() is False


def test_enable_creates_shim(tmp_path, monkeypatch):
    _patch_dir(monkeypatch, tmp_path)
    assert startup.enable() is True
    assert startup.is_enabled() is True
    files = list(tmp_path.glob("*.cmd"))
    assert files, "a .cmd shim should exist"
    assert "rubric-tray" in files[0].read_text(encoding="utf-8").lower()


def test_disable_removes_shim(tmp_path, monkeypatch):
    _patch_dir(monkeypatch, tmp_path)
    startup.enable()
    assert startup.disable() is True
    assert startup.is_enabled() is False
    assert not list(tmp_path.glob("*.cmd"))


def test_disable_is_idempotent(tmp_path, monkeypatch):
    _patch_dir(monkeypatch, tmp_path)
    assert startup.disable() is True   # nothing to remove, still fine


def test_enable_frozen_shim_launches_the_exe(tmp_path, monkeypatch):
    # In an exe-only install there is no rubric-trayw; the Startup shim must launch the packaged exe itself.
    _patch_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(startup.sys, "frozen", True, raising=False)
    monkeypatch.setattr(startup.sys, "executable", r"C:\Program Files\Rubric\Rubric.exe", raising=False)
    assert startup.enable() is True
    shim = list(tmp_path.glob("*.cmd"))[0].read_text(encoding="utf-8")
    assert "Rubric.exe" in shim
