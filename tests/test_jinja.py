"""The dbt Jinja renderer: a corpus of real macro patterns, the sandbox's refusals, and a differential
check against the Jinja2 renderer that came before it."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from konstruct.inputs.jinja import Renderer
from tests import jinja_oracle
from tests.conftest import copy_example, dbt_executable, run_dbt
from tests.jinja_corpus import CASES, MANIFEST

GOLDEN = Path(__file__).parent / 'golden' / 'jinja_corpus.json'


def project(root: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
    return root


def render(factory: type, root: Path, files: dict[str, str], sql: str) -> str:
    """The rendered text, or ``'!error'`` when rendering fails (the build then falls back)."""
    renderer = factory(project(root, files), MANIFEST)
    try:
        return str(renderer.render(sql))
    except Exception:
        return '!error'


def expected() -> dict[str, str]:
    loaded: dict[str, str] = json.loads(GOLDEN.read_text(encoding='utf-8'))
    return loaded


if os.environ.get('KONSTRUCT_RECORD_JINJA'):

    def test_record(tmp_path: Path) -> None:
        recorded = {}
        for name, (sql, files) in CASES.items():
            recorded[name] = render(jinja_oracle.Renderer, tmp_path / name, files, sql)
        GOLDEN.write_text(json.dumps(recorded, indent=1, ensure_ascii=False) + '\n', encoding='utf-8')

else:

    @pytest.mark.parametrize('name', list(CASES))
    def test_corpus_matches_the_recorded_output(name: str, tmp_path: Path) -> None:
        sql, files = CASES[name]
        assert render(Renderer, tmp_path, files, sql) == expected()[name]


def test_a_macro_file_that_does_not_parse_drops_its_whole_package(tmp_path: Path) -> None:
    project(
        tmp_path,
        {
            'macros/a.sql': '{% macro a() %}A{% endmacro %}',
            'macros/b.sql': '{% macro b() %}{% if %}{% endmacro %}',
        },
    )
    renderer = Renderer(tmp_path, MANIFEST)
    assert renderer.render('{{ a() }}|{{ a(1) }}') == '|konstruct_macro(1)'


def test_keep_chooses_which_macro_files_are_read(tmp_path: Path) -> None:
    project(
        tmp_path,
        {'macros/a.sql': '{% macro a() %}A{% endmacro %}', 'macros/b.sql': '{% macro b() %}B{% endmacro %}'},
    )
    renderer = Renderer(tmp_path, MANIFEST, keep=lambda files: [f for f in files if f.name == 'a.sql'])
    assert renderer.render('{{ a() }}{{ b(1) }}') == 'Akonstruct_macro(1)'


def test_macro_paths_replace_the_default_macros_directory(tmp_path: Path) -> None:
    project(
        tmp_path,
        {'lib/a.sql': '{% macro a() %}A{% endmacro %}', 'macros/b.sql': '{% macro b() %}B{% endmacro %}'},
    )
    renderer = Renderer(tmp_path, MANIFEST, macro_paths=[tmp_path / 'lib'])
    assert renderer.render('{{ a() }}{{ b(1) }}') == 'Akonstruct_macro(1)'


def test_the_adapter_type_chooses_the_dispatched_macro(tmp_path: Path) -> None:
    project(
        tmp_path,
        {
            'macros/a.sql': "{% macro f() %}{{ adapter.dispatch('f')() }}{% endmacro %}"
            '{% macro default__f() %}D{% endmacro %}{% macro snowflake__f() %}S{% endmacro %}'
        },
    )
    assert Renderer(tmp_path, MANIFEST, adapter_type='snowflake').render('{{ f() }}') == 'S'
    assert Renderer(tmp_path, MANIFEST, adapter_type='postgres').render('{{ f() }}') == 'D'
    assert Renderer(tmp_path, MANIFEST).render('{{ f() }}') == 'D'  # the manifest says duckdb


# The Rust renderer against the Jinja2 one, on every model of a project that dbt has only parsed.


def parsed_manifest(project: Path) -> dict[str, Any]:
    run_dbt(project, 'parse')
    loaded: dict[str, Any] = json.loads((project / 'target' / 'manifest.json').read_text(encoding='utf-8'))
    return loaded


def assert_same_on_every_model(project: Path, manifest: dict[str, Any]) -> int:
    macros = [project / 'macros']
    new = Renderer(project, manifest, macro_paths=macros)
    old = jinja_oracle.Renderer(project, manifest, macro_paths=macros)
    checked = 0
    for node in manifest['nodes'].values():
        raw = node.get('raw_code')
        if node.get('resource_type') not in ('model', 'snapshot') or not raw:
            continue
        try:
            expected = old.render(raw)
        except Exception:
            with pytest.raises(ValueError):
                new.render(raw)
        else:
            assert new.render(raw) == expected, node['unique_id']
        checked += 1
    return checked


@pytest.mark.dbt
def test_matches_the_oracle_on_every_model_of_jaffle_shop(tmp_path: Path) -> None:
    if dbt_executable() is None:
        pytest.skip('dbt is not installed')
    project = copy_example(tmp_path)
    manifest = parsed_manifest(project)
    assert assert_same_on_every_model(project, manifest) >= 3


@pytest.mark.dbt
def test_matches_the_oracle_on_a_generated_project(tmp_path: Path) -> None:
    if dbt_executable() is None:
        pytest.skip('dbt is not installed')
    project = tmp_path / 'generated'
    write_dbt_project(project, models=60)
    manifest = parsed_manifest(project)
    assert assert_same_on_every_model(project, manifest) == 60


def write_dbt_project(root: Path, models: int) -> None:
    """A dbt project in the shape of ``tests/test_scale.py``'s, written as dbt writes models: ``ref``
    and ``source``, ``config``, a macro, ``var`` and ``is_incremental``."""
    (root / 'models').mkdir(parents=True)
    (root / 'macros').mkdir()
    (root / 'dbt_project.yml').write_text(
        "name: gen\nprofile: gen\nconfig-version: 2\nmodel-paths: ['models']\nmacro-paths: ['macros']\n"
    )
    (root / 'profiles.yml').write_text(
        'gen:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n      path: gen.duckdb\n'
    )
    (root / 'macros' / 'double.sql').write_text('{% macro double(c) %}({{ c }} * 2){% endmacro %}\n')
    columns = [f'c{j:02}' for j in range(8)]
    sources = ['version: 2', 'sources:', '  - name: raw', '    schema: raw', '    tables:']
    sources += [f'      - name: raw_m{index:03}' for index in range(10)]
    (root / 'models' / 'sources.yml').write_text('\n'.join(sources) + '\n')
    for index in range(models):
        if index < 10:
            sql = f"{{{{ config(materialized='table') }}}}\nselect {', '.join(columns)} from {{{{ source('raw', 'raw_m{index:03}') }}}}\n"
        else:
            picks = ', '.join(
                f'a.{c}'
                if j % 3 == 0
                else (f"{{{{ double('b.{c}') }}}} as {c}" if j % 3 == 1 else f'coalesce(a.{c}, b.{c}) as {c}')
                for j, c in enumerate(columns)
            )
            sql = (
                f"{{{{ config(materialized='table', tags=['g{index // 50}']) }}}}\n"
                f"with a as (select * from {{{{ ref('m{index // 2:03}') }}}}),\n b as (select * from {{{{ ref('m{index // 3:03}') }}}}),\n"
                f'joined as (select {picks} from a join b on a.c00 = b.c00\n'
                "{% if var('only_positive', true) %} where b.c01 > 0 {% endif %}\n"
                '{% if is_incremental() %} and a.c02 > (select max(c02) from {{ this }}) {% endif %})\n'
                'select * from joined\n'
            )
        (root / 'models' / f'm{index:03}.sql').write_text(sql)
