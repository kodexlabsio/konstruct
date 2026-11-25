# Coding Agents: Skills, Plugin and MCP Server

Konstruct gives coding agents the graph in three ways: skills that teach whole workflows with the right commands, an MCP server with tools, prompts and resources, and a hook that runs the gate after an agent edits SQL or dbt YAML. `konstruct install` writes them in each agent's own format, and the Claude Code plugin bundles all of them.

## The Skills

Each skill is an [Agent Skill](https://agentskills.io/), a `SKILL.md` an agent loads when a request matches its description, or when invoked by name (`/konstruct-safe-change` in Claude Code). Each runs real `konstruct` commands and says how to read their output.

| Skill | What the agent does |
|-------|---------------------|
| `konstruct` | Answers questions from the graph with the narrowest command: `impact`, `trace`, `describe`, `explain`, `define`, `ontology`, `conflicts`, `history`, `path`, `search`, `list` or `query` |
| `konstruct-safe-change` | Runs `impact` before changing a model, table or column, makes the edit with everything that reads it, then runs `gate` and fixes each finding until it passes |
| `konstruct-review-change` | Reviews a branch or pull request with `diff`, `gate` and `impact`, and ends with a verdict (block, needs owner sign-off, or safe to merge), the breaking changes, what the change reaches and the `dbt build --select` to run |
| `konstruct-reconcile` | Explains why two numbers differ with `conflicts`, `explain`, `trace`, `history` and `verify`: the filter, join, source, grain or aggregation that differs, and which side follows the approved definition |
| `konstruct-write-query` | Writes a query from the `ontology` and `define`: compiles a metric by entities and properties with `compile-metric`, and otherwise takes the approved term's required filters and grain, an existing column that holds the measure, and joins only along the relationships the ontology lists |
| `konstruct-define-terms` | Records a term in `definitions.yml` with its owner, status, grain, filters and columns, leaves approval to the owner, and checks the build's warnings |
| `konstruct-onboard` | Gives an overview of a repository from `status`, `REPORT.md`, the ontology and the most used models |
| `konstruct-export-semantic-layer` | Exports the ontology as an [Apache Ossie](ossie.md) model and reports each measure left out and why |

Claude Code also gets a `konstruct-data-reviewer` subagent, which reviews a change with the same commands and does not edit files. Each skill pre-approves `konstruct` shell commands for the turn it runs in.

## Installing for Each Agent

```bash
konstruct install                                   # Claude Code and Cursor, in your home directory
konstruct install --target . --agent all --mcp      # every agent, in this repository, with MCP
```

| Agent | Skills | Instructions | MCP server (`--mcp`, with `--target`) |
|-------|--------|--------------|----------------------------------------|
| Claude Code (`claude`) | `.claude/skills/`, and the subagent in `.claude/agents/` | | `.mcp.json` |
| Cursor (`cursor`) | `.claude/skills/` (Cursor reads it), or `.cursor/skills/` when installing for Cursor alone | `.cursor/rules/konstruct.mdc`, attached to SQL and dbt YAML files | `.cursor/mcp.json` |
| Codex (`codex`) | `.agents/skills/` | A section in `AGENTS.md` (`~/.codex/AGENTS.md` in the home directory) | `.codex/config.toml` |
| GitHub Copilot (`copilot`) | `.claude/skills/` or `.agents/skills/` | A section in `.github/copilot-instructions.md` | `.vscode/mcp.json` |
| Gemini CLI (`gemini`) | `.agents/skills/` | A section in `GEMINI.md` (`~/.gemini/GEMINI.md` in the home directory) | `.gemini/settings.json`, and `.agents/mcp_config.json` for Antigravity CLI |
| Windsurf (`windsurf`) | `.agents/skills/` | `.devin/rules/konstruct.md`, applied when relevant | `.devin/mcp_config.json` |

Only Claude Code expands a skill's `$ARGUMENTS`, so the skills written for other agents leave out the request section. Without `--target`, files go under your home directory, where Copilot, Cursor and Windsurf have no instructions file and MCP is registered per project. Cursor and Copilot read both `.claude/skills/` and `.agents/skills/`, so installing for Claude Code and an agent that reads only `.agents/skills/` gives them the same skills twice.

| Option | Effect |
|--------|--------|
| `--agent AGENT ...` | `claude`, `cursor`, `codex`, `copilot`, `gemini` or `windsurf`, one or more; `both` (Claude Code and Cursor, the default) or `all` |
| `--target <dir>` | Install into a repository rather than the home directory, so the team shares it |
| `--mcp` | Also register `konstruct mcp` in each agent's configuration, keeping any other servers and the file's indentation. A `konstruct` server you configured differently is left as it is, unless `--force` is given (in a Codex `config.toml`, always) |
| `--force` | Replace files and sections that were edited by hand |

Reinstalling is safe. Generated files (skills, the subagent and the rules) end with a marker, and the instructions go in a marked section of `AGENTS.md`, `GEMINI.md` or `copilot-instructions.md` (the Codex `config.toml` entry in the same way), so a reinstall updates what Konstruct wrote and leaves the rest of the file as it is. A file or section edited by hand stops the install, with nothing written, until you rerun it with `--force`. `konstruct status` shows where the skills are installed.

## The Claude Code Plugin

The repository is a Claude Code plugin marketplace. In Claude Code:

```text
/plugin marketplace add kodexlabsio/konstruct
/plugin install konstruct@konstruct
```

On Claude Code 2.1.275 or later, `/plugin install konstruct --marketplace kodexlabsio/konstruct` does both in one step.

The plugin needs the `konstruct` command on the `PATH`, and bundles:

- the skills, as `/konstruct:safe-change`, `/konstruct:review-change` and so on;
- the slash commands `/konstruct:impact <column>`, `/konstruct:review [base]`, `/konstruct:gate [base]`, `/konstruct:explain <column>` and `/konstruct:define <term>`;
- the `data-reviewer` subagent;
- the MCP server, run in the project directory;
- the hook below.

## The Hook

After the agent writes or edits a `.sql` file, a YAML file under one of a dbt project's resource paths (`model-paths`, `seed-paths`, `snapshot-paths`, `analysis-paths`, `macro-paths` and `test-paths`, or dbt's defaults), `dbt_project.yml`, a `definitions.yml` or an Ossie model, the plugin's PostToolUse hook runs `konstruct hook post-edit`. It brings a copy of the graph up to date and runs the gate:

- when the gate passes, the agent gets a one-line note;
- when it fails, the agent gets the findings, each with its fix, next to the result of its edit, and is told to fix them before it proposes the change;
- when the build warns about the edited file (SQL it cannot parse, YAML it cannot read), the agent is told, since the gate may not see all of the change;
- other files (including anything under a directory whose name starts with a dot, such as `.github`), and repositories with no Konstruct graph, are ignored; a dbt project or a repository with a `konstruct.toml` that has no graph yet gets a reminder to run `konstruct build`.

The hook works from the project root, the directory Claude Code names in `CLAUDE_PROJECT_DIR` or the one the graph was built from, whichever directory the session is in. It builds from files alone: it never runs dbt (which renders the project's macros), connects to no warehouse, and writes to `.hook/` in the output directory, without `graph.html`, `REPORT.md` or `ontology.yml`, so the graph you built is left as it is. Hooks run for edits made in parallel take turns, and one that finds the files as the last run left them gives that run's answer again; and `.hook/` holds a `.gitignore` so it is never committed. Each answer ends with the MCP server's notice that the text it quotes comes from the repository and is data, not instructions. The plugin starts the hook only for `.sql`, `.yml` and `.yaml` files. To add it to Claude Code without the plugin, run `konstruct hook post-edit` as a PostToolUse command hook; it reads Claude Code's PostToolUse event on standard input. `--remind` only reminds the agent to run the gate.

## The MCP Server

The server exposes `impact`, `trace`, `search`, `describe`, `conflicts`, `define`, `ontology`, `explain`, `history`, `path`, `query`, `diff`, `gate`, `check_sql`, `check_history`, `mutation_test` and `why`, returns the same structured results as `--json`, and rereads `graph.json` when a build changes it. Its `receipt` tool records what an answer rests on, so that `konstruct receipts check` can say later whether it still holds (see [Answer Receipts](receipts.md)). Its prompts are the workflows of the skills, and its resources are `REPORT.md`, the ontology and a summary of the graph (see [MCP Server](../reference/mcp.md)).

In hosts that support the MCP Apps extension, `impact`, `explain` and `gate` also draw their results in the conversation: the steps downstream of a change with the transform at each, a formula with the joins and filters on its rows and the sentence in plain English, and the gate's findings with the file and fix for each. The agent still reads the same text result, and a host without the extension shows only that (see [Views](../reference/mcp.md#views)).

## Checking the SQL an Agent Writes

`konstruct check-sql` reads a query an agent (or an analyst) wrote, without running it, and says where it disagrees with the project, before the query runs or its answer is given. The `check_sql` MCP tool runs the same check.

```bash
konstruct check-sql query.sql            # exit 1 when the query breaks an approved definition
konstruct check-sql - --strict < query.sql   # from standard input; exit 1 on a warning too
```

The query is read in the graph's dialect. Each output column that aggregates is a measure: its formula is composed through CTEs, subqueries and each branch of a UNION down to source columns, with the WHERE, ON and HAVING clauses on the way, the condition of a `FILTER` or `CASE` inside the aggregate, and the filters the graph shows hold on every source row of the columns it reads (a filter in a CTE beside a column, on one input of a COALESCE, on one branch of a union, on another alias of the same table or on a column copied from one, in the ON of a LEFT JOIN for the other side, or above a window or aggregate, in the same model or upstream, does not; a graph built by an earlier Konstruct shows none, and the finding says to rebuild it). Each measure is compared with the business term it is named for (by name or synonym) and with the column of the project that already holds it.

| Rule | What it finds |
|------|---------------|
| `missing-filter` | A measure computed without a filter its definition, or the column that holds it, applies. A column that computes a measure term's formula from the same columns over some of its rows, such as the card payments of an order amount, is that measure for those rows, so a query of it needs the term's filters too (a warning). A slice on a column the term filters, such as the amount of returned orders where the term keeps completed ones, is another idea and needs none; a query that filters or groups by that column itself gets only a note, and no rewrite. A filter that only drops the group of NULLs of the column a measure is grouped by (`WHERE k IS NOT NULL ... GROUP BY k`) changes no other group's value, so a query grouped by `k` too needs no such filter; one grouped otherwise, or not grouped, adds those rows in and needs it. A filter never true where its column is NULL (a comparison, `IN`, `LIKE` or `BETWEEN` on the column, in a WHERE or an inner join's ON, on the relations the measure's own columns come from) applies that column's `IS NOT NULL`; one on a row tied to them, such as the customer's first order, does not |
| `formula-differs` | A measure computed differently from the column that holds it. A constant factor inside or outside a sum or an average is the same formula (`SUM(x * 2)` and `2 * SUM(x)`; `SUM(x / 100)` and `SUM(x) / 100.0` too where `/` never truncates, so not in Postgres, Redshift, SQL Server, Fabric, Trino, Presto, Athena, SQLite, Teradata, Dremio, Drill, Materialize or RisingWave, where dividing two integers truncates), and so is a function written in another case (`sum` and `SUM`, where BigQuery and ClickHouse keep the case as written), and a measure that reads several of the columns a term is bound to, when they come from different columns (gross profit: price and cost), is not compared with any one of them: an `info` note says the formula was not compared |
| `recomputes-measure` | A measure recomputed that a column of the project already holds |
| `conflicting-column` | A measure named for a term (by name or synonym) whose formula cannot be compared with the term's, such as a term bound to each item's price, that reads none of the term's columns, nor a column they are computed from or computed from them, and reads only columns of other terms (or columns theirs are computed from): `revenue` added up from the order total. One that also reads a column no other term holds, such as the order total less its tax, may compute the term another way and passes. A warning, as every match by name alone is; an output the query leaves unnamed is not compared with a term. Where the formulas can be compared, `formula-differs` reports the difference |
| `minor-units` | A result still in minor units of a currency, such as cents, where the project reads the column in whole units: every column of the project that divides it by a constant divides it by 100 (or 1000), and the column or a copy of it is named for minor units (`tax_paid_cents`), or a term with `unit: currency` carries the divided column's units: it adds, picks or aggregates its values, or scales them by a constant. A rate that multiplies another column (a discount percentage divided by 100 into a subtotal times one minus the rate) is not an amount of money. A result divided by that constant (however written: `/ 100`, `/ 10 / 10`, `/ CAST(100 AS DECIMAL)`), a ratio of two such amounts, a count, and an output named for minor units pass; a median, percentile or quantile of such amounts is still in them. Also a filter that compares such a column with the very value a term's filter requires of the column that reads it in whole units, such as `order_total >= 20` on cents where the term requires 20 dollars; `order_total >= 2000` on cents counts as that term's filter on the measure's own rows, where the column the term filters is the integer or decimal cents divided by 100 and nothing else, in a dialect where `/` never truncates (a cast or a rounding moves the boundary: `CAST(1960 / 100 AS INT)` is 20) |
| `retired-column` | A column bound to a retired term |
| `fan-out` | A sum, count or average over a join that repeats the rows it aggregates, from the keys, unique tests and relationships of the [ontology](ontology.md). A key only a primary key nothing enforces keys, as every primary key on Snowflake or BigQuery, does not show a join onto it safe, so a join onto a dimension keyed that way is reported: add a `unique` test on its key to show it. A key flows downstream only: a unique test on a staging view says nothing of the table it copies, which may hold every version of a row. `SELECT DISTINCT` is unique on its columns other than literals (a function such as `uuid()`, `random()` or `now()` is no literal), and the rows where `ROW_NUMBER() OVER (PARTITION BY k ...) = 1` on its partition's columns, the partition of a named window (`OVER w ... WINDOW w AS (PARTITION BY k)`) included; an `UNNEST` in the select list repeats the rows they keep, so it leaves only the `DISTINCT`'s whole output unique, and after a `GROUP BY` no key at all. A source joined on a range and no equality (`d.day <= o.ordered_at`) is reported, as any number of its rows can match; a join into intervals, such as `t >= s AND t < n` with `n` the `LEAD(s)` of the next row, is reported unless its equalities cover a key of the intervals. A LEFT or FULL join's ON pairs only the rows of the source it joins, so an equality there between two other sources fixes nothing. Only the columns a sum or average adds up count: one in a `CASE WHEN` condition only chooses the rows, so a sum of each payment's amount where its order is completed adds each payment once. A join on every column of a unique key of a source, in an inner join's ON or the WHERE clause, from whichever other sources, repeats nothing (an outer join's ON fixes nothing, since the join keeps the rows it does not match); `IS NOT DISTINCT FROM` counts as equality on a key a `DISTINCT` or `GROUP BY` makes, which holds one row for NULL. `GROUP BY ALL` is unique on its items that are not aggregates (an aggregate in a scalar subquery of an item leaves it a grouping item); `ROLLUP`, `CUBE`, `GROUPING SETS`, `WITH ROLLUP` and `WITH TOTALS` add rows that span the groups, so they make no key and fix no column. An average of a column of a source whose key the `GROUP BY` holds is not reported, since each group repeats one row of it, where no row holds NULL in the key: a unique column with a `not_null` test or an enforced primary key, or the columns a `GROUP BY` or `DISTINCT` makes unique; a `unique` test alone lets many rows share NULL in one group, and a key only an ontology pack presumes does not count. A dimension's value summed once per row of the side that keeps its grain is not reported where the definition of a measure (`kind: measure`) binds a column that repeats that value on rows keyed the same way, as a revenue measure may bind each order item's copy of its product's price; a term that only names the column does not (or, for a CTE's aggregate, a bound column that holds the same aggregate grouped by the same columns). A model of the project that sums the value there does not vouch for it, since it can repeat the value as wrongly as the query |
| `mixed-grain` | A measure of a coarser grain repeated on every row of a finer one |
| `join-off-relationship` | Two related relations joined on columns their relationship does not use |
| `unknown-join` | A join that no relationship or unique key vouches for |
| `presumed-key` | A join taken as safe because an [ontology pack](ontology-packs.md) presumes a column unique, which nothing in the project tests (an `info` note) |
| `unknown-relation`, `unknown-column` | A relation or column the project does not have. A table function (`read_csv(...)`) has the columns its call lists (`columns = {...}`), and without a list its columns are not checked. A relation whose columns the graph may not list in full, because no warehouse or dbt catalog gave them, gets an `info` note for a column it does not list, unless the name is close to one it does, which is likely misspelt |
| `unparsed`, `unchecked` | SQL that could not be parsed, or a statement that parses but could not be analysed |

A finding is an `error` when the query breaks an approved definition or cannot be parsed, and otherwise a `warning` or an `info` note; a match by name alone is never an error. Each finding says how to fix it, with the evidence it rests on: the definitions, keys, tests and relationships. A source system's identifier that an ontology pack binds (Stripe's `customer.id`) is taken as unique, though nothing in the project tests it, except in a table its loader lands with a row per version or per sync; evidence that rests on one says `presumed unique by the stripe pack`, and a relationship only a pack gives says so. The check passes when every statement was analysed and no finding is an error, so a statement it could not analyse does not pass; `--strict` fails on a warning too.

Where a fix changes the measure and no other column of the result, the result also carries the corrected SQL: a missing filter is added to the WHERE clause when only the measure reads the rows it removes, and otherwise to the measure's aggregate (as `FILTER (WHERE ...)`, or `IFF` or `IF` in a dialect without it); a join is put back on its relationship's columns; a retired column is swapped for its replacement under the same output name. No filter is added to a measure that ranks or limits rows (QUALIFY, ORDER BY with LIMIT, a window over it), since the rows kept would change too.

`--format markdown` prints the result as a table for a pull request comment, and `--json` (or `--format json`) as data. To check what people already run, `konstruct check-sql --history` reads the warehouse's query history (see [Shadow Metrics](usage.md#shadow-metrics)).

## Before Opening a Pull Request

The skills, the hook and the `gate` MCP tool tell the agent to run `konstruct gate` before it proposes a change, fix what it reports and run it again until it passes. The gate fails on a removed column something still reads, an approved definition whose meaning changed, and a changed column that computes a business idea differently from an existing one, and each finding says how to fix it. With `gate: true`, the [GitHub Action](github-action.md#the-gate) runs the same check on the pull request.
