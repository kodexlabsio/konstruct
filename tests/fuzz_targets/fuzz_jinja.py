"""Fuzz the dbt Jinja renderer: for any template, with macros and packages of any text, a render
returns text or raises ``ValueError``, within its limits of time and memory, and never overflows the
stack or panics.

Run with ``uv run --with atheris==3.1.0 python tests/fuzz_targets/fuzz_jinja.py -max_total_time=60``; see
docs/development.md.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import atheris

with atheris.instrument_imports(include=['konstruct']):
    from konstruct.inputs.jinja import Renderer

ROOT = Path(tempfile.mkdtemp(prefix='konstruct-fuzz-jinja-'))
(ROOT / 'macros').mkdir()
(ROOT / 'dbt_packages' / 'pkg' / 'macros').mkdir(parents=True)
MANIFEST = {
    'metadata': {'adapter_type': 'duckdb', 'project_name': 'shop'},
    'nodes': {
        'model.shop.a': {
            'resource_type': 'model',
            'name': 'a',
            'package_name': 'shop',
            'relation_name': '"db"."main"."a"',
        }
    },
    'sources': {},
}
# Templates that reach each part of the grammar, which a mutating fuzzer starts from.
SEEDS = [
    "{% macro m(a, b=1) %}{{ a ~ b }}{% endmacro %}{{ m('x') }}{{ ref('a') }}",
    "{% for k, v in {'a': [1, 2]}.items() %}{{ loop.index }}{{ v | join(',') }}{% endfor %}",
    '{% set ns = namespace(n=0) %}{% for i in range(3) %}{% set ns.n = ns.n + i %}{% endfor %}{{ ns.n }}',
    "{% autoescape true %}{{ '<b>' | e }}{{ ['x'] | tojson }}{% endautoescape %}",
    '{% for x in [[1, [2]]] recursive %}{{ loop(x) if x is iterable else x }}{% endfor %}',
    "{{ adapter.dispatch('f')(1) }}{{ var('x', 1) }}{{ target.type }}{{ run_query('x') }}",
    "{{ 'a b'.split() }}{{ '%s' % 'x' }}{{ '{:>3}'.format(1) }}{{ 7 // 2 }}{{ 2 ** 10 }}",
]


def test_one_input(data: bytes) -> None:
    provider = atheris.FuzzedDataProvider(data)
    macros = provider.ConsumeUnicodeNoSurrogates(provider.ConsumeIntInRange(0, 200))
    package = provider.ConsumeUnicodeNoSurrogates(provider.ConsumeIntInRange(0, 200))
    template = provider.PickValueInList(SEEDS) + provider.ConsumeUnicodeNoSurrogates(
        provider.remaining_bytes()
    )
    (ROOT / 'macros' / 'm.sql').write_text(macros, encoding='utf-8')
    (ROOT / 'dbt_packages' / 'pkg' / 'macros' / 'p.sql').write_text(package, encoding='utf-8')
    try:
        renderer = Renderer(ROOT, MANIFEST)
        renderer.render(template)
        renderer.render(template[len(template) // 2 :])
    except ValueError:
        pass


if __name__ == '__main__':
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()
