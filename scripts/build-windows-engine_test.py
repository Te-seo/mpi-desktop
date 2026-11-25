import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("windows_engine", Path(__file__).with_name("build-windows-engine.py"))
engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine)


class WindowsEngineBuildTest(unittest.TestCase):
    def test_sanitized_child_loses_tool_and_python_injections(self):
        root = Path.cwd()
        system = root / "win"
        environment = engine.child_env(root / "python", {
            "SystemRoot": str(system), "PATH": str(root / "untrusted-tool"),
            "PYTHONPATH": "injection", "PYTHONHOME": "injection", "UPX": "injection",
            "USERPROFILE": str(root),
        }, root)
        self.assertEqual(environment["PATH"].split(os.pathsep),
                         [str(system / "System32"), str(system), str(root / "python")])
        for key in ("PYTHONPATH", "PYTHONHOME", "UPX"):
            self.assertNotIn(key, environment)

    def test_ambient_and_system_libraries_are_rejected(self):
        root = Path.cwd()
        for name, origin, error in (
            ("hello.dll", root.parent / "ambient" / "hello.dll", "external origin"),
            ("api-ms-win-core-fake.dll", root / "python" / "api-ms-win-core-fake.dll", "system DLL"),
            ("intruder.pyd", root / "generated" / "intruder.pyd", "unattributed checkout"),
        ):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, error):
                engine.inspect_sources([(name, str(origin), "BINARY")], root, root / "python",
                                       root / "venv", root / "generated", [])

    def test_archive_inventory_checks_both_layers(self):
        pkg = [("PYZ-00.pyz", "/x", "PYZ")]
        pyz = [("mpi.api", "/src/mpi/api.py", "PYMODULE")]
        self.assertEqual(engine.reconcile({"PYZ.pyz": "index"}, {"mpi.api": "index"}, pkg, pyz), pkg + pyz)
        with self.assertRaisesRegex(ValueError, "CArchive"):
            engine.reconcile({"PYZ.pyz": "", "tampered.dll": ""}, {"mpi.api": ""}, pkg, pyz)
        with self.assertRaisesRegex(ValueError, "PYZ"):
            engine.reconcile({"PYZ.pyz": ""}, {"unexpected": ""}, pkg, pyz)

    def test_receipt_is_bound_to_source_artifact_and_notice_hashes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            binary, notices, receipt = (root / n for n in (engine.ENGINE, engine.NOTICES, engine.RECEIPT))
            binary.write_bytes(b"MZ unsigned fixture")
            notices.write_text("notice fixture", encoding="utf-8")
            expected = "a" * 40
            body = {
                "schema": 1, "source_commit": expected, "platform": "windows-x64-win10-win11",
                "python_version": engine.PYTHON_VERSION, "python_base_input_sha256": engine.BASE_FILES,
                "packaging_tools": engine.TOOL_VERSIONS, "bootloader_sha256": engine.BOOTLOADER_SHA,
                "engine_sha256": engine.file_sha(binary), "notices_sha256": engine.file_sha(notices),
                "carchive_entries": 1, "pyz_entries": 1,
                "runtime_distributions": [{"name": name, "version": version, "license": license_,
                                           "archive_member_count": 1, "license_files": [{"sha256":"a"*64}]}
                                          for name, (version, license_) in engine.RUNTIME.items()],
                "native_runtime": {name.rsplit("/", 1)[-1]: sha for name, sha in engine.BASE_FILES.items()
                                   if name.endswith(".dll")},
            }
            receipt.write_text(json.dumps(body), encoding="utf-8")
            self.assertEqual(engine.verify(binary, receipt, notices, expected), body)
            with self.assertRaisesRegex(ValueError, "wrong engine receipt source"):
                engine.verify(binary, receipt, notices, "b"*40)
            notices.write_bytes(b"modified")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                engine.verify(binary, receipt, notices, expected)


if __name__ == "__main__":
    unittest.main()
