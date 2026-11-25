"""Prove that the actual Windows payload contains no Android distribution archive."""
import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import struct
import subprocess
import tempfile
import threading
import zipfile

MAX_MEMBER = 1 << 30
MAX_DEPTH = 6
ANDROID_SUFFIX = {".apk", ".aab", ".apks", ".xapk"}
OLD_RESOURCES = {"companion.apk", "companion-desktop.apk", "android-companion-notices.txt",
                 "mpi-desktop-transport-evidence.json", "companion-desktop-proof.json"}
ARCHIVE_MAGIC = (b"7z\xbc\xaf\x27\x1c", b"Rar!\x1a\x07", b"\x1f\x8b", b"\xfd7zXZ\x00")
ANDROID_ENTRY = re.compile(rb"PK\x03\x04.{26}(?:base/manifest/)?AndroidManifest\.xml", re.DOTALL)


def require(value, message):
    if not value:
        raise ValueError(message)


def safe_name(name):
    require(isinstance(name, str) and name and not any(ord(c) < 32 for c in name), "invalid archive member")
    path = name.replace("\\", "/").rstrip("/")
    require(not path.startswith("/") and all(p not in ("", ".", "..") and ":" not in p
                                               for p in path.split("/")), "unsafe archive member")
    tail = PurePosixPath(path).name.lower()
    require(tail not in OLD_RESOURCES and PurePosixPath(tail).suffix not in ANDROID_SUFFIX,
            f"Android package or stale companion resource: {path}")
    return path


def copy_exact(source, destination, length):
    require(type(length) is int and 0 <= length <= MAX_MEMBER, "oversized or malformed package entry")
    remaining = length
    while remaining:
        chunk = source.read(min(remaining, 1 << 20))
        require(chunk, "truncated package entry")
        destination.write(chunk)
        remaining -= len(chunk)


def inspect_stream(data, path, depth=0):
    require(depth <= MAX_DEPTH, "nested payload too deep")
    safe_name(path)
    head = data.read(16)
    data.seek(0)
    if path.lower().endswith(".asar"):
        return inspect_asar(data, path, depth)
    if zipfile.is_zipfile(data):
        data.seek(0)
        with zipfile.ZipFile(data) as archive:
            files = archive.infolist()
            require(len(files) <= 25_000, "too many nested zip entries")
            names = {e.filename.replace("\\", "/").lower().lstrip("/") for e in files}
            require("androidmanifest.xml" not in names and "base/manifest/androidmanifest.xml" not in names,
                    f"Android package hidden under {path}")
            for item in files:
                name = safe_name(item.filename)
                if item.is_dir():
                    continue
                require(not item.flag_bits & 1, f"encrypted nested payload in {path}")
                with archive.open(item) as src, tempfile.SpooledTemporaryFile(max_size=1 << 20) as target:
                    copy_exact(src, target, item.file_size)
                    target.seek(0)
                    inspect_stream(target, name, depth + 1)
        return
    require(not head.startswith((b"PK\x03\x04", *ARCHIVE_MAGIC)), f"unexpected opaque archive: {path}")
    data.seek(0)
    carry = b""
    while True:
        chunk = data.read(1 << 20)
        if not chunk:
            break
        window = carry + chunk
        require(not ANDROID_ENTRY.search(window), f"embedded Android ZIP signature in {path}")
        carry = window[-90:]


def inspect_asar(data, path, depth):
    initial = data.read(8)
    require(len(initial) == 8, "invalid ASAR size header")
    payload_size, header_size = struct.unpack("<II", initial)
    require(payload_size == 4 and 8 <= header_size <= 32 * 1024 * 1024, "invalid ASAR header")
    header = data.read(header_size)
    require(len(header) == header_size, "truncated ASAR header")
    json_length = struct.unpack_from("<I", header, 4)[0]
    require(json_length <= len(header) - 8, "invalid ASAR JSON")
    root = json.loads(header[8:8 + json_length])
    data.seek(0, 2)
    end = data.tell()
    start = 8 + header_size
    count = 0

    def entries(node, parent="", nesting=0):
        nonlocal count
        require(nesting <= 60 and isinstance(node, dict) and isinstance(node.get("files"), dict), "invalid ASAR tree")
        for basename, entry in node["files"].items():
            count += 1
            require(count <= 60_000 and "/" not in basename and "\\" not in basename and isinstance(entry, dict),
                    "invalid ASAR member metadata")
            name = safe_name(parent + basename)
            if "files" in entry:
                entries(entry, name + "/", nesting + 1)
                continue
            require("link" not in entry, "ASAR symbolic link is not auditable")
            if entry.get("unpacked") is True:
                continue  # app.asar.unpacked is separately walked from the exact outer payload.
            offset = entry.get("offset")
            size = entry.get("size")
            require(isinstance(offset, str) and re.fullmatch(r"[0-9]{1,12}", offset) and
                    type(size) is int and 0 <= size <= MAX_MEMBER and start + int(offset) + size <= end,
                    "ASAR member outside payload")
            data.seek(start + int(offset))
            with tempfile.SpooledTemporaryFile(max_size=1 << 20) as target:
                copy_exact(data, target, size)
                target.seek(0)
                inspect_stream(target, name, depth + 1)

    entries(root)


def inspect_file(path, name):
    require(not path.is_symlink() and not getattr(path, "is_junction", lambda: False)(), "linked installer payload is not auditable")
    require(path.is_file(), "installer payload entry is not a file")
    with path.open("rb") as stream:
        inspect_stream(stream, name)


def inspect_directory(root):
    require(root.is_dir() and not root.is_symlink() and not getattr(root, "is_junction", lambda: False)(), "missing staged package directory")
    for path in root.rglob("*"):
        name = str(path.relative_to(root)).replace("\\", "/")
        safe_name(name)
        require(not path.is_symlink() and not getattr(path, "is_junction", lambda: False)(), "linked installer payload is not auditable")
        if path.is_file():
            inspect_file(path, name)


def installer_members(seven, source, expected_type):
    listing = subprocess.check_output([seven, "l", "-slt", "-ba", str(source)], timeout=120,
                                      encoding="utf-8", errors="strict")
    detail = subprocess.check_output([seven, "l", "-slt", str(source)], timeout=120,
                                     encoding="utf-8", errors="strict")
    header, sep, _ = detail.partition("----------")
    require(sep and re.search(r"(?m)^Type = " + expected_type + r"\s*$", header), f"not a complete {expected_type} archive")
    records = []
    seen = set()
    for chunk in re.split(r"\r?\n\r?\n", listing.strip()):
        fields = dict(line.split(" = ", 1) for line in chunk.splitlines() if " = " in line)
        require("Path" in fields, "unknown installer entry")
        name = fields["Path"]
        path = safe_name(name).lower()
        require(path not in seen and "Symbolic Link" not in fields and "Hard Link" not in fields,
                "ambiguous or linked installer entry")
        seen.add(path)
        if fields.get("Folder") == "+" or fields.get("Attributes", "").startswith("D"):
            continue
        reported = fields.get("Size", "")
        require(re.fullmatch("[0-9]{1,12}", reported) or (not reported and expected_type == "Nsis"),
                "installer entry has malformed size")
        size = int(reported) if reported else None
        require(size is None or size <= MAX_MEMBER, "oversized installer entry")
        records.append((name, path, size))
    require(0 < len(records) <= 20_000, "invalid installer entry count")
    return records


def extract_entry(seven, source, name, size, dest):
    with dest.open("wb") as out, subprocess.Popen(
        [seven, "x", "-so", "-spd", str(source), "--", name], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    ) as proc:
        limit = threading.Timer(120, lambda: proc.poll() is None and proc.kill())
        limit.daemon = True
        limit.start()
        try:
            total = 0
            while chunk := proc.stdout.read(1 << 20):
                total += len(chunk)
                require(total <= MAX_MEMBER and (size is None or total <= size), "oversized installer entry")
                out.write(chunk)
            require(proc.wait(timeout=120) == 0 and (size is None or total == size), "incomplete installer entry")
            return total
        finally:
            limit.cancel()
            if proc.poll() is None:
                proc.kill()


def inspect_installer(source, notices, license_file, engine, engine_notices):
    selected = os.getenv("MPI_7ZIP")
    seven = str(Path(selected).resolve(strict=True)) if selected else shutil.which("7z")
    require(seven, "full 7-Zip (7z) is required for Windows installer proof")
    with source.open("rb") as stream:
        require(stream.read(2) == b"MZ", "Windows artifact is not a PE image")
    expected = {"resources/third-party-notices.txt": notices, "resources/license.txt": license_file,
                "resources/data-engine.exe": engine, "resources/engine-runtime-notices.txt": engine_notices}
    found = set()

    def inspect_archive(archive, archive_type, root, depth=0):
        for index, (name, normalized, size) in enumerate(installer_members(seven, archive, archive_type)):
            suffix = ".asar" if normalized.endswith(".asar") else ".bin"
            candidate = root / f"entry-{depth}-{index}{suffix}"
            size = extract_entry(seven, archive, name, size, candidate)
            if normalized.split("/")[-1] == "app-64.7z" and depth == 0:
                inspect_archive(candidate, "7z", root, depth + 1)
                candidate.unlink()
                continue
            inspect_file(candidate, name)
            for tail, canonical in expected.items():
                if normalized == tail or normalized.endswith("/" + tail):
                    require(tail not in found and size == canonical.stat().st_size, f"unexpected installer {tail}")
                    with candidate.open("rb") as a, canonical.open("rb") as b:
                        while x := a.read(1 << 20):
                            require(x == b.read(1 << 20), f"incorrect packaged {tail}")
                    found.add(tail)
            candidate.unlink()

    with tempfile.TemporaryDirectory(prefix="mpi-nsis-no-apk-") as scratch:
        inspect_archive(source, "Nsis", Path(scratch))
    require(found == expected.keys(), "installer missing verified license, notice or engine")


def main():
    parser = argparse.ArgumentParser()
    input_ = parser.add_mutually_exclusive_group(required=True)
    input_.add_argument("--directory", type=Path)
    input_.add_argument("--installer", type=Path)
    parser.add_argument("--notices", type=Path)
    parser.add_argument("--license", type=Path)
    parser.add_argument("--engine", type=Path)
    parser.add_argument("--engine-notices", type=Path)
    args = parser.parse_args()
    try:
        if args.directory:
            inspect_directory(args.directory)
        else:
            require(args.notices and args.license and args.engine and args.engine_notices, "missing exact installer inputs")
            inspect_installer(args.installer, args.notices, args.license, args.engine, args.engine_notices)
        print("No Android APK/AAB found in the verified Windows payload.")
    except (ValueError, OSError, RuntimeError, zipfile.BadZipFile, subprocess.SubprocessError) as exc:
        parser.exit(1, f"Windows payload blocked: {exc}\n")


if __name__ == "__main__":
    main()
