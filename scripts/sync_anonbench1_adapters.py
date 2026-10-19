"""Copy selected native adapters and settings from a committed source.

Only allowlisted non-task files enter the package. The manifest records source
and published hashes. Check copied bytes in CI, or supply the source checkout to
verify both sides against the recorded commit. OpenClaw requires a separate
admitted runtime bundle and is not an executable entry point in this release.
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "packages/anonbench1-adapters"
TARGET_DIR = REPO_ROOT / PACKAGE / "src/anonbench1_adapters"
MANIFEST = REPO_ROOT / PACKAGE / "sync-manifest.json"

COPIED_FILES = (
    (
        "runs/2026-10-16-shellbench-pi-runtime/pi/adapter/campaign_pi.py",
        "anonbench1_pi.py",
    ),
    ("runs/2026-10-16-shellbench-pi-runtime/campaign_pins.py", "anonbench1_pins.py"),
    (
        "runs/2026-10-16-shellbench-pi-runtime/pi/adapter/campaign_evidence.mjs",
        "campaign_evidence.mjs",
    ),
    ("runs/2026-10-16-shellbench-pi-runtime/pi/adapter/offline_pi.py", "offline_pi.py"),
    (
        "runs/2026-10-16-shellbench-pi-runtime/pi/adapter/offline_session.mjs",
        "offline_session.mjs",
    ),
    (
        "runs/2026-10-16-shellbench-pi-runtime/pi/adapter/offline_task_helper.mjs",
        "offline_task_helper.mjs",
    ),
    (
        "runs/2026-10-16-shellbench-pi-runtime/pi/adapter/offline_tools.mjs",
        "offline_tools.mjs",
    ),
    (
        "runs/2026-10-17-shellbench-standalone-observations/codex/adapter/observed_codex.py",
        "observed_codex.py",
    ),
    (
        "runs/2026-10-17-shellbench-standalone-observations/codex/adapter/retained_codex.py",
        "retained_codex.py",
    ),
    (
        "runs/2026-10-17-shellbench-standalone-observations/codex/adapter/retained_codex_probe.py",
        "retained_codex_probe.py",
    ),
    (
        "runs/2026-10-17-shellbench-standalone-observations/claude-code/adapter/observed_claude_code.py",
        "observed_claude_code.py",
    ),
    (
        "runs/2026-10-17-shellbench-standalone-observations/standalone_observations.py",
        "standalone_observations.py",
    ),
    (
        "runs/2026-10-17-shellbench-resource-observations/resource_telemetry.py",
        "resource_telemetry.py",
    ),
)

# The recorded rename. The source is reviewed and pinned; this repository
# publishes the hidden artifact names. Order matters only for readability.
RENAMES = (
    ("ShellBenchHermes", "Anonbench1Hermes"),
    ("ShellBenchOpenClaw", "Anonbench1OpenClaw"),
    ("ShellBenchPi", "Anonbench1Pi"),
    ("ShellBenchHermesOptions", "Anonbench1HermesOptions"),
    ("ShellBenchOpenClawOptions", "Anonbench1OpenClawOptions"),
    ("ShellBenchPiOptions", "Anonbench1PiOptions"),
    ("ShellBench's", "Anonbench1's"),
    ("ShellBench", "Anonbench1"),
    ("shellbench", "anonbench1"),
)

# These are native Harbor configuration files, not benchmark task data. The
# current source selects a separate dated runtime for each harness.
SETTINGS_DIR = REPO_ROOT / PACKAGE / "config"
SETTINGS_MANIFEST = SETTINGS_DIR / "sync-manifest.json"
RUNTIME_RUNS = {
    "pi": "2026-10-16-shellbench-pi-runtime",
    "openclaw": "2026-10-19-shellbench-openclaw-runtime",
    "hermes": "2026-10-13-shellbench-hermes-runtime",
    "codex": "2026-10-17-shellbench-standalone-observations",
    "claude-code": "2026-10-17-shellbench-standalone-observations",
}
SETTINGS_FILES = {
    **{
        f"{agent}/harbor.json": f"runs/{run}/harbor.json"
        for agent, run in RUNTIME_RUNS.items()
    },
    **{
        f"{agent}/{name}": f"runs/{run}/{agent}/{name}"
        for agent, run in RUNTIME_RUNS.items()
        for name in ("pins.json", "job-template.yaml")
    },
}


def settings_bytes(data: bytes) -> bytes:
    """Keep source settings intact apart from the published benchmark name."""
    text = data.decode("utf-8").replace("SHELLBENCH", "ANONBENCH1")
    for old, new in RENAMES:
        text = text.replace(old, new)
    return text.encode("utf-8")


def committed_settings(source_root: Path, revision: str) -> dict[str, bytes]:
    """Read only the allowlisted config files from one committed revision."""
    files = {}
    for target, source in SETTINGS_FILES.items():
        files[target] = subprocess.run(
            ["git", "-C", str(source_root), "show", f"{revision}:{source}"],
            capture_output=True,
            check=True,
        ).stdout
    return files


def sync_settings(source_root: Path) -> None:
    """Save the native templates and both their source and published hashes."""
    revision = source_revision(source_root)
    originals = committed_settings(source_root, revision)
    files = {name: settings_bytes(data) for name, data in originals.items()}
    for name, data in files.items():
        target = SETTINGS_DIR / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    manifest = {
        "source_revision": revision,
        "source_files": {name: digest(data) for name, data in originals.items()},
        "files": {name: digest(data) for name, data in files.items()},
    }
    SETTINGS_MANIFEST.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def check_settings(source_root: Path | None = None) -> list[str]:
    """Check every copied setting, optionally against the recorded source too."""
    manifest = json.loads(SETTINGS_MANIFEST.read_text())
    if not re.fullmatch(r"[0-9a-f]{40}", manifest["source_revision"]):
        return ["configuration source revision must be a full commit"]
    recorded = manifest["files"]
    drifted = []
    if set(recorded) != set(SETTINGS_FILES) or set(manifest["source_files"]) != set(
        SETTINGS_FILES
    ):
        drifted.append("configuration inventory differs from the selected source files")
    for name in SETTINGS_FILES:
        target = SETTINGS_DIR / name
        if not target.is_file() or digest(target.read_bytes()) != recorded.get(name):
            drifted.append(f"{name} does not match the configuration sync manifest")
    if source_root is not None:
        originals = committed_settings(source_root, manifest["source_revision"])
        for name, data in originals.items():
            if digest(data) != manifest["source_files"].get(name) or digest(
                settings_bytes(data)
            ) != recorded.get(name):
                drifted.append(f"{name} differs from the recorded source revision")
    return drifted


def transform(data: bytes) -> bytes:
    """Apply only the recorded package imports and published name changes."""
    text = data.decode("utf-8")
    modules = {
        "campaign_pins": "anonbench1_pins",
        "offline_pi": "offline_pi",
        "resource_telemetry": "resource_telemetry",
        "standalone_observations": "standalone_observations",
        "retained_codex": "retained_codex",
        "retained_codex_probe": "retained_codex_probe",
    }
    for source, target in modules.items():
        text = text.replace(
            f"from {source} import", f"from anonbench1_adapters.{target} import"
        )
    for old, new in RENAMES:
        text = text.replace(old, new)
    return text.encode("utf-8")


def source_revision(source_root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(source_root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"cannot read the source revision at {source_root}")
    return result.stdout.strip()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def adapter_sources(source_root: Path, revision: str) -> dict[str, bytes]:
    return {
        target: subprocess.run(
            ["git", "-C", str(source_root), "show", f"{revision}:{source}"],
            capture_output=True,
            check=True,
        ).stdout
        for source, target in COPIED_FILES
    }


def sync(source_root: Path) -> None:
    revision = source_revision(source_root)
    originals = adapter_sources(source_root, revision)
    previous = json.loads(MANIFEST.read_text())["files"]
    files = {name: transform(data) for name, data in originals.items()}
    for name in previous.keys() - files.keys():
        (TARGET_DIR / name).unlink()
    for name, data in files.items():
        (TARGET_DIR / name).write_bytes(data)
    manifest = {
        "source_revision": revision,
        "source_files": {name: digest(data) for name, data in originals.items()},
        "files": {name: digest(data) for name, data in files.items()},
    }
    MANIFEST.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def check(source_root: Path | None = None) -> list[str]:
    manifest = json.loads(MANIFEST.read_text())
    recorded = manifest["files"]
    expected = {target for _, target in COPIED_FILES}
    drifted = []
    if not re.fullmatch(r"[0-9a-f]{40}", manifest["source_revision"]):
        drifted.append("adapter source revision must be a full commit")
    if set(recorded) != expected or set(manifest["source_files"]) != expected:
        drifted.append("adapter inventory differs from the selected source files")
    for name in expected:
        path = TARGET_DIR / name
        if not path.is_file() or digest(path.read_bytes()) != recorded.get(name):
            drifted.append(f"{name} does not match sync-manifest.json")
    if source_root is not None:
        for name, data in adapter_sources(
            source_root, manifest["source_revision"]
        ).items():
            if digest(data) != manifest["source_files"].get(name) or digest(
                transform(data)
            ) != recorded.get(name):
                drifted.append(f"{name} differs from the recorded source revision")
    return drifted


def settings_command(args: argparse.Namespace) -> int:
    if args.check:
        drifted = check_settings(args.source_root)
        for line in drifted:
            print(f"drift: {line}", file=sys.stderr)
        return int(bool(drifted))
    sync_settings(args.source_root)
    print("runtime settings in sync; worker qualification remains required")
    return 0


def adapter_command(args: argparse.Namespace) -> int:
    if args.check:
        drifted = check(args.source_root)
        for line in drifted:
            print(f"drift: {line}", file=sys.stderr)
        return int(bool(drifted))
    sync(args.source_root)
    print("adapters in sync; OpenClaw remains held for runtime admission")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-root",
        type=Path,
        help="The reviewed source; required for sync, optional for --check.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify committed files against the sync manifest; never writes.",
    )
    parser.add_argument(
        "--settings",
        action="store_true",
        help="Sync runtime settings; --source-root is the configuration checkout.",
    )
    args = parser.parse_args()
    if not args.check and not args.source_root:
        parser.error("--source-root is required for sync")
    return settings_command(args) if args.settings else adapter_command(args)


if __name__ == "__main__":
    sys.exit(main())
