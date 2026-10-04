#!/usr/bin/env python3
"""Safely fork a local Codex session from one CODEX_HOME into another.

The source home is treated as read-only. A temporary CODEX_HOME is populated
with the selected rollout lineage and its paginated history, then the official
Codex app-server creates a new fork. The fork is imported into the target home
under a new thread id, so source and target can continue concurrently.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import selectors
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Iterable


UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
STATE_DB_RE = re.compile(r"^state_(\d+)\.sqlite$")
HISTORY_TABLES = (
    "thread_history_projection_state",
    "thread_turns",
    "thread_items",
    "thread_realtime_items",
)
STATE_THREAD_TABLES = ("thread_dynamic_tools", "thread_artifacts")


class ShareError(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class Rollout:
    path: Path
    thread_id: str
    history_base_id: str | None
    history_base_end_byte_offset: int | None
    history_mode: str


@dataclasses.dataclass(frozen=True)
class Lineage:
    rollouts: tuple[Rollout, ...]
    required_prefix_bytes: dict[Path, int]

    @property
    def thread_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item.thread_id for item in self.rollouts))


def discover_homes(root: Path | None = None, current: str | None = None) -> dict[str, Path]:
    """Find local Codex homes without opening authentication data."""
    root = (root or Path.home()).expanduser().resolve()
    current = os.environ.get("CODEX_HOME") if current is None else current
    current_path = Path(current).expanduser().resolve() if current else None
    homes: dict[str, Path] = {}
    candidates = [root / ".codex", *sorted(root.glob(".codex_*")),
                  *sorted(root.glob(".codex-*"))]
    if current_path:
        candidates.append(current_path)
    for candidate in candidates:
        if not candidate.is_dir():
            continue
        if candidate.resolve() != current_path and not any(
            (candidate / marker).exists() for marker in
            ("auth.json", "config.toml", "sessions", "archived_sessions")
        ) and not state_db(candidate):
            continue
        path = candidate.resolve()
        if candidate.name == ".codex":
            label = "default"
        elif candidate.name.startswith(".codex_share_"):
            label = candidate.name.removeprefix(".codex_share_")
        elif candidate.name.startswith(".codex_"):
            label = candidate.name.removeprefix(".codex_")
        elif candidate.name.startswith(".codex-"):
            label = candidate.name.removeprefix(".codex-")
        else:
            label = candidate.name.lstrip(".") or "home"
        if not label or (label in homes and homes[label] != path):
            label = candidate.name.lstrip(".") or "home"
        if label in homes and homes[label] != path:
            label = str(path)
        homes[label] = path
    preferred = {
        label: (root.name[:1] if label == "default" else label[:1])
        for label in homes
    }
    for label, short in preferred.items():
        if short and list(preferred.values()).count(short) == 1 and short not in homes:
            homes[short] = homes[label]
    if current:
        if current_path in homes.values():
            homes["current"] = current_path
    elif "default" in homes:
        homes["current"] = homes["default"]
    return homes


def resolve_home(value: str) -> Path:
    homes = discover_homes()
    if value in homes:
        return homes[value]
    if value == "current":
        raise ShareError("current CODEX_HOME was not found; pass a HOME path")
    if "/" not in value and value not in {".", ".."}:
        raise ShareError(f"unknown HOME {value!r}; run 'codex-merge homes' to see available names")
    return Path(value).expanduser().resolve()


def profile_label(path: Path) -> str:
    for key, candidate in discover_homes().items():
        if candidate == path and key != "current":
            return key
    return str(path)


def launcher_for_home(path: Path, codex_binary: str = "codex") -> str:
    return f"CODEX_HOME={shlex.quote(str(path))} {shlex.quote(codex_binary)}"


def latest_numbered_db(home: Path, pattern: re.Pattern[str]) -> Path | None:
    candidates: list[tuple[int, Path]] = []
    for path in home.glob("*.sqlite"):
        match = pattern.match(path.name)
        if match:
            candidates.append((int(match.group(1)), path))
    return max(candidates, default=(0, None), key=lambda item: item[0])[1]


def state_db(home: Path) -> Path | None:
    return latest_numbered_db(home, STATE_DB_RE)


def history_db(home: Path) -> Path:
    return home / "thread_history_1.sqlite"


def history_schema_ready(home: Path) -> bool:
    database = history_db(home)
    if not database.exists():
        return False
    try:
        with sqlite3.connect(database) as connection:
            return table_exists(connection, "thread_turns") and table_exists(
                connection, "thread_items"
            )
    except sqlite3.Error:
        return False


def sqlite_ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def table_exists(connection: sqlite3.Connection, table: str, schema: str = "main") -> bool:
    row = connection.execute(
        f"SELECT 1 FROM {schema}.sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def table_columns(connection: sqlite3.Connection, schema: str, table: str) -> list[str]:
    return [
        row[1]
        for row in connection.execute(f'PRAGMA {schema}.table_info("{table}")')
    ]


def table_primary_key(connection: sqlite3.Connection, schema: str, table: str) -> list[str]:
    rows = list(connection.execute(f'PRAGMA {schema}.table_info("{table}")'))
    return [row[1] for row in sorted((r for r in rows if r[5]), key=lambda r: r[5])]


def parse_rollout(path: Path) -> Rollout:
    if path.suffix == ".zst":
        raise ShareError(f"compressed rollout is not supported yet: {path}")
    try:
        with path.open("r", encoding="utf-8") as handle:
            for _ in range(128):
                line = handle.readline()
                if not line:
                    break
                record = json.loads(line)
                if not isinstance(record, dict):
                    continue
                if record.get("type") != "session_meta":
                    continue
                payload = record.get("payload") or {}
                if not isinstance(payload, dict):
                    raise ShareError(f"invalid session metadata in {path}")
                thread_id = str(payload.get("id") or "")
                if not UUID_RE.match(thread_id):
                    raise ShareError(f"invalid session id in {path}")
                base = payload.get("history_base") or {}
                if not isinstance(base, dict):
                    raise ShareError(f"invalid history base in {path}")
                base_id = base.get("thread_id")
                base_offset = base.get("end_byte_offset")
                if base_id and not UUID_RE.match(str(base_id)):
                    raise ShareError(f"invalid history base ID in {path}")
                if base_offset is not None and (not isinstance(base_offset, int) or base_offset < 0):
                    raise ShareError(f"invalid history boundary in {path}")
                return Rollout(
                    path=path.resolve(),
                    thread_id=thread_id,
                    history_base_id=str(base_id) if base_id else None,
                    history_base_end_byte_offset=(
                        base_offset
                    ),
                    history_mode=str(payload.get("history_mode") or "legacy"),
                )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise ShareError(f"cannot read rollout {path}: {exc}") from exc
    raise ShareError(f"session metadata not found in {path}")


def rollout_paths(home: Path) -> Iterable[Path]:
    for root_name in ("sessions", "archived_sessions"):
        root = home / root_name
        if not root.exists():
            continue
        yield from root.rglob("rollout-*.jsonl")
        yield from root.rglob("rollout-*.jsonl.zst")


def rollout_path_from_state(home: Path, thread_id: str) -> Path | None:
    database = state_db(home)
    if not database:
        return None
    try:
        with sqlite_ro(database) as connection:
            if not table_exists(connection, "threads"):
                return None
            row = connection.execute(
                "SELECT rollout_path FROM threads WHERE id=?", (thread_id,)
            ).fetchone()
    except sqlite3.Error:
        return None
    if not row or not row[0]:
        return None
    path = Path(row[0]).expanduser()
    if path.exists():
        return path.resolve()
    return None


def find_rollout(home: Path, identifier: str) -> Rollout:
    state_path = rollout_path_from_state(home, identifier)
    if state_path:
        rollout = parse_rollout(state_path)
        if rollout.thread_id == identifier:
            return rollout

    exact: list[Rollout] = []
    for path in rollout_paths(home):
        if identifier not in path.name:
            continue
        try:
            rollout = parse_rollout(path)
        except ShareError:
            continue
        if rollout.thread_id == identifier:
            exact.append(rollout)
    if not exact:
        raise ShareError(f"session/rollout {identifier} not found under {home}")
    return max(exact, key=lambda item: (item.path.stat().st_mtime_ns, item.path.name))


def resolve_thread_ref(home: Path, reference: str) -> str:
    if UUID_RE.match(reference):
        return reference.lower()
    if not re.fullmatch(r"[0-9a-fA-F-]{4,35}", reference):
        raise ShareError("session reference must be a UUID or a UUID prefix (at least 4 characters)")

    candidates: set[str] = set()
    database = state_db(home)
    if database:
        try:
            with sqlite_ro(database) as connection:
                if table_exists(connection, "threads"):
                    for row in connection.execute(
                        "SELECT id FROM threads WHERE id LIKE ? OR name=?",
                        (reference + "%", reference),
                    ):
                        candidates.add(str(row[0]))
        except sqlite3.Error:
            pass
    if not candidates:
        for path in rollout_paths(home):
            try:
                thread_id = parse_rollout(path).thread_id
            except ShareError:
                continue
            if thread_id.startswith(reference):
                candidates.add(thread_id)
    if not candidates:
        raise ShareError(f"no session matches {reference!r} in {home}")
    if len(candidates) > 1:
        raise ShareError(
            f"session prefix {reference!r} is ambiguous: {', '.join(sorted(candidates))}"
        )
    return next(iter(candidates))


def resolve_lineage(home: Path, thread_id: str) -> Lineage:
    rollouts: list[Rollout] = []
    required: dict[Path, int] = {}
    seen: set[str] = set()
    found: dict[str, Rollout] = {}

    def get_rollout(identifier: str) -> Rollout:
        if identifier not in found:
            found[identifier] = find_rollout(home, identifier)
        return found[identifier]

    current_id: str | None = thread_id
    while current_id:
        if current_id in seen:
            raise ShareError(f"history_base cycle detected at {current_id}")
        seen.add(current_id)
        rollout = get_rollout(current_id)
        rollouts.append(rollout)
        if rollout.history_base_id:
            if rollout.history_base_end_byte_offset is not None:
                base_rollout = get_rollout(rollout.history_base_id)
                required[base_rollout.path] = max(
                    required.get(base_rollout.path, 0), rollout.history_base_end_byte_offset
                )
            current_id = rollout.history_base_id
        else:
            current_id = None
    return Lineage(tuple(rollouts), required)


def relative_rollout_path(home: Path, path: Path) -> Path:
    try:
        relative = path.resolve().relative_to(home.resolve())
    except ValueError as exc:
        raise ShareError(f"rollout is outside CODEX_HOME: {path}") from exc
    if not relative.parts or relative.parts[0] not in {"sessions", "archived_sessions"}:
        raise ShareError(f"unexpected rollout location: {path}")
    return relative


def hash_prefix(path: Path, byte_count: int | None = None) -> str:
    digest = hashlib.sha256()
    remaining = byte_count
    with path.open("rb") as handle:
        while True:
            size = 1024 * 1024 if remaining is None else min(1024 * 1024, remaining)
            if size <= 0:
                break
            block = handle.read(size)
            if not block:
                break
            digest.update(block)
            if remaining is not None:
                remaining -= len(block)
    if remaining not in (None, 0):
        raise ShareError(f"rollout is shorter than required history boundary: {path}")
    return digest.hexdigest()


def copy_rollouts(source_home: Path, target_home: Path, lineage: Lineage) -> list[Path]:
    created: list[Path] = []
    for rollout in reversed(lineage.rollouts):
        relative = relative_rollout_path(source_home, rollout.path)
        destination = target_home / relative
        boundary = lineage.required_prefix_bytes.get(rollout.path)
        if destination.exists():
            if boundary is not None:
                if hash_prefix(rollout.path, boundary) != hash_prefix(destination, boundary):
                    raise ShareError(f"target has a conflicting history base: {destination}")
            elif hash_prefix(rollout.path) != hash_prefix(destination):
                raise ShareError(f"target has a conflicting rollout: {destination}")
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(rollout.path, destination)
        os.chmod(destination, 0o600)
        created.append(destination)
    return created


def rollout_conflicts(source_home: Path, target_home: Path, lineage: Lineage) -> bool:
    """Return whether TARGET already has incompatible data for this lineage."""
    for rollout in lineage.rollouts:
        relative = relative_rollout_path(source_home, rollout.path)
        destination = target_home / relative
        if not destination.exists():
            continue
        boundary = lineage.required_prefix_bytes.get(rollout.path)
        try:
            source_hash = hash_prefix(rollout.path, boundary)
            target_hash = hash_prefix(destination, boundary)
        except ShareError:
            return True
        if source_hash != target_hash:
            return True
    return False


def _replace_json_string_tokens(data: bytes, id_map: dict[str, str]) -> bytes:
    """Replace exact JSON string values while preserving every byte offset."""
    for old_id, new_id in id_map.items():
        old_token = json.dumps(old_id).encode("ascii")
        new_token = json.dumps(new_id).encode("ascii")
        if len(old_token) != len(new_token):
            raise ShareError("thread id remapping changed a JSON token length")
        data = data.replace(old_token, new_token)
    return data


def _remap_database_text_tokens(database: Path, id_map: dict[str, str]) -> None:
    """Remap exact UUID values and UUID-valued JSON fields in an isolated DB."""
    if not database.exists():
        return
    connection = sqlite3.connect(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        for table in tables:
            columns = table_columns(connection, "main", table)
            column_types = {
                row[1]: str(row[2] or "").upper()
                for row in connection.execute(f'PRAGMA table_info("{table}")')
            }
            for column in columns:
                if column_types[column] not in {"", "TEXT"}:
                    continue
                quoted_table = table.replace('"', '""')
                quoted_column = column.replace('"', '""')
                for old_id, new_id in id_map.items():
                    # The first branch updates primary/foreign-key cells. The
                    # second updates structured JSON values without rewriting
                    # UUIDs embedded in prompts, commands, or paths.
                    old_token = json.dumps(old_id)
                    new_token = json.dumps(new_id)
                    connection.execute(
                        f'UPDATE "{quoted_table}" SET "{quoted_column}" = '
                        f'CASE WHEN "{quoted_column}"=? THEN ? '
                        f'ELSE replace("{quoted_column}", ?, ?) END '
                        f'WHERE "{quoted_column}"=? OR instr("{quoted_column}", ?) > 0',
                        (old_id, new_id, old_token, new_token, old_id, old_token),
                    )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def remap_lineage(home: Path, lineage: Lineage) -> tuple[Lineage, dict[str, str]]:
    """Give an isolated lineage fresh IDs so it cannot collide on import.

    UUIDs have a fixed encoded length, so exact JSON-token substitution keeps
    history byte boundaries valid. This must only be used on a temporary HOME.
    """
    id_map = {thread_id: str(uuid.uuid4()) for thread_id in lineage.thread_ids}
    planned_paths: dict[Path, Path] = {}
    created: list[Path] = []
    try:
        for rollout in lineage.rollouts:
            new_id = id_map[rollout.thread_id]
            if rollout.thread_id not in rollout.path.name:
                raise ShareError(
                    f"rollout filename does not contain its thread id: {rollout.path}"
                )
            destination = rollout.path.with_name(
                rollout.path.name.replace(rollout.thread_id, new_id)
            )
            if destination.exists():
                raise ShareError(f"remapped rollout already exists: {destination}")
            with rollout.path.open("rb") as source, destination.open("wb") as target:
                for line in source:
                    target.write(_replace_json_string_tokens(line, id_map))
            shutil.copystat(rollout.path, destination)
            os.chmod(destination, 0o600)
            planned_paths[rollout.path] = destination
            created.append(destination)

        _remap_database_text_tokens(history_db(home), id_map)
        database = state_db(home)
        if database:
            _remap_database_text_tokens(database, id_map)
            connection = sqlite3.connect(database)
            try:
                connection.execute("BEGIN IMMEDIATE")
                for rollout in lineage.rollouts:
                    connection.execute(
                        "UPDATE threads SET rollout_path=? WHERE id=?",
                        (str(planned_paths[rollout.path]), id_map[rollout.thread_id]),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

        for original in planned_paths:
            original.unlink()
    except Exception:
        for path in created:
            with contextlib.suppress(OSError):
                path.unlink()
        raise

    remapped = resolve_lineage(home, id_map[lineage.rollouts[0].thread_id])
    return remapped, id_map


def backup_sqlite(database: Path, destination: Path) -> None:
    if not database.exists():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite_ro(database)
    target_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(target_connection)
    finally:
        target_connection.close()
        source_connection.close()
    os.chmod(destination, 0o600)


def clone_history_schema(template: Path, target: Path) -> None:
    """Create an empty history DB with Codex's exact current schema.

    Codex app-server currently opens this database lazily and does not run its
    migrations for a brand-new home during thread/read. Copying only schema
    objects and SQLx migration bookkeeping avoids embedding a version-specific
    schema here and, importantly, does not copy any session rows.
    """
    if not template.exists():
        raise ShareError(f"history schema template is missing: {template}")
    target.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite_ro(template)
    target_connection = sqlite3.connect(target)
    try:
        existing = target_connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        if existing:
            raise ShareError(
                f"history database has an incomplete/unsupported schema: {target}"
            )
        objects = list(
            source_connection.execute(
                "SELECT type, name, sql FROM sqlite_master "
                "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
                "ORDER BY CASE type "
                "WHEN 'table' THEN 0 WHEN 'index' THEN 1 "
                "WHEN 'trigger' THEN 2 WHEN 'view' THEN 3 ELSE 4 END, name"
            )
        )
        if not any(name == "thread_turns" for _, name, _ in objects):
            raise ShareError(f"template lacks Codex history schema: {template}")
        target_connection.execute("BEGIN IMMEDIATE")
        for _, _, sql in objects:
            target_connection.execute(sql)
        if table_exists(source_connection, "_sqlx_migrations"):
            columns = table_columns(source_connection, "main", "_sqlx_migrations")
            quoted = ", ".join(f'"{column}"' for column in columns)
            placeholders = ", ".join("?" for _ in columns)
            rows = source_connection.execute(
                f'SELECT {quoted} FROM "_sqlx_migrations"'
            ).fetchall()
            target_connection.executemany(
                f'INSERT INTO "_sqlx_migrations" ({quoted}) VALUES ({placeholders})',
                rows,
            )
        target_connection.commit()
    except Exception:
        target_connection.rollback()
        raise
    finally:
        target_connection.close()
        source_connection.close()
    os.chmod(target, 0o600)


def merge_history_rows(source: Path, target: Path, thread_ids: Iterable[str]) -> dict[str, int]:
    ids = tuple(dict.fromkeys(thread_ids))
    if not ids:
        return {}
    if not source.exists():
        raise ShareError(f"source paginated history database is missing: {source}")
    if not target.exists():
        raise ShareError(f"target paginated history database is missing: {target}")

    counts: dict[str, int] = {}
    connection = sqlite3.connect(target, timeout=30, uri=True)
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("ATTACH DATABASE ? AS src", (source.resolve().as_uri() + "?mode=ro",))
    try:
        connection.execute("BEGIN IMMEDIATE")
        for table in HISTORY_TABLES:
            if not table_exists(connection, table, "main") or not table_exists(
                connection, table, "src"
            ):
                continue
            target_columns = table_columns(connection, "main", table)
            source_columns = set(table_columns(connection, "src", table))
            common = [column for column in target_columns if column in source_columns]
            primary_key = table_primary_key(connection, "main", table)
            if "thread_id" not in common or not primary_key:
                raise ShareError(f"unsupported history schema for table {table}")
            quoted = ", ".join(f'"{column}"' for column in common)
            placeholders = ", ".join("?" for _ in ids)
            source_rows = connection.execute(
                f'SELECT {quoted} FROM src."{table}" '
                f"WHERE thread_id IN ({placeholders})",
                ids,
            )
            inserted = 0
            indices = {column: index for index, column in enumerate(common)}
            for row in source_rows:
                where = " AND ".join(f'"{column}"=?' for column in primary_key)
                key_values = tuple(row[indices[column]] for column in primary_key)
                existing = connection.execute(
                    f'SELECT {quoted} FROM main."{table}" WHERE {where}', key_values
                ).fetchone()
                if existing is not None:
                    if table == "thread_history_projection_state":
                        existing_ordinal = existing[indices["next_rollout_ordinal"]]
                        source_ordinal = row[indices["next_rollout_ordinal"]]
                        if existing_ordinal >= source_ordinal:
                            continue
                    if tuple(existing) != tuple(row):
                        raise ShareError(
                            f"target history conflicts in {table} for key {key_values}"
                        )
                    continue
                values = ", ".join("?" for _ in common)
                connection.execute(
                    f'INSERT INTO main."{table}" ({quoted}) VALUES ({values})', row
                )
                inserted += 1
            counts[table] = inserted
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return counts


def merge_state_rows(
    source_home: Path, target_home: Path, thread_ids: Iterable[str]
) -> dict[str, int]:
    """Import only the selected thread metadata, rewriting rollout paths."""
    ids = tuple(dict.fromkeys(thread_ids))
    source = state_db(source_home)
    target = state_db(target_home)
    if not source or not target:
        raise ShareError("source or target state database is missing")
    counts: dict[str, int] = {}
    connection = sqlite3.connect(target, timeout=30, uri=True)
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("ATTACH DATABASE ? AS src", (source.resolve().as_uri() + "?mode=ro",))
    try:
        connection.execute("BEGIN IMMEDIATE")
        target_columns = table_columns(connection, "main", "threads")
        source_columns = set(table_columns(connection, "src", "threads"))
        common = [column for column in target_columns if column in source_columns]
        if "id" not in common or "rollout_path" not in common:
            raise ShareError("unsupported Codex state schema for threads")
        quoted = ", ".join(f'"{column}"' for column in common)
        placeholders = ", ".join("?" for _ in ids)
        indices = {column: index for index, column in enumerate(common)}
        inserted = 0
        for source_row in connection.execute(
            f'SELECT {quoted} FROM src."threads" WHERE id IN ({placeholders})', ids
        ).fetchall():
            row = list(source_row)
            thread_id = str(row[indices["id"]])
            rollout = find_rollout(source_home, thread_id)
            row[indices["rollout_path"]] = str(
                target_home / relative_rollout_path(source_home, rollout.path)
            )
            # Projects and UI sections are per-home organization, not session
            # history. Avoid dangling references when importing metadata.
            for column in ("project_id", "thread_section_id"):
                if column in indices:
                    row[indices[column]] = None
            existing = connection.execute(
                f'SELECT {quoted} FROM main."threads" WHERE id=?', (thread_id,)
            ).fetchone()
            if existing is not None:
                if str(existing[indices["rollout_path"]]) != row[indices["rollout_path"]]:
                    raise ShareError(f"target state conflicts for thread {thread_id}")
                continue
            values = ", ".join("?" for _ in common)
            connection.execute(
                f'INSERT INTO main."threads" ({quoted}) VALUES ({values})', row
            )
            inserted += 1
        counts["threads"] = inserted

        for table in STATE_THREAD_TABLES:
            if not table_exists(connection, table, "main") or not table_exists(
                connection, table, "src"
            ):
                continue
            target_columns = table_columns(connection, "main", table)
            source_columns = set(table_columns(connection, "src", table))
            common = [column for column in target_columns if column in source_columns]
            primary_key = table_primary_key(connection, "main", table)
            if "thread_id" not in common or not primary_key:
                continue
            quoted = ", ".join(f'"{column}"' for column in common)
            rows = connection.execute(
                f'SELECT {quoted} FROM src."{table}" '
                f'WHERE thread_id IN ({placeholders})',
                ids,
            ).fetchall()
            column_indices = {column: index for index, column in enumerate(common)}
            inserted = 0
            for row in rows:
                where = " AND ".join(f'"{column}"=?' for column in primary_key)
                keys = tuple(row[column_indices[column]] for column in primary_key)
                existing = connection.execute(
                    f'SELECT {quoted} FROM main."{table}" WHERE {where}', keys
                ).fetchone()
                if existing is not None:
                    if tuple(existing) != tuple(row):
                        raise ShareError(f"target state conflicts in {table} for key {keys}")
                    continue
                values = ", ".join("?" for _ in common)
                connection.execute(
                    f'INSERT INTO main."{table}" ({quoted}) VALUES ({values})', row
                )
                inserted += 1
            counts[table] = inserted
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return counts


class AppServer:
    def __init__(self, home: Path, codex_binary: str, timeout: float = 30.0):
        self.home = home
        self.codex_binary = codex_binary
        self.timeout = timeout
        self.process: subprocess.Popen[bytes] | None = None
        self.selector: selectors.BaseSelector | None = None
        self.request_id = 0
        self.stdout_buffer = bytearray()
        self.stderr_file: Any = None

    def __enter__(self) -> "AppServer":
        environment = os.environ.copy()
        environment["CODEX_HOME"] = str(self.home)
        self.stderr_file = tempfile.TemporaryFile(mode="w+t")
        try:
            self.process = subprocess.Popen(
                [self.codex_binary, "app-server", "--stdio"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self.stderr_file,
                env=environment,
            )
        except Exception:
            self.stderr_file.close()
            raise
        assert self.process.stdout is not None
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        try:
            self.call(
                "initialize",
                {
                    "clientInfo": {
                        "name": "codex-merge",
                        "title": "Codex Session Share",
                    "version": "0.4.0",
                    },
                    "capabilities": {"experimentalApi": True},
                },
            )
            self.notify("initialized")
        except Exception:
            self.__exit__(*sys.exc_info())
            raise
        return self

    def _send(self, payload: dict[str, Any]) -> None:
        assert self.process is not None and self.process.stdin is not None
        self.process.stdin.write((json.dumps(payload, separators=(",", ":")) + "\n").encode())
        self.process.stdin.flush()

    def _read_line(self, deadline: float) -> bytes | None:
        assert self.process is not None and self.process.stdout is not None
        assert self.selector is not None
        while time.monotonic() < deadline:
            newline = self.stdout_buffer.find(b"\n")
            if newline >= 0:
                line = bytes(self.stdout_buffer[:newline])
                del self.stdout_buffer[:newline + 1]
                return line
            if not self.selector.select(max(0.0, deadline - time.monotonic())):
                return None
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                return None
            self.stdout_buffer.extend(chunk)
        return None

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"method": method}
        if params is not None:
            payload["params"] = params
        self._send(payload)

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.request_id += 1
        request_id = self.request_id
        self._send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout
        assert self.process is not None
        while time.monotonic() < deadline:
            line = self._read_line(deadline)
            if line is None:
                break
            try:
                response = json.loads(line)
            except json.JSONDecodeError:
                continue
            if response.get("id") != request_id:
                continue
            if "error" in response:
                raise ShareError(f"app-server {method} failed: {response['error']}")
            result = response.get("result")
            if not isinstance(result, dict):
                raise ShareError(f"app-server {method} returned an invalid response")
            return result
        stderr = ""
        if self.process.poll() is not None and self.stderr_file is not None:
            self.stderr_file.seek(0)
            stderr = self.stderr_file.read().strip()[-2000:]
        raise ShareError(f"app-server {method} timed out or exited: {stderr}")

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if not self.process:
            if self.stderr_file:
                self.stderr_file.close()
            return
        with contextlib.suppress(Exception):
            if self.process.stdin:
                self.process.stdin.close()
        # App-server can keep background maintenance tasks alive after stdin
        # reaches EOF. All RPC writes are durable before their responses, so
        # terminate the isolated stdio server instead of adding a 10 s delay
        # for every validation phase.
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.process.wait(timeout=5)
        if self.selector:
            self.selector.close()
        if self.process.stdout:
            self.process.stdout.close()
        if self.stderr_file:
            self.stderr_file.close()


def ensure_databases(home: Path, codex_binary: str, history_template: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    if history_schema_ready(home) and state_db(home):
        return
    if not history_schema_ready(home):
        clone_history_schema(history_template, history_db(home))
    # Starting the official server creates/migrates the state database and
    # validates that the cloned history migration bookkeeping is accepted.
    with AppServer(home, codex_binary) as server:
        server.call("thread/list", {"limit": 1})
    if not history_schema_ready(home) or not state_db(home):
        raise ShareError(f"Codex did not initialize local databases under {home}")


def read_thread(server: AppServer, thread_id: str, *, resume: bool = False) -> dict[str, Any]:
    # Full-history hydration is deprecated for paginated threads in Codex
    # 0.154+. It can wait indefinitely, so request metadata and page turns via
    # thread/turns/list instead.
    method = "thread/resume" if resume else "thread/read"
    params = (
        {"threadId": thread_id, "excludeTurns": True}
        if resume
        else {"threadId": thread_id, "includeTurns": False}
    )
    return server.call(method, params)["thread"]


def count_thread_turns(server: AppServer, thread_id: str) -> int:
    count = 0
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {
            "threadId": thread_id,
            "limit": 100,
            "sortDirection": "asc",
            "itemsView": "notLoaded",
        }
        if cursor is not None:
            params["cursor"] = cursor
        page = server.call("thread/turns/list", params)
        count += len(page.get("data") or [])
        cursor = page.get("nextCursor")
        if not cursor:
            return count


def make_backup(home: Path, child_id: str) -> Path:
    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = home / "backups" / f"session-merge-{timestamp}-{child_id[:8]}"
    backup.mkdir(parents=True, exist_ok=False)
    backup_sqlite(history_db(home), backup / "thread_history_1.sqlite")
    current_state = state_db(home)
    if current_state:
        backup_sqlite(current_state, backup / current_state.name)
    return backup


@contextlib.contextmanager
def target_lock(home: Path):
    lock_dir = home / "thread-writer-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / ".codex-merge.lock"
    with lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def share_session(
    source_home: Path,
    target_home: Path,
    reference: str,
    codex_binary: str,
    *,
    dry_run: bool,
) -> tuple[str, Path | None, int]:
    if source_home == target_home:
        raise ShareError("source and target CODEX_HOME must differ")
    if not source_home.is_dir():
        raise ShareError(f"source CODEX_HOME does not exist: {source_home}")
    thread_id = resolve_thread_ref(source_home, reference)
    source_lineage = resolve_lineage(source_home, thread_id)
    if dry_run:
        for path, boundary in source_lineage.required_prefix_bytes.items():
            hash_prefix(path, boundary)
        conflict = rollout_conflicts(source_home, target_home, source_lineage)
        print(f"source: {profile_label(source_home)} ({source_home})")
        print(f"target: {profile_label(target_home)} ({target_home})")
        print(f"session: {thread_id}")
        print(f"target exists: {'yes' if target_home.is_dir() else 'no'}")
        print(f"history collision: {'yes (will remap IDs)' if conflict else 'no'}")
        print("lineage:")
        for rollout in source_lineage.rollouts:
            print(f"  {rollout.thread_id}  {rollout.path}")
        return thread_id, None, 0

    with tempfile.TemporaryDirectory(prefix="codex-session-merge-") as temp_name:
        temporary_home = Path(temp_name)
        copy_rollouts(source_home, temporary_home, source_lineage)
        ensure_databases(temporary_home, codex_binary, history_db(source_home))
        merge_history_rows(
            history_db(source_home), history_db(temporary_home), source_lineage.thread_ids
        )
        merge_state_rows(source_home, temporary_home, source_lineage.thread_ids)

        with AppServer(temporary_home, codex_binary) as server:
            read_thread(server, thread_id)
            expected_turns = count_thread_turns(server, thread_id)
            fork_result = server.call(
                "thread/fork", {"threadId": thread_id, "excludeTurns": True}
            )
            child_id = str(fork_result["thread"]["id"])

        fork_lineage = resolve_lineage(temporary_home, child_id)
        target_home.mkdir(parents=True, exist_ok=True)
        if rollout_conflicts(temporary_home, target_home, fork_lineage):
            print(
                "target contains a divergent copy of this history; "
                "importing a collision-free lineage"
            )
            fork_lineage, id_map = remap_lineage(temporary_home, fork_lineage)
            child_id = id_map[child_id]
            with AppServer(temporary_home, codex_binary) as server:
                read_thread(server, child_id)
                remapped_turns = count_thread_turns(server, child_id)
            if remapped_turns != expected_turns:
                raise ShareError(
                    "lineage remap verification failed: "
                    f"expected {expected_turns} turns, got {remapped_turns}"
                )
        with target_lock(target_home):
            backup = make_backup(target_home, child_id)
            copy_rollouts(temporary_home, target_home, fork_lineage)
            ensure_databases(target_home, codex_binary, history_db(temporary_home))
            merge_history_rows(
                history_db(temporary_home), history_db(target_home), fork_lineage.thread_ids
            )
            merge_state_rows(temporary_home, target_home, fork_lineage.thread_ids)

        with AppServer(target_home, codex_binary) as server:
            read_thread(server, child_id)
            target_turns = count_thread_turns(server, child_id)
            read_thread(server, child_id, resume=True)
            resumed_turns = count_thread_turns(server, child_id)
        if target_turns != expected_turns or resumed_turns != expected_turns:
            raise ShareError(
                "verification failed: "
                f"source fork has {expected_turns} turns, target read/resume has "
                f"{target_turns}/{resumed_turns}; backup: {backup}"
            )
        return child_id, backup, expected_turns


def show_homes() -> None:
    homes = discover_homes()
    if not homes:
        print("No local Codex homes found. Pass a CODEX_HOME path directly.")
        return
    displayed: set[Path] = set()
    for label, path in homes.items():
        if path in displayed:
            continue
        displayed.add(path)
        aliases = [alias for alias, candidate in homes.items()
                   if alias != label and candidate == path]
        suffix = f" ({', '.join(aliases)})" if aliases else ""
        auth = "auth.json present" if (path / "auth.json").is_file() else "no auth.json"
        print(f"{label}{suffix}  {path}  [{auth}]")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex-merge",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "在多个独立 CODEX_HOME 之间安全复制（fork）Codex Session。\n\n"
            "源 Session 保持不变，目标 HOME 会得到一个新的 Session ID。\n"
            "两边之后可以独立继续，不会共享正在写入的 SQLite 数据库，也不会复制 auth.json。"
        ),
        epilog="""HOME 可使用 homes 列出的名称、current 或目录路径。
唯一的首字母也可作短名；默认 HOME 使用系统用户名的首字母。

常用示例:
  codex-merge homes
      自动查找本机 CODEX_HOME。

  codex-merge primary secondary 01a0ad83-f4e --dry-run
      预览从 primary 到 secondary 的迁移；Session ID 可以使用唯一前缀。

  codex-merge primary secondary 01a0ad83-f4e
      创建安全 fork，完成后打印新 Session ID 和 resume 命令。

  codex-merge primary secondary 01a0ad83-f4e --resume
      创建 fork，并立即使用 secondary HOME 继续该 Session。

完整写法:
  codex-merge fork SOURCE TARGET SESSION [--dry-run] [--resume]

提示:
  最好等源 Session 当前回复结束后再迁移。迁移是一次性快照，迁移后两边的新消息不会自动同步。
  运行 `codex-merge <子命令> --help` 可查看该子命令的详细说明。""",
    )
    parser.add_argument(
        "--codex-bin",
        default=os.environ.get("CODEX_BIN") or shutil.which("codex") or "codex",
        metavar="PATH",
        help="Codex CLI 可执行文件路径（默认：PATH 中的 codex）",
    )
    parser.add_argument(
        "--version", action="version", version="codex-merge 0.4.0", help="显示版本号并退出"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    homes_parser = subparsers.add_parser(
        "homes",
        help="自动查找本机的 Codex HOME",
        description="查找 ~/.codex、~/.codex_*、~/.codex-* 和当前 CODEX_HOME；只显示 auth.json 是否存在。",
        epilog="示例:\n  codex-merge homes",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    fork_parser = subparsers.add_parser(
        "fork",
        help="把一个 Session 安全 fork 到另一个 HOME",
        description=(
            "从 SOURCE 读取指定 Session，在隔离临时 HOME 中创建官方 fork，\n"
            "然后将新 Session 导入 TARGET。源 Session 不会被修改。\n\n"
            "目标 Session 使用新的 ID，因此两个账号之后可以并行、独立继续。"
        ),
        epilog="""示例:
  codex-merge fork primary secondary 01a0ad83-f4e --dry-run
      只检查 Session、历史链和目标位置，不写入目标 HOME。

  codex-merge fork primary secondary 01a0ad83-f4e
      从 primary fork 到 secondary，完成后打印新 ID 和 resume 命令。

  codex-merge fork primary secondary 01a0ad83-f4e --resume
      迁移成功后立即使用 secondary HOME 进入新 Session。

  codex-merge primary secondary 01a0ad83-f4e -r
      与上一条等价的简写。

  codex-merge secondary default 01a01234
      迁移到默认 HOME；唯一的 Session ID 前缀即可。

注意:
  这是一次性 fork，不是实时双向同步。目标数据库会先备份到
  <TARGET>/backups/session-merge-*，并在 read/resume 验证通过后才报告成功。""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    fork_parser.add_argument(
        "source", metavar="SOURCE", help="源 HOME 名称或 CODEX_HOME 路径"
    )
    fork_parser.add_argument(
        "target", metavar="TARGET", help="目标 HOME 名称或 CODEX_HOME 路径"
    )
    fork_parser.add_argument(
        "session", metavar="SESSION", help="完整 Session UUID，或能够唯一匹配的 UUID 前缀"
    )
    fork_parser.add_argument(
        "-n", "--dry-run", action="store_true", help="仅预览和校验，不向目标 HOME 写入数据"
    )
    fork_parser.add_argument(
        "-r", "--resume", action="store_true", help="迁移成功后立即在目标 HOME 中运行 codex resume"
    )
    for child, positional_title in (
        (parser, "子命令"),
        (homes_parser, "位置参数"),
        (fork_parser, "位置参数"),
    ):
        child._positionals.title = positional_title
        child._optionals.title = "选项"
        for action in child._actions:
            if action.dest == "help":
                action.help = "显示此帮助信息并退出"
    return parser


def normalize_shorthand(argv: list[str]) -> list[str]:
    commands = {"homes", "fork", "-h", "--help"}
    if argv and not argv[0].startswith("-") and argv[0] not in commands and len(argv) >= 3:
        return ["fork", *argv]
    return argv


def main(argv: list[str] | None = None) -> int:
    raw_arguments = list(sys.argv[1:] if argv is None else argv)
    arguments = normalize_shorthand(raw_arguments)
    parser = build_parser()
    args = parser.parse_args(arguments)
    try:
        if args.command == "homes":
            show_homes()
            return 0
        source = resolve_home(args.source)
        target = resolve_home(args.target)
        child_id, backup, turns = share_session(
            source,
            target,
            args.session,
            args.codex_bin,
            dry_run=args.dry_run,
        )
        if args.dry_run:
            return 0
        print(f"forked session: {child_id}")
        print(f"target home:    {target}")
        print(f"verified turns: {turns}")
        print(f"backup:         {backup}")
        command = f"{launcher_for_home(target, args.codex_bin)} resume {child_id}"
        print(f"resume:         {command}")
        if args.resume:
            environment = os.environ.copy()
            environment["CODEX_HOME"] = str(target)
            os.execvpe(args.codex_bin, [args.codex_bin, "resume", child_id], environment)
        return 0
    except (ShareError, sqlite3.Error, OSError, KeyError) as exc:
        print(f"codex-merge: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
