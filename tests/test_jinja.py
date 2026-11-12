"""The dbt Jinja renderer: a corpus of real macro patterns, the sandbox's refusals, and a differential
check against the Jinja2 renderer that came before it."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from konstruct.inputs.jinja import Renderer
from tests import jinja_oracle
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
