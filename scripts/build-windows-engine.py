"""Freeze and inventory the Windows 10/11 engine in an explicit audited environment."""
from __future__ import annotations

import argparse
import ast
from collections import defaultdict
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
ENGINE = "data-engine.exe"
RECEIPT = "engine-runtime-receipt.json"
NOTICES = "ENGINE-RUNTIME-NOTICES.txt"
PYTHON_VERSION = "3.13.3"
TOOL_VERSIONS = {"PyInstaller": "6.21.0", "pyinstaller-hooks-contrib": "2026.6"}
BOOTLOADER_SHA = "0214e42624efe0d6f586ca673ef235f9d4bdee136853b4289e34e9adcd3787ed"
BASE_FILES = {
    "python.exe": "d87063e5597f257004c731b66c59c56c91038861c6877b1a3dca6b8c4e919125",
    "python313.dll": "69fd86ea29370697c203f7e12830084f920f490766a8e3045af52c036a9ad529",
    "LICENSE.txt": "62bec384df47b0328307db41455ff6ea2559e5546b394ac69148561b21703120",
    "VCRUNTIME140.dll": "052ad6a20d375957e82aa6a3c441ea548d89be0981516ca7eb306e063d5027f4",
    "VCRUNTIME140_1.dll": "6a99bc0128e0c7d6cbbf615fcc26909565e17d4ca3451b97f8987f9c6acbc6c8",
    "DLLs/libcrypto-3.dll": "ccfffddcd3defb8d899026298af9af43bc186130f8483d77e97c93233d5f27d7",
    "DLLs/libssl-3.dll": "007142039f04d04e0ed607bda53de095e5bc6a8a10d26ecedde94ea7d2d7eefe",
    "DLLs/libffi-8.dll": "eff52743773eb550fcc6ce3efc37c85724502233b6b002a35496d828bd7b280a",
}
# Runtime exception and native licenses are carried in publisher texts, not
# inferred from the versions of the (larger) available build environment.
RUNTIME = {
    "click": ("8.5.0", "BSD-3-Clause"),
    "et-xmlfile": ("2.0.0", "MIT"),
    "markdown-it-py": ("4.3.0", "MIT"),
    "mdurl": ("0.1.2", "MIT"),
    "numpy": ("2.5.4", "BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0; see bundled native-library texts"),
    "openpyxl": ("3.1.5", "MIT"),
    "pillow": ("12.3.0", "MIT-CMU; see bundled native-library texts"),
    "pygments": ("2.21.0", "BSD-2-Clause"),
    "pyinstaller": ("6.21.0", "GPL-2.0-or-later with application bundling exception; see COPYING"),
    "pywin32": ("312", "PSF"),
    "rich": ("15.0.0", "MIT"),
}
SHA = re.compile(r"[0-9a-f]{64}")
GIT_SHA = re.compile(r"[0-9a-f]{40}")


def need(cond, why):
    if not cond:
        raise ValueError(why)


def file_sha(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def package_key(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def child_env(base, env, work):
    system = Path(env["SystemRoot"])
    need(system.is_absolute(), "invalid Windows system path")
    kept = {k: v for k, v in env.items() if k.upper() in {
        "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "SYSTEMDRIVE",
        "USERPROFILE", "APPDATA", "LOCALAPPDATA", "HOMEDRIVE", "HOMEPATH",
        "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432",
    }}
    # Scope the search to Windows OS and the inspected interpreter only. Venv
    # Scripts is unnecessary because PyInstaller is invoked as a module.
    kept.update(PATH=os.pathsep.join(str(x) for x in (system / "System32", system, base)),
                PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1",
                PYINSTALLER_CONFIG_DIR=str(work / "cache"))
    return kept


def archive_name(name):
    return "PYZ.pyz" if name == "PYZ-00.pyz" else name.replace("\\", "/")


def reconcile(carchive, module_archive, pkg, pyz):
    pkg_rows = [row for row in pkg if row[2] != "OPTION"]
    need(len({archive_name(r[0]) for r in pkg_rows}) == len(pkg_rows), "duplicate frozen package names")
    need({archive_name(r[0]) for r in pkg_rows} == {archive_name(k) for k in carchive},
         "actual CArchive and package TOC differ")
    need(len({r[0] for r in pyz}) == len(pyz), "duplicate frozen Python modules")
    need({r[0] for r in pyz} == set(module_archive), "actual PYZ and module TOC differ")
    return pkg_rows + pyz


def inspect_sources(rows, checkout, base, venv, work, distributions):
    database = defaultdict(list)
    def key(path):
        return str(path.resolve()).casefold()

    for d in distributions:
        for f in d.files or []:
            database[key(Path(d.locate_file(f)))].append(d)
    mapped, binary_sources = defaultdict(list), {}
    site = venv / "Lib" / "site-packages"
    for name, raw, kind in rows:
        path = Path(raw).resolve()
        basename = name.replace("\\", "/").rsplit("/", 1)[-1].lower()
        # The supported Windows 10/11 installations provide their own UCRT.
        # Never copy DLLs with these names from our host, even System32.
        need(not (kind == "BINARY" and (basename.startswith("api-ms-win-") or basename == "ucrtbase.dll")),
             f"unexpected bundled Windows system DLL: {basename}")
        if path.is_relative_to(site):
            found = database[key(path)]
            if not found and path.parent.name == "__pycache__":
                found = database[key(path.parent.parent / (path.name.split(".")[0] + ".py"))]
            need(len(found) == 1, f"package attribution missing or ambiguous for {name}")
            mapped[package_key(found[0].metadata["Name"])].append((name, kind, found[0]))
        elif path.is_relative_to(base):
            if kind in {"BINARY", "EXTENSION"}:
                binary_sources[name] = path
        elif path.is_relative_to(work) or (path.is_relative_to(checkout) and not path.is_relative_to(venv)):
            # Generated build resources, MPI source/embedded rules, and
            # first-party source sidecars; never let checkout fallback Hide a
            # binary with no recorded dependency origin.
            need(kind not in {"BINARY", "EXTENSION"}, f"unattributed checkout/build binary: {name}")
        else:
            raise ValueError(f"frozen archive includes an unapproved external origin: {name}")
    return mapped, binary_sources


def inspect_environment():
    need(sys.platform == "win32" and sys.maxsize > 2**32 and sys.prefix != sys.base_prefix,
         "use the isolated Windows x64 release virtualenv")
    need(".".join(map(str, sys.version_info[:3])) == PYTHON_VERSION, "unreviewed CPython build version")
    for name, version in TOOL_VERSIONS.items():
        need(metadata.version(name) == version, f"unreviewed packaging tool: {name}")
    base = Path(sys.base_prefix).resolve()
    for rel, expected in BASE_FILES.items():
        need(file_sha(base / rel) == expected, f"CPython release input differs from reviewed hash: {rel}")
    pyi = metadata.distribution("PyInstaller")
    loader = Path(pyi.locate_file("PyInstaller/bootloader/Windows-64bit-intel/run.exe"))
    need(file_sha(loader) == BOOTLOADER_SHA, "frozen bootloader differs from reviewed Windows build")
    return base


def notice_text(mapped, core):
    sections = [
        "MPI Windows measurement engine - actual bundled runtime notices\n",
        "CPython 3.13.3 (includes terms for incorporated native libraries and Microsoft distributable code)\n",
        (core / "LICENSE.txt").read_text(encoding="utf-8"),
    ]
    runtime = []
    for name in sorted(mapped):
        entries = mapped[name]
        dist = entries[0][2]
        version, license_id = RUNTIME[name]
        need(dist.version == version, f"unreviewed actual runtime dependency version: {name}")
        texts = []
        for path in dist.files or []:
            if not any(part.lower().endswith(".dist-info") for part in path.parts):
                continue
            if not (any(part.lower() == "licenses" for part in path.parts) or
                    re.match(r"(?:licen[cs]e|copying|authors|notice)", path.name, re.I)):
                continue
            f = Path(dist.locate_file(path))
            raw = f.read_bytes()
            need(0 < len(raw) <= 4_000_000, f"invalid publisher attribution text: {path.name}")
            texts.append({"name": str(path).replace("\\", "/"), "sha256": hashlib.sha256(raw).hexdigest()})
            sections.extend([f"\n=== {name} {version} - {license_id} - {path.name} ===\n", raw.decode("utf-8", errors="strict")])
        need(texts, f"no publisher license text recovered for {name}")
        runtime.append({"name": name, "version": version, "license": license_id,
                        "archive_member_count": len(entries), "license_files": texts})
    return ("\n".join(sections) + "\n").encode(), runtime


def build_worker(output, work, commit):
    from PyInstaller.archive.readers import CArchiveReader

    base = inspect_environment()
    subprocess.run([sys.executable, "-m", "pip", "check"], cwd=ROOT, check=True)
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--workpath", str(work / "work"),
                    "--distpath", str(output), str(ROOT / "data-engine.spec")], cwd=ROOT, check=True)
    engine = output / ENGINE
    tables = work / "work" / "data-engine"
    pkg = ast.literal_eval((tables / "PKG-00.toc").read_text(encoding="utf-8"))[2]
    pyz = ast.literal_eval((tables / "PYZ-00.toc").read_text(encoding="utf-8"))[1]
    physical = CArchiveReader(str(engine))
    modules = physical.open_embedded_archive("PYZ.pyz")
    rows = reconcile(physical.toc, modules.toc, pkg, pyz)
    mapped, native = inspect_sources(rows, ROOT, base, Path(sys.prefix).resolve(), work,
                                     metadata.distributions())
    need(set(mapped) == set(RUNTIME), f"frozen distribution set differs: {sorted(set(mapped) ^ set(RUNTIME))}")
    archive_members = {archive_name(raw): raw for raw in physical.toc}
    base_runtime = {}
    for name, path in sorted(native.items()):
        data_sha = hashlib.sha256(physical.extract(archive_members[archive_name(name)])).hexdigest()
        need(data_sha == file_sha(path), f"packaged native runtime differs from reviewed source: {name}")
        base_runtime[name] = data_sha
    notices, runtime = notice_text(mapped, base)
    (output / NOTICES).write_bytes(notices)
    receipt = {
        "schema": 1, "source_commit": commit, "python_version": PYTHON_VERSION,
        "packaging_tools": TOOL_VERSIONS, "bootloader_sha256": BOOTLOADER_SHA,
        "engine_sha256": file_sha(engine), "notices_sha256": file_sha(output / NOTICES),
        "carchive_entries": len(physical.toc), "pyz_entries": len(modules.toc),
        "runtime_distributions": runtime, "native_runtime": base_runtime,
        "python_base_input_sha256": BASE_FILES, "platform": "windows-x64-win10-win11",
    }
    (output / RECEIPT).write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")


def verify(engine, receipt, notices, commit):
    need(GIT_SHA.fullmatch(commit), "full expected source commit required")
    r = json.loads(receipt.read_text(encoding="utf-8"))
    need(isinstance(r, dict) and type(r.get("schema")) is int and r["schema"] == 1 and
         r.get("source_commit") == commit and r.get("platform") == "windows-x64-win10-win11",
         "wrong engine receipt source, schema or platform")
    need(r.get("python_version") == PYTHON_VERSION and r.get("python_base_input_sha256") == BASE_FILES and
         r.get("packaging_tools") == TOOL_VERSIONS and r.get("bootloader_sha256") == BOOTLOADER_SHA,
         "unreviewed engine build provenance")
    need(engine.stat().st_size > 0 and file_sha(engine) == r.get("engine_sha256") and
         notices.stat().st_size > 0 and file_sha(notices) == r.get("notices_sha256"), "engine/notices hash mismatch")
    need(type(r.get("carchive_entries")) is int and r["carchive_entries"] > 0 and
         type(r.get("pyz_entries")) is int and r["pyz_entries"] > 0, "empty frozen archive proof")
    rows = r["runtime_distributions"]
    need(isinstance(rows, list) and len(rows) == len(RUNTIME), "runtime receipt incomplete")
    versions = {p["name"]: (p["version"], p["license"]) for p in rows}
    need(versions == RUNTIME and all(type(p["archive_member_count"]) is int and p["archive_member_count"] > 0 and
         p["license_files"] and all(SHA.fullmatch(f["sha256"]) for f in p["license_files"]) for p in rows),
         "unreviewed runtime versions or missing publisher attribution")
    native = r["native_runtime"]
    library_hashes = {name.rsplit("/", 1)[-1]: expected for name, expected in BASE_FILES.items() if name.endswith(".dll")}
    need(isinstance(native, dict) and all(native.get(n) == h for n, h in library_hashes.items()) and
         all(SHA.fullmatch(v) and (k in library_hashes or re.fullmatch(r"[_a-z0-9]+\.pyd", k))
             for k, v in native.items()), "unreviewed native runtime evidence")
    return r


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_subparsers(dest="action", required=True)
    build = action.add_parser("build")
    build.add_argument("--python", type=Path, required=True, help="audited isolated .venv/Scripts/python.exe")
    build.add_argument("--out-dir", type=Path, required=True, help="new artifact directory")
    worker = action.add_parser("_worker", help=argparse.SUPPRESS)
    worker.add_argument("output", type=Path)
    worker.add_argument("work", type=Path)
    worker.add_argument("commit")
    check = action.add_parser("verify")
    check.add_argument("--engine", type=Path, required=True)
    check.add_argument("--receipt", type=Path, required=True)
    check.add_argument("--notices", type=Path, required=True)
    check.add_argument("--source-commit", required=True)
    args = parser.parse_args()
    try:
        if args.action == "verify":
            facts = verify(args.engine, args.receipt, args.notices, args.source_commit)
            print(json.dumps({"engine_sha256": facts["engine_sha256"], "source_commit": facts["source_commit"]}))
        elif args.action == "_worker":
            build_worker(args.output, args.work, args.commit)
        else:
            exe = args.python.resolve()
            git = lambda *a: subprocess.check_output(["git", *a], cwd=ROOT, text=True).strip()
            need(not git("status", "--porcelain", "--untracked-files=no"), "commit tracked changes before freezing")
            commit = git("rev-parse", "HEAD")
            result = args.out_dir.resolve()
            need(not result.exists(), "use a new artifact directory; refuse to overwrite evidence")
            need(exe.name.lower() == "python.exe" and exe.parent.name.lower() == "scripts" and
                 (exe.parent.parent / "pyvenv.cfg").is_file(), "exact Windows venv python executable required")
            result.mkdir(parents=True)
            work = result / "_engine-build"
            work.mkdir()
            # Query only the interpreter base under isolated Python mode; build
            # child starts with its own environment stripped of ambient tools.
            base = Path(subprocess.check_output([str(exe), "-I", "-c", "import sys; print(sys.base_prefix)"],
                                                 text=True).strip()).resolve()
            subprocess.run([str(exe), "-I", str(Path(__file__).resolve()), "_worker", str(result), str(work), commit],
                           cwd=ROOT, env=child_env(base, os.environ, work), check=True)
            verify(result / ENGINE, result / RECEIPT, result / NOTICES, commit)
            print(f"Windows engine proof ready: {commit}")
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"Windows engine proof failed: {exc}\n")


if __name__ == "__main__":
    main()
