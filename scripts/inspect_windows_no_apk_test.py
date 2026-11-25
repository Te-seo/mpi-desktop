import io
import json
import struct
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from inspect_windows_no_apk import inspect_directory, inspect_stream


def archive(files):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for name, contents in files.items():
            bundle.writestr(name, contents)
    return data.getvalue()


def asar(name, content):
    obj = json.dumps({"files": {name: {"size": len(content), "offset": "0"}}}).encode()
    raw_header = struct.pack("<I", len(obj)) + obj
    header = struct.pack("<I", len(raw_header)) + raw_header
    return struct.pack("<II", 4, len(header)) + header + content


@pytest.mark.parametrize("payload,path", [
    (b"unrelated", "resources/companion.apk"),
    (b"file", "resources/NESTED.AAB"),
    (archive({"AndroidManifest.xml": b"manifest"}), "resources/icon.blob"),
    (archive({"base/manifest/AndroidManifest.xml": b"manifest"}), "resources/database.bin"),
    (archive({"images/cache": archive({"AndroidManifest.xml": b"manifest"})}), "resources/cache.dat"),
    (asar("image.jpg", archive({"AndroidManifest.xml": b"manifest"})), "resources/app.asar"),
    (b"MZ" + archive({"AndroidManifest.xml": b"manifest"}) + b"x" * 70000, "resource.exe"),
], ids=["named-apk","named-aab","renamed-apk","renamed-aab","nested-zip","nested-asar","appended-pe"])
def test_rejects_named_renamed_and_embedded_android_package(payload, path):
    with pytest.raises(ValueError, match="Android"):
        inspect_stream(io.BytesIO(payload), path)


def test_valid_inputs_do_not_confuse_text_reference_and_archive(tmp_path):
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "app.asar").write_bytes(asar("instructions.txt", b"Install companion.apk via Play"))
    (resources / "license.txt").write_bytes(b"fixture")
    (resources / "data-engine.exe").write_bytes(b"MZ-demo")
    inspect_directory(tmp_path)
    (resources / "payload.dat").write_bytes(archive({"AndroidManifest.xml": b"manifest"}))
    with pytest.raises(ValueError, match="hidden"):
        inspect_directory(tmp_path)


def test_unsafe_archive_member_and_opaque_container_fail():
    with pytest.raises(ValueError, match="unsafe"):
        inspect_stream(io.BytesIO(archive({"../resource.txt": b"test"})), "packed.bin")
    with pytest.raises(ValueError, match="opaque"):
        inspect_stream(io.BytesIO(b"7z\xbc\xaf\x27\x1cgarbled"), "packed.bin")
