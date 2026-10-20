"""Exact upstream source artifacts for the existing campaign installer owner.

Prepare a new carrier from the selected upstream archive, preserving its bytes.
The full exported tree, native lock and runtime executables remain independently
bound; source preparation does not qualify the offline integration.
"""

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from anonbench1_adapters.anonbench1_pins import HERMES_SHA, HERMES_TAG, HERMES_VERSION, NODE_VERSION

SOURCE_REPOSITORY = "https://github.com/NousResearch/hermes-agent"
SOURCE_TREE = "5849eacde63aaea608ca418821cc84771fce3bec"
SOURCE_ARCHIVE_SHA256 = "38d01fd8bdb1eb8a3866afa613c1b1631d2ca18f9c050436c07c4c51bd1d6c9d"
UV_LOCK_SHA256 = "5b3798f326209475abca8ef7cbf7c9406f12e687c28c0b540dfe597466f48590"


def file_sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def require_digest(value: str, length: int) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{" + str(length) + "}", value) is None:
        raise ValueError("Source identity requires a full lowercase digest")


@dataclass(frozen=True)
class UpstreamHermesSource:
    format: int
    repo_url: str
    source_ref: str
    source_commit: str
    package_version: str
    source_tree: str
    archive_sha256: str
    uv_lock_sha256: str

    def __post_init__(self):
        if (
            type(self.format) is not int or self.format != 2
            or self.repo_url != SOURCE_REPOSITORY
            or self.source_ref != "refs/tags/" + HERMES_TAG
            or self.source_commit != HERMES_SHA
            or self.package_version != HERMES_VERSION
            or self.source_tree != SOURCE_TREE
            or self.archive_sha256 != SOURCE_ARCHIVE_SHA256
            or self.uv_lock_sha256 != UV_LOCK_SHA256
        ):
            raise ValueError("Hermes source must match the selected unmodified upstream archive")

    @classmethod
    def load(cls, path: Path, expected_sha256: str):
        require_digest(expected_sha256, 64)
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise ValueError("Private source identity differs from its reviewed digest")
        return cls(**json.loads(raw))

    def encoded(self) -> bytes:
        return (json.dumps(asdict(self), sort_keys=True, indent=2) + "\n").encode()


def _object_hash(kind: bytes, content: bytes) -> bytes:
    return hashlib.sha1(kind + b" " + str(len(content)).encode() + b"\0" + content).digest()


def source_tree(path: Path) -> str:
    """Hash the entire exported source with Git's SHA-1 tree/blob encoding.

    No ignore rules or exclusions: extra Python, bytecode, dependencies or Git
    metadata in the source directory invalidate its identity. The venv and
    mutable state belong beside this immutable source export.
    """
    if not path.is_dir() or path.is_symlink():
        raise ValueError("Source must be an exported directory")

    def tree(directory):
        entries = []
        with os.scandir(directory) as children:
            for child in children:
                name = os.fsencode(child.name)
                mode = child.stat(follow_symlinks=False).st_mode
                if stat.S_ISDIR(mode):
                    digest = tree(child.path)
                    git_mode, sort_name = b"40000", name + b"/"
                elif stat.S_ISLNK(mode):
                    digest = _object_hash(b"blob", os.readlink(os.fsencode(child.path)))
                    git_mode, sort_name = b"120000", name
                elif stat.S_ISREG(mode):
                    size = child.stat(follow_symlinks=False).st_size
                    hasher = hashlib.sha1(b"blob " + str(size).encode() + b"\0")
                    with open(child.path, "rb") as source:
                        while chunk := source.read(1024 * 1024):
                            hasher.update(chunk)
                    digest = hasher.digest()
                    git_mode = b"100755" if mode & stat.S_IXUSR else b"100644"
                    sort_name = name
                else:
                    raise ValueError("Source export contains a non-source filesystem object")
                entries.append((sort_name, git_mode + b" " + name + b"\0" + digest))
        return _object_hash(b"tree", b"".join(entry for _, entry in sorted(entries)))

    return tree(path).hex()


def verify_source(path: Path, identity: UpstreamHermesSource) -> None:
    if source_tree(path) != identity.source_tree:
        raise ValueError("Hermes source bytes/modes differ from the selected upstream tree")
    if file_sha256(path / "uv.lock") != identity.uv_lock_sha256:
        raise ValueError("Hermes native lock differs from the selected upstream lock")


def verify_archive(path: Path, identity: UpstreamHermesSource) -> None:
    if file_sha256(path) != identity.archive_sha256:
        raise ValueError("Private source archive differs from the admitted identity")


@dataclass(frozen=True)
class TaskHermesRuntime:
    """Reviewed task-context paths and executable identity; never host PATH inputs."""

    python: str
    python_sha256: str
    python_version: str
    source: str
    source_identity: str
    source_identity_sha256: str
    node: str
    node_sha256: str

    def __post_init__(self):
        for value in (self.python, self.source, self.source_identity, self.node):
            if not isinstance(value, str) or not value.startswith("/") or "\0" in value:
                raise ValueError("Task runtime paths must be explicit absolute task paths")
        require_digest(self.python_sha256, 64)
        require_digest(self.source_identity_sha256, 64)
        require_digest(self.node_sha256, 64)
        if re.fullmatch(r"3\.(11|12|13)\.[0-9]+", self.python_version) is None:
            raise ValueError("Task Hermes requires an admitted exact Python >=3.11,<3.14")


@dataclass(frozen=True)
class HostHermesRuntime(TaskHermesRuntime):
    """The same identity fields, explicitly interpreted in the isolated host namespace."""


def task_runtime_receipt(source: Path, identity: UpstreamHermesSource, node: str) -> dict:
    """Run inside the selected task interpreter after full source admission.

    Do not add the export to sys.path here: the admitted venv itself must resolve
    Hermes to that export before the native task workers start.
    """
    if not (3, 11) <= sys.version_info[:2] < (3, 14) or sys.prefix == sys.base_prefix:
        raise ValueError("Task document extraction requires the admitted Hermes venv")
    spec = importlib.util.find_spec("hermes_cli")
    if spec is None or spec.origin is None or not Path(spec.origin).resolve().is_relative_to(source.resolve()):
        raise ValueError("Task interpreter does not resolve the admitted Hermes source")
    import hermes_cli

    if hermes_cli.__version__ != identity.package_version:
        raise ValueError("Task Hermes package version differs from its source identity")
    if importlib.metadata.version("firecrawl-anydoc") != "0.2.4" or importlib.util.find_spec("anydoc") is None:
        raise ValueError("Task document extraction requires installed firecrawl-anydoc 0.2.4")
    selected = shutil.which("node")
    if selected is None or Path(selected).resolve() != Path(node).resolve():
        raise ValueError("Native Node lookup does not select the admitted executable")
    version = subprocess.run([node, "--version"], check=True, capture_output=True, text=True, timeout=10)
    if version.stdout.strip() != "v" + NODE_VERSION:
        raise ValueError("Native Node version differs from the campaign pin")
    return {
        "python_sha256": file_sha256(Path(sys.executable)),
        "python_version": ".".join(map(str, sys.version_info[:3])),
        "anydoc_version": "0.2.4",
        "node_sha256": file_sha256(Path(node)),
        "node_version": NODE_VERSION,
        "source_identity": asdict(identity),
    }


def prepare_artifact(archive: Path, *, destination: Path, max_bytes: int) -> UpstreamHermesSource:
    """Called only after source/route review with an explicit private destination.

    This copies and verifies source artifacts, without installing dependencies,
    changing a source checkout or granting runtime qualification.
    """
    if destination.exists() or destination.is_symlink() or max_bytes <= 0:
        raise ValueError("A fresh private artifact destination and byte budget are required")
    identity = UpstreamHermesSource(
        format=2, repo_url=SOURCE_REPOSITORY, source_ref="refs/tags/" + HERMES_TAG,
        source_commit=HERMES_SHA, package_version=HERMES_VERSION, source_tree=SOURCE_TREE,
        archive_sha256=SOURCE_ARCHIVE_SHA256, uv_lock_sha256=UV_LOCK_SHA256,
    )
    if archive.stat().st_size + len(identity.encoded()) > max_bytes:
        raise ValueError("Source artifact budget exceeded")
    verify_archive(archive, identity)
    with tempfile.TemporaryDirectory(prefix="hermes-source-") as temporary:
        scratch = Path(temporary)
        bundle = scratch / "bundle"
        bundle.mkdir(mode=0o700)
        frozen_archive = bundle / "source.tar"
        shutil.copyfile(archive, frozen_archive)
        frozen_archive.chmod(0o600)
        verify_archive(frozen_archive, identity)
        exported = scratch / "source"
        exported.mkdir()
        with tarfile.open(frozen_archive, "r:") as source:
            source.extractall(exported, filter="data")
        verify_source(exported, identity)
        identity_path = bundle / "source-identity.json"
        identity_path.write_bytes(identity.encoded())
        identity_path.chmod(0o600)
        if sum(p.stat().st_size for p in bundle.iterdir()) > max_bytes:
            raise ValueError("Source artifact budget exceeded")
        # Exclusive destination admission; never replace a prior carrier.
        destination.mkdir(mode=0o700)
        retained_archive = destination / frozen_archive.name
        with retained_archive.open("xb") as output, frozen_archive.open("rb") as source:
            shutil.copyfileobj(source, output)
        retained_archive.chmod(0o600)
        verify_archive(retained_archive, identity)
        with (destination / identity_path.name).open("xb") as output:
            output.write(identity.encoded())
        (destination / identity_path.name).chmod(0o600)
        return identity


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--archive", type=Path, required=True)
    prepare.add_argument("--destination", type=Path, required=True)
    prepare.add_argument("--max-bytes", type=int, required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--source", type=Path, required=True)
    verify.add_argument("--identity", type=Path, required=True)
    verify.add_argument("--identity-sha256", required=True)
    verify.add_argument("--archive", type=Path)
    verify.add_argument("--task-runtime", action="store_true")
    verify.add_argument("--node")
    args = parser.parse_args()
    if args.command == "prepare":
        identity = prepare_artifact(
            args.archive, destination=args.destination, max_bytes=args.max_bytes,
        )
        print(json.dumps({"identity_sha256": hashlib.sha256(identity.encoded()).hexdigest()}))
    else:
        identity = UpstreamHermesSource.load(args.identity, args.identity_sha256)
        if args.archive is not None:
            verify_archive(args.archive, identity)
        verify_source(args.source, identity)
        if args.task_runtime:
            if not args.node or not Path(args.node).is_absolute():
                raise ValueError("Runtime admission requires an explicit native Node executable")
            sys.dont_write_bytecode = True
            try:
                receipt = task_runtime_receipt(args.source, identity, args.node)
            finally:
                verify_source(args.source, identity)
            print(json.dumps(receipt, sort_keys=True))
        else:
            print(json.dumps(asdict(identity), sort_keys=True))


if __name__ == "__main__":
    main()
