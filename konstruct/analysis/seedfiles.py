"""A project's dbt seed files, checked and read without following anything out of the project.

``konstruct verify --conflicts`` and ``--why`` read a dbt seed from its CSV file when the
warehouse does not hold it. The seed's path comes from the project (``seed-paths`` in
``dbt_project.yml``, a manifest, or a committed ``graph.json``), so it is untrusted, and so are the
seed's options. This module is the one place such a file is opened:

* the path must be relative, inside the project, with no ``..`` and no component named ``.git``;
* each directory on the way is opened without following a symbolic link, from the one before it,
  and so is the file, which must be a regular file with no other hard link, of at most
  :data:`MAX_BYTES`, checked before it is opened (a FIFO is never opened) and again once open, so
  neither a link nor a file swapped in during the walk reads anything else;
* DuckDB never sees the project's path: the bytes are written to a private temporary directory
  and read there by a connection that may reach that directory alone, with access to other files,
  URLs and extensions off and its configuration locked. The rows are then copied into the
  caller's connection, which reads no file at all.

A seed's ``delimiter`` and ``column_types`` (from its dbt config) are honoured when they are
plain values; a seed that cannot be read is left out, with the reason.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from konstruct.core.graph import Graph

__all__ = ['MAX_BYTES', 'MAX_TOTAL_BYTES', 'SeedFile', 'load_seeds', 'read_seed', 'seed_files']

MAX_BYTES = 256 * 1024 * 1024
"""The largest seed file read."""
_TYPE = re.compile(r'[A-Za-z][A-Za-z0-9_ ]*(\(\s*\d+\s*(,\s*\d+\s*)?\))?')
"""A column type as dbt's ``column_types`` gives it: a name, with a precision and scale."""
MAX_TOTAL_BYTES = 512 * 1024 * 1024
"""The most bytes of seed files one :func:`load_seeds` copies, all seeds together."""
_SAFE_DIRECTORY = re.compile(r'[A-Za-z0-9_./-]+')
"""A directory DuckDB is given: no character it reads as a pattern."""
_SHORT_GIT = re.compile(r'git~\d+')
"""A short (8.3) name Windows can give ``.git``."""
_HFS_IGNORED = frozenset([*range(0x200C, 0x2010), *range(0x202A, 0x202F), *range(0x206A, 0x2070), 0xFEFF])
"""The code points HFS+ leaves out when it compares names, so that ``.g\u200cit`` is ``.git`` there
(the list git checks for the same reason)."""


@dataclass(frozen=True)
class SeedFile:
    """One dbt seed and its file."""

    relation: str
    """The relation the seed builds (``db.main.raw_orders``)."""
    path: str
    """Its file, relative to the project, with ``/`` between components."""
    delimiter: str = ','
    """The character between values."""
    column_types: Mapping[str, str] = field(default_factory=dict)
    """Columns whose type is set rather than inferred."""


def seed_files(graph: Graph, root: Path) -> dict[str, SeedFile]:
    """Each dbt seed of the graph whose CSV file is a regular file inside ``root``, by relation;
    a seed whose path is absolute, leaves the project, runs through ``.git`` or a symbolic link,
    or is not a regular file is left out."""
    found: dict[str, SeedFile] = {}
    for node in graph.nodes.values():
        if node.kind != 'seed' or not node.relation or not node.path or not node.path.endswith('.csv'):
            continue
        parts = _parts(node.path)
        if parts is None or not _is_regular(root, parts):
            continue
        found[node.relation] = SeedFile(
            node.relation, '/'.join(parts), _delimiter(node.attrs.get('delimiter')), _types(node.attrs)
        )
    return found


def read_seed(root: Path, relative: str) -> bytes:
    """The bytes of the file ``relative`` (``seeds/orders.csv``) inside ``root``, opened component
    by component without following a symbolic link.

    Raises:
        OSError: The path is not a regular file inside ``root`` reached without a link, or it is
            larger than :data:`MAX_BYTES`.
    """
    parts = _parts(relative)
    if parts is None:
        raise OSError(f'{relative} is not a path inside the project')
    descriptor = _open(root, parts)
    try:
        info = os.fstat(descriptor)
        if info.st_size > MAX_BYTES:
            raise OSError(f'{relative} is larger than {MAX_BYTES // (1024 * 1024)} MB')
        chunks = []
        remaining = MAX_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b''.join(chunks)
        if len(data) > MAX_BYTES:
            raise OSError(f'{relative} is larger than {MAX_BYTES // (1024 * 1024)} MB')
        return data
    finally:
        os.close(descriptor)


def load_seeds(
    con: Any,
    root: Path,
    seeds: Mapping[str, SeedFile],
    *,
    schema: str = 'kp_seed',
    memory_limit: str | None = None,
    directory: Path | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Read ``seeds`` into tables of ``schema`` in the DuckDB connection ``con``, which must not
    be locked yet. Returns the table each seed was read into, by relation, and the reason each
    seed that could not be read was left out.

    ``directory`` is an empty private directory to work in, removed afterwards; by default a new
    temporary one. ``con`` attaches its ``seeds.duckdb`` (:func:`seed_database`), so a connection
    whose external access is already off must allow that path.

    The connection never opens a file of the project: see the module docstring."""
    import duckdb

    tables: dict[str, str] = {}
    skipped: dict[str, str] = {}
    if not seeds:
        return tables, skipped
    private = (directory or Path(tempfile.mkdtemp(prefix='konstruct-seeds-'))).resolve()
    try:
        if not _SAFE_DIRECTORY.fullmatch(str(private)):
            raise OSError(f'the temporary directory {private} has characters DuckDB reads as a pattern')
        files: list[tuple[str, SeedFile, Path]] = []
        copied = 0
        for index, (relation, seed) in enumerate(sorted(seeds.items())):
            try:
                data = read_seed(root, seed.path)
            except OSError as error:
                skipped[relation] = str(error)
                continue
            if copied + len(data) > MAX_TOTAL_BYTES:
                # Each copy is written to a temporary directory: together they may not fill a disk.
                skipped[relation] = (
                    f'the seeds together are larger than {MAX_TOTAL_BYTES // (1024 * 1024)} MB'
                )
                continue
            copied += len(data)
            copy = private / f's{index}.csv'
            copy.write_bytes(data)
            files.append((f's{index}', seed, copy))
        database = seed_database(private)
        reader = duckdb.connect(str(database))
        try:
            if memory_limit is not None:
                reader.execute(f'SET memory_limit = {_literal(memory_limit)}')
            reader.execute(f'SET allowed_directories = [{_literal(str(private) + "/")}]')
            for setting in (
                'enable_external_access',
                'autoinstall_known_extensions',
                'autoload_known_extensions',
            ):
                reader.execute(f'SET {setting} = false')
            reader.execute('SET lock_configuration = true')
            read: list[tuple[str, str]] = []
            for table, seed, copy in files:
                options = ['header = true', f'delim = {_literal(seed.delimiter)}']
                if seed.column_types:
                    typed = ', '.join(
                        f'{_literal(c)}: {_literal(t)}' for c, t in sorted(seed.column_types.items())
                    )
                    options.append(f'types = {{{typed}}}')
                try:
                    reader.execute(
                        f'CREATE TABLE {table} AS SELECT * FROM read_csv({_literal(str(copy))}, {", ".join(options)})'
                    )
                except duckdb.Error as error:
                    skipped[seed.relation] = str(error).splitlines()[0]
                    continue
                read.append((table, seed.relation))
        finally:
            reader.close()
        if not read:
            return tables, skipped
        con.execute(f'ATTACH {_literal(str(database))} AS kp_seedfile (READ_ONLY)')
        try:
            con.execute(f'CREATE SCHEMA IF NOT EXISTS {schema}')
            for table, relation in read:
                con.execute(f'CREATE TABLE {schema}.{table} AS SELECT * FROM kp_seedfile.main.{table}')
                tables[relation] = f'{schema}.{table}'
        finally:
            con.execute('DETACH kp_seedfile')
    finally:
        shutil.rmtree(private, ignore_errors=True)
    return tables, skipped


def seed_database(directory: Path) -> Path:
    """The database :func:`load_seeds` writes the seeds to in ``directory``, and attaches."""
    return directory.resolve() / 'seeds.duckdb'


def _parts(relative: str) -> list[str] | None:
    """The components of a project-relative path, or None for one that is absolute, leaves the
    project or runs through ``.git``. Each component is also read as Windows reads it (without
    trailing dots and spaces or a stream after ``:``, and ``GIT~1`` as the short name of ``.git``)
    and as HFS+ reads it (without the code points it ignores), so no spelling of ``.git`` or ``..``
    passes on any system."""
    if not relative or '\\' in relative or '\0' in relative:
        return None
    path = PurePosixPath(relative)
    if path.is_absolute() or (len(relative) > 1 and relative[1] == ':'):
        return None
    parts = [p for p in path.parts if p not in ('', '.')]
    if not parts:
        return None
    for part in parts:
        read = ''.join(c for c in part if ord(c) not in _HFS_IGNORED).split(':', 1)[0].rstrip('. ').lower()
        if read in ('', '.git') or _SHORT_GIT.fullmatch(read):
            return None
    return parts


def _is_regular(root: Path, parts: list[str]) -> bool:
    try:
        descriptor = _open(root, parts)
    except OSError:
        return False
    os.close(descriptor)
    return True


def _open(root: Path, parts: list[str]) -> int:
    """A descriptor of the regular file ``root/parts``, each component opened from the one before
    without following a link. The file is checked before it is opened, so a FIFO or a device is
    never opened (which could block), and again once open, so nothing swapped in between is read;
    a file with another hard link is refused, as it may be a file of ``.git`` under another name.

    Raises:
        OSError: The path is not a regular file reached without a link, or has another link.
    """
    nofollow = getattr(os, 'O_NOFOLLOW', 0)
    directory = getattr(os, 'O_DIRECTORY', 0)
    nonblock = getattr(os, 'O_NONBLOCK', 0)
    if os.open in os.supports_dir_fd and nofollow:
        current = os.open(root, os.O_RDONLY | directory)
        try:
            for part in parts[:-1]:
                following = os.open(part, os.O_RDONLY | directory | nofollow, dir_fd=current)
                os.close(current)
                current = following
            before = os.stat(parts[-1], dir_fd=current, follow_symlinks=False)
            _regular(before, parts)
            return _same(os.open(parts[-1], os.O_RDONLY | nofollow | nonblock, dir_fd=current), before, parts)
        finally:
            os.close(current)
    # Where a descriptor cannot open its children (Windows), each component is checked with
    # lstat first; a link or a junction anywhere on the way is refused.
    path = Path(root)
    info = None
    for part in parts:
        path = path / part
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_reparse_tag', 0)):
            raise OSError(f'{path} is a link')
    assert info is not None
    _regular(info, parts)
    return _same(os.open(path, os.O_RDONLY | getattr(os, 'O_BINARY', 0) | nonblock), info, parts)


def _regular(info: os.stat_result, parts: list[str]) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise OSError(f'{"/".join(parts)} is not a regular file')
    if info.st_nlink > 1:
        raise OSError(f'{"/".join(parts)} has another hard link')


def _same(descriptor: int, before: os.stat_result, parts: list[str]) -> int:
    """``descriptor``, when it is the file ``before`` described; else it is closed and refused."""
    try:
        info = os.fstat(descriptor)
        _regular(info, parts)
        if (info.st_ino, info.st_dev) != (before.st_ino, before.st_dev):
            raise OSError(f'{"/".join(parts)} changed while it was opened')
    except OSError:
        os.close(descriptor)
        raise
    return descriptor


def _delimiter(value: object) -> str:
    return value if isinstance(value, str) and len(value) == 1 and value not in '\n\r\0' else ','


def _types(attrs: Mapping[str, object]) -> dict[str, str]:
    value = attrs.get('column_types')
    if not isinstance(value, Mapping):
        return {}
    return {
        str(k): str(v)
        for k, v in value.items()
        if isinstance(k, str) and isinstance(v, str) and _TYPE.fullmatch(v.strip())
    }


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
