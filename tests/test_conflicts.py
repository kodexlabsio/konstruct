"""konstruct conflicts: one business idea computed in more than one way."""

from __future__ import annotations

import json
import shutil
import textwrap
from pathlib import Path
from typing import Any

import duckdb
import pytest

from konstruct import Project
from konstruct.analysis.conflicts import SAME_UNLESS_NULL, SHOWN, Conflict, core, find_conflicts
from konstruct.core.graph import Graph, Node
from konstruct.interfaces.cli import main

FINANCE = """\
create table finance.daily_revenue as
select order_date, sum(amount) as revenue
from raw.orders
where status = 'completed'
group by order_date
"""
MARKETING = """\
create table marketing.daily_performance as
select order_date, sum(amount) as total_revenue, count(*) as orders
from raw.orders
group by order_date
"""
WEEKLY_DAG = """\
from airflow.sdk import DAG
from airflow.providers.common.sql.operators.sql import SQLExecuteQueryOperator
with DAG("marketing_report"):
    SQLExecuteQueryOperator(
        task_id="weekly",
        sql="create table marketing.weekly as select date_trunc('week', order_date) as week, "
        "sum(total_revenue) as revenue from marketing.daily_performance group by 1",
    )
"""


def make_shop(root: Path, *, invoices: bool = False, definitions: str | None = None) -> Project:
    con = duckdb.connect(str(root / 'shop.duckdb'))
    con.execute('create schema raw')
    con.execute('create table raw.orders (id int, order_date date, amount decimal(10,2), status varchar)')
    con.execute('create table raw.invoices (id int, invoice_date date, total decimal(10,2))')
    con.close()
    (root / 'finance').mkdir()
    (root / 'marketing').mkdir()
    (root / 'dags').mkdir()
    (root / 'finance' / 'revenue.sql').write_text(FINANCE)
    (root / 'marketing' / 'daily.sql').write_text(MARKETING)
    (root / 'dags' / 'marketing.py').write_text(WEEKLY_DAG)
    if invoices:
        (root / 'finance' / 'invoiced.sql').write_text(
            'create table finance.invoiced as select invoice_date, sum(total) as sales_amount '
            'from raw.invoices group by invoice_date'
        )
    if definitions:
        (root / 'definitions.yml').write_text(textwrap.dedent(definitions))
    project = Project(root)
    project.build()
    return project


def test_names_reduce_to_the_idea() -> None:
    assert core('total_revenue') == core('revenue') == core('sales_amount') == core('NetRevenue') - {'net'}
    assert core('number_of_orders') == core('order_count') == frozenset({'count', 'order'})
    assert core('customer_lifetime_value') == frozenset({'customer', 'lifetime', 'value'})
    assert core('total') == frozenset()
    # An amount names a measure only when nothing else does; amt is short for it.
    assert core('amount') == core('total_amt') == frozenset({'value'})
    assert core('refund_amount') == core('refunds') == frozenset({'refund'})


def test_two_revenue_definitions_that_differ_by_one_filter(tmp_path: Path) -> None:
    """Acceptance: the pair is reported with the differing filter and its file."""
    result = make_shop(tmp_path).conflicts()
    [conflict] = result.conflicts
    assert {conflict.a.label, conflict.b.label} == {
        'daily_revenue.revenue',
        'daily_performance.total_revenue',
    }
    assert conflict.idea == 'revenue'
    [difference] = conflict.differences
    assert difference.kind == 'filter'
    assert difference.detail == "WHERE: orders.status = 'completed'"
    assert difference.path == 'finance/revenue.sql'
    side = conflict.a if difference.side == 'a' else conflict.b
    assert side.label == 'daily_revenue.revenue'
    assert conflict.a.composed == conflict.b.composed == 'SUM(orders.amount)'
    # weekly.revenue only re-aggregates total_revenue, so it is carried into, not a third side.
    marketing = conflict.b if conflict.b.label.startswith('daily_performance') else conflict.a
    assert marketing.carried_into == ['weekly.revenue']
    assert marketing.path == 'marketing/daily.sql'
    assert result.summary() == '1 conflicting definition among 5 computed columns.'


def test_filters_written_differently_are_not_a_difference(tmp_path: Path) -> None:
    project = make_shop(tmp_path)
    (tmp_path / 'marketing' / 'daily.sql').write_text(
        MARKETING.replace('group by order_date', "where 'completed' = status\ngroup by order_date")
    )
    project.build()
    assert project.conflicts().conflicts == []


def test_different_sources_and_ranking(tmp_path: Path) -> None:
    result = make_shop(tmp_path, invoices=True).conflicts()
    pairs = [{c.a.label, c.b.label} for c in result.conflicts]
    assert {'daily_revenue.revenue', 'invoiced.sales_amount'} in pairs
    invoice = next(
        c
        for c in result.conflicts
        if {c.a.label, c.b.label} == {'daily_revenue.revenue', 'invoiced.sales_amount'}
    )
    kinds = sorted((d.kind, d.detail) for d in invoice.differences)
    assert kinds == [
        ('filter', "WHERE: orders.status = 'completed'"),
        ('source', 'invoices.total'),
        ('source', 'orders.amount'),
    ]
    # Pairs involving total_revenue, which a task reads downstream, rank above the one without.
    scores = [c.score for c in result.conflicts]
    assert scores == sorted(scores, reverse=True)
    assert 'daily_performance.total_revenue' in {result.conflicts[0].a.label, result.conflicts[0].b.label}
    assert result.conflicts[-1] is invoice


def test_conflicts_touching_columns_are_those_of_every_conflict(tmp_path: Path) -> None:
    """The gate compares only the pairs of the columns a change alters: the same conflicts, in the
    same order, as comparing every pair and keeping those."""
    project = make_shop(tmp_path, invoices=True)
    every = find_conflicts(project.graph)
    columns = sorted({side.column for c in every.conflicts for side in (c.a, c.b)})
    assert len(every.conflicts) > 1 and len(columns) > 2
    for touching in [[column] for column in columns] + [columns[:2], columns]:
        found = find_conflicts(project.graph, touching=set(touching))
        expected = [c for c in every.conflicts if c.a.column in touching or c.b.column in touching]
        # The rank is the place among the pairs compared, so it is left out.
        assert [unranked(c) for c in found.conflicts] == [unranked(c) for c in expected]
        assert (found.candidates, found.groups) == (every.candidates, every.groups)
    assert find_conflicts(project.graph, touching=set()).conflicts == []


def test_terms_group_columns_named_differently(tmp_path: Path) -> None:
    project = make_shop(
        tmp_path,
        definitions="""
        terms:
          - name: Gross merchandise value
            synonyms: [GMV]
            definition: Value of everything ordered.
            columns: [daily_revenue.revenue]
        """,
    )
    (tmp_path / 'finance' / 'gmv.sql').write_text(
        'create table finance.gmv_daily as select order_date, sum(amount) * 1.2 as gmv from raw.orders group by order_date'
    )
    project.build()
    result = project.conflicts()
    by_term = [c for c in result.conflicts if c.idea == 'Gross merchandise value']
    assert by_term
    labels = {label for c in by_term for label in (c.a.label, c.b.label)}
    assert {'gmv_daily.gmv', 'daily_revenue.revenue'} <= labels
    assert any("business term 'Gross merchandise value'" in c.reason for c in by_term)
    assert any('Gross merchandise value' in c.a.terms + c.b.terms for c in by_term)


@pytest.mark.dbt
def test_no_noise_on_the_example(jaffle_built_graph: Path, tmp_path: Path) -> None:
    """The example's one conflict is the one it was given: finance_orders leaves out returned
    orders, which orders.amount counts. Copies (customer_ltv.lifetime_value) and columns that
    differ only in grain are not conflicts."""
    copy = tmp_path / 'jaffle_shop'
    shutil.copytree(jaffle_built_graph / 'konstruct-out', copy / 'konstruct-out')
    result = Project(copy).conflicts()
    [conflict] = result.conflicts
    assert (conflict.idea, conflict.a.label, conflict.b.label) == (
        'Order amount',
        'finance_orders.order_value',
        'orders.amount',
    )
    assert [(d.kind, d.side) for d in conflict.differences] == [('filter', 'a'), ('join', 'a'), ('join', 'b')]
    assert conflict.differences[0].path == 'models/finance_orders.sql'
    assert result.candidates > 10


def test_command_files_and_formats(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    make_shop(tmp_path)
    assert main(['conflicts', '-C', str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert out.startswith('1 likely conflicting definition (check each) among 5 computed columns.')
    assert (
        "filter only in daily_revenue.revenue: WHERE orders.status = 'completed'  (finance/revenue.sql)"
        in out
    )
    report = (tmp_path / 'konstruct-out' / 'CONFLICTS.md').read_text()
    assert report.startswith('# Conflicting definitions')
    assert '## 1. revenue: `daily_performance.total_revenue` and `daily_revenue.revenue`' in report or (
        '## 1. revenue: `daily_revenue.revenue` and `daily_performance.total_revenue`' in report
    )
    assert (
        '- filter (only in ' in report
        and "`WHERE: orders.status = 'completed'`, in `finance/revenue.sql`" in report
    )
    data = json.loads((tmp_path / 'konstruct-out' / 'conflicts.json').read_text())
    assert data['conflicts'][0]['differences'][0]['path'] == 'finance/revenue.sql'
    assert main(['conflicts', '-C', str(tmp_path), '--fail-on-conflict', '--no-write', '-q']) == 1
    capsys.readouterr()
    assert main(['conflicts', '-C', str(tmp_path), '--format', 'markdown', '--no-write']) == 0
    assert capsys.readouterr().out.startswith('# Conflicting definitions')
    assert main(['conflicts', '-C', str(tmp_path), '--json', '--no-write', '-n', '0']) == 0
    assert json.loads(capsys.readouterr().out)['conflicts'] == []


def test_the_page_shows_the_conflicts_without_composing_its_formulas_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """graph.html and explain.json from the build of this graph keep their formulas, SQL and
    ontology, and gain the conflicts; a page of another graph is made again in full."""
    from konstruct.outputs import html

    project = make_shop(tmp_path)
    out = tmp_path / 'konstruct-out'
    built = json.loads((out / 'explain.json').read_text())
    assert built['conflicts'] == {}

    def composed(*args: object, **kwargs: object) -> dict[str, object]:
        raise AssertionError('the formulas were composed again')

    with monkeypatch.context() as patch:
        patch.setattr(html, 'explanations', composed)
        project.conflicts(write=True)
    shown = json.loads((out / 'explain.json').read_text())
    assert shown['conflicts']['conflicts'][0]['idea'] == 'revenue'
    assert {k: v for k, v in shown.items() if k != 'conflicts'} == {
        k: v for k, v in built.items() if k != 'conflicts'
    }
    page = (out / 'graph.html').read_text()
    assert html._embedded(out / 'graph.html', html._embed_json(project.graph.to_dict())) == shown
    assert page == html._page(html._embed_json(shown), html._embed_json(project.graph.to_dict()))

    html.write_html(Graph(), out / 'graph.html')
    project.conflicts(write=True)
    again = html.viewer_data(project.graph, out)
    assert json.loads((out / 'explain.json').read_text()) == json.loads(json.dumps(again))
    assert html._embedded(out / 'graph.html', html._embed_json(project.graph.to_dict())) is not None


NET_FINANCE = """\
create table finance.revenue as
select order_date, sum(amount) - sum(discount) as net_revenue
from raw.orders
group by order_date
"""
NET_MARKETING = """\
create table marketing.daily as
select order_date, sum(amount - discount) as net_revenue
from raw.orders
group by order_date
"""


def net_revenue_shop(root: Path, marketing: str = NET_MARKETING) -> Project:
    con = duckdb.connect(str(root / 'shop.duckdb'))
    con.execute('create schema raw')
    con.execute(
        'create table raw.orders (id int, order_date date, amount decimal(10,2), discount decimal(10,2), '
        'status varchar)'
    )
    con.close()
    (root / 'finance').mkdir()
    (root / 'marketing').mkdir()
    (root / 'finance' / 'revenue.sql').write_text(NET_FINANCE)
    (root / 'marketing' / 'daily.sql').write_text(textwrap.dedent(marketing))
    project = Project(root)
    project.build()
    return project


def _net_revenue_graph(not_null: bool, formulas: tuple[str, str] | None = None) -> Graph:
    """Two models computing net revenue as SUM(amount) - SUM(discount) and SUM(amount - discount),
    or as ``formulas``, with or without not_null tests on the source columns."""
    graph = Graph()
    orders = graph.add_node(Node('source.raw.orders', 'source', 'orders', relation='db.raw.orders'))
    for name in ('amount', 'discount'):
        column = graph.add_column(orders, name)
        if not_null:
            test = graph.add_node(
                Node(f'test.not_null_{name}', 'test', f'not_null_{name}', attrs={'test': 'not_null'})
            )
            graph.add_edge(test.id, column.id, 'tests')
    finance, marketing = formulas or (
        'SUM(db.raw.orders.amount) - SUM(db.raw.orders.discount)',
        'SUM(db.raw.orders.amount - db.raw.orders.discount)',
    )
    for name, formula in (('finance', finance), ('marketing', marketing)):
        model = graph.add_node(Node(f'model.{name}', 'model', name, relation=f'db.s.{name}'))
        column = graph.add_column(model, 'net_revenue', attrs={'expression': formula})
        graph.add_edge(orders.id, model.id, 'feeds')
        for source in ('amount', 'discount'):
            graph.add_edge(column.id, f'{orders.id}#{source}', 'derived_from')
    return graph


def test_conflicts_count_formulas_that_are_the_same_as_one() -> None:
    """Acceptance: SUM(a) - SUM(b) and SUM(a - b) are one definition when a and b are never NULL."""
    result = find_conflicts(_net_revenue_graph(not_null=True))
    assert result.conflicts == []
    assert result.candidates == 2
    [conflict] = find_conflicts(_net_revenue_graph(not_null=False)).conflicts
    [difference] = conflict.differences
    assert difference.detail.endswith(f', {SAME_UNLESS_NULL}')


def test_formulas_the_same_only_without_nulls_are_reported_with_values(tmp_path: Path) -> None:
    result = net_revenue_shop(tmp_path).conflicts()
    [conflict] = result.conflicts
    [difference] = conflict.differences
    assert difference.kind == 'formula'
    assert difference.detail.endswith('the same unless a source value is NULL or there are no rows')
    witness = difference.witness
    assert witness is not None and witness.kind == 'values'
    assert 'NULL' in str(witness)


def test_conflicts_that_differ_by_a_filter_are_reported(tmp_path: Path) -> None:
    filtered = NET_MARKETING.replace('from raw.orders', "from raw.orders where status = 'completed'")
    [conflict] = net_revenue_shop(tmp_path, filtered).conflicts().conflicts
    assert [(d.kind, d.detail) for d in conflict.differences] == [
        ('filter', "WHERE: orders.status = 'completed'"),
        (
            'formula',
            'SUM(orders.amount) - SUM(orders.discount) against SUM(orders.amount - orders.discount), '
            + SAME_UNLESS_NULL,
        ),
    ]


def test_conflicting_formulas_show_values_that_differ(tmp_path: Path) -> None:
    other = NET_MARKETING.replace('sum(amount - discount)', 'sum(amount) - 2 * sum(discount)')
    result = net_revenue_shop(tmp_path, other).conflicts()
    [conflict] = result.conflicts
    [difference] = conflict.differences
    assert difference.kind == 'formula'
    witness = difference.witness
    assert witness is not None and witness.kind == 'values'
    assert witness.rows is not None
    assert {'orders.amount', 'orders.discount'} <= set(witness.rows[0])
    assert witness.a != witness.b
    result.every = True
    assert f'\n        {witness}' in str(result)
    assert f'  - {witness}' in result.markdown().splitlines()
    assert result.to_dict()['conflicts'][0]['differences'][0]['witness']['kind'] == 'values'


def test_conflicting_aggregations_show_the_function(tmp_path: Path) -> None:
    other = NET_MARKETING.replace('sum(amount - discount)', 'max(amount - discount)')
    [conflict] = net_revenue_shop(tmp_path, other).conflicts().conflicts
    [difference] = [d for d in conflict.differences if d.kind == 'aggregation']
    assert difference.witness is not None
    assert difference.witness.kind == 'functions'
    assert 'MAX' in difference.witness.functions


def test_formulas_written_the_same_are_never_a_formula_difference() -> None:
    # A not_null test on one of the two makes their verdict differ, but not their formula.
    same = 'SUM(db.raw.orders.amount) * 1'
    graph = _net_revenue_graph(not_null=False, formulas=(same, same))
    test = graph.add_node(Node('test.not_null_net', 'test', 'not_null_net', attrs={'test': 'not_null'}))
    graph.add_edge(test.id, 'model.finance#net_revenue', 'tests')
    result = find_conflicts(graph)
    assert result.candidates == 2
    assert result.conflicts == []


def _amount_and_credit_not_null(graph: Graph) -> Graph:
    """The graph with not_null tests on orders.amount and customers.credit."""
    for relation, column in (('orders', 'amount'), ('customers', 'credit')):
        [tested] = [
            n for n in graph.nodes.values() if n.kind == 'column' and n.id.endswith(f'.{relation}#{column}')
        ]
        test = graph.add_node(Node(f'test.not_null_{column}', 'test', 'not_null', attrs={'test': 'not_null'}))
        graph.add_edge(test.id, tested.id, 'tests')
    return graph


@pytest.mark.parametrize('join', ['on customers.customer_id = orders.customer_id', 'using (customer_id)'])
def test_a_not_null_measure_read_through_an_outer_join_conflicts(tmp_path: Path, join: str) -> None:
    """Acceptance: amount and credit are never NULL in their own tables, but a customer with no
    order has no amount, so SUM(amount) - SUM(credit) and SUM(amount - credit) differ."""
    con = duckdb.connect(str(tmp_path / 'shop.duckdb'))
    con.execute('create schema raw')
    con.execute('create table raw.customers (customer_id int, region varchar, credit int)')
    con.execute('create table raw.orders (customer_id int, amount int)')
    con.close()
    (tmp_path / 'customer_orders.sql').write_text(
        'create table marts.customer_orders as\n'
        'select customers.region, customers.credit, orders.amount\n'
        f'from raw.customers left join raw.orders {join}\n'
    )
    for name, formula in (('apart', 'sum(amount) - sum(credit)'), ('together', 'sum(amount - credit)')):
        (tmp_path / f'{name}.sql').write_text(
            f'create table marts.{name} as\n'
            f'select region, {formula} as net_amount from marts.customer_orders group by region\n'
        )
    graph = _amount_and_credit_not_null(Project(tmp_path).build().graph)
    [conflict] = find_conflicts(graph).conflicts
    [difference] = [d for d in conflict.differences if d.kind == 'formula']
    assert difference.detail.endswith(f', {SAME_UNLESS_NULL}')
    assert difference.witness is not None and difference.witness.kind == 'values'
    # Only the outer join makes them differ.
    inner = tmp_path / 'customer_orders.sql'
    inner.write_text(inner.read_text().replace(' left join ', ' join '))
    graph = _amount_and_credit_not_null(Project(tmp_path).build().graph)
    assert find_conflicts(graph).conflicts == []


def test_an_outer_join_on_one_route_is_the_join_difference_alone(tmp_path: Path) -> None:
    # credit is read from the preserved side of a LEFT JOIN on one route and directly on the
    # other: the formulas are the same once it is taken as it is in customers, and the join is
    # reported on its own.
    con = duckdb.connect(str(tmp_path / 'shop.duckdb'))
    con.execute('create schema raw')
    con.execute('create table raw.customers (customer_id int, region varchar, credit int)')
    con.execute('create table raw.orders (customer_id int, amount int)')
    con.close()
    (tmp_path / 'a.sql').write_text(
        'create table marts.a as\n'
        'select customers.region, sum(customers.credit) as total_credit from raw.customers\n'
        'left join raw.orders on customers.customer_id = orders.customer_id group by customers.region\n'
    )
    (tmp_path / 'b.sql').write_text(
        'create table marts.b as\n'
        'select region, sum(credit * 1) as total_credit from raw.customers group by region\n'
    )
    graph = Project(tmp_path).build().graph
    [credit] = [n for n in graph.nodes.values() if n.kind == 'column' and n.id.endswith('.customers#credit')]
    graph.add_node(Node('test.not_null_credit', 'test', 'not_null', attrs={'test': 'not_null'}))
    graph.add_edge('test.not_null_credit', credit.id, 'tests')
    [conflict] = find_conflicts(graph).conflicts
    assert [d.kind for d in conflict.differences] == ['join']


def test_a_source_difference_hides_a_formula_the_same_unless_null() -> None:
    graph = _net_revenue_graph(
        not_null=False,
        formulas=('SUM(db.raw.orders.amount) + SUM(db.raw.orders.discount) * 0', 'SUM(db.raw.orders.amount)'),
    )
    [conflict] = find_conflicts(graph).conflicts
    assert [(d.kind, d.side) for d in conflict.differences] == [('source', 'a')]


def test_each_conflict_lists_the_copies_of_its_own_idea(tmp_path: Path) -> None:
    """base.revenue is one side of two ideas: revenue, where copy.revenue is derived from it, and
    the term Booked value, where nothing is. Each conflict lists the copies of its own idea."""
    (tmp_path / 'konstruct.toml').write_text('dialect = "duckdb"\n')
    (tmp_path / 'base.sql').write_text(
        'create table main.base as select customer_id, sum(amount) as revenue from raw.orders group by 1'
    )
    (tmp_path / 'copy.sql').write_text(
        'create table main.copy as select customer_id, revenue * 1 as revenue from main.base'
    )
    (tmp_path / 'other.sql').write_text(
        'create table main.other as select customer_id, sum(amount) as revenue from raw.orders '
        "where status = 'paid' group by 1"
    )
    (tmp_path / 'booked.sql').write_text(
        'create table main.booked as select customer_id, sum(total) as booked from raw.invoices group by 1'
    )
    (tmp_path / 'definitions.yml').write_text(
        'terms:\n  - name: Booked value\n    definition: What customers are billed.\n'
        '    columns: [main.base.revenue, main.booked.booked]\n'
    )
    graph = Project(tmp_path).build(run_dbt=False, warehouses=False).graph
    found = {(c.idea, c.a.label, c.b.label): c for c in find_conflicts(graph).conflicts}
    assert set(found) == {
        ('revenue', 'base.revenue', 'other.revenue'),
        ('Booked value', 'base.revenue', 'booked.booked'),
    }
    assert found['revenue', 'base.revenue', 'other.revenue'].a.carried_into == ['copy.revenue']
    assert found['Booked value', 'base.revenue', 'booked.booked'].a.carried_into == []
    assert 'carried into `copy.revenue`' in find_conflicts(graph).markdown()
    # Only the pairs of one column, as the gate compares them, give the same.
    [revenue] = find_conflicts(graph, touching={'table.main.other#revenue'}).conflicts
    assert revenue.a.carried_into == ['copy.revenue']


JOINED_ON_TRUE = (
    'create table main.pairs as select a.id, b.amount from raw.orders a left join raw.orders b on true '
    'left join raw.orders c on c.customer_id = b.customer_id'
)


def _joined_on_true(root: Path) -> Path:
    """A project whose model joins a reading on an ON that does not name it, which the build lists
    once more naming the reading."""
    root.mkdir(parents=True, exist_ok=True)
    (root / 'konstruct.toml').write_text('dialect = "duckdb"\n')
    (root / 'pairs.sql').write_text(JOINED_ON_TRUE)
    assert main(['build', str(root), '--no-warehouse', '-q']) == 0
    return root


def test_a_join_named_for_its_reading_is_one_join(tmp_path: Path) -> None:
    from konstruct.analysis.conflicts import _Finder

    graph = Project(_joined_on_true(tmp_path / 'project')).graph
    _, joins = _Finder(graph).conditions(graph.nodes['table.main.pairs'])
    assert len(joins) == 2, joins
    assert not any('"orders#1" ON' in text for _, text, _ in joins)


# Precision: a pair is reported by default only when something more than a name links its sides.

STUDY_TABLES = """
create schema raw;
create table raw.orders (id int, customer_id int, order_date date, status varchar, shipping decimal(10,2));
create table raw.items (id int, order_id int, product_id int, price decimal(10,2), quantity int,
    list_price decimal(10,2), cost decimal(10,2));
create table raw.expenses (id int, department_id int, amount decimal(10,2));
create table raw.departments (id int, name varchar);
create table raw.payments (id int, order_id int, amount decimal(10,2));
"""
# Five ways one business number is computed twice, as real projects do it.
STUDY_MODELS = {
    # Order-level revenue that adds shipping, against item-level revenue.
    'order_revenue.sql': 'create table marts.order_revenue as select o.id as order_id, '
    'sum(i.price * i.quantity) + o.shipping as revenue from raw.orders o join raw.items i on i.order_id = o.id '
    'group by o.id, o.shipping',
    'item_revenue.sql': 'create table marts.item_revenue as select order_id, sum(price * quantity) as revenue '
    'from raw.items group by order_id',
    # Total expenses, where one side drops the rows with no department.
    'expenses.sql': 'create table finance.expenses as select sum(amount) as total_expenses from raw.expenses',
    'department_expenses.sql': 'create table ops.department_expenses as select sum(e.amount) as total_expenses '
    'from raw.expenses e join raw.departments d on d.id = e.department_id',
    # An order count limited to delivered orders in a fixed window, against every order.
    'delivered.sql': 'create table ops.delivered as select count(distinct id) as order_count from raw.orders '
    "where status = 'delivered' and order_date between date '2024-01-01' and date '2024-12-31'",
    'order_stats.sql': 'create table report.order_stats as select count(distinct id) as order_count from raw.orders',
    # Total revenue, where a join keeps only recent orders.
    'revenue.sql': 'create table finance.revenue as select sum(amount) as total_revenue from raw.payments',
    'recent_revenue.sql': 'create table marketing.recent_revenue as with recent as (select id from raw.orders '
    "where order_date >= date '2024-06-01') select sum(p.amount) as total_revenue from raw.payments p "
    'join recent r on r.id = p.order_id',
    # Margin as list price minus cost, against realised profit over net revenue.
    'product_margin.sql': 'create table products.product_margin as select product_id, avg(list_price - cost) as margin '
    'from raw.items group by product_id',
    'margin.sql': 'create table finance.margin as select product_id, '
    'sum(price * quantity - cost * quantity) / sum(price * quantity) as margin from raw.items group by product_id',
}
# Columns that share a name and nothing else: one name reused for other entities, and dimensions.
NOISE_TABLES = """
create schema raw;
create table raw.crypto (ts timestamp, close double);
create table raw.forex (ts timestamp, close double);
create table raw.football (match_id int, p_home double);
create table raw.tennis (match_id int, p_home double);
create table raw.shipments (id int, shipped boolean);
create table raw.tickets (id int, closed boolean);
"""
NOISE_MODELS = {
    'crypto.sql': 'create table markets.crypto_prices as select round(close, 2) as close_price, '
    'cast(ts as date) as event_date from raw.crypto',
    'forex.sql': 'create table markets.forex_prices as select round(close, 4) as close_price, '
    'cast(ts as date) as event_date from raw.forex',
    'football.sql': 'create table sport.football_odds as select match_id, 1 / p_home as home_odds from raw.football',
    'tennis.sql': 'create table sport.tennis_odds as select match_id, 1 / p_home as home_odds from raw.tennis',
    'shipments.sql': "create table ops.shipment_status as select id, case when shipped then 'shipped' "
    "else 'open' end as status from raw.shipments",
    'tickets.sql': "create table ops.ticket_status as select id, case when closed then 'closed' "
    "else 'open' end as status from raw.tickets",
}


def make_project(root: Path, tables: str, models: dict[str, str]) -> Project:
    root.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(root / 'warehouse.duckdb'))
    con.execute(tables)
    con.close()
    for name, sql in models.items():
        (root / name).write_text(sql)
    project = Project(root)
    project.build()
    return project


def pairs_of(conflicts: list[Any]) -> dict[frozenset[str], Any]:
    return {frozenset((c.a.label, c.b.label)): c for c in conflicts}


def unranked(conflict: Conflict) -> dict[str, Any]:
    return {k: v for k, v in conflict.to_dict().items() if k != 'rank'}


STUDY_PAIRS = {
    frozenset(
        {'order_revenue.revenue', 'item_revenue.revenue'}
    ): 'order_revenue.revenue also adds orders.shipping',
    frozenset({'expenses.total_expenses', 'department_expenses.total_expenses'}): (
        'department_expenses.total_expenses keeps only the rows that match on '
        'departments.id = expenses.department_id'
    ),
    frozenset({'delivered.order_count', 'order_stats.order_count'}): (
        "delivered.order_count counts only the rows where orders.status = 'delivered' and orders.order_date "
        "BETWEEN CAST('2024-01-01' AS DATE) AND CAST('2024-12-31' AS DATE)"
    ),
    frozenset({'revenue.total_revenue', 'recent_revenue.total_revenue'}): (
        "recent_revenue.total_revenue counts only the rows where orders.order_date >= CAST('2024-06-01' AS DATE)"
    ),
    frozenset({'product_margin.margin', 'margin.margin'}): (
        'margin.margin computes SUM(items.price * items.quantity - items.cost * items.quantity) / '
        'SUM(items.price * items.quantity), where product_margin.margin computes AVG(items.list_price - items.cost)'
    ),
}


@pytest.fixture(scope='module')
def study(tmp_path_factory: pytest.TempPathFactory) -> Project:
    return make_project(tmp_path_factory.mktemp('study') / 'shop', STUDY_TABLES, STUDY_MODELS)


@pytest.fixture(scope='module')
def noise(tmp_path_factory: pytest.TempPathFactory) -> Project:
    return make_project(tmp_path_factory.mktemp('noise') / 'markets', NOISE_TABLES, NOISE_MODELS)


def test_each_way_of_computing_one_number_twice_is_a_measure_conflict(study: Project) -> None:
    """Acceptance: the five pairs read the same source columns and differ by a filter, a join, a
    formula or an aggregation, so each is reported, with a plain explanation of the difference."""
    result = study.conflicts()
    measures = pairs_of(result.measures)
    assert set(measures) == set(STUDY_PAIRS)
    for pair, explanation in STUDY_PAIRS.items():
        assert measures[pair].kind == 'measure'
        assert measures[pair].explanation() == explanation, pair
    # Revenue from payments and revenue from items share a name and no source.
    named = pairs_of([c for c in result.conflicts if c.kind == 'name'])
    assert frozenset({'revenue.total_revenue', 'item_revenue.revenue'}) in named
    ranked = sorted(result.conflicts, key=lambda c: c.rank)
    assert ranked[: len(measures)] == sorted(result.measures, key=lambda c: c.rank)
    # The JSON keeps the order by downstream weight; the rank is the order by likelihood.
    assert [c.score for c in result.conflicts] == sorted((c.score for c in result.conflicts), reverse=True)


def test_pairs_that_share_only_a_name_are_not_reported_by_default(noise: Project) -> None:
    result = noise.conflicts()
    assert result.measures == []
    named = pairs_of(result.conflicts)
    assert set(named) == {
        frozenset({'crypto_prices.close_price', 'forex_prices.close_price'}),
        frozenset({'football_odds.home_odds', 'tennis_odds.home_odds'}),
        frozenset({'shipment_status.status', 'ticket_status.status'}),
        frozenset({'crypto_prices.event_date', 'forex_prices.event_date'}),
    }
    assert {c.kind for c in result.conflicts} == {'name'}
    assert result.overview() == (
        'No likely conflicting definitions among 8 computed columns; 4 more pairs share only a name '
        '(--all lists them).'
    )
    # The JSON, MCP and Python summary counts every pair, as it always has.
    assert result.summary() == '4 conflicting definitions among 8 computed columns.'
    assert str(result) == result.overview()
    result.every = True
    assert 'crypto_prices.close_price  vs  forex_prices.close_price' in str(result)
    assert 'read no source column in common' in str(result)
    result.every = False
    assert 'share only a name' in result.markdown()


def test_the_cli_lists_pairs_that_share_only_a_name_with_all(
    noise: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(['conflicts', '-C', str(noise.root), '--no-write']) == 0
    assert capsys.readouterr().out.startswith(
        'No likely conflicting definitions among 8 computed columns; 4 more'
    )
    assert main(['conflicts', '-C', str(noise.root), '--no-write', '--all']) == 0
    assert 'football_odds.home_odds  vs  tennis_odds.home_odds' in capsys.readouterr().out
    assert main(['conflicts', '-C', str(noise.root), '--no-write', '--json']) == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data['conflicts']) == 4 and {c['kind'] for c in data['conflicts']} == {'name'}
    assert all(c['explanation'] for c in data['conflicts'])


def test_columns_bound_to_one_term_are_measures_whatever_they_read(tmp_path: Path) -> None:
    """A business term binding both columns is a link a person made, not a name."""
    project = make_shop(
        tmp_path,
        invoices=True,
        definitions="""
        terms:
          - name: Sales
            definition: What was sold.
            columns: [daily_revenue.revenue, invoiced.sales_amount]
        """,
    )
    invoice = pairs_of(project.conflicts().conflicts)[
        frozenset({'daily_revenue.revenue', 'invoiced.sales_amount'})
    ]
    assert invoice.kind == 'measure'


def test_a_measure_is_a_number(tmp_path: Path) -> None:
    """A date, a label or a flag computed from the same source in two ways is not a measure."""
    con = duckdb.connect(str(tmp_path / 'warehouse.duckdb'))
    con.execute('create schema raw; create table raw.orders (id int, ts timestamp, shipped boolean)')
    con.close()
    (tmp_path / 'a.sql').write_text(
        "create table a.t as select cast(ts as date) as order_date, case when shipped then 'yes' "
        "else 'no' end as shipped_label from raw.orders"
    )
    (tmp_path / 'b.sql').write_text(
        "create table b.t as select date_trunc('month', ts) as order_date, case when shipped then 'y' "
        "else 'n' end as shipped_label from raw.orders"
    )
    project = Project(tmp_path)
    project.build()
    result = project.conflicts()
    assert len(result.conflicts) == 2 and result.measures == []


def test_the_explanation_of_a_having_and_of_an_outer_join() -> None:
    from konstruct.analysis.conflicts import Difference, _keeps

    having = Difference('filter', 'HAVING: SUM(orders.amount) > 100', 'a')
    assert _keeps('big.total', having) == 'big.total keeps only the groups where SUM(orders.amount) > 100'
    outer = Difference('join', 'LEFT JOIN ON: orders.id = payments.order_id', 'b')
    assert _keeps('orders.amount', outer) == (
        'orders.amount reads through a LEFT JOIN on orders.id = payments.order_id, which keeps the rows '
        'with no match'
    )


# Grain, lineage, comments and deduplications.

GRAIN_TABLES = """
create schema raw;
create table raw.orders (id int, customer_id int, order_date date, status varchar, amount decimal(10,2));
create table raw.history (id int, customer_id int, recorded_at date, score int);
"""


def test_the_same_measure_at_another_grain_is_not_a_conflict(tmp_path: Path) -> None:
    """Revenue per day and revenue per customer with its latest score joined on are one definition
    at two grains: the join the grain brings is not a difference of definition."""
    project = make_project(
        tmp_path / 'p',
        GRAIN_TABLES,
        {
            'daily.sql': 'create table m.daily as select order_date, sum(amount) as revenue from raw.orders '
            'group by order_date',
            'by_customer.sql': 'create table m.by_customer as select o.customer_id, sum(o.amount) as revenue, '
            'max(h.score) as score from raw.orders o left join raw.history h on h.customer_id = o.customer_id '
            'group by o.customer_id',
        },
    )
    result = project.conflicts()
    [pair] = result.conflicts
    assert pair.kind == 'grain' and result.measures == []
    assert [d.kind for d in pair.differences] == ['join']
    grains = {pair.a.label: pair.a.grain, pair.b.label: pair.b.grain}
    assert grains == {'daily.revenue': ['order_date'], 'by_customer.revenue': ['customer_id']}
    assert pair.explanation().startswith('the same measure at a different grain (per ')
    assert result.overview().endswith('1 more pair is one measure at another grain (--all lists them).')
    assert pair.to_dict()['kind'] == 'grain' and 'grain_sources' not in pair.to_dict()['a']


def test_a_filter_at_another_grain_is_still_a_conflict(tmp_path: Path) -> None:
    project = make_project(
        tmp_path / 'p',
        GRAIN_TABLES,
        {
            'daily.sql': 'create table m.daily as select order_date, sum(amount) as revenue from raw.orders '
            "where status = 'completed' group by order_date",
            'by_customer.sql': 'create table m.by_customer as select customer_id, sum(amount) as revenue '
            'from raw.orders group by customer_id',
        },
    )
    [pair] = project.conflicts().measures
    assert pair.explanation() == "daily.revenue counts only the rows where orders.status = 'completed'"


def test_one_filter_on_two_copies_of_a_column_is_no_difference(tmp_path: Path) -> None:
    """``fact.status = 'delivered'`` and ``dim.status = 'delivered'`` filter the same source column."""
    project = make_project(
        tmp_path / 'p',
        GRAIN_TABLES,
        {
            'fact.sql': 'create table s.fact as select id, customer_id, status, amount from raw.orders',
            'dim.sql': 'create table s.dim as select id, status from raw.orders',
            'a.sql': 'create table m.a as select customer_id, sum(f.amount) as revenue from s.fact f '
            "where f.status = 'delivered' group by customer_id",
            'b.sql': 'create table m.b as select f.customer_id, sum(f.amount) as revenue from s.fact f '
            "join s.dim d on d.id = f.id where d.status = 'delivered' group by f.customer_id",
        },
    )
    [pair] = project.conflicts().conflicts
    # The pair lists each condition as written, as it always has; read through lineage, the two
    # filters keep the same rows, so only the join tells the two apart.
    assert [d.kind for d in pair.differences] == ['filter', 'filter', 'join']
    assert pair.kind == 'measure'  # an inner join at one grain may keep fewer rows
    assert pair.explanation() == 'b.revenue keeps only the rows that match on dim.id = fact.id'


def test_a_comment_is_no_difference_and_is_not_quoted(tmp_path: Path) -> None:
    project = make_project(
        tmp_path / 'p',
        GRAIN_TABLES,
        {
            'a.sql': 'create table m.a as select customer_id, sum(amount) as revenue from raw.orders '
            "where status = 'completed' /* completed only */ group by customer_id",
            'b.sql': 'create table m.b as select customer_id, sum(amount) /* gross */ as revenue from raw.orders '
            "where status = 'completed' group by customer_id",
        },
    )
    # The conditions are listed as written, as they always have been, but a comment changes
    # nothing they count, and no explanation quotes one.
    for pair in project.conflicts().conflicts:
        assert pair.kind != 'measure' and not pair.confident
        assert 'gross' not in pair.explanation() and 'completed only' not in pair.explanation()


def test_a_deduplication_is_explained_and_one_off_the_measures_route_is_no_difference(tmp_path: Path) -> None:
    """A QUALIFY that keeps the latest history row per customer, on a relation joined by an outer
    join and not added up, changes no revenue; one on the rows added up reads as what it keeps."""
    latest = 'qualify row_number() over (partition by h.customer_id order by h.recorded_at desc) = 1'
    project = make_project(
        tmp_path / 'p',
        GRAIN_TABLES,
        {
            'a.sql': 'create table m.a as with h as (select * from raw.history h ' + latest + ') '
            'select o.customer_id, sum(o.amount) as revenue, max(h.score) as score from raw.orders o '
            'left join h on h.customer_id = o.customer_id group by o.customer_id',
            'b.sql': 'create table m.b as select customer_id, sum(amount) as revenue from raw.orders group by customer_id',
            'c.sql': 'create table m.c as select customer_id, sum(amount) as revenue from (select * from '
            'raw.orders o qualify row_number() over (partition by o.id order by o.order_date desc) = 1) '
            'group by customer_id',
        },
    )
    pairs = pairs_of(project.conflicts().conflicts)
    off_route = pairs.get(frozenset({'a.revenue', 'b.revenue'}))
    assert off_route is None or (off_route.kind != 'measure' and 'latest' not in off_route.explanation())
    deduped = pairs[frozenset({'b.revenue', 'c.revenue'})]
    assert (
        deduped.explanation() == 'c.revenue counts only the latest row per orders.id (by orders.order_date)'
    )


def test_a_key_counted_distinctly_is_what_the_count_is_by(tmp_path: Path) -> None:
    """Revenue over COUNT(DISTINCT id) at another grain reads the key only to count by it."""
    project = make_project(
        tmp_path / 'p',
        GRAIN_TABLES,
        {
            'a.sql': 'create table m.a as select customer_id, sum(amount) / count(distinct id) as avg_order_value '
            'from raw.orders group by customer_id',
            'b.sql': 'create table m.b as select order_date, sum(amount) / count(*) as avg_order_value '
            'from raw.orders group by order_date',
        },
    )
    [pair] = project.conflicts().conflicts
    [source] = [d for d in pair.differences if d.kind == 'source']
    assert source.counted and pair.kind == 'grain'
    assert 'counted' not in source.to_dict()


def test_the_explanation_is_bounded_and_shows_where_the_formulas_differ(tmp_path: Path) -> None:
    long = ' + '.join(f'coalesce(amount, {i})' for i in range(40))
    project = make_project(
        tmp_path / 'p',
        GRAIN_TABLES,
        {
            'a.sql': f'create table m.a as select id, {long} as revenue from raw.orders',
            'b.sql': 'create table m.b as select id, amount * 2 as revenue from raw.orders',
        },
    )
    [pair] = project.conflicts().measures
    explanation = pair.explanation()
    assert explanation.startswith(
        'a.revenue computes COALESCE(orders.amount, 0) + COALESCE(orders.amount, 1)'
    )
    assert explanation.endswith(' ..., where b.revenue computes orders.amount * 2')
    assert len(explanation) < 2 * SHOWN


def test_readings_of_one_relation_are_shown_apart() -> None:
    """Two readings of one relation are two columns: a self-join's sides never read as one."""
    from konstruct.analysis.conflicts import _readable

    text = 'COALESCE("evolutions#1/1".pokemon, "evolutions#1/2".pokemon) = "evolutions#1/1".pokemon'
    assert _readable(text) == 'COALESCE(evolutions.pokemon, "evolutions#2".pokemon) = evolutions.pokemon'
    assert _readable('a  =\n\t1') == 'a = 1'


def test_the_most_likely_differences_of_definition_come_first(study: Project) -> None:
    measures = sorted(study.conflicts().measures, key=lambda c: c.rank)
    likelihoods = [c.likelihood for c in measures]
    assert likelihoods == sorted(likelihoods, reverse=True)
    assert all(c.to_dict()['likelihood'] == c.likelihood for c in measures)


def test_the_exit_status_counts_every_pair_unless_asked_for_measures(
    noise: Project, study: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--fail-on-conflict`` keeps its meaning and says why it fails with no conflicting
    definition; ``--fail-on-measure-conflict`` fails on a conflicting definition alone."""
    assert main(['conflicts', '-C', str(noise.root), '--no-write', '--fail-on-conflict']) == 1
    assert '--fail-on-conflict counts every pair, and 4 share only a name' in capsys.readouterr().err
    assert main(['conflicts', '-C', str(noise.root), '--no-write', '--fail-on-measure-conflict']) == 0
    assert main(['conflicts', '-C', str(study.root), '--no-write', '--fail-on-measure-conflict']) == 1


def test_the_gate_says_what_kind_of_pair_it_fails_on(tmp_path: Path) -> None:
    """A changed column that shares only a name with another fails the gate as before, saying so;
    with ``measure_conflicts_only`` it does not."""
    import subprocess

    root = tmp_path / 'repo'
    project = make_project(root, NOISE_TABLES, {'crypto.sql': NOISE_MODELS['crypto.sql']})
    git = ['git', '-C', str(root), '-c', 'user.name=t', '-c', 'user.email=t@e', '-c', 'commit.gpgsign=false']
    subprocess.run(['git', 'init', '-q', '-b', 'main', str(root)], check=True)
    subprocess.run([*git, 'add', '-A'], check=True)
    subprocess.run([*git, 'commit', '-qm', 'base'], check=True)
    (root / 'forex.sql').write_text(NOISE_MODELS['forex.sql'])
    gated = project.gate(base='main')
    found = [f for f in gated.findings if f.rule == 'conflicting-definition']
    assert found and all(f.message.endswith('; they share only a name)') for f in found)
    assert not gated.passed
    quiet = project.gate(base='main', measure_conflicts_only=True)
    assert all(f.rule != 'conflicting-definition' for f in quiet.findings)


# Precise or silent: only a pair very likely to be two definitions of one number is confident.

PRECISION_TABLES = """
create schema raw;
create table raw.trips (id int, license varchar, status varchar, fare decimal(10,2), month int);
create table raw.travel (id int, miles double, mode varchar);
create table raw.vehicles (id int, distance double, mode varchar);
create table raw.factors (mode varchar, kgco2e double);
create table raw.subscriptions (id int, status varchar, mrr decimal(10,2));
"""


def precise(tmp_path: Path, models: dict[str, str]) -> list[Conflict]:
    return find_conflicts(make_project(tmp_path / 'p', PRECISION_TABLES, models).graph).conflicts


def test_two_segments_of_one_column_are_not_a_conflict(tmp_path: Path) -> None:
    """Each side keeps values of one column the other leaves out: one measure over two segments."""
    [pair] = precise(
        tmp_path,
        {
            'black.sql': 'create table m.black as select month, sum(fare) as fares from raw.trips '
            "where license like '%Black%' group by month",
            'green.sql': 'create table m.green as select month, sum(fare) as fares from raw.trips '
            "where license like '%Green%' group by month",
        },
    )
    assert (pair.kind, pair.confident) == ('segment', False)
    assert pair.explanation().startswith('the same measure over another segment: ')
    assert "counts only the rows where trips.license LIKE '%Black%'" in pair.explanation()


def test_a_filter_on_a_grouping_column_only_drops_a_group(tmp_path: Path) -> None:
    [pair] = precise(
        tmp_path,
        {
            'a.sql': 'create table m.a as select license, sum(fare) as fares from raw.trips group by license',
            'b.sql': 'create table m.b as select license, sum(fare) as fares from raw.trips '
            "where license <> 'Unknown' group by license",
        },
    )
    assert pair.kind == 'grain' and not pair.confident


def test_having_and_where_on_a_grouped_column_keep_the_same_rows(tmp_path: Path) -> None:
    conflicts = precise(
        tmp_path,
        {
            'a.sql': 'create table m.a as select month, sum(fare) as fares from raw.trips '
            'group by month having month >= 3',
            'b.sql': 'create table m.b as select month, sum(fare) as fares from raw.trips '
            'where month >= 3 group by month',
        },
    )
    # Listed, as the two conditions are written differently, but each drops whole months alike.
    assert all(c.kind == 'grain' and not c.confident for c in conflicts)


def test_two_amounts_that_share_only_a_lookup_table_are_not_one_measure(tmp_path: Path) -> None:
    [pair] = precise(
        tmp_path,
        {
            'travel.sql': 'create table m.travel as select t.id, t.miles * f.kgco2e as total_kgco2e '
            'from raw.travel t join raw.factors f on f.mode = t.mode',
            'fleet.sql': 'create table m.fleet as select v.id, v.distance * f.kgco2e as total_kgco2e '
            'from raw.vehicles v join raw.factors f on f.mode = v.mode',
        },
    )
    assert pair.kind == 'name'
    assert pair.title == 'kgco2e'  # the words of the name, not the stems search reads


def test_a_filter_in_front_of_an_aggregate_is_confident_and_one_per_row_is_not(tmp_path: Path) -> None:
    """A filter in front of no aggregate only drops rows: every row both have holds one value."""
    per_row = precise(
        tmp_path / 'rows',
        {
            'a.sql': 'create table m.a as select id, fare * 1 as revenue from raw.trips',
            'b.sql': "create table m.b as select id, fare * 1 as revenue from raw.trips where status <> 'void'",
        },
    )
    [row] = per_row
    assert row.kind == 'measure' and not row.confident
    [total] = precise(
        tmp_path / 'totals',
        {
            'a.sql': 'create table m.a as select month, sum(fare) as revenue from raw.trips group by month',
            'b.sql': 'create table m.b as select month, sum(fare) as revenue from raw.trips '
            "where status <> 'void' group by month",
        },
    )
    assert total.confident and total.rank == 1 and total.to_dict()['confident'] is True
    assert total.explanation() == "b.revenue counts only the rows where trips.status <> 'void'"


def test_a_negated_test_reads_as_it_is_written(tmp_path: Path) -> None:
    """sqlglot reads ``status NOT IN (...)`` as a NOT in front of the test; the explanation writes it
    back as people write it."""
    [total] = precise(
        tmp_path,
        {
            'a.sql': 'create table m.a as select month, sum(fare) as revenue from raw.trips group by month',
            'b.sql': 'create table m.b as select month, sum(fare) as revenue from raw.trips '
            "where status not in ('void', 'refunded') and tip is not null group by month",
        },
    )
    assert total.explanation() == (
        "b.revenue counts only the rows where trips.status NOT IN ('void', 'refunded') and trips.tip IS NOT NULL"
    )


def test_two_overlapping_populations_say_what_each_counts(tmp_path: Path) -> None:
    """``<> 'canceled'`` and ``= 'active'`` keep values in common, so they are two definitions of
    one population, not two segments; the explanation says both rules."""
    [pair] = precise(
        tmp_path,
        {
            'accounts.sql': "create table m.accounts as select id, case when status <> 'canceled' "
            'then mrr * 12 end as arr from raw.subscriptions',
            'subs.sql': "create table m.subs as select id, case when status = 'active' then mrr * 12 end "
            'as arr from raw.subscriptions',
        },
    )
    assert pair.kind == 'measure' and pair.confident
    assert pair.explanation() == (
        "accounts.arr counts only the rows where subscriptions.status <> 'canceled'; "
        "subs.arr counts only the rows where subscriptions.status = 'active'"
    )


def test_a_filter_in_a_cte_the_column_never_meets_is_not_its_filter(tmp_path: Path) -> None:
    found = precise(
        tmp_path,
        {
            'a.sql': "create table m.a as with v as (select count(*) as n from raw.travel where mode = 'air') "
            'select month, sum(fare) as fares, (select n from v) as flights from raw.trips group by month',
            'b.sql': 'create table m.b as select month, sum(fare) as fares from raw.trips group by month '
            'having sum(fare) > 0',
        },
    )
    assert all(
        'mode' not in condition for pair in found for _, condition, _ in pair.a.counted + pair.b.counted
    )
    assert all('mode' not in pair.explanation() for pair in found)


def test_the_difference_of_two_formulas_is_shown_whole_in_readable_names() -> None:
    from konstruct.analysis.conflicts import _differing

    first = 'CASE WHEN "orders#1/2".status = \'paid\' THEN SUM("orders#1/2".amount) ELSE 0 END'
    second = 'CASE WHEN "orders#1/2".status = \'paid\' THEN SUM("orders#1/2".amount * 0.9) ELSE 0 END'
    assert _differing(first, second) == ('orders.amount', 'orders.amount * 0.9')


@pytest.mark.parametrize('measure', ['count(*)', 'sum(1)', '1'])
def test_a_value_of_no_source_column_keeps_the_filters_of_its_query(tmp_path: Path, measure: str) -> None:
    """``COUNT(*)``, ``SUM(1)`` and a constant read no source column, so their route back to source
    is their own query's: its WHERE is on it, and the pair is reported with it."""
    project = make_project(
        tmp_path / 'p',
        'create schema raw; create table raw.orders (id int, customer_id int, status varchar, amount int)',
        {
            'a.sql': f'create table m.completed as select customer_id, {measure} as order_count '
            "from raw.orders where status = 'completed' group by customer_id",
            'b.sql': f'create table m.every as select customer_id, {measure} as order_count '
            'from raw.orders group by customer_id',
        },
    )
    [pair] = project.conflicts().conflicts
    assert [(d.kind, d.side) for d in pair.differences] == [('filter', 'a')]
    assert "orders.status = 'completed'" in pair.differences[0].detail
    assert [c for _, c, _ in pair.a.filters] == [pair.differences[0].detail] and pair.b.filters == []
    assert pair.a.counted == pair.a.filters
    assert pair.explanation() == "completed.order_count keeps only the rows where orders.status = 'completed'"
