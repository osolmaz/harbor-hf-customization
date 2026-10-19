"""Sync the copied adapter modules 1-1 with the reviewed private adapter source.

The three adapter modules in `packages/anonbench1-adapters/src/anonbench1_adapters/`
are copies of the reviewed adapter source folder. This script is the only way
they change. It copies each source file byte for byte, applies one recorded
rename (the source carries its internal project name; this repository publishes
the hidden one), and records the result's SHA256 in `sync-manifest.json`
together with the source revision. CI runs `--check`, which verifies the
committed files against the manifest, so a hand edit to any copy fails the
build even though the private source is not available there.

Usage:

    uv run --project packages/anonbench1-adapters \
        python scripts/sync_anonbench1_adapters.py \
        --source-root <reviewed adapter source folder> [--check]
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
    ("pi/adapter/campaign_pi.py", "anonbench1_pi.py"),
    ("openclaw/adapter/campaign_openclaw.py", "anonbench1_openclaw.py"),
    ("hermes/adapter/campaign_hermes.py", "anonbench1_hermes.py"),
    ("campaign_pins.py", "anonbench1_pins.py"),
    ("pi/adapter/campaign_evidence.mjs", "anonbench1_evidence.mjs"),
    ("pi/adapter/offline_pi.py", "anonbench1_offline_pi.py"),
    ("pi/adapter/offline_session.mjs", "anonbench1_offline_session.mjs"),
    ("pi/adapter/offline_task_helper.mjs", "anonbench1_offline_task_helper.mjs"),
    ("pi/adapter/offline_tools.mjs", "anonbench1_offline_tools.mjs"),
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
    ("campaign_evidence.mjs", "anonbench1_evidence.mjs"),
)

# These are native Harbor configuration files, not benchmark task data. The
# current source selects a separate dated runtime for each harness.
SETTINGS_DIR = REPO_ROOT / PACKAGE / "config"
SETTINGS_MANIFEST = SETTINGS_DIR / "sync-manifest.json"
RUNTIME_RUNS = {
    "pi": "2026-10-16-shellbench-pi-runtime",
    "openclaw": "2026-10-15-shellbench-openclaw-runtime",
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
    text = data.decode("utf-8")
    # The source adapters import the shared pins module by its bare source name;
    # the published package keeps it inside anonbench1_adapters.
    text = text.replace(
        "from campaign_pins import", "from anonbench1_adapters.anonbench1_pins import"
    )
    text = text.replace(
        "from offline_pi import",
        "from anonbench1_adapters.anonbench1_offline_pi import",
    )
    for asset in (
        "offline_session.mjs",
        "offline_task_helper.mjs",
        "offline_tools.mjs",
    ):
        text = text.replace(asset, f"anonbench1_{asset}")
    # The source expects a Harbor build whose OpenClaw commands select Node 24;
    # the published launch contract pins public Harbor 3c823808, whose commands
    # select Node 22. The runtime guard replaces that prefix either way.
    text = text.replace("nvm use 24 >/dev/null && ", "nvm use 22 && ")
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


def sync(pairs: list[tuple[Path, Path]], source_root: Path) -> list[str]:
    changed = []
    files: dict[str, str] = {}
    for source, target in pairs:
        if not source.is_file():
            raise FileNotFoundError(f"missing source file: {source}")
        new = transform(source.read_bytes())
        before = target.read_bytes()
        target.write_bytes(new)
        if new != before:
            changed.append(f"{target.name} synced from the source")
        files[target.name] = digest(new)
    manifest = {"source_revision": source_revision(source_root), "files": files}
    MANIFEST.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return changed


def check(pairs: list[tuple[Path | None, Path]]) -> list[str]:
    recorded = json.loads(MANIFEST.read_text())["files"]
    drifted = []
    for _, target in pairs:
        expected = recorded.get(target.name)
        if expected is None:
            drifted.append(f"{target.name} is not in sync-manifest.json")
        elif digest(target.read_bytes()) != expected:
            drifted.append(f"{target.name} does not match sync-manifest.json")
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
    pairs: list[tuple[Path | None, Path]] = [
        (args.source_root / relative if args.source_root else None, TARGET_DIR / target)
        for relative, target in COPIED_FILES
    ]

    if args.check:
        drifted = check(pairs)
        if drifted:
            for line in drifted:
                print(f"drift: {line}", file=sys.stderr)
            print(
                "run scripts/sync_anonbench1_adapters.py with the source root "
                "and commit the result",
                file=sys.stderr,
            )
            return 1
        print("in sync")
        return 0
    resolved: list[tuple[Path, Path]] = []
    for source, target in pairs:
        if source is None:
            raise RuntimeError("sync needs every source file path")
        resolved.append((source, target))
    for line in sync(resolved, args.source_root):
        print(line)
    print("in sync")
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
