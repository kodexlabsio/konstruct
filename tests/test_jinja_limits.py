"""The renderer's limits: what a hostile template, macro file or manifest can ask of it. Whatever a
template says, a render returns text or raises ``ValueError``; it never overflows the stack, panics,
runs for long or takes the memory of the machine. Each case runs on a 128 KiB thread stack."""

from __future__ import annotations

import contextlib
import json
import os
import random
import resource
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from konstruct.core.graph import Graph
from konstruct.inputs import dbt
from konstruct.inputs.jinja import Renderer

SMALL_STACK = 128 * 1024
Outcome = tuple[str, str]


def on_a_small_stack(work: Callable[[], Outcome]) -> Outcome:
    """``work`` on a thread with a 128 KiB stack; a stack overflow would end the process."""
    found: list[Outcome] = []
    previous = threading.stack_size(SMALL_STACK)
    try:
        thread = threading.Thread(target=lambda: found.append(work()))
        thread.start()
    finally:
        threading.stack_size(previous)
    thread.join(60)
    assert not thread.is_alive(), 'the render did not stop'
    return found[0]


def render(sql: str, root: Path | None = None, manifest: dict[str, Any] | None = None) -> Outcome:
    """``('text', rendered)`` or ``('error', message)``; any other exception fails the test."""

    def work() -> Outcome:
        try:
            return 'text', Renderer(root or Path('/nonexistent'), manifest or {}).render(sql)
        except ValueError as error:
            # A caught panic is a bug, not a refusal: it must not pass for one.
            assert 'internal error' not in str(error), str(error)
            return 'error', str(error)

    return on_a_small_stack(work)


def text_or_error(sql: str) -> Outcome:
    started = time.perf_counter()
    outcome = render(sql)
    assert time.perf_counter() - started < 20, 'the render took too long'
    return outcome


NESTED = 100_000
HOSTILE: dict[str, str] = {
    'attribute chain': '{{ x' + '.a' * 50_000 + ' }}',
    'subscript chain': '{{ x' + '[0]' * 50_000 + ' }}',
    'call chain': '{{ x' + '()' * 50_000 + ' }}',
    'sum chain': '{{ 1' + '+1' * 50_000 + ' }}',
    'product chain': '{{ 1' + '*1' * 50_000 + ' }}',
    'power chain': '{{ 1' + '**1' * 50_000 + ' }}',
    'and chain': '{{ 1' + ' and 1' * 50_000 + ' }}',
    'or chain': '{{ 0' + ' or 0' * 50_000 + ' }}',
    'filter chain': '{{ 1' + '|string' * 50_000 + ' }}',
    'test chain': '{{ 1' + ' is defined' * 50_000 + ' }}',
    'conditional chain': '{{ 1' + ' if 1' * 50_000 + ' }}',
    'concat chain': "{{ 'a'" + " ~ 'a'" * 50_000 + ' }}',
    'parentheses': '{{ ' + '(' * NESTED + '1' + ')' * NESTED + ' }}',
    'brackets': '{{ ' + '[' * NESTED + ']' * NESTED + ' }}',
    'unary': '{{ ' + '-' * NESTED + '1 }}',
    'not': '{{ ' + 'not ' * NESTED + '1 }}',
    'set target': '{% set ' + '(' * NESTED + 'a' + ')' * NESTED + ' = 1 %}',
    'blocks': '{% if 1 %}' * NESTED + 'x' + '{% endif %}' * NESTED,
    'for blocks': '{% for i in [1] %}' * 50_000 + 'x' + '{% endfor %}' * 50_000,
    'macro blocks': '{% macro m() %}' * 20_000 + 'x' + '{% endmacro %}' * 20_000,
    'list contains itself': '{% set l = [] %}{% do l.append(l) %}{{ l }}',
    'list contains itself, as json': '{% set l = [] %}{% do l.append(l) %}{{ l | tojson }}',
    'list contains itself, as string': '{% set l = [] %}{% do l.append(l) %}{{ l | string }}{{ l | join }}',
    'namespace contains itself': '{% set ns = namespace() %}{% set ns.me = ns %}{{ ns }}',
    'dict contains itself': '{% set d = {} %}{% do d.update(me=d) %}{{ d }}{{ d | tojson }}',
    'lists that contain themselves, compared': (
        '{% set a = [] %}{% set b = [] %}{% do a.append(a) %}{% do b.append(b) %}{{ a == b }}{{ a < b }}'
    ),
    'self-containing list as macro argument': (
        "{% set l = [] %}{% do l.append(l) %}{{ unknown(l) }}{{ unknown(a=l) }}{{ [l] | map('string') | list }}"
    ),
    'tuples nested a hundred thousand deep, then dropped': (
        '{% set t = () %}{% for i in range(100000) %}{% set t = (t,) %}{% endfor %}x'
    ),
    'lists nested deep, then printed': (
        '{% set t = [] %}{% for i in range(20000) %}{% set t = [t] %}{% endfor %}{{ t }}{{ t | tojson }}'
    ),
    'lists nested deep, then compared': (
        '{% set a = [] %}{% set b = [] %}{% for i in range(20000) %}{% set a = [a] %}{% set b = [b] %}'
        '{% endfor %}{{ a == b }}'
    ),
    'macro recursion': '{% macro r() %}{{ r() }}{% endmacro %}{{ r() }}',
    'macro recursion with caller': ('{% macro r() %}{% call r() %}x{% endcall %}{% endmacro %}{{ r() }}'),
    'recursive loop': '{% for x in [1] recursive %}{{ loop([1]) }}{% endfor %}',
    'extend a list with itself': '{% set l = [1] %}{% do l.extend(l) %}{{ l }}',
    'index, count and remove on a list that holds itself': (
        '{% set l = [1] %}{% do l.append(l) %}{{ l.index(l) }}{{ l.count(l) }}{% do l.remove(l) %}{{ l | length }}'
    ),
    'sort a list that holds itself': '{% set l = [] %}{% do l.append(l) %}{% do l.append(l) %}{% do l.sort() %}',
    'most negative integer divided by minus one': '{{ (-(2 ** 127)) // -1 }}',
    'most negative integer modulo minus one': '{{ (-(2 ** 127)) % -1 }}',
    'most negative integer divisible': '{{ (-(2 ** 127)) is divisibleby -1 }}',
    'slice step of two to the sixty-fourth': '{{ [1, 2, 3][::18446744073709551616] }}{{ [1,2,3][::-18446744073709551616] }}',
    'previtem after the list shrinks': (
        '{% set l = [1, 2, 3] %}{% for x in l %}{% set keep = loop %}{% do l.clear() %}{% endfor %}'
        '{{ keep.previtem }}{{ keep.nextitem }}{{ keep.revindex }}{{ keep.revindex0 }}{{ keep.length }}{{ keep.last }}'
    ),
    'repetition past the cap': "{{ ('ab' * (2 ** 126 + 2 ** 40)) | length }}",
    'list repetition past the cap': '{{ ([1] * (2 ** 126 + 2 ** 40)) | length }}',
    'abs of the most negative integer': '{{ (-(2 ** 127)) | abs }}',
    'round of a large integer with negative precision': '{{ (2 ** 126) | round(-37) }}{{ (2 ** 126) | round(-1000) }}',
    'truncate with a huge length': "{{ 'abc' | truncate(2 ** 127) }}{{ 'abc' | truncate(1, false, '...', 2 ** 127) }}",
    'range over the whole integer line': '{{ range(-(2 ** 127), 2 ** 127) | length }}',
    'sort of mixed integers and floats': '{{ [2 ** 100, 1e30, 2 ** 100 + 1, 1.2676506002282294e30] | sort }}',
    'sort of values that cannot be ordered': "{{ [1, 'a', 2] | sort }}",
    'string doubling': "{% set s = 'x' %}{% for i in range(60) %}{% set s = s + s %}{% endfor %}{{ s | length }}",
    'concat doubling': "{% set s = 'x' %}{% for i in range(60) %}{% set s = s ~ s %}{% endfor %}{{ s | length }}",
    'list doubling': '{% set s = [1] %}{% for i in range(60) %}{% set s = s + s %}{% endfor %}{{ s | length }}',
    'big string printed in loops': (
        "{% set s = 'x' * 1000000 %}{% for i in range(10000) %}{{ s }}{% endfor %}"
    ),
    'ljust': "{{ 'a'.ljust(10 ** 13) | length }}",
    'rjust': "{{ 'a'.rjust(10 ** 13) | length }}",
    'center': "{{ 'a'.center(10 ** 13) | length }}{{ 'a' | center(10 ** 13) | length }}",
    'zfill': "{{ '1'.zfill(10 ** 13) | length }}",
    'format width': "{{ '{:>10000000000000}'.format('a') | length }}",
    'format precision': "{{ '{:.10000000000000f}'.format(1.5) | length }}",
    'percent width': "{{ '%10000000000000d' % 1 }}",
    'percent precision': "{{ '%.10000000000000f' % 1.5 }}",
    'round precision': '{{ 1.5 | round(10 ** 13) }}{{ 1.5 | round(-(10 ** 13)) }}',
    'indent': "{{ 'a' | indent(10 ** 13) | length }}{{ 'a\nb' | indent(10 ** 9, true) | length }}",
    'batch': '{{ [1] | batch(10 ** 13, 0) | list | length }}',
    'slice': '{{ [1] | slice(10 ** 13) | list | length }}',
    'replace of nothing': "{{ ('x' * 100000).replace('', 'y' * 100000) | length }}{{ ('x' * 100000) | replace('', 'y' * 100000) | length }}",
    'nested empty loops': (
        '{% for a in range(100000) %}{% for b in range(100000) %}{% for c in range(100000) %}{% endfor %}'
        '{% endfor %}{% endfor %}'
    ),
    'growing list in a loop': ('{% set l = [1] %}{% for x in l %}{% do l.append(x) %}{% endfor %}'),
    'large builtins in a loop': (
        "{% set s = 'x' * 5000000 %}{% for i in range(100000) %}{{ s | upper | length }}{% endfor %}"
    ),
    'quadratic filters': (
        '{% set l = range(99999) | list %}{% for i in range(10000) %}{{ l | unique | list | length }}{% endfor %}'
    ),
    'membership in a large list': (
        '{% set l = range(99999) | list %}{% for i in range(100000) %}{{ (i in l) }}{% endfor %}'
    ),
    'indexing a large string': (
        "{% set s = 'x' * 5000000 %}{% for i in range(100000) %}{{ s[i] }}{% endfor %}"
    ),
}


@pytest.mark.parametrize('name', list(HOSTILE))
def test_a_hostile_template_gives_text_or_an_error(name: str) -> None:
    kind, _ = text_or_error(HOSTILE[name])
    assert kind in ('text', 'error')


def test_a_list_that_holds_itself_prints_as_python_prints_it() -> None:
    assert render('{% set l = [1] %}{% do l.append(l) %}{{ l }}') == ('text', '[1, [...]]')
    assert render('{% set d = {} %}{% do d.update(me=d) %}{{ d }}') == ('text', "{'me': {...}}")


def test_values_of_python_that_a_list_may_take_are_still_taken() -> None:
    assert render('{% set l = [1] %}{% do l.extend(l) %}{{ l }}') == ('text', '[1, 1]')
    assert render('{% set l = [1, 2, 1] %}{{ l.index(1) }}{{ l.count(1) }}{% do l.remove(1) %}{{ l }}') == (
        'text',
        '02[2, 1]',
    )


def test_integers_overflow_as_python_never_does_but_an_error_is_an_error() -> None:
    assert render('{{ (-(2 ** 127)) // -1 }}')[0] == 'error'
    assert render('{{ 7 // -2 }}{{ -7 % 3 }}') == ('text', '-42')
    assert render("{{ ('ab' * (2 ** 126 + 2 ** 40)) | length }}")[0] == 'error'
    assert render('{{ (-(2 ** 127)) | abs }}')[0] == 'error'


def test_integers_and_floats_are_compared_exactly() -> None:
    assert render(
        '{{ 2 ** 100 == 2.0 ** 100 }}{{ 2 ** 100 + 1 > 2.0 ** 100 }}{{ 2 ** 100 + 1 == 2.0 ** 100 }}'
    ) == (
        'text',
        'TrueTrueFalse',
    )
    # Equal as numbers, so the sort keeps them in the order they came in.
    assert render('{{ [2 ** 100 + 1, 2.0 ** 100, 2 ** 100] | sort }}') == (
        'text',
        '[1.2676506002282294e+30, 1267650600228229401496703205376, 1267650600228229401496703205377]',
    )


def test_sorting_stops_at_the_first_pair_that_cannot_be_ordered() -> None:
    assert render("{{ [3, 'a', 1] | sort }}")[0] == 'error'
    assert render("{% set l = [3, 'a', 1] %}{% do l.sort() %}")[0] == 'error'


def test_a_slice_with_a_huge_step_takes_what_python_takes() -> None:
    assert render('{{ [1, 2, 3][::18446744073709551616] }}') == ('text', '[1]')
    assert render('{{ [1, 2, 3][::-18446744073709551616] }}') == ('text', '[3]')


def test_loop_reads_what_is_left_of_a_list_that_shrank() -> None:
    kind, _ = render(
        '{% set l = [1, 2, 3] %}{% for x in l %}{% set keep = loop %}{% do l.clear() %}{% endfor %}'
        '{{ keep.previtem }}|{{ keep.nextitem }}|{{ keep.revindex }}|{{ keep.revindex0 }}'
    )
    assert kind == 'text'


def test_the_widths_and_precisions_of_formats_are_bounded() -> None:
    for sql in (
        "{{ 'a'.ljust(10 ** 13) }}",
        "{{ '{:>10000000000000}'.format('a') }}",
        "{{ '%10000000000000d' % 1 }}",
        '{{ 1.5 | round(10 ** 13) }}',
    ):
        assert render(sql)[0] == 'error', sql


def test_a_template_that_builds_a_huge_string_stops() -> None:
    assert render("{% set s = 'x' %}{% for i in range(60) %}{% set s = s + s %}{% endfor %}")[0] == 'error'
    assert render("{% set s = 'x' * 1000000 %}{% for i in range(10000) %}{{ s }}{% endfor %}")[0] == 'error'


def test_a_template_that_loops_without_end_stops_quickly() -> None:
    started = time.perf_counter()
    assert (
        render('{% for a in range(100000) %}{% for b in range(100000) %}{% endfor %}{% endfor %}')[0]
        == 'error'
    )
    assert time.perf_counter() - started < 10


def test_the_work_of_every_render_of_a_renderer_adds_up() -> None:
    def work() -> Outcome:
        renderer = Renderer(Path('/nonexistent'), {})
        failures = 0
        for _ in range(800):
            try:
                renderer.render('{% for i in range(100000) %}{% endfor %}')
            except ValueError:
                failures += 1
        return 'text', str(failures)

    kind, failures = on_a_small_stack(work)
    # Each render is within its own limit; together they are not, and the last ones fail at once.
    assert kind == 'text'
    assert int(failures) > 0


def test_macros_nest_to_a_depth_python_allows_on_a_small_stack() -> None:
    sql = '{% macro r(n) %}{% if n > 0 %}{{ r(n - 1) }}{% endif %}x{% endmacro %}{{ r(40) | length }}'
    assert render(sql) == ('text', '41')


def test_target_is_the_same_for_every_model() -> None:
    renderer = Renderer(Path('/nonexistent'), {'metadata': {'adapter_type': 'duckdb'}})
    with contextlib.suppress(ValueError):
        renderer.render("{% do target.update({'leak': 1}) %}")
    assert renderer.render('[{{ target.leak }}]{{ target.type }}') == '[]duckdb'
    macros = Renderer(Path('/nonexistent'), {})
    assert macros.render('{{ target.type }}') == 'default'


def test_rendering_does_not_leak() -> None:
    renderer = Renderer(Path('/nonexistent'), {})
    sql = '{% macro m(x) %}{{ x }}{% endmacro %}{% for i in range(3) %}{{ m(i) }}{% endfor %}'
    for _ in range(2000):
        renderer.render(sql)
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    for _ in range(200_000):
        renderer.render(sql)
    after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    assert after - before < 20_000  # kilobytes


# Macro files


def project_with(tmp_path: Path) -> Path:
    (tmp_path / 'macros').mkdir()
    (tmp_path / 'macros' / 'ok.sql').write_text('{% macro ok() %}OK{% endmacro %}')
    return tmp_path


def test_macro_directories_are_not_followed_through_links(tmp_path: Path) -> None:
    root = project_with(tmp_path)
    (root / 'macros' / 'a').symlink_to('.', target_is_directory=True)
    (root / 'macros' / 'b').symlink_to('.', target_is_directory=True)
    (root / 'macros' / 'r').symlink_to('/', target_is_directory=True)
    started = time.perf_counter()
    assert Renderer(root, {}).render('{{ ok() }}') == 'OK'
    assert time.perf_counter() - started < 5


def test_a_macro_file_that_is_a_link_out_of_the_project_is_not_read(tmp_path: Path) -> None:
    outside = tmp_path / 'outside.sql'
    outside.write_text('{% macro stolen() %}SECRET{% endmacro %}')
    root = tmp_path / 'project'
    (root / 'macros').mkdir(parents=True)
    (root / 'macros' / 'ok.sql').write_text('{% macro ok() %}OK{% endmacro %}')
    (root / 'macros' / 'link.sql').symlink_to(outside)
    rendered = Renderer(root, {}).render('{{ ok() }}{{ stolen() }}')
    assert rendered == 'OK'


def test_a_macro_file_that_is_a_link_inside_the_project_is_read(tmp_path: Path) -> None:
    root = project_with(tmp_path)
    (root / 'real.sql').write_text('{% macro inside() %}IN{% endmacro %}')
    (root / 'macros' / 'link.sql').symlink_to(root / 'real.sql')
    assert Renderer(root, {}).render('{{ inside() }}') == 'IN'


def test_a_macro_file_that_is_not_a_regular_file_is_not_read(tmp_path: Path) -> None:
    root = project_with(tmp_path)
    (root / 'macros' / 'zero.sql').symlink_to('/dev/zero')
    os.mkfifo(root / 'macros' / 'pipe.sql')
    started = time.perf_counter()
    assert Renderer(root, {}).render('{{ ok() }}') == 'OK'
    assert time.perf_counter() - started < 5


def test_a_macro_file_that_is_not_a_regular_file_is_not_read_in_a_trusted_project(tmp_path: Path) -> None:
    # With `keep` the project is trusted to link outside, but never to a device or a pipe.
    root = project_with(tmp_path)
    (root / 'macros' / 'zero.sql').symlink_to('/dev/zero')
    os.mkfifo(root / 'macros' / 'pipe.sql')
    started = time.perf_counter()
    assert Renderer(root, {}, keep=lambda paths: list(paths)).render('{{ ok() }}') == 'OK'
    assert time.perf_counter() - started < 5


def test_the_random_filter_is_refused() -> None:
    outcome = render('{{ [1, 2, 3] | random }}')
    assert outcome[0] == 'error' and "no filter named 'random'" in outcome[1]


def test_a_macro_path_outside_the_project_is_not_read(tmp_path: Path) -> None:
    root = tmp_path / 'project'
    root.mkdir()
    shared = tmp_path / 'shared'
    shared.mkdir()
    (shared / 'm.sql').write_text('{% macro shared() %}S{% endmacro %}')
    (root / 'macros').mkdir()
    for path in (shared, root / '..' / 'shared'):
        assert Renderer(root, {}, macro_paths=[path]).render('{{ shared() }}') == ''


# Manifests


def project_with_manifest(
    tmp_path: Path, nodes: dict[str, Any], **extra: Any
) -> tuple[dbt.DbtProject, Graph]:
    (tmp_path / 'target').mkdir()
    (tmp_path / 'dbt_project.yml').write_text('name: p\n')
    manifest = {'metadata': {'adapter_type': 'duckdb'}, 'nodes': nodes, **extra}
    (tmp_path / 'target' / 'manifest.json').write_text(json.dumps(manifest))
    graph = Graph()
    graph.meta['root'] = str(tmp_path)
    return dbt.find_projects(tmp_path)[0], graph


def model(name: str, **extra: Any) -> dict[str, Any]:
    return {
        'resource_type': 'model',
        'unique_id': f'model.p.{name}',
        'name': name,
        'package_name': 'p',
        'relation_name': f'"db"."main"."{name}"',
        'compiled_code': 'select 1',
        **extra,
    }


@pytest.mark.parametrize(
    'extra',
    [
        {'depends_on': {'nodes': 'model.p.aA'}},
        {'depends_on': {'nodes': {'model.p.a': 1}}},
        {'depends_on': 'model.p.a'},
        {'columns': 'abc'},
        {'columns': ['a', 'b']},
        {'constraints': 'primary_key'},
        {'constraints': {'type': 'primary_key'}},
        {'constraints': [{'type': 'primary_key', 'columns': 'abc'}]},
        {'columns': {'a': {'constraints': 'primary_key'}}},
        {'columns': {'a': {'constraints': {'type': 'primary_key'}}}},
        {'fqn': 'abc', 'tags': 'abc'},
        {'config': 'abc'},
        {'config': {'contract': 'abc', 'materialized': 'incremental', 'unique_key': 'abc'}},
        {'config': ['abc']},
        {'checksum': 'abc'},
    ],
)
def test_a_manifest_with_values_of_the_wrong_kind_adds_no_edges_it_did_not_say(
    tmp_path: Path, extra: dict[str, Any]
) -> None:
    project, graph = project_with_manifest(tmp_path, {'model.p.m': model('m', **extra)})
    dbt.load(project, graph, dialect=None)
    assert not [e for e in graph.edges if e.kind == 'feeds']


@pytest.mark.parametrize(
    'sections',
    [
        {'sources': 'abc'},
        {'sources': ['x']},
        {'exposures': 'abc'},
        {'exposures': {'e': {'unique_id': 'exposure.p.e', 'name': 'e', 'depends_on': {'nodes': 'ab'}}}},
        {'exposures': {'e': 'x'}},
        {'docs': 'abc'},
        {'macros': 'abc'},
        {'semantic_models': 'abc', 'metrics': 'abc'},
        {'metadata': 'abc'},
    ],
)
def test_sections_of_the_wrong_kind_are_ignored(tmp_path: Path, sections: dict[str, Any]) -> None:
    project, graph = project_with_manifest(tmp_path, {'model.p.m': model('m')}, **sections)
    dbt.load(project, graph, dialect=None)
    assert 'model.p.m' in graph.nodes


def test_a_test_whose_depends_on_is_a_string_makes_no_edges(tmp_path: Path) -> None:
    test = {
        'resource_type': 'test',
        'unique_id': 'test.p.t',
        'name': 't',
        'depends_on': {'nodes': 'model.p.m'},
        'test_metadata': 'x',
    }
    project, graph = project_with_manifest(tmp_path, {'model.p.m': model('m'), 'test.p.t': test})
    dbt.load(project, graph, dialect=None)
    assert not [e for e in graph.edges if e.kind == 'tests']


def test_a_node_whose_id_is_not_text_is_refused(tmp_path: Path) -> None:
    project, graph = project_with_manifest(tmp_path, {'model.p.m': model('m', unique_id=5)})
    with pytest.raises((TypeError, ValueError)):
        dbt.load(project, graph, dialect=None)


def test_a_catalog_of_many_nodes_is_read_in_a_bounded_time(tmp_path: Path) -> None:
    count = 3000
    nodes = {f'model.p.m{i}': model(f'm{i}') for i in range(count)}
    project, graph = project_with_manifest(tmp_path, nodes)
    columns = {f'c{j}': {'name': f'c{j}', 'type': 'int', 'index': j} for j in range(20)}
    catalog = {'nodes': {f'model.p.m{i}': {'columns': columns} for i in range(count)}}
    (tmp_path / 'target' / 'catalog.json').write_text(json.dumps(catalog))
    started = time.perf_counter()
    dbt.load(project, graph, dialect=None)
    assert time.perf_counter() - started < 30, 'the catalog maps are not linear'
    assert sum(1 for n in graph.nodes.values() if n.kind == 'column') >= count * 20


def test_the_last_of_a_repeated_key_wins_as_json_loads_has_it(tmp_path: Path) -> None:
    (tmp_path / 'target').mkdir()
    (tmp_path / 'dbt_project.yml').write_text('name: p\n')
    one = json.dumps(model('m', description='first'))
    two = json.dumps(model('m', description='second'))
    (tmp_path / 'target' / 'manifest.json').write_text(
        '{"metadata": {"adapter_type": "duckdb"}, "nodes": {"model.p.m": '
        + one
        + ', "model.p.m": '
        + two
        + '}}'
    )
    graph = Graph()
    graph.meta['root'] = str(tmp_path)
    dbt.load(dbt.find_projects(tmp_path)[0], graph, dialect=None)
    assert graph.nodes['model.p.m'].description == 'second'
    assert [n for n in graph.nodes if n.startswith('model.')] == ['model.p.m']


def test_integers_beyond_sixty_four_bits_stay_integers(tmp_path: Path) -> None:
    project, graph = project_with_manifest(
        tmp_path, {'model.p.m': model('m', tags=[123456789012345678901234567890, 1.5, True])}
    )
    dbt.load(project, graph, dialect=None)
    assert graph.nodes['model.p.m'].attrs['tags'] == [123456789012345678901234567890, 1.5, True]


def test_nan_and_infinity_are_read_as_json_loads_reads_them(tmp_path: Path) -> None:
    (tmp_path / 'target').mkdir()
    (tmp_path / 'dbt_project.yml').write_text('name: p\n')
    (tmp_path / 'target' / 'manifest.json').write_text(
        '{"metadata": {"adapter_type": "duckdb"}, "nodes": {"model.p.m": {"resource_type": "model", '
        '"unique_id": "model.p.m", "name": "m", "relation_name": "\\"db\\".\\"main\\".\\"m\\"", '
        '"compiled_code": "select 1", "tags": [NaN, Infinity, -Infinity, 1e999], "meta": {"x": NaN}}}}'
    )
    graph = Graph()
    graph.meta['root'] = str(tmp_path)
    assert dbt.load(dbt.find_projects(tmp_path)[0], graph, dialect=None) is not None
    tags = graph.nodes['model.p.m'].attrs['tags']
    assert tags[0] != tags[0]
    assert tags[1:] == [float('inf'), float('-inf'), float('inf')]


# Random templates


GRAMMAR = [
    '{{ ',
    ' }}',
    '{% ',
    ' %}',
    '{# ',
    ' #}',
    'set ',
    'if ',
    'else',
    'endif',
    'for ',
    'in ',
    'endfor',
    'macro ',
    'endmacro',
    'call ',
    'endcall',
    'do ',
    'with ',
    'endwith',
    'raw',
    'endraw',
    'filter ',
    'endfilter',
    'x',
    'y',
    'l',
    'd',
    'ns',
    'loop',
    'caller',
    'range(',
    'namespace(',
    'dict(',
    '[',
    ']',
    '{',
    '}',
    '(',
    ')',
    ',',
    ':',
    '.',
    '|',
    '~',
    '+',
    '-',
    '*',
    '/',
    '//',
    '%',
    '**',
    '==',
    '<',
    '>',
    '=',
    ' and ',
    ' or ',
    ' not ',
    ' is ',
    ' if ',
    ' else ',
    "'a'",
    '"b"',
    '1',
    '0',
    '2 ** 64',
    '-1',
    '1.5',
    'none',
    'true',
    'upper',
    'length',
    'join',
    'sort',
    'unique',
    'tojson',
    'format',
    'center',
    'indent',
    'batch',
    'slice',
    'round',
    'replace',
    'sum',
    'min',
    'max',
    'items',
    'append',
    'extend',
    'update',
    'pop',
    'split',
    'ljust',
    'zfill',
    'recursive',
    ' ',
    '\n',
]


def fuzz_templates(count: int, seed: int) -> list[str]:
    from tests.jinja_corpus import CASES

    chance = random.Random(seed)
    corpus = [sql for sql, _ in CASES.values()]
    found = []
    for _ in range(count):
        if chance.random() < 0.5:
            found.append(''.join(chance.choice(GRAMMAR) for _ in range(chance.randint(1, 40))))
        else:
            text = list(chance.choice(corpus))
            for _ in range(chance.randint(1, 4)):
                at = chance.randrange(len(text) + 1)
                action = chance.random()
                if action < 0.4 and text:
                    del text[min(at, len(text) - 1)]
                elif action < 0.8:
                    text.insert(at, chance.choice(GRAMMAR))
                elif text:
                    other = chance.choice(corpus)
                    text[at : at + chance.randint(0, 5)] = other[: chance.randint(0, 20)]
            found.append(''.join(text))
    return found


def test_random_templates_give_text_or_an_error_on_a_small_stack() -> None:
    count = int(os.environ.get('KONSTRUCT_FUZZ_ITERATIONS', '3000'))

    def work() -> Outcome:
        renderer = Renderer(Path('/nonexistent'), {})
        outcomes = {'text': 0, 'error': 0}
        for sql in fuzz_templates(count, seed=7):
            try:
                renderer.render(sql)
                outcomes['text'] += 1
            except ValueError:
                outcomes['error'] += 1
        return 'text', json.dumps(outcomes)

    kind, outcomes = on_a_small_stack(work)
    found = json.loads(outcomes)
    print(f'{count} random templates: {found["text"]} rendered, {found["error"]} raised ValueError')
    assert kind == 'text'
    assert found['text'] + found['error'] == count
    assert found['text'] > 0
