"""``konstruct conform``: platform semantic layers against what the code computes.

The proof rests on layers other people authored (tests/fixtures/conform/README.md says where each
comes from), with divergences planted in them, each of which must be found exactly once. The
round trip from Konstruct's own exports is a regression check, not the evidence.
"""

from __future__ import annotations

import itertools
import json
import sys
import types
from pathlib import Path
from typing import Any

import duckdb
import pytest

from konstruct import Project, QueryError, _core
from konstruct.core.formats import document
from konstruct.core.graph import Graph, Node
from konstruct.inputs import warehouse
from konstruct.inputs.warehouse import read_layers
from konstruct.interfaces.project import Conformance
from konstruct.interfaces.project import conform_layers as conform
from konstruct.semantics.ontology import derive

FIXTURES = Path(__file__).parent / 'fixtures' / 'conform'
TPCH = 'snowflake_sample_data.tpch_sf1'
COLUMNS = {
    'orders': ['o_orderkey', 'o_custkey', 'o_totalprice', 'o_orderdate', 'o_orderstatus', 'o_comment'],
    'customer': ['c_custkey', 'c_name', 'c_mktsegment', 'c_nationkey'],
    'lineitem': ['l_orderkey', 'l_linenumber', 'l_extendedprice', 'l_discount', 'l_quantity'],
}


def tpch(
    *,
    term_filter: str | None = None,
    status: str = 'approved',
    omit: str | None = None,
    prefix: str = TPCH,
    line_items: bool = False,
) -> Graph:
    """TPC-H sources, their primary keys, the foreign key a test states, and an approved term for
    the order average value, with a required filter when ``term_filter`` is given. With
    ``line_items``, the line items have a key of their own and a test says each has one order."""
    graph = Graph()
    graph.meta['dialect'] = 'snowflake'
    keys = {'orders': ['o_orderkey'], 'customer': ['c_custkey']}
    if line_items:
        keys['lineitem'] = ['l_lineid']
    for name, columns in COLUMNS.items():
        node = graph.add_node(
            Node(
                f'source.tpch.{name}',
                'source',
                name,
                relation=f'{prefix}.{name}',
                attrs={'primary_key': keys[name]} if name in keys else {},
            )
        )
        for column in [*columns, *(['l_lineid'] if line_items and name == 'lineitem' else [])]:
            if column != omit:
                graph.add_column(node, column)

    def test(name: str, kind: str, column: str, target: str | None = None) -> None:
        node = graph.add_node(Node(f'test.tpch.{name}', 'test', name, attrs={'test': kind}))
        graph.add_edge(node.id, column, 'tests')
        if target is not None:
            graph.add_edge(node.id, target, 'tests', role='relationship target')

    test('unique_orders', 'unique', 'source.tpch.orders#o_orderkey')
    test('unique_customer', 'unique', 'source.tpch.customer#c_custkey')
    test('orders_customer', 'relationships', 'source.tpch.orders#o_custkey', 'source.tpch.customer#c_custkey')
    if line_items:  # each line item has one order
        test(
            'lineitem_orders',
            'relationships',
            'source.tpch.lineitem#l_orderkey',
            'source.tpch.orders#o_orderkey',
        )
    attrs: dict[str, Any] = {'status': status, 'line': 3}
    if term_filter:
        attrs['filters'] = [term_filter]
    term = graph.add_node(
        Node('term.order_average_value', 'term', 'Order average value', path='definitions.yml', attrs=attrs)
    )
    graph.add_edge(term.id, 'source.tpch.orders#o_totalprice', 'defines')
    return graph


def layers(*names: str) -> list[tuple[str, dict[str, Any]]]:
    found: list[tuple[str, dict[str, Any]]] = []
    for name in names:
        read, problems = read_layers((FIXTURES / name).read_text(), str(FIXTURES / name))
        assert not problems, problems
        found += read
    return found


def check(graph: Graph, *names: str) -> Conformance:
    return conform(graph, derive(graph), layers(*names))


def verdicts(result: Conformance, *, kind: str | None = None) -> dict[str, str]:
    return {f['name']: f['verdict'] for f in result.findings if kind in (None, f['kind'])}


# Foreign layers, read


def test_the_snowflake_reference_example_is_read_in_full() -> None:
    [(_, doc)] = layers('snowflake_reference.sql')
    assert doc['name'] == 'tpch_rev_analysis'
    assert [t['name'] for t in doc['tables']] == ['orders', 'customers', 'line_items']
    assert doc['tables'][2]['primary_key'] == ['l_orderkey', 'l_linenumber']
    assert {m['name'] for m in doc['metrics']} == {
        'customer_count',
        'order_average_value',
        'average_line_items_per_order',
    }
    assert doc['tables'][0]['synonyms'] == ['sales orders']
    first = doc['relationships'][0]
    assert (first['name'], first['from'], first['to']) == (
        'orders_to_customers',
        'orders',
        'customers',
    )


def test_the_databricks_reference_example_is_read_with_its_filter_and_join() -> None:
    [(_, doc)] = layers('databricks_reference.yaml')
    assert doc['filter'] == "o_orderstatus = 'F'"
    assert [t['name'] for t in doc['tables']] == ['source', 'customer']
    assert doc['relationships'][0]['from_columns'] == ['o_custkey']
    assert [m['name'] for m in doc['metrics']] == ['Total revenue', 'Revenue for open orders']


def test_a_metric_view_in_its_statement_form_is_read() -> None:
    text = 'CREATE OR REPLACE VIEW main.s.orders_mv WITH METRICS LANGUAGE YAML AS $$\nversion: 1.1\nsource: main.s.orders\nmeasures:\n  - name: n\n    expr: COUNT(1)\n$$;'
    [(_, doc)] = read_layers(text, 'orders.sql')[0]
    assert (doc['platform'], doc['name'], doc['metrics'][0]['expr']) == (
        'databricks',
        'orders_mv',
        'COUNT(1)',
    )


def test_snowflake_model_yaml_keeps_its_verified_queries() -> None:
    [(_, doc)] = layers('snowflake_model.yaml')
    [query] = doc['verified_queries']
    assert (query['name'], query['verified_by'], query['verified_at']) == (
        'revenue_by_status',
        'priya',
        '1700000000',
    )
    assert doc['tables'][0]['source'] == 'SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.ORDERS'


def test_a_file_that_is_no_layer_is_reported_not_guessed(tmp_path: Path) -> None:
    layers_, problems = read_layers('just: [text]', 'x.yaml')
    assert layers_ == []
    assert 'not a Snowflake semantic model' in problems[0]
    assert read_layers('CREATE SEMANTIC VIEW v TABLES (t AS', 'v.sql')[1]
    assert read_layers('- a\n- b\n', 'l.yaml')[1]
    assert read_layers('a: [unclosed', 'bad.yaml')[1]


# Planted divergences, each found exactly once


def test_a_required_filter_the_view_drops_is_missing_against_the_approved_definition() -> None:
    result = check(tpch(term_filter="o_orderstatus = 'F'"), 'snowflake_reference.sql')
    [found] = [f for f in result.findings if f['verdict'] == 'differs']
    assert found['name'] == 'order_average_value'
    # The approved term is the metric's by name, so a missing filter is an error.
    assert found['severity'] == 'error'
    assert found['missing_filters'] == [
        {'filter': "o_orderstatus = 'F'", 'against': 'approved definition', 'by': 'Order average value'}
    ]
    assert found['against'][0]['grade'] == 'columns+name'
    assert found['code'] == 'AVG(orders.o_totalprice)'
    assert "missing filter: o_orderstatus = 'F' (approved definition: Order average value)" in str(result)


def test_filters_that_hold_never_make_a_metric_conform() -> None:
    graph = tpch(term_filter="o_orderstatus = 'F'", prefix='samples.tpch')
    graph.nodes['term.order_average_value'].name = 'Total revenue'
    found = next(
        f for f in check(graph, 'databricks_reference.yaml').findings if f['name'] == 'Total revenue'
    )
    # Identity compared no formula, so there is nothing to conform to and no error bound.
    assert found['verdict'] == 'unbound'
    assert found['detail'] == 'its required filters hold; no formula to compare'
    assert 'error_bound' not in found


def test_two_layers_that_compute_a_metric_alike_conform_with_the_identity_error_bound() -> None:
    text = (
        'CREATE SEMANTIC VIEW again TABLES (o AS snowflake_sample_data.tpch_sf1.orders) '
        'METRICS (o.order_average_value AS AVG(o.o_totalprice));'
    )
    documents = layers('snowflake_reference.sql') + read_layers(text, 'again.sql')[0]
    graph = tpch()
    result = conform(graph, derive(graph), documents)
    found = next(
        f for f in result.findings if f['document'].startswith('again.sql') and f['kind'] == 'metric'
    )
    assert found['verdict'] == 'conforms'
    assert 0 < found['error_bound'] < 1e-30
    assert not result.failing


def test_a_view_level_filter_holds_the_required_filter() -> None:
    graph = tpch(term_filter="o_orderstatus = 'F'", prefix='samples.tpch')
    graph.nodes['term.order_average_value'].name = 'Total revenue'
    result = check(graph, 'databricks_reference.yaml')
    assert verdicts(result, kind='metric')['Total revenue'] == 'unbound'
    assert (
        'required filters hold' in next(f for f in result.findings if f['name'] == 'Total revenue')['detail']
    )


def test_a_filter_dropped_by_a_tmdl_import_is_found() -> None:
    graph = tpch(term_filter="o_orderstatus = 'F'", prefix='samples.tpch')
    graph.nodes['term.order_average_value'].name = 'Fulfilled revenue'
    result = check(graph, 'tmdl_import_metric_view.yaml')
    [found] = [f for f in result.findings if f['verdict'] == 'differs']
    assert found['name'] == 'Fulfilled revenue'
    assert found['missing_filters'][0]['filter'] == "o_orderstatus = 'F'"


def test_a_product_of_sums_differs_from_a_sum_of_products_with_a_witness() -> None:
    graph = tpch()
    text = (
        'CREATE SEMANTIC VIEW lines_sv TABLES (line_items AS snowflake_sample_data.tpch_sf1.lineitem) '
        'METRICS (line_items.line_value AS SUM(line_items.l_extendedprice) * SUM(line_items.l_quantity));'
    )
    documents = layers('ossie_orders.ossie.yaml') + read_layers(text, 'lines.sql')[0]
    result = conform(graph, derive(graph), documents)
    [found] = [f for f in result.findings if f['kind'] == 'pair']
    assert found['severity'] == 'error' and found['grade'] == 'columns+name'
    assert {layer['document'].rsplit('/', 1)[-1] for layer in found['layers']} == {
        'ossie_orders.ossie.yaml#lines',
        'lines.sql#lines_sv',
    }
    assert found['verdict_detail']['equal'] is False
    assert found['verdict_detail']['witness']['kind'] == 'values'
    assert {'6', '12'} <= set(found['witness'].replace(':', ' ').split())
    # Neither layer is the reference: the metrics are not charged with it, and the finding is the
    # same whichever was read first.
    assert [f['verdict'] for f in result.findings if f['kind'] == 'metric'] == ['unbound', 'unbound']
    flipped = conform(graph, derive(graph), documents[::-1])
    [again] = [f for f in flipped.findings if f['kind'] == 'pair']
    assert again['layers'] == found['layers'] and again['detail'] == found['detail']
    assert result.summary['pairs'] == 1
    text = str(result)
    assert 'ossie_orders.ossie.yaml' in text and 'lines.sql' in text and 'witness:' in text


def test_a_many_to_one_join_the_relationships_test_contradicts_is_found_once() -> None:
    result = check(tpch(prefix='samples.tpch', line_items=True), 'databricks_fan_out.yaml')
    [found] = [f for f in result.findings if f['severity'] != 'info']
    assert (found['kind'], found['verdict'], found['name']) == (
        'relationship',
        'cardinality-contradicted',
        'lineitem',
    )
    assert found['detail'] == "refers the other way round to the code's relationship"
    assert result.summary['cardinality_contradicted'] == 1


def test_a_key_declared_a_fact_a_reversed_relationship_and_a_renamed_column_are_each_found_once() -> None:
    result = check(tpch(omit='o_orderdate'), 'snowflake_variant.sql')  # the code renamed the column
    found = {(f['kind'], f['verdict']) for f in result.findings if f['severity'] != 'info'}
    assert found == {
        ('key', 'key-misfiled'),
        ('relationship', 'cardinality-contradicted'),
        ('dimension', 'column-missing'),
    }
    assert len([f for f in result.findings if f['severity'] != 'info']) == 3
    assert [f['verdict'] for f in result.findings if f['kind'] == 'metric'] == ['unbound']


def test_a_match_through_columns_alone_is_never_an_error() -> None:
    graph = tpch(term_filter="o_orderstatus = 'F'", prefix='samples.tpch')
    graph.nodes['term.order_average_value'].name = 'Fulfilled revenue'
    text = (
        'version: 1.1\nsource: samples.tpch.orders\nmeasures:\n'
        '  - {name: Open revenue, expr: "SUM(o_totalprice) FILTER (WHERE o_orderstatus = \'O\')"}\n'
        '  - {name: Order count, expr: COUNT(o_totalprice)}\n'
    )
    layers_, _ = read_layers(text, 'm.yaml')
    result = conform(graph, derive(graph), layers_)
    assert not result.failing
    assert all(f['verdict'] != 'conforms' for f in result.findings)


def test_a_required_filter_missing_from_a_term_matched_by_name_is_an_error() -> None:
    graph = tpch(term_filter="o_orderstatus = 'F'")
    graph.nodes['term.order_average_value'].name = 'Order total'
    graph.nodes['term.order_average_value'].attrs['synonyms'] = ['order_total']
    # The term is no longer defined on the columns the view reads: only its name matches.
    for edge in list(graph.out_edges('term.order_average_value', 'defines')):
        graph.remove_edge(edge)
    result = check(graph, 'snowflake_variant.sql')
    found = next(f for f in result.findings if f['name'] == 'order_total')
    assert (found['verdict'], found['severity']) == ('differs', 'error')
    assert found['against'][0]['grade'] == 'name'
    assert found in result.failing


def test_two_logical_tables_over_one_relation_are_each_bound() -> None:
    text = (
        'CREATE SEMANTIC VIEW v TABLES (placed AS snowflake_sample_data.tpch_sf1.orders, '
        'shipped AS snowflake_sample_data.tpch_sf1.orders) '
        'METRICS (placed.placed_total AS SUM(placed.o_totalprice), shipped.shipped_total AS SUM(shipped.o_totalprice));'
    )
    graph = tpch()
    result = conform(graph, derive(graph), read_layers(text, 'v.sql')[0])
    assert {f['verdict'] for f in result.findings} == {'unbound'}  # nothing approved to compare with
    assert [f['expression'] for f in result.findings] == [
        'SUM(placed.o_totalprice)',
        'SUM(shipped.o_totalprice)',
    ]


# Never conforms without something to conform to


def test_a_view_over_a_table_the_graph_lacks_is_unbound() -> None:
    text = 'CREATE SEMANTIC VIEW v TABLES (t AS nowhere.main.missing PRIMARY KEY (id)) METRICS (t.n AS COUNT(t.id));'
    result = conform(tpch(), derive(tpch()), read_layers(text, 'v.sql')[0])
    assert verdicts(result, kind='metric') == {'n': 'unbound'}
    assert 'conforms' not in {f['verdict'] for f in result.findings}
    assert result.findings[0]['detail'] == 'it reads nowhere.main.missing, which the graph does not have'


def test_an_expression_that_does_not_parse_is_unparsed_and_never_conforms() -> None:
    graph = tpch()
    text = (
        'CREATE SEMANTIC VIEW v TABLES (o AS snowflake_sample_data.tpch_sf1.orders) '
        'METRICS (o.bad AS SUM(o.o_totalprice) FILTER (WHERE), o.good AS SUM(o.o_totalprice));'
    )
    result = conform(graph, derive(graph), read_layers(text, 'v.sql')[0])
    assert verdicts(result, kind='metric')['bad'] == 'unparsed'
    assert verdicts(result, kind='metric')['good'] == 'unbound'
    assert result.summary['unparsed'] == 1


def test_a_derived_metric_is_unbound_not_compared() -> None:
    result = check(tpch(), 'snowflake_reference.sql')
    # average_line_items_per_order reads a fact that is itself an aggregate over another table.
    assert verdicts(result, kind='metric')['customer_count'] == 'unbound'


# The report


def test_the_report_is_conform_json_format_one() -> None:
    result = check(tpch(term_filter="o_orderstatus = 'F'"), 'snowflake_reference.sql')
    data = document(result)
    assert data['format_version'] == 1
    assert set(data) == {'format_version', 'documents', 'summary', 'findings', 'problems', 'inputs'}
    assert data['summary']['differs'] == 1
    assert json.loads(json.dumps(data)) == data
    assert 'tpch_rev_analysis (snowflake semantic layer' in str(result)


def test_nothing_given_says_so() -> None:
    assert 'Nothing to check' in str(conform(tpch(), derive(tpch()), []))


# Metadata reads: a fixed set of statements


class _Cursor:
    def __init__(self, log: list[str], answers: dict[str, tuple[list[str], list[tuple[Any, ...]]]]) -> None:
        self.log, self.answers = log, answers
        self.description: list[tuple[str]] = []
        self.rows: list[tuple[Any, ...]] = []

    def execute(self, sql: str) -> None:
        self.log.append(sql)
        names, self.rows = self.answers[sql]
        self.description = [(n,) for n in names]

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows


def _driver(monkeypatch: pytest.MonkeyPatch, name: str, log: list[str], answers: dict[str, Any]) -> None:
    connection = types.SimpleNamespace(
        cursor=lambda: _Cursor(log, answers), close=lambda: log.append('closed')
    )
    module = types.ModuleType(name)
    module.connect = lambda **_: connection  # type: ignore[attr-defined]
    package = types.ModuleType(name.split('.')[0])
    package.__path__ = []
    monkeypatch.setitem(sys.modules, name.split('.')[0], package)
    monkeypatch.setitem(sys.modules, name, module)


def test_snowflake_layers_are_read_by_two_allowed_statements(monkeypatch: pytest.MonkeyPatch) -> None:
    log: list[str] = []
    yaml_text = (FIXTURES / 'snowflake_model.yaml').read_text()
    _driver(
        monkeypatch,
        'snowflake.connector',
        log,
        {
            'SHOW SEMANTIC VIEWS': (['name', 'database_name', 'schema_name'], [('SALES', 'DB', 'S')]),
            'SELECT SYSTEM$READ_YAML_FROM_SEMANTIC_VIEW(\'"DB"."S"."SALES"\')': (['y'], [(yaml_text,)]),
        },
    )
    found = warehouse.read_semantic_layers(warehouse.SnowflakeConnector(options={}))
    assert found == [('snowflake-yaml', 'SALES', yaml_text)]
    assert log == [
        'SHOW SEMANTIC VIEWS',
        'SELECT SYSTEM$READ_YAML_FROM_SEMANTIC_VIEW(\'"DB"."S"."SALES"\')',
        'closed',
    ]


def test_databricks_metric_views_are_read_from_view_text(monkeypatch: pytest.MonkeyPatch) -> None:
    log: list[str] = []
    body = (FIXTURES / 'tmdl_import_metric_view.yaml').read_text()
    _driver(
        monkeypatch,
        'databricks.sql',
        log,
        {'DESCRIBE TABLE EXTENDED main.s.mv AS JSON': (['json'], [(json.dumps({'view_text': body}),)])},
    )
    found = warehouse.read_semantic_layers(warehouse.DatabricksConnector(options={}), ['main.s.mv'])
    assert found == [('databricks-yaml', 'mv', body)]
    assert log == ['DESCRIBE TABLE EXTENDED main.s.mv AS JSON', 'closed']


@pytest.mark.parametrize(
    'sql',
    [
        'SELECT * FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY',
        "SELECT SYSTEM$READ_YAML_FROM_SEMANTIC_VIEW('a'); DROP TABLE x",
        "SELECT SYSTEM$READ_YAML_FROM_SEMANTIC_VIEW('a') -- x",
        'DESCRIBE TABLE EXTENDED a; SELECT 1 AS JSON',
        'SHOW SEMANTIC VIEWS; SELECT 1',
        "SELECT SYSTEM$READ_YAML_FROM_SEMANTIC_VIEW('a'') ",
        'DROP SEMANTIC VIEW v',
    ],
)
def test_any_other_statement_is_refused(sql: str) -> None:
    with pytest.raises(ValueError, match='not a statement'):
        warehouse.semantic_statement(sql)


def test_a_view_name_that_is_no_identifier_runs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    log: list[str] = []
    _driver(monkeypatch, 'snowflake.connector', log, {})
    with pytest.raises(ValueError, match='not a statement'):
        warehouse.read_semantic_layers(warehouse.SnowflakeConnector(options={}), ["x'); DROP TABLE t; --"])
    assert [line for line in log if line != 'closed'] == []


def test_only_snowflake_and_databricks_serve_layers() -> None:
    with pytest.raises(ValueError, match='only Snowflake and Databricks'):
        warehouse.read_semantic_layers(warehouse.DuckDBConnector(path=Path('x.duckdb')))


def test_the_native_readers_refuse_what_they_cannot_read() -> None:
    with pytest.raises(ValueError, match='expected SEMANTIC'):
        _core.conform_read_ddl('CREATE TABLE t (a int)')
    with pytest.raises(ValueError, match='no reader'):
        _core.conform_read_yaml('other', '{}', 'x')
    with pytest.raises(ValueError, match='not JSON'):
        _core.conform_judge('{', _core.FormulaSet(), lambda _: [])


# Through the project


def test_project_conform_rejects_a_path_that_does_not_exist(jaffle_built_graph: Path) -> None:
    with pytest.raises(QueryError, match='does not exist'):
        Project(jaffle_built_graph).conform(['nowhere.sql'])


def test_project_conform_without_a_snowflake_or_databricks_warehouse_says_so(
    jaffle_built_graph: Path,
) -> None:
    with pytest.raises(QueryError, match='no Snowflake or Databricks warehouse'):
        Project(jaffle_built_graph).conform(warehouse=True)


def test_a_dbt_semantic_view_over_the_jaffle_marts(jaffle_built_graph: Path) -> None:
    result = Project(jaffle_built_graph).conform([FIXTURES / 'dbt_semantic_view_jaffle.sql'])
    kinds = {(f['kind'], f['verdict']) for f in result.findings if f['severity'] != 'info'}
    assert ('key', 'key-misfiled') in kinds
    by_name = verdicts(result, kind='metric')
    assert by_name['order_amount'] == 'unbound'  # the approved term states no filter or formula
    completed = next(f for f in result.findings if f['name'] == 'completed_order')
    assert (completed['verdict'], completed['severity']) == ('differs', 'warning')
    assert completed['against'][0]['grade'] == 'name'


def test_konstruct_own_exports_read_back_conforming(jaffle_built_graph: Path, tmp_path: Path) -> None:
    project = Project(jaffle_built_graph)
    paths = []
    for fmt, name in (('semantic-view', 'a.sql'), ('metric-view', 'b.sql'), ('ossie', 'c.ossie.yaml')):
        (tmp_path / name).write_text(project.export(fmt))
        paths.append(tmp_path / name)
    result = project.conform(paths)
    assert not result.problems
    # One layer alone has nothing to conform to; the three agree with each other by name.
    assert not [f for f in result.findings if f['verdict'] not in ('conforms', 'unbound')]
    assert any(f['verdict'] == 'conforms' for f in result.findings)
    assert result.summary['pairs'] == 0


def test_a_date_in_a_layer_is_read_as_text_not_a_crash() -> None:
    text = 'name: m\ntables:\n  - name: o\n    base_table: {table: ORDERS}\n    facts:\n      - {name: f, expr: a, as_of: 2024-01-31}\n'
    found, problems = read_layers(text, 'd.yaml')
    assert problems == [] and found[0][1]['name'] == 'm'
    assert (
        read_layers('source: t\nmeasures:\n  - {name: n, expr: COUNT(1), since: 2024-01-31}\n', 'd2.yaml')[1]
        == []
    )


@pytest.mark.parametrize(
    'sql',
    [
        "SELECT SYSTEM$READ_YAML_FROM_SEMANTIC_VIEW('\"a'' || ''b\"')",
        "SELECT SYSTEM$READ_YAML_FROM_SEMANTIC_VIEW('\"a'b\"')",
        "DESCRIBE TABLE EXTENDED `a'b` AS JSON",
        "SELECT SYSTEM$READ_YAML_FROM_SEMANTIC_VIEW('`a`')",
    ],
)
def test_a_single_quote_or_a_foreign_quote_in_a_name_is_refused(sql: str) -> None:
    with pytest.raises(ValueError, match='not a statement'):
        warehouse.semantic_statement(sql)


def test_names_from_show_are_quoted_and_a_quote_in_one_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    log: list[str] = []
    _driver(
        monkeypatch,
        'snowflake.connector',
        log,
        {
            'SHOW SEMANTIC VIEWS': (
                ['name', 'database_name', 'schema_name'],
                [('my view', 'db', 'Sales'), ("it's", 'db', 'S')],
            ),
            'SELECT SYSTEM$READ_YAML_FROM_SEMANTIC_VIEW(\'"db"."Sales"."my view"\')': (['y'], [('name: x',)]),
        },
    )
    found = warehouse.read_semantic_layers(warehouse.SnowflakeConnector(options={}))
    assert found == [('snowflake-yaml', 'my view', 'name: x')]


def test_a_formula_the_set_does_not_hold_is_an_error_not_a_panic() -> None:
    request = json.dumps({'claims': [{'document': 'd', 'name': 'm', 'state': 'bound', 'formula': 9}]})
    with pytest.raises(ValueError, match='no formula 9'):
        _core.conform_judge(request, _core.FormulaSet(), lambda _: [])


def test_every_cardinality_is_checked_not_only_many_to_one() -> None:
    report = json.loads(
        _core.conform_judge(
            json.dumps(
                {
                    'code_joins': [{'from': 'a', 'to': 'b', 'cardinality': 'many-to-one'}],
                    'joins': [
                        {'document': 'd', 'name': 'r', 'from': 'a', 'to': 'b', 'declared': 'one-to-one'}
                    ],
                }
            ),
            _core.FormulaSet(),
            lambda _: [],
        )
    )
    assert [f['verdict'] for f in report['findings']] == ['cardinality-contradicted']


def test_a_held_callback_that_raises_is_the_callers_error() -> None:
    def refuse(_: list[str]) -> list[tuple[str, str]]:
        raise RuntimeError('no graph')

    request = {
        'claims': [
            {'document': d, 'name': 'm', 'synonyms': [], 'state': 'bound', 'columns': ['c'], 'formula': 0}
            for d in 'ab'
        ]
    }
    formulas = _core.FormulaSet()
    formulas.add('(lit "int" "1")', '')
    with pytest.raises(RuntimeError, match='no graph'):
        _core.conform_judge(json.dumps(request), formulas, refuse)


# Filters that hold only where the data says so: the graph is built from SQL, not assembled

SHOP = """
create schema raw;
create table raw.customers (customer_id int primary key, status varchar);
create table raw.orders (order_id int primary key, customer_id int, amount decimal(10, 2), status varchar);
"""
KEPT = """version: 1
terms:
  - name: Kept amount
    kind: measure
    status: approved
    owner: {name: Ana Ruiz}
    approved: 2026-10-01
    columns: [kept.v]
    filters: ["orders.status <> 'returned'"]
"""


def kept_verdict(tmp_path: Path, model: str) -> dict[str, Any]:
    """The verdict on a semantic view metric over the column ``v`` of a mart that ``model``
    builds, which an approved definition requires to exclude returned orders."""
    con = duckdb.connect(str(tmp_path / 'shop.duckdb'))
    con.execute(SHOP)
    con.close()
    (tmp_path / 'mart').mkdir()
    (tmp_path / 'mart' / 'kept.sql').write_text(f'create table mart.kept as {model}')
    (tmp_path / 'definitions.yml').write_text(KEPT)
    graph = Project(tmp_path).build(run_dbt=False).graph
    view = (
        'CREATE SEMANTIC VIEW v TABLES (k AS shop.mart.kept PRIMARY KEY (order_id)) '
        'METRICS (k.kept_amount AS SUM(k.v));'
    )
    result = conform(graph, derive(graph), read_layers(view, 'v.sql')[0])
    [found] = [f for f in result.findings if f['kind'] == 'metric']
    return found


def test_a_filter_the_mart_applies_is_held_and_gives_no_formula_to_conform_to(tmp_path: Path) -> None:
    found = kept_verdict(tmp_path, "select order_id, amount as v from raw.orders where status <> 'returned'")
    assert found['verdict'] == 'unbound'
    assert found['detail'] == 'its required filters hold; no formula to compare'


@pytest.mark.parametrize(
    'model',
    [
        # Kept rows and returned rows in one mart.
        "select order_id, amount as v from raw.orders where status <> 'returned' union all "
        "select order_id, amount as v from raw.orders where status = 'returned'",
        # The filter is applied above a window that has already summed every row.
        'with w as (select order_id, status, sum(amount) over () as v from raw.orders) '
        "select order_id, v from w where status <> 'returned'",
        # The filter is on another table's column of the same name.
        'select o.order_id, o.amount as v from raw.orders o join raw.customers c '
        "on c.customer_id = o.customer_id where c.status <> 'returned'",
    ],
    ids=['union-all', 'above-a-window', 'same-name-on-another-table'],
)
def test_a_filter_that_does_not_hold_on_the_mart_is_missing_and_never_conforms(
    tmp_path: Path, model: str
) -> None:
    found = kept_verdict(tmp_path, model)
    assert found['verdict'] == 'differs'
    assert [m['filter'] for m in found['missing_filters']] == ["orders.status <> 'returned'"]


# The rows a metric aggregates are its own table's

ORDERS = 'orders AS snowflake_sample_data.tpch_sf1.orders PRIMARY KEY (o_orderkey)'
LINE_ITEMS = 'line_items AS snowflake_sample_data.tpch_sf1.lineitem'


def views(*statements: str) -> list[tuple[str, dict[str, Any]]]:
    found: list[tuple[str, dict[str, Any]]] = []
    for number, text in enumerate(statements):
        found += read_layers(text, f'v{number}.sql')[0]
    return found


def test_a_count_of_rows_on_two_tables_is_not_the_same_metric() -> None:
    graph = tpch()
    result = conform(
        graph,
        derive(graph),
        views(
            f'CREATE SEMANTIC VIEW a TABLES ({ORDERS}) METRICS (orders.row_count AS COUNT(*));',
            f'CREATE SEMANTIC VIEW b TABLES ({LINE_ITEMS}) METRICS (line_items.row_count AS COUNT(*));',
        ),
    )
    assert {f['verdict'] for f in result.findings if f['kind'] == 'metric'} == {'unbound'}
    [pair] = [f for f in result.findings if f['kind'] == 'pair']
    assert pair['verdict'] == 'differs'


def test_a_count_of_rows_on_one_table_in_two_layers_conforms() -> None:
    graph = tpch()
    one = f'CREATE SEMANTIC VIEW a TABLES ({ORDERS}) METRICS (orders.row_count AS COUNT(*));'
    result = conform(graph, derive(graph), views(one, one.replace('VIEW a', 'VIEW b')))
    assert {f['verdict'] for f in result.findings if f['kind'] == 'metric'} == {'conforms'}


FAN_OUT = (
    'CREATE SEMANTIC VIEW {name} TABLES ({orders}, {line_items} PRIMARY KEY (l_orderkey, l_linenumber)) '
    'RELATIONSHIPS (li_orders AS line_items (l_orderkey) REFERENCES orders) '
    'METRICS ({owner}.total_price AS {aggregate}(orders.o_totalprice));'
)


def fan_out(name: str, owner: str, aggregate: str = 'SUM') -> str:
    return FAN_OUT.format(name=name, orders=ORDERS, line_items=LINE_ITEMS, owner=owner, aggregate=aggregate)


def test_a_metric_over_a_parents_column_counts_each_parent_once_per_child_and_is_not_compared() -> None:
    graph = tpch()
    result = conform(graph, derive(graph), views(fan_out('a', 'orders'), fan_out('b', 'line_items')))
    by_document = {f['document'].split('#')[1]: f for f in result.findings if f['kind'] == 'metric'}
    assert by_document['b']['verdict'] == 'unbound'
    assert 'once for each line_items row' in by_document['b']['detail']
    assert 'conforms' not in {f['verdict'] for f in result.findings}
    assert not [f for f in result.findings if f['kind'] == 'pair']


def test_an_aggregate_a_repeated_row_does_not_change_is_not_a_fan_out() -> None:
    graph = tpch()
    result = conform(
        graph, derive(graph), views(fan_out('a', 'orders', 'MAX'), fan_out('b', 'line_items', 'MAX'))
    )
    assert {f['verdict'] for f in result.findings if f['kind'] == 'metric'} == {'conforms'}


def test_a_fact_that_does_not_parse_makes_the_metric_over_it_unparsed_not_column_missing() -> None:
    graph = tpch()
    text = (
        f'CREATE SEMANTIC VIEW v TABLES ({ORDERS}) FACTS (orders.broken AS o_totalprice +) '
        'METRICS (orders.total AS SUM(orders.broken));'
    )
    result = conform(graph, derive(graph), views(text))
    assert verdicts(result) == {'total': 'unparsed', 'orders.broken': 'unparsed'}
    assert 'broken' in next(f for f in result.findings if f['name'] == 'total')['detail']
    assert result.summary['unparsed'] == 1 and result.summary['column_missing'] == 0


def test_the_report_does_not_depend_on_the_order_the_layers_are_read_in() -> None:
    names = ('snowflake_reference.sql', 'snowflake_variant.sql', 'ossie_orders.ossie.yaml')
    documents = [layers(name)[0] for name in names]
    graph = tpch(term_filter="o_orderstatus = 'F'")

    def report(order: tuple[int, ...]) -> tuple[list[str], dict[str, int], dict[str, Any]]:
        result = conform(graph, derive(graph), [documents[i] for i in order])
        return (
            sorted(json.dumps(f, sort_keys=True) for f in result.findings),
            result.summary,
            {d['key']: d['counts'] for d in result.documents},
        )

    first = report((0, 1, 2))
    assert first[0], 'the layers give findings to compare'
    assert all(report(order) == first for order in itertools.permutations(range(3)))


# A metric's own filters hold a required filter

REQUIRED = "o_orderstatus = 'F'"


def fulfilled(expression: str) -> dict[str, Any]:
    """The verdict on a metric ``Fulfilled revenue`` of a metric view, whose approved term requires
    the orders to be fulfilled."""
    graph = tpch(term_filter=REQUIRED, prefix='samples.tpch')
    graph.nodes['term.order_average_value'].name = 'Fulfilled revenue'
    text = f'version: 1.1\nsource: samples.tpch.orders\nmeasures:\n  - name: Fulfilled revenue\n    expr: "{expression}"\n'
    result = conform(graph, derive(graph), read_layers(text, 'm.yaml')[0])
    [found] = [f for f in result.findings if f['kind'] == 'metric']
    return found


@pytest.mark.parametrize(
    'expression',
    [
        "SUM(o_totalprice) FILTER (WHERE o_orderstatus = 'F')",
        "COUNT(o_totalprice) FILTER (WHERE o_orderstatus = 'F' AND o_totalprice > 0)",
        *(
            f"{agg}(CASE WHEN o_orderstatus = 'F' THEN o_totalprice{tail} END)"
            for agg in ('SUM', 'COUNT', 'AVG', 'MIN', 'MAX')
            for tail in ('', ' ELSE NULL')
        ),
    ],
)
def test_a_filter_or_a_case_without_an_else_on_the_aggregate_holds_the_required_filter(
    expression: str,
) -> None:
    found = fulfilled(expression)
    assert found['verdict'] == 'unbound'
    assert found['detail'] == 'its required filters hold; no formula to compare'


@pytest.mark.parametrize(
    'expression',
    [
        "SUM(CASE WHEN o_orderstatus = 'F' THEN o_totalprice WHEN o_totalprice > 9 THEN 1 END)",
        "SUM(o_totalprice) FILTER (WHERE o_orderstatus = 'O')",
        "SUM(o_totalprice) + SUM(o_totalprice) FILTER (WHERE o_orderstatus = 'F')",
        'SUM(o_totalprice)',
    ],
)
def test_a_case_with_an_else_or_a_filter_not_on_every_aggregate_does_not_hold_it(expression: str) -> None:
    found = fulfilled(expression)
    assert (found['verdict'], found['severity']) == ('differs', 'error')
    assert [m['filter'] for m in found['missing_filters']] == [REQUIRED]


@pytest.mark.parametrize('constant', ['COUNT(1)', 'SUM(1)', 'COUNT(0)', "COUNT('x')", 'COUNT(*)'])
def test_an_aggregate_of_a_constant_counts_its_own_tables_rows(constant: str) -> None:
    def view(name: str, tables: str, owner: str, relationships: str = '') -> str:
        return f'CREATE SEMANTIC VIEW {name} TABLES ({tables}) {relationships} METRICS ({owner}.n AS {constant});'

    graph = tpch()
    other = view(
        'b',
        f'{ORDERS}, {LINE_ITEMS}',
        'line_items',
        'RELATIONSHIPS (li AS line_items (l_orderkey) REFERENCES orders)',
    )
    result = conform(graph, derive(graph), views(view('a', ORDERS, 'orders'), other))
    assert {f['verdict'] for f in result.findings if f['kind'] == 'metric'} == {'unbound'}
    [pair] = [f for f in result.findings if f['kind'] == 'pair']
    assert pair['verdict'] == 'differs'
    # On one table, in two layers, it is the same metric.
    again = conform(graph, derive(graph), views(view('a', ORDERS, 'orders'), view('c', ORDERS, 'orders')))
    assert {f['verdict'] for f in again.findings if f['kind'] == 'metric'} == {'conforms'}


def test_a_nan_in_a_layer_is_a_clear_problem() -> None:
    text = 'source: samples.tpch.orders\nmeasures:\n  - {name: n, expr: COUNT(1), weight: .nan}\n'
    layers_, problems = read_layers(text, 'nan.yaml')
    assert layers_ == []
    assert problems == ['nan.yaml: a value is .nan or .inf, which a semantic layer does not hold']


# More ways to write a filtered measure that hold the required filter


@pytest.mark.parametrize(
    'expression',
    [
        "SUM(IF(o_orderstatus = 'F', o_totalprice, NULL))",
        "SUM(IFF(o_orderstatus = 'F', o_totalprice, NULL))",
        "SUM(CASE o_orderstatus WHEN 'F' THEN o_totalprice END)",
        "COUNT(DISTINCT CASE WHEN o_orderstatus = 'F' THEN o_totalprice END)",
        "SUM(CASE WHEN o_orderstatus = 'F' THEN o_totalprice ELSE 0 END)",
        "SUM(IF(o_orderstatus = 'F', o_totalprice, 0))",
    ],
)
def test_other_spellings_of_a_filtered_measure_hold_the_required_filter(expression: str) -> None:
    found = fulfilled(expression)
    assert found['verdict'] == 'unbound' and found['held'] is True, found


@pytest.mark.parametrize(
    'expression',
    [
        "AVG(CASE WHEN o_orderstatus = 'F' THEN o_totalprice ELSE 0 END)",
        "COUNT(CASE WHEN o_orderstatus = 'F' THEN o_totalprice ELSE 0 END)",
        "SUM(CASE WHEN o_orderstatus <> 'F' THEN o_totalprice END)",
        "SUM(CASE WHEN NOT o_orderstatus = 'F' THEN o_totalprice END)",
        "SUM(CASE WHEN o_orderstatus = 'F' OR o_totalprice > 9 THEN o_totalprice END)",
        "SUM(CASE o_orderstatus WHEN 'O' THEN o_totalprice END)",
        "SUM(IF(o_orderstatus = 'F', o_totalprice, 1))",
    ],
)
def test_the_inverted_the_negated_the_or_and_the_non_zero_else_hold_nothing(expression: str) -> None:
    found = fulfilled(expression)
    assert (found['verdict'], found['severity'], found['held']) == ('differs', 'error', False), found


def test_a_metric_that_reads_a_column_the_code_lost_blames_the_code_not_the_layer() -> None:
    result = check(tpch(omit='o_totalprice'), 'snowflake_variant.sql')
    found = next(f for f in result.findings if f['name'] == 'order_total')
    assert found['verdict'] == 'column-missing'
    assert 'field of the model' not in found['detail']
    assert 'the code no longer has' in found['detail'], found['detail']
