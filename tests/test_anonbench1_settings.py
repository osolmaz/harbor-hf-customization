"""Check configuration provenance without private inputs or model calls."""

import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts/sync_anonbench1_adapters.py"


@pytest.fixture
def sync_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    spec = importlib.util.spec_from_file_location("adapter_sync", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    destination = tmp_path / "config"
    monkeypatch.setattr(module, "SETTINGS_DIR", destination)
    monkeypatch.setattr(module, "SETTINGS_MANIFEST", destination / "sync-manifest.json")
    monkeypatch.setattr(
        module,
        "SETTINGS_FILES",
        {"pi/pins.json": "runs/current/pi/pins.json"},
    )
    return module


@pytest.fixture
def source(tmp_path: Path) -> Path:
    checkout = tmp_path / "source"
    checkout.mkdir()
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    pins = checkout / "runs/current/pi/pins.json"
    pins.parent.mkdir(parents=True)
    pins.write_text('{"agent": "ShellBenchPi", "env": "SHELLBENCH_RUNTIME"}\n')
    subprocess.run(["git", "-C", str(checkout), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "test fixture",
        ],
        check=True,
    )
    return checkout


def test_sync_ignores_uncommitted_source_changes(
    sync_module: ModuleType,
    source: Path,
) -> None:
    (source / "runs/current/pi/pins.json").write_text('{"unexpected": true}')
    sync_module.sync_settings(source)
    copied = json.loads((sync_module.SETTINGS_DIR / "pi/pins.json").read_text())
    assert copied == {"agent": "Anonbench1Pi", "env": "ANONBENCH1_RUNTIME"}
    assert sync_module.check_settings(source) == []


def test_check_rejects_missing_or_edited_copy(
    sync_module: ModuleType,
    source: Path,
) -> None:
    sync_module.sync_settings(source)
    copied = sync_module.SETTINGS_DIR / "pi/pins.json"
    copied.write_text('{"version": "wrong"}')
    assert sync_module.check_settings()
    copied.unlink()
    assert sync_module.check_settings()


def test_source_check_rejects_self_consistent_replacement(
    sync_module: ModuleType,
    source: Path,
) -> None:
    sync_module.sync_settings(source)
    copied = sync_module.SETTINGS_DIR / "pi/pins.json"
    copied.write_text('{"version": "wrong"}')
    manifest = json.loads(sync_module.SETTINGS_MANIFEST.read_text())
    manifest["files"]["pi/pins.json"] = sync_module.digest(copied.read_bytes())
    sync_module.SETTINGS_MANIFEST.write_text(json.dumps(manifest))
    assert sync_module.check_settings() == []
    assert sync_module.check_settings(source) == [
        "pi/pins.json differs from the recorded source revision"
    ]


def test_check_rejects_incomplete_inventory(
    sync_module: ModuleType,
    source: Path,
) -> None:
    sync_module.sync_settings(source)
    manifest = json.loads(sync_module.SETTINGS_MANIFEST.read_text())
    manifest["files"] = {}
    sync_module.SETTINGS_MANIFEST.write_text(json.dumps(manifest))
    assert "configuration inventory" in sync_module.check_settings()[0]


def test_check_requires_a_full_revision(
    sync_module: ModuleType,
    source: Path,
) -> None:
    sync_module.sync_settings(source)
    manifest = json.loads(sync_module.SETTINGS_MANIFEST.read_text())
    manifest["source_revision"] = "main"
    sync_module.SETTINGS_MANIFEST.write_text(json.dumps(manifest))
    assert sync_module.check_settings() == [
        "configuration source revision must be a full commit"
    ]
