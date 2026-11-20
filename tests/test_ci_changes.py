"""Which CI jobs a change runs (.kodex/scripts/ci_changes.py).

A job that guards a release runs unless every changed path is one it is known not to read, so a
path no list names runs it; an optional job runs when the change touches a path it lists. The
patterns name files that exist, the workflows ask only for areas the script writes and use every
one, and the Fuzz workflow's own paths are its harnesses' areas.
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from types import ModuleType

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / '.kodex' / 'scripts' / 'ci_changes.py'
WORKFLOWS = REPO / '.github' / 'workflows'


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location('ci_changes', SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


changes = _load()


def tracked() -> list[str]:
    listed = subprocess.run(['git', 'ls-files'], cwd=REPO, check=True, capture_output=True, text=True)
    return listed.stdout.splitlines()


def on(paths: list[str]) -> set[str]:
    return {name for name, needed in changes.areas(paths).items() if needed}


GUARDED = (globs for pair in changes.GUARDS.values() for globs in pair)
LISTS = (*GUARDED, *changes.AREAS.values(), changes.CI)
# Where composite actions of the workflows would go: none is written yet, and a CI pattern that
# names nothing only waits, as it can turn jobs on and never off.
TO_COME = {'.github/actions/**'}
GLOBS = sorted({glob for globs in LISTS for glob in globs} - TO_COME)


@pytest.mark.parametrize('glob', GLOBS)
def test_every_pattern_names_a_file(glob: str) -> None:
    """A pattern that names nothing, after a rename, would keep an optional job from running, or
    stop saving a guarding job's run for a change it does not read."""
    assert any(changes.pattern(glob).fullmatch(path) for path in tracked()), glob


def test_the_patterns_to_come_are_ci_and_name_nothing_yet() -> None:
    """Once a composite action is written, its pattern joins the ones above."""
    assert set(changes.CI) >= TO_COME
    assert not any(changes.pattern(glob).fullmatch(path) for glob in TO_COME for path in tracked())


@pytest.mark.parametrize(
    ('glob', 'path', 'matched'),
    [
        ('*.json', 'package.json', True),
        ('*.json', 'docs/conf.json', False),
        ('*.md', 'CHANGELOG.md', True),
        ('*.md', 'konstruct/interfaces/toolkit/skill.md', False),
        ('src/**', 'src/yaml/scan.rs', True),
        ('konstruct/**.py', 'konstruct/analysis/query.py', True),
        ('konstruct/**.py', 'konstruct/outputs/viewer.html', False),
        ('tests/*.rs', 'tests/serve.rs', True),
        ('tests/*.rs', 'tests/fixtures/x.rs', False),
        ('**.py', 'setup.py', True),
    ],
)
def test_a_pattern_is_a_glob_over_the_whole_path(glob: str, path: str, matched: bool) -> None:
    assert bool(changes.pattern(glob).fullmatch(path)) is matched


# Each Rust source that reaches outside its own directory through CARGO_MANIFEST_DIR, in any form
# (env!, option_env!, std::env::var), with the files it reads there.
MANIFEST_DIR = {'src/identity/blake2b/mod.rs': {'tests/fixtures/blake2b-kat.txt'}}


def test_the_crate_reads_only_files_cargo_includes() -> None:
    """A Rust source that names CARGO_MANIFEST_DIR is listed above with what it reads, and each
    file it reads is one Cargo.toml includes in the crate, which the Rust jobs keep: a new use fails
    here until it is listed, whatever form it takes."""
    include = tomllib.loads((REPO / 'Cargo.toml').read_text())['package']['include']
    assert tuple(entry.lstrip('/') for entry in include) == changes.CRATE
    sources = [
        *(REPO / 'src').rglob('*.rs'),
        *(REPO / 'tests').glob('*.rs'),
        *(REPO / 'benches').rglob('*.rs'),
    ]
    naming = {
        str(source.relative_to(REPO)) for source in sources if 'CARGO_MANIFEST_DIR' in source.read_text()
    }
    assert naming == set(MANIFEST_DIR)
    for source, read in MANIFEST_DIR.items():
        text = (REPO / source).read_text()
        for path in read:
            assert f'"/{path}"' in text, (source, path)
            assert changes.matches(path, changes.CRATE), path
            assert on([path]) >= {'rust', 'wheels'}, path


def test_the_workflows_ask_only_for_areas_the_script_writes_and_use_every_one() -> None:
    used: set[str] = set()
    extra = {'full', 'pythons_matrix', 'targets'}
    for workflow in sorted(WORKFLOWS.glob('*.yml')):
        text = workflow.read_text()
        jobs = yaml.safe_load(text)['jobs']
        asked = set(re.findall(r'needs\.changes\.outputs\.(\w+)', text))
        declared = set(jobs['changes']['outputs']) if 'changes' in jobs else set()
        assert asked <= declared, (workflow.name, asked - declared)
        assert declared <= {*changes.NAMES, *extra}, workflow.name
        used |= asked | set(re.findall(r'(\w+)=true', text))
        # The Fuzz workflow names each harness with its seeds, as `sql=*.sql`.
        used |= {f'fuzz_{target}' for target in re.findall(r"(?:\$'|\\n)(\w+)=\*\.", text)}
    assert set(changes.NAMES) <= used, set(changes.NAMES) - used


def test_the_fuzz_workflow_runs_on_its_harnesses_paths() -> None:
    """GitHub filters the Fuzz workflow by its own list, before the script picks the harnesses."""
    fuzz = yaml.safe_load((WORKFLOWS / 'fuzz.yml').read_text())
    globs = {glob for name, globs in changes.AREAS.items() if name.startswith('fuzz_') for glob in globs}
    assert fuzz[True]['pull_request']['paths'] == sorted(globs | set(changes.CI))


PYTHON = {
    'package',
    'realworld',
    'end_to_end',
    'action',
    'airflow',
    'dagster',
    'trino',
    'snowflake',
    'databricks',
}
RUST = {'rust', 'wheels'}
GUARDS = PYTHON | RUST
FUZZED = {'fuzz_sql', 'fuzz_python', 'fuzz_yaml', 'fuzz_ingestion', 'fuzz_jinja', 'fuzz_manifest'}
CODEQL = {'codeql_python', 'codeql_javascript', 'codeql_actions', 'codeql_rust'}


def test_the_guards_are_the_jobs_that_hold_a_release() -> None:
    assert set(changes.GUARDS) == GUARDS


@pytest.mark.parametrize(
    ('paths', 'expected'),
    [
        (['docs/development.md'], {'docs'}),
        (['CHANGELOG.md'], {'docs'}),
        (['AGENTS.md'], set()),
        (['paper/sections/intro.tex'], {'paper'}),
        (['README.md'], {'package', 'wheels', 'action', 'docs'}),
        (['konstruct/analysis/query.py'], {*PYTHON, 'docs', 'codeql_python'}),
        (['konstruct/inputs/spark.py'], {*PYTHON, 'docs', 'codeql_python', 'fuzz_python'}),
        (['konstruct/_core.pyi'], {*PYTHON, 'docs'}),
        (['konstruct/outputs/viewer.html'], {*PYTHON, 'docs', 'viewer'}),
        (['src/airflow/mod.rs'], {*GUARDS, 'docs', 'codeql_rust', *FUZZED}),
        (['src/substring/x86_64.asm'], {*GUARDS, 'docs', 'codeql_rust', *FUZZED}),
        (['tests/serve.rs'], {*GUARDS, 'codeql_rust'}),
        (['tests/fixtures/blake2b-kat.txt'], GUARDS),
        (['tests/test_trino.py'], {*PYTHON, 'codeql_python'}),
        (['tests/fixtures/dagster/definitions.py'], {*PYTHON, 'codeql_python'}),
        (['examples/jaffle_shop/models/orders.sql'], {*PYTHON, 'docs'}),
        (['viewer/src/App.svelte'], {'viewer', 'codeql_javascript'}),
        (['tsconfig.json'], {*PYTHON, 'viewer'}),
        (['action.yml'], {*PYTHON, 'codeql_actions'}),
        (['.github/dependabot.yml'], {'codeql_actions'}),
        (['pyproject.toml'], {*PYTHON, 'wheels', 'pythons', 'docs', 'paper', *FUZZED}),
        (['uv.lock'], {*PYTHON, 'pythons', 'docs', 'paper', *FUZZED}),
        (['docs/index.md', 'paper/konstruct.tex'], {'docs', 'paper'}),
        (['docs/index.md', 'konstruct/core/graph.py'], {*PYTHON, 'docs', 'codeql_python', *FUZZED}),
    ],
)
def test_a_change_runs_the_jobs_that_exercise_it(paths: list[str], expected: set[str]) -> None:
    assert on(paths) == expected


@pytest.mark.parametrize(
    'path',
    [
        'build.rs',
        '.cargo/config.toml',
        'rust-toolchain.toml',
        'clippy.toml',
        'rustfmt.toml',
        'tests/fixtures/new-vectors.bin',
        'something/new.txt',
    ],
)
def test_a_path_no_list_names_runs_every_job_that_holds_a_release(path: str) -> None:
    """The guards fail closed: a file that is not known to be unread by a job runs the job."""
    assert on([path]) >= GUARDS - (RUST if path.startswith('tests/fixtures/') else set())
    if path.startswith('tests/fixtures/'):
        return
    assert on([path]) >= RUST


@pytest.mark.parametrize(
    'path',
    [
        '.github/workflows/build-and-test.yml',
        '.github/actions/setup/action.yml',
        '.kodex/scripts/ci_changes.py',
        '.kodex/scripts/ci_tested.sh',
    ],
)
def test_a_change_to_ci_itself_runs_everything(path: str) -> None:
    assert on([path]) == set(changes.NAMES)


def test_no_change_runs_nothing() -> None:
    assert on([]) == set()


def run(*args: str, input: bytes, output: Path) -> list[str]:
    env = {**os.environ, 'GITHUB_OUTPUT': str(output)}
    subprocess.run(
        [sys.executable, str(SCRIPT), *args], input=input, env=env, check=True, capture_output=True
    )
    return output.read_text().splitlines()


def test_the_script_writes_every_area_for_actions(tmp_path: Path) -> None:
    output = tmp_path / 'output'
    written = dict(line.split('=') for line in run(input=b'Cargo.lock\n', output=output))
    assert written == {name: str(name in on(['Cargo.lock'])).lower() for name in changes.NAMES}
    assert run('--all', input=b'', output=output)[len(changes.NAMES) :] == [
        f'{n}=true' for n in changes.NAMES
    ]


def test_the_script_reads_paths_separated_by_nul(tmp_path: Path) -> None:
    """`git diff -z` writes each path as it is, so a name with a newline or a non-ASCII letter is
    read whole rather than quoted."""
    paths = ['docs/café.md', 'konstruct/core/a\nb.py']
    given = b'\0'.join(path.encode() for path in paths) + b'\0'
    written = dict(line.split('=', 1) for line in run('-z', input=given, output=tmp_path / 'output'))
    assert written == {name: str(name in on(paths)).lower() for name in changes.NAMES}
    assert written['codeql_python'] == 'true'


@pytest.mark.parametrize(
    ('path', 'jobs'),
    [
        ('src/substring/aarch64.asm', {'airflow', 'dagster'}),
        ('tests/conftest.py', {'dagster'}),
        ('examples/jaffle_shop/dbt_project.yml', {'dagster'}),
        ('konstruct/inputs/dbt.py', {'dagster'}),
        ('konstruct/engine/build.py', {'dagster', 'trino', 'snowflake', 'databricks'}),
        ('konstruct/core/sql.py', {'trino', 'snowflake', 'databricks'}),
        ('konstruct/inputs/jinja.py', {'trino', 'snowflake', 'databricks'}),
        ('tests/test_warehouse.py', {'snowflake'}),
    ],
)
def test_what_a_parity_or_connector_test_reads_through_runs_it(path: str, jobs: set[str]) -> None:
    """Paths the include lists these jobs once had missed, each read on the way to the job's test."""
    assert on([path]) >= jobs
