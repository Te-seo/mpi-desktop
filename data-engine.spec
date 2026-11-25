# PyInstaller spec for the mpi JSON-IPC backend.
#
# Build with: `pyinstaller data-engine.spec`
# Output:     `dist/data-engine.exe`  (single-file Windows binary)
#
# The Electron frontend (frontend/) bundles this exe via electron-builder's
# `extraResources` so the desktop app does not require Python at runtime.

block_cipher = None


a = Analysis(
    ['scripts/data_engine_entry.py'],
    pathex=['.'],
    binaries=[],
    datas=[
        ('mpi/play-memory-rules.json', 'mpi'),
        # The iOS metrics streamer runs as a *separate* process under a Python
        # interpreter that has pymobiledevice3 (never the frozen exe, which does
        # not bundle pymobiledevice3). Ship its source as a real on-disk file so
        # mpi.ios.pmd3 can launch it by path at runtime.
        ('mpi/ios/pmd3_stream.py', 'mpi/ios'),
        # Same story for the batch app-icon fetcher: it runs under the external
        # pymobiledevice3 interpreter, launched by file path, so ship its source.
        ('mpi/ios/pmd3_icons.py', 'mpi/ios'),
    ],
    hiddenimports=[
        # mpi dispatch graph: api.py imports the rest dynamically via
        # the package, so make every module explicit for PyInstaller.
        'mpi',
        'mpi.api',
        'mpi.analysis',
        'mpi.backend',
        'mpi.cli',
        'mpi.migration',
        'mpi.memory_qa',
        'mpi.report',
        'mpi.schema',
        'mpi.android',
        'mpi.android.adb',
        'mpi.android.axml',
        'mpi.android.backend',
        'mpi.android.parsers',
        'mpi.ios',
        'mpi.ios.backend',
        'mpi.ios.capture',
        'mpi.ios.parsers',
        'mpi.ios.pmd3',
        'mpi.ios.xcrun',
        'mpi.reports',
        'mpi.reports.adapter',
        'mpi.reports.excel_export',
        # Excel writer used by the live xlsx export (reports.excel_export)
        'openpyxl',
        'openpyxl.styles',
        'openpyxl.utils',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Keep the bundle small: tests + matplotlib backends we don't need.
        'matplotlib.tests',
        'pandas.tests',
        'numpy.tests',
        'tkinter',
        'PySide6',
        'pyqtgraph',
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='data-engine',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    # Electron spawns this with windowsHide=True, so the console subsystem keeps
    # stdin/stdout pipes usable without showing a separate console window.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
