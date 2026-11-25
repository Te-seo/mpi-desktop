# MPI Desktop 2026.11.24-1 release source

The Windows QR release was built from the original source commit
`315e8bc7994b1a32d9ba48ce395239c762c221fb` (Git tree
`cc60d6d51346e029c96638bd7361855f3570a07c`) with Electron 43.8.12.

- [Exact corresponding source ZIP](https://dl.mpinspect.com/mpi-source-2026.11.24-1-core-1e3f384204efb519fac800d1629f27efdf07c33af357098b69c3b731484d4de4.zip),
  SHA-256 `1e3f384204efb519fac800d1629f27efdf07c33af357098b69c3b731484d4de4`.
- [Windows installer](https://dl.mpinspect.com/MPI-Setup-2026.11.24-1.exe),
  SHA-256 `9e19e5ccca95eec01fec0ffab374ee6cd4706fddbc0ad69e7d93e67e9a0e94ef`.

The ZIP is the original release source distribution. This repository was
seeded from its verified 283 files and includes documentation and
standalone build support changes. A new repository commit is not the
original seller build provenance. The source does not redistribute the
Android Play app, SDK binaries, installer, or local credentials.
# Public source exclusions

Only Desktop source, build instructions, and required license notices belong in
this public repository. Environment files, private keys, installers, captures,
reports, and generated files should stay local. Store release binaries outside
the Git source history.

Run `python scripts/check-public-files.py` to detect excluded files already in the
Git index. Enable the local staged-file guard on each clone with
`git config --local core.hooksPath .githooks`; if the clone already uses a custom
hooks path, integrate the check into the existing hook instead. GitHub Actions
runs the check on pushes and PRs. Neither this check nor `.gitignore` replaces
reviewing staged changes for sensitive values in otherwise allowed source files.
