"""Whether a push to main brings a tree a pull request's run passed (.kodex/scripts/ci_tested.sh),
with GitHub's answers and the runs' records from a stand-in for `gh`, and the pushed tree from a
throwaway repository."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / '.kodex' / 'scripts' / 'ci_tested.sh'
ENV = {
    'GIT_CONFIG_GLOBAL': os.devnull,
    'GIT_CONFIG_NOSYSTEM': '1',
    'GIT_AUTHOR_NAME': 't',
    'GIT_AUTHOR_EMAIL': 't@example.com',
    'GIT_COMMITTER_NAME': 't',
    'GIT_COMMITTER_EMAIL': 't@example.com',
}

# `gh api <endpoint> --jq <filter>` prints the answer of the first entry of GH_ANSWERS whose key
# is in the endpoint, and fails for an entry of null or an endpoint no key is in. `gh run download
# <id> ... -n <name> -D <dir>` writes the record GH_RECORDS has for `<id>/<name>` to
# <dir>/<name>.txt, and fails for a run and name it has none for.
GH = """\
import json, os, sys
from pathlib import Path
if sys.argv[1:3] == ['run', 'download']:
    name = sys.argv[sys.argv.index('-n') + 1]
    record = json.loads(os.environ['GH_RECORDS']).get(f'{sys.argv[3]}/{name}')
    if record is None:
        sys.exit(1)
    folder = Path(sys.argv[sys.argv.index('-D') + 1])
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f'{name}.txt').write_text(record)
    sys.exit(0)
endpoint = sys.argv[2]
for key, answer in json.loads(os.environ['GH_ANSWERS']).items():
    if key in endpoint:
        if answer is None:
            sys.exit(1)
        print(answer)
        sys.exit(0)
sys.exit(1)
"""

HEAD = 'c' * 40
BEFORE = 'b' * 40


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ['git', *args], cwd=repo, env={**os.environ, **ENV}, check=True, capture_output=True, text=True
    )
    return done.stdout.strip()


@pytest.fixture
def pushed(tmp_path: Path) -> tuple[Path, str, str]:
    """A repository whose HEAD is the pushed commit: its sha and its tree."""
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init', '-q', '-b', 'main')
    (repo / 'a.txt').write_text('a\n')
    git(repo, 'add', '-A')
    git(repo, 'commit', '-qm', 'a')
    return repo, git(repo, 'rev-parse', 'HEAD'), git(repo, 'rev-parse', 'HEAD^{tree}')


def record(tested_tree: str, **changes: str) -> str:
    """What the Changes job of pull request #7's run on HEAD wrote, testing ``tested_tree`` on BEFORE."""
    fields = {
        'number': '7',
        'head': HEAD,
        'base': BEFORE,
        'base_ref': 'main',
        'base_repo': 'o/r',
        'tree': tested_tree,
        'every_job': 'false',
        **changes,
    }
    return ''.join(f'{key}={value}\n' for key, value in fields.items())


def answers(**changes: str | None) -> dict[str, str | None]:
    """GitHub's answers: #7 merged into o/r's main from a branch of o/r, its run 41 passed on HEAD,
    and main before the push and the latest nightly run passed."""
    given: dict[str, str | None] = {
        'commits/': f'7 {HEAD} o/r',
        'event=pull_request': '41',
        'event=push': 'success',
        'branch=main&status=completed': '50 schedule success',
    }
    keys = {
        'pull': 'commits/',
        'runs': 'event=pull_request',
        'before': 'event=push',
        'latest': 'branch=main&status=completed',
    }
    return {**given, **{keys[name]: value for name, value in changes.items()}}


def run(
    repo: Path,
    sha: str,
    answered: Mapping[str, str | None],
    records: Mapping[str, str | None],
    tmp_path: Path,
) -> tuple[str, str]:
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / 'gh'
    gh.write_text(f'#!{sys.executable}\n{GH}')
    gh.chmod(0o755)
    output = tmp_path / 'output'
    output.write_text('')
    env = {
        **os.environ,
        **ENV,
        'PATH': f'{bin_dir}{os.pathsep}{os.environ["PATH"]}',
        'GH_ANSWERS': json.dumps(answered),
        'GH_RECORDS': json.dumps(records),
        'GITHUB_OUTPUT': str(output),
        'RUNNER_TEMP': str(tmp_path),
        'REPOSITORY': 'o/r',
        'SHA': sha,
        'BEFORE': BEFORE,
    }
    done = subprocess.run(
        ['bash', str(SCRIPT)], cwd=repo, env=env, check=True, capture_output=True, text=True
    )
    return output.read_text().strip(), done.stdout


def test_a_tree_its_pull_request_passed_on_a_green_main_is_tested(
    pushed: tuple[Path, str, str], tmp_path: Path
) -> None:
    repo, sha, tree = pushed
    verdict, said = run(repo, sha, answers(), {'41/tested': record(tree)}, tmp_path)
    assert verdict == 'tested=true'
    assert f'run 41 of #7 passed Build and Test on {tree}' in said


def test_any_of_the_runs_on_the_head_may_be_the_one(pushed: tuple[Path, str, str], tmp_path: Path) -> None:
    """A head can be tested again, after main moved; the run on the base the push left is the one."""
    repo, sha, tree = pushed
    records = {'43/tested': record(tree, base='e' * 40), '41/tested': record(tree)}
    verdict, said = run(repo, sha, answers(runs='43\n41'), records, tmp_path)
    assert verdict == 'tested=true'
    assert 'run 41 of #7' in said


@pytest.mark.parametrize(
    ('change', 'reason'),
    [
        ({'pull': 'null null null'}, 'belongs to no merged pull request'),
        ({'pull': None}, 'Could not look up the pull request'),
        ({'pull': f'7 {HEAD} fork/r'}, 'came from fork/r'),
        ({'runs': ''}, 'no run of Build and Test on'),
        ({'runs': None}, 'Could not list the runs'),
        ({'before': 'failure'}, f'main before the push, {BEFORE}, did not pass Build and Test (failure)'),
        ({'before': 'none'}, f'main before the push, {BEFORE}, did not pass Build and Test (none)'),
        ({'before': None}, 'Could not look up the run on'),
        (
            {'latest': '50 schedule failure'},
            'the latest full run of Build and Test on main, 50, did not pass (failure)',
        ),
        (
            {'latest': '50 workflow_dispatch cancelled'},
            'the latest full run of Build and Test on main, 50, did not',
        ),
        ({'latest': ''}, 'no full run of Build and Test among the latest on main'),
        ({'latest': None}, 'Could not list the runs on main'),
    ],
)
def test_anything_github_says_against_it_runs_every_job(
    change: dict[str, str | None], reason: str, pushed: tuple[Path, str, str], tmp_path: Path
) -> None:
    repo, sha, tree = pushed
    verdict, said = run(repo, sha, answers(**change), {'41/tested': record(tree)}, tmp_path)
    assert verdict == 'tested=false'
    assert reason in said


@pytest.mark.parametrize(
    'change',
    [
        {'number': '8'},
        {'head': 'd' * 40},
        {'base': 'e' * 40},
        {'base_ref': 'release'},
        {'base_repo': 'fork/r'},
        {'tree': 'f' * 40},
    ],
)
def test_a_run_that_tested_another_tree_or_base_runs_every_job(
    change: dict[str, str], pushed: tuple[Path, str, str], tmp_path: Path
) -> None:
    repo, sha, tree = pushed
    verdict, said = run(repo, sha, answers(), {'41/tested': record(tree, **change)}, tmp_path)
    assert verdict == 'tested=false'
    assert f'no run of Build and Test on {HEAD} recorded #7 testing {tree} on {BEFORE}' in said


def test_a_run_without_a_record_runs_every_job(pushed: tuple[Path, str, str], tmp_path: Path) -> None:
    repo, sha, _ = pushed
    verdict, said = run(repo, sha, answers(), {'41/tested': None}, tmp_path)
    assert verdict == 'tested=false'
    assert 'no run of Build and Test on' in said


# Each answer lists runs on main, newest first, as `<id> <event> <conclusion>`; a push run records
# whether it ran every job.
FULL = {'52/full': 'full=false\n', '51/full': 'full=true\n', '49/full': 'full=true\n'}


@pytest.mark.parametrize(
    ('latest', 'tested'),
    [
        # A flaky nightly run is followed by a full push, which stands for it.
        ('52 push success\n51 push success\n50 schedule failure', True),
        # A push that took the fast path is not a full run, so the nightly run before it decides.
        ('52 push success\n50 schedule failure', False),
        ('52 push success\n50 schedule success', True),
        ('53 workflow_dispatch success\n50 schedule failure', True),
        ('51 push failure\n50 schedule success', False),
        # A push without a record (its Changes job failed, say) is passed over.
        ('54 push success\n50 schedule success', True),
        ('54 push success\n52 push success', False),
        ('49 push success', True),
    ],
)
def test_the_latest_full_run_on_main_decides(
    latest: str, tested: bool, pushed: tuple[Path, str, str], tmp_path: Path
) -> None:
    repo, sha, tree = pushed
    verdict, _ = run(repo, sha, answers(latest=latest), {'41/tested': record(tree), **FULL}, tmp_path)
    assert verdict == f'tested={str(tested).lower()}'
