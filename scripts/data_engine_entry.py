"""PyInstaller entry script for the data-engine sidecar.

Wraps ``mpi.api.main`` so PyInstaller's bootloader runs the API as a
package member (with a real ``__package__``) rather than as a stray script,
which would break the relative imports inside ``mpi.api``.

Also swallows OSError / BrokenPipeError at the very top level so the windowed
PyInstaller bootloader never pops a "Failed to execute script" dialog when
Electron tears the sidecar down mid-write.
"""

from __future__ import annotations

import sys

from mpi.api import main


def _safe_main() -> int:
    try:
        return main()
    except (OSError, BrokenPipeError, KeyboardInterrupt):
        return 0
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 0


if __name__ == "__main__":
    sys.exit(_safe_main())
