"""konstruct check-sql: ad-hoc SQL checked against the project's definitions and ontology."""

from __future__ import annotations

import ast
import itertools
import json
import os
import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

import duckdb
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from konstruct import Project
from konstruct.analysis.checksql import (
    RULES,
    Adoption,
    Checker,
    Finding,
    HistoryCheck,
    SqlCheck,
    check_history,
    check_sql,
    pseudonym,
    reachable,
)
from konstruct.core.graph import Graph
from konstruct.inputs.usage import HistoryQuery, QueryHistory, UsageSettings, fetch_history

from .conftest import cpu_seconds, dbt_executable

WAREHOUSE = """
create schema raw;
create table raw.customers (customer_id int primary key, name varchar, region varchar);
create table raw.orders (
    order_id int primary key, customer_id int, amount decimal(10, 2), status varchar, order_date date
);
create table raw.payments (payment_id int primary key, order_id int, amount decimal(10, 2), method varchar);
"""
CUSTOMER_REVENUE = """\
create table mart.customer_revenue as
select c.customer_id, c.region, sum(o.amount) as net_revenue, sum(o.amount) as gross_revenue
from raw.customers c
join raw.orders o on o.customer_id = c.customer_id
where o.status <> 'returned'
group by c.customer_id, c.region
"""
ORDER_PAYMENTS = """\
create table mart.order_payments as
select o.order_id, sum(p.amount) as paid
from raw.orders o
join raw.payments p on p.order_id = o.order_id
group by o.order_id
"""
DEFINITIONS = """\
version: 1
terms:
  - name: Net revenue
    kind: measure
    synonyms: [revenue]
    definition: Order value from orders that were not returned.
    owner: {name: Ana Ruiz, role: Finance}
    status: approved
    approved: 2026-10-01
    grain: one row per customer
    columns: [customer_revenue.net_revenue]
    filters: ["orders.status <> 'returned'"]
  - name: Gross revenue
    kind: measure
    definition: Order value before returns; replaced by net revenue.
    status: retired
    columns: [customer_revenue.gross_revenue]
    related: [Net revenue]
  - name: Paid amount
    kind: measure
    definition: What customers paid against an order.
    status: draft
    columns: [order_payments.paid]
  - name: Order amount
    kind: measure
    synonyms: [order value, order total]
    definition: What the customer was charged for an order.
    owner: {name: Ana Ruiz, role: Finance}
    status: approved
    approved: 2026-10-01
    columns: [orders.amount]
"""


def make_shop(root: Path, *, definitions: str | None = DEFINITIONS, dialect: str | None = None) -> Graph:
    con = duckdb.connect(str(root / 'shop.duckdb'))
    con.execute(WAREHOUSE)
    con.close()
    (root / 'mart').mkdir()
    (root / 'mart' / 'customer_revenue.sql').write_text(CUSTOMER_REVENUE)
    (root / 'mart' / 'order_payments.sql').write_text(ORDER_PAYMENTS)
    if definitions:
        (root / 'definitions.yml').write_text(definitions)
    return Project(root).build(run_dbt=False, dialect=dialect).graph


BRIDGE_WAREHOUSE = """
create schema raw;
create table raw.customers (customer_id int primary key, name varchar, country varchar, signup_date date);
create table raw.segments (segment_id int primary key, segment_name varchar);
create table raw.customer_segments (customer_id int, segment_id int, primary key (customer_id, segment_id));
create table raw.customer_notes (customer_id int, note varchar);
create table raw.orders (
    order_id int primary key, customer_id int, order_date date, status varchar, channel varchar,
    amount decimal(12, 2), legacy_amount decimal(12, 2)
);
create table raw.order_items (item_id int primary key, order_id int, product_id int, quantity int, price decimal(12, 2));
create table raw.products (product_id int primary key, category varchar, list_price decimal(12, 2));
"""
BRIDGE_MODELS = {
    'order_revenue': """\
create table mart.order_revenue as
select o.order_id, o.customer_id, o.order_date, sum(i.quantity * i.price) as item_revenue
from raw.orders o
join raw.order_items i on i.order_id = o.order_id
where o.status <> 'cancelled'
group by o.order_id, o.customer_id, o.order_date
""",
    'customer_value': """\
create table mart.customer_value as
select c.customer_id, c.country, sum(o.amount) as completed_revenue, count(*) as completed_orders
from raw.customers c
join raw.orders o on o.customer_id = c.customer_id
where o.status = 'completed' and o.order_date >= date '2020-01-01'
group by c.customer_id, c.country
""",
    'segment_revenue': """\
create table mart.segment_revenue as
select cs.segment_id, sum(o.amount) as segment_revenue
from raw.customer_segments cs
join raw.orders o on o.customer_id = cs.customer_id
where o.status = 'completed'
group by cs.segment_id
""",
}
BRIDGE_DEFINITIONS = """\
version: 1
terms:
  - name: Completed revenue
    kind: measure
    synonyms: [booked revenue]
    definition: Order value of completed orders since 2020.
    owner: {name: Ana Ruiz, role: Finance}
    status: approved
    approved: 2026-09-01
    columns: [customer_value.completed_revenue]
    filters: ["orders.status = 'completed'", "orders.order_date >= '2020-01-01'"]
  - name: Item revenue
    kind: measure
    definition: Quantity times price of every item of an order that was not cancelled.
    owner: {name: Ana Ruiz, role: Finance}
    status: approved
    approved: 2026-09-01
    columns: [order_revenue.item_revenue]
    filters: ["orders.status <> 'cancelled'"]
  - name: Order amount
    kind: measure
    definition: What the customer was charged for an order.
    status: agreed
    columns: [orders.amount]
  - name: Legacy amount
    definition: The order amount of the old billing system.
    status: retired
    columns: [orders.legacy_amount]
    related: [Order amount]
"""


def make_bridge(root: Path, *, dialect: str | None = None) -> Graph:
    """A shop whose customers belong to segments through a bridge table with a composite key."""
    con = duckdb.connect(str(root / 'bridge.duckdb'))
    con.execute(BRIDGE_WAREHOUSE)
    con.close()
    (root / 'mart').mkdir()
    for name, sql in BRIDGE_MODELS.items():
        (root / 'mart' / f'{name}.sql').write_text(sql)
    (root / 'definitions.yml').write_text(BRIDGE_DEFINITIONS)
    return Project(root).build(run_dbt=False, dialect=dialect).graph


@pytest.fixture(scope='module')
def shop(tmp_path_factory: pytest.TempPathFactory) -> Checker:
    return Checker(make_shop(tmp_path_factory.mktemp('shop')))


@pytest.fixture(scope='module')
def bare(tmp_path_factory: pytest.TempPathFactory) -> Checker:
    """The same shop without definitions.yml: only what Konstruct derives."""
    return Checker(make_shop(tmp_path_factory.mktemp('bare'), definitions=None))


@pytest.fixture(scope='module')
def bridge(tmp_path_factory: pytest.TempPathFactory) -> Checker:
    return Checker(make_bridge(tmp_path_factory.mktemp('bridge')))


@pytest.fixture(scope='module')
def dialects(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Checker]:
    """The shop read as Snowflake and as BigQuery SQL."""
    return {
        dialect: Checker(make_shop(tmp_path_factory.mktemp(f'shop_{dialect}'), dialect=dialect))
        for dialect in ('snowflake', 'bigquery')
    }


def only(result: SqlCheck, rule: str) -> Finding:
    found = [f for f in result.findings if f.rule == rule]
    assert len(found) == 1, str(result)
    return found[0]


def rules(result: SqlCheck) -> list[str]:
    return sorted(f.rule for f in result.findings)


def loud(result: SqlCheck) -> list[str]:
    return sorted(f.rule for f in result.findings if f.severity != 'info')


def flat(sql: str) -> str:
    return ' '.join(sql.split())


# Data to run a query and its correction on, so a correction can be seen to change its measure and
# nothing else. Customers 11 and 12 have only returned orders, and order 9 only cancelled items.


def shop_rows(con: duckdb.DuckDBPyConnection) -> None:
    statuses = ['completed', 'returned', 'shipped']
    for c in range(1, 13):
        con.execute(
            'insert into raw.customers values (?, ?, ?)',
            [c, f'customer {c}', ['North', 'South', 'East'][c % 3]],
        )
    payment = 1
    for o in range(1, 61):
        customer = 11 + o % 2 if o > 50 else o % 10 + 1
        status = 'returned' if o > 50 else statuses[o % 3]
        date = f'2026-{o % 12 + 1:02d}-{o % 27 + 1:02d}'
        con.execute('insert into raw.orders values (?, ?, ?, ?, ?)', [o, customer, o * 1.5, status, date])
        for p in range(o % 3):
            con.execute(
                'insert into raw.payments values (?, ?, ?, ?)',
                [payment, o, o / 2, ['card', 'coupon', 'cash'][p]],
            )
            payment += 1


def bridge_rows(con: duckdb.DuckDBPyConnection) -> None:
    for c in range(1, 9):
        con.execute(
            'insert into raw.customers values (?, ?, ?, ?)', [c, f'c{c}', ['UK', 'ES'][c % 2], '2019-06-01']
        )
        for segment in range(1, c % 3 + 2):
            con.execute('insert into raw.customer_segments values (?, ?)', [c, segment])
    for segment in range(1, 4):
        con.execute('insert into raw.segments values (?, ?)', [segment, f'segment {segment}'])
    for product in range(1, 5):
        con.execute(
            'insert into raw.products values (?, ?, ?)',
            [product, ['toys', 'books'][product % 2], product * 3.0],
        )
    item = 1
    for o in range(1, 41):
        status = ['completed', 'cancelled', 'pending'][o % 3]
        date = f'{[2019, 2020, 2026][o % 3]}-{o % 12 + 1:02d}-01'
        con.execute(
            'insert into raw.orders values (?, ?, ?, ?, ?, ?, ?)',
            [o, o % 8 + 1, date, status, ['web', 'store'][o % 2], o * 2.0, o * 2.0],
        )
        for i in range(o % 3 + 1):
            con.execute(
                'insert into raw.order_items values (?, ?, ?, ?, ?)', [item, o, i + 1, i + 1, o * 0.5]
            )
            item += 1


def database(
    root: Path, warehouse: str, models: list[str], rows: Callable[[duckdb.DuckDBPyConnection], None]
) -> Path:
    path = root / 'data.duckdb'
    con = duckdb.connect(str(path))
    con.execute(warehouse)
    rows(con)
    con.execute('create schema mart')
    for model in models:
        con.execute(model)
    con.close()
    return path


@pytest.fixture(scope='module')
def shop_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return database(
        tmp_path_factory.mktemp('shop_db'), WAREHOUSE, [CUSTOMER_REVENUE, ORDER_PAYMENTS], shop_rows
    )


@pytest.fixture(scope='module')
def bridge_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return database(
        tmp_path_factory.mktemp('bridge_db'), BRIDGE_WAREHOUSE, list(BRIDGE_MODELS.values()), bridge_rows
    )


def runs_alike(db: Path, sql: str, result: SqlCheck, dialect: str | None = None) -> None:
    """The corrected SQL runs on DuckDB, keeps the query's columns, and changes only the columns of
    its findings: no other column's values, and no row of the result, come or go."""
    import sqlglot

    assert result.corrected is not None, str(result)
    if dialect:
        sql = sqlglot.transpile(sql, read=dialect, write='duckdb')[0]
        corrected = ';\n'.join(sqlglot.transpile(result.corrected, read=dialect, write='duckdb'))
    else:
        corrected = result.corrected
    changed = {f.column for f in result.findings if f.rewrite and f.column}
    con = duckdb.connect(str(db), read_only=True)
    try:
        before = con.execute(sql)
        names = [d[0].lower() for d in before.description]
        rows = before.fetchall()
        after = con.execute(corrected)
        assert [d[0].lower() for d in after.description] == names
        fixed = after.fetchall()
    finally:
        con.close()
    kept = [i for i, name in enumerate(names) if name not in changed]
    assert sorted(str([r[i] for i in kept]) for r in rows) == sorted(str([r[i] for i in kept]) for r in fixed)


# Required filters


NET_REVENUE = """
select c.region, sum(o.amount) as net_revenue
from raw.orders o join raw.customers c on c.customer_id = o.customer_id
group by c.region
"""


def test_a_measure_without_its_required_filter_is_an_error_with_the_filter_added(
    shop: Checker, shop_db: Path
) -> None:
    result = shop.check(NET_REVENUE)
    finding = only(result, 'missing-filter')
    assert (finding.severity, finding.column, finding.term) == ('error', 'net_revenue', 'Net revenue')
    assert "orders.status <> 'returned'" in finding.message
    # Grouped by region: a WHERE clause would drop a region whose orders were all returned.
    assert finding.rewrite == "o.status <> 'returned' applied to SUM(o.amount) alone"
    assert not result.passed and result.failed()
    assert result.corrected is not None
    assert "SUM(o.amount) FILTER(WHERE o.status <> 'returned') AS net_revenue" in flat(result.corrected)
    # The corrected SQL computes the measure as its column does, which is only a note.
    assert rules(shop.check(result.corrected)) == ['recomputes-measure']
    runs_alike(shop_db, NET_REVENUE, result)


ORDER_AMOUNTS = """\
create table mart.order_amounts as
select o.order_id, o.status, sum(p.amount) as amount,
    sum(case when p.method = 'card' then p.amount else 0 end) as card_amount,
    sum(case when o.status = 'returned' then p.amount else 0 end) as returned_amount,
    count(*) as payments
from raw.orders o
join raw.payments p on p.order_id = o.order_id
group by o.order_id, o.status
"""


AMOUNTS_DEFINITIONS = """\
version: 1
terms:
  - name: Net amount
    kind: measure
    status: approved
    approved: 2026-10-01
    owner: {name: Ana Ruiz}
    columns: [order_amounts.amount]
    filters: ["order_amounts.status = 'completed'"]
"""


@pytest.fixture(scope='module')
def amounts(tmp_path_factory: pytest.TempPathFactory) -> Checker:
    """The shop with each order's payments added up in total and by card."""
    root = tmp_path_factory.mktemp('amounts')
    make_shop(root, definitions=AMOUNTS_DEFINITIONS)
    (root / 'mart' / 'order_amounts.sql').write_text(ORDER_AMOUNTS)
    return Checker(Project(root).build(run_dbt=False).graph)


def test_a_column_that_computes_a_measure_over_some_of_its_rows_needs_its_filters(amounts: Checker) -> None:
    """card_amount adds up the same payment amounts as amount, the column of 'Net amount', over the
    card payments alone: it is net amount by card, so it needs the filter net amount requires. A
    column with the same formula over the same rows, or a count, is not a slice of it."""
    finding = only(amounts.check('select sum(card_amount) from mart.order_amounts'), 'missing-filter')
    assert (finding.severity, finding.term) == ('warning', 'Net amount')
    assert finding.evidence == [
        "required by term 'Net amount' (definitions.yml:3)",
        "order_amounts.card_amount computes 'Net amount' as order_amounts.amount does, over the rows where "
        "payments.method = 'card'",
    ]
    assert finding.rewrite == "order_amounts.status = 'completed' added to the WHERE clause"
    for sql in (
        "select sum(card_amount) from mart.order_amounts where status = 'completed'",
        'select sum(payments) from mart.order_amounts',
        'select sum(paid) from mart.order_payments',
    ):
        assert loud(amounts.check(sql)) == [], sql


def test_a_column_s_not_null_filter_on_its_group_key_is_no_filter_of_the_same_groups(tmp_path: Path) -> None:
    """Grouped by order, WHERE order_id IS NOT NULL drops only the group of payments with no order,
    and changes no other group's average: a query grouped by order too has those groups and one
    more, for NULL, so it needs no such filter. A query grouped otherwise (by method) averages the
    payments with no order into its groups, which DuckDB shows differ, so it needs the filter; and
    a filter on other columns still counts."""
    root = tmp_path / 'shop'
    root.mkdir()
    make_shop(root)
    model = (
        'create table mart.order_averages as select order_id, avg(amount) as avg_paid, '
        "avg(amount) filter (where method <> 'voucher') as avg_kept "
        'from raw.payments where order_id is not null group by order_id'
    )
    (root / 'mart' / 'order_averages.sql').write_text(model)
    checker = Checker(Project(root).build(run_dbt=False).graph)
    by_order = 'select order_id, avg(amount) as avg_paid from raw.payments group by order_id'
    assert 'missing-filter' not in rules(checker.check(by_order))
    assert 'missing-filter' not in rules(checker.check(by_order.replace('group by order_id', 'group by 1')))
    by_method = 'select method, avg(amount) as avg_paid from raw.payments group by method'
    finding = only(checker.check(by_method), 'missing-filter')
    assert 'order_id IS NULL' in finding.message
    for sql in (by_method, 'select avg(amount) as avg_paid from raw.payments'):
        assert 'missing-filter' in rules(checker.check(sql)), sql
    con = duckdb.connect()
    con.execute(WAREHOUSE)
    con.execute("insert into raw.payments values (1, 1, 10, 'card'), (2, null, 30, 'card')")
    assert (
        con.execute(by_method).fetchall()
        != con.execute(by_method.replace('group by', 'where order_id is not null group by')).fetchall()
    )
    finding = only(checker.check(by_order.replace('avg_paid', 'avg_kept')), 'missing-filter')
    assert "method <> 'voucher'" in finding.message and 'NULL' not in finding.message
    # Written in the query too, the filter is the model's own: the same SQL recomputes it.
    same = by_order.replace('from raw.payments', 'from raw.payments where order_id is not null')
    assert rules(checker.check(same)) == ['recomputes-measure']


def test_a_comparison_on_a_column_implies_it_is_not_null(tmp_path: Path) -> None:
    """A comparison is NULL where its column is, so a query that keeps payments of 10 or more keeps
    no payment with a NULL amount: it applies amount IS NOT NULL. One that tests the column only
    inside an OR, or with IS DISTINCT FROM, does not."""
    root = tmp_path / 'shop'
    root.mkdir()
    make_shop(root)
    (root / 'mart' / 'method_averages.sql').write_text(
        'create table mart.method_averages as select method, avg(order_id) as avg_order '
        'from raw.payments where amount is not null group by method'
    )
    checker = Checker(Project(root).build(run_dbt=False).graph)
    base = 'select method, avg(order_id) as avg_order from raw.payments where {} group by method'
    for kept in ('amount >= 10', 'amount <> 0', 'amount in (1, 2)', 'not (amount < 0)'):
        assert 'missing-filter' not in rules(checker.check(base.format(kept))), kept
    for loose in ("amount >= 10 or method = 'card'", 'amount is distinct from 0'):
        assert 'missing-filter' in rules(checker.check(base.format(loose))), loose


ACTIVE_WAREHOUSE = """
create schema raw;
create table raw.orders (order_id int primary key, customer_id int, amount int, status varchar);
create table raw.customers (customer_id int primary key, first_order_id int);
"""
ACTIVE_DATA = """
create schema raw; create schema stg;
create table raw.orders as select * from (values (1, 10, 100, 'x'), (2, 10, 50, null)) v(order_id, customer_id, amount, status);
create view stg.stg_orders as select order_id, customer_id, status from raw.orders;
create table raw.customers as select * from (values (10, 1)) v(customer_id, first_order_id);
"""


def test_a_comparison_on_a_row_tied_to_the_measure_s_is_no_filter_of_its_rows(tmp_path: Path) -> None:
    """Active amount adds up orders whose status is not NULL. A comparison on the status of the
    customer's first order, or of the row a deduplicated subquery keeps for the customer, says
    nothing of the status of the orders summed: DuckDB shows 150 where the term gives 100. On
    the summed order's own status it is the filter."""
    con = duckdb.connect(str(tmp_path / 'w.duckdb'))
    con.execute(ACTIVE_WAREHOUSE)
    con.close()
    (tmp_path / 'stg').mkdir()
    (tmp_path / 'stg' / 'stg_orders.sql').write_text(
        'create view stg.stg_orders as select order_id, customer_id, status from raw.orders'
    )
    (tmp_path / 'definitions.yml').write_text(
        'version: 1\nterms:\n  - name: Active amount\n    kind: measure\n    status: approved\n'
        '    approved: 2026-10-01\n    owner: {name: Ana Ruiz}\n    columns: [raw.orders.amount]\n'
        '    filters: ["orders.status IS NOT NULL"]\n'
    )
    checker = Checker(Project(tmp_path).build(run_dbt=False).graph)
    data = duckdb.connect()
    data.execute(ACTIVE_DATA)
    truth = data.execute('select sum(amount) from raw.orders where status is not null').fetchall()
    for sql in (
        'select sum(o.amount) as active_amount from raw.orders o join (select customer_id, status '
        'from stg.stg_orders qualify row_number() over (partition by customer_id order by status nulls last) = 1) s '
        "on s.customer_id = o.customer_id where s.status = 'x'",
        'select sum(o.amount) as active_amount from raw.orders o join raw.customers c '
        "on c.customer_id = o.customer_id join raw.orders f on f.order_id = c.first_order_id where f.status = 'x'",
    ):
        assert 'missing-filter' in rules(checker.check(sql)), sql
        assert data.execute(sql).fetchall() != truth
    own = "select sum(o.amount) as active_amount from raw.orders o where o.status = 'x'"
    assert 'missing-filter' not in rules(checker.check(own))


def test_a_slice_on_the_column_a_measure_filters_is_not_that_measure(amounts: Checker) -> None:
    """returned_amount keeps the payments of returned orders: a slice on the very column net amount
    filters is another idea, not net amount for some rows. A query that filters or groups by that
    column chose its rows itself: at most a note, and the SQL is not rewritten."""
    assert 'missing-filter' not in rules(amounts.check('select sum(returned_amount) from mart.order_amounts'))
    for sql in (
        "select sum(card_amount) from mart.order_amounts where status = 'returned'",
        'select status, sum(card_amount) from mart.order_amounts group by status',
    ):
        result = amounts.check(sql)
        assert loud(result) == [] and result.corrected is None, sql


def test_a_filter_two_terms_require_is_added_once(tmp_path: Path) -> None:
    """Two terms of one column require the same filter: each finding names its term, and the
    corrected SQL applies the filter once."""
    second = AMOUNTS_DEFINITIONS + AMOUNTS_DEFINITIONS.split('terms:\n', 1)[1].replace(
        'Net amount', 'Average net amount'
    )
    root = tmp_path / 'shop'
    root.mkdir()
    make_shop(root, definitions=second)
    (root / 'mart' / 'order_amounts.sql').write_text(ORDER_AMOUNTS)
    result = check_sql(Project(root).build(run_dbt=False).graph, 'select sum(amount) from mart.order_amounts')
    assert sorted(f.term or '' for f in result.findings) == ['Average net amount', 'Net amount']
    assert flat(result.corrected or '').count("status = 'completed'") == 1


@pytest.mark.parametrize(
    'condition',
    [
        "o.status <> 'returned'",
        "'returned' != o.status",
        "not o.status = 'returned'",
        "not (o.status in ('returned'))",
        "o.status not in ('returned')",
    ],
)
def test_a_required_filter_counts_however_it_is_written(shop: Checker, condition: str) -> None:
    sql = NET_REVENUE.replace('group by', f'where {condition}\ngroup by')
    assert rules(shop.check(sql)) == ['recomputes-measure']


@pytest.mark.parametrize(
    'condition',
    [
        "o.order_date >= '2020-01-01'",
        "o.order_date >= date '2020-01-01'",
        "'2020-01-01' <= o.order_date",
        "o.order_date >= cast('2020-01-01' as date)",
        "o.order_date >= '2020-01-01'::date",
        "o.order_date >= timestamp '2020-01-01 00:00:00'",
        "o.order_date between '2020-01-01' and '2026-12-31'",
        "o.order_date > '2019-12-31'",
    ],
)
def test_a_date_filter_counts_however_it_is_written(bridge: Checker, condition: str) -> None:
    sql = (
        'select sum(o.amount) as completed_revenue from raw.orders o '
        f"where o.status in ('completed') and {condition}"
    )
    assert loud(bridge.check(sql)) == []


def test_a_different_filter_on_the_same_column_is_a_warning_without_a_rewrite(shop: Checker) -> None:
    result = shop.check(NET_REVENUE.replace('group by', "where o.status = 'completed'\ngroup by"))
    finding = only(result, 'missing-filter')
    assert finding.severity == 'warning' and finding.rewrite is None
    assert "it filters the same column with orders.status = 'completed'" in finding.message
    assert result.passed and result.failed(strict=True) and result.corrected is None


def test_reading_the_approved_column_needs_no_filter_it_already_applies(shop: Checker) -> None:
    result = shop.check('select region, sum(net_revenue) as revenue from mart.customer_revenue group by 1')
    assert result.findings == [] and result.terms == ['Net revenue']
    assert result.summary() == 'No findings in 1 statement.'


SIBLING_REVENUE = """\
create table mart.customer_revenue as
with net as (
    select customer_id, sum(amount) as net from raw.orders where status <> 'returned' group by customer_id
),
gross as (select customer_id, sum(amount) as gross from raw.orders group by customer_id)
select c.customer_id, c.region, net.net as kept, gross.gross as net_revenue, gross.gross as gross_revenue
from raw.customers c
join net on net.customer_id = c.customer_id
join gross on gross.customer_id = c.customer_id
"""


def sibling_shop(root: Path) -> Graph:
    """The shop whose net revenue is computed in a CTE beside the one that leaves returns out."""
    make_shop(root)
    (root / 'mart' / 'customer_revenue.sql').write_text(SIBLING_REVENUE)
    return Project(root).build(run_dbt=False).graph


def test_a_filter_applies_on_the_route_only_where_the_column_is_computed(
    shop: Checker, tmp_path: Path
) -> None:
    net = shop.graph.resolve('customer_revenue.net_revenue')[0]
    assert shop.applied_on_route('Net revenue', net) == {
        "orders.status <> 'returned'": 'customer_revenue (mart/customer_revenue.sql)'
    }
    # A sibling CTE leaves returns out; the CTE the column is computed in does not.
    sibling = Checker(sibling_shop(tmp_path))
    column = sibling.graph.resolve('customer_revenue.net_revenue')[0]
    assert sibling.applied_on_route('Net revenue', column) == {}
    kept = sibling.graph.resolve('customer_revenue.kept')[0]
    assert sibling.applied_on_route('Net revenue', kept) != {}


NET_PART = """\
create table mart.net_part as
select customer_id, sum(amount) as net from raw.orders where status <> 'returned' group by customer_id
"""
ADJUSTMENT = """\
create table mart.adjustment as
select customer_id, sum(amount) as extra from raw.orders where status = 'returned' group by customer_id
"""
SUMMED_REVENUE = """\
create table mart.customer_revenue as
select c.customer_id, c.region, coalesce(n.net, 0) + coalesce(a.extra, 0) as net_revenue,
       0 as gross_revenue
from raw.customers c
left join mart.net_part n on n.customer_id = c.customer_id
left join mart.adjustment a on a.customer_id = c.customer_id
"""
WINDOWED_REVENUE = """\
create table mart.customer_revenue as
with w as (
    select customer_id, status, sum(amount) over (partition by customer_id) as total from raw.orders
)
select distinct c.customer_id, c.region, w.total as net_revenue, w.total as gross_revenue
from raw.customers c
join w on w.customer_id = c.customer_id
where w.status <> 'returned'
"""

SELF_JOINED_REVENUE = """\
create table mart.customer_revenue as
select c.customer_id, c.region, sum(o.amount) as net_revenue, sum(o.amount) as gross_revenue
from raw.customers c
join raw.orders o on o.customer_id = c.customer_id
left join raw.orders o2 on o2.order_id = o.order_id and o2.status <> 'returned'
group by 1, 2
"""
TWICE_READ_REVENUE = """\
create table mart.customer_revenue as
with f as (select * from raw.orders)
select c.customer_id, c.region, sum(f2.amount) as net_revenue, sum(f2.amount) as gross_revenue
from raw.customers c
left join {f2} f2 on f2.customer_id = c.customer_id
left join f f1 on f1.order_id = f2.order_id and f1.status <> 'returned'
group by 1, 2
"""
ORDER_WINDOW = """\
create {kind} mart.order_window as
select order_id, customer_id, status, sum(amount) over (partition by customer_id) as total from raw.orders
"""
WINDOW_READ_REVENUE = """\
create table mart.customer_revenue as
select distinct c.customer_id, c.region, w.total as net_revenue, w.total as gross_revenue
from raw.customers c
join mart.order_window w on w.customer_id = c.customer_id
where w.status <> 'returned'
"""
PAIRED = (
    'select a.order_id, a.customer_id, a.amount, b.status as pst from raw.orders a '
    'left join raw.orders b on b.order_id = a.order_id - 1'
)
"""Each order with the status of the one before it: two readings of raw.orders."""
PAIRED_REVENUE = """\
create table mart.customer_revenue as
{ctes}select c.customer_id, c.region, sum(sj.amount) as net_revenue, sum(sj.amount) as gross_revenue
from raw.customers c
{join} {source} sj on sj.customer_id = c.customer_id {filter}
group by 1, 2
"""
LAST_REVENUE = """\
create table mart.customer_revenue as
with last as (select customer_id, max(order_id) as last_id from raw.orders group by 1),
x as (
    select o.order_id, o.customer_id, o.amount, l2.status as last_status from raw.orders o
    join last l on l.customer_id = o.customer_id
    join raw.orders l2 on l2.order_id = l.last_id
)
select c.customer_id, c.region, sum(x.amount) as net_revenue, sum(x.amount) as gross_revenue
from raw.customers c
join x on x.customer_id = c.customer_id
where x.last_status <> 'returned'
group by 1, 2
"""
MISSED_SHAPES = {
    'a sibling CTE': {'customer_revenue.sql': SIBLING_REVENUE},
    'a sum with an unfiltered model': {
        'net_part.sql': NET_PART,
        'adjustment.sql': ADJUSTMENT,
        'customer_revenue.sql': SUMMED_REVENUE,
    },
    'a filter above a window': {'customer_revenue.sql': WINDOWED_REVENUE},
    'a filter on another alias of the table': {'customer_revenue.sql': SELF_JOINED_REVENUE},
    'a filter on another reading of a CTE': {'customer_revenue.sql': TWICE_READ_REVENUE.format(f2='f')},
    'a filter on a reading of a CTE beside a subquery over it': {
        'customer_revenue.sql': TWICE_READ_REVENUE.format(f2='(select * from f)')
    },
    'a filter on a column of a CTE copied from another reading': {
        'customer_revenue.sql': PAIRED_REVENUE.format(
            ctes=f'with sj as ({PAIRED})\n', join='left join', source='sj', filter="and sj.pst <> 'returned'"
        )
    },
    'a filter in WHERE on a column of a CTE copied from another reading': {
        'customer_revenue.sql': PAIRED_REVENUE.format(
            ctes=f'with sj as ({PAIRED})\n', join='join', source='sj', filter="where sj.pst <> 'returned'"
        )
    },
    'a filter on a column of a subquery copied from another reading': {
        'customer_revenue.sql': PAIRED_REVENUE.format(
            ctes='', join='left join', source=f'({PAIRED})', filter="and sj.pst <> 'returned'"
        )
    },
    'a filter on a column of an upstream model copied from another reading': {
        'sj.sql': f'create table mart.sj as {PAIRED}',
        'customer_revenue.sql': PAIRED_REVENUE.format(
            ctes='', join='join', source='mart.sj', filter="where sj.pst <> 'returned'"
        ),
    },
    "a filter on the last order's status": {'customer_revenue.sql': LAST_REVENUE},
    "a filter on an upstream table's window": {
        'order_window.sql': ORDER_WINDOW.format(kind='table'),
        'customer_revenue.sql': WINDOW_READ_REVENUE,
    },
    "a filter on an upstream view's window": {
        'order_window.sql': ORDER_WINDOW.format(kind='view'),
        'customer_revenue.sql': WINDOW_READ_REVENUE,
    },
}
"""Models of net revenue whose filter does not hold on every row the revenue adds up."""


def revenue_shop(root: Path, models: dict[str, str]) -> Graph:
    """The shop with its net revenue computed by ``models`` (file name to SQL) instead."""
    make_shop(root)
    for name, sql in models.items():
        (root / 'mart' / name).write_text(sql)
    return Project(root).build(run_dbt=False).graph


@pytest.mark.parametrize('models', list(MISSED_SHAPES.values()), ids=list(MISSED_SHAPES))
def test_a_filter_that_does_not_hold_on_the_measures_rows_is_missing(
    tmp_path: Path, models: dict[str, str]
) -> None:
    """customer_revenue filters returns somewhere, but not on every row its net revenue adds up:
    the filter is not applied there, and a query reading the column leaves it out."""
    checker = Checker(revenue_shop(tmp_path, models))
    column = checker.graph.resolve('customer_revenue.net_revenue')[0]
    assert checker.applied_on_route('Net revenue', column) == {}
    result = checker.check(
        'select region, sum(net_revenue) as net_revenue from mart.customer_revenue group by 1'
    )
    assert 'missing-filter' in {f.rule for f in result.findings}


TWO_READINGS_REVENUE = """\
create table mart.customer_revenue as
select c.customer_id, c.region, sum(o.amount) as net_revenue, sum(o.amount) as gross_revenue
from raw.customers c
join raw.orders o on o.customer_id = c.customer_id
left join raw.orders o2 on o2.order_id = o.order_id - 1
where o.status <> 'returned'
group by 1, 2
"""


def test_a_filter_on_the_values_own_reading_is_applied(tmp_path: Path) -> None:
    """orders is read twice, so the model's conditions name each reading (``"orders#1"``); the
    term's filter on orders.status matches the one on the reading the revenue is summed from."""
    checker = Checker(revenue_shop(tmp_path, {'customer_revenue.sql': TWO_READINGS_REVENUE}))
    relation = checker.graph.resolve('customer_revenue')[0]
    assert any('"orders#' in str(c) for c in relation.attrs['conditions'])
    column = checker.graph.resolve('customer_revenue.net_revenue')[0]
    assert set(checker.applied_on_route('Net revenue', column)) == {"orders.status <> 'returned'"}


def test_a_filter_on_a_reading_the_graph_cannot_resolve_is_not_applied(tmp_path: Path) -> None:
    """A condition on the side of an outer join that names a relation the graph does not have
    could be on any rows: it is not taken as applied."""
    graph = revenue_shop(tmp_path, {'customer_revenue.sql': TWO_READINGS_REVENUE})
    relation = graph.resolve('customer_revenue')[0]
    ghost = 'LEFT JOIN WHERE: raw."ghost#2".status <> \'returned\''
    relation.attrs['conditions'] = [ghost]
    for column in graph.columns_of(relation.id):
        column.attrs['row_conditions'] = [0]
    column = graph.resolve('customer_revenue.net_revenue')[0]
    assert Checker(graph).applied_on_route('Net revenue', column) == {}


def test_without_row_conditions_no_filter_counts_as_applied(tmp_path: Path) -> None:
    """A graph an earlier Konstruct built says which conditions a relation has but not which
    hold on each column: none is taken as applied, so the required filter is reported, as what
    the graph cannot show rather than as a defect of the SQL."""
    graph = make_shop(tmp_path)
    for node in graph.nodes.values():
        node.attrs.pop('row_conditions', None)
    result = Checker(graph).check(
        'select region, sum(net_revenue) as revenue from mart.customer_revenue group by 1'
    )
    finding = only(result, 'missing-filter')
    assert finding.severity == 'warning'
    assert finding.message == (
        "'Net revenue' requires orders.status <> 'returned', and the graph cannot show whether "
        'customer_revenue applies it: it was built by an earlier Konstruct, which did not record '
        "which filters hold on each column's rows"
    )
    assert finding.fix.startswith('Run konstruct build again, then check the query again.')
    # Rebuilt, the graph shows the model applies it.
    assert (
        Checker(Project(tmp_path).build(run_dbt=False).graph)
        .check('select region, sum(net_revenue) as revenue from mart.customer_revenue group by 1')
        .findings
        == []
    )
    # A query of a relation SQL does not write is told plainly that it leaves the filter out.
    raw = only(Checker(graph).check('select sum(amount) as net_revenue from raw.orders'), 'missing-filter')
    assert raw.message == "'Net revenue' requires orders.status <> 'returned', which the query does not apply"


def test_a_filter_on_a_relation_the_query_does_not_read_is_advice_only(shop: Checker) -> None:
    result = shop.check('select sum(paid) as net_revenue from mart.order_payments')
    finding = only(result, 'missing-filter')
    assert finding.rewrite is None and result.corrected is None and finding.severity == 'warning'
    assert finding.fix == "Add orders.status <> 'returned' to the query."


def test_the_filter_lands_in_the_subquery_that_reads_the_measure(shop: Checker, shop_db: Path) -> None:
    sql = 'with o as (select customer_id, amount from raw.orders) select sum(amount) as net_revenue from o'
    result = shop.check(sql)
    assert only(result, 'missing-filter').rewrite == "orders.status <> 'returned' added to the WHERE clause"
    assert result.corrected is not None
    assert rules(shop.check(result.corrected)) == ['recomputes-measure']
    runs_alike(shop_db, sql, result)
    # Grouped, the filter cannot go into the CTE, which leaves out the column it needs.
    grouped = (
        'with o as (select customer_id, amount from raw.orders) '
        'select customer_id, sum(amount) as net_revenue from o group by 1'
    )
    assert only(shop.check(grouped), 'missing-filter').rewrite is None


def test_a_filter_goes_into_the_scope_whose_aggregate_produces_the_measure(shop: Checker) -> None:
    sql = (
        'with all_o as (select count(*) as n from raw.orders), '
        'rev as (select sum(amount) as net_revenue from raw.orders) '
        'select all_o.n, rev.net_revenue from all_o cross join rev'
    )
    result = shop.check(sql)
    assert only(result, 'missing-filter').rewrite == "orders.status <> 'returned' added to the WHERE clause"
    assert result.corrected is not None
    before, after = result.corrected.split('rev AS')
    assert "WHERE\n    orders.status <> 'returned'" in after and 'WHERE' not in before


def test_a_filter_that_would_change_another_column_is_written_into_the_measure(shop: Checker) -> None:
    sql = 'select sum(o.amount) as net_revenue, count(*) as orders_placed from raw.orders o'
    result = shop.check(sql)
    finding = only(result, 'missing-filter')
    assert finding.rewrite == "o.status <> 'returned' applied to SUM(o.amount) alone"
    assert result.corrected is not None
    flat = ' '.join(result.corrected.split())
    assert flat == (
        "SELECT SUM(o.amount) FILTER(WHERE o.status <> 'returned') AS net_revenue, "
        'COUNT(*) AS orders_placed FROM raw.orders AS o'
    )
    assert loud(shop.check(result.corrected)) == []


def test_a_left_join_filter_that_would_change_another_count_is_written_into_the_measure(
    shop: Checker,
) -> None:
    sql = (
        'select c.region, sum(o.amount) as net_revenue, count(o.order_id) as orders_placed '
        'from raw.customers c left join raw.orders o on o.customer_id = c.customer_id group by 1'
    )
    result = shop.check(sql)
    assert only(result, 'missing-filter').rewrite == "o.status <> 'returned' applied to SUM(o.amount) alone"
    assert result.corrected is not None
    assert 'ON o.customer_id = c.customer_id\n' in result.corrected
    assert 'COUNT(o.order_id) AS orders_placed' in result.corrected


def test_a_filter_on_a_left_join_with_the_measure_alone_goes_into_its_on_clause(
    shop: Checker, shop_db: Path
) -> None:
    sql = (
        'select sum(o.amount) as net_revenue from raw.customers c '
        'left join raw.orders o on o.customer_id = c.customer_id'
    )
    result = shop.check(sql)
    assert (
        only(result, 'missing-filter').rewrite
        == "o.status <> 'returned' added to the ON clause of the join to o"
    )
    runs_alike(shop_db, sql, result)


def test_a_filter_on_rows_another_query_reads_too_is_advice_only(shop: Checker) -> None:
    sql = (
        'with o as (select customer_id, amount from raw.orders), '
        'a as (select sum(amount) as net_revenue from o), b as (select count(*) as n from o) '
        'select a.net_revenue, b.n from a cross join b'
    )
    result = shop.check(sql)
    finding = only(result, 'missing-filter')
    assert finding.rewrite is None and result.corrected is None


def test_a_filter_is_not_added_to_the_where_clause_of_a_right_or_full_join(
    shop: Checker, shop_db: Path
) -> None:
    sql = (
        'select sum(o.amount) as net_revenue, count(*) as n from raw.orders o '
        'full join raw.customers c on c.customer_id = o.customer_id'
    )
    result = shop.check(sql)
    assert only(result, 'missing-filter').rewrite == "o.status <> 'returned' applied to SUM(o.amount) alone"
    runs_alike(shop_db, sql, result)


@pytest.mark.parametrize(
    'tie',
    [
        "where o.order_id in (select order_id from raw.orders where status <> 'returned')",
        "where exists (select 1 from raw.orders k where k.order_id = o.order_id and k.status <> 'returned')",
        "join (select order_id from raw.orders where status <> 'returned') k on k.order_id = o.order_id",
        "join raw.orders k on k.order_id = o.order_id where k.status <> 'returned'",
    ],
)
def test_a_filter_on_the_same_rows_through_their_key_counts(shop: Checker, tie: str) -> None:
    sql = f'select sum(o.amount) as net_revenue from raw.orders o {tie}'
    assert loud(shop.check(sql)) == []


def test_a_filter_through_a_cte_joined_on_the_key_counts(shop: Checker) -> None:
    sql = (
        "with kept as (select order_id from raw.orders where status <> 'returned') "
        'select sum(o.amount) as net_revenue from raw.orders o join kept using (order_id)'
    )
    assert loud(shop.check(sql)) == []


def test_a_filter_on_other_rows_of_the_same_relation_does_not_count(shop: Checker) -> None:
    exists = (
        'select sum(o.amount) as net_revenue from raw.orders o '
        "where exists (select 1 from raw.orders o2 where o2.status <> 'returned')"
    )
    assert only(shop.check(exists), 'missing-filter').severity == 'error'
    # Tied to the measure's rows in a way the check does not follow: a note, not an error.
    for tie in (
        "where o.order_id not in (select order_id from raw.orders where status = 'returned')",
        "join raw.orders o2 on o2.customer_id = o.customer_id where o2.status <> 'returned'",
    ):
        sql = f'select sum(o.amount) as net_revenue from raw.orders o {tie}'
        assert only(shop.check(sql), 'missing-filter').severity == 'info'
    # The same rows, filtered to the opposite.
    contradiction = (
        'select sum(a.amount) as net_revenue from raw.orders a join raw.orders b on b.order_id = a.order_id '
        "where b.status <> 'returned' and a.status = 'returned'"
    )
    assert only(shop.check(contradiction), 'missing-filter').severity == 'warning'


def test_a_filter_on_a_joined_relation_is_applied(bridge: Checker, bridge_db: Path) -> None:
    sql = (
        'select o.order_date, sum(i.quantity * i.price) as item_revenue from raw.orders o '
        'join raw.order_items i on i.order_id = o.order_id group by 1'
    )
    result = bridge.check(sql)
    assert only(result, 'missing-filter').rewrite == (
        "o.status <> 'cancelled' applied to SUM(i.quantity * i.price) alone"
    )
    assert result.corrected is not None and loud(bridge.check(result.corrected)) == []
    runs_alike(bridge_db, sql, result)


CORRECTIONS = [
    ('select sum(amount) as net_revenue from raw.orders', None),
    ('select customer_id, sum(amount) as net_revenue from raw.orders group by customer_id', None),
    (
        'select c.customer_id, sum(o.amount) as net_revenue from raw.customers c '
        'join raw.orders o on o.customer_id = c.customer_id group by 1',
        None,
    ),
    (
        'select c.customer_id, c.name, sum(o.amount) as net_revenue from raw.customers c '
        'left join raw.orders o on o.customer_id = c.customer_id group by 1, 2',
        None,
    ),
    (
        'with base as (select o.order_id, o.customer_id, o.amount, o.status from raw.orders o) '
        'select customer_id, sum(amount) as net_revenue, count(*) as n from base group by 1',
        None,
    ),
    (
        'select (select count(*) from raw.orders) as orders, (select sum(amount) from raw.orders) as net_revenue',
        None,
    ),
    (
        'select c.region, sum(o.amount) as net_revenue, count(*) as orders_placed from raw.orders o '
        'join raw.customers c on c.customer_id = o.customer_id group by 1',
        'snowflake',
    ),
    (
        'select c.region, sum(o.amount) as net_revenue, count(*) as orders_placed from raw.orders o '
        'join raw.customers c on c.customer_id = o.customer_id group by 1',
        'bigquery',
    ),
    ('select customer_id, sum(amount) as net_revenue from raw.orders group by customer_id', 'snowflake'),
    ('select sum(amount) as net_revenue from raw.orders', 'bigquery'),
    (
        'select region, sum(gross_revenue) as gross_revenue from mart.customer_revenue group by 1 '
        'order by gross_revenue desc',
        None,
    ),
    (
        'select customer_id, gross_revenue from mart.customer_revenue where gross_revenue > 100 '
        'order by gross_revenue',
        None,
    ),
    ('select region, sum(gross_revenue) as total from mart.customer_revenue group by 1', 'snowflake'),
    (
        'with all_o as (select count(*) as n from raw.orders), '
        'rev as (select sum(amount) as net_revenue from raw.orders) '
        'select all_o.n, rev.net_revenue from all_o cross join rev',
        None,
    ),
    ('select sum(o.amount) as net_revenue, count(*) as orders_placed from raw.orders o', None),
    (
        'select c.region, sum(o.amount) as net_revenue, count(o.order_id) as orders_placed '
        'from raw.customers c left join raw.orders o on o.customer_id = c.customer_id group by 1',
        None,
    ),
    ('select region, sum(gross_revenue) as total from mart.customer_revenue group by 1', None),
    ('select r.region, r.gross_revenue from mart.customer_revenue r', None),
]


@pytest.mark.parametrize(('sql', 'dialect'), CORRECTIONS)
def test_every_correction_runs_and_changes_only_what_it_fixes(
    shop: Checker, dialects: dict[str, Checker], shop_db: Path, sql: str, dialect: str | None
) -> None:
    checker = dialects[dialect] if dialect else shop
    result = checker.check(sql)
    runs_alike(shop_db, sql, result, dialect)
    # Snowflake and BigQuery never enforce the primary key that alone keys customers, so there a
    # join onto it could repeat rows, before the correction and after.
    unkeyed = dialect is not None and 'join raw.customers' in sql
    assert loud(checker.check(result.corrected or '')) == (['fan-out'] if unkeyed else [])
    assert ('fan-out' in {f.rule for f in result.findings}) is unkeyed


@pytest.mark.parametrize(
    'sql',
    [
        'select region, sum(o.amount) as net_revenue, rank() over (order by sum(o.amount) desc) as rnk '
        'from raw.orders o join raw.customers c on c.customer_id = o.customer_id group by region',
        'select customer_id, sum(amount) as net_revenue from raw.orders group by 1 '
        'qualify rank() over (order by net_revenue desc) <= 3',
        'select customer_id, sum(amount) as net_revenue from raw.orders group by 1 order by 2 desc limit 3',
        'with t as (select customer_id, sum(amount) as net_revenue from raw.orders group by 1) '
        'select * from t order by net_revenue desc limit 3',
    ],
)
def test_no_correction_where_the_measure_chooses_or_ranks_the_rows(shop: Checker, sql: str) -> None:
    result = shop.check(sql)
    assert only(result, 'missing-filter').rewrite is None and result.corrected is None


@pytest.mark.parametrize(
    'sql',
    [
        # The difference would read the replacement twice.
        'select r.customer_id, r.gross_revenue - r.net_revenue as diff from mart.customer_revenue r',
        'select sum(r.gross_revenue) as total, max(r.net_revenue) as top from mart.customer_revenue r',
    ],
)
def test_a_retired_column_is_not_swapped_for_one_the_select_already_reads(shop: Checker, sql: str) -> None:
    result = shop.check(sql)
    assert only(result, 'retired-column').rewrite is None and result.corrected is None


def test_a_retired_column_passed_on_by_select_star_is_a_note(shop: Checker) -> None:
    finding = only(shop.check('select * from mart.customer_revenue'), 'retired-column')
    assert finding.severity == 'info' and finding.rewrite is None


# Other dialects


@pytest.mark.parametrize(
    ('dialect', 'sql'),
    [
        ('snowflake', "select sum(amount) as net_revenue from raw.orders where status <> 'returned'"),
        (
            'snowflake',
            "select customer_id, sum(iff(status <> 'returned', amount, 0)) as net_revenue from raw.orders group by 1",
        ),
        (
            'snowflake',
            'select region, sum(net_revenue) as net_revenue from mart.customer_revenue group by all',
        ),
        (
            'snowflake',
            "select to_char(order_date, 'YYYY-MM') as month, sum(amount) as net_revenue from raw.orders "
            "where status != 'returned' group by 1",
        ),
        (
            'snowflake',
            'select sum(amount) as net_revenue from raw.orders '
            "where status <> 'returned' and order_date >= '2026-01-01'::date",
        ),
        (
            'bigquery',
            "select customer_id, sum(if(status <> 'returned', amount, 0)) as net_revenue from raw.orders "
            'group by customer_id',
        ),
        (
            'bigquery',
            "select format_date('%Y-%m', order_date) as month, sum(amount) as net_revenue from raw.orders "
            "where status != 'returned' group by month",
        ),
        (
            'bigquery',
            'select region, sum(net_revenue) as net_revenue from `mart.customer_revenue` group by region',
        ),
    ],
)
def test_correct_sql_in_snowflake_and_bigquery_passes(
    dialects: dict[str, Checker], dialect: str, sql: str
) -> None:
    assert loud(dialects[dialect].check(sql)) == []


@pytest.mark.parametrize('dialect', ['snowflake', 'bigquery'])
def test_a_missing_filter_in_snowflake_and_bigquery_is_found_and_added_once(
    dialects: dict[str, Checker], dialect: str
) -> None:
    result = dialects[dialect].check(
        "select sum(amount) as net_revenue from raw.orders where order_date >= '2026-01-01'"
    )
    assert [f.rule for f in result.findings if f.severity == 'error'] == ['missing-filter']
    assert result.corrected is not None and result.corrected.count('returned') == 1


@pytest.mark.parametrize(
    ('dialect', 'condition'),
    [
        ('bigquery', "o.status not in unnest(['returned'])"),
        (None, "o.status not like 'returned'"),
        (None, "o.status not in (values ('returned'))"),
        (None, "not o.status = any(['returned'])"),
    ],
)
def test_a_required_filter_written_as_a_list_or_pattern_counts(
    shop: Checker, dialects: dict[str, Checker], dialect: str | None, condition: str
) -> None:
    checker = dialects[dialect] if dialect else shop
    assert (
        loud(checker.check(f'select sum(o.amount) as net_revenue from raw.orders o where {condition}')) == []
    )


def test_an_extra_exclusion_is_an_error_only_on_a_column_the_definition_filters(shop: Checker) -> None:
    sql = "select sum(o.amount) as net_revenue from raw.orders o where o.status <> 'returned' and {}"
    other = only(shop.check(sql.format('o.customer_id <> 7')), 'formula-differs')
    assert other.severity == 'warning'
    same = only(shop.check(sql.format("o.status <> 'cancelled'")), 'formula-differs')
    assert same.severity == 'error'
    # A filter in place of the required one is the missing filter's finding, not another formula.
    instead = shop.check("select sum(amount) as net_revenue from raw.orders where status not like 'return%'")
    assert rules(instead) == ['missing-filter']


def test_a_window_aggregate_is_a_measure(shop: Checker) -> None:
    window = 'select customer_id, sum(amount) over (partition by customer_id) as net_revenue from raw.orders'
    finding = only(shop.check(window), 'missing-filter')
    assert finding.severity == 'error' and finding.rewrite is None
    fanned = (
        'select o.order_id, sum(o.amount) over (partition by o.customer_id) as total from raw.orders o '
        'join raw.payments p on p.order_id = o.order_id'
    )
    assert only(shop.check(fanned), 'fan-out').column == 'total'
    # Partitioned by the key of the side that repeats, each partition holds one order row.
    by_payment = fanned.replace('partition by o.customer_id', 'partition by p.payment_id')
    assert 'fan-out' not in rules(shop.check(by_payment))


@pytest.mark.parametrize(
    ('dialect', 'sql'),
    [
        (
            None,
            'select o.customer_id, sum(o.amount) as total from raw.orders o cross join unnest([1, 2]) as t(x) group by 1',
        ),
        (
            'bigquery',
            'select o.customer_id, sum(o.amount) as total from raw.orders o cross join unnest([1, 2]) as x group by 1',
        ),
        (
            'snowflake',
            'select o.customer_id, sum(o.amount) as total from raw.orders o, lateral flatten(input => array_construct(1, 2)) f group by 1',
        ),
    ],
)
def test_an_unnested_array_repeats_the_rows_it_is_joined_to(
    shop: Checker, dialects: dict[str, Checker], dialect: str | None, sql: str
) -> None:
    checker = dialects[dialect] if dialect else shop
    assert only(checker.check(sql), 'fan-out').severity == 'warning'


@pytest.mark.parametrize('name', ['orders_placed', 'order_count', 'orders'])
def test_a_count_named_for_a_repeated_source_is_a_fan_out(shop: Checker, name: str) -> None:
    sql = (
        f'select c.region, count(*) as {name} from raw.customers c join raw.orders o on o.customer_id = c.customer_id '
        'join raw.payments p on p.order_id = o.order_id group by 1'
    )
    assert only(shop.check(sql), 'fan-out').message.startswith('COUNT(*) counts each orders row')
    # Named for the rows it does count, it is right.
    assert 'fan-out' not in rules(shop.check(sql.replace(f'as {name}', 'as payment_count')))


def test_an_unknown_column_named_through_an_alias_is_found(shop: Checker) -> None:
    result = shop.check('select sum(o.amount_total) as net_revenue from raw.orders o')
    assert only(result, 'unknown-column').message == 'orders has no column amount_total; did you mean amount?'


REFERENCES = [
    # A query, and one written by hand that answers it correctly: the correction must agree with it.
    (
        'select sum(amount) as net_revenue from raw.orders',
        "select sum(amount) as net_revenue from raw.orders where status <> 'returned'",
        None,
    ),
    (
        'select customer_id, sum(amount) as net_revenue from raw.orders group by customer_id',
        "select customer_id, sum(case when status <> 'returned' then amount end) as net_revenue "
        'from raw.orders group by customer_id',
        None,
    ),
    (
        'select customer_id, sum(amount) as net_revenue from raw.orders group by 1 having count(*) >= 3',
        "select customer_id, sum(case when status <> 'returned' then amount end) as net_revenue "
        'from raw.orders group by 1 having count(*) >= 3',
        None,
    ),
    (
        'select c.customer_id, c.name, sum(o.amount) as net_revenue from raw.customers c '
        'left join raw.orders o on o.customer_id = c.customer_id group by 1, 2',
        "select c.customer_id, c.name, sum(case when o.status <> 'returned' then o.amount end) as net_revenue "
        'from raw.customers c left join raw.orders o on o.customer_id = c.customer_id group by 1, 2',
        None,
    ),
    (
        'select sum(o.amount) as net_revenue, count(*) as orders_placed from raw.orders o',
        "select sum(case when o.status <> 'returned' then o.amount end) as net_revenue, count(*) as orders_placed "
        'from raw.orders o',
        None,
    ),
    (
        'with all_o as (select count(*) as n from raw.orders), '
        'rev as (select sum(amount) as net_revenue from raw.orders) '
        'select all_o.n, rev.net_revenue from all_o cross join rev',
        "select (select count(*) from raw.orders) as n, (select sum(amount) from raw.orders where status <> 'returned') "
        'as net_revenue',
        None,
    ),
    (
        'select region, sum(gross_revenue) as total from mart.customer_revenue group by 1',
        'select region, sum(net_revenue) as total from mart.customer_revenue group by 1',
        None,
    ),
    (
        'select o.order_id, c.name from raw.orders o join raw.customers c on o.order_id = c.customer_id',
        'select o.order_id, c.name from raw.orders o join raw.customers c on o.customer_id = c.customer_id',
        None,
    ),
    (
        'select customer_id, sum(amount) as net_revenue from raw.orders group by customer_id',
        "select customer_id, sum(case when status <> 'returned' then amount end) as net_revenue "
        'from raw.orders group by customer_id',
        'snowflake',
    ),
    (
        'select c.region, sum(o.amount) as net_revenue, count(*) as orders_placed from raw.orders o '
        'join raw.customers c on c.customer_id = o.customer_id group by 1',
        "select c.region, sum(case when o.status <> 'returned' then o.amount end) as net_revenue, "
        'count(*) as orders_placed from raw.orders o join raw.customers c on c.customer_id = o.customer_id group by 1',
        'bigquery',
    ),
]


@pytest.mark.parametrize(('sql', 'reference', 'dialect'), REFERENCES)
def test_every_correction_gives_the_right_answer(
    shop: Checker, dialects: dict[str, Checker], shop_db: Path, sql: str, reference: str, dialect: str | None
) -> None:
    import sqlglot

    result = (dialects[dialect] if dialect else shop).check(sql)
    assert result.corrected is not None, str(result)
    corrected = (
        sqlglot.transpile(result.corrected, read=dialect, write='duckdb')[0] if dialect else result.corrected
    )
    con = duckdb.connect(str(shop_db), read_only=True)
    try:
        assert sorted(map(str, con.execute(corrected).fetchall())) == sorted(
            map(str, con.execute(reference).fetchall())
        )
    finally:
        con.close()


@pytest.mark.parametrize(
    'sql',
    [
        'select customer_id, sum(amount) as net_revenue from raw.orders group by 1 having sum(amount) > 140',
        'with t as (select customer_id, sum(amount) as net_revenue from raw.orders group by 1) '
        'select * from t where coalesce(net_revenue, 0) < 120',
        'with t as (select customer_id, sum(amount) as net_revenue from raw.orders group by 1) '
        'select c.name, t.net_revenue from raw.customers c join t on t.customer_id = c.customer_id '
        'and t.net_revenue > 100',
    ],
)
def test_no_correction_where_a_filter_reads_the_measure(shop: Checker, sql: str) -> None:
    result = shop.check(sql)
    assert only(result, 'missing-filter').rewrite is None and result.corrected is None


def test_an_asof_join_repeats_the_rows_of_the_side_it_joins(shop: Checker, shop_db: Path) -> None:
    join = (
        'from raw.payments p asof join raw.orders o on o.order_id = p.order_id and o.order_id <= p.order_id'
    )
    assert only(shop.check(f'select sum(o.amount) as total {join}'), 'fan-out').severity == 'warning'
    assert loud(shop.check(f'select sum(p.amount) as total {join}')) == []
    con = duckdb.connect(str(shop_db), read_only=True)
    try:
        con.execute(f'select sum(o.amount) as total {join}').fetchall()
    finally:
        con.close()


def test_a_statement_the_analysis_cannot_read_is_reported_as_not_checked(shop: Checker) -> None:
    sql = 'select p.customer_id, p.returned from (pivot raw.orders on status using sum(amount) group by customer_id) p'
    finding = only(shop.check(sql), 'unchecked')
    assert finding.severity == 'info' and 'not checked' in finding.message


@pytest.mark.parametrize(
    'condition',
    [
        "o.status not in (select 'returned')",
        "o.status <> (select 'returned')",
        "o.status <> all(select 'returned')",
        "o.status not in (values ('returned'))",
    ],
)
def test_a_filter_through_a_query_of_constants_counts(shop: Checker, condition: str) -> None:
    assert loud(shop.check(f'select sum(o.amount) as net_revenue from raw.orders o where {condition}')) == []


def test_a_filter_through_a_list_of_values_counts(shop: Checker) -> None:
    for values in (
        "excluded(status) as (values ('returned'))",
        "excluded as (select * from (values ('returned')) t(status))",
    ):
        sql = (
            f'with {values} select sum(o.amount) as net_revenue from raw.orders o '
            'where o.status not in (select status from excluded)'
        )
        assert rules(shop.check(sql)) == ['recomputes-measure']


def test_a_filter_through_a_query_of_another_relation_is_a_note(shop: Checker) -> None:
    sql = 'select sum(o.amount) as net_revenue from raw.orders o where o.status not in (select method from raw.payments)'
    assert only(shop.check(sql), 'missing-filter').severity == 'info'


def test_an_unchecked_statement_does_not_pass(shop: Checker) -> None:
    sql = 'select p.customer_id, p.returned from (pivot raw.orders on status using sum(amount) group by customer_id) p'
    result = shop.check(sql)
    assert not result.checked and not result.passed and result.count('error') == 0
    assert result.to_dict()['checked'] is False
    assert shop.check('select 1').checked


def test_grouping_by_a_value_of_the_element_is_not_a_fan_out(dialects: dict[str, Checker]) -> None:
    sql = (
        'select f.value::string as v, count(*) as n from raw.orders o, '
        'lateral flatten(input => array_construct(1, 2)) f group by 1'
    )
    assert loud(dialects['snowflake'].check(sql)) == []


def test_a_typed_date_on_a_text_column_is_not_a_plain_string(shop: Checker) -> None:
    def key(text: str) -> str:
        tree = shop.parse(text)
        assert tree is not None
        made = shop.condition(tree, shop.resolved)
        assert made is not None
        return made.key

    assert key("shop.raw.orders.status = DATE '2020-01-01'") != key("shop.raw.orders.status = '2020-01-01'")
    assert key("shop.raw.orders.order_date = DATE '2020-01-01'") == key(
        "shop.raw.orders.order_date = '2020-01-01'"
    )
    assert key("shop.raw.orders.status = '2020-01-01 00:00:00'") != key(
        "shop.raw.orders.status = '2020-01-01'"
    )


@pytest.mark.parametrize(
    ('dialect', 'sql'),
    [
        (None, 'select t.x, count(*) as n from raw.orders o cross join unnest([1, 2]) as t(x) group by 1'),
        ('bigquery', 'select x, count(*) as n from raw.orders o cross join unnest([1, 2]) as x group by x'),
        (
            'snowflake',
            'select f.value, count(*) as n from raw.orders o, lateral flatten(input => array_construct(1, 2)) f group by 1',
        ),
        (
            None,
            'select tag, count(*) as n from (select o.amount, unnest([1, 2]) as tag from raw.orders o) t group by 1',
        ),
    ],
)
def test_counting_by_the_unnested_element_is_not_a_fan_out(
    shop: Checker, dialects: dict[str, Checker], dialect: str | None, sql: str
) -> None:
    assert loud((dialects[dialect] if dialect else shop).check(sql)) == []


@pytest.mark.parametrize(
    ('dialect', 'sql'),
    [
        (
            'snowflake',
            'select o.customer_id, sum(o.amount) as total from raw.orders o, table(flatten(input => array_construct(1, 2))) f group by 1',
        ),
        (
            'snowflake',
            'select o.customer_id, sum(o.amount) as total from raw.orders o join table(flatten(input => array_construct(1, 2))) f on true group by 1',
        ),
        (
            None,
            'select sum(amount) as total from (select o.amount, unnest([1, 2]) as tag from raw.orders o) t',
        ),
    ],
)
def test_a_flattened_or_listed_array_repeats_rows(
    shop: Checker, dialects: dict[str, Checker], dialect: str | None, sql: str
) -> None:
    assert only((dialects[dialect] if dialect else shop).check(sql), 'fan-out').severity == 'warning'


def test_an_unknown_column_of_a_cte_is_found(shop: Checker) -> None:
    sql = 'with t as (select customer_id, amount from raw.orders) select sum(t.amount_total) as total from t'
    assert (
        only(shop.check(sql), 'unknown-column').message
        == 't has no column amount_total; did you mean amount?'
    )


@pytest.mark.parametrize(
    'condition',
    ['o.customer_id <> 7', 'c.customer_id <> 7', "o.order_date <> '2026-01-01'"],
)
def test_every_extra_exclusion_is_a_warning(shop: Checker, condition: str) -> None:
    sql = (
        'select sum(o.amount) as net_revenue from raw.orders o join raw.customers c on c.customer_id = o.customer_id '
        f"where o.status <> 'returned' and {condition}"
    )
    assert only(shop.check(sql), 'formula-differs').severity == 'warning'


@pytest.mark.parametrize(
    'condition',
    [
        "o.status <> 'returned' and o.amount <> 0",
        "o.status not like 'returned' escape '!'",
        "o.status <> all(['returned'])",
        "o.status <> 'returned' and o.order_date >= make_date(2026, 1, 1)",
    ],
)
def test_filters_that_change_nothing_or_are_written_another_way_pass(shop: Checker, condition: str) -> None:
    assert loud(shop.check(f'select sum(o.amount) as net_revenue from raw.orders o where {condition}')) == []


def test_a_filter_of_the_graph_counts_wherever_it_sits(shop: Checker) -> None:
    orders = 'table.shop.raw.orders'
    for label, limited in (
        ('WHERE', frozenset()),
        ('WHERE BEFORE WINDOW', frozenset()),
        ('WHERE BEFORE WINDOW, LIMIT', frozenset()),
        ('JOIN ON', frozenset()),
        # On the nullable side of an outer join, a filter restricts that side's rows only.
        ('LEFT JOIN WHERE', frozenset({orders})),
        ('LEFT JOIN ON', frozenset({orders})),
    ):
        [condition] = shop.conjuncts(f"{label}: shop.raw.orders.status <> 'returned'")
        assert (condition.label, condition.limited) == ('WHERE', limited), label
    for label in ('DISTINCT', 'LIMIT', 'GROUP BY', 'RIGHT JOIN WHERE'):
        assert shop.conjuncts(f'{label}: shop.raw.orders.status') == []
    # The equality of a join is the join, not a filter.
    assert shop.conjuncts('JOIN ON: shop.raw.orders.customer_id = shop.raw.customers.customer_id') == []


REGION_MODELS = {
    'region_counts': (
        'create table mart.region_counts as with returned as (select customer_id, count(*) as n from raw.orders '
        "where status = 'returned' group by 1) select c.region, count(distinct c.customer_id) as customers, "
        'sum(r.n) as returns from raw.customers c left join returned r on r.customer_id = c.customer_id '
        'group by c.region'
    ),
    'region_returns': (
        'create table mart.region_returns as with returned as (select customer_id, amount from raw.orders '
        "where status = 'returned') select c.region, sum(r.amount) as returned_amount from raw.customers c "
        'left join returned r on r.customer_id = c.customer_id group by c.region'
    ),
}


def test_a_model_s_filter_on_the_side_of_an_outer_join_holds_for_that_side_only(
    tmp_path: Path, shop_db: Path
) -> None:
    make_shop(tmp_path, definitions=None)
    for name, sql in REGION_MODELS.items():
        (tmp_path / 'mart' / f'{name}.sql').write_text(sql)
    checker = Checker(Project(tmp_path).build(run_dbt=False).graph)
    # The customers of a region are all of them, whatever their returns.
    counted = 'select c.region, count(distinct c.customer_id) as customers from raw.customers c group by 1'
    assert loud(checker.check(counted)) == []
    # The returned amount reads the joined orders, which the model filters.
    summed = (
        'select c.region, sum(r.amount) as returned_amount from raw.customers c '
        'left join raw.orders r on r.customer_id = c.customer_id group by 1'
    )
    result = checker.check(summed)
    assert only(result, 'missing-filter').severity == 'warning' and result.corrected is not None
    reference = (
        "select c.region, sum(case when r.status = 'returned' then r.amount end) as returned_amount "
        'from raw.customers c left join raw.orders r on r.customer_id = c.customer_id group by 1'
    )
    con = duckdb.connect(str(shop_db), read_only=True)
    try:
        fixed = con.execute(result.corrected).fetchall()
        assert sorted(map(str, fixed)) == sorted(map(str, con.execute(reference).fetchall()))
    finally:
        con.close()


# Names: which terms an output column stands for


@pytest.mark.parametrize(
    'sql',
    [
        'select count(o.order_id) as orders from raw.orders o',
        'select count(*) as total_orders from raw.orders',
        'select order_id as "order" from raw.orders',
        'select amount as net_revenue from raw.orders',
        'select sum(amount) as sales from raw.orders',
        'select sum(amount) as income from raw.payments',
        'select sum(amount) as amount from raw.payments',
    ],
)
def test_only_an_aggregate_named_exactly_for_a_term_is_compared_with_it(shop: Checker, sql: str) -> None:
    assert loud(shop.check(sql)) == []


def test_a_synonym_names_its_term(shop: Checker) -> None:
    finding = only(shop.check('select sum(amount) as revenue from raw.orders'), 'missing-filter')
    assert finding.term == 'Net revenue'


def test_a_match_by_name_alone_is_never_an_error(shop: Checker) -> None:
    sql = NET_REVENUE.replace('sum(o.amount)', 'sum(o.amount * 1.2)').replace(
        'group by', "where o.status <> 'returned'\ngroup by"
    )
    finding = only(shop.check(sql), 'formula-differs')
    assert finding.severity == 'warning' and finding.term == 'Net revenue'
    assert finding.detail == 'SUM(orders.amount * 1.2)'
    assert 'customer_revenue.net_revenue computes it as SUM(orders.amount)' in finding.message
    assert 'Read customer_revenue.net_revenue (approved' in finding.fix
    missing = only(
        shop.check('select sum(o.amount * 1.2) as net_revenue from raw.orders o'), 'missing-filter'
    )
    assert missing.severity == 'warning'


# Formulas


@pytest.mark.parametrize(
    'value',
    [
        'round(sum(amount), 2)',
        'cast(sum(amount) as double)',
        'coalesce(sum(amount), 0)',
        'sum(amount) * 1.0',
        'sum(cast(amount as decimal(12, 2)))',
    ],
)
def test_a_value_preserving_wrapper_is_the_same_formula(shop: Checker, value: str) -> None:
    sql = f"select {value} as net_revenue from raw.orders where status <> 'returned'"
    assert rules(shop.check(sql)) == ['recomputes-measure']


@pytest.mark.parametrize('dialect', ['bigquery', 'snowflake'])
def test_a_function_is_the_same_whatever_case_it_is_written_in(
    dialects: dict[str, Checker], dialect: str
) -> None:
    """BigQuery keeps a function name's case as written, where Snowflake upper-cases it; SUM and
    sum are one function either way."""
    sql = 'select o.order_id, SUM(p.amount) as paid from raw.orders o join raw.payments p on p.order_id = o.order_id group by 1'
    assert 'formula-differs' not in rules(dialects[dialect].check(sql))
    assert 'formula-differs' not in rules(dialects[dialect].check(sql.replace('SUM', 'sum')))


@pytest.mark.parametrize(
    'value',
    [
        "sum(case when status <> 'returned' then amount else 0 end)",
        "sum(case when status <> 'returned' then amount end)",
        "sum(amount) filter (where status <> 'returned')",
        "sum(if(status <> 'returned', amount, 0))",
    ],
)
def test_a_condition_inside_the_aggregate_is_its_filter(shop: Checker, value: str) -> None:
    assert rules(shop.check(f'select {value} as net_revenue from raw.orders')) == ['recomputes-measure']
    # A WHERE clause that repeats it changes nothing.
    sql = f"select {value} as net_revenue from raw.orders where status <> 'returned'"
    assert rules(shop.check(sql)) == ['recomputes-measure']


def test_each_branch_of_a_union_is_a_measure_of_its_own(shop: Checker) -> None:
    both = (
        "select 'north' as part, sum(o.amount) as net_revenue from raw.orders o join raw.customers c "
        "on c.customer_id = o.customer_id where c.region = 'North' and o.status <> 'returned' "
        "union all select 'south', sum(o.amount) from raw.orders o join raw.customers c "
        "on c.customer_id = o.customer_id where c.region = 'South' and o.status <> 'returned'"
    )
    assert loud(shop.check(both)) == []
    one = both.rsplit(" and o.status <> 'returned'", 1)[0]
    finding = only(shop.check(one), 'missing-filter')
    assert finding.column == 'net_revenue' and finding.severity == 'error'


def test_each_union_branch_without_the_filter_is_reported(shop: Checker) -> None:
    sql = (
        'select sum(amount) as net_revenue from raw.orders union all '
        "select sum(amount) from raw.orders where order_date >= '2026-06-01'"
    )
    assert [f.rule for f in shop.check(sql).findings] == ['missing-filter', 'missing-filter']


def test_a_scalar_subquery_is_a_measure_of_its_own(shop: Checker) -> None:
    sql = 'select (select sum(amount) from raw.orders) as net_revenue'
    finding = only(shop.check(sql), 'missing-filter')
    assert 'SELECT' not in finding.message and finding.column == 'net_revenue'
    filtered = "select (select sum(amount) from raw.orders where status <> 'returned') as net_revenue"
    assert loud(shop.check(filtered)) == []


def test_a_value_computed_from_a_query_is_not_compared(shop: Checker) -> None:
    sql = (
        'select sum(amount) / (select count(*) from raw.customers) as net_revenue from raw.orders '
        "where status <> 'returned'"
    )
    assert shop.check(sql).findings == []


def test_an_extra_filter_that_leaves_out_rows_is_another_formula(shop: Checker) -> None:
    sql = (
        'select o.order_id, sum(p.amount) as paid_amount from raw.orders o '
        "join raw.payments p on p.order_id = o.order_id where p.method <> 'coupon' group by 1"
    )
    finding = only(shop.check(sql), 'formula-differs')
    assert finding.detail == "with payments.method <> 'coupon'" and finding.severity == 'warning'
    assert 'but with payments.method' in finding.message


@pytest.mark.parametrize(
    'condition',
    [
        "p.method = 'card'",
        "o.order_date >= '2026-01-01'",
    ],
)
def test_a_filter_that_chooses_the_rows_of_a_question_is_not_another_formula(
    shop: Checker, condition: str
) -> None:
    sql = (
        'select o.order_id, sum(p.amount) as paid_amount from raw.orders o '
        f'join raw.payments p on p.order_id = o.order_id where {condition} group by 1'
    )
    assert shop.check(sql).findings == []


def test_a_measure_from_other_sources_names_them(shop: Checker) -> None:
    finding = only(shop.check('select sum(amount) as net_revenue from raw.payments'), 'formula-differs')
    assert finding.evidence == [
        'reads payments.amount, which customer_revenue.net_revenue does not',
        'leaves out orders.amount',
    ]


def test_an_average_against_a_sum_is_an_aggregation_difference(shop: Checker) -> None:
    sql = NET_REVENUE.replace('sum(o.amount)', 'avg(o.amount)').replace(
        'group by', "where o.status <> 'returned'\ngroup by"
    )
    finding = only(shop.check(sql), 'formula-differs')
    assert finding.evidence == ['aggregates with AVG against SUM']


def test_a_draft_measure_recomputed_the_same_way_is_a_note(shop: Checker) -> None:
    sql = (
        'select o.order_id, sum(p.amount) as paid_amount from raw.orders o '
        'join raw.payments p on p.order_id = o.order_id group by 1'
    )
    finding = only(shop.check(sql), 'recomputes-measure')
    assert finding.severity == 'info' and finding.term == 'Paid amount'
    assert finding.fix.startswith("Read order_payments.paid (draft 'Paid amount'")


def test_re_aggregating_a_sum_is_the_same_formula(shop: Checker) -> None:
    sql = (
        'with t as (select o.order_id, sum(p.amount) as paid from raw.orders o '
        'join raw.payments p on p.order_id = o.order_id group by 1) '
        'select sum(paid) as paid_amount from t'
    )
    assert rules(shop.check(sql)) == ['recomputes-measure']


# Retired columns


def test_a_retired_column_is_swapped_for_its_replacement_in_the_same_relation(shop: Checker) -> None:
    result = shop.check('select region, sum(gross_revenue) as total from mart.customer_revenue group by 1')
    finding = only(result, 'retired-column')
    assert finding.severity == 'warning' and finding.term == 'Gross revenue'
    assert finding.rewrite == 'gross_revenue read from net_revenue'
    assert finding.fix.startswith('Read customer_revenue.net_revenue (approved')
    assert result.corrected is not None and 'SUM(net_revenue) AS total' in result.corrected


def test_a_retired_column_in_the_result_keeps_its_name(shop: Checker) -> None:
    result = shop.check('select r.region, r.gross_revenue from mart.customer_revenue r')
    assert result.corrected is not None and 'r.net_revenue AS gross_revenue' in result.corrected


@pytest.mark.parametrize(
    'sql',
    [
        # The column passes out of the CTE under its name, which the query around it reads.
        'with r as (select region, gross_revenue from mart.customer_revenue) '
        'select region, sum(gross_revenue) as total from r group by 1',
        # Its replacement is already in the result.
        'select r.gross_revenue, r.net_revenue from mart.customer_revenue r',
    ],
)
def test_a_retired_column_is_not_swapped_where_that_would_break_the_query(shop: Checker, sql: str) -> None:
    result = shop.check(sql)
    assert only(result, 'retired-column').rewrite is None and result.corrected is None


# Joins


def test_a_sum_over_a_join_that_repeats_its_rows_is_a_fan_out(shop: Checker) -> None:
    sql = (
        'select c.region, sum(o.amount) as total from raw.orders o '
        'join raw.payments p on p.order_id = o.order_id '
        'join raw.customers c on c.customer_id = o.customer_id group by 1'
    )
    finding = only(shop.check(sql), 'fan-out')
    assert finding.severity == 'warning' and finding.column == 'total'
    assert finding.message == 'SUM(o.amount) adds orders.amount once per matching payments row'
    assert finding.evidence == [
        'payments.order_id refers to order (payments.order_id = orders.order_id), so many payments rows '
        'can match one orders row'
    ]
    assert finding.fix.startswith('Aggregate payments to one row per order_id in a subquery before the join')


@pytest.mark.parametrize(
    'sql',
    [
        # The many side's own column: each payment appears once.
        'select sum(p.amount) from raw.orders o join raw.payments p on p.order_id = o.order_id',
        # Pre-aggregated to the join key first.
        'with p as (select order_id, sum(amount) as paid from raw.payments group by 1) '
        'select sum(o.amount), sum(p.paid) from raw.orders o join p on p.order_id = o.order_id',
        # Measures that do not change with repeated rows.
        'select max(o.amount), count(distinct o.order_id), sum(distinct o.amount) '
        'from raw.orders o join raw.payments p on p.order_id = o.order_id',
        # Many-to-one joins keep the grain.
        'select c.region, sum(o.amount) from raw.orders o join raw.customers c on c.customer_id = o.customer_id '
        'group by 1',
        # A window over the many side's own column.
        'select sum(p.amount) over (partition by o.order_id) from raw.orders o '
        'join raw.payments p on p.order_id = o.order_id',
        # Grouped by the key of the side that repeats: one payment, so one order, per group.
        'select p.payment_id, sum(o.amount) from raw.payments p join raw.orders o on o.order_id = p.order_id '
        'group by p.payment_id',
        # Made distinct on the key of the side that repeats.
        'with x as (select distinct o.order_id, o.amount from raw.orders o '
        "join raw.payments p on p.order_id = o.order_id where p.method = 'card') select sum(amount) from x",
    ],
)
def test_joins_that_keep_the_grain_are_not_fan_outs(shop: Checker, sql: str) -> None:
    assert 'fan-out' not in rules(shop.check(sql))


def test_an_average_of_a_value_fixed_by_its_group_is_that_value(shop: Checker) -> None:
    """Grouped by the customer's key, every row of a group repeats one customer, so averaging the
    copies of its value gives the value: the join's repeats change nothing. A sum of the copies, or
    an average grouped by something else, still counts each repeat."""
    sql = (
        'select c.customer_id, avg(c.customer_id) as avg_id from raw.customers c '
        'join raw.orders o on o.customer_id = c.customer_id group by c.customer_id'
    )
    assert 'fan-out' not in rules(shop.check(sql))
    assert 'fan-out' in rules(shop.check(sql.replace('avg(', 'sum(')))
    assert 'fan-out' in rules(
        shop.check(
            sql.replace('group by c.customer_id', 'group by c.region').replace(
                'select c.customer_id,', 'select c.region,'
            )
        )
    )


ACCOUNTS = "select * from (values (1, null, 100), (2, null, 200), (3, 'k3', 300)) v(account_id, account_key, credit_limit)"
EVENTS = 'select * from (values (10, 1), (11, 1), (12, 1), (13, 2), (14, 3)) v(event_id, account_id)'


@pytest.fixture(scope='module')
def accounts(tmp_path_factory: pytest.TempPathFactory) -> Checker:
    """A dbt project whose accounts have an id tested unique and not null, and a key tested unique
    only, which two accounts leave NULL."""
    from konstruct.engine.build import build
    from konstruct.engine.config import load_config

    dbt = dbt_executable()
    if dbt is None:
        pytest.skip('dbt is not installed')
    root = tmp_path_factory.mktemp('accounts')
    (root / 'models').mkdir()
    (root / 'dbt_project.yml').write_text(
        "name: shop\nversion: '1.0'\nprofile: shop\nmodel-paths: [models]\n"
    )
    (root / 'profiles.yml').write_text(
        'shop:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n      path: shop.duckdb\n'
    )
    (root / 'models' / 'accounts.sql').write_text(ACCOUNTS.replace('null', 'cast(null as varchar)'))
    (root / 'models' / 'events.sql').write_text(EVENTS)
    (root / 'models' / 'schema.yml').write_text(
        'version: 2\nmodels:\n  - name: accounts\n    columns:\n      - name: account_id\n'
        '        tests: [unique, not_null]\n      - name: account_key\n        tests: [unique]\n'
        '  - name: events\n    columns:\n      - name: event_id\n        tests: [unique, not_null]\n'
    )
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('PATH', f'{Path(dbt).parent}{os.pathsep}{os.environ.get("PATH", "")}')
        patch.setenv('DBT_SEND_ANONYMOUS_USAGE_STATS', 'false')
        build(load_config(root, trusted=True))
    return Checker(Project(root).graph)


def test_an_average_grouped_by_a_key_that_can_be_null_is_a_fan_out(accounts: Checker) -> None:
    """A unique test lets many rows hold NULL, and grouped by the key they make one group, in which
    the join repeats each account as often as it has events: the average weighs them so. Grouped by
    an id also tested not null, each group is one account. DuckDB shows the averages of NULL's
    group differ (125 against 150)."""
    sql = (
        'select a.account_key, avg(a.credit_limit) as avg_limit from accounts a '
        'join events e on e.account_id = a.account_id group by a.account_key'
    )
    assert only(accounts.check(sql), 'fan-out').severity == 'warning'
    assert 'fan-out' not in rules(accounts.check(sql.replace('account_key', 'account_id')))
    con = duckdb.connect()
    con.execute(f'create table accounts as {ACCOUNTS}; create table events as {EVENTS}')
    assert (
        con.execute(sql + ' order by 1').fetchall()
        != con.execute('select account_key, avg(credit_limit) from accounts group by 1 order by 1').fetchall()
    )


@pytest.fixture(scope='module')
def two_projects(tmp_path_factory: pytest.TempPathFactory) -> tuple[Checker, str]:
    """A repository of two dbt projects, each building its models in its own DuckDB file: alpha's
    orders, with prices read from a CSV file, and beta's trips."""
    from konstruct.engine.build import build
    from konstruct.engine.config import load_config

    dbt = dbt_executable()
    if dbt is None:
        pytest.skip('dbt is not installed')
    root = tmp_path_factory.mktemp('two_projects')
    (root / 'prices.csv').write_text('sku,price,valid_from\na,10,2026-01-01\n')
    models = {
        'alpha': {
            'orders.sql': "select 1 as order_id, 'a' as sku, date '2026-02-01' as ordered_at",
            'order_prices.sql': (
                f"select o.order_id, p.price from read_csv('{root / 'prices.csv'}', columns = "
                "{'sku': 'VARCHAR', 'price': 'INT', 'valid_from': 'DATE'}) p "
                "join {{ ref('orders') }} o using (sku) where o.ordered_at >= p.valid_from"
            ),
        },
        'beta': {'trips.sql': 'select 1 as trip_id, 3 as km'},
    }
    for name, files in models.items():
        project = root / name
        (project / 'models').mkdir(parents=True)
        (project / 'dbt_project.yml').write_text(
            f"name: {name}\nversion: '1.0'\nprofile: {name}\nmodel-paths: [models]\n"
        )
        (project / 'profiles.yml').write_text(
            f'{name}:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n      path: {name}.duckdb\n'
        )
        for file, sql in files.items():
            (project / 'models' / file).write_text(sql)
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('PATH', f'{Path(dbt).parent}{os.pathsep}{os.environ.get("PATH", "")}')
        patch.setenv('DBT_SEND_ANONYMOUS_USAGE_STATS', 'false')
        build(load_config(root, trusted=True))
    return Checker(Project(root).graph), str(root / 'prices.csv')


def test_an_unqualified_name_is_read_in_the_schema_of_the_project_that_has_it(
    two_projects: tuple[Checker, str],
) -> None:
    """The graph keeps the default schema of one project, alpha's; beta's SQL names its trips
    unqualified, as beta's own default schema holds them. A name no schema holds, or names two
    schemas hold between them, is still unknown."""
    checker = two_projects[0]
    assert checker.graph.meta['default_catalog'] == 'alpha'
    assert loud(checker.check('select sum(km) as km from trips')) == []
    assert loud(checker.check('select count(*) from orders')) == []
    finding = only(checker.check('select * from nothing_here'), 'unknown-relation')
    assert finding.message == 'alpha.main.nothing_here is not a relation of the project'
    both = checker.check('select sum(km) from trips join orders on order_id = trip_id')
    assert 'unknown-relation' in loud(both)


def test_a_table_function_s_columns_are_those_it_lists(two_projects: tuple[Checker, str]) -> None:
    """read_csv(...) is no relation of the graph, whatever the default schema's name stands for:
    the columns read from it are those its call lists, and with none listed, those of a file the
    check does not read."""
    checker, csv = two_projects
    listed = (
        f"select p.price, o.order_id from read_csv('{csv}', columns = {{'sku': 'VARCHAR', 'price': 'INT', "
        "'valid_from': 'DATE'}) p join orders o using (sku) where o.ordered_at >= p.valid_from"
    )
    # The file is a relation like another to the join check, which knows no key of it or of orders.
    assert rules(checker.check(listed)) == ['unknown-join']
    assert loud(checker.check(listed.replace(', columns = {', ', types = {'))) == []
    finding = only(checker.check(listed.replace('p.price', 'p.cost')), 'unknown-column')
    assert finding.severity == 'warning' and finding.message == 'read_csv(...) has no column cost'
    unqualified = listed.replace('p.price', 'price').replace('p.valid_from', 'valid_from')
    assert loud(checker.check(unqualified)) == []
    assert only(checker.check(unqualified.replace('select price', 'select cost')), 'unknown-column')


def test_a_column_the_graph_may_not_list_is_only_a_note(tmp_path: Path) -> None:
    """Events are in no warehouse the build read: the graph has the columns a model was seen to
    read of them, which may be fewer than they have, so another column is a note, not a warning.
    A CTE that unnests a struct in its select list has the struct's fields as columns."""
    root = tmp_path / 'shop'
    root.mkdir()
    make_shop(root)
    (root / 'mart' / 'clicks.sql').write_text(
        "create table mart.clicks as select e.event_id, e.kind from raw.events e where e.kind = 'click'"
    )
    checker = Checker(Project(root).build(run_dbt=False).graph)
    finding = only(checker.check('select e.page from raw.events e'), 'unknown-column')
    assert finding.severity == 'info'
    assert finding.message.startswith('events has no column page that the graph knows of')
    # A name close to one the graph lists is likely misspelt.
    finding = only(checker.check('select e.kinds from raw.events e'), 'unknown-column')
    assert (
        finding.severity == 'warning' and finding.message == 'events has no column kinds; did you mean kind?'
    )
    assert only(checker.check('select o.page from raw.orders o'), 'unknown-column').severity == 'warning'
    struct = (
        "with c as (select o.order_id, unnest([{'a': 1, 'b': 2}], recursive := true) from raw.orders o) "
        'select c.order_id, c.a, c.b from c'
    )
    assert 'unknown-column' not in rules(checker.check(struct))
    assert 'unknown-column' in rules(
        checker.check('with c as (select order_id from raw.orders) select c.a from c')
    )


def test_a_column_that_only_chooses_the_rows_a_sum_adds_up_repeats_nothing(
    shop: Checker, shop_db: Path
) -> None:
    """Joined to its order, each payment is one row: a sum of the payments of completed orders adds
    each payment once, though the order's status repeats on every payment. The order's amount, as
    the value, repeats; and so does a count whose value is a constant, of rows the order's status
    chooses."""
    sql = (
        "select sum(case when o.status = 'completed' then p.amount end) as paid from raw.orders o "
        'join raw.payments p on p.order_id = o.order_id'
    )
    assert 'fan-out' not in rules(shop.check(sql))
    con = duckdb.connect(str(shop_db), read_only=True)
    assert (
        con.execute(sql).fetchall()
        == con.execute(
            "select sum(amount) from raw.payments where order_id in (select order_id from raw.orders where status = 'completed')"
        ).fetchall()
    )
    con.close()
    assert 'fan-out' in rules(shop.check(sql.replace('p.amount', 'o.amount')))
    assert 'fan-out' in rules(
        shop.check(sql.replace('sum(case', 'count(case').replace('then p.amount end', 'then 1 end'))
    )


def test_a_file_read_by_its_path_is_an_unknown_relation(shop: Checker, tmp_path: Path) -> None:
    """DuckDB reads a quoted path as a table, but nothing says what the file holds: joined to
    orders, a file with two rows for one order adds its amount twice, so the read is a warning,
    as for any relation the project does not have."""
    tags = tmp_path / 'tags.csv'
    tags.write_text('order_id,tag\n1,a\n1,b\n2,c\n')
    sql = f"select sum(o.amount) from raw.orders o join '{tags}' t on t.order_id = o.order_id"
    con = duckdb.connect()
    con.execute(
        'create schema raw; create table raw.orders as select * from (values (1, 100), (2, 50)) v(order_id, amount)'
    )
    assert con.execute(sql).fetchall() != con.execute('select sum(amount) from raw.orders').fetchall()
    for read in (f"'{tags}'", f'"{tags}"', "'tags.parquet'", '"events_*"'):
        finding = only(shop.check(sql.replace(f"'{tags}'", read)), 'unknown-relation')
        assert finding.severity == 'warning' and 'is not a relation of the project' in finding.message


def test_a_named_window_is_the_window_it_names(shop: Checker) -> None:
    """``OVER w`` with ``WINDOW w AS (PARTITION BY ...)`` runs over the partition the WINDOW clause
    gives: the first row of each order's payments is one row per order, and of each method's is
    not."""
    first = (
        'select sum(o.amount) from raw.orders o join (select order_id, amount from raw.payments '
        'window w as (partition by order_id order by payment_id) qualify row_number() over w = 1) p '
        'on p.order_id = o.order_id'
    )
    assert 'fan-out' not in rules(shop.check(first))
    assert 'fan-out' in rules(shop.check(first.replace('partition by order_id', 'partition by method')))


def test_a_rollup_has_rows_that_span_its_groups(shop: Checker, tmp_path: Path) -> None:
    """Grouped by the payment, each group reads one order; but the subtotal row of a ROLLUP, or of
    MySQL's WITH ROLLUP, reads every payment, and repeats each order once per payment. DuckDB shows
    the total row is not the total of the orders."""
    sql = (
        'select p.payment_id, sum(o.amount) as total from raw.payments p '
        'join raw.orders o on o.order_id = p.order_id group by rollup (p.payment_id)'
    )
    assert only(shop.check(sql), 'fan-out').severity == 'warning'
    assert 'fan-out' not in rules(shop.check(sql.replace('rollup (p.payment_id)', 'p.payment_id')))
    mysql = Checker(make_shop(tmp_path, dialect='mysql'))
    written = sql.replace('rollup (p.payment_id)', 'p.payment_id with rollup')
    assert only(mysql.check(written), 'fan-out').severity == 'warning'
    average = written.replace('sum(o.amount)', 'avg(p.amount)').replace(
        'join raw.orders o on o.order_id = p.order_id', 'join raw.payments q on q.order_id = p.order_id'
    )
    assert only(mysql.check(average), 'fan-out').severity == 'warning'
    con = duckdb.connect()
    con.execute(WAREHOUSE)
    con.execute("insert into raw.orders values (1, 1, 10, 'completed', '2026-01-01')")
    con.execute("insert into raw.payments values (1, 1, 4, 'card'), (2, 1, 6, 'card')")
    total = [r for r in con.execute(sql).fetchall() if r[0] is None]
    assert total == [(None, 20)] != con.execute('select null, sum(amount) from raw.orders').fetchall()


def test_a_null_safe_join_on_the_columns_a_distinct_keeps_matches_one_row(shop: Checker) -> None:
    """SELECT DISTINCT returns one row per combination of its columns, and a column that is the same
    on every row (a constant) distinguishes none, so the rows are unique on the others. Joined on
    those columns with IS NOT DISTINCT FROM, which matches NULL to NULL as DISTINCT groups NULLs
    into one row, each order matches at most one of them; with a column the join leaves out, it can
    match several."""
    sql = (
        "with w as (select distinct customer_id, status, date '1900-01-01' as since from raw.orders) "
        'select sum(p.amount) from raw.payments p join raw.orders o on o.order_id = p.order_id '
        'join w on w.customer_id = o.customer_id and w.status is not distinct from o.status '
        'and o.order_date >= w.since'
    )
    assert 'fan-out' not in rules(shop.check(sql))
    loose = sql.replace(
        'select distinct customer_id, status,', 'select distinct customer_id, status, amount,'
    )
    assert 'fan-out' in rules(shop.check(loose))
    # A key matched from two sources: each payment's order and its method fix one row of w.
    two = (
        'with w as (select distinct customer_id, status from raw.orders) select sum(p.amount) '
        'from raw.payments p join raw.orders o on o.order_id = p.order_id '
        'join w on w.customer_id = o.customer_id and w.status = p.method'
    )
    assert 'fan-out' not in rules(shop.check(two))
    # Customers and orders each joined on a key the other fixes, but orders joined to payments on
    # no key of theirs: many orders, each with its customer, match one payment.
    cycle = (
        'select sum(p.amount) from raw.payments p join raw.orders o on o.status = p.method '
        'join raw.customers c on c.customer_id = o.customer_id and o.order_id = c.customer_id'
    )
    assert only(shop.check(cycle), 'fan-out').severity == 'warning'
    # Rows a unique test shows unique can still share NULL, which IS NOT DISTINCT FROM matches.
    null_safe = [('p', 'order_id', 'o', 'order_id')]
    tested = {'o': [frozenset({'order_id'})]}
    assert reachable(['p', 'o'], [], tested, None, null_safe)['p'] == {'p'}
    assert reachable(['p', 'o'], [], tested, None, null_safe, tested)['p'] == {'p', 'o'}


ITEMS_WAREHOUSE = """
create schema raw;
create schema stg;
create schema mart;
create table raw.items (id int, order_id int, sku varchar);
create table raw.products (sku varchar, price int, weight int);
create table raw.supplies (supply_id int, sku varchar, cost int);
create table raw.orders (id int, order_total int, tax_paid int);
create table raw.events (id int, kind varchar);
create table stg.stg_items (item_id int primary key, order_id int, product_id varchar);
create table stg.stg_products (
    product_id varchar primary key, product_price decimal(12, 2), weight int, weight_kg decimal(12, 3)
);
create table stg.stg_orders (
    order_id int primary key, order_total decimal(12, 2), tax_paid_cents int, tax_paid decimal(12, 2)
);
create table stg.stg_events (event_id int primary key);
create table mart.order_items (
    item_id int primary key, order_id int, product_id varchar, product_price decimal(12, 2),
    supply_cost decimal(12, 2)
);
"""
ITEMS_MODELS = {
    'stg/stg_items.sql': 'create view stg.stg_items as select id as item_id, order_id, sku as product_id from raw.items',
    'stg/stg_products.sql': (
        'create view stg.stg_products as select sku as product_id, price / 100 as product_price, weight, '
        'weight / 1000 as weight_kg '
        'from raw.products'
    ),
    'stg/stg_orders.sql': (
        'create view stg.stg_orders as select id as order_id, order_total / 100 as order_total, '
        'tax_paid as tax_paid_cents, tax_paid / 100 as tax_paid from raw.orders'
    ),
    'stg/stg_events.sql': "create view stg.stg_events as select id as event_id from raw.events where kind = 'click'",
    'mart/order_items.sql': """\
create table mart.order_items as
with supply as (select sku, sum(cost / 100) as supply_cost from raw.supplies group by sku)
select i.item_id, i.order_id, i.product_id, p.product_price, s.supply_cost
from stg.stg_items i
left join stg.stg_products p on p.product_id = i.product_id
left join supply s on s.sku = i.product_id
""",
    'mart/order_totals.sql': (
        'create table mart.order_totals as select order_id, sum(product_price) as subtotal, '
        'sum(supply_cost) as cost from mart.order_items group by 1'
    ),
}


ITEMS_DEFINITIONS = """\
version: 1
terms:
  - name: Revenue
    status: approved
    approved: 2026-10-01
    owner: {name: Ana Ruiz}
    columns: [order_items.product_price]
    unit: currency
  - name: Order total
    status: approved
    approved: 2026-10-01
    owner: {name: Ana Ruiz}
    columns: [stg_orders.order_total]
    unit: currency
  - name: Large order
    status: approved
    approved: 2026-10-01
    owner: {name: Ana Ruiz}
    columns: [stg_orders.order_total]
    filters: ["stg_orders.order_total >= 20"]
  - name: Subtotal
    kind: measure
    status: approved
    approved: 2026-10-01
    owner: {name: Ana Ruiz}
    columns: [order_totals.subtotal]
  - name: Gross profit
    kind: measure
    status: approved
    approved: 2026-10-01
    owner: {name: Ana Ruiz}
    columns: [order_items.product_price, order_items.supply_cost]
"""


def make_items(
    root: Path, extra: dict[str, str] | None = None, warehouse: str = '', definitions: str = ''
) -> Graph:
    """Raw tables with no key of their own, staging views that copy them under tested keys, and a
    mart that carries each product's price and supply cost onto its order items and adds them up;
    the raw amounts are in cents."""
    con = duckdb.connect(str(root / 'items.duckdb'))
    con.execute(ITEMS_WAREHOUSE + warehouse)
    con.close()
    for name, sql in {**ITEMS_MODELS, **(extra or {})}.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(sql)
    (root / 'definitions.yml').write_text(ITEMS_DEFINITIONS + definitions)
    return Project(root).build(run_dbt=False).graph


@pytest.fixture(scope='module')
def items(tmp_path_factory: pytest.TempPathFactory) -> Checker:
    return Checker(make_items(tmp_path_factory.mktemp('items')))


@pytest.mark.parametrize(
    ('dialect', 'kept'),
    [
        ('duckdb', True),
        # A MySQL flag is a number: WHERE is_active keeps 2, which is_active = TRUE (= 1) does not.
        ('mysql', False),
    ],
)
def test_a_boolean_compared_with_true_is_the_boolean_where_it_is_a_type(
    tmp_path: Path, dialect: str, kept: bool
) -> None:
    con = duckdb.connect(str(tmp_path / 'w.duckdb'))
    con.execute(
        'create schema stg; create table stg.accounts (account_id int primary key, is_active int, balance int)'
    )
    con.close()
    (tmp_path / 'stg').mkdir()
    (tmp_path / 'stg' / 'account_bal.sql').write_text(
        'create view stg.account_bal as select account_id, balance from stg.accounts where is_active = true'
    )
    (tmp_path / 'definitions.yml').write_text(
        'version: 1\nterms:\n  - name: Active balance\n    kind: measure\n    status: approved\n'
        '    approved: 2026-10-01\n    owner: {name: Ana Ruiz}\n    columns: [account_bal.balance]\n'
        '    filters: ["accounts.is_active = true"]\n'
    )
    checker = Checker(Project(tmp_path).build(run_dbt=False, dialect=dialect).graph)
    sql = 'select sum(a.balance) as active_balance from stg.accounts a where a.is_active'
    assert ('missing-filter' not in rules(checker.check(sql))) is kept
    assert 'missing-filter' in rules(checker.check(sql.replace(' where a.is_active', '')))


def test_a_key_of_a_copy_is_no_key_of_what_it_copies(items: Checker) -> None:
    """Uniqueness flows downstream only: a staging view tested unique on a key it renames may be
    an incremental model merged on that key over a source that keeps every version of a row."""
    keys = {str(r.relation).split('.', 1)[1]: items.relation_keys(r) for r in items.graph.relations()}
    assert frozenset({'order_id'}) in keys['stg.stg_orders']
    assert keys['raw.orders'] == keys['raw.events'] == []


UNSOUND_WAREHOUSE = """
create schema raw; create schema stg; create schema mart;
create table raw.orders (id int, customer_id int, amount int, tags varchar[]);
create table raw.cust_cdc (id int, name varchar, updated_at timestamp, primary key (id, updated_at));
create table stg.stg_orders (order_id int primary key, customer_id int, amount int, ordered_at date, valid_to date);
create table stg.stg_cust (customer_id int primary key, name varchar);
create table stg.customers (customer_id int primary key, region varchar, credit_limit int);
create table stg.customer_months (customer_id int, month int, primary key (customer_id, month));
create table stg.order_months (order_id int primary key, month int);
create table mart.order_tags (order_id int, tag varchar, primary key (order_id, tag));
create table mart.order_lines (line_id int primary key, order_id int, customer_id int, credit_limit int);
create table mart.rates (region varchar, valid_from date, rate int, tags varchar[], primary key (region, valid_from));
create table mart.days (day date primary key);
create table mart.region_days (region varchar, day date, primary key (region, day));
create table raw.orders_tagged (order_id int primary key, tags varchar[]);
create table raw.lines (line_id int, order_id int);
"""
UNSOUND_MODELS = {
    'stg/stg_cust.sql': 'create table stg.stg_cust as select id as customer_id, name from raw.cust_cdc',
    'mart/order_tags.sql': 'create table mart.order_tags as select order_id, unnest(tags) as tag from stg.stg_orders',
    'mart/order_lines.sql': (
        'create table mart.order_lines as select l.line_id, l.order_id, o.customer_id, c.credit_limit '
        'from raw.lines l join stg.stg_orders o on o.order_id = l.order_id '
        'join stg.customers c on c.customer_id = o.customer_id'
    ),
}
UNSOUND_DEFINITIONS = """\
version: 1
terms:
  - name: Credit limit
    kind: term
    status: approved
    approved: 2026-10-01
    owner: {name: Ana Ruiz}
    columns: [order_lines.credit_limit]
"""
UNSOUND_DATA = """
create schema raw; create schema stg; create schema mart;
create table raw.cust_cdc as select * from (values (10, 'Ann', timestamp '2026-01-01'),
    (10, 'Ann B', timestamp '2026-02-01')) v(id, name, updated_at);
create table stg.stg_orders as select * from (values (1, 10, 100, date '2026-03-01', null::date),
    (2, 10, 50, date '2026-03-02', null::date)) v(order_id, customer_id, amount, ordered_at, valid_to);
create table stg.customers as select * from (values (10, 'EU', 500), (11, 'EU', 700)) v(customer_id, region, credit_limit);
create table stg.customer_months as select * from (values (10, 1), (10, 2), (10, 3)) v(customer_id, month);
create table stg.order_months as select * from (values (1, 1), (2, 2)) v(order_id, month);
create table mart.order_tags as select * from (values (1, 'a'), (1, 'b'), (2, 'c')) v(order_id, tag);
create table mart.rates as select * from (values ('EU', date '2026-01-01', 1, ['x']),
    ('EU', date '2026-02-01', 2, ['y', 'z']), ('US', date '2026-01-01', 3, ['w'])) v(region, valid_from, rate, tags);
create table mart.region_days as select * from (values ('EU', date '2026-01-15'), ('EU', date '2026-01-20'),
    ('EU', date '2026-02-15')) v(region, day);
create table mart.days as select * from (values (date '2026-01-15'), (date '2026-02-15'), (date '2026-03-15')) v(day);
create table raw.orders_tagged as select * from (values (1, ['a', 'b']), (2, ['c'])) v(order_id, tags);
create table raw.lines as select * from (values (1, 1), (2, 1), (3, 2)) v(line_id, order_id);
create view mart.order_lines as select l.line_id, l.order_id, o.customer_id, c.credit_limit
    from raw.lines l join stg.stg_orders o on o.order_id = l.order_id join stg.customers c on c.customer_id = o.customer_id;
"""


@pytest.fixture(scope='module')
def unsound(tmp_path_factory: pytest.TempPathFactory) -> Checker:
    """A shop whose joins each repeat rows in a way a looser rule would miss."""
    root = tmp_path_factory.mktemp('unsound')
    con = duckdb.connect(str(root / 'w.duckdb'))
    con.execute(UNSOUND_WAREHOUSE)
    con.close()
    for name, sql in UNSOUND_MODELS.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(sql)
    (root / 'definitions.yml').write_text(UNSOUND_DEFINITIONS)
    return Checker(Project(root).build(run_dbt=False).graph)


INTERVALS = (
    '(select region, valid_from, lead(valid_from) over (partition by region order by valid_from) as valid_to, '
    'rate from mart.rates)'
)
RATES = (
    'join stg.customers c on c.customer_id = o.customer_id join (select region, valid_from, '
    'lead(valid_from) over (partition by region order by valid_from) as valid_to from mart.rates) r '
    'on r.region = c.region and o.ordered_at >= r.valid_from'
)


@pytest.mark.parametrize(
    ('sql', 'truth'),
    [
        # A unique test on a copy shows nothing of its source, which may hold every version.
        (
            'select sum(o.amount) from stg.stg_orders o join raw.cust_cdc c on c.id = o.customer_id',
            'select sum(amount) from stg.stg_orders',
        ),
        # An UNNEST in the select list repeats each order once per tag.
        (
            'select sum(o.amount) from stg.stg_orders o join mart.order_tags t on t.order_id = o.order_id',
            'select sum(amount) from stg.stg_orders',
        ),
        # A key matched only in an outer join's ON fixes nothing: the join keeps what it misses.
        (
            'select sum(o.amount) from stg.stg_orders o join stg.customer_months cm on cm.customer_id = o.customer_id '
            'left join stg.order_months om on om.order_id = o.order_id and om.month = cm.month',
            'select sum(amount) from stg.stg_orders',
        ),
        # The NULL arm tests the order's own column, so every interval after the start matches.
        (
            f'select sum(o.amount) from stg.stg_orders o {RATES} and (o.valid_to is null or o.ordered_at < r.valid_to)',
            'select sum(amount) from stg.stg_orders',
        ),
        # uuid() differs on every row, so DISTINCT keeps every tag of an order.
        (
            'select sum(o.amount) from stg.stg_orders o join (select distinct uuid() as u, order_id '
            'from mart.order_tags) t on t.order_id = o.order_id',
            'select sum(amount) from stg.stg_orders',
        ),
        # A term that only names the copy says nothing of adding it up per line.
        (
            'select c.region, sum(c.credit_limit) from mart.order_lines l join stg.customers c '
            'on c.customer_id = l.customer_id group by 1',
            'select region, sum(credit_limit) from stg.customers where customer_id in (select customer_id from mart.order_lines) group by 1',
        ),
        # GROUP BY ALL groups by the order and the tag, not by nothing.
        (
            'select sum(o.amount) from stg.stg_orders o join (select order_id, tag, count(*) as n '
            'from mart.order_tags group by all) g on g.order_id = o.order_id',
            'select sum(amount) from stg.stg_orders',
        ),
        # IS NOT DISTINCT FROM on a column that is no key is a join that repeats rows.
        (
            'select sum(o.amount) from stg.stg_orders o join (select order_id, tag from mart.order_tags) d '
            'on d.order_id is not distinct from o.order_id',
            'select sum(amount) from stg.stg_orders',
        ),
        # A scalar subquery's aggregate is computed once per group: GROUP BY ALL groups by tg too.
        (
            'select sum(o.amount) from stg.stg_orders o join (select order_id, coalesce(tag, (select max(tag) '
            'from mart.order_tags)) as tg, count(*) as n from mart.order_tags group by all) g '
            'on g.order_id = o.order_id',
            'select sum(amount) from stg.stg_orders',
        ),
        # The first row of each tag, through a named window, is one row per tag, not per order.
        (
            'select sum(o.amount) from stg.stg_orders o join (select order_id, tag from mart.order_tags '
            'window w as (partition by tag order by order_id) qualify row_number() over w = 1) t '
            'on t.order_id = o.order_id',
            'select sum(amount) from stg.stg_orders',
        ),
        # Intervals per region, through a named window, joined without the region: one per region.
        (
            'select sum(o.amount) from stg.stg_orders o join (select region, valid_from, lead(valid_from) '
            'over (w order by valid_from) as valid_to from mart.rates window w as (partition by region)) r '
            'on o.ordered_at >= r.valid_from and (r.valid_to is null or o.ordered_at < r.valid_to)',
            'select sum(amount) from stg.stg_orders',
        ),
        # The point is a day, which the customer does not fix: each order matches every day's interval.
        (
            'select sum(o.amount) from stg.stg_orders o join stg.customers c on c.customer_id = o.customer_id '
            'join (select region, valid_from, lead(valid_from) over (partition by region order by valid_from) '
            'as valid_to from mart.rates) r on r.region = c.region join mart.days d on d.day >= r.valid_from '
            'and (r.valid_to is null or d.day < r.valid_to)',
            'select sum(amount) from stg.stg_orders',
        ),
        # Each interval matches every day of its region in it: the rate repeats once per day.
        (
            f'select sum(r.rate) from mart.region_days d join {INTERVALS} r on r.region = d.region '
            'and d.day >= r.valid_from and (r.valid_to is null or d.day < r.valid_to)',
            "select sum(rate) from mart.rates where region = 'EU'",
        ),
        # A subquery of intervals that unnests in its select list repeats each interval per element.
        (
            'select sum(o.amount) from stg.stg_orders o join stg.customers c on c.customer_id = o.customer_id '
            'join (select region, valid_from, lead(valid_from) over (partition by region order by valid_from) '
            'as valid_to, unnest(tags) as tag from mart.rates) r on r.region = c.region '
            'and o.ordered_at >= r.valid_from and (r.valid_to is null or o.ordered_at < r.valid_to)',
            'select sum(amount) from stg.stg_orders',
        ),
        # A range in a later LEFT JOIN's ON pairs nothing: each order matches every interval of its region.
        (
            'select sum(o.amount) from stg.stg_orders o join stg.customers c on c.customer_id = o.customer_id '
            f'join {INTERVALS} r on r.region = c.region left join mart.days d on d.day = o.ordered_at '
            'and o.ordered_at >= r.valid_from and (r.valid_to is null or o.ordered_at < r.valid_to)',
            'select sum(amount) from stg.stg_orders',
        ),
        # So does a key's equality there: k is every order, for each order.
        (
            'select sum(o.amount) from stg.stg_orders o join stg.stg_orders k on true '
            'left join mart.days d on d.day = o.ordered_at and k.order_id = o.order_id',
            'select sum(amount) from stg.stg_orders',
        ),
        # Joined in parentheses, the LEFT JOIN still keeps every order and every k beside it.
        (
            'select sum(o.amount) from stg.stg_orders o join stg.stg_orders k on true '
            'left join (mart.days d join stg.order_months m on m.month = 1) '
            'on d.day = o.ordered_at and k.order_id = o.order_id',
            'select sum(amount) from stg.stg_orders',
        ),
        # Grouped, then unnested in the select list: each group's row repeats per element.
        (
            'select sum(o.amount) from stg.stg_orders o join (select order_id, unnest(list(tag)) as tag '
            'from mart.order_tags group by order_id) t on t.order_id = o.order_id',
            'select sum(amount) from stg.stg_orders',
        ),
        (
            'select sum(o.amount) from stg.stg_orders o join (select order_id, unnest(list(tag)) as tag '
            'from mart.order_tags group by order_id) t on t.order_id is not distinct from o.order_id',
            'select sum(amount) from stg.stg_orders',
        ),
        # A ROW_NUMBER beside an UNNEST in the select list numbers the rows before they repeat.
        (
            'select sum(o.amount) from stg.stg_orders o join (select order_id, tag from (select order_id, '
            'unnest(tags) as tag, row_number() over (partition by order_id order by order_id) as rn '
            'from raw.orders_tagged) s where rn = 1) t on t.order_id = o.order_id',
            'select sum(amount) from stg.stg_orders',
        ),
        # Joined on a range alone, any number of days match an order.
        (
            'select sum(o.amount) from stg.stg_orders o join mart.days d on d.day <= o.ordered_at',
            'select sum(amount) from stg.stg_orders',
        ),
        # An UNNEST in the select list runs after QUALIFY and DISTINCT: it repeats the rows they keep.
        (
            'select sum(o.amount) from stg.stg_orders o join (select order_id, unnest(tags) as tag '
            'from raw.orders_tagged qualify row_number() over (partition by order_id order by order_id) = 1) t '
            'on t.order_id = o.order_id',
            'select sum(amount) from stg.stg_orders',
        ),
        (
            'select sum(o.amount) from stg.stg_orders o join (select distinct order_id, unnest(tags) as tag '
            'from raw.orders_tagged) t on t.order_id = o.order_id',
            'select sum(amount) from stg.stg_orders',
        ),
    ],
)
def test_a_join_that_repeats_rows_is_reported_however_it_is_written(
    unsound: Checker, sql: str, truth: str
) -> None:
    """Each join repeats rows in the data: the total it gives is not the true one."""
    assert only(unsound.check(sql), 'fan-out').severity == 'warning'
    con = duckdb.connect()
    con.execute(UNSOUND_DATA)
    assert con.execute(sql).fetchall() != con.execute(truth).fetchall()


def test_a_join_in_parentheses_pairs_the_rows_of_each_source_in_it(tmp_path: Path) -> None:
    """A LEFT JOIN of a join in parentheses brings in each source in it: an equality in its ON on
    the key of one of them pairs each order with one customer, and each customer with one day."""
    warehouse = (
        'create schema stg; create schema mart; '
        'create table stg.stg_orders (order_id int primary key, customer_id int, amount int); '
        'create table mart.cust (customer_id int primary key, since date); '
        'create table mart.days (day date primary key); '
    )
    data = (
        'insert into stg.stg_orders values (1, 10, 100), (2, 10, 50); '
        "insert into mart.cust values (10, date '2026-03-01'), (11, date '2026-01-10'); "
        "insert into mart.days values (date '2026-01-15'), (date '2026-02-15'), (date '2026-03-01');"
    )
    con = duckdb.connect(str(tmp_path / 'w.duckdb'))
    con.execute(warehouse + data)
    sql = (
        'select sum(o.amount) from stg.stg_orders o left join (mart.cust k join mart.days d on d.day = k.since) '
        'on k.customer_id = o.customer_id'
    )
    assert con.execute(sql).fetchall() == con.execute('select sum(amount) from stg.stg_orders').fetchall()
    con.close()
    result = Checker(Project(tmp_path).build(run_dbt=False).graph).check(sql)
    assert result.passed and result.findings == []


def test_a_value_the_project_adds_up_at_the_grain_of_the_rows_it_is_joined_to_is_not_a_fan_out(
    items: Checker,
) -> None:
    """Revenue is the price of each item's product, which the project's own order_items repeats on
    every item and adds up: joined to items on the product's key, the price counts once per item,
    as the measure means. A value the project never adds up at that grain still repeats."""
    for sql in (
        'select sum(p.product_price) as revenue from stg.stg_items i join stg.stg_products p on p.product_id = i.product_id',
        'select sum(p.product_price) from stg.stg_items i join stg.stg_products p on p.product_id = i.product_id',
        'select count(*) as items from stg.stg_items i join stg.stg_products p on p.product_id = i.product_id',
    ):
        assert loud(items.check(sql)) == [], sql
    for sql in (
        'select sum(p.weight) from stg.stg_items i join stg.stg_products p on p.product_id = i.product_id',
        'select sum(o.order_total) from stg.stg_orders o join stg.stg_items i on i.order_id = o.order_id',
    ):
        assert only(items.check(sql), 'fan-out').severity == 'warning', sql


def test_a_value_the_project_sums_without_a_definition_at_that_grain_still_repeats(
    tmp_path: Path,
) -> None:
    """A model that copies each product's weight onto its items and sums it there, and another
    that sums the weight per product, say nothing about whether the weight adds up once per item:
    a project's own model can repeat a value as wrongly as the query. Only a definition bound to
    the copy says the value is a measure at that grain."""
    checker = Checker(
        make_items(
            tmp_path,
            {
                'mart/item_weights.sql': (
                    'create table mart.item_weights as select i.item_id, i.order_id, p.weight '
                    'from stg.stg_items i join stg.stg_products p on p.product_id = i.product_id'
                ),
                'mart/shipped.sql': (
                    'create table mart.shipped as select order_id, sum(weight) as shipped_weight '
                    'from mart.item_weights group by 1'
                ),
                'mart/product_weight.sql': (
                    'create table mart.product_weight as select product_id, sum(weight) as total_weight '
                    'from stg.stg_products group by 1'
                ),
            },
            'create table mart.item_weights (item_id int primary key, order_id int, weight int);',
        )
    )
    sql = 'select sum(p.weight) from stg.stg_items i join stg.stg_products p on p.product_id = i.product_id'
    assert only(checker.check(sql), 'fan-out').severity == 'warning'
    sql = 'select sum(p.product_price) as revenue from stg.stg_items i join stg.stg_products p on p.product_id = i.product_id'
    assert loud(checker.check(sql)) == []


def test_an_aggregate_the_project_adds_up_at_the_grain_of_the_rows_it_is_joined_to_is_not_a_fan_out(
    items: Checker,
) -> None:
    """A product's supply cost, summed over its supplies and joined to items on the product, is what
    order_items repeats on each item and the project adds up: it counts once per item. A total of
    another grain (each order's subtotal) still repeats on every item of the order."""
    sql = (
        'with supply as (select sku, sum(cost) as cost from raw.supplies group by sku) '
        'select sum(p.product_price) - sum(coalesce(s.cost, 0)) / 100 as gross_profit from stg.stg_items i '
        'join stg.stg_products p on p.product_id = i.product_id left join supply s on s.sku = i.product_id'
    )
    assert loud(items.check(sql)) == []
    by_supply = sql.replace('group by sku', 'group by sku, supply_id').replace(
        'sku, sum', 'sku, supply_id, sum'
    )
    assert 'fan-out' in loud(items.check(by_supply))
    subtotal = (
        'with t as (select order_id, sum(product_price) as subtotal from mart.order_items group by order_id) '
        'select sum(t.subtotal) from stg.stg_items i join t on t.order_id = i.order_id'
    )
    assert only(items.check(subtotal), 'fan-out').severity == 'warning'


@pytest.mark.parametrize(
    ('sql', 'flagged'),
    [
        # tax_paid is in cents: the project copies it as tax_paid_cents and divides it by 100.
        ('select sum(tax_paid) from raw.orders', True),
        ('select sum(tax_paid_cents) from stg.stg_orders', True),
        ('select sum(tax_paid) / 100.0 from raw.orders', False),
        ('select sum(tax_paid) as tax_paid_cents from raw.orders', False),
        ('select sum(tax_paid) from stg.stg_orders', False),
        # price is in cents: Revenue, an amount of money, reads it divided by 100.
        ('select max(p.price) from raw.products p', True),
        ('select round(avg(p.price) / 100, 2) as average_price from raw.products p', False),
        ('select sum(p.price * 2) / 100 from raw.products p', False),
        # A ratio of two amounts in cents is not in cents.
        (
            'select round(100.0 * sum(p.price) filter (where p.weight > 1) / sum(p.price), 1) from raw.products p',
            False,
        ),
        # weight is divided by 1000, but nothing says it is an amount of money or in minor units.
        ('select sum(weight) from raw.products', False),
        # Divided by 100 however it is written, the result is in whole units.
        ('select sum(p.price) / 10 / 10 from raw.products p', False),
        ('select sum(p.price) / cast(100 as decimal) from raw.products p', False),
        ('select sum(p.price) / 100::numeric from raw.products p', False),
        ('select sum(p.price) * 1.0 / 100 from raw.products p', False),
        # A percentile of amounts is an amount.
        ('select percentile_cont(0.5) within group (order by p.price) from raw.products p', True),
        ('select quantile_cont(p.price, 0.5) from raw.products p', True),
    ],
)
def test_a_result_in_cents_of_what_the_project_reads_in_whole_units_is_a_warning(
    items: Checker, sql: str, flagged: bool
) -> None:
    result = items.check(sql)
    assert ('minor-units' in loud(result)) is flagged, str(result)


def test_a_rate_that_scales_an_amount_of_money_is_not_in_cents(tmp_path: Path) -> None:
    """A discount in percent, divided by 100 to make the rate that multiplies a subtotal, is not an
    amount of money: the net amount carries the subtotal's units, not the rate's. An amount divided
    by 100 that the net amount adds up as it is still is."""
    checker = Checker(
        make_items(
            tmp_path,
            {
                'stg/stg_quotes.sql': (
                    'create view stg.stg_quotes as select id as quote_id, subtotal, '
                    'discount_percent / 100 as discount_rate, fee / 100 as fee from raw.quotes'
                ),
                'mart/quotes.sql': (
                    'create table mart.quotes as select quote_id, subtotal * (1 - discount_rate) as net_amount, '
                    'subtotal * discount_rate as discount, fee + subtotal as total from stg.stg_quotes'
                ),
            },
            'create table raw.quotes (id int, subtotal decimal(12, 2), discount_percent int, fee int);',
            """\
  - name: Net quote
    status: approved
    columns: [quotes.net_amount]
    unit: currency
  - name: Discount
    status: approved
    columns: [quotes.discount]
    unit: currency
  - name: Quote total
    status: approved
    columns: [quotes.total]
    unit: currency
""",
        )
    )
    assert 'minor-units' not in rules(
        checker.check('select avg(discount_percent) as discount from raw.quotes')
    )
    assert only(checker.check('select sum(fee) as fees from raw.quotes'), 'minor-units').severity == 'warning'


def test_a_minor_units_finding_says_how_the_project_reads_the_column(items: Checker) -> None:
    finding = only(items.check('select sum(tax_paid) as tax from raw.orders'), 'minor-units')
    assert (finding.severity, finding.column) == ('warning', 'tax')
    assert finding.message == (
        'tax is in hundredths: orders.tax_paid holds hundredths of the unit stg_orders.tax_paid reads '
        'it in, and the query does not divide it by 100'
    )
    assert finding.fix == 'Divide SUM(orders.tax_paid) by 100, or read stg_orders.tax_paid.'
    assert finding.evidence == [
        'stg_orders.tax_paid = orders.tax_paid / 100',
        'stg_orders.tax_paid_cents copies orders.tax_paid',
    ]


def test_a_threshold_a_definition_sets_in_whole_units_compared_with_cents_is_a_warning(
    items: Checker,
) -> None:
    finding = only(items.check('select count(*) from raw.orders where order_total >= 20'), 'minor-units')
    assert finding.message == (
        'the query compares orders.order_total, in hundredths, with 20, the value '
        "'Large order' requires of stg_orders.order_total = orders.order_total / 100"
    )
    assert finding.fix == 'Compare orders.order_total with 2000, or compare stg_orders.order_total with 20.'
    for sql in (
        'select count(*) from raw.orders where order_total >= 2000',
        'select count(*) from stg.stg_orders where order_total >= 20',
        'select count(*) from raw.orders where order_total >= 25',
    ):
        assert loud(items.check(sql)) == [], sql


def test_a_measure_named_for_a_term_computed_from_another_term_s_column_is_a_warning(items: Checker) -> None:
    """Revenue is the price of each item; an order's total, which another term holds, is not it,
    though both are amounts of an order. A measure named revenue that reads only the order total
    computes the other term; one that also reads a column no other term holds may be revenue
    computed another way."""
    for sql in (
        'select sum(order_total) / 100.0 as revenue from raw.orders',
        'select sum(order_total) as revenue from stg.stg_orders',
    ):
        finding = only(items.check(sql), 'conflicting-column')
        assert (finding.severity, finding.column, finding.term) == ('warning', 'revenue', 'Revenue'), sql
    finding = only(
        items.check('select sum(order_total) as revenue from stg.stg_orders'), 'conflicting-column'
    )
    assert finding.message == (
        "revenue is named for 'Revenue', but reads stg_orders.order_total, which holds 'Order total', "
        "not 'Revenue' (order_items.product_price)"
    )
    assert finding.fix == (
        "Compute 'Revenue' from order_items.product_price (approved 'Gross profit', approved 'Revenue'), "
        'or name the column for what it computes.'
    )
    for sql in (
        'select sum(p.product_price) as revenue from stg.stg_items i join stg.stg_products p on p.product_id = i.product_id',
        'select sum(subtotal) as revenue from mart.order_totals',
        'select sum(weight) as revenue from raw.products',
        # The order total less its tax reads a column no other term holds: it may be revenue,
        # computed another way.
        'select sum(order_total) - sum(tax_paid) as revenue from stg.stg_orders',
    ):
        assert 'conflicting-column' not in rules(items.check(sql)), sql


def test_a_constant_factor_inside_or_outside_a_sum_is_the_same_formula(items: Checker) -> None:
    """A sum of each price divided by 100 is the sum of the prices divided by 100, where division is
    exact (DuckDB): the subtotal recomputed either way computes the same measure."""
    join = 'from raw.items i join raw.products p on p.sku = i.sku'
    for value in ('sum(p.price) / 100.0', 'sum(p.price / 100)', '0.01 * sum(p.price)'):
        assert 'formula-differs' not in rules(items.check(f'select {value} as subtotal {join}')), value
    assert 'formula-differs' in rules(items.check(f'select sum(p.price) / 10 as subtotal {join}'))


def test_a_measure_of_a_term_bound_to_its_inputs_is_not_compared_with_one_of_them(items: Checker) -> None:
    """Gross profit is bound to the price and the supply cost it is computed from: a query that
    computes it from both is not a different formula for the price alone. Nothing says how the two
    combine, so the check says it did not compare the formula, whether it subtracts or adds."""
    sql = (
        'with supply as (select sku, sum(cost) as cost from raw.supplies group by sku) '
        'select sum(p.product_price) - sum(coalesce(s.cost, 0)) / 100 as gross_profit from stg.stg_items i '
        'join stg.stg_products p on p.product_id = i.product_id left join supply s on s.sku = i.product_id'
    )
    for written in (sql, sql.replace(') - sum(', ') + sum(')):
        result = items.check(written)
        assert loud(result) == []
        note = only(result, 'formula-differs')
        assert note.severity == 'info' and note.term == 'Gross profit'
        assert 'order_items.product_price, order_items.supply_cost' in note.message
        assert 'not compared' in note.message


def test_a_threshold_in_cents_scaled_from_the_definition_s_counts_as_its_filter(items: Checker) -> None:
    """Large orders are those of 20 dollars or more: order_total >= 2000 on the cents it is read
    from is that filter."""
    sql = 'select count(*) as large_orders from raw.orders where order_total >= 2000'
    assert 'missing-filter' not in rules(items.check(sql))
    finding = only(
        items.check('select count(*) as large_orders from raw.orders where order_total >= 1000'),
        'missing-filter',
    )
    assert finding.message.startswith("'Large order' requires stg_orders.order_total >= 20")


@pytest.mark.parametrize(
    'total',
    [
        'cast(order_total / 100 as int)',
        'round(order_total / 100)',
        'order_total // 100',
    ],
)
def test_a_threshold_in_cents_is_no_filter_of_a_rounded_amount(tmp_path: Path, total: str) -> None:
    """Rounded or cast to an integer, 1960 cents is 20 dollars, a large order, though it is below
    2000 cents: order_total >= 2000 on the cents leaves it out. A division that truncates moves the
    boundary of other comparisons (2050 // 100 > 20 is false), so it is not taken as scaled either."""
    model = (
        f'create view stg.stg_orders as select id as order_id, {total} as order_total, '
        'tax_paid as tax_paid_cents, tax_paid / 100 as tax_paid from raw.orders'
    )
    checker = Checker(make_items(tmp_path, {'stg/stg_orders.sql': model}))
    sql = 'select count(*) as large_orders from raw.orders where order_total >= 2000'
    assert 'missing-filter' in rules(checker.check(sql))
    con = duckdb.connect()
    con.execute('create table orders as select * from (values (1960), (2500)) v(order_total)')
    rounded = con.execute(f'select count(*) from (select {total} as t from orders) where t >= 20').fetchall()
    if '//' not in total:
        assert rounded != con.execute('select count(*) from orders where order_total >= 2000').fetchall()


def test_distinct_without_the_key_keeps_the_fan_out(shop: Checker) -> None:
    sql = (
        'with x as (select distinct o.customer_id, o.amount, p.payment_id from raw.orders o '
        'join raw.payments p on p.order_id = o.order_id) select sum(amount) from x'
    )
    assert 'fan-out' in rules(shop.check(sql))


def test_a_fan_out_inside_a_subquery_is_found_where_it_is_aggregated(shop: Checker) -> None:
    sql = (
        'with j as (select o.amount, p.payment_id from raw.orders o join raw.payments p on p.order_id = o.order_id) '
        'select sum(amount) from j'
    )
    finding = only(shop.check(sql), 'fan-out')
    assert finding.column is None and finding.message.startswith('SUM(j.amount) adds orders.amount')


def test_a_fan_out_through_a_bridge_table(bridge: Checker) -> None:
    sql = (
        'select c.country, sum(o.amount) as revenue from raw.orders o '
        'join raw.customer_segments cs on cs.customer_id = o.customer_id '
        'join raw.customers c on c.customer_id = o.customer_id group by 1'
    )
    finding = only(bridge.check(sql), 'fan-out')
    assert finding.severity == 'warning'
    assert finding.evidence == [
        'customer_segments has one row per customer_id, segment_id, not per customer_id, so many of its rows '
        'can match one orders row'
    ]
    # The side joined on its unique key repeats once per segment.
    unique_side = (
        'select sum(v.completed_revenue) as completed_revenue from mart.customer_value v '
        'join raw.customer_segments cs on cs.customer_id = v.customer_id'
    )
    assert only(bridge.check(unique_side), 'fan-out').severity == 'warning'
    # Grouped by the rest of the bridge's key, each customer counts once per segment.
    grouped = (
        'select cs.segment_id, sum(v.completed_revenue) as completed_revenue from mart.customer_value v '
        'join raw.customer_segments cs on cs.customer_id = v.customer_id group by cs.segment_id'
    )
    assert 'fan-out' not in rules(bridge.check(grouped))


def test_a_join_on_a_key_to_a_relation_with_no_known_key_is_a_note(bridge: Checker) -> None:
    sql = (
        'select sum(v.completed_revenue) as completed_revenue from mart.customer_value v '
        'join raw.customer_notes n on n.customer_id = v.customer_id'
    )
    finding = only(bridge.check(sql), 'fan-out')
    assert finding.severity == 'info'
    assert finding.evidence == [
        'customer_value is joined on its key, and nothing says customer_notes has one row per customer_id'
    ]


def test_a_coarser_measure_repeated_on_finer_rows_is_a_mixed_grain(shop: Checker) -> None:
    sql = (
        'select o.order_id, r.net_revenue from raw.orders o '
        'join mart.customer_revenue r on r.customer_id = o.customer_id'
    )
    finding = only(shop.check(sql), 'mixed-grain')
    assert finding.column == 'net_revenue'
    assert finding.message.startswith('customer_revenue.net_revenue has one value per customer_revenue row')
    # A dimension repeated on finer rows is fine.
    assert 'mixed-grain' not in rules(
        shop.check(
            'select o.order_id, c.region from raw.orders o join raw.customers c on c.customer_id = o.customer_id'
        )
    )


@pytest.mark.parametrize(
    ('latest', 'repeats'),
    [
        ('select customer_id, status as top_status from ranked where rn = 1', False),
        ('select customer_id, status as top_status from ranked where 1 = rn', False),
        ('select customer_id, status as top_status from ranked where rn <= 2', True),
        (
            'select customer_id, status as top_status from by_status '
            'qualify row_number() over (partition by customer_id order by amount desc) = 1',
            False,
        ),
        (
            'select customer_id, status as top_status from by_status '
            'qualify rank() over (partition by customer_id order by amount desc) = 1',
            True,
        ),
    ],
)
def test_the_first_row_of_each_partition_is_one_row_per_partition(
    shop: Checker, latest: str, repeats: bool
) -> None:
    """Keeping the rows where ROW_NUMBER() over a partition is 1 keeps one row per value of the
    partition's columns; RANK() keeps every row that ties, and rn <= 2 keeps two."""
    sql = (
        'with by_status as (select customer_id, status, sum(amount) as amount from raw.orders group by 1, 2), '
        'ranked as (select customer_id, status, '
        'row_number() over (partition by customer_id order by amount desc) as rn from by_status), '
        f'top_status as ({latest}) '
        'select r.customer_id, r.net_revenue, t.top_status from mart.customer_revenue r '
        'join top_status t on t.customer_id = r.customer_id'
    )
    assert ('mixed-grain' in rules(shop.check(sql))) is repeats


def test_an_amount_the_project_adds_up_repeated_on_finer_rows_is_a_mixed_grain(
    bare: Checker, bridge: Checker
) -> None:
    # orders.amount is bound to no term, but customer_revenue adds it up.
    sql = 'select p.payment_id, o.amount from raw.payments p join raw.orders o on o.order_id = p.order_id'
    assert only(bare.check(sql), 'mixed-grain').column == 'amount'
    # A price is not added up anywhere, so repeating it is not a mixed grain.
    price = 'select i.item_id, p.list_price from raw.order_items i join raw.products p on p.product_id = i.product_id'
    assert 'mixed-grain' not in rules(bridge.check(price))


def test_a_join_on_the_wrong_key_is_put_back_on_the_relationship(shop: Checker, shop_db: Path) -> None:
    result = shop.check(
        'select o.order_id, c.name from raw.orders o join raw.customers c on o.order_id = c.customer_id'
    )
    finding = only(result, 'join-off-relationship')
    assert finding.rewrite == 'join on o.customer_id = c.customer_id'
    assert 'order refers to customer through orders.customer_id = customers.customer_id' in finding.message
    assert result.corrected is not None and 'ON o.customer_id = c.customer_id' in result.corrected
    assert shop.check(result.corrected).findings == []
    con = duckdb.connect(str(shop_db), read_only=True)
    try:
        assert con.execute(result.corrected).fetchall()
    finally:
        con.close()


def test_a_join_of_one_key_to_another_entity_s_identifier_is_a_warning(shop: Checker) -> None:
    result = shop.check(
        'select c.name from raw.payments p join raw.customers c on p.payment_id = c.customer_id'
    )
    finding = only(result, 'unknown-join')
    assert finding.severity == 'warning'
    assert 'payments.payment_id, which identifies a payment' in finding.message


def test_a_join_nothing_vouches_for_is_a_note(shop: Checker) -> None:
    result = shop.check('select o.order_id from raw.orders o join raw.payments p on o.status = p.method')
    finding = only(result, 'unknown-join')
    assert finding.severity == 'info' and result.passed and not result.failed(strict=True)


@pytest.mark.parametrize(
    'sql',
    [
        # A CTE grouped by its join column has one row per value of it.
        'with p as (select method, sum(amount) as paid from raw.payments group by method) '
        'select o.status, sum(p.paid) from p join raw.orders o on o.status = p.method group by 1',
        # A relation built by one query grouped by a key has one row per key.
        'select r.region, r.net_revenue, p.payment_id from mart.customer_revenue r '
        'join raw.payments p on p.amount = r.customer_id',
    ],
)
def test_a_join_on_a_grouped_key_is_not_an_unknown_join(shop: Checker, sql: str) -> None:
    assert 'unknown-join' not in rules(shop.check(sql))


GROUPED_THEN_JOINED = {
    'region_customers': (
        'create table mart.region_customers as with r as (select c.region, sum(o.amount) as region_revenue '
        'from raw.orders o join raw.customers c on c.customer_id = o.customer_id group by c.region) '
        'select r.region, r.region_revenue, c.customer_id, c.name from r join raw.customers c on c.region = r.region'
    ),
    'order_lines': (
        'create table mart.order_lines as select o.order_id, o.customer_id, p.payment_id, p.amount as paid '
        'from (select order_id, customer_id from raw.orders group by order_id, customer_id) o '
        'join raw.payments p on p.order_id = o.order_id'
    ),
}


def test_a_model_that_groups_and_joins_back_to_detail_has_no_grouped_key(tmp_path: Path) -> None:
    make_shop(tmp_path)
    for name, sql in GROUPED_THEN_JOINED.items():
        (tmp_path / 'mart' / f'{name}.sql').write_text(sql)
    checker = Checker(Project(tmp_path).build(run_dbt=False).graph)
    for name in GROUPED_THEN_JOINED:
        relation = checker.graph.node_for_relation(f'shop.mart.{name}')
        assert relation is not None and checker.relation_keys(relation) == [], name
    grouped = checker.graph.node_for_relation('shop.mart.customer_revenue')
    assert grouped is not None and checker.relation_keys(grouped) == [frozenset({'customer_id'})]
    fanned = checker.check(
        'select o.status, sum(o.amount) as order_amount from raw.orders o '
        'join mart.order_lines l on l.order_id = o.order_id group by 1'
    )
    assert 'fan-out' in rules(fanned)


def test_a_join_through_a_subquery_is_checked_against_the_columns_it_passes_through(shop: Checker) -> None:
    sql = (
        'with c as (select customer_id as id, name from raw.customers) '
        'select o.order_id, c.name from raw.orders o join c on o.order_id = c.id'
    )
    assert 'join-off-relationship' in rules(shop.check(sql))


# Names and statements


def test_unknown_relations_and_columns_suggest_close_names(shop: Checker) -> None:
    result = shop.check('select amount_total from raw.orders; select * from raw.orderz')
    assert only(result, 'unknown-column').message == 'orders has no column amount_total; did you mean amount?'
    assert (
        'raw.orderz is not a relation of the project; did you mean orders'
        in only(result, 'unknown-relation').message
    )
    assert result.statements == 2


def test_sql_that_does_not_parse_is_an_error(shop: Checker) -> None:
    result = shop.check('select sum(amount from raw.orders')
    assert only(result, 'unparsed').severity == 'error' and result.statements == 0


def test_statements_that_are_not_queries_are_skipped_and_create_as_select_is_checked(shop: Checker) -> None:
    result = shop.check('create schema tmp; create table tmp.t as ' + NET_REVENUE)
    assert result.statements == 1 and rules(result) == ['missing-filter']
    assert result.corrected is not None and result.corrected.startswith('CREATE SCHEMA tmp;')


def test_the_result_renders_as_text_markdown_and_json(shop: Checker) -> None:
    result = shop.check(NET_REVENUE)
    text = str(result)
    assert text.startswith('1 error, 0 warnings, 0 notes in 1 statement.')
    assert '\nerror    missing-filter  net_revenue computes ' in text and '\nCorrected SQL\n  SELECT' in text
    assert '\n         fix       Apply the filter: ' in text
    markdown = result.markdown()
    assert '| error | `missing-filter` |' in markdown and '```sql' in markdown
    data = json.loads(json.dumps(result.to_dict()))
    assert data['passed'] is False and data['findings'][0]['rule'] == 'missing-filter'
    assert set(RULES) >= {f['rule'] for f in data['findings']}


def test_every_finding_has_the_same_keys_in_json(shop: Checker) -> None:
    keys = {'rule', 'severity', 'message', 'fix', 'column', 'term', 'detail', 'rewrite', 'evidence'}
    assert set(Finding('fan-out', 'warning', 'm', 'f').to_dict()) == keys
    sql = NET_REVENUE + '; select o.order_id from raw.orders o join raw.payments p on o.status = p.method'
    findings = shop.check(sql).to_dict()['findings']
    assert len(findings) == 2 and all(set(f) == keys for f in findings)


def test_check_sql_builds_its_own_checker(shop: Checker) -> None:
    assert rules(check_sql(shop.graph, NET_REVENUE)) == ['missing-filter']


def test_the_module_uses_only_public_names_of_other_modules() -> None:
    import konstruct.analysis.checksql as module

    tree = ast.parse(Path(module.__file__).read_text())
    imported = [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and (node.module or '').startswith('konstruct')
        for alias in node.names
    ]
    assert imported and [name for name in imported if name.startswith('_')] == []


# Without definitions.yml


def test_without_definitions_a_filter_the_existing_column_applies_is_still_required(bare: Checker) -> None:
    result = bare.check(NET_REVENUE)
    finding = only(result, 'missing-filter')
    assert finding.severity == 'warning' and finding.term is None
    assert finding.message == (
        "net_revenue computes 'net_revenue' as customer_revenue.net_revenue does, but without its filter "
        "orders.status <> 'returned'"
    )
    assert finding.evidence == ['WHERE in customer_revenue (mart/customer_revenue.sql)']
    assert finding.rewrite == "o.status <> 'returned' applied to SUM(o.amount) alone"


def test_without_definitions_another_formula_for_a_named_idea_is_a_note(bare: Checker) -> None:
    sql = NET_REVENUE.replace('sum(o.amount)', 'sum(o.amount) * 2').replace(
        'group by', "where o.status <> 'returned'\ngroup by"
    )
    assert only(bare.check(sql), 'formula-differs').severity == 'info'


def test_without_definitions_joins_use_the_derived_keys(bare: Checker) -> None:
    sql = (
        'select sum(o.amount) from raw.orders o join raw.payments p on p.order_id = o.order_id; '
        'select o.order_id from raw.orders o join raw.customers c on o.order_id = c.customer_id'
    )
    assert rules(bare.check(sql)) == ['fan-out', 'join-off-relationship']


STRIPE = """
create schema stripe;
create table stripe.charge (
    id varchar, amount bigint, amount_refunded bigint, captured boolean, paid boolean,
    currency varchar, livemode boolean, customer_id varchar
);
create table stripe.customer (id varchar, email varchar, delinquent boolean, livemode boolean);
create table stripe.invoice (id varchar, customer_id varchar, amount_due bigint);
"""


def test_an_ontology_pack_gives_keys_and_terms_to_a_project_without_definitions(tmp_path: Path) -> None:
    con = duckdb.connect(str(tmp_path / 'warehouse.duckdb'))
    con.execute(STRIPE)
    con.close()
    graph = Project(tmp_path).build(run_dbt=False).graph
    result = check_sql(
        graph,
        'select u.email, sum(c.amount) as charged from stripe.customer u '
        'join stripe.charge c on c.customer_id = u.id '
        'join stripe.invoice i on i.customer_id = u.id group by 1',
    )
    finding = only(result, 'fan-out')
    assert finding.evidence == [
        'invoice.customer_id refers to stripe_customer (invoice.customer_id = customer.id), as the stripe '
        'pack gives it, so many invoice rows can match one charge row'
    ]
    # The query reads the customer's email, which the pack marks personal.
    assert result.terms == ['Stripe charge amount', 'Stripe customer contact details']


# The example project


def test_the_example_project(jaffle_built_graph: Path) -> None:
    checker = Checker(Project(jaffle_built_graph).graph)
    result = checker.check(
        'select o.customer_id, sum(p.amount * 100) as lifetime_value from stg_payments p '
        'join stg_orders o on o.order_id = p.order_id group by 1'
    )
    finding = only(result, 'formula-differs')
    assert finding.term == 'Customer lifetime value' and finding.severity == 'warning'
    assert 'SUM(raw_payments.amount / 100)' in finding.message
    completed = checker.check('select count(*) as completed_orders from orders')
    assert (
        only(completed, 'missing-filter').rewrite == "orders.status = 'completed' added to the WHERE clause"
    )
    fanned = checker.check(
        'select c.customer_id, sum(c.customer_lifetime_value) as ltv from customers c '
        'join orders o on o.customer_id = c.customer_id group by 1'
    )
    assert only(fanned, 'fan-out').evidence[0].startswith('orders.customer_id refers to customer')
    assert checker.check('select customer_id, customer_lifetime_value from customers').findings == []
    # A FILTER inside the aggregate is the CASE the project's column applies.
    by_card = (
        "select sum(amount) filter (where payment_method = 'credit_card') as credit_card_amount "
        'from stg_payments'
    )
    assert rules(checker.check(by_card)) == ['recomputes-measure']


# Query history, from rows recorded in each warehouse's shape


FIXTURES = Path(__file__).parent / 'fixtures' / 'history'


def recorded(name: str) -> list[Any]:
    rows: list[Any] = json.loads((FIXTURES / f'{name}.json').read_text())['rows']
    return rows


def _stub(monkeypatch: pytest.MonkeyPatch, module_name: str, rows: list[Any]) -> list[str]:
    log: list[str] = []

    class Cursor:
        def execute(self, sql: str) -> None:
            log.append(sql)

        def fetchall(self) -> list[tuple[Any, ...]]:
            return [tuple(r) for r in rows]

    connection = types.SimpleNamespace(cursor=Cursor, close=lambda: None)
    module = types.ModuleType(module_name)
    module.connect = lambda **kwargs: connection  # type: ignore[attr-defined]
    package = types.ModuleType(module_name.split('.')[0])
    setattr(package, module_name.split('.')[1], module)
    monkeypatch.setitem(sys.modules, module_name.split('.')[0], package)
    monkeypatch.setitem(sys.modules, module_name, module)
    return log


def test_history_from_snowflake_counts_shadow_metrics_by_queries_runs_and_people(
    shop: Checker, monkeypatch: pytest.MonkeyPatch
) -> None:
    from konstruct.inputs.warehouse import SnowflakeConnector

    _stub(monkeypatch, 'snowflake.connector', recorded('snowflake'))
    history = fetch_history(SnowflakeConnector({'account': 'x'}), UsageSettings())
    result = check_history(shop.graph, history)
    assert (result.queries, result.runs, result.unparsed, result.redacted) == (5, 959, 0, 0)
    missing, extra = result.shadow
    assert (missing.term, missing.status, missing.rule) == ('Net revenue', 'approved', 'missing-filter')
    assert missing.detail == "without orders.status <> 'returned'"
    assert (missing.queries, missing.runs, missing.users, missing.people) == (2, 17, 2, ['ANA', 'BEN'])
    assert missing.example.startswith('select c.region, sum(o.amount) as net_revenue')
    assert (extra.rule, extra.detail) == ('formula-differs', 'with orders.customer_id <> 1042')
    adopted = next(a for a in result.adoption if a.term == 'Net revenue')
    assert (adopted.queries, adopted.runs, adopted.users) == (1, 40, 1)
    text = str(result)
    assert text.startswith('5 queries checked (959 runs); 2 shadow metrics, 2 of an approved measure.')
    assert "2 queries by 2 people compute 'Net revenue' (approved) without" in text
    assert "1 query by 1 person computes 'Net revenue' (approved) with orders.customer_id <> 1042" in text
    assert '\n    people  ANA, BEN\n' in text and '\nRead through their own columns (2)\n' in text
    data = json.loads(json.dumps(result.to_dict()))
    assert data['shadow'][0]['queries'] == 2 and adopted.to_dict() in data['adoption']


def test_history_from_databricks_counts_redacted_texts_apart(
    shop: Checker, monkeypatch: pytest.MonkeyPatch
) -> None:
    from konstruct.inputs.warehouse import DatabricksConnector

    _stub(monkeypatch, 'databricks.sql', recorded('databricks'))
    history = fetch_history(DatabricksConnector({'server_hostname': 'x'}), UsageSettings())
    result = check_history(shop.graph, history)
    assert (result.queries, result.redacted) == (2, 15)
    assert [s.users for s in result.shadow] == [1]
    assert 'databricks_pii_access' in str(result) and result.to_dict()['redacted'] == 15


def test_history_from_bigquery(shop: Checker) -> None:
    from konstruct.inputs.warehouse import BigQueryConnector

    rows = recorded('bigquery')

    class Job:
        def result(self) -> list[dict[str, Any]]:
            return rows

    connector = BigQueryConnector('acme', location='EU')
    connector.client = lambda: types.SimpleNamespace(query=lambda sql: Job())  # type: ignore[method-assign]
    result = check_history(shop.graph, fetch_history(connector, UsageSettings()))
    assert result.queries == 2
    [shadow] = result.shadow
    assert (shadow.rule, shadow.users) == ('missing-filter', 1)


def test_history_can_pseudonymise_people_and_limit_the_queries(shop: Checker) -> None:
    sql = (
        'select sum(o.amount) as net_revenue from raw.orders o '
        "where o.status <> 'returned' and o.customer_id <> 1042"
    )
    history = QueryHistory([HistoryQuery(sql, 'ann@example.com', 3), HistoryQuery('select 1', 'bob', 1)])
    result = check_history(shop.graph, history, pseudonymise=True, limit=1)
    assert result.queries == 1
    [shadow] = result.shadow
    assert (shadow.users, shadow.people) == (1, [])
    assert '1042' not in shadow.example and 'returned' not in shadow.example and '<> ?' in shadow.example
    assert 'ann' not in json.dumps(result.to_dict())


def test_the_project_checks_sql_and_the_history_of_its_warehouses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from konstruct import QueryError

    make_shop(tmp_path)
    project = Project(tmp_path)
    assert project.check_sql(NET_REVENUE).to_dict() == check_sql(project.graph, NET_REVENUE).to_dict()
    with pytest.raises(QueryError, match='no Snowflake, BigQuery or Databricks warehouse is configured'):
        project.check_history()
    (tmp_path / 'konstruct.toml').write_text(
        '[[warehouse]]\ntype = "snowflake"\naccount = "x"\n\n[usage]\ndays = 14\n'
    )
    log = _stub(monkeypatch, 'snowflake.connector', recorded('snowflake'))
    # The project's own connection is used only once it is trusted.
    with pytest.raises(QueryError, match=r'cannot read query history: .* is not trusted'):
        project.check_history()
    monkeypatch.setenv('KONSTRUCT_TRUST_CONFIG', '1')
    result = project.check_history(limit=3, pseudonymise=True)
    assert 'dateadd(day, -14,' in log[-1] and 'limit 3' in log[-1]
    assert result.queries == 3 and [s.people for s in result.shadow] == [[]]
    assert project.check_history(days=7).shadow[0].people == ['ANA', 'BEN']
    assert 'dateadd(day, -7,' in log[-1]
    with pytest.raises(QueryError, match='at least 1 query'):
        project.check_history(limit=0)
    # A window of no days, or one in the future, is refused, not read as the default.
    for days in (0, -3):
        with pytest.raises(QueryError, match='at least 1 day'):
            project.check_history(days=days)


def test_the_project_reports_a_warehouse_that_refuses_its_history_and_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from konstruct import QueryError

    make_shop(tmp_path)
    project = Project(tmp_path)
    (tmp_path / 'konstruct.toml').write_text('[[warehouse]]\ntype = "snowflake"\naccount = "x"\n')
    monkeypatch.setenv('KONSTRUCT_TRUST_CONFIG', '1')

    class Error(Exception):
        """The driver's DB-API base error."""

    def connect(failure: BaseException) -> Callable[..., Any]:
        def refused(**options: Any) -> Any:
            raise failure

        return refused

    module = types.ModuleType('snowflake.connector')
    module.Error = Error  # type: ignore[attr-defined]
    package = types.ModuleType('snowflake')
    package.connector = module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, 'snowflake', package)
    monkeypatch.setitem(sys.modules, 'snowflake.connector', module)
    module.connect = connect(Error('250001: could not connect'))  # type: ignore[attr-defined]
    with pytest.raises(
        QueryError, match='cannot read query history: Snowflake refused the query history: 250001'
    ):
        project.check_history()
    # Any other error is a bug, and is not passed off as the warehouse's answer.
    module.connect = connect(KeyError('account'))  # type: ignore[attr-defined]
    with pytest.raises(KeyError):
        project.check_history()
    monkeypatch.delitem(sys.modules, 'snowflake.connector')
    monkeypatch.setitem(sys.modules, 'snowflake', None)
    with pytest.raises(QueryError, match='cannot read query history: install the snowflake extra'):
        project.check_history()


def test_a_databricks_connection_that_never_reaches_the_warehouse_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from urllib3.exceptions import MaxRetryError

    from konstruct import QueryError

    make_shop(tmp_path)
    project = Project(tmp_path)
    (tmp_path / 'konstruct.toml').write_text('[[warehouse]]\ntype = "databricks"\nserver_hostname = "x"\n')
    monkeypatch.setenv('KONSTRUCT_TRUST_CONFIG', '1')
    module = types.ModuleType('databricks.sql')
    package = types.ModuleType('databricks')
    package.sql = module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, 'databricks', package)
    monkeypatch.setitem(sys.modules, 'databricks.sql', module)
    # The driver has no DB-API Error here, so only the transport's own errors are reported.
    for failure in (
        MaxRetryError(None, '/sql/1.0'),  # type: ignore[arg-type]
        ConnectionRefusedError(111, 'refused'),
    ):

        def refused(failure: BaseException = failure, **options: Any) -> Any:
            raise failure

        module.connect = refused  # type: ignore[attr-defined]
        with pytest.raises(
            QueryError, match='cannot read query history: Databricks refused the query history'
        ):
            project.check_history()


def test_bigquery_credentials_that_cannot_be_read_are_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from konstruct import QueryError
    from konstruct.interfaces.cli import main

    pytest.importorskip('google.cloud.bigquery')
    make_shop(tmp_path)
    (tmp_path / 'konstruct.toml').write_text('[[warehouse]]\ntype = "bigquery"\nproject = "acme"\n')
    monkeypatch.setenv('KONSTRUCT_TRUST_CONFIG', '1')
    monkeypatch.setenv('GOOGLE_APPLICATION_CREDENTIALS', '/nonexistent/creds.json')
    with pytest.raises(QueryError, match='cannot read query history: BigQuery refused the query history'):
        Project(tmp_path).check_history()
    assert main(['--no-color', '-q', '-C', str(tmp_path), 'check-sql', '--history']) == 1
    assert 'BigQuery refused the query history' in capsys.readouterr().err


def test_pseudonyms_cannot_be_matched_across_reports() -> None:
    import konstruct.analysis.checksql as module

    first, second = (pseudonym('ann@example.com', key) for key in (b'a' * 32, b'b' * 32))
    assert first != second and first == pseudonym('ann@example.com', b'a' * 32)
    assert len(first.removeprefix('user-')) >= 12
    # Without a key, each check makes its own.
    keys: list[bytes] = []
    real = module.pseudonym

    def spy(user: str, key: bytes) -> str:
        keys.append(key)
        return real(user, key)

    history = QueryHistory([HistoryQuery('select 1', 'ann', 1)])
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(module, 'pseudonym', spy)
        check_history(Graph(), history, pseudonymise=True)
        check_history(Graph(), history, pseudonymise=True)
    assert len(keys) == 2 and keys[0] != keys[1]


def test_counts_read_as_words() -> None:
    assert Adoption('Net revenue', 1, 1, 1).to_dict() == {
        'term': 'Net revenue',
        'queries': 1,
        'runs': 1,
        'users': 1,
    }
    one = HistoryCheck(queries=1, runs=1).summary()
    assert one == '1 query checked (1 run); 0 shadow metrics, 0 of an approved measure.'
    assert HistoryCheck(queries=2, runs=3).summary().startswith('2 queries checked (3 runs)')


# The grain of a join, against a brute-force join of random tables


@settings(deadline=None)  # the brute-force join of the first example can take far longer than the rest
@given(st.data())
def test_a_source_said_to_keep_its_grain_never_has_a_row_repeated_within_a_group(data: st.DataObject) -> None:
    count = data.draw(st.integers(2, 4))
    aliases = [f't{i}' for i in range(count)]
    columns = st.sampled_from(['k', 'a', 'b'])
    equalities = data.draw(
        st.lists(
            # Two distinct sources, drawn directly rather than filtered, which Hypothesis may reject.
            st.tuples(st.permutations(aliases), columns, columns).map(
                lambda e: (e[0][0], e[1], e[0][1], e[2])
            ),
            max_size=5,
        )
    )
    grouped = data.draw(st.lists(st.tuples(st.sampled_from(aliases), columns), max_size=3))
    fixed: dict[str, set[str]] = {}
    for alias, column in grouped:
        fixed.setdefault(alias, set()).add(column)
    single = {a for a in aliases if data.draw(st.booleans())}
    rows = {
        a: [
            {'k': i, 'a': data.draw(st.integers(0, 2)), 'b': data.draw(st.integers(0, 2))}
            for i in range(data.draw(st.integers(0, 1 if a in single else 3)))
        ]
        for a in aliases
    }
    # Every k is unique; a source known to hold one row has the empty set as a unique set.
    unique = {a: [frozenset({'k'}), *([frozenset()] if a in single else [])] for a in aliases}
    reach = reachable(aliases, equalities, unique, fixed)
    position = {a: i for i, a in enumerate(aliases)}
    joined = [
        combination
        for combination in itertools.product(*(rows[a] for a in aliases))
        if all(combination[position[x]][xc] == combination[position[y]][yc] for x, xc, y, yc in equalities)
    ]
    for alias in aliases:
        if reach[alias] == set(aliases):
            groups: dict[tuple[Any, ...], list[int]] = {}
            for combination in joined:
                group = tuple(combination[position[a]][c] for a, c in grouped)
                groups.setdefault(group, []).append(combination[position[alias]]['k'])
            for keys in groups.values():
                assert len(keys) == len(set(keys))


def test_a_chain_of_many_to_one_joins_keeps_the_grain_of_its_first_source() -> None:
    unique = {
        'p': [frozenset({'payment_id'})],
        'o': [frozenset({'order_id'})],
        'c': [frozenset({'customer_id'})],
    }
    reach = reachable(
        ['p', 'o', 'c'],
        [('p', 'order_id', 'o', 'order_id'), ('o', 'customer_id', 'c', 'customer_id')],
        unique,
    )
    assert reach == {'p': {'p', 'o', 'c'}, 'o': {'o', 'c'}, 'c': {'c'}}
    grouped = reachable(['p', 'o'], [('p', 'order_id', 'o', 'order_id')], unique, {'p': {'payment_id'}})
    assert grouped['o'] == {'o', 'p'}


# Scale


@pytest.mark.scale
def test_checking_a_query_on_a_large_project_is_fast(tmp_path: Path) -> None:
    from tests.test_scale import make_project

    make_project(tmp_path, models=300)
    graph = Project(tmp_path).build(run_dbt=False).graph
    started = cpu_seconds()
    checker = Checker(graph)
    ready = cpu_seconds() - started
    sql = """
        select a.c00, sum(a.c01) as c01, sum(b.c02 * 2) as c04
        from wh.analytics.m299 a join wh.analytics.m150 b on a.c00 = b.c00
        where a.c03 > 0
        group by 1
    """
    started = cpu_seconds()
    for _ in range(20):
        checker.check(sql)
    per_query = (cpu_seconds() - started) / 20
    assert ready < 20 and per_query < 2, (ready, per_query)


def test_a_key_a_definition_lists_without_a_test_does_not_hide_a_fan_out(tmp_path: Path) -> None:
    """A term of kind entity binds payments.order_id as well as orders.order_id. That makes it a key
    of order, but no test or constraint makes it unique: many payments still match one order, so a
    sum of the order's amount over the join is still a fan-out."""
    definitions = (
        'version: 1\nterms:\n  - name: Order\n    kind: entity\n'
        '    columns: [orders.order_id, payments.order_id]\n'
    )
    checker = Checker(make_shop(tmp_path, definitions=definitions))
    order = next(e for e in checker.ontology.entities if e.name == 'order')
    assert any(i.endswith('payments#order_id') for i in order.key_ids)
    sql = 'select sum(o.amount) as total from raw.orders o join raw.payments p on p.order_id = o.order_id'
    assert only(checker.check(sql), 'fan-out').column == 'total'


@pytest.fixture(scope='module')
def stripe(tmp_path_factory: pytest.TempPathFactory) -> Graph:
    root = tmp_path_factory.mktemp('stripe')
    con = duckdb.connect(str(root / 'warehouse.duckdb'))
    con.execute(STRIPE)
    con.close()
    return Project(root).build(run_dbt=False).graph


def test_a_finding_says_when_it_rests_on_a_key_a_pack_presumes(stripe: Graph) -> None:
    """Stripe's ids are unique in Stripe, which the pack presumes and nothing in the project tests:
    a finding that rests on one says so."""
    sql = 'select u.email, sum(i.amount_due) from stripe.invoice i join stripe.customer u on u.email = i.id group by 1'
    assert only(check_sql(stripe, sql), 'fan-out').evidence == [
        'customer has one row per id (id presumed unique by the stripe pack), not per email, so many of its '
        'rows can match one invoice row'
    ]
    keyed = 'select sum(c.amount) from stripe.charge c join (select email from stripe.customer) x on x.email = c.id'
    assert only(check_sql(stripe, keyed), 'fan-out').evidence == [
        'charge is joined on its key (id presumed unique by the stripe pack), and nothing says the subquery '
        'x has one row per email'
    ]


def test_a_join_a_presumed_key_makes_safe_is_noted(stripe: Graph) -> None:
    result = check_sql(
        stripe, 'select sum(c.amount) from stripe.charge c join stripe.customer u on u.id = c.customer_id'
    )
    assert rules(result) == ['presumed-key']
    [note] = result.findings
    assert note.severity == 'info'
    assert note.message == (
        'the join u.id = c.customer_id is taken not to repeat rows because the stripe ontology pack '
        'presumes customer.id unique; nothing has tested it'
    )


def test_an_average_grouped_by_a_key_only_a_pack_presumes_is_still_a_fan_out(stripe: Graph) -> None:
    """Nothing in the project shows a charge id unique, or never NULL: grouped by it, a group may
    hold several charges, whose copies an average weighs by how often the join repeats each."""
    sql = (
        'select c.id, avg(c.amount) from stripe.charge c '
        'join stripe.invoice i on i.customer_id = c.customer_id group by c.id'
    )
    assert only(check_sql(stripe, sql), 'fan-out').severity == 'warning'


def test_a_relationship_only_a_pack_gives_is_labelled_so(stripe: Graph) -> None:
    sql = (
        'select sum(i.amount_due) from stripe.invoice i join stripe.charge c on c.customer_id = i.customer_id'
    )
    assert only(check_sql(stripe, sql), 'fan-out').evidence == [
        'charge.customer_id refers to stripe_customer (charge.customer_id = customer.id), as the stripe pack '
        'gives it, so many charge rows can match one invoice row'
    ]


def test_columns_named_keeps_each_name_together_in_the_order_names_are_first_computed() -> None:
    """Only the columns of a matching name are asked for their edges; the columns found are as
    before: each name's computed columns together, the names in the order their first computed
    column comes in the graph, and none when more than twenty share the name."""
    from konstruct.core.graph import Node

    graph = Graph()
    graph.meta['dialect'] = 'duckdb'
    source = graph.add_node(Node(id='source.raw', kind='source', name='raw', relation='raw'))
    graph.add_column(source, 'amount')
    for index, (name, transform) in enumerate(
        [
            ('revenue', 'direct'),
            ('revenues', 'aggregate'),
            ('revenue', 'aggregate'),
            ('revenues', 'aggregate'),
        ]
    ):
        model = graph.add_node(
            Node(id=f'model.t{index}', kind='model', name=f't{index}', relation=f't{index}')
        )
        column = graph.add_column(model, name, attrs={'expression': 'SUM(raw.amount)'})
        graph.add_edge(column.id, 'source.raw#amount', 'derived_from', transform=transform)
    for index in range(4, 25):
        model = graph.add_node(
            Node(id=f'model.t{index}', kind='model', name=f't{index}', relation=f't{index}')
        )
        column = graph.add_column(model, 'cost', attrs={'expression': 'SUM(raw.amount)'})
        graph.add_edge(column.id, 'source.raw#amount', 'derived_from', transform='aggregate')
    checker = Checker(graph)
    assert [n.id for n in checker.columns_named('revenues')] == [
        'model.t1#revenues',
        'model.t3#revenues',
        'model.t2#revenue',
    ]
    assert checker.columns_named('cost') == []


# Which filters hold on a column, against the data


_RETURNED = "where status <> 'returned'"


def _trap_models() -> dict[str, str]:
    """Small models of the shapes that decide whether a filter holds on a column's rows, each in
    every placement of the filter: an aggregate, a COALESCE and a sum of two CTEs, the branches of
    a UNION ALL in a subquery, a window under a filter, a filter above an aggregate, a column copied
    or summed from upstream models, a relation two statements write, a filter on one of two readings
    of a table or a CTE, a filter on a window an upstream table or view computes, a filter on a
    column copied from another reading of the table (in a CTE, a subquery or an upstream model, and
    the last order's status), a filter in WHERE on another alias, and one on the side of a LEFT JOIN
    that keeps every row."""
    models: dict[str, str] = {}
    places = ['', _RETURNED]

    def agg(where: str, extra: str = '') -> str:
        return f'select customer_id{extra}, sum(amount) as v from raw.orders {where} group by all'

    for i, w in enumerate(places):
        models[f'agg_{i}'] = f'create table mart.agg_{i} as {agg(w)}'
    for i, (wa, wb) in enumerate(itertools.product(places, places)):
        ctes = f'with a as ({agg(wa)}), b as ({agg(wb)})'
        joins = 'from raw.customers c left join a on a.customer_id = c.customer_id left join b on b.customer_id = c.customer_id'
        models[f'either_{i}'] = (
            f'create table mart.either_{i} as {ctes} select c.customer_id, coalesce(a.v, b.v) as v {joins}'
        )
        models[f'plus_{i}'] = (
            f'create table mart.plus_{i} as {ctes} select c.customer_id, coalesce(a.v, 0) + coalesce(b.v, 0) as v {joins}'
        )
        models[f'branches_{i}'] = (
            f'create table mart.branches_{i} as select customer_id, sum(amount) as v from ('
            f'select customer_id, amount from raw.orders {wa} union all '
            f'select customer_id, amount from raw.orders {wb}) u group by customer_id'
        )
        models[f'window_{i}'] = (
            f'create table mart.window_{i} as with w as (select customer_id, status, sum(amount) over '
            f'(partition by customer_id) as v from raw.orders {wa}) select distinct customer_id, v from w {wb}'
        )
        models[f'above_{i}'] = (
            f'create table mart.above_{i} as with a as ({agg(wa, ", status")}) '
            f'select customer_id, sum(v) as v from a {wb} group by customer_id'
        )
        models[f'twice_{i}'] = (
            f'create table mart.twice_{i} as {agg(wa)};\ninsert into mart.twice_{i} {agg(wb)}'
        )
        models[f'joined_{i}'] = (
            f'create table mart.joined_{i} as select c.customer_id, coalesce(x.v, 0) + coalesce(y.v, 0) as v '
            f'from raw.customers c left join mart.agg_{i // 2} x on x.customer_id = c.customer_id '
            f'left join mart.agg_{i % 2} y on y.customer_id = c.customer_id'
        )
    for i in range(2):
        models[f'copy_{i}'] = f'create table mart.copy_{i} as select customer_id, v from mart.agg_{i}'
    on_o2 = ['', "and o2.status <> 'returned'"]
    for i, (read, on) in enumerate(itertools.product(['o', 'o2'], on_o2)):
        models[f'selfjoin_{i}'] = (
            f'create table mart.selfjoin_{i} as select o.customer_id, {read}.amount as v from raw.orders o '
            f'left join raw.orders o2 on o2.order_id = o.order_id {on}'
        )
    on_f1 = ['', "and f1.status <> 'returned'"]
    for i, (read, first, on) in enumerate(itertools.product(['f1', 'f2'], ['f', '(select * from f)'], on_f1)):
        models[f'reread_{i}'] = (
            f'create table mart.reread_{i} as with f as (select * from raw.orders) '
            f'select f2.customer_id, {read}.amount as v from {first} f2 '
            f'left join f f1 on f1.order_id = f2.order_id {on}'
        )
    for i, (kind, w) in enumerate(itertools.product(['table', 'view'], places)):
        models[f'windowed_{i}'] = (
            f'create {kind} mart.windowed_{i} as select order_id, customer_id, status, '
            f'sum(amount) over (partition by customer_id) as v from raw.orders {w}'
        )
    for i, (upstream, w) in enumerate(itertools.product(range(4), places)):
        models[f'after_{i}'] = (
            f'create table mart.after_{i} as select customer_id, v from mart.windowed_{upstream} {w}'
        )
    for i, w in enumerate(places):
        models[f'two_alias_where_{i}'] = (
            f'create table mart.two_alias_where_{i} as select o.customer_id, o.amount as v from raw.orders o '
            f'join raw.orders o2 on o2.customer_id = o.customer_id {w.replace("status", "o2.status")}'
        )
        on = "and o.status <> 'returned'" if w else ''
        models[f'preserved_on_{i}'] = (
            f'create table mart.preserved_on_{i} as select o.customer_id, o.amount as v from raw.orders o '
            f'left join raw.customers c on c.customer_id = o.customer_id {on}'
        )
    paired = (
        'select a.order_id, a.customer_id, a.amount, b.status as pst from raw.orders a '
        'left join raw.orders b on b.order_id = a.order_id - 1'
    )
    shapes = {
        'cte_on': (f'with sj as ({paired}) ', 'left join', 'sj', "and sj.pst <> 'returned'"),
        'cte_where': (f'with sj as ({paired}) ', 'join', 'sj', "where sj.pst <> 'returned'"),
        'subquery_on': ('', 'left join', f'({paired})', "and sj.pst <> 'returned'"),
        'model_where': ('', 'join', 'mart.pairs', "where sj.pst <> 'returned'"),
    }
    models['pairs'] = f'create table mart.pairs as select p.*, p.amount as v from ({paired}) p'
    for name, (ctes, join, source, w) in shapes.items():
        # Downstream of mart.pairs, the model goes after it.
        model = f'after_paired_{name}' if source == 'mart.pairs' else f'paired_{name}'
        models[model] = (
            f'create table mart.{model} as {ctes}select c.customer_id, sj.amount as v from raw.customers c '
            f'{join} {source} sj on sj.customer_id = c.customer_id {w}'
        )
    models['last_status'] = (
        'create table mart.last_status as with last as (select customer_id, max(order_id) as last_id '
        'from raw.orders group by 1), x as (select o.customer_id, o.amount, l2.status as last_status '
        'from raw.orders o join last l on l.customer_id = o.customer_id '
        'join raw.orders l2 on l2.order_id = l.last_id) '
        "select x.customer_id, x.amount as v from x where x.last_status <> 'returned'"
    )
    return models


def _totals(path: Path, models: dict[str, str], *, keep_returned: bool) -> dict[str, Any]:
    con = duckdb.connect(str(path))
    try:
        con.execute(WAREHOUSE)
        shop_rows(con)
        if not keep_returned:
            con.execute("delete from raw.orders where status = 'returned'")
        con.execute('create schema mart')
        order = sorted(models, key=lambda m: m.startswith(('joined', 'copy', 'after')))
        for name in order:
            con.execute(models[name])
        return {name: con.execute(f'select sum(v) from mart.{name}').fetchall()[0][0] for name in models}
    finally:
        con.close()


def test_a_filter_is_held_only_where_the_data_shows_it_holds(tmp_path: Path) -> None:
    """For every trap model, a filter Konstruct says holds on its column leaves the column's total
    as the data without the filtered rows gives it, and where it does not, check-sql reports the
    filter missing from a query of the column."""
    models = _trap_models()
    terms = '\n'.join(
        f'  - name: Kept {name}\n    kind: measure\n    status: approved\n    owner: {{name: Ana Ruiz}}\n'
        f'    approved: 2026-10-01\n    columns: [{name}.v]\n    filters: ["orders.status <> \'returned\'"]'
        for name in models
    )
    root = tmp_path / 'traps'
    root.mkdir()
    make_shop(root, definitions=f'version: 1\nterms:\n{terms}\n')
    for name, sql in models.items():
        (root / 'mart' / f'{name}.sql').write_text(sql)
    checker = Checker(Project(root).build(run_dbt=False).graph)
    full = _totals(tmp_path / 'full.duckdb', models, keep_returned=True)
    kept = _totals(tmp_path / 'kept.duckdb', models, keep_returned=False)
    # The traps where only which reading or which side a filter is on decides, do not hold.
    traps = ['two_alias_where_1', 'preserved_on_1', 'last_status', *(m for m in models if 'paired_' in m)]
    assert all(full[name] != kept[name] for name in traps), [n for n in traps if full[n] == kept[n]]
    claimed = []
    for name in models:
        column = checker.graph.resolve(f'{name}.v')[0]
        holds = full[name] == kept[name]
        if checker.applied_on_route(f'Kept {name}', column):
            claimed.append(name)
            assert holds, f'{name}: the filter is taken as held, but the data says it is not'
        if not holds:
            found = checker.check(f'select sum(v) as kept_{name} from mart.{name}')
            assert 'missing-filter' in {f.rule for f in found.findings}, name
    # The filter is shown held where it plainly is.
    assert {
        'agg_1',
        'either_3',
        'plus_3',
        'branches_3',
        'window_2',
        'copy_1',
        'twice_3',
        'joined_3',
        'selfjoin_3',
        'reread_1',
        'reread_3',
        'windowed_1',
        'after_2',
        'after_3',
    } <= set(claimed)
