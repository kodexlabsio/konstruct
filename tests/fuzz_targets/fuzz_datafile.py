"""Fuzz the readers of the data files a project ships (Parquet footers and CSV headers): for any
bytes in a tracked file, the columns come back as a list of names and types or as None, never an
exception, a hang or a crash, and are bounded: no more than the schema and header caps allow.

Run with ``uv run --with atheris==3.1.0 python tests/fuzz_targets/fuzz_datafile.py -max_total_time=60``; see
docs/development.md.
"""

from __future__ import annotations

import struct
import sys
import tempfile
import unicodedata
from pathlib import Path

import atheris

with atheris.instrument_imports(include=['konstruct']):
    from konstruct._core import data_file_columns

ROOT = Path(tempfile.mkdtemp(prefix='konstruct-fuzz-datafile-'))
MOST_COLUMNS = 100_000
TRACKED = {'x.parquet', 'x.csv', 'x.tsv'}
# A footer is a Thrift struct, so a mutating fuzzer takes long to reach one that parses: one that
# does is the seed, and the cases it could not build are read once at start (a map of 2^62
# booleans, a collection larger than the bytes left).
FOOTER = bytes.fromhex('1915' + '5c0405') + b'\x00' * 8
HOSTILE_FOOTERS = [
    bytes([0x1B]) + bytes.fromhex('ffffffffffffffff3f') + bytes([0x11]),
    bytes([0x19, 0xFC]) + bytes.fromhex('ffffffff0f') + b'\x00',
    bytes([0x29, 0xFC]) + bytes.fromhex('c0843d') + b'\x00' * 1_000_000,
]


def parquet(footer: bytes) -> bytes:
    return b'PAR1' + footer + struct.pack('<I', len(footer)) + b'PAR1'


def read(name: str, content: bytes, fmt: str, **options: object) -> None:
    (ROOT / name).write_bytes(content)
    found = data_file_columns(str(ROOT), name, fmt, TRACKED, **options)  # type: ignore[arg-type]
    if found is None:
        return
    path, columns = found
    assert path == name
    assert len(columns) <= MOST_COLUMNS
    for column, dtype in columns:
        assert isinstance(column, str) and isinstance(dtype, str)
        # A CSV header's names have no control characters, whatever the file holds.
        assert fmt == 'parquet' or all(unicodedata.category(c) != 'Cc' for c in column)


def test_one_input(data: bytes) -> None:
    provider = atheris.FuzzedDataProvider(data)
    kind = provider.ConsumeIntInRange(0, 3)
    rest = provider.ConsumeBytes(len(data))
    if kind == 0:
        read('x.parquet', parquet(rest), 'parquet')
    elif kind == 1:
        read('x.parquet', rest, 'parquet')
    elif kind == 2:
        read('x.csv', rest, 'csv', header=bool(len(rest) % 2))
    else:
        read('x.tsv', rest, 'csv', delimiter=rest[:1].decode('latin-1') or None)


if __name__ == '__main__':
    for footer in HOSTILE_FOOTERS:
        read('x.parquet', parquet(footer), 'parquet')
    read('x.parquet', parquet(FOOTER), 'parquet')
    read('x.csv', b','.join(b'a' for _ in range(4_000)) + b'\n', 'csv')
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()
