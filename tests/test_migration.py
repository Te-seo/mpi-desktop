from __future__ import annotations

from pathlib import Path

import pytest

from mpi import migration


def test_migrate_directory_uses_atomic_rename_for_a_clean_tree(tmp_path: Path) -> None:
    legacy = tmp_path / "PerfInspect"
    target = tmp_path / "MPI"
    (legacy / "nested").mkdir(parents=True)
    (legacy / "nested" / "session.csv").write_bytes(b"sample")

    result = migration.migrate_directory(legacy, target)

    assert result["complete"] is True
    assert result["mode"] == "atomic"
    assert not legacy.exists()
    assert (target / "nested" / "session.csv").read_bytes() == b"sample"


def test_migrate_directory_deduplicates_and_uses_deterministic_collision_names(
    tmp_path: Path,
) -> None:
    legacy = tmp_path / "PerfInspect"
    target = tmp_path / "MPI"
    legacy.mkdir()
    target.mkdir()
    (target / "same.csv").write_bytes(b"same")
    (legacy / "same.csv").write_bytes(b"same")
    (target / "different.csv").write_bytes(b"current")
    (target / "different-legacy-1.csv").write_bytes(b"older legacy")
    (legacy / "different.csv").write_bytes(b"incoming")

    result = migration.migrate_directory(legacy, target)

    assert result["complete"] is True
    assert not legacy.exists()
    assert (target / "same.csv").read_bytes() == b"same"
    assert (target / "different.csv").read_bytes() == b"current"
    assert (target / "different-legacy-1.csv").read_bytes() == b"older legacy"
    assert (target / "different-legacy-2.csv").read_bytes() == b"incoming"
    assert len(result["deduplicated"]) == 1


def test_partial_failure_keeps_both_roots_and_resumes_on_the_next_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = tmp_path / "PerfInspect"
    target = tmp_path / "MPI"
    legacy.mkdir()
    target.mkdir()
    locked = legacy / "locked.csv"
    unlocked = legacy / "unlocked.csv"
    locked.write_bytes(b"locked")
    unlocked.write_bytes(b"unlocked")
    original_copy2 = migration.shutil.copy2

    def fail_locked(source: Path, destination: Path) -> None:
        if Path(source).read_bytes() == b"locked":
            raise PermissionError("file is locked")
        original_copy2(source, destination)

    monkeypatch.setattr(migration.shutil, "copy2", fail_locked)
    first = migration.migrate_directory(legacy, target)

    assert first["complete"] is False
    assert locked.exists()
    assert (target / "unlocked.csv").read_bytes() == b"unlocked"
    assert first["errors"]

    monkeypatch.setattr(migration.shutil, "copy2", original_copy2)
    second = migration.migrate_directory(legacy, target)

    assert second["complete"] is True
    assert not legacy.exists()
    assert (target / "locked.csv").read_bytes() == b"locked"


def test_concurrent_current_file_wins_without_being_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = tmp_path / "PerfInspect"
    target = tmp_path / "MPI"
    legacy.mkdir()
    target.mkdir()
    source = legacy / "session.csv"
    destination = target / "session.csv"
    source.write_bytes(b"legacy")
    original_copy2 = migration.shutil.copy2
    raced = False

    def create_current_during_copy(source_path: Path, temporary: Path) -> None:
        nonlocal raced
        original_copy2(source_path, temporary)
        if not raced:
            destination.write_bytes(b"current")
            raced = True

    monkeypatch.setattr(migration.shutil, "copy2", create_current_during_copy)

    result = migration.migrate_directory(legacy, target)

    assert result["complete"] is True
    assert destination.read_bytes() == b"current"
    assert (target / "session-legacy-1.csv").read_bytes() == b"legacy"


def test_resume_cleans_nested_interrupted_temporaries(tmp_path: Path) -> None:
    legacy = tmp_path / "PerfInspect"
    target = tmp_path / "MPI"
    (legacy / "nested").mkdir(parents=True)
    (target / "nested").mkdir(parents=True)
    (legacy / "nested" / "session.csv").write_bytes(b"session")
    stale = target / "nested" / ".mpi-migrate-interrupted.tmp"
    stale.write_bytes(b"partial")

    result = migration.migrate_directory(legacy, target)

    assert result["complete"] is True
    assert not stale.exists()
    assert (target / "nested" / "session.csv").read_bytes() == b"session"


def test_target_parent_reparse_race_keeps_the_legacy_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = tmp_path / "PerfInspect"
    target = tmp_path / "MPI"
    (legacy / "nested").mkdir(parents=True)
    target.mkdir()
    source = legacy / "nested" / "session.csv"
    source.write_bytes(b"legacy")
    original_check = migration._destination_has_reparse
    checks = 0

    def become_reparse(root: Path, parent: Path) -> bool:
        nonlocal checks
        if Path(root) == target:
            checks += 1
            return checks >= 2
        return original_check(root, parent)

    monkeypatch.setattr(migration, "_destination_has_reparse", become_reparse)

    result = migration.migrate_directory(legacy, target)

    assert result["complete"] is False
    assert source.read_bytes() == b"legacy"
    assert not (target / "nested" / "session.csv").exists()
    assert any("reparse point" in item["error"] for item in result["errors"])


def test_writer_recreating_original_name_during_publish_is_not_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = tmp_path / "PerfInspect"
    target = tmp_path / "MPI"
    legacy.mkdir()
    target.mkdir()
    source = legacy / "session.csv"
    source.write_bytes(b"first")
    original_publish = migration._publish_without_replace

    def publish_then_write(temporary: Path, destination: Path) -> None:
        original_publish(temporary, destination)
        source.write_bytes(b"second")

    monkeypatch.setattr(migration, "_publish_without_replace", publish_then_write)

    first = migration.migrate_directory(legacy, target)

    assert first["complete"] is False
    assert (target / "session.csv").read_bytes() == b"first"
    assert source.read_bytes() == b"second"

    monkeypatch.setattr(migration, "_publish_without_replace", original_publish)
    second = migration.migrate_directory(legacy, target)

    assert second["complete"] is True
    assert (target / "session.csv").read_bytes() == b"first"
    assert (target / "session-legacy-1.csv").read_bytes() == b"second"


def test_current_target_write_after_publish_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = tmp_path / "PerfInspect"
    target = tmp_path / "MPI"
    legacy.mkdir()
    target.mkdir()
    source = legacy / "session.csv"
    destination = target / "session.csv"
    source.write_bytes(b"legacy")
    original_publish = migration._publish_without_replace

    def publish_then_write_current(temporary: Path, published: Path) -> None:
        original_publish(temporary, published)
        published.write_bytes(b"current")

    monkeypatch.setattr(
        migration,
        "_publish_without_replace",
        publish_then_write_current,
    )

    result = migration.migrate_directory(legacy, target)

    assert result["complete"] is False
    assert destination.read_bytes() == b"current"
    assert source.read_bytes() == b"legacy"
    assert any("hash mismatch" in item["error"] for item in result["errors"])


def test_final_target_hash_is_write_locked_until_claim_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = tmp_path / "PerfInspect"
    target = tmp_path / "MPI"
    legacy.mkdir()
    target.mkdir()
    (legacy / "session.csv").write_bytes(b"legacy")
    destination = target / "session.csv"
    original_stream_hash = migration._stream_sha256
    hashes = 0
    blocked = False

    def hash_then_try_current_write(handle) -> str:
        nonlocal hashes, blocked
        digest = original_stream_hash(handle)
        hashes += 1
        if hashes == 2:
            try:
                destination.write_bytes(b"current")
            except PermissionError:
                blocked = True
        return digest

    monkeypatch.setattr(migration, "_stream_sha256", hash_then_try_current_write)

    result = migration.migrate_directory(legacy, target)

    assert blocked is True
    assert result["complete"] is True
    assert result["errors"] == []
    assert destination.read_bytes() == b"legacy"
    assert not legacy.exists()


def test_open_writer_changing_claimed_inode_keeps_it_for_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = tmp_path / "PerfInspect"
    target = tmp_path / "MPI"
    legacy.mkdir()
    target.mkdir()
    (legacy / "session.csv").write_bytes(b"first")
    original_publish = migration._publish_without_replace

    def publish_then_change_claim(temporary: Path, destination: Path) -> None:
        original_publish(temporary, destination)
        claimed = next(legacy.glob(".mpi-migration-claim-*.data"))
        claimed.write_bytes(b"second")

    monkeypatch.setattr(
        migration,
        "_publish_without_replace",
        publish_then_change_claim,
    )

    first = migration.migrate_directory(legacy, target)

    assert first["complete"] is False
    assert any("changed during migration" in item["error"] for item in first["errors"])
    assert (legacy / "session.csv").read_bytes() == b"second"

    monkeypatch.setattr(migration, "_publish_without_replace", original_publish)
    second = migration.migrate_directory(legacy, target)

    assert second["complete"] is True
    assert (target / "session.csv").read_bytes() == b"first"
    assert (target / "session-legacy-1.csv").read_bytes() == b"second"


def test_source_parent_reparse_race_is_rejected_before_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = tmp_path / "PerfInspect"
    target = tmp_path / "MPI"
    (legacy / "nested").mkdir(parents=True)
    target.mkdir()
    source = legacy / "nested" / "session.csv"
    source.write_bytes(b"outside")
    original_check = migration._destination_has_reparse
    source_checks = 0

    def swap_source_parent(root: Path, parent: Path) -> bool:
        nonlocal source_checks
        if Path(root) == legacy:
            source_checks += 1
            return source_checks >= 2
        return original_check(root, parent)

    monkeypatch.setattr(
        migration,
        "_destination_has_reparse",
        swap_source_parent,
    )

    result = migration.migrate_directory(legacy, target)

    assert result["complete"] is False
    assert source.read_bytes() == b"outside"
    assert not (target / "nested" / "session.csv").exists()
    assert result["errors"]


def test_escaped_published_hardlink_is_removed_and_source_retained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = tmp_path / "PerfInspect"
    target = tmp_path / "MPI"
    legacy.mkdir()
    target.mkdir()
    (legacy / "session.csv").write_bytes(b"legacy")
    original_resolved = migration._resolved_within

    def target_escapes_after_publish(root: Path, candidate: Path) -> bool:
        if (
            Path(root) == target
            and Path(candidate).name == "session.csv"
            and Path(candidate).exists()
        ):
            return False
        return original_resolved(root, candidate)

    monkeypatch.setattr(migration, "_resolved_within", target_escapes_after_publish)

    result = migration.migrate_directory(legacy, target)

    assert result["complete"] is False
    assert not (target / "session.csv").exists()
    assert (legacy / "session.csv").read_bytes() == b"legacy"
    assert any("escaped the MPI root" in item["error"] for item in result["errors"])


def test_missing_legacy_root_still_cleans_interrupted_target_temp(
    tmp_path: Path,
) -> None:
    legacy = tmp_path / "PerfInspect"
    target = tmp_path / "MPI"
    nested = target / "nested"
    nested.mkdir(parents=True)
    stale = nested / ".mpi-migrate-interrupted.tmp"
    stale.write_bytes(b"partial")

    result = migration.migrate_directory(legacy, target)

    assert result["complete"] is True
    assert not stale.exists()


def test_reparse_points_are_never_moved_or_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = tmp_path / "PerfInspect"
    target = tmp_path / "MPI"
    legacy.mkdir()
    target.mkdir()
    normal = legacy / "normal.csv"
    skipped = legacy / "link.csv"
    normal.write_bytes(b"normal")
    skipped.write_bytes(b"outside")
    original_is_reparse = migration._is_reparse

    monkeypatch.setattr(
        migration,
        "_is_reparse",
        lambda path: Path(path) == skipped or original_is_reparse(Path(path)),
    )
    result = migration.migrate_directory(legacy, target)

    assert result["complete"] is False
    assert skipped.read_bytes() == b"outside"
    assert not (target / "link.csv").exists()
    assert (target / "normal.csv").read_bytes() == b"normal"
    assert any(item["path"] == str(skipped) for item in result["errors"])
