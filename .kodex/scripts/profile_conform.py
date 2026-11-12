"""Profile `Project.conform` on a generated project: where the time of `konstruct conform` goes.

Usage:
    uv run .kodex/scripts/profile_conform.py 2000      # the 25 functions that take longest
"""

from __future__ import annotations

import cProfile
import pstats
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import benchmark


def main() -> int:
    models = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
    with tempfile.TemporaryDirectory(prefix='konstruct-profile-') as directory:
        root = Path(directory)
        benchmark.make_project(root, models=models, columns_per_model=benchmark.COLUMNS)
        project = benchmark.Project(root)
        project.build(run_dbt=False, warehouses=False)
        fresh = benchmark.Project(root)
        fresh.ontology()
        profile = cProfile.Profile()
        profile.enable()
        benchmark.conform_times(fresh, root, models)
        profile.disable()
        pstats.Stats(profile).sort_stats('cumulative').print_stats(25)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
