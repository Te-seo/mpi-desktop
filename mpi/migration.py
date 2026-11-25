"""One-time, restart-safe migration helpers for the MPI namespace change."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO, Dict, Iterable, Iterator, List


LEGACY_PRODUCT_DIR = "PerfInspect"
PRODUCT_DIR = "MPI"
LEGACY_CACHE_DIR = "perfinspect"
CACHE_DIR = "mpi"
_JOURNAL_NAME = ".mpi-migration-journal.json"
_CLAIM_PREFIX = ".mpi-migration-claim-"
_migration_lock = threading.RLock()


def _is_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(getattr(info, "st_file_attributes", 0) & reparse_flag)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        _update_digest(digest, handle)
    return digest.hexdigest()


def _update_digest(digest: Any, handle: BinaryIO) -> None:
    handle.seek(0)
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)


def _stream_sha256(handle: BinaryIO) -> str:
    digest = hashlib.sha256()
    _update_digest(digest, handle)
    return digest.hexdigest()


@contextmanager
def _write_excluding_reader(
    path: Path,
    *,
    delete_access: bool = False,
) -> Iterator[tuple[BinaryIO, int | None]]:
    """Open a stable read handle that rejects concurrent writers on Windows."""

    if os.name != "nt":
        with path.open("rb") as handle:
            yield handle, None
        return

    import ctypes
    import msvcrt
    from ctypes import wintypes

    create_file = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    desired_access = 0x80000000  # GENERIC_READ
    if delete_access:
        desired_access |= 0x00010000  # DELETE
    share_mode = 0x00000001  # FILE_SHARE_READ; exclude concurrent writes/deletes
    open_existing = 3
    open_reparse_point = 0x00200000
    raw_handle = create_file(
        str(path),
        desired_access,
        share_mode,
        None,
        open_existing,
        open_reparse_point,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if raw_handle == invalid_handle:
        error = ctypes.get_last_error()
        raise OSError(error, f"exclusive migration read failed: {path}")
    try:
        descriptor = msvcrt.open_osfhandle(raw_handle, os.O_RDONLY)
    except BaseException:
        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(raw_handle)
        raise
    with os.fdopen(descriptor, "rb") as handle:
        yield handle, int(raw_handle)


def _delete_locked_file(path: Path, raw_handle: int | None) -> None:
    if raw_handle is None:
        path.unlink()
        return

    import ctypes
    from ctypes import wintypes

    class FileDispositionInfo(ctypes.Structure):
        _fields_ = [("DeleteFile", wintypes.BOOL)]

    set_information = ctypes.WinDLL(
        "kernel32", use_last_error=True
    ).SetFileInformationByHandle
    set_information.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    set_information.restype = wintypes.BOOL
    disposition = FileDispositionInfo(True)
    if not set_information(
        raw_handle,
        4,  # FileDispositionInfo
        ctypes.byref(disposition),
        ctypes.sizeof(disposition),
    ):
        error = ctypes.get_last_error()
        raise OSError(error, f"locked migration delete failed: {path}")


def _write_journal(root: Path, state: Dict[str, Any]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    target = root / _JOURNAL_NAME
    temporary = root / f"{_JOURNAL_NAME}.{uuid.uuid4().hex}.tmp"
    payload = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True)
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)


def _try_write_journal(root: Path, state: Dict[str, Any]) -> None:
    try:
        _write_journal(root, state)
    except OSError as exc:
        state["errors"].append(
            {"path": str(root / _JOURNAL_NAME), "error": f"journal write failed: {exc}"}
        )


def _iter_source_files(root: Path, errors: List[Dict[str, str]]) -> Iterable[Path]:
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        safe_dirs: List[str] = []
        for name in sorted(dirs):
            candidate = current_path / name
            if _is_reparse(candidate):
                errors.append(
                    {"path": str(candidate), "error": "reparse point skipped"}
                )
            else:
                safe_dirs.append(name)
        dirs[:] = safe_dirs
        for name in sorted(files):
            candidate = current_path / name
            if _is_reparse(candidate):
                errors.append(
                    {"path": str(candidate), "error": "reparse point skipped"}
                )
                continue
            if candidate.is_file():
                yield candidate


def _tree_contains_reparse(root: Path) -> bool:
    errors: List[Dict[str, str]] = []
    list(_iter_source_files(root, errors))
    return bool(errors)


def _tree_contains_claim(root: Path) -> bool:
    errors: List[Dict[str, str]] = []
    return any(_is_claim_artifact(path) for path in _iter_source_files(root, errors))


def _destination_has_reparse(root: Path, parent: Path) -> bool:
    try:
        relative = parent.relative_to(root)
    except ValueError:
        return True
    current = root
    if _is_reparse(current):
        return True
    for part in relative.parts:
        current = current / part
        if current.exists() and _is_reparse(current):
            return True
    return False


def _collision_target(destination: Path, source_hash: str) -> tuple[Path, bool]:
    """Return ``(target, already_present)`` with deterministic legacy suffixes."""
    if not destination.exists():
        return destination, False
    if destination.is_file() and not _is_reparse(destination):
        try:
            if _sha256(destination) == source_hash:
                return destination, True
        except OSError:
            pass

    index = 1
    while True:
        candidate = destination.with_name(
            f"{destination.stem}-legacy-{index}{destination.suffix}"
        )
        if not candidate.exists():
            return candidate, False
        if candidate.is_file() and not _is_reparse(candidate):
            try:
                if _sha256(candidate) == source_hash:
                    return candidate, True
            except OSError:
                pass
        index += 1


def _resolved_within(root: Path, candidate: Path) -> bool:
    """Return true only while ``candidate`` still resolves below ``root``."""
    try:
        resolved_root = root.resolve(strict=True)
        resolved_candidate = candidate.resolve(strict=True)
        resolved_candidate.relative_to(resolved_root)
    except (OSError, ValueError):
        return False
    return True


def _publish_without_replace(temporary: Path, target: Path) -> None:
    """Atomically publish ``temporary`` while refusing to replace ``target``.

    The temporary lives beside the destination, so a hard link is a same-volume
    atomic create. Unlike ``os.replace``, a concurrently created current MPI
    file wins with ``FileExistsError`` and is never overwritten.
    """
    try:
        os.link(temporary, target, follow_symlinks=False)
    except FileExistsError:
        raise
    except OSError as exc:
        raise OSError(f"atomic no-replace publish failed: {exc}") from exc


def _is_claim_artifact(path: Path) -> bool:
    return path.name.startswith(_CLAIM_PREFIX) and path.suffix in {".json", ".data"}


def _write_claim_metadata(path: Path, relative: Path) -> None:
    payload = json.dumps(
        {"relative": relative.as_posix()},
        ensure_ascii=False,
        sort_keys=True,
    )
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _claim_source(
    legacy_root: Path,
    source: Path,
    relative: Path,
) -> tuple[Path, Path]:
    """Atomically detach a source name before copying it.

    A capture process that writes the original name after this rename creates a
    new file that this migration never deletes. The JSON sibling makes a claim
    restart-safe if the process exits after the rename.
    """

    if _is_reparse(source) or not _resolved_within(legacy_root, source):
        raise OSError("source path escaped the legacy root")
    if _destination_has_reparse(legacy_root, source.parent):
        raise OSError("source path contains a reparse point")

    claim_id = uuid.uuid4().hex
    metadata = source.parent / f"{_CLAIM_PREFIX}{claim_id}.json"
    claimed = source.parent / f"{_CLAIM_PREFIX}{claim_id}.data"
    _write_claim_metadata(metadata, relative)
    renamed = False
    try:
        if (
            not _resolved_within(legacy_root, metadata)
            or _destination_has_reparse(legacy_root, source.parent)
        ):
            raise OSError("source parent escaped the legacy root")
        os.rename(source, claimed)
        renamed = True
        if (
            _is_reparse(claimed)
            or not _resolved_within(legacy_root, claimed)
            or _destination_has_reparse(legacy_root, claimed.parent)
        ):
            # The parent was swapped between validation and rename. Put the
            # outside file back without ever reading or deleting its contents.
            try:
                if not source.exists():
                    os.rename(claimed, source)
                    renamed = False
            except OSError:
                pass
            raise OSError("claimed source escaped the legacy root")
        return claimed, metadata
    except BaseException:
        if not renamed:
            try:
                metadata.unlink()
            except FileNotFoundError:
                pass
        raise


def _iter_pending_claims(
    legacy_root: Path,
    errors: List[Dict[str, str]],
) -> Iterable[tuple[Path, Path, Path]]:
    """Yield ``(claimed_data, original_relative, metadata)`` for resumptions."""

    files = list(_iter_source_files(legacy_root, errors))
    metadata_files = {
        path.with_suffix(".data"): path
        for path in files
        if path.name.startswith(_CLAIM_PREFIX) and path.suffix == ".json"
    }
    for data, metadata in sorted(
        metadata_files.items(), key=lambda item: str(item[0]).casefold()
    ):
        try:
            if (
                _is_reparse(metadata)
                or not _resolved_within(legacy_root, metadata)
                or _destination_has_reparse(legacy_root, metadata.parent)
            ):
                raise OSError("claim metadata escaped the legacy root")
            raw = json.loads(metadata.read_text(encoding="utf-8"))
            relative = Path(raw["relative"])
            if (
                relative.is_absolute()
                or not relative.parts
                or any(part in {"", ".", ".."} for part in relative.parts)
                or (legacy_root / relative).parent.absolute()
                != metadata.parent.absolute()
            ):
                raise ValueError("invalid claim relative path")
            if not data.exists():
                metadata.unlink()
                continue
            if (
                _is_reparse(data)
                or not _resolved_within(legacy_root, data)
                or _destination_has_reparse(legacy_root, data.parent)
            ):
                raise OSError("claimed source escaped the legacy root")
            yield data, relative, metadata
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            errors.append({"path": str(metadata), "error": f"invalid claim: {exc}"})

    for data in files:
        if (
            data.name.startswith(_CLAIM_PREFIX)
            and data.suffix == ".data"
            and data not in metadata_files
        ):
            errors.append(
                {"path": str(data), "error": "orphaned migration claim retained"}
            )


def _cleanup_interrupted_temporaries(
    target_root: Path,
    errors: List[Dict[str, str]],
) -> None:
    if not target_root.exists() or _is_reparse(target_root):
        return
    cleanup_errors: List[Dict[str, str]] = []
    for stale in _iter_source_files(target_root, cleanup_errors):
        if (
            stale.name.startswith(".mpi-migrate-")
            and stale.name.endswith(".tmp")
        ) or (
            stale.name.startswith(f"{_JOURNAL_NAME}.")
            and stale.name.endswith(".tmp")
        ):
            try:
                stale.unlink()
            except OSError as exc:
                errors.append({"path": str(stale), "error": f"temp cleanup failed: {exc}"})
    errors.extend(cleanup_errors)


def migrate_directory(legacy_root: Path, target_root: Path) -> Dict[str, Any]:
    """Move ``legacy_root`` into ``target_root`` without following links.

    A same-volume atomic rename is attempted only when the target is absent and
    the legacy tree contains no reparse points. Otherwise every file is copied
    to a temporary sibling, hash-verified, atomically published and only then
    removed from the legacy tree. A journal makes interrupted runs observable;
    the data operations themselves are idempotent and resume safely.
    """

    legacy_root = legacy_root.absolute()
    target_root = target_root.absolute()
    state: Dict[str, Any] = {
        "legacy": str(legacy_root),
        "target": str(target_root),
        "moved": [],
        "deduplicated": [],
        "errors": [],
        "complete": False,
        "mode": "none",
    }
    with _migration_lock:
        if target_root.exists() and _is_reparse(target_root):
            state["errors"].append(
                {"path": str(target_root), "error": "target root is a reparse point"}
            )
            return state
        _cleanup_interrupted_temporaries(target_root, state["errors"])
        if not legacy_root.exists():
            state["complete"] = True
            return state
        if _is_reparse(legacy_root):
            state["errors"].append(
                {"path": str(legacy_root), "error": "legacy root is a reparse point"}
            )
            return state
        if (
            not target_root.exists()
            and not _tree_contains_reparse(legacy_root)
            and not _tree_contains_claim(legacy_root)
        ):
            try:
                # Windows os.rename refuses an existing destination, unlike
                # replace semantics that could discard a concurrently-created
                # empty MPI directory.
                os.rename(legacy_root, target_root)
            except OSError as exc:
                state["errors"].append(
                    {"path": str(legacy_root), "error": f"atomic rename failed: {exc}"}
                )
            else:
                state["mode"] = "atomic"
                # A capture writer may recreate the legacy directory immediately
                # after the atomic rename. Keep both roots discoverable in that
                # case and let the next launch migrate the new files.
                state["complete"] = not legacy_root.exists()
                return state

        state["mode"] = "journaled"
        try:
            target_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            state["errors"].append(
                {"path": str(target_root), "error": f"target create failed: {exc}"}
            )
            return state
        source_errors: List[Dict[str, str]] = []
        pending_claims = list(_iter_pending_claims(legacy_root, source_errors))

        def process_claim(claimed: Path, relative: Path, metadata: Path) -> None:
            original_source = legacy_root / relative
            destination = target_root / relative
            entry = {
                "source": str(original_source),
                "relative": relative.as_posix(),
            }
            try:
                if (
                    _is_reparse(claimed)
                    or not _resolved_within(legacy_root, claimed)
                    or _destination_has_reparse(legacy_root, claimed.parent)
                ):
                    raise OSError("claimed source escaped the legacy root")
                if _destination_has_reparse(target_root, destination.parent):
                    raise OSError("target path contains a reparse point")
                destination.parent.mkdir(parents=True, exist_ok=True)
                if _destination_has_reparse(target_root, destination.parent):
                    raise OSError("target path contains a reparse point")
                source_hash = _sha256(claimed)
                target, already_present = _collision_target(
                    destination, source_hash
                )
                if already_present and not _resolved_within(target_root, target):
                    raise OSError("deduplicated target escaped the MPI root")
                outcome = "deduplicated" if already_present else ""
                if already_present:
                    pass
                else:
                    temporary = target.parent / f".mpi-migrate-{uuid.uuid4().hex}.tmp"
                    try:
                        shutil.copy2(claimed, temporary)
                        with temporary.open("rb+") as handle:
                            handle.flush()
                            os.fsync(handle.fileno())
                        if not _resolved_within(target_root, temporary):
                            raise OSError("temporary copy escaped the MPI root")
                        if _sha256(temporary) != source_hash:
                            raise OSError("temporary copy hash mismatch")
                        for _attempt in range(1024):
                            target, already_present = _collision_target(
                                destination, source_hash
                            )
                            if already_present:
                                if not _resolved_within(target_root, target):
                                    raise OSError(
                                        "deduplicated target escaped the MPI root"
                                    )
                                outcome = "deduplicated"
                                break
                            if _destination_has_reparse(
                                target_root, target.parent
                            ):
                                raise OSError("target path contains a reparse point")
                            try:
                                _publish_without_replace(temporary, target)
                            except FileExistsError:
                                # A current file won the race after collision
                                # selection. Recompute a deterministic suffix.
                                continue
                            if not _resolved_within(target_root, target):
                                try:
                                    if os.path.samefile(temporary, target):
                                        target.unlink()
                                except OSError:
                                    pass
                                raise OSError(
                                    "published target escaped the MPI root"
                                )
                            if _sha256(target) != source_hash:
                                raise OSError("published copy hash mismatch")
                            outcome = "moved"
                            break
                        else:
                            raise OSError("too many concurrent target collisions")
                    finally:
                        try:
                            temporary.unlink()
                        except FileNotFoundError:
                            pass

                # An already-open writer can still modify the claimed inode
                # after its original name was detached. Never delete it unless
                # the exact bytes that were copied/deduplicated are still there.
                if _is_reparse(claimed) or _is_reparse(target):
                    raise OSError("migration endpoint became a reparse point")
                with _write_excluding_reader(claimed, delete_access=True) as (
                    claimed_handle,
                    claimed_raw_handle,
                ), _write_excluding_reader(target) as (target_handle, _target_raw_handle):
                    if (
                        not _resolved_within(legacy_root, claimed)
                        or _stream_sha256(claimed_handle) != source_hash
                    ):
                        raise OSError("claimed source changed during migration")
                    if (
                        not _resolved_within(target_root, target)
                        or _stream_sha256(target_handle) != source_hash
                    ):
                        raise OSError("target changed before source removal")
                    # The claimed handle excludes FILE_SHARE_WRITE and is marked
                    # for deletion by handle while that exclusion is active.
                    # A writer can neither change the verified inode nor redirect
                    # the path between this check and deletion.
                    _delete_locked_file(claimed, claimed_raw_handle)
                try:
                    metadata.unlink()
                except FileNotFoundError:
                    pass
                record = {**entry, "target": str(target), "sha256": source_hash}
                if outcome == "moved":
                    state["moved"].append(record)
                elif outcome == "deduplicated":
                    state["deduplicated"].append(record)
                else:
                    raise OSError("migration produced no target")
            except OSError as exc:
                restore_error: OSError | None = None
                try:
                    if (
                        claimed.exists()
                        and not original_source.exists()
                        and _resolved_within(legacy_root, claimed)
                        and not _destination_has_reparse(
                            legacy_root, original_source.parent
                        )
                    ):
                        os.rename(claimed, original_source)
                        if _resolved_within(legacy_root, original_source):
                            try:
                                metadata.unlink()
                            except FileNotFoundError:
                                pass
                        else:
                            try:
                                os.rename(original_source, claimed)
                            except OSError:
                                pass
                            raise OSError(
                                "restored source escaped the legacy root"
                            )
                except OSError as claim_restore_exc:
                    restore_error = claim_restore_exc
                error = str(exc)
                if restore_error is not None:
                    error += f"; claim restore failed: {restore_error}"
                state["errors"].append({**entry, "error": error})
            _try_write_journal(target_root, state)

        for claimed, relative, metadata in pending_claims:
            process_claim(claimed, relative, metadata)

        for source in list(_iter_source_files(legacy_root, source_errors)):
            if _is_claim_artifact(source):
                continue
            relative = source.relative_to(legacy_root)
            entry = {"source": str(source), "relative": relative.as_posix()}
            try:
                claimed, metadata = _claim_source(legacy_root, source, relative)
            except OSError as exc:
                state["errors"].append({**entry, "error": str(exc)})
                _try_write_journal(target_root, state)
                continue
            process_claim(claimed, relative, metadata)

        state["errors"].extend(source_errors)
        for current, dirs, files in os.walk(legacy_root, topdown=False):
            path = Path(current)
            if _is_reparse(path):
                continue
            try:
                if not dirs and not files:
                    path.rmdir()
                elif path.exists() and not any(path.iterdir()):
                    path.rmdir()
            except OSError:
                pass

        state["complete"] = not legacy_root.exists()
        _try_write_journal(target_root, state)
        return state


def documents_root() -> Path:
    documents = Path.home() / "Documents"
    return documents if documents.is_dir() else Path.home()


def session_roots() -> tuple[Path, Path]:
    base = documents_root()
    return base / PRODUCT_DIR, base / LEGACY_PRODUCT_DIR


def migrate_session_storage() -> Dict[str, Any]:
    target, legacy = session_roots()
    return migrate_directory(legacy, target)


def cache_root() -> Path:
    base = Path.home() / ".cache"
    target = base / CACHE_DIR
    legacy = base / LEGACY_CACHE_DIR
    migrate_directory(legacy, target)
    return target
