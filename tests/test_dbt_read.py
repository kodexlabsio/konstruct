"""Reading a dbt manifest and catalog into a graph: the Rust reader against the Python one it
replaced (``tests/dbt_oracle.py``). Each project is loaded both ways, and the two graphs must be
the same down to the bytes of graph.json."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from konstruct.core.graph import Graph
from konstruct.core.sql import DIALECTS as DIALECT_NAMES
from konstruct.inputs import dbt
from tests import dbt_oracle
from tests.conftest import copy_example, dbt_executable, run_dbt
from tests.test_jinja import write_dbt_project
from tests.test_scale import make_project


def load_both(root: Path, tmp_path: Path, dialect: str | None = None) -> tuple[Graph, Graph]:
    """The project at ``root`` loaded by the Rust reader and by the oracle; asserts they agree on
    the graph's file and on everything the build reads from the load."""
    project = dbt.find_projects(root)[0]
    graphs, loads = [], []
    for load in (dbt.load, dbt_oracle.load):
        graph = Graph()
        graph.meta['root'] = str(root)
        loads.append(load(project, graph, dialect=dialect))
        graphs.append(graph)
    new, old = graphs
    assert loads[0] is not None and loads[1] is not None
    assert loads[0] == loads[1]
    assert new.to_dict() == old.to_dict()
    assert list(new.nodes) == list(old.nodes)
    assert new.warnings == old.warnings
    assert new.meta == old.meta
    new.write(tmp_path / 'new.json')
    old.write(tmp_path / 'old.json')
    assert (tmp_path / 'new.json').read_bytes() == (tmp_path / 'old.json').read_bytes()
    return new, old


def test_the_scale_project_loads_as_it_did(tmp_path: Path) -> None:
    root = tmp_path / 'project'
    root.mkdir()
    make_project(root, models=60, columns_per_model=6)
    new, _ = load_both(root, tmp_path)
    assert sum(1 for n in new.nodes.values() if n.kind == 'column') > 300


@pytest.mark.dbt
def test_jaffle_shop_with_its_catalog_loads_as_it_did(jaffle_with_artefacts: Path, tmp_path: Path) -> None:
    new, _ = load_both(jaffle_with_artefacts, tmp_path)
    assert any(n.kind == 'exposure' for n in new.nodes.values())
    assert any(n.kind == 'test' for n in new.nodes.values())


@pytest.mark.dbt
def test_a_project_dbt_only_parsed_loads_as_it_did(tmp_path: Path) -> None:
    if dbt_executable() is None:
        pytest.skip('dbt is not installed')
    project = copy_example(tmp_path)
    run_dbt(project, 'parse')
    (tmp_path / 'jaffle-out').mkdir()
    load_both(project, tmp_path / 'jaffle-out')
    generated = tmp_path / 'generated'
    write_dbt_project(generated, models=40)
    run_dbt(generated, 'parse')
    (tmp_path / 'generated-out').mkdir()
    load_both(generated, tmp_path / 'generated-out')


def edge_cases() -> tuple[dict[str, Any], dict[str, Any]]:
    """A manifest and a catalog with what the others lack: constraints, documented-only columns,
    columns whose case differs, every test shape, exposures, sources with a loader and an identifier,
    incremental keys, seeds and snapshots, models in other languages, and quoting in every style."""

    def model(name: str, **extra: Any) -> dict[str, Any]:
        return {
            'resource_type': 'model',
            'unique_id': f'model.p.{name}',
            'name': name,
            'package_name': 'p',
            'fqn': ['p', name],
            'tags': ['t'],
            'database': 'db',
            'schema': 'main',
            'relation_name': f'"db"."main"."{name}"',
            'original_file_path': f'models/{name}.sql',
            'checksum': {'name': 'sha256', 'checksum': name},
            'config': {'materialized': 'table'},
            'depends_on': {'nodes': [], 'macros': []},
            'raw_code': f'select 1 as a from {{{{ ref("base") }}}} -- {name}',
            **extra,
        }

    nodes: dict[str, Any] = {
        'model.p.base': model('base', compiled_code='select 1 as a', description='  The base.  '),
        'model.p.incremental': model(
            'incremental',
            config={'materialized': 'incremental', 'unique_key': ['a', 'b'], 'incremental_strategy': 'merge'},
            depends_on={'nodes': ['model.p.base', 'macro.p.helper', 'source.p.raw.events'], 'macros': []},
            alias='inc',
        ),
        'model.p.scalar_key': model('scalar_key', config={'materialized': 'incremental', 'unique_key': 'id'}),
        'model.p.uncompiled': model(
            'uncompiled', raw_code="{{ config(materialized='view') }}select {{ var('x', 1) }}"
        ),
        'model.p.broken': model('broken', raw_code='{% if %}select 1'),
        'model.p.python': model('python', language='python', raw_code='def model(dbt, session): ...'),
        'model.p.no_relation': model('no_relation', relation_name=None, alias='aliased', database=None),
        'model.p.no_sql': model('no_sql', raw_code=None),
        'model.p.pk': model(
            'pk',
            constraints=[{'type': 'primary_key', 'columns': ['id', 7]}],
            config={'materialized': 'table', 'contract': {'enforced': True}},
            columns={
                'id': {'name': 'id', 'description': ' The key ', 'data_type': 'int'},
                'Amount': {'description': 'Documented only', 'data_type': 'numeric'},
                'AMOUNT': {'name': 'amount_again', 'description': 'Same lower-case name'},
                'ignored': 'not a dict',
            },
            compiled_code='select 1',
        ),
        'model.p.column_pk': model(
            'column_pk', columns={'k': {'constraints': [{'type': 'primary_key'}], 'name': 'K'}}
        ),
        'seed.p.countries': {
            'resource_type': 'seed',
            'unique_id': 'seed.p.countries',
            'name': 'countries',
            'package_name': 'p',
            'relation_name': '"db"."main"."countries"',
            'original_file_path': 'seeds/countries.csv',
            'config': {'materialized': 'seed'},
        },
        'snapshot.p.snap': {
            'resource_type': 'snapshot',
            'unique_id': 'snapshot.p.snap',
            'name': 'snap',
            'package_name': 'p',
            'relation_name': '"db"."snapshots"."snap"',
            'original_file_path': 'snapshots/snap.sql',
            'raw_code': 'select * from {{ source("raw", "events") }}',
            'config': {},
            'tags': [],
        },
        'model.p.bq': model('bq', relation_name='`proj-1`.`ds`.`Tbl`', database='proj-1', schema='ds'),
        'model.p.tsql': model('tsql', relation_name='[Db].[dbo].[T]', database='Db', schema='dbo'),
        'test.p.unique_base_a': {
            'resource_type': 'test',
            'unique_id': 'test.p.unique_base_a',
            'name': 'unique_base_a',
            'original_file_path': 'models/schema.yml',
            'column_name': 'a',
            'attached_node': 'model.p.base',
            'depends_on': {'nodes': ['model.p.base']},
            'test_metadata': {'name': 'unique', 'kwargs': {}},
            'config': {'severity': 'warn', 'where': 'a > 0'},
        },
        'test.p.relationships': {
            'resource_type': 'test',
            'unique_id': 'test.p.relationships',
            'name': 'relationships_incremental_a',
            'column_name': 'a',
            'depends_on': {'nodes': ['model.p.incremental', 'model.p.base']},
            'test_metadata': {'name': 'relationships', 'kwargs': {'field': 'a', 'to': "ref('base')"}},
            'config': {},
        },
        'test.p.missing_column': {
            'resource_type': 'test',
            'unique_id': 'test.p.missing_column',
            'name': 'missing_column',
            'column_name': 'nope',
            'depends_on': {'nodes': ['model.p.base']},
            'test_metadata': {'name': 'not_null', 'kwargs': {}},
        },
        'test.p.combination': {
            'resource_type': 'test',
            'unique_id': 'test.p.combination',
            'name': 'combination',
            'depends_on': {'nodes': ['model.p.base']},
            'test_metadata': {
                'name': 'unique_combination_of_columns',
                'kwargs': {'combination_of_columns': ['a', 'b']},
            },
        },
        'test.p.singular': {
            'resource_type': 'test',
            'unique_id': 'test.p.singular',
            'name': 'singular',
            'depends_on': {'nodes': ['model.p.base', 'model.p.pk']},
        },
        'analysis.p.skipped': {
            'resource_type': 'analysis',
            'unique_id': 'analysis.p.skipped',
            'name': 'skipped',
        },
    }
    manifest = {
        'metadata': {'adapter_type': 'duckdb', 'project_name': 'p', 'invocation_id': 'abc-123'},
        'nodes': nodes,
        'sources': {
            'source.p.raw.events': {
                'resource_type': 'source',
                'unique_id': 'source.p.raw.events',
                'name': 'events',
                'identifier': 'raw_events',
                'source_name': 'raw',
                'package_name': 'p',
                'database': 'db',
                'schema': 'raw',
                'relation_name': '"db"."raw"."raw_events"',
                'loader': 'fivetran',
                'description': None,
                'source_description': ' The raw events ',
                'original_file_path': 'models/sources.yml',
                'columns': {'id': {'name': 'id', 'data_type': 'bigint', 'description': 'Key'}},
            },
            'source.p.raw.plain': {
                'resource_type': 'source',
                'unique_id': 'source.p.raw.plain',
                'name': 'plain',
                'source_name': 'raw',
                'package_name': 'p',
                'schema': 'raw',
                'loader': '',
            },
        },
        'exposures': {
            'exposure.p.dash': {
                'resource_type': 'exposure',
                'unique_id': 'exposure.p.dash',
                'name': 'dash',
                'type': 'dashboard',
                'label': 'Dash',
                'maturity': None,
                'owner': {'name': 'Ada', 'email': 'ada@example.com'},
                'url': 'https://example.com',
                'description': 'A dashboard',
                'depends_on': {'nodes': ['model.p.base', 'model.p.pk']},
                'original_file_path': 'models/exposures.yml',
            },
            'exposure.p.bare': {'resource_type': 'exposure', 'unique_id': 'exposure.p.bare', 'name': 'bare'},
        },
        'docs': {'doc.p.__overview__': {'block_contents': '\n  The overview.\n'}},
        'semantic_models': {
            'semantic_model.p.orders': {
                'depends_on': {'nodes': ['model.p.base']},
                'measures': [{'name': 'revenue', 'agg': 'sum', 'expr': 'a'}],
            }
        },
        'metrics': {
            'metric.p.revenue': {
                'name': 'revenue',
                'label': 'Revenue',
                'type': 'simple',
                'type_params': {'measure': {'name': 'revenue'}},
                'original_file_path': 'models/metrics.yml',
            }
        },
    }
    catalog = {
        'nodes': {
            'model.p.base': {
                'columns': {
                    'B': {'name': 'b', 'type': 'text', 'index': 2, 'comment': ' second '},
                    'A': {'name': 'a', 'type': 'integer', 'index': 1},
                    'C': {'name': 'c', 'type': None, 'comment': ''},
                    'nameless': {'type': 'x', 'index': 9},
                }
            },
            'model.p.pk': {'columns': {'id': {'name': 'ID', 'type': 'bigint', 'index': 1}}},
            'model.p.empty': {},
        },
        'sources': {
            'source.p.raw.events': {'columns': {'id': {'name': 'Id', 'type': None, 'index': 1}}},
        },
    }
    return manifest, catalog


@pytest.mark.parametrize('dialect', [None, 'duckdb', 'bigquery', 'tsql', 'snowflake'])
def test_every_shape_of_manifest_loads_as_it_did(tmp_path: Path, dialect: str | None) -> None:
    manifest, catalog = edge_cases()
    root = tmp_path / 'project'
    (root / 'target').mkdir(parents=True)
    (root / 'dbt_project.yml').write_text('name: p\n')
    (root / 'target' / 'manifest.json').write_text(json.dumps(manifest))
    (root / 'target' / 'catalog.json').write_text(json.dumps(catalog))
    (root / 'macros').mkdir()
    (root / 'macros' / 'm.sql').write_text('{% macro helper() %}1{% endmacro %}')
    out = tmp_path / 'out'
    out.mkdir()
    new, _ = load_both(root, out, dialect)
    assert new.nodes['model.p.pk'].attrs['primary_key'] == ['id', '7']
    assert new.meta['overviews'] == {'p': 'The overview.'}


def test_without_a_catalog_the_documented_columns_are_not_marked(tmp_path: Path) -> None:
    manifest, _ = edge_cases()
    root = tmp_path / 'project'
    (root / 'target').mkdir(parents=True)
    (root / 'dbt_project.yml').write_text('name: p\n')
    (root / 'target' / 'manifest.json').write_text(json.dumps(manifest))
    out = tmp_path / 'out'
    out.mkdir()
    new, _ = load_both(root, out)
    assert not any(n.attrs.get('documented_only') for n in new.nodes.values())


def test_an_empty_catalog_is_a_catalog_that_lists_nothing(tmp_path: Path) -> None:
    manifest, _ = edge_cases()
    root = tmp_path / 'project'
    (root / 'target').mkdir(parents=True)
    (root / 'dbt_project.yml').write_text('name: p\n')
    (root / 'target' / 'manifest.json').write_text(json.dumps(manifest))
    (root / 'target' / 'catalog.json').write_text('{}')
    out = tmp_path / 'out'
    out.mkdir()
    load_both(root, out)


@pytest.mark.parametrize('content', [None, '', 'not json', '[1, 2]', '{"nodes": '])
def test_an_unreadable_manifest_is_a_warning_and_no_load(tmp_path: Path, content: str | None) -> None:
    (tmp_path / 'target').mkdir()
    (tmp_path / 'dbt_project.yml').write_text('name: p\n')
    if content is not None:
        (tmp_path / 'target' / 'manifest.json').write_text(content)
    graph = Graph()
    assert dbt.load(dbt.find_projects(tmp_path)[0], graph, dialect=None) is None
    assert graph.warnings == [f'dbt: p: no readable manifest at {tmp_path / "target" / "manifest.json"}']


NAMES = [
    '"db"."main"."stg"',
    '`p`.`d`.`t`',
    '[db].[s].[t]',
    'my-proj.ds.t',
    '"a.b"."c"',
    '"a""b".c',
    'x',
    '"X"',
    'a..b',
    '"db"."s"."t" ',
    'DB.S.T',
    'db.s.Ünï',
    '`a.b`.c.d',
    '"a.b".c.d',
    'a.b.c.d',
    'a.b.c.d.e',
    'a.b.1z.c',
    '0."x"."y"',
    '"a".0.b',
    '1.2.3',
    '"unclosed.t',
]
DIALECTS = [None, *sorted(set(DIALECT_NAMES.values()))]


def fixture_names() -> list[str]:
    """Every relation name the fixtures of the tests write."""
    from tests.jinja_corpus import MANIFEST

    manifest, _ = edge_cases()
    names = list(NAMES)
    for group in (MANIFEST['nodes'], MANIFEST['sources'], manifest['nodes'], manifest['sources']):
        names += [item['relation_name'] for item in group.values() if item.get('relation_name')]
    names += [
        '"jaffle_shop"."main"."orders"',
        '"wh"."analytics"."m001"',
        '"memory"."main_staging"."stg_orders"',
    ]
    return names


@pytest.mark.parametrize('dialect', DIALECTS)
def test_relation_names_of_the_fixtures_are_the_keys_the_sql_parser_gave(dialect: str | None) -> None:
    for name in fixture_names():
        assert dbt.parse_relation_name(name, dialect) == dbt_oracle.parse_relation_name(name, dialect), (
            name,
            dialect,
        )


BACKTICKED = {'bigquery', 'spark', 'databricks', 'mysql', 'clickhouse'}
# A name does not start with `$`, which is a parameter to some parsers, and is not only digits,
# which sqlglot reads as a number after a quoted part, and does not start with `0`, which
# BigQuery reads as hexadecimal (`0Xa`).
UNQUOTED = st.builds(
    lambda first, middle, last: first + middle + last,
    st.sampled_from('abcXYZ_19'),
    st.text(alphabet='abcXYZ_019$', max_size=4),
    st.sampled_from('abcXYZ_'),
)
LETTERED = st.builds(
    lambda first, rest: first + rest, st.sampled_from('abcXYZ_'), st.text(alphabet='abcXYZ_019$', max_size=4)
)
# A quoted part holds dots, and neither starts nor ends with one.
QUOTED = st.builds(
    lambda first, middle, last: first + middle + last,
    st.sampled_from('abXY_ -'),
    st.text(alphabet='abXY_ -.', max_size=4),
    st.sampled_from('abXY_ -'),
)


@st.composite
def relation_names(draw: st.DrawFn) -> tuple[str, str | None]:
    """A name of one to four parts, each plain or quoted as its dialect quotes (with dots inside the
    quotes), in a dialect Konstruct supports."""
    dialect = draw(st.sampled_from(DIALECTS))
    quotes = '`' if dialect in BACKTICKED else ('[]' if dialect == 'tsql' and draw(st.booleans()) else '"')
    count = draw(st.integers(1, 4))
    quoted = [draw(st.booleans()) for _ in range(count)]
    # sqlglot lexes a part that starts with a digit, beside a quoted part, by rules that Konstruct
    # does not copy: such a name is read as it is written. Names that mix them are not generated.
    unquoted = LETTERED if any(quoted) else UNQUOTED
    parts = [quotes[0] + draw(QUOTED) + quotes[-1] if q else draw(unquoted) for q in quoted]
    return '.'.join(parts), dialect


@settings(max_examples=int(os.environ.get('KONSTRUCT_RELATION_EXAMPLES', '600')), deadline=None)
@given(relation_names())
def test_generated_relation_names_are_the_keys_the_sql_parser_gave(case: tuple[str, str | None]) -> None:
    name, dialect = case
    assert dbt.parse_relation_name(name, dialect) == dbt_oracle.parse_relation_name(name, dialect), case
