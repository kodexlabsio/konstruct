# Checking Semantic Layers

Platforms serve business definitions as objects of their own: Snowflake semantic views, Databricks metric views and Apache Ossie models. Each is authored by hand, generated from query history, translated from another tool or converted from a BI model, and then trusted by whoever or whatever reads it. `konstruct conform` checks each against what the code computes.

```text
$ konstruct conform semantic_views/
sales_sv (snowflake semantic layer, read from semantic_views/sales_sv.sql)
  metric net_revenue  differs from Net revenue (term, by columns+name) [error]
      view:  SUM(payments.amount)
      code:  SUM(raw_payments.amount / 100)
      missing filter: status = 'completed' (approved definition: Net revenue)
      witness: where status = ...
  key orders.order_key  key-misfiled: is the key of entity order, a key and not a fact
  relationship orders_customers  ...
  4 of 6 metrics conform; 1 differ, 1 unbound, 0 unparsed.
```

## What it reads

- **Snowflake semantic views:** a `CREATE SEMANTIC VIEW` statement in a `.sql` file, with its tables, relationships, facts, dimensions, metrics and `AI_VERIFIED_QUERIES`; or the YAML that `SYSTEM$READ_YAML_FROM_SEMANTIC_VIEW` returns, in a `.yaml` file.
- **Databricks metric views:** the YAML (a file, or the `view_text` of `DESCRIBE TABLE EXTENDED ... AS JSON`), or the `CREATE VIEW ... WITH METRICS LANGUAGE YAML` statement.
- **Apache Ossie models:** an Ossie file, read as `konstruct build` reads it.

With `--warehouse`, `konstruct conform` reads the layers the project's Snowflake and Databricks connections serve: every Snowflake semantic view (or those `--view` names) by `SHOW SEMANTIC VIEWS` and `SELECT SYSTEM$READ_YAML_FROM_SEMANTIC_VIEW(...)`, and each Databricks metric view `--view` names by `DESCRIBE TABLE EXTENDED ... AS JSON`. These are the only statements it runs; each is metadata, no view is queried and no model SQL runs, and the project must be trusted (`konstruct trust`). Expressions are parsed, never executed.

## What it decides

Each logical table is mapped to the relation it reads, and each expression to the columns it reads. A metric is then composed through the graph from those columns, as `konstruct explain` composes a column, and compared by what it computes, not how it is written (see [Finding Conflicting Definitions](conflicts.md)):

1. with the **approved definitions** that apply: a term in `definitions.yml` defined on a column the metric reads, and its required filters;
2. with the **other implementations of the same concept**: a metric of that name or synonym in an Ossie model the project holds, or in another layer given to the same command. A disagreement between two layers is reported once, as a pair that names both, whichever was read first.

How the match was made grades the finding. A difference is an error when the metric answers to the approved definition by its name or a synonym, or the formula is an approved one: a formula that differs, or a required filter that is not held. A match through the columns a metric reads alone is a candidate, and is at most a warning. A metric with nothing to compare with is `unbound`, and one whose expression does not parse is `unparsed`; neither is ever `conforms`.

| Verdict | Meaning |
|---------|---------|
| `conforms` | The identity test compared the metric's formula with another's and found it the same, with the error bound it reports on the finding. Never reported when only filters were checked |
| `differs` | The formula differs (with a witness: values at which the two give different results), or a required filter is not held. An error when matched by name or a synonym, or against an approved formula; a warning when matched through the columns alone |
| `unbound` | Nothing to compare the metric's formula with: no formula to compare (when its approved term's required filters hold, it says so), its table is not in the graph, it is built on other metrics, it is non-additive by a dimension its expression does not hold, or it aggregates a column of a parent table at the grain of its own (see below) |
| `unparsed` | The expression, or a fact or dimension it reads, is not SQL Konstruct can read |
| `column-missing` | A fact, dimension or metric reads a column its relation no longer has |
| `key-misfiled` | A key the code tests unique is declared a fact |
| `key-untested` | A declared primary or unique key has no unique test in the code |
| `cardinality-contradicted` | A relationship refers the other way round to the code's, or has a cardinality the code contradicts |
| `unknown-join` | A relationship joins columns that no join, test or relationship in the code relates |

A **missing filter** is reported only against an approved definition's required filter, or against a sibling implementation of the same concept, and the finding says which. A metric that reads a mart column already has the mart's filters, as `check-sql` decides them, by resolved column and reading; the view's own filter (a Databricks metric view's `filter`) counts too. A metric's own conditions hold a required filter when every aggregate of the metric carries them: `AGG(x) FILTER (WHERE c)`, or `AGG(CASE WHEN c THEN x END)` under `SUM`, `COUNT`, `AVG`, `MIN` or `MAX`, with no `ELSE` or `ELSE NULL`. A `CASE` with another `ELSE` or several `WHEN` branches keeps every row, and a condition on only some of the aggregates holds nothing, so neither is a held filter.

A metric belongs to a table, and aggregates that table's rows. `COUNT(*)` counts the rows of the relation its table reads, so a count on `orders` and a count on `line_items` are different metrics even when both are called `row_count`. A metric of `line_items` that sums `orders.o_totalprice` counts each order once for every one of its line items, which is not what `orders`'s own metric computes, so it is `unbound` and says so; `MIN`, `MAX` and `ANY_VALUE`, which a repeated row does not change, are compared as usual.

Two logical tables over one relation (role-playing dimensions, such as an order's placed date and shipped date) each resolve to that relation, and each is checked on its own.

## Output

`--json` prints `conform.json`, with `format_version` 1: the layers read, a summary of counts, and the findings, each with its `kind`, `name`, `verdict`, `severity`, `detail` and, for a metric, the expression as the layer wrote it, the code's composed formula with the conditions on its rows, what it was compared with and how the match was made, the missing filters, and the witness. Exit status is 1 when a finding is an error, which makes the command a check for CI. Without `--warehouse` it also writes `konstruct-out/conform.json` (the same document, with the files it read); layers read with `--warehouse` are not recorded. Once that is committed, `konstruct gate` reads those files again and fails a change that makes a metric that conformed, or whose required filters held, an error, or leaves the graph without a table or column it reads (`platform-definition-differs`). A change that only removes or alters the metric it was compared with is reported as the pair of the two, and removing it reports nothing.

## Comparing with reference SQL

Snowflake's guide [Getting Started with Apache OSI](https://www.snowflake.com/en/developers/guides/getting-started-with-apache-osi/) checks a semantic view by writing each business question twice, once against the view and once as independent reference SQL over the raw tables, running both in the warehouse and requiring zero differing rows. `konstruct conform` needs no hand-written reference SQL, because the reference is the code Konstruct has already read, and no warehouse run; it also covers keys and relationships as well as metrics, and reads Databricks and Ossie as it reads Snowflake. The reference-SQL method measures the data the two queries return on the day they run; `konstruct conform` compares what is computed, and says "the same" only with the identity test's error bound. The platforms' own checks compare a representation with the formula it holds, structurally: they find a changed or missing formula, and none decides whether it is equivalent to the code.
