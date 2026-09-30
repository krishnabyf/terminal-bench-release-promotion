#!/usr/bin/env python3
"""Safely promote an immutable release and roll it back on health failure."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
OUTCOMES = {"promoted", "rejected", "rolled_back", "rollback_failed"}


class PromotionError(Exception):
    """A validation or promotion error safe to report to the caller."""


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def resolved_child(root: Path, relative: str, label: str) -> Path:
    value = Path(relative)
    if value.is_absolute() or not value.parts or ".." in value.parts:
        raise PromotionError(f"{label} must be a safe relative path")
    candidate = root.joinpath(value)
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PromotionError(f"{label} does not resolve to an existing file") from exc
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PromotionError(f"{label} escapes the release directory") from exc
    if candidate.is_symlink():
        raise PromotionError(f"{label} must not be a symbolic link")
    return resolved


def load_and_validate(releases_dir: Path, release_id: str) -> tuple[Path, list[str]]:
    if not RELEASE_ID.fullmatch(release_id):
        raise PromotionError("release ID has an invalid format")

    try:
        root = releases_dir.resolve(strict=True)
    except OSError as exc:
        raise PromotionError("releases directory does not exist") from exc
    if not root.is_dir():
        raise PromotionError("releases directory is not a directory")

    lexical_release = root / release_id
    if lexical_release.is_symlink():
        raise PromotionError("release directory must not be a symbolic link")
    try:
        release = lexical_release.resolve(strict=True)
    except OSError as exc:
        raise PromotionError("release directory does not exist") from exc
    if not release.is_dir() or release.parent != root:
        raise PromotionError("release must be a direct child of the releases directory")

    manifest_path = release / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise PromotionError("manifest.json must be a regular non-symlink file")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PromotionError("manifest.json is not valid UTF-8 JSON") from exc
    if not isinstance(manifest, dict):
        raise PromotionError("manifest root must be an object")
    if manifest.get("release_id") != release_id:
        raise PromotionError("manifest release_id does not match the requested release")

    artifact_name = manifest.get("artifact")
    expected_digest = manifest.get("sha256")
    if not isinstance(artifact_name, str) or not artifact_name:
        raise PromotionError("manifest artifact must be a non-empty string")
    if not isinstance(expected_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
        raise PromotionError("manifest sha256 must be a lowercase SHA-256 digest")
    artifact = resolved_child(release, artifact_name, "artifact")
    if not artifact.is_file():
        raise PromotionError("artifact must be a regular file")
    actual_digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    if actual_digest != expected_digest:
        raise PromotionError("artifact checksum mismatch")

    healthcheck = manifest.get("healthcheck")
    if (
        not isinstance(healthcheck, list)
        or not healthcheck
        or any(not isinstance(item, str) for item in healthcheck)
    ):
        raise PromotionError("manifest healthcheck must be a non-empty list of strings")
    executable = resolved_child(release, healthcheck[0], "healthcheck executable")
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise PromotionError("healthcheck executable must be a regular executable file")
    return release, [str(executable), *healthcheck[1:]]


@contextmanager
def promotion_lock(current: Path, timeout: float) -> Iterator[None]:
    current.parent.mkdir(parents=True, exist_ok=True)
    lock_path = current.parent / f".{current.name}.promotion.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise PromotionError("timed out waiting for the promotion lock")
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def atomic_link(target: str | Path, link: Path) -> None:
    temporary = link.parent / f".{link.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    try:
        temporary.symlink_to(target)
        os.replace(temporary, link)
    finally:
        if temporary.is_symlink() or temporary.exists():
            temporary.unlink()


def active_target(current: Path) -> str | None:
    if not current.is_symlink():
        return None
    raw = os.readlink(current)
    target = Path(raw)
    if not target.is_absolute():
        target = current.parent / target
    return str(target.resolve(strict=False))


def make_record(
    release_id: str,
    outcome: str,
    previous_target: str | None,
    current: Path,
    detail: str,
) -> dict[str, object]:
    if outcome not in OUTCOMES:
        raise ValueError(f"unsupported outcome: {outcome}")
    return {
        "timestamp": utc_timestamp(),
        "release_id": release_id,
        "outcome": outcome,
        "previous_target": previous_target,
        "active_target": active_target(current),
        "detail": detail or "no additional detail",
    }


def emit(record: dict[str, object], audit_log: Path) -> None:
    audit_log.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, sort_keys=True, separators=(",", ":"))
    descriptor = os.open(audit_log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(descriptor, (line + "\n").encode("utf-8"))
    finally:
        os.close(descriptor)
    print(line)


def run_healthcheck(command: list[str], release: Path, timeout: float) -> tuple[bool, str]:
    """Run the check in its own process group so timeout kills descendants."""
    try:
        process = subprocess.Popen(
            command,
            cwd=release,
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except OSError as exc:
        return False, f"healthcheck execution failed: {exc}"

    try:
        process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        return False, "healthcheck timed out"

    if process.returncode == 0:
        return True, "healthcheck passed"
    return False, f"healthcheck exited with status {process.returncode}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--releases-dir", required=True)
    parser.add_argument("--current-link", required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--audit-log", required=True)
    parser.add_argument("--health-timeout", type=float, default=5.0)
    args = parser.parse_args()
    if args.health_timeout <= 0:
        parser.error("--health-timeout must be greater than zero")
    return args


def main() -> int:
    args = parse_args()
    current = Path(args.current_link)
    audit_log = Path(args.audit_log)
    previous_target: str | None = None

    try:
        release, healthcheck = load_and_validate(Path(args.releases_dir), args.release)
        with promotion_lock(current, args.health_timeout):
            if current.exists() and not current.is_symlink():
                raise PromotionError("current path exists and is not a symbolic link")
            if current.is_symlink():
                previous_raw = os.readlink(current)
                previous_target = active_target(current)
            else:
                previous_raw = None

            atomic_link(release, current)
            healthy, health_detail = run_healthcheck(healthcheck, release, args.health_timeout)
            if healthy:
                record = make_record(
                    args.release,
                    "promoted",
                    previous_target,
                    current,
                    "release promoted and healthcheck passed",
                )
                emit(record, audit_log)
                return 0
            failure = health_detail

            try:
                if previous_raw is None:
                    if current.is_symlink():
                        current.unlink()
                else:
                    atomic_link(previous_raw, current)
                record = make_record(args.release, "rolled_back", previous_target, current, failure)
            except OSError as exc:
                record = make_record(
                    args.release,
                    "rollback_failed",
                    previous_target,
                    current,
                    f"{failure}; rollback failed: {exc}",
                )
            emit(record, audit_log)
            return 1
    except (PromotionError, OSError) as exc:
        record = make_record(args.release, "rejected", previous_target, current, str(exc))
        emit(record, audit_log)
        return 1


if __name__ == "__main__":
    sys.exit(main())
