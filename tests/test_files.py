"""Files read by a literal path as relations of their own: the ``file`` nodes a build makes, their
columns, and the files of the repository it reads them from."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import duckdb
import pytest

from konstruct import Project
from konstruct._core import TrackedFiles, data_file_columns
from konstruct.analysis.checksql import Checker
from konstruct.core.graph import Graph, Node, file_key

SALES = file_key('parquet', ['data/sales/*.parquet'])


def track(root: Path, *paths: str) -> None:
    """Make ``root`` a git repository that tracks ``paths`` (all it holds by default), as the
    repository a build reads files from is."""
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    subprocess.run(['git', '-C', str(root), 'add', '-f', *(paths or ('.',))], check=True)


def build(root: Path, models: dict[str, str], tracked: bool = True) -> Graph:
    (root / 'models').mkdir(exist_ok=True)
    for name, sql in models.items():
        (root / 'models' / f'{name}.sql').write_text(sql)
    if tracked:
        track(root)
    return Project(root).build(run_dbt=False, dialect='duckdb', warehouses=False).graph


def derived(graph: Graph, column: str) -> list[str]:
    return sorted(e.target for e in graph.out_edges(column, 'derived_from'))


def test_file_key() -> None:
    assert SALES == 'file.parquet.data/sales/*%2eparquet'
    assert file_key('CSV', ['A.csv', 'b#[1],%.csv']) == 'file.csv.a%2ecsv,b%23%5b1%5d%2c%25%2ecsv'


def test_files_a_query_reads_are_a_file_node_its_columns_derive_from(tmp_path: Path) -> None:
    graph = build(
        tmp_path,
        {
            'sales': "create table sales as select country, sum(amount) as amount from read_parquet('data/sales/*.parquet') group by 1"
        },
    )
    files = graph.nodes[SALES]
    assert (files.kind, files.name, files.relation) == ('file', 'data/sales/*.parquet', SALES)
    assert files.attrs['format'] == 'parquet'
    assert files.attrs['paths'] == ['data/sales/*.parquet']
    assert files.attrs['columns_guessed'] is True
    assert derived(graph, 'table.sales#amount') == [f'{SALES}#amount']
    assert derived(graph, 'table.sales#country') == [f'{SALES}#country']
    assert ('feeds' in {e.kind for e in graph.out_edges(SALES)}) and not graph.built(files)
    assert graph.nodes_for_relation(SALES) == [files]


def test_two_models_reading_the_same_files_share_their_columns(tmp_path: Path) -> None:
    graph = build(
        tmp_path,
        {
            'a': "create table a as select id, amount from read_parquet('data/sales/*.parquet')",
            'b': "create table b as select id, amount * 2 as doubled from read_parquet('./data/sales/*.parquet') s",
        },
    )
    assert [n.id for n in graph.nodes.values() if n.kind == 'file'] == [SALES]
    assert derived(graph, 'table.a#amount') == derived(graph, 'table.b#doubled') == [f'{SALES}#amount']
    assert sorted(c.name for c in graph.columns_of(SALES)) == ['amount', 'id']


def test_the_columns_a_csv_read_lists_are_its_own_not_the_files(tmp_path: Path) -> None:
    graph = build(
        tmp_path,
        {
            'p': "create table p as select * from read_csv('x.csv', columns={'a': 'INT', 'b': 'VARCHAR'})",
            'q': "create table q as select c from read_csv('x.csv', columns={'c': 'INT'})",
        },
    )
    key = file_key('csv', ['x.csv'])
    # The files hold what the reads of them read, whichever came first.
    assert sorted(c.name for c in graph.columns_of(key)) == ['a', 'b', 'c']
    assert graph.nodes[key].attrs['columns_guessed'] is True
    assert derived(graph, 'table.p#a') == [f'{key}#a']
    assert derived(graph, 'table.q#c') == [f'{key}#c']
    assert [c.name for c in graph.columns_of('table.p')] == ['a', 'b']


def test_a_path_that_is_not_a_literal_stays_unresolved(tmp_path: Path) -> None:
    graph = build(tmp_path, {'p': "create table p as select amount from read_parquet(getvariable('x'))"})
    assert not [n for n in graph.nodes.values() if n.kind == 'file']
    assert derived(graph, 'table.p#amount') == []


def test_the_columns_of_files_the_repository_holds_are_read_from_them(tmp_path: Path) -> None:
    (tmp_path / 'data' / 'sales').mkdir(parents=True)
    duckdb.sql(
        "copy (select 'nl' as country, 1.5::double as amount, date '2026-01-01' as day) "
        f"to '{tmp_path / 'data' / 'sales' / 'b.parquet'}'"
    )
    duckdb.sql(f"copy (select 1 as other) to '{tmp_path / 'data' / 'sales' / 'c.parquet'}'")
    (tmp_path / 'data' / 'sales' / 'a.parquet').mkdir()  # a directory, not a file
    (tmp_path / 'data' / 'prices.csv').write_text('Station Name;price\nx;1\n')
    graph = build(
        tmp_path,
        {
            'sales': "create table sales as select * from read_parquet('data/sales/*.parquet')",
            'prices': "create table prices as select * from read_csv('data/prices.csv')",
        },
    )
    files = graph.nodes[SALES]
    assert files.attrs['columns_from'] == 'data/sales/b.parquet'
    assert [(c.name, c.data_type) for c in graph.columns_of(SALES)] == [
        ('country', 'VARCHAR'),
        ('amount', 'DOUBLE'),
        ('day', 'DATE'),
    ]
    assert 'columns_guessed' not in files.attrs
    assert sorted(c.name for c in graph.columns_of('table.sales')) == ['amount', 'country', 'day']
    assert derived(graph, 'table.sales#day') == [f'{SALES}#day']
    prices = file_key('csv', ['data/prices.csv'])
    assert [c.name for c in graph.columns_of(prices)] == ['Station Name', 'price']
    assert derived(graph, 'table.prices#price') == [f'{prices}#price']
    assert 'data/sales/b.parquet' in graph.meta['watched']


@pytest.mark.skipif(not hasattr(os, 'symlink'), reason='no symbolic links')
def test_a_repository_path_that_is_a_link_is_not_read(tmp_path: Path) -> None:
    outside = tmp_path / 'outside'
    outside.mkdir()
    duckdb.sql(f"copy (select 1 as secret) to '{outside / 'x.parquet'}'")
    (outside / 'x.csv').write_text('secret\n1\n')
    root = tmp_path / 'repo'
    (root / 'data').mkdir(parents=True)
    (root / 'data' / 'x.parquet').symlink_to(outside / 'x.parquet')
    (root / 'linked').symlink_to(outside, target_is_directory=True)
    graph = build(
        root,
        {
            'a': "create table a as select id from read_parquet('data/x.parquet')",
            'b': "create table b as select id from read_csv('linked/x.csv')",
            'c': "create table c as select id from read_parquet('../outside/x.parquet')",
        },
    )
    for path in ('data/x.parquet', 'linked/x.csv', '../outside/x.parquet'):
        node = graph.nodes[file_key(path.rsplit('.', 1)[1], [path])]
        assert 'columns_from' not in node.attrs
        assert [c.name for c in graph.columns_of(node.id)] == ['id']
    held = TrackedFiles(['data/x.parquet', 'linked/x.csv', '../outside/x.csv', 'outside/x.csv'])
    assert data_file_columns(str(root), 'data/x.parquet', 'parquet', held) is None
    assert data_file_columns(str(root), 'linked/x.csv', 'csv', held) is None
    assert data_file_columns(str(root), '../outside/x.csv', 'csv', held) is None


def test_a_project_without_file_reads_lists_no_file_kind(tmp_path: Path) -> None:
    project = Project(tmp_path)
    (tmp_path / 'm.sql').write_text('create table t as select a from s')
    project.build(run_dbt=False, warehouses=False)
    assert 'file' not in project.list().kinds
    (tmp_path / 'n.sql').write_text("create table u as select a from read_csv('a.csv')")
    project.build(run_dbt=False, warehouses=False)
    assert 'file' in project.list().kinds
    assert [i.to_dict()['kind'] for i in project.list(['file']).items] == ['file']


def test_check_sql_knows_the_columns_a_csv_read_lists(tmp_path: Path) -> None:
    """The columns a call lists are all it returns, wherever the query names them, and a table
    function is never taken for a relation of the default schema with the same key (``mart``)."""
    (tmp_path / 'konstruct.toml').write_text('dialect = "duckdb"\ndefault_schema = "mart"\n')
    graph = build(
        tmp_path,
        {
            'price': "create table price as select I, PRICE from read_csv('price.csv', columns={'I': 'VARCHAR', 'PRICE': 'DOUBLE'})"
        },
    )
    mart = graph.add_node(Node(id='table.mart', kind='table', name='mart', relation='mart'))
    graph.add_column(mart, 'x')
    checker = Checker(graph)
    for sql in (
        "select I, PRICE from read_csv('price.csv', columns={'I': 'VARCHAR', 'PRICE': 'DOUBLE'})",
        "select t.I, t.PRICE from read_csv('price.csv', columns={'I': 'VARCHAR', 'PRICE': 'DOUBLE'}) t",
        "select t.I from read_csv('other.csv', columns={'I': 'VARCHAR'}) t",
        "select I, PRICE from read_csv('price.csv')",
    ):
        assert [f.rule for f in checker.check(sql).findings] == [], sql
    missing = checker.check("select t.NOPE from read_csv('price.csv', columns={'I': 'VARCHAR'}) t")
    assert [f.message for f in missing.findings if f.rule == 'unknown-column'] == [
        'read_csv(...) has no column nope'
    ]
    # One finding for a column, however many times and ways the query names it.
    twice = checker.check(
        "select t.NOPE, NOPE, t.NOPE + 1 from read_csv('price.csv', columns={'I': 'VARCHAR'}) t"
    )
    assert [f.rule for f in twice.findings].count('unknown-column') == 1


def test_a_file_the_project_holds_is_checked_only_for_a_read_like_the_one_it_was_read_by(
    tmp_path: Path,
) -> None:
    (tmp_path / 'data' / 'day=1').mkdir(parents=True)
    (tmp_path / 'data' / 'prices.csv').write_text('id,price\n1,2\n')
    (tmp_path / 'data' / 'day=1' / 'p.parquet').write_bytes(b'')
    duckdb.sql(f"copy (select 1 as id, 2 as price) to '{tmp_path / 'data' / 'day=1' / 'p.parquet'}'")
    graph = build(
        tmp_path,
        {
            'a': "create table a as select id, price from read_csv('data/prices.csv')",
            'b': "create table b as select id from read_parquet('data/day=1/p.parquet')",
        },
    )
    checker = Checker(graph)
    assert graph.nodes[file_key('csv', ['data/prices.csv'])].attrs['columns_from'] == 'data/prices.csv'

    def unknown(sql: str) -> list[str]:
        return [f.message for f in checker.check(sql).findings if f.rule == 'unknown-column']

    assert unknown("select id, price from read_csv('data/prices.csv')") == []
    assert len(unknown("select cost from read_csv('data/prices.csv')")) == 1
    # Another read of the files has other columns: names it gives, none for a header, a partition
    # key or the file's name, a delimiter that may split the record elsewhere.
    for sql in (
        "select cost from read_csv('data/prices.csv', names=['cost'])",
        "select column0 from read_csv('data/prices.csv', header=false)",
        "select cost from read_csv('data/prices.csv', delim=';')",
        "select filename from read_csv('data/prices.csv', filename=true)",
        "select filename, file_row_number from read_csv('data/prices.csv')",
        "select day from read_parquet('data/day=1/p.parquet')",
        "select day from read_parquet('data/*/p.parquet', hive_partitioning=true)",
    ):
        assert unknown(sql) == [], sql
    assert unknown("select cost from read_parquet('data/day=1/p.parquet', union_by_name=true)") == []
    # names=[...] renames the first columns and leaves the rest: id is now called ident.
    assert unknown("select ident, price from read_csv('data/prices.csv', names=['ident'])") == []
    assert unknown("select id from read_csv('data/prices.csv', names=['ident'])") == [
        'read_csv(...) has no column id'
    ]


def test_a_dbt_project_s_file_is_found_by_the_path_its_model_reads_it_by(tmp_path: Path) -> None:
    root = tmp_path / 'sub'
    (root / 'target').mkdir(parents=True)
    (root / 'data').mkdir()
    (root / 'dbt_project.yml').write_text('name: sub\n')
    (root / 'data' / 'x.csv').write_text('id,price\n1,2\n')
    model = {
        'unique_id': 'model.sub.a',
        'resource_type': 'model',
        'name': 'a',
        'alias': 'a',
        'package_name': 'sub',
        'database': 'db',
        'schema': 'main',
        'relation_name': '"db"."main"."a"',
        'original_file_path': 'models/a.sql',
        'fqn': ['sub', 'a'],
        'raw_code': "select id, price from read_csv('data/x.csv')",
        'language': 'sql',
        'depends_on': {'nodes': [], 'macros': []},
        'config': {'materialized': 'table'},
        'tags': [],
        'columns': {},
    }
    manifest = {'metadata': {'adapter_type': 'duckdb'}, 'nodes': {'model.sub.a': model}, 'sources': {}}
    (root / 'target' / 'manifest.json').write_text(json.dumps(manifest))
    track(tmp_path)
    graph = Project(tmp_path).build(run_dbt=False, dialect='duckdb', warehouses=False).graph
    key = file_key('csv', ['sub/data/x.csv'])
    assert [c.name for c in graph.columns_of(key)] == ['id', 'price']
    found = Checker(graph).check("select cost from read_csv('data/x.csv')")
    assert [f.rule for f in found.findings] == ['unknown-column']


def test_only_the_files_git_tracks_are_read(tmp_path: Path) -> None:
    """A pull request can name any path of the workspace: a credential the CI wrote beside the
    checkout is never read into the graph, nor a file the repository does not track."""
    (tmp_path / 'data').mkdir()
    (tmp_path / 'data' / 'ok.csv').write_text('id,amount\n1,2\n')
    (tmp_path / 'data' / 'new.csv').write_text('untracked_column\n1\n')
    (tmp_path / '.env').write_text('SECRET_TOKEN=abc\n')
    (tmp_path / 'gha-creds-1a2b.json').write_text('{"private_key": "x"}\n')
    (tmp_path / '.gitignore').write_text('data/new.csv\n')
    track(tmp_path, 'data/ok.csv', '.gitignore')
    graph = build(
        tmp_path,
        {
            'ok': "create table ok as select id from read_csv('data/ok.csv')",
            'new': "create table new as select id from read_csv('data/new.csv')",
            'env': "create table e as select id from read_csv('.env')",
            'creds': "create table c as select id from read_csv('gha-creds-*.json')",
            'all': "create table w as select id from read_csv('*')",
        },
        tracked=False,
    )
    assert graph.nodes[file_key('csv', ['data/ok.csv'])].attrs['columns_from'] == 'data/ok.csv'
    text = repr([(n.id, n.attrs, [c.name for c in graph.columns_of(n.id)]) for n in graph.nodes.values()])
    for secret in ('SECRET_TOKEN', 'private_key', 'untracked_column'):
        assert secret not in text
    for node in graph.nodes.values():
        if node.kind == 'file' and node.name != 'data/ok.csv':
            assert 'columns_from' not in node.attrs


def test_no_git_repository_means_no_file_is_read(tmp_path: Path) -> None:
    (tmp_path / 'x.csv').write_text('secret\n1\n')
    graph = build(tmp_path, {'a': "create table a as select id from read_csv('x.csv')"}, tracked=False)
    node = graph.nodes[file_key('csv', ['x.csv'])]
    assert 'columns_from' not in node.attrs and [c.name for c in graph.columns_of(node.id)] == ['id']


def test_a_path_with_a_drive_is_not_read_as_one_of_the_repository(tmp_path: Path) -> None:
    (tmp_path / 'C:').mkdir()
    (tmp_path / 'C:' / 'x.csv').write_text('secret\n1\n')
    held = TrackedFiles(['C:/x.csv', 'C:x.csv'])
    assert data_file_columns(str(tmp_path), 'C:/x.csv', 'csv', held) is None
    assert data_file_columns(str(tmp_path), 'C:x.csv', 'csv', held) is None
    graph = build(tmp_path, {'a': "create table a as select id from read_csv('C:/x.csv')"})
    assert [c.name for c in graph.columns_of(file_key('csv', ['C:/x.csv']))] == ['id']


def test_a_file_whose_columns_are_only_those_queries_read_may_have_others(tmp_path: Path) -> None:
    graph = build(
        tmp_path,
        {'a': "create table a as select id, amount from read_parquet('s3://bucket/sales/*.parquet')"},
    )
    checker = Checker(graph)
    sql = "select {} from read_parquet('s3://bucket/sales/*.parquet')"
    assert checker.check(sql.format('id, amount')).findings == []
    # Not close to a column the graph knows: a note, as for any relation whose columns are guessed.
    note = checker.check(sql.format('region')).findings
    assert [(f.rule, f.severity) for f in note] == [('unknown-column', 'info')]
    # Close to one: likely misspelt.
    near = checker.check(sql.format('amounts')).findings
    assert [(f.rule, f.severity) for f in near] == [('unknown-column', 'warning')]


def commit(root: Path, message: str = 'c') -> None:
    git = ['git', '-C', str(root), '-c', 'user.email=t@t', '-c', 'user.name=t', '-c', 'commit.gpgsign=false']
    subprocess.run(['git', '-C', str(root), 'init', '-q', '-b', 'main'], check=True)
    subprocess.run([*git, 'add', '-A'], check=True)
    subprocess.run([*git, 'commit', '-qm', message], check=True)


def test_a_build_of_a_commit_reads_the_files_the_commit_holds(tmp_path: Path) -> None:
    """The gate's base is built from a tree written from the commit, which has no ``.git``: it
    reads the same files the working tree does, but not a link, which the working tree refuses."""
    from konstruct.analysis.sensitivity import base_graph

    (tmp_path / 'data').mkdir()
    (tmp_path / 'data' / 'x.csv').write_text('id,email\n1,a\n')
    (tmp_path / 'data' / 'real.csv').write_text('id,secret\n1,a\n')
    if hasattr(os, 'symlink'):
        (tmp_path / 'data' / 'link.csv').symlink_to('real.csv')
    (tmp_path / 'models').mkdir()
    (tmp_path / 'models' / 'a.sql').write_text(
        "create table a as select * from read_csv('data/x.csv');\n"
        "create table b as select id from read_csv('data/link.csv');"
    )
    (tmp_path / 'konstruct.toml').write_text('dialect = "duckdb"\n')
    commit(tmp_path)
    graph = base_graph(tmp_path, 'HEAD')
    assert graph is not None
    x = graph.nodes[file_key('csv', ['data/x.csv'])]
    assert x.attrs['columns_from'] == 'data/x.csv'
    assert [c.name for c in graph.columns_of(x.id)] == ['id', 'email']
    assert 'columns_from' not in graph.nodes[file_key('csv', ['data/link.csv'])].attrs
    working = Project(tmp_path).build(run_dbt=False, warehouses=False).graph
    assert 'columns_from' not in working.nodes[file_key('csv', ['data/link.csv'])].attrs


def test_a_repository_git_cannot_list_is_warned_of_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / 'x.csv').write_text('id\n1\n')
    monkeypatch.setenv('PATH', str(tmp_path / 'no-git'))
    graph = build(
        tmp_path,
        {
            'a': "create table a as select id from read_csv('x.csv')",
            'b': "create table b as select id from read_csv('x.csv', header=false)",
        },
        tracked=False,
    )
    assert [w for w in graph.warnings if 'git could not list' in w] and len(
        [w for w in graph.warnings if 'git could not list' in w]
    ) == 1
    assert 'columns_from' not in graph.nodes[file_key('csv', ['x.csv'])].attrs


def test_links_and_submodule_files_are_not_read(tmp_path: Path) -> None:
    (tmp_path / 'data').mkdir()
    (tmp_path / 'data' / 'real.csv').write_text('id\n1\n')
    (tmp_path / 'data' / 'link.csv').symlink_to('real.csv')
    sub = tmp_path / 'vendor'
    sub.mkdir()
    (sub / 'x.csv').write_text('inner\n1\n')
    subprocess.run(['git', '-C', str(sub), 'init', '-q', '-b', 'main'], check=True)
    cfg = ['git', '-C', str(sub), '-c', 'user.email=t@t', '-c', 'user.name=t', '-c', 'commit.gpgsign=false']
    subprocess.run([*cfg, 'add', '-A'], check=True)
    subprocess.run([*cfg, 'commit', '-qm', 'i'], check=True)
    (tmp_path / 'm.sql').write_text(
        "create table a as select * from read_csv('data/link.csv');\n"
        "create table b as select * from read_csv('vendor/x.csv');\n"
        "create table c as select * from read_csv('data/real.csv');"
    )
    subprocess.run(['git', '-C', str(tmp_path), 'init', '-q'], check=True)
    subprocess.run(
        ['git', '-C', str(tmp_path), '-c', 'protocol.file.allow=always', 'add', '-f', '.'], check=True
    )
    graph = Project(tmp_path).build(run_dbt=False, dialect='duckdb', warehouses=False).graph
    for path, got in (('data/link.csv', False), ('vendor/x.csv', False), ('data/real.csv', True)):
        assert ('columns_from' in graph.nodes[file_key('csv', [path])].attrs) is got, path


GIT = ['git', '-c', 'user.email=t@t', '-c', 'user.name=t', '-c', 'commit.gpgsign=false']


def pull_request(
    root: Path,
    files: dict[str, str | bytes],
    models: dict[str, str],
    change: dict[str, str | bytes | None],
) -> Project:
    """A repository at ``main`` with ``files`` and ``models``, and a branch ``pr`` that writes
    ``change`` over it (None deletes a file); the project, built at the branch's head."""
    for name, content in {**files, **{f'models/{n}.sql': q for n, q in models.items()}}.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content if isinstance(content, bytes) else content.encode())
    (root / 'konstruct.toml').write_text('dialect = "duckdb"\n')
    commit(root)
    subprocess.run([*GIT[:1], '-C', str(root), *GIT[1:], 'checkout', '-qb', 'pr'], check=True)
    for name, new in change.items():
        target = root / name
        target.unlink(missing_ok=True)
        if new is not None:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(new if isinstance(new, bytes) else new.encode())
    subprocess.run([*GIT[:1], '-C', str(root), *GIT[1:], 'add', '-A'], check=True)
    subprocess.run([*GIT[:1], '-C', str(root), *GIT[1:], 'commit', '-qm', 'change'], check=True)
    project = Project(root)
    project.build(run_dbt=False, warehouses=False)
    return project


READS = {
    'a': "create table a as select id, amount from read_csv('data/x.csv')",
    'b': "create table b as select * from read_csv('data/x.csv')",
}


def test_an_edited_data_file_is_a_changed_relation(tmp_path: Path) -> None:
    """A pull request that only renames a header in a tracked CSV file changes the columns of the
    file: the diff names both sides' columns, and the gate does not pass a column a model still
    reads going missing. A model that only passes the column on (``select *``) does not read it."""
    project = pull_request(
        tmp_path, {'data/x.csv': 'id,amount\n1,2\n'}, READS, {'data/x.csv': 'id,amt\n1,2\n'}
    )
    change = project.diff(base='main')
    assert 'data/x.csv' not in change.other
    [relation] = [r for r in change.relations if r.kind == 'file']
    assert relation.status == 'modified' and relation.path == 'data/x.csv'
    assert {(c.column, c.change) for c in relation.columns} == {('amount', 'removed'), ('amt', 'added')}
    removed = next(c for c in relation.columns if c.column == 'amount')
    assert removed.breaks == ['a']
    result = project.gate(base='main', run_dbt=False, warehouses=False)
    assert result.findings and not result.passed


def test_a_column_a_star_passes_on_is_missing_from_what_reads_it(tmp_path: Path) -> None:
    """``b`` selects every column of the file and ``c`` reads ``amount`` from ``b``: the header's
    rename takes the column out of ``b``, so ``c`` reads a column that is gone."""
    project = pull_request(
        tmp_path,
        {'data/x.csv': 'id,amount\n1,2\n'},
        {'b': READS['b'], 'c': 'create table c as select amount from b'},
        {'data/x.csv': 'id,amt\n1,2\n'},
    )
    change = project.diff(base='main')
    [relation] = [r for r in change.relations if r.kind == 'file']
    removed = next(c for c in relation.columns if c.column == 'amount')
    assert removed.breaks == ['c']
    result = project.gate(base='main', run_dbt=False, warehouses=False)
    assert not result.passed and any('c' in f.message for f in result.findings)


def test_a_deleted_data_file_removes_its_columns_from_what_reads_it(tmp_path: Path) -> None:
    project = pull_request(tmp_path, {'data/x.csv': 'id,amount\n1,2\n'}, READS, {'data/x.csv': None})
    change = project.diff(base='main')
    assert 'data/x.csv' not in change.other
    [relation] = [r for r in change.relations if r.kind == 'file']
    assert relation.status == 'removed'
    assert {(c.column, c.change) for c in relation.columns} == {('id', 'removed'), ('amount', 'removed')}
    assert next(c for c in relation.columns if c.column == 'amount').breaks == ['a']
    assert not project.gate(base='main', run_dbt=False, warehouses=False).passed


def test_a_file_added_before_the_first_one_a_pattern_matches_changes_its_columns(tmp_path: Path) -> None:
    """A read of ``data/*.csv`` has the columns of the first file in name order: a new file that
    sorts first takes them over, though ``columns_from`` at the head names a file that was added."""
    project = pull_request(
        tmp_path,
        {'data/m.csv': 'id,amount\n1,2\n'},
        {'a': "create table a as select id from read_csv('data/*.csv')"},
        {'data/a.csv': 'zz\n1\n'},
    )
    change = project.diff(base='main')
    assert 'data/a.csv' not in change.other
    [relation] = [r for r in change.relations if r.kind == 'file']
    assert relation.status == 'modified'
    assert {(c.column, c.change) for c in relation.columns} == {
        ('zz', 'added'),
        ('id', 'removed'),
        ('amount', 'removed'),
    }
    assert next(c for c in relation.columns if c.column == 'id').breaks == ['a']


def test_a_blob_that_is_not_a_regular_file_is_not_a_data_file_at_the_base(tmp_path: Path) -> None:
    """A symbolic link tracked as ``data/x.csv`` is its target's name, not CSV: the file is new."""
    (tmp_path / 'data').mkdir()
    (tmp_path / 'data' / 'x.csv').symlink_to('real.csv')
    project = pull_request(
        tmp_path,
        {},
        {'a': "create table a as select id from read_csv('data/x.csv')"},
        {'data/x.csv': 'id,amount\n1,2\n'},
    )
    [relation] = [r for r in project.diff(base='main').relations if r.kind == 'file']
    assert relation.status == 'added'
    assert {(c.column, c.change) for c in relation.columns} == {('id', 'added'), ('amount', 'added')}


def test_a_data_file_over_the_size_limit_is_not_read_for_the_comparison(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from konstruct.analysis import diff as diff_module

    monkeypatch.setattr(diff_module, 'DATA_FILE_LIMIT', 8)
    project = pull_request(
        tmp_path, {'data/x.csv': 'id,amount\n1,2\n'}, READS, {'data/x.csv': 'id,amt\n1,2\n'}
    )
    [relation] = [r for r in project.diff(base='main').relations if r.kind == 'file']
    assert relation.note and [(c.column, c.breaks) for c in relation.columns] == [('amount', ['a'])]


LFS = b'version https://git-lfs.github.com/spec/v1\noid sha256:' + b'0' * 64 + b'\nsize 12345\n'


def test_an_lfs_pointer_is_not_a_data_file(tmp_path: Path) -> None:
    """Where the base commit holds a Git LFS pointer, the file's text is the pointer, not the
    data: neither the base's build nor the comparison reads columns from it."""
    from konstruct.analysis.sensitivity import base_graph

    project = pull_request(tmp_path, {'data/x.csv': LFS}, READS, {'data/x.csv': 'id,amt\n1,2\n'})
    graph = base_graph(tmp_path, 'main')
    assert graph is not None
    assert 'columns_from' not in graph.nodes[file_key('csv', ['data/x.csv'])].attrs
    [relation] = [r for r in project.diff(base='main').relations if r.kind == 'file']
    assert relation.note and [(c.column, c.breaks) for c in relation.columns] == [('amount', ['a'])]


def test_a_file_a_filter_rewrites_is_not_read_from_its_blob(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``filter`` attribute (Git LFS is one) stores other text in a commit than the file holds:
    the blob is not the data, so a commit's build and the comparison do not read it as one."""
    from konstruct.analysis.sensitivity import base_graph

    for number, (key, value) in enumerate(
        [('filter.fake.clean', 'sed s/^/PTR_/'), ('filter.fake.smudge', 'sed s/^PTR_//')]
    ):
        monkeypatch.setenv(f'GIT_CONFIG_KEY_{number}', key)
        monkeypatch.setenv(f'GIT_CONFIG_VALUE_{number}', value)
    monkeypatch.setenv('GIT_CONFIG_COUNT', '2')
    (tmp_path / '.gitattributes').write_text('data/*.csv filter=fake\n')
    project = pull_request(
        tmp_path, {'data/x.csv': 'id,email\n1,a\n'}, READS, {'data/x.csv': 'id,mail\n1,a\n'}
    )
    assert subprocess.run(
        ['git', '-C', str(tmp_path), 'show', 'main:data/x.csv'], capture_output=True, check=True
    ).stdout.startswith(b'PTR_')
    graph = base_graph(tmp_path, 'main')
    assert graph is not None
    assert 'columns_from' not in graph.nodes[file_key('csv', ['data/x.csv'])].attrs
    [relation] = [r for r in project.diff(base='main').relations if r.kind == 'file']
    assert relation.note and [(c.column, c.breaks) for c in relation.columns] == [('amount', ['a'])]


def test_a_commit_with_a_name_that_is_not_utf8_can_be_built(tmp_path: Path) -> None:
    """The gate's base and the timeline build a commit's files, among them a file a name of bytes
    that are not text names: it is not a file a query can name, and it must not stop the build."""
    from konstruct.analysis.sensitivity import base_graph

    odd = os.fsdecode(b'notes-\xff.txt')
    try:
        (tmp_path / odd).write_text('x')
    except OSError:
        pytest.skip('the file system does not take a name that is not UTF-8')
    (tmp_path / 'data').mkdir()
    (tmp_path / 'data' / 'x.csv').write_text('id\n1\n')
    (tmp_path / 'models').mkdir()
    (tmp_path / 'models' / 'a.sql').write_text(READS['a'])
    (tmp_path / 'konstruct.toml').write_text('dialect = "duckdb"\n')
    commit(tmp_path)
    graph = base_graph(tmp_path, 'HEAD')
    assert graph is not None
    assert graph.nodes[file_key('csv', ['data/x.csv'])].attrs['columns_from'] == 'data/x.csv'


def test_a_file_that_only_ends_in_the_path_is_not_the_file_read(tmp_path: Path) -> None:
    (tmp_path / 'vendor' / 'data').mkdir(parents=True)
    (tmp_path / 'vendor' / 'data' / 'x.csv').write_text('id,amount\n1,2\n')
    graph = build(
        tmp_path,
        {'v': "create table v as select id, amount from read_csv('vendor/data/x.csv')"},
    )
    checker = Checker(graph)
    sql = "select foo from read_csv('data/x.csv')"
    assert [f.rule for f in checker.check(sql).findings if f.rule == 'unknown-column'] == []
    assert len(checker.check("select foo from read_csv('vendor/data/x.csv')").findings) == 1


def filter_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``filter`` driver called ``fake`` that stores a file with a prefix, as a pointer does."""
    for number, (key, value) in enumerate(
        [('filter.fake.clean', 'sed s/^/PTR_/'), ('filter.fake.smudge', 'sed s/^PTR_//')]
    ):
        monkeypatch.setenv(f'GIT_CONFIG_KEY_{number}', key)
        monkeypatch.setenv(f'GIT_CONFIG_VALUE_{number}', value)
    monkeypatch.setenv('GIT_CONFIG_COUNT', '2')


def old_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``git`` that does not know ``--source=``, as before 2.40."""
    real = shutil.which('git')
    bin_dir = tmp_path.parent / f'{tmp_path.name}-oldgit'
    bin_dir.mkdir()
    (bin_dir / 'git').write_text(
        f'#!/bin/sh\nfor a in "$@"; do case "$a" in --source=*) exit 129;; esac; done\nexec {real} "$@"\n'
    )
    (bin_dir / 'git').chmod(0o755)
    monkeypatch.setenv('PATH', f'{bin_dir}{os.pathsep}{os.environ["PATH"]}')


@pytest.mark.parametrize('filtered', [False, True])
def test_a_git_without_attribute_sources_reads_the_attributes_of_the_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filtered: bool
) -> None:
    """Before git 2.40 ``check-attr`` cannot name a commit: the commit's own attributes are read
    through an index of it, so a file is unread only where a filter really changes it."""
    from konstruct.analysis.sensitivity import base_graph

    old_git(tmp_path, monkeypatch)
    if filtered:
        filter_config(monkeypatch)
        (tmp_path / '.gitattributes').write_text('data/*.csv filter=fake\n')
    pull_request(tmp_path, {'data/x.csv': 'id,email\n1,a\n'}, READS, {'data/x.csv': 'id,mail\n1,a\n'})
    graph = base_graph(tmp_path, 'main')
    assert graph is not None
    assert ('columns_from' in graph.nodes[file_key('csv', ['data/x.csv'])].attrs) is not filtered


def sensitivity_project(root: Path, pay_base: str, pay_head: str) -> Project:
    (root / 'data').mkdir(parents=True)
    (root / 'data' / 'x.csv').write_text('id,email\n1,a@b.c\n')
    (root / 'pay.sql').write_text(pay_base)
    (root / 'definitions.yml').write_text(
        'version: 1\nterms:\n  - name: Contact email\n    definition: e.\n    sensitivity: personal\n'
        '    columns: [crm.pay.email]\n'
    )
    (root / 'konstruct.toml').write_text('dialect = "duckdb"\n[sensitivity]\nholders = ["crm.pay"]\n')
    (root / '.gitignore').write_text('konstruct-out/\n')
    commit(root)
    subprocess.run([*GIT[:1], '-C', str(root), *GIT[1:], 'checkout', '-qb', 'pr'], check=True)
    (root / 'pay.sql').write_text(pay_head)
    subprocess.run([*GIT[:1], '-C', str(root), *GIT[1:], 'commit', '-qam', 'drop the column'], check=True)
    project = Project(root)
    project.build(run_dbt=False, warehouses=False)
    return project


@pytest.mark.parametrize('filtered', [False, True])
def test_a_column_that_loses_its_mark_is_not_passed_where_the_base_could_not_read_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filtered: bool
) -> None:
    """``pay`` reads a data file and stops selecting its ``email`` column, which a term marks.
    Where the base's file is stored by a filter its columns at the base are not known, which is
    no reason to pass the change: the gate says it cannot tell, as it does for a base it cannot
    build."""
    if filtered:
        filter_config(monkeypatch)
        (tmp_path / '.gitattributes').write_text('data/*.csv filter=fake\n')
    project = sensitivity_project(
        tmp_path,
        "create table crm.pay as select * from read_csv('data/x.csv')\n",
        "create table crm.pay as select id from read_csv('data/x.csv')\n",
    )
    result = project.gate(base='main', run_dbt=False, warehouses=False)
    assert [f.rule for f in result.findings] == ['sensitivity-removed']
    assert not result.passed


def test_a_header_renamed_in_a_file_the_base_cannot_read_still_breaks_what_reads_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whatever the base's file held, a model that reads a column the head's file lacks is broken:
    the comparison checks the readers against the head's columns when a side cannot be read."""
    filter_config(monkeypatch)
    (tmp_path / '.gitattributes').write_text('data/*.csv filter=fake\n')
    project = pull_request(
        tmp_path, {'data/x.csv': 'id,amount\n1,2\n'}, READS, {'data/x.csv': 'id,amt\n1,2\n'}
    )
    [relation] = [r for r in project.diff(base='main').relations if r.kind == 'file']
    assert relation.note
    assert {(c.column, c.change, tuple(c.breaks)) for c in relation.columns} == {
        ('amount', 'removed', ('a',))
    }
    assert not project.gate(base='main', run_dbt=False, warehouses=False).passed


def test_a_relation_reached_by_two_paths_keeps_the_columns_of_both(tmp_path: Path) -> None:
    """``j`` joins ``b1``, which passes ``amount`` on, to ``b2``, which passes ``qty`` on: whichever
    the walk reaches first, ``l`` still finds ``amount`` gone from ``j``."""
    project = pull_request(
        tmp_path,
        {'data/x.csv': 'id,amount,qty\n1,2,3\n'},
        {
            'b1': "create table b1 as select * exclude (qty) from read_csv('data/x.csv')",
            'b2': "create table b2 as select * exclude (amount) from read_csv('data/x.csv')",
            'j': 'create table j as select * from b1 join b2 using (id)',
            'l': 'create table l as select amount from j',
        },
        {'data/x.csv': 'id,amt,q\n1,2,3\n'},
    )
    [relation] = [r for r in project.diff(base='main').relations if r.kind == 'file']
    assert next(c for c in relation.columns if c.column == 'amount').breaks == ['l']
    assert not project.gate(base='main', run_dbt=False, warehouses=False).passed


def test_each_reader_is_analysed_once_however_many_columns_are_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from konstruct.analysis.diff import Context

    count = 0
    original = Context.lineage

    def counted(self: Context, *args: object, **kwargs: object) -> object:
        nonlocal count
        count += 1
        return original(self, *args, **kwargs)  # type: ignore[arg-type]

    models = {
        f'm{i}': f"create table m{i} as select c{i % 10} from read_csv('data/x.csv')" for i in range(12)
    }
    project = pull_request(
        tmp_path,
        {'data/x.csv': ','.join(f'c{j}' for j in range(10)) + '\n' + ','.join('1' * 10) + '\n'},
        models,
        {'data/x.csv': ','.join(f'r{j}' for j in range(10)) + '\n' + ','.join('1' * 10) + '\n'},
    )
    monkeypatch.setattr(Context, 'lineage', counted)
    change = project.diff(base='main')
    assert sum(1 for r in change.relations for c in r.columns if c.breaks) == 10
    assert count <= 3 * len(models), count


def test_a_pointer_in_the_working_tree_is_not_a_data_file(tmp_path: Path) -> None:
    pointer = 'version https://git-lfs.github.com/spec/v1\noid sha256:' + '0' * 64 + '\nsize 12345\n'
    (tmp_path / 'data').mkdir()
    (tmp_path / 'data' / 'x.csv').write_text(pointer)
    graph = build(tmp_path, {'a': READS['a']})
    node = graph.nodes[file_key('csv', ['data/x.csv'])]
    assert 'columns_from' not in node.attrs
    assert graph.meta['unread_files'] == ['data/x.csv']
