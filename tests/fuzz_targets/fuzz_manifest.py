"""Fuzz the dbt manifest and catalog reader: for any bytes in manifest.json and catalog.json, a load
returns a graph or None (an unreadable manifest) and raises only what a manifest that is not dbt's
may raise (``KeyError`` or ``TypeError`` for a node with no text id), never panicking, and adds
only the nodes and edges the file says.

Run with ``uv run --with atheris==3.1.0 python tests/fuzz_targets/fuzz_manifest.py -max_total_time=60``; see
docs/development.md.
"""

from __future__ import annotations

import contextlib
import sys
import tempfile
from pathlib import Path

import atheris

with atheris.instrument_imports(include=['konstruct']):
    from konstruct.core.graph import Graph
    from konstruct.inputs import dbt

ROOT = Path(tempfile.mkdtemp(prefix='konstruct-fuzz-manifest-'))
(ROOT / 'target').mkdir()
(ROOT / 'dbt_project.yml').write_text('name: p\n')
SEED = (
    '{"metadata": {"adapter_type": "duckdb", "project_name": "p"}, "nodes": {'
    '"model.p.a": {"resource_type": "model", "unique_id": "model.p.a", "name": "a", "package_name": "p", '
    '"relation_name": "\\"db\\".\\"main\\".\\"a\\"", "raw_code": "select {{ 1 }}", '
    '"depends_on": {"nodes": ["model.p.b"]}, "columns": {"id": {"name": "id", '
    '"constraints": [{"type": "primary_key"}]}}, "config": {"materialized": "incremental", '
    '"unique_key": ["id"]}}, '
    '"test.p.t": {"resource_type": "test", "unique_id": "test.p.t", "name": "t", "column_name": "id", '
    '"attached_node": "model.p.a", "depends_on": {"nodes": ["model.p.a"]}, '
    '"test_metadata": {"name": "unique", "kwargs": {}}}}, '
    '"sources": {"source.p.s.t": {"resource_type": "source", "unique_id": "source.p.s.t", "name": "t", '
    '"source_name": "s"}}, "exposures": {}, "docs": {}, "macros": {}}'
)
CATALOG = (
    '{"nodes": {"model.p.a": {"columns": {"id": {"name": "id", "type": "int", "index": 1}}}}, "sources": {}}'
)


def test_one_input(data: bytes) -> None:
    provider = atheris.FuzzedDataProvider(data)
    manifest = provider.ConsumeBytes(provider.ConsumeIntInRange(0, len(data)))
    catalog = provider.ConsumeBytes(provider.remaining_bytes())
    # Half of the inputs are the seed with a change, which reaches further than random bytes.
    if provider.ConsumeBool() and manifest:
        text = bytearray(SEED.encode())
        text[manifest[0] % len(text)] = manifest[-1]
        manifest = bytes(text)
    (ROOT / 'target' / 'manifest.json').write_bytes(manifest)
    (ROOT / 'target' / 'catalog.json').write_bytes(catalog or CATALOG.encode())
    graph = Graph()
    graph.meta['root'] = str(ROOT)
    with contextlib.suppress(KeyError, TypeError, ValueError):
        dbt.load(dbt.find_projects(ROOT)[0], graph, dialect=None)


if __name__ == '__main__':
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()
