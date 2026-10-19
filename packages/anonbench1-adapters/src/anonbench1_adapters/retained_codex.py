"""Admit an existing native Codex capture; Harbor still owns the entire Trial."""

import asyncio
import hashlib
import json
import os
import re
import shlex
import tarfile
import time
from pathlib import Path, PurePosixPath

from anonbench1_adapters.retained_codex_probe import digest, read_manifest, require

ARTIFACT = "retained-runtime-admission.json"
REMOTE_MANIFEST = "/installed-agent/retained-codex-runtime.json"
REMOTE_PROBE = "/installed-agent/retained-codex-probe.py"
SHELL_GUARD = (
    'for _retained_bin in node codex python3; do '
    'case "$(builtin type -t "$_retained_bin")" in ""|file) ;; '
    '*) echo "Retained Codex shell shadowing" >&2; exit 1;; esac; done; '
    'if [ -n "${PYTHONPATH:-}${PYTHONHOME:-}" ]; then '
    'echo "Retained Codex Python override" >&2; exit 1; fi; '
)

# Use Node's module resolver from the admitted launcher, as the unmodified
# official launcher does. The subsequent native version command exercises it.
RESOLVE = """
const fs = require('node:fs');
const path = require('node:path');
const {createRequire} = require('node:module');
const launcher = fs.realpathSync(process.argv[2]);
const packageJson = createRequire(launcher).resolve('@openai/codex-linux-x64/package.json');
const platformRoot = path.join(path.dirname(packageJson), 'vendor', 'x86_64-unknown-linux-musl');
console.log(JSON.stringify({node: process.execPath, node_version: process.version,
  platform: process.platform, arch: process.arch, launcher: process.argv[2],
  resolved_launcher: launcher, platform_package: packageJson, platform_root: platformRoot,
  executable: fs.realpathSync(path.join(platformRoot, 'bin', 'codex'))}));
"""


class RetainedCodex:
    def __init__(self, binding, *, version):
        require(isinstance(binding, dict)
                and set(binding) == {"archive_path", "manifest_path", "manifest_sha256"},
                "retained_runtime requires archive_path, manifest_path and manifest_sha256")
        require(isinstance(binding["manifest_sha256"], str)
                and re.fullmatch(r"[a-f0-9]{64}", binding["manifest_sha256"]),
                "invalid retained manifest digest")
        self.archive = Path(binding["archive_path"])
        self.manifest_path = Path(binding["manifest_path"])
        require(self.archive.is_absolute() and self.manifest_path.is_absolute(),
                "retained input paths must be absolute")
        self.manifest_sha256 = binding["manifest_sha256"]
        self.manifest, self.entries = read_manifest(self.manifest_path, self.manifest_sha256)
        self.manifest_text = self.manifest_path.read_text()
        require(self.manifest_sha256 == hashlib.sha256(
            self.manifest_text.encode()).hexdigest(), "capture manifest changed while reading")
        require(version == "0.163.0", "retained Codex requires explicit version 0.163.0")
        self.version = version
        self.archive_verified = False
        self.setup_passed = False
        self.records = []

    def verify_archive(self):
        expected = self.manifest["archive"]
        with self.archive.open("rb") as stream:
            before = os.fstat(stream.fileno())
            require(before.st_size == expected["bytes"] and digest(stream) == expected["sha256"],
                    "retained archive digest or size mismatch")
            stream.seek(0)
            seen = set()
            with tarfile.open(fileobj=stream, mode="r:gz") as archive:
                for member in archive:
                    require(member.name.startswith("rootfs/"), "invalid archive member path")
                    path = "/" + member.name.removeprefix("rootfs/")
                    require(path in self.entries and path not in seen,
                            "archive member inventory mismatch")
                    seen.add(path)
                    entry = self.entries[path]
                    kind = ("file" if member.isfile() else "directory" if member.isdir()
                            else "symlink" if member.issym() else "unsupported")
                    require((kind, member.mode, member.uid, member.gid)
                            == (entry["type"], entry["mode"], entry["uid"], entry["gid"]),
                            "archive member type/mode/ownership mismatch")
                    if kind == "file":
                        with archive.extractfile(member) as content:
                            require(member.size == entry["bytes"]
                                    and digest(content) == entry["sha256"],
                                    "archive member bytes mismatch")
                    elif kind == "symlink":
                        require(member.linkname == entry["target"], "archive symlink mismatch")
            require(seen == set(self.entries), "archive member inventory incomplete")
            after = os.fstat(stream.fileno())
            require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                    == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns),
                    "retained archive changed during admission")

    async def admit(self, agent, environment, *, phase):
        started = time.monotonic()
        record = {"phase": phase, "status": "failed", "manifest_sha256": self.manifest_sha256,
                  "archive": self.manifest["archive"], "members": len(self.entries),
                  "source_manufacture": self.manifest.get("source_manufacture"),
                  "capture_gaps": self.manifest.get("gaps", []),
                  "scope": "admission_outside_sampled_native_execution"}
        try:
            require(phase == "setup" or self.setup_passed,
                    "retained runtime requires successful setup admission")
            if phase == "setup":
                if not self.archive_verified:
                    await asyncio.to_thread(self.verify_archive)
                    self.archive_verified = True
                for content, target in (
                    (self.manifest_text, REMOTE_MANIFEST),
                    (Path(__file__).with_name("retained_codex_probe.py").read_text(), REMOTE_PROBE),
                ):
                    await agent._upload_config_text(environment, content=content,
                                                    remote_path=target,
                                                    filename=PurePosixPath(target).name)
            require(self.archive_verified, "retained runtime requires successful setup admission")
            result = await agent.exec_as_agent(
                environment,
                command=(SHELL_GUARD
                         + f"python3 -B {REMOTE_PROBE} {REMOTE_MANIFEST} {self.manifest_sha256}"),
            )
            record["task_view"] = json.loads(result.stdout)
            version_command = agent.get_version_command()
            require(version_command is not None and version_command.endswith("codex --version"),
                    "unsupported native Codex version command")
            prefix = version_command.removesuffix("codex --version")
            resolution = await agent.exec_as_agent(
                environment,
                command=(SHELL_GUARD + prefix + '[ "$(builtin type -t node)" = file ] && '
                         '[ "$(builtin type -t codex)" = file ] && '
                         f"node -e {shlex.quote(RESOLVE)} -- unused "
                         '"$(command -v codex)"'),
            )
            selected = json.loads(resolution.stdout)
            node = self.manifest["node_identity"]["path"]
            launcher = self.manifest["launcher"]
            root = self.manifest["native_package_layout"]["root"]
            require(selected == {
                "node": node, "node_version": self.manifest["node_version"],
                "platform": "linux", "arch": "x64", "launcher": launcher,
                "resolved_launcher": self.entries[launcher].get("resolved", launcher),
                "platform_package": str(PurePosixPath(root).parent.parent / "package.json"),
                "platform_root": root, "executable": root + "/bin/codex",
            }, "native Node/launcher/platform resolution mismatch")
            result = await agent.exec_as_agent(environment, command=version_command)
            require(agent.parse_version(result.stdout or "") == self.version
                    and agent.parse_version(self.manifest["version_stdout"]) == self.version,
                    "native retained Codex version mismatch")
            record.update(status="passed", native_resolution=selected, version=self.version)
        finally:
            record["duration_seconds"] = time.monotonic() - started
            self.records.append(record)
            # Do not populate AgentContext before Harbor's native session conversion.
            try:
                (agent.logs_dir / ARTIFACT).write_text(json.dumps(self.records, indent=2) + "\n")
            except OSError:
                if record["status"] == "passed":
                    raise
                agent.logger.debug("Could not retain failed runtime admission")
        if phase == "setup":
            self.setup_passed = True
