"""Which CI jobs a change needs.

Reads the paths a change touches, one per line on stdin (or separated by NUL with ``-z``, as
``git diff --name-only -z`` writes them), and writes ``<area>=true`` or ``<area>=false`` for each
area below to ``$GITHUB_OUTPUT`` (or to stdout outside Actions).

A job that guards a release fails closed: it runs unless every changed path is one it is known not
to read (``GUARDS``), so a file no list names runs it. An optional job (``AREAS``) runs when the
change touches a path it lists. A change to CI runs everything, and nightly, by hand and on a push
to main that no pull request tested, every area is on (``--all``). A pattern is a glob over the
repository's paths: ``*`` stays within one directory and ``**`` crosses them.
``tests/test_ci_changes.py`` checks that every pattern still names a file.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tomllib
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# A change to CI itself turns every area on, so that the jobs it may have changed are seen to run.
CI = (
    '.github/workflows/**',
    '.github/actions/**',
    '.kodex/scripts/ci_changes.py',
    '.kodex/scripts/ci_tested.sh',
)

# Every file Cargo.toml includes in the crate, which the Rust jobs and the wheels read whatever the
# lists below say: it is how the Rust core reads a file beside its sources (the BLAKE2b test
# vectors under tests/fixtures/).
CRATE = tuple(
    entry.lstrip('/') for entry in tomllib.loads((ROOT / 'Cargo.toml').read_text())['package']['include']
)

# The README is the package's long description, so the jobs that build the package read it.
README = ('README.md',)

# What no job that guards a release reads: the pages, the paper, the notes at the root (the
# README aside, for the jobs that build the package) and the repository's GitHub settings (its
# workflows and actions are CI, above).
UNREAD = ('docs/**', 'paper/**', '*.md', '.github/**')

# The Python jobs install the package and drive it. The viewer's source is built into
# konstruct/outputs/viewer.html, which they do read.
PYTHON_UNREAD = (*UNREAD, 'viewer/**')

# Cargo builds and tests the crate from the files it includes (CRATE) and its own configuration,
# so the Python package, its tests and fixtures, the examples, the viewer, the project's scripts
# and the other toolchains' files are known not to be read. Anything else, such as build.rs,
# .cargo/config.toml or rust-toolchain.toml, runs the Rust jobs.
CARGO_UNREAD = (
    *PYTHON_UNREAD,
    'konstruct/**',
    'tests/**.py',
    'tests/fixtures/**',
    'tests/golden/**',
    'tests/compat/**',
    'examples/**',
    '.kodex/**',
    '.claude/**',
    '.claude-plugin/**',
    'claude-code-plugin/**',
    'licenses/**',
    'pyproject.toml',
    'uv.lock',
    '.python-version',
    'package.json',
    'pnpm-lock.yaml',
    'tsconfig.json',
    'vite.config.ts',
    'svelte.config.js',
    '.prettierrc',
    '.prettierignore',
    '.nvmrc',
    '.pre-commit-config.yaml',
    'action.yml',
    'osv-scanner.toml',
    'CITATION.cff',
    'Makefile',
)

# A wheel is the crate built by maturin, whose settings are in pyproject.toml, with the licence
# texts. The Python it carries is checked on every push to main, which builds and imports the
# native wheels, so on a pull request the wheels follow the crate.
WHEEL_UNREAD = tuple(glob for glob in CARGO_UNREAD if glob not in ('pyproject.toml', 'licenses/**'))

# The jobs that hold a release, each with what it is known not to read and what it reads all the
# same. `rust` runs the Rust jobs: format, Clippy and tests, the oldest Rust, and the tests on
# each architecture under qemu. The wheels, the package and the Action build the package.
GUARDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    'rust': (CARGO_UNREAD, CRATE),
    'wheels': (WHEEL_UNREAD, (*CRATE, *README)),
    'package': (PYTHON_UNREAD, README),
    'realworld': (PYTHON_UNREAD, ()),
    'end_to_end': (PYTHON_UNREAD, ()),
    'action': (PYTHON_UNREAD, README),
    'airflow': (PYTHON_UNREAD, ()),
    'dagster': (PYTHON_UNREAD, ()),
    'trino': (PYTHON_UNREAD, ()),
    'snowflake': (PYTHON_UNREAD, ()),
    'databricks': (PYTHON_UNREAD, ()),
}

# What every fuzz harness reaches: the core modules, the Rust core and the locked parsers.
FUZZED = ('konstruct/core/**', 'src/**', 'Cargo.toml', 'Cargo.lock', 'pyproject.toml', 'uv.lock')

# The optional jobs, each on a change to a path it lists.
AREAS: dict[str, tuple[str, ...]] = {
    # The oldest and newest Python on a pull request, beside 3.12 on every change: dependencies
    # resolve and install per interpreter. Every push to main runs them.
    'pythons': ('pyproject.toml', 'uv.lock', '.python-version'),
    'viewer': (
        'viewer/**',
        'package.json',
        'pnpm-lock.yaml',
        '*.json',
        '*.ts',
        '*.js',
        '.prettierrc',
        '.prettierignore',
        '.nvmrc',
        'konstruct/outputs/viewer.html',
    ),
    # The Sphinx build: the pages, the files they include, the docstrings, command line and API
    # that autodoc and sphinx-argparse read (from the package, its stubs and the Rust core it
    # imports), the built viewer and the example the demo viewer is built from.
    'docs': (
        'docs/**',
        'README.md',
        'CHANGELOG.md',
        'CONTRIBUTING.md',
        'SECURITY.md',
        'konstruct/**',
        'src/**',
        'Cargo.toml',
        'Cargo.lock',
        'pyproject.toml',
        'uv.lock',
        'examples/jaffle_shop/**',
        '.kodex/scripts/prepare_example.py',
    ),
    # The paper and what paper/generate.py runs to measure it. The names it reads from the package
    # are type checked by mypy in Lint; the numbers that move with the engine are macros, so the PDF
    # still builds, and every push to main measures them again.
    'paper': (
        'paper/**',
        'tests/test_paper.py',
        'tests/test_ossie_conformance.py',
        '.kodex/scripts/benchmark.py',
        '.kodex/scripts/answer_benchmark.py',
        '.kodex/scripts/answer_questions/**',
        'konstruct/analysis/verify.py',
        'konstruct/semantics/ossie.py',
        'konstruct/semantics/registry.py',
        'konstruct/semantics/ontologies/**',
        'pyproject.toml',
        'uv.lock',
    ),
    # CodeQL analyses a language on a pull request that changes its files.
    'codeql_python': ('**.py',),
    'codeql_javascript': ('viewer/**', '*.ts', '*.js', 'package.json', 'pnpm-lock.yaml'),
    'codeql_actions': ('.github/**', 'action.yml'),
    'codeql_rust': ('src/**', 'Cargo.toml', 'Cargo.lock', 'benches/**', 'tests/*.rs'),
    # Each fuzz harness on a pull request that changes what it reaches: its readers, the core they
    # read through and the Rust core beneath it.
    'fuzz_sql': (*FUZZED, 'tests/fuzz_targets/fuzz_sql.py'),
    'fuzz_python': (
        *FUZZED,
        'konstruct/inputs/airflow.py',
        'konstruct/inputs/dagster.py',
        'konstruct/inputs/spark.py',
        'tests/fuzz_targets/fuzz_python.py',
    ),
    'fuzz_yaml': (*FUZZED, 'konstruct/semantics/**', 'tests/fuzz_targets/fuzz_yaml.py'),
    'fuzz_ingestion': (
        *FUZZED,
        'konstruct/inputs/ingestion.py',
        'konstruct/inputs/hcl.py',
        'tests/fuzz_targets/fuzz_ingestion.py',
    ),
    'fuzz_jinja': (*FUZZED, 'konstruct/inputs/jinja.py', 'tests/fuzz_targets/fuzz_jinja.py'),
    'fuzz_manifest': (*FUZZED, 'konstruct/inputs/dbt.py', 'tests/fuzz_targets/fuzz_manifest.py'),
}

NAMES = (*GUARDS, *AREAS)


@cache
def pattern(glob: str) -> re.Pattern[str]:
    """``glob`` as a regular expression over a whole path."""
    parts = re.split(r'(\*\*/|\*\*|\*|\?)', glob)
    wild = {'**/': '(?:.*/)?', '**': '.*', '*': '[^/]*', '?': '[^/]'}
    return re.compile(''.join(wild.get(part, re.escape(part)) for part in parts), re.DOTALL)


def matches(path: str, globs: tuple[str, ...]) -> bool:
    return any(pattern(glob).fullmatch(path) for glob in globs)


def areas(paths: list[str], everything: bool = False) -> dict[str, bool]:
    """Each area, and whether a change to ``paths`` needs it."""
    everything = everything or any(matches(path, CI) for path in paths)
    guarded = {
        name: everything or any(matches(path, kept) or not matches(path, unread) for path in paths)
        for name, (unread, kept) in GUARDS.items()
    }
    listed = {
        name: everything or any(matches(path, globs) for path in paths) for name, globs in AREAS.items()
    }
    return {**guarded, **listed}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--all', action='store_true', help='every area, whatever changed')
    parser.add_argument('-z', action='store_true', help='paths separated by NUL, as git diff -z writes them')
    args = parser.parse_args()
    paths: list[str] = []
    if not args.all:
        given = sys.stdin.buffer.read().decode('utf-8', 'surrogateescape')
        paths = [path for path in (given.split('\0') if args.z else given.splitlines()) if path.strip()]
    lines = [f'{name}={str(on).lower()}' for name, on in areas(paths, args.all).items()]
    output = os.environ.get('GITHUB_OUTPUT')
    if output:
        with Path(output).open('a', encoding='utf-8') as file:
            file.write(''.join(f'{line}\n' for line in lines))
    print('\n'.join(lines))
    return 0


if __name__ == '__main__':
    sys.exit(main())
