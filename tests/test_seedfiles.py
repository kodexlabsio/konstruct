"""The seed loader: a project's seed files read without following anything out of the project, and
never handed to DuckDB as a path."""

from __future__ import annotations

import os
from pathlib import Path

import duckdb
import pytest

from konstruct.analysis import seedfiles
from konstruct.analysis.conflict_data import SeedEngine
from konstruct.analysis.seedfiles import SeedFile, load_seeds, read_seed, seed_files
from konstruct.core.graph import Graph, Node

SECRET = 'id,v\n1,STOLEN-TOKEN\n'
links = pytest.mark.skipif(not hasattr(os, 'symlink') or os.name == 'nt', reason='needs symbolic links')


def graph_of(*seeds: tuple[str, str]) -> Graph:
    graph = Graph()
    for name, path in seeds:
        graph.add_node(Node(f'seed.p.{name}', 'seed', name, relation=f'db.main.{name}', path=path))
    return graph


def rows(
    root: Path, seeds: dict[str, SeedFile]
) -> tuple[dict[str, list[tuple[object, ...]]], dict[str, str]]:
    con = duckdb.connect()
    tables, skipped = load_seeds(con, root, seeds)
    found = {
        key: con.execute(f'SELECT * FROM {table} ORDER BY 1').fetchall() for key, table in tables.items()
    }
    con.close()
    return found, skipped


@pytest.mark.parametrize('folder', ['[s]eeds', 's*eds', 'se?ds', 'seeds{a,b}'])
def test_a_path_with_pattern_characters_is_read_as_it_is_written(tmp_path: Path, folder: str) -> None:
    """DuckDB reads ``[s]eeds/x.csv`` as a pattern matching ``seeds/x.csv``; the loader never gives
    DuckDB the path, so the file read is the one named."""
    (tmp_path / folder).mkdir()
    (tmp_path / folder / 'customers.csv').write_text('id,v\n1,decoy\n')
    (tmp_path / 'seeds').mkdir()
    (tmp_path / 'seeds' / 'customers.csv').write_text(SECRET)
    found = seed_files(graph_of(('customers', f'{folder}/customers.csv')), tmp_path)
    assert found == {'db.main.customers': SeedFile('db.main.customers', f'{folder}/customers.csv')}
    read, skipped = rows(tmp_path, found)
    assert read == {'db.main.customers': [(1, 'decoy')]} and skipped == {}


@links
def test_the_reviewers_probe_reads_no_file_outside_the_project(tmp_path: Path) -> None:
    """A decoy ``[s]eeds/customers.csv`` and ``seeds/customers.csv`` linked outside: the named
    file is the decoy, and the link is never read whatever the pattern would match."""
    root = tmp_path / 'p3'
    (root / '[s]eeds').mkdir(parents=True)
    (root / '[s]eeds' / 'customers.csv').write_text('id,v\n1,decoy\n')
    (root / 'seeds').mkdir()
    outside = tmp_path / 'secret_outside.csv'
    outside.write_text(SECRET)
    (root / 'seeds' / 'customers.csv').symlink_to(outside)
    graph = graph_of(('customers', '[s]eeds/customers.csv'), ('linked', 'seeds/customers.csv'))
    found = seed_files(graph, root)
    assert set(found) == {'db.main.customers'}
    read, _ = rows(root, {**found, 'db.main.linked': SeedFile('db.main.linked', 'seeds/customers.csv')})
    assert 'STOLEN-TOKEN' not in repr(read)
    assert read['db.main.customers'] == [(1, 'decoy')]


@links
def test_a_link_inside_a_seed_folder_is_refused(tmp_path: Path) -> None:
    (tmp_path / 'seeds').mkdir()
    outside = tmp_path.parent / f'{tmp_path.name}-outside.csv'
    outside.write_text(SECRET)
    (tmp_path / 'seeds' / 'x.csv').symlink_to(outside)
    assert seed_files(graph_of(('x', 'seeds/x.csv')), tmp_path) == {}
    with pytest.raises(OSError):
        read_seed(tmp_path, 'seeds/x.csv')
    _, skipped = rows(tmp_path, {'db.main.x': SeedFile('db.main.x', 'seeds/x.csv')})
    assert set(skipped) == {'db.main.x'}


@links
def test_a_seed_linked_to_the_git_config_is_refused(tmp_path: Path) -> None:
    """``seeds/x.csv -> ../.git/config`` stays inside the repository, but is a link into ``.git``."""
    (tmp_path / '.git').mkdir()
    (tmp_path / '.git' / 'config').write_text('url = https://x-access-token:SECRET@example.com/r.git\n')
    (tmp_path / 'seeds').mkdir()
    (tmp_path / 'seeds' / 'x.csv').symlink_to('../.git/config')
    assert seed_files(graph_of(('x', 'seeds/x.csv'), ('g', '.git/config.csv')), tmp_path) == {}
    with pytest.raises(OSError):
        read_seed(tmp_path, 'seeds/x.csv')
    with pytest.raises(OSError):
        read_seed(tmp_path, '.git/config')


@links
def test_a_linked_folder_on_the_way_is_refused(tmp_path: Path) -> None:
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    (elsewhere / 'x.csv').write_text(SECRET)
    root = tmp_path / 'root'
    root.mkdir()
    (root / 'seeds').symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(OSError):
        read_seed(root, 'seeds/x.csv')


@pytest.mark.parametrize('path', ['/etc/passwd', '../x.csv', 'seeds/../../x.csv', 'a\\b.csv', '', 'C:/x.csv'])
def test_paths_that_leave_the_project_are_refused(tmp_path: Path, path: str) -> None:
    with pytest.raises(OSError):
        read_seed(tmp_path, path)


def test_a_file_too_large_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / 'big.csv').write_text('a\n' + '1\n' * 100)
    monkeypatch.setattr(seedfiles, 'MAX_BYTES', 50)
    with pytest.raises(OSError, match='larger'):
        read_seed(tmp_path, 'big.csv')


def test_the_delimiter_and_column_types_of_a_seed_are_honoured(tmp_path: Path) -> None:
    (tmp_path / 'codes.csv').write_text('code;amount\n007;1.5\n')
    graph = Graph()
    graph.add_node(
        Node(
            'seed.p.codes',
            'seed',
            'codes',
            relation='db.main.codes',
            path='codes.csv',
            attrs={
                'delimiter': ';',
                'column_types': {'code': 'varchar', 'amount': 'decimal(10,2)', 'x': 'a); drop'},
            },
        )
    )
    found = seed_files(graph, tmp_path)
    assert found['db.main.codes'].column_types == {'code': 'varchar', 'amount': 'decimal(10,2)'}
    read, skipped = rows(tmp_path, found)
    assert skipped == {} and [(c, str(a)) for c, a in read['db.main.codes']] == [('007', '1.50')]


def test_the_engine_reads_no_file_once_the_seeds_are_in(tmp_path: Path) -> None:
    (tmp_path / 'orders.csv').write_text('id,amount\n1,10\n')
    (tmp_path / 'other.csv').write_text('x\n1\n')
    engine = SeedEngine(None, tmp_path, {'db.main.orders': SeedFile('db.main.orders', 'orders.csv')})
    try:
        assert engine.run(f'SELECT sum(amount) FROM {engine.table("db.main.orders")}')[1] == [(10,)]
        for sql in (
            f"SELECT * FROM read_csv('{tmp_path / 'other.csv'}')",
            "SELECT * FROM read_csv('/tmp/konstruct-seeds-*/s0.csv')",
            "SET allowed_directories = ['/']",
        ):
            # A statement that is not a query never reaches DuckDB; a query DuckDB refuses.
            with pytest.raises((duckdb.Error, ValueError)):
                engine.run(sql)
    finally:
        engine.close()


@pytest.mark.dbt
def test_a_seeds_dbt_config_reaches_the_graph(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``+delimiter`` and ``+column_types`` from dbt_project.yml are recorded on the seed, and the
    loader reads the file with them."""
    from konstruct import Project

    from .conftest import dbt_executable

    dbt = dbt_executable()
    if dbt is None:
        pytest.skip('dbt is not installed')
    # The build runs the dbt on PATH, which is the test environment's own only when it is active.
    monkeypatch.setenv('PATH', f'{Path(dbt).parent}{os.pathsep}{os.environ.get("PATH", "")}')
    root = tmp_path / 'p'
    (root / 'seeds').mkdir(parents=True)
    (root / 'models').mkdir()
    (root / 'seeds' / 'codes.csv').write_text('code;amount\n007;1.5\n')
    (root / 'models' / 'm.sql').write_text("select code, amount from {{ ref('codes') }}\n")
    (root / 'dbt_project.yml').write_text(
        'name: p\nversion: "1.0"\nprofile: p\nseeds:\n  p:\n    +delimiter: ";"\n'
        '    +column_types:\n      code: varchar\n'
    )
    (root / 'profiles.yml').write_text(
        f'p:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n      path: {root / "p.duckdb"}\n'
    )
    project = Project(root)
    project.trust()
    project.build()
    [seed] = [n for n in project.graph.nodes.values() if n.kind == 'seed']
    assert seed.attrs.get('delimiter') == ';' and seed.attrs.get('column_types') == {'code': 'varchar'}
    found = seed_files(project.graph, root)
    read, skipped = rows(root, found)
    assert skipped == {} and [r[0] for r in next(iter(read.values()))] == ['007']


@pytest.mark.parametrize(
    'path', ['.git./config', '.GIT::$DATA/config', 'GIT~1/config', 'seeds/.git /config', 'seeds/... /x.csv']
)
def test_a_spelling_of_git_windows_reads_as_git_is_refused(path: str) -> None:
    from konstruct.analysis.seedfiles import _parts

    assert _parts(path) is None


@pytest.mark.skipif(not hasattr(os, 'mkfifo'), reason='no FIFOs')
def test_a_fifo_at_a_seed_path_is_never_opened(tmp_path: Path) -> None:
    """Opening a FIFO for reading waits for a writer: the path is checked before it is opened."""
    from konstruct.analysis.seedfiles import read_seed

    (tmp_path / 'seeds').mkdir()
    os.mkfifo(tmp_path / 'seeds' / 'orders.csv')
    with pytest.raises(OSError, match='not a regular file'):
        read_seed(tmp_path, 'seeds/orders.csv')


def test_a_file_with_another_hard_link_is_refused(tmp_path: Path) -> None:
    from konstruct.analysis.seedfiles import read_seed

    (tmp_path / 'seeds').mkdir()
    (tmp_path / 'secret').write_text('token\n')
    os.link(tmp_path / 'secret', tmp_path / 'seeds' / 'orders.csv')
    with pytest.raises(OSError, match='another hard link'):
        read_seed(tmp_path, 'seeds/orders.csv')


def test_a_file_swapped_between_the_check_and_the_open_is_refused(tmp_path: Path) -> None:
    from konstruct.analysis.seedfiles import _same

    (tmp_path / 'a.csv').write_text('x\n')
    (tmp_path / 'b.csv').write_text('y\n')
    before = (tmp_path / 'a.csv').stat()
    descriptor = os.open(tmp_path / 'b.csv', os.O_RDONLY)
    with pytest.raises(OSError, match='changed while it was opened'):
        _same(descriptor, before, ['b.csv'])


def test_duckdb_reaches_no_other_file_while_the_warehouse_is_attached(tmp_path: Path) -> None:
    """External access is off, with the warehouse alone allowed, before the warehouse is attached."""
    import duckdb

    from konstruct.analysis.verify import DuckDBEngine

    warehouse = tmp_path / 'w.duckdb'
    duckdb.connect(str(warehouse)).close()
    (tmp_path / 'other.csv').write_text('a\n1\n')
    engine = DuckDBEngine(warehouse)
    try:
        [(access,)] = engine.con.execute("SELECT current_setting('enable_external_access')").fetchall()
        assert access is False
        with pytest.raises(duckdb.Error):
            engine.con.execute(f"SELECT * FROM read_csv('{tmp_path / 'other.csv'}')").fetchall()
        with pytest.raises(duckdb.Error):
            engine.con.execute('SET enable_external_access = true')
    finally:
        engine.close()


@pytest.mark.parametrize('path', ['.g‌it/config', 'seeds/.GI﻿T/config', '‎.git/hooks/x.csv'])
def test_a_spelling_of_git_hfs_reads_as_git_is_refused(path: str) -> None:
    """HFS+ leaves some code points out when it compares names, so ``.g\\u200cit`` is ``.git``."""
    from konstruct.analysis.seedfiles import _parts

    assert _parts(path) is None


def test_the_seeds_together_are_capped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each seed is copied to a temporary directory, so all of them together are bounded too."""
    (tmp_path / 'seeds').mkdir()
    for name in ('a', 'b', 'c'):
        (tmp_path / 'seeds' / f'{name}.csv').write_text('x\n' + '1\n' * 10)
    monkeypatch.setattr(seedfiles, 'MAX_TOTAL_BYTES', 50)
    seeds = {f'db.main.{n}': SeedFile(f'db.main.{n}', f'seeds/{n}.csv') for n in ('a', 'b', 'c')}
    con = duckdb.connect()
    tables, skipped = load_seeds(con, tmp_path, seeds)
    assert set(tables) == {'db.main.a', 'db.main.b'}
    assert skipped == {'db.main.c': 'the seeds together are larger than 0 MB'}
