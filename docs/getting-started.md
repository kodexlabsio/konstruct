# Getting Started

Konstruct is not on PyPI yet, so install it from GitHub with [uv](https://docs.astral.sh/uv/):

```bash
uv tool install git+https://github.com/kodexlabsio/konstruct
```

Installing from GitHub compiles Konstruct, so it needs a [Rust toolchain](https://rustup.rs/) (1.95
or newer) on the machine: `curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh` installs
one, and GitHub's hosted runners have one already; each release also has wheels that do not
(below). Konstruct runs on x86_64,
aarch64 (arm64), riscv64, ppc64le, s390x (IBM z13 or later) and loongarch64 processors, on Python
3.11 or newer.

Or run it without installing anything, each time:

```bash
uvx --from git+https://github.com/kodexlabsio/konstruct konstruct build
```

For a warehouse extra, name it in the requirement, for example
`uv tool install "konstruct[snowflake] @ git+https://github.com/kodexlabsio/konstruct"` (or `[bigquery]`, `[databricks]`, `[trino]`, `[rdf]` for SKOS and OWL vocabularies, `[all]`).
To pin a release, add its tag: `git+https://github.com/kodexlabsio/konstruct@v0.8.0`. Each [release](https://github.com/kodexlabsio/konstruct/releases) also has wheels attached, which install without compiling: for Linux on x86_64, aarch64, ppc64le, s390x, riscv64 and loongarch64 (glibc 2.28 or newer, 2.31 for riscv64 and 2.36 for loongarch64), for macOS on Apple silicon and Intel, and for Windows on x64 and arm64. Install one with `uv tool install <wheel URL or file>`. Each file attached, the sdist and the CycloneDX SBOM of the locked dependencies too, has a signed build provenance attestation; `gh attestation verify <file> -R kodexlabsio/konstruct` checks a downloaded file.

Then, in a repository with dbt models, SQL, DAGs or a warehouse:

```bash
cd your-data-repo
konstruct status                 # what konstruct finds here, before building anything
konstruct trust                  # once you have reviewed its konstruct.toml, profiles.yml and dbt project
konstruct build                  # writes konstruct-out/ and ends with the findings worth a look
konstruct verify --conflicts     # do two definitions of one number disagree on your data, and where?
konstruct impact orders.amount   # everything downstream of a column
konstruct open                   # the interactive graph in a browser
konstruct install                # adds the Konstruct skills to Claude Code and Cursor
```

![konstruct impact analysis on the jaffle_shop example](images/demo-cli.gif)

To try it on the bundled example (jaffle_shop on DuckDB, with an Airflow DAG), from a clone of this repository:

```bash
uv sync
uv run konstruct trust examples/jaffle_shop                   # lets Konstruct run dbt in the example
uv run konstruct build examples/jaffle_shop                   # about six seconds the first time, most of it dbt
uv run konstruct verify --conflicts -C examples/jaffle_shop   # reads the seeds' CSV files: no warehouse needed
uv run konstruct impact stg_payments.amount -C examples/jaffle_shop
```

`uv run .kodex/scripts/prepare_example.py` builds the example's DuckDB warehouse with dbt, for `konstruct verify` and the other checks that rerun the models on their built inputs.

`konstruct build` needs no configuration: it finds dbt projects, Airflow DAG files, Dagster definitions, PySpark jobs, LookML, Power BI semantic models, SQL files, DuckDB files and the warehouse each dbt profile points at (DuckDB, Postgres, Snowflake, BigQuery, Databricks or Trino), and prints what it found and what it did with each. If a dbt project's `target/manifest.json` is missing or older than the project files and `dbt` is on `PATH`, it runs `dbt docs generate` first, once the project is trusted. A warehouse named in the repository's own `konstruct.toml` or `profiles.yml`, other than a DuckDB file inside it, is read, and dbt is run, only once you run `konstruct trust` after reviewing the project; until then the build skips them with a warning, and builds from the files and the dbt artefacts already in `target/` (see [Trusting a Project's Connections](reference/configuration.md#trusting-a-projects-connections)). `~/.dbt/profiles.yml` is always used. `konstruct init` writes a commented `konstruct.toml` recording what discovery found, for when you want to pin or change it.

```text
$ konstruct build
Built /src/jaffle_shop

  dbt          jaffle_shop (.)            6 models, 3 seeds, 26 tests, 1 exposure  (artefacts up to date)
  duckdb       duckdb jaffle_shop.duckdb  10 relations  (dbt profile jaffle_shop)
  airflow      dags/jaffle_shop_daily.py  1 DAG, 6 tasks
  definitions  definitions.yml            6 terms

  graph        1 dag, 1 exposure, 6 models, 3 seeds, 1 table, 6 tasks, 6 terms, 26 tests; 48 columns
  lineage      36 of 36 columns of models and built tables (100%)
  warnings     none
  wrote        graph.json, REPORT.md, graph.html, ontology.yml, explain.json to konstruct-out in 0.7s

Findings
  conflict  orders.amount is 'Order amount' in definitions.yml, and finance_orders.order_value, named like it, computes it another way: finance_orders.order_value counts only the rows where stg_orders.status NOT IN ('returned', 'return_pending'); compare them on the data: `konstruct verify --conflicts`
  claim     the lineage claims 40 column edges, none yet checked against the data; check each one by rerunning the models: `konstruct verify`
  reach     a change to raw_payments.amount reaches 1 exposure (customer_dashboard) and 9 columns in 5 relations; see everything downstream: `konstruct impact raw_payments.amount`
```

## First Findings

A build ends with up to six findings, each one line that says what was found and the command that shows more:

- **conflict:** up to three pairs of columns that the project's own terms say hold one number and that compute it in two ways, the most consequential first, each with a sentence that explains the difference (see [Finding Conflicting Definitions](guide/conflicts.md)). Both columns are bound to the term in `definitions.yml` (or a vocabulary), or one is bound to it and the other, bound to no term of its own, is named like the term or one of its synonyms and is a likely measure conflict with it (`confident` in `konstruct conflicts --json`): a roll-up, a running total, a subset or a count named for a synonym is not one. A pair that only a name links is never one: in public projects such pairs are rarely two definitions of one number, and most differ on purpose, by grain or segment; `konstruct conflicts` lists them. Once `konstruct verify --conflicts` has run the pair on the data, the line says how the two differ there, by the key both share, and gives the `verify --why` command for the row that differs most (see [Conflicts on the Data](guide/verify.md#conflicts-on-the-data)).
- **definition:** an approved term in `definitions.yml` that names a column no model or table builds any more, or a column `konstruct.lock` pins that changed since it was locked. There is none when a dbt project was found but not read (it is not trusted, or `--no-dbt` was given and it has no manifest), as the graph then lacks its models.
- **claim:** what the last `konstruct verify` found the lineage or the ontology claims wrongly (an influence the lineage does not record, a key that repeats, a relationship with values its key lacks), or, where a warehouse the build read holds the models and verify has not run, that the lineage is unchecked. Seeds alone are not enough for `konstruct verify`, which reruns each model on its own inputs; run `dbt build` first.
- **reach:** the project's own column whose change reaches the most exposures and dashboards, and then the most of the project's own columns. Models of installed dbt packages, such as elementary or dbt_project_evaluator, are left out.

Each kind but `conflict` gives at most one finding. A finding that cannot be made, such as one from a `verify.json` or `conflict_data.json` this Konstruct cannot read, is left out with a note under the findings; it never fails the build.

The findings read only the graph and what earlier `verify` runs wrote beside it, never the warehouse, so a project with no `definitions.yml` and no warehouse still gets the reach. The explanation of a conflict comes from the SQL alone; how the two differ on the data is added once `verify --conflicts` can run them, on the warehouse or on the seeds. `--json` lists them under `findings`, and `Project.findings()` returns them.

```text
$ konstruct verify --conflicts
Conflicting definitions on the data

1 conflicting pair run on jaffle_shop.duckdb, with the seeds raw_orders, raw_payments read from their CSV files: 1 disagrees on the data, 0 agree, 0 not run.

Order amount
  finance_orders.order_value  vs  orders.amount
    finance_orders.order_value and orders.amount differ: 6 rows of order_id only in orders.amount; totals 1,585 and 1,672, 87 apart (5.5% of the first)
    - filter only in finance_orders.order_value: WHERE NOT stg_orders.status IN ('returned', 'return_pending')
    ...
    next  konstruct verify --why finance_orders.order_value --where order_id=23
```
