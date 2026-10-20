"""Read-only task-side validation of the existing native capture manifest.

This file is uploaded through Harbor to its existing /installed-agent directory. It
does not install, extract, mount, change ownership, or invoke the runtime.
"""

import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path, PurePosixPath


class AdmissionError(ValueError):
    """Only fixed admission messages are safe to send across the task boundary."""


def require(condition, reason):
    if not condition:
        raise AdmissionError(reason)


def digest(stream):
    return hashlib.file_digest(stream, "sha256").hexdigest()


def absolute(value):
    require(isinstance(value, str), "invalid capture path")
    path = PurePosixPath(value)
    require(path.is_absolute() and str(path) == value and ".." not in path.parts,
            "invalid capture path")
    return path


def within(path, root):
    return path == root or path.startswith(root + "/")


def validate_manifest(manifest):
    require(manifest.get("schema") == 1 and manifest.get("harness") == "codex"
            and manifest.get("status") == "captured", "unsupported capture manifest")
    home = str(absolute(manifest["home"]))
    require(home != "/", "invalid capture home")
    roots = manifest["roots"]
    require(isinstance(roots, list) and len(roots) == len(set(roots)) and roots,
            "invalid capture roots")
    for root in roots:
        absolute(root)
        require(within(root, home) and root != home, "capture root outside runtime home")
        require(not any(root != other and within(root, other) for other in roots),
                "overlapping capture roots")
    entries = {}
    for entry in manifest["files"]:
        path = str(absolute(entry["path"]))
        require(path not in entries and any(within(path, root) for root in roots),
                "duplicate or unbound capture member")
        require(entry["type"] in ("file", "directory", "symlink"), "unsupported capture member")
        for key in ("uid", "gid", "mode"):
            require(type(entry[key]) is int and entry[key] >= 0, "invalid capture identity")
        require(entry["mode"] <= 0o7777, "invalid capture mode")
        if entry["type"] == "file":
            require(type(entry["bytes"]) is int and entry["bytes"] >= 0
                    and re.fullmatch(r"[a-f0-9]{64}", entry["sha256"]),
                    "invalid captured file digest")
        elif entry["type"] == "symlink":
            absolute(entry["resolved"])
            require(any(within(entry["resolved"], root) for root in roots),
                    "captured symlink escapes closure")
        entries[path] = entry
    require(all(root in entries for root in roots), "capture root missing from inventory")
    for path in entries:
        root = next(root for root in roots if within(path, root))
        for parent in PurePosixPath(path).parents:
            if str(parent) == root or within(str(parent), root):
                require(entries.get(str(parent), {}).get("type") == "directory",
                        "missing captured parent directory")
    node = manifest["node_identity"]
    require(node == entries.get(node["path"]), "node identity differs from inventory")
    require(manifest["launcher"] in entries, "launcher absent from inventory")
    layout = manifest["native_package_layout"]
    require(layout["metadata"]["target"] == "x86_64-unknown-linux-musl"
            and layout["metadata"]["version"] == "0.163.0",
            "unsupported retained platform or version")
    expected = ("bin/codex", "bin/codex-code-mode-host", "codex-path/rg", "codex-resources/bwrap")
    require({row["path"] for row in layout["required_executables"]}
            == {layout["root"] + "/" + name for name in expected},
            "incomplete native executable closure")
    for row in layout["required_executables"]:
        require(row == entries.get(row["path"]) and row["type"] == "file"
                and row["mode"] & 0o111, "invalid native executable identity")
    require(layout["root"] + "/codex-package.json" in entries, "native layout metadata missing")
    for name in ("nvm.sh", "nvm-exec", "bash_completion", "alias/default"):
        require(home + "/.nvm/" + name in entries, "incomplete NVM closure")
    return entries


def read_manifest(path, expected_digest):
    raw = Path(path).read_bytes()
    require(hashlib.sha256(raw).hexdigest() == expected_digest, "capture manifest digest mismatch")
    manifest = json.loads(raw)
    return manifest, validate_manifest(manifest)


def mount_table(text):
    def decode(value):
        return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)

    rows = []
    for line in text.splitlines():
        before, after = line.split(" - ", 1)
        fields, fs = before.split(), after.split()
        rows.append({"id": fields[0], "root": decode(fields[3]),
                     "target": decode(fields[4]), "options": fields[5].split(","),
                     "filesystem": fs[0]})
    require(rows, "mount visibility missing")
    return rows


def covering_mount(path, mounts):
    matches = [row for row in mounts if within(path, row["target"].rstrip("/"))]
    require(matches, "mount coverage missing")
    longest = max(len(row["target"]) for row in matches)
    effective = [row for row in matches if len(row["target"]) == longest]
    require(len(effective) == 1, "ambiguous stacked runtime mount")
    return effective[0]


def probe(manifest, entries):
    require((os.getuid(), os.getgid(), os.environ.get("HOME"))
            == (manifest["uid"], manifest["gid"], manifest["home"]),
            "task default UID/GID/HOME mismatch")
    # NODE_PATH is a fallback search path. Native resolution must still select
    # the exact captured platform package, as checked by the admission owner.
    for key in ("NODE_OPTIONS", "LD_PRELOAD", "LD_LIBRARY_PATH", "BASH_ENV", "ENV"):
        require(not os.environ.get(key), f"conflicting interpreter or loader override: {key}")
    require(os.environ.get("NVM_DIR", manifest["home"] + "/.nvm")
            == manifest["home"] + "/.nvm", "conflicting NVM_DIR")
    mounts = mount_table(Path("/proc/self/mountinfo").read_text())
    observed = set()
    root_mounts = []
    for root in manifest["roots"]:
        mount = covering_mount(root, mounts)
        require(mount["target"] == root and "ro" in mount["options"],
                "runtime root lacks original-path read-only mount")
        root_mounts.append(mount)
        require(all("ro" in row["options"] for row in mounts if within(row["target"], root)),
                "writable mount inside runtime closure")
        observed.add(root)
        if entries[root]["type"] == "directory":
            for directory, dirs, files in os.walk(root, followlinks=False):
                observed.update(str(Path(directory) / name) for name in dirs + files)
    require(observed == set(entries), "task runtime member inventory mismatch")
    for path, entry in entries.items():
        actual = Path(path)
        metadata = actual.lstat()
        kind = ("symlink" if stat.S_ISLNK(metadata.st_mode) else
                "directory" if stat.S_ISDIR(metadata.st_mode) else
                "file" if stat.S_ISREG(metadata.st_mode) else "unsupported")
        require((kind, stat.S_IMODE(metadata.st_mode), metadata.st_uid, metadata.st_gid)
                == (entry["type"], entry["mode"], entry["uid"], entry["gid"]),
                "task runtime type/mode/ownership mismatch")
        resolved = str(actual.resolve(strict=True))
        require(resolved in entries and "ro" in covering_mount(resolved, mounts)["options"],
                "task runtime path escapes read-only closure")
        if kind == "symlink":
            require(os.readlink(actual) == entry["target"] and resolved == entry["resolved"],
                    "task runtime symlink mismatch")
        elif kind == "file":
            with actual.open("rb") as stream:
                require(metadata.st_size == entry["bytes"] and digest(stream) == entry["sha256"],
                        "task runtime bytes mismatch")
    require(Path(manifest["home"] + "/.nvm/alias/default").read_text().strip()
            == manifest["default_alias"], "NVM default alias mismatch")
    metadata = json.loads(Path(manifest["native_package_layout"]["root"],
                               "codex-package.json").read_text())
    require(metadata == manifest["native_package_layout"]["metadata"],
            "native package metadata mismatch")
    require("rw" in covering_mount("/tmp/codex-home", mounts)["options"]
            and "rw" in covering_mount("/tmp/codex-secrets", mounts)["options"],
            "native writable state is on a read-only mount")
    return {"uid": os.getuid(), "gid": os.getgid(), "home": os.environ["HOME"],
            "members_verified": len(entries), "runtime_mounts": root_mounts,
            "scope": "read_only_task_view_not_controller_immutability"}


if __name__ == "__main__":
    try:
        capture, members = read_manifest(sys.argv[1], sys.argv[2])
        print(json.dumps(probe(capture, members)))
    except (ValueError, KeyError, TypeError, OSError) as error:
        reason = str(error) if isinstance(error, AdmissionError) else type(error).__name__
        print("Retained Codex task admission failed: " + reason, file=sys.stderr)
        raise SystemExit(1) from None
