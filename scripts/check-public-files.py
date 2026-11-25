"""Check the staged or committed public source against its Git exclusions."""

import argparse
import subprocess
import sys


def git(*args: str, data: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args], input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true", help="Check only added/changed staged paths")
    options = parser.parse_args()

    listing_args = (
        ("diff", "--cached", "--name-only", "--diff-filter=ACMRT", "-z")
        if options.staged
        else ("ls-files", "--cached", "-z")
    )
    paths = git(*listing_args)
    if paths.returncode:
        sys.stderr.buffer.write(paths.stderr)
        return 2
    if not paths.stdout:
        return 0

    ignored = git("check-ignore", "--no-index", "-z", "--stdin", data=paths.stdout)
    if ignored.returncode not in (0, 1):
        sys.stderr.buffer.write(ignored.stderr)
        return 2
    excluded = sorted(set(ignored.stdout.rstrip(b"\0").split(b"\0")) - {b""})
    if excluded:
        print("Excluded files are staged/tracked in the public source repository:", file=sys.stderr)
        for path in excluded:
            print(f"  {path.decode('utf-8', 'backslashreplace')!r}", file=sys.stderr)
        print("Unstage them without removing the local files. Do not force-add them.", file=sys.stderr)
        return 1
    print("Public source exclusion check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
