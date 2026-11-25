# Auditable Windows engine freeze

The Windows installer uses a Python sidecar, distinct from the Play Store app
and from the Electron renderer. Build the sidecar on a Windows x64 runner from
the exact clean source commit with an isolated release virtual environment.
The current proof lane consumes Windows 10/11 x64, CPython 3.13.3, PyInstaller
6.21.0 and its 2026.6 hook bundle. It does not sign or publish artifacts.

    python scripts/build-windows-engine.py build --python .venv/Scripts/python.exe --out-dir dist-release-2026.11.24-1

Pass the exact output triplet to installer validation without rewriting it:

    python scripts/build-windows-engine.py verify --engine dist-release-2026.11.24-1/data-engine.exe --receipt dist-release-2026.11.24-1/engine-runtime-receipt.json --notices dist-release-2026.11.24-1/ENGINE-RUNTIME-NOTICES.txt --source-commit <full-source-commit>

The listing is derived from both layers of the actual frozen executable, then
matched to PyInstaller's source tables and the isolated wheel owners. It is
not a listing of everything installed in the environment. Known publisher
notices are recovered from the packages and CPython installation, including
their native-library terms. Preserve the complete notice file in any customer
installer. The receipt binds the exact new binary, source and notices; it is a
build record, not a signature or legal authorization. Only accept receipts
from the controlled release build.

## Windows provenance and supported configuration

The builder supplies child-only search paths limited to Windows and the pinned
Python installation. It fails on attempted inclusion of ambient DLLs or the
UCRT/API-set files. Microsoft documents that on Windows 10 and 11 the Universal
CRT is part of the operating system and the system copy is always used even
if an application includes an app-local copy. This intentionally does not
promise support for older Windows versions.
See [Universal CRT deployment](https://learn.microsoft.com/en-us/cpp/windows/universal-crt-deployment).

The frozen CPython files come from the PSF Windows distribution, audited
against [Python 3.13.3](https://www.python.org/downloads/release/python-3133/);
interpreter/DLLs were Authenticode valid, and the two included VC Runtime DLLs
were Microsoft-signed. CPython's complete license notice includes its
incorporated libraries and Microsoft distributable-code limitations; keep those
terms distinct. Verify the exact binary suppliers, distribution basis and
maintenance/security status before a public release. This version is a
reproduction pin, not a claim it remains the latest supported security patch.
Updating Python or a frozen package requires renewed version/hash and notices
review, not a switch that turns the check off.

PyInstaller's packaged bootloader is pinned to the exact Windows wheel from
[PyPI PyInstaller 6.21.0](https://pypi.org/project/pyinstaller/6.21.0/), wheel
SHA-256 7fae06c494ce0ebfe6bd3055c0e409def884f63af2e3705d06bd431ad9237fc7,
and the build-time hooks are pinned to 2026.6. PyInstaller's [bundling exception](https://www.pyinstaller.org/en/stable/license.html)
applies to its incorporated runtime pieces; it does not waive any licenses
for the application dependencies. Hook tooling and uncollected modules are
not declared as physically shipped.

Unit checks: python scripts/build-windows-engine_test.py.
