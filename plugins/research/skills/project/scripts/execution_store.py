"""Execution store: publish_if_absent, directory barriers, tmp-reap, and grant registry.

publish_if_absent uses link(2) as the no-clobber primitive (§7.2).  The grant registry
uses atomic replacement under a directory lock (§10.4).  The two must not be confused:
journal records are immutable; use publish_if_absent.  Grants are mutable; use
GrantRegistry.scan_and_insert / update_grant under the registry lock.  publish_if_absent
must never be used to write a grant — a second 'issued' state would be a 'conflict' against
the first, which is exactly wrong for a field that must change in place.

Comparison keys arrive already resolved; this module never resolves symlinks, hashes
paths, or infers resources from command strings.
"""

import errno
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List

from execution_serialization import content_digest

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class StoreError(OSError):
    """Raised on I/O failures that leave an attempt INDETERMINATE (§7.5 D1 class)."""


class GrantRegistryError(OSError):
    """Raised when the grant registry is unavailable or the lock times out."""


# ---------------------------------------------------------------------------
# Directory barrier helpers
# ---------------------------------------------------------------------------


def fsync_dir(path: Path) -> None:
    """fsync the directory entry at path."""
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    except OSError as exc:
        _to_store_error(exc)
    finally:
        os.close(fd)


def _to_store_error(exc: OSError) -> None:
    """Re-raise ENOSPC / EIO / EROFS as StoreError; re-raise everything else unchanged."""
    if exc.errno in (errno.ENOSPC, errno.EIO, errno.EROFS):
        raise StoreError(exc.strerror or str(exc)) from exc
    raise exc


def _mkdir_synced(path: Path) -> None:
    """Create path and every missing ancestor, fsync each newly-created entry's parent."""
    to_create: List[Path] = []
    p = path
    while not p.exists():
        to_create.append(p)
        p = p.parent
    for d in reversed(to_create):
        try:
            d.mkdir(exist_ok=True)
        except OSError as exc:
            _to_store_error(exc)
        try:
            fsync_dir(d.parent)
        except OSError as exc:
            _to_store_error(exc)


# ---------------------------------------------------------------------------
# publish_if_absent (§7.2)
# ---------------------------------------------------------------------------


def publish_if_absent(final: Path, body: bytes, tmp_dir: Path) -> str:
    """Publish body at final using link(2) as the no-clobber primitive (§7.2).

    Returns one of the string literals 'created', 'identical', or 'conflict'.

    tmp_dir must reside on the same filesystem volume as final's parent so that
    link(2) can atomically move the file between them without a copy.  The caller
    establishes this at activation (config.same_volume); this function verifies it.

    Durability notes (§7.5):
    - 'created': the record is D1-durable — fd-fsynced, hard-linked, and
      the containing directory is fsynced.
    - 'identical': the winning record was already D1-durable (linked by a prior
      caller); this call issues the directory fsync it may have missed before
      returning, so the barrier is never skipped.
    - 'conflict': the losing bytes are discarded; the winning bytes stay intact.
      No barrier is issued for a conflict (no new record was made durable).

    StoreError is raised on ENOSPC, EIO, EROFS, or a short write; these leave
    the attempt INDETERMINATE rather than silently treating absence as failure.
    """
    # Step 1: refuse mismatched volumes.
    _check_same_volume(final, tmp_dir)

    # Step 2: create missing ancestors with per-entry barriers.
    _mkdir_synced(final.parent)
    _mkdir_synced(tmp_dir)

    # Step 3: write body to tmp; fsync the fd; close.
    tmp_path = _write_tmp(body, tmp_dir)

    # Step 4: link(tmp, final).
    outcome = "created"
    try:
        os.link(str(tmp_path), str(final))
    except OSError as exc:
        if exc.errno != errno.EEXIST:
            tmp_path.unlink(missing_ok=True)
            _to_store_error(exc)
        # EEXIST: compare content digests.
        try:
            existing = json.loads(final.read_bytes())
            incoming = json.loads(body)
        except (OSError, ValueError) as read_exc:
            tmp_path.unlink(missing_ok=True)
            raise StoreError(f"cannot read existing record at {final}") from read_exc
        if content_digest(existing) == content_digest(incoming):
            outcome = "identical"
        else:
            # Conflict: discard tmp; winning bytes stay untouched.
            tmp_path.unlink(missing_ok=True)
            return "conflict"

    # Steps 5-7: fsync final's dir; unlink tmp; fsync tmp's dir.
    # Both 'created' and 'identical' owe this barrier (§7.2 bullet 3).
    try:
        fsync_dir(final.parent)
    except OSError as exc:
        _to_store_error(exc)
    tmp_path.unlink(missing_ok=True)
    try:
        fsync_dir(tmp_dir)
    except OSError as exc:
        _to_store_error(exc)

    return outcome


def _check_same_volume(final: Path, tmp_dir: Path) -> None:
    # Walk up to the nearest existing ancestor for the stat when the parent does not yet exist.
    p = final.parent
    while not p.exists():
        p = p.parent
    try:
        dev_final = os.stat(str(p)).st_dev
        dev_tmp = os.stat(str(tmp_dir)).st_dev if tmp_dir.exists() else os.stat(str(tmp_dir.parent)).st_dev
    except OSError as exc:
        _to_store_error(exc)
    if dev_final != dev_tmp:
        raise StoreError(
            f"final ({final.parent}) and tmp_dir ({tmp_dir}) are on different volumes"
        )


def _write_tmp(body: bytes, tmp_dir: Path) -> Path:
    """Write body to a new temp file in tmp_dir; fsync; close; return the path."""
    tmp_dir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(suffix=".json", dir=str(tmp_dir))
    tmp = Path(name)
    try:
        buf = memoryview(body)
        offset = 0
        while offset < len(buf):
            try:
                n = os.write(fd, buf[offset:])
            except OSError as exc:
                _to_store_error(exc)
            if n == 0:
                raise StoreError("short write: 0 bytes written to tmp file")
            offset += n
        try:
            os.fsync(fd)
        except OSError as exc:
            _to_store_error(exc)
    except (OSError, StoreError):
        try:
            os.close(fd)
        except OSError:
            pass
        tmp.unlink(missing_ok=True)
        raise
    try:
        os.close(fd)
    except OSError:
        pass
    return tmp


# ---------------------------------------------------------------------------
# Tmp-file reap (§7.2, last paragraph)
# ---------------------------------------------------------------------------


def reap_tmp_files(tmp_dir: Path, max_age_seconds: float) -> int:
    """Remove stale .json tmp files from tmp_dir older than max_age_seconds.

    Called only at activation and takeover, never during normal operation.
    Returns the number of files removed.  Ignores errors on individual files so
    that one unremovable file does not block reaping of the others.
    """
    if not tmp_dir.exists():
        return 0
    cutoff = time.time() - max_age_seconds
    removed = 0
    try:
        entries = list(tmp_dir.iterdir())
    except OSError:
        return 0
    for entry in entries:
        if entry.suffix != ".json":
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink(missing_ok=True)
                removed += 1
        except OSError:
            pass
    return removed


# ---------------------------------------------------------------------------
# Mutable grant registry (§10.4)
# ---------------------------------------------------------------------------


class _RegistryLock:
    """Cross-process directory-based lock for the grant registry (§10.4 [C24])."""

    def __init__(self, path: Path, timeout: float) -> None:
        self._path = path
        self._timeout = timeout

    def __enter__(self) -> "_RegistryLock":
        deadline = time.monotonic() + self._timeout
        while True:
            try:
                self._path.mkdir(parents=True, exist_ok=False)
                return self
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise GrantRegistryError(
                        f"registry lock busy: {self._path}; "
                        "inspect the directory before removing a stale lock"
                    ) from None
                time.sleep(0.05)

    def __exit__(self, exc_type: object, exc_val: object, tb: object) -> None:
        try:
            self._path.rmdir()
        except OSError:
            pass


class GrantRegistry:
    """Mutable grant table at <workspace_root>/.execution-registry/ (§10.4).

    A grant records the live claims of one unreleased attempt.  It is the authority
    for invariant I1 (no two executors hold conflicting claims).

    Grants are mutable files written with _atomic_write_json (os.replace) under
    registry.lock — NOT with publish_if_absent.  The lock is what makes the
    sequence of capability states (none → issued → consumed) well-defined; immutable
    publication cannot express a field that changes in place.
    """

    def __init__(self, workspace_root: Path, lock_timeout: float = 5.0) -> None:
        self._root = workspace_root / ".execution-registry"
        self._grants_dir = self._root / "grants"
        self._lock_path = self._root / "registry.lock"
        self._lock_timeout = lock_timeout

    def _lock(self) -> _RegistryLock:
        return _RegistryLock(self._lock_path, self._lock_timeout)

    def read_all_grants(self) -> List[Dict[str, Any]]:
        """Read all current grant files without acquiring the lock.

        Callers that need a consistent snapshot — O2 admission, O17 take-over —
        must acquire the lock themselves before calling this method.
        """
        if not self._grants_dir.exists():
            return []
        result: List[Dict[str, Any]] = []
        for entry in sorted(self._grants_dir.iterdir()):
            if entry.suffix != ".json":
                continue
            try:
                raw: Dict[str, Any] = json.loads(entry.read_bytes())
                result.append(raw)
            except (OSError, ValueError):
                pass
        return result

    def scan_and_insert(self, attempt_id: str, grant: Dict[str, Any]) -> None:
        """Acquire registry.lock, write grant, release (§10.4 scan-and-insert).

        The caller is responsible for re-testing admission clauses (§10.1 clauses
        9 and 10) against read_all_grants() inside the critical section before
        invoking this method; 'scan' is that re-check, not work this method does.
        """
        with self._lock():
            self._grants_dir.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(self._grants_dir / f"{attempt_id}.json", grant)

    def update_grant(self, attempt_id: str, grant: Dict[str, Any]) -> None:
        """Replace a grant in place under the lock (e.g. capability none→issued→consumed)."""
        with self._lock():
            path = self._grants_dir / f"{attempt_id}.json"
            if not path.exists():
                raise GrantRegistryError(f"grant not found: {attempt_id}")
            _atomic_write_json(path, grant)

    def scan_and_remove(self, attempt_id: str) -> None:
        """Acquire registry.lock, remove the grant file, release."""
        with self._lock():
            path = self._grants_dir / f"{attempt_id}.json"
            path.unlink(missing_ok=True)


def _atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    """Write data as JSON to path atomically using os.replace (clobbers existing).

    Used only for mutable grant files.  Journal records use publish_if_absent.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    fd, name = tempfile.mkstemp(suffix=".tmp", dir=str(path.parent))
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(str(tmp), str(path))
        fsync_dir(path.parent)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
