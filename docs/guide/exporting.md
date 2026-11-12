# Exporting

`konstruct export` hands the lineage to other tools:

| Format | What it writes | Use |
|--------|----------------|-----|
| `openlineage` | One OpenLineage `JobEvent` per built table, as JSON lines: inputs, outputs with schema and column lineage facets, each column's input fields with `DIRECT` transformations (`IDENTITY`, `TRANSFORMATION` or `AGGREGATION`, with the expression), and the columns used in joins, filters and grouping as dataset-wide `INDIRECT` dependencies (`JOIN`, `FILTER`, `GROUP_BY`) | Load into Marquez, DataHub, OpenMetadata or any OpenLineage consumer |
| `mermaid` | A `flowchart` of tables, models and tasks, or of columns with `--columns` | Documentation, pull requests, GitHub renders it inline |
| `dot` | A Graphviz graph, with column ports under `--columns` | Large static diagrams |
| `csv` | One row per column edge: relation, column, upstream relation and column, transform, verification and expression | Spreadsheets, or a lineage table in the warehouse |
| `ossie` | The [ontology](ontology.md) as an Apache Ossie semantic model: datasets, relationships, and each measure as a metric with its composed formula and a `KONSTRUCT` extension for status, owner and verification (see [Apache Ossie Models](ossie.md)) | Semantic layers and BI tools that read Ossie |
| `owl` | The ontology as OWL 2 in Turtle: entities as classes, their columns as data properties, relationships as object properties, and measures as annotations (see [below](#owl-and-turtle)) | Knowledge graphs, ontology editors and platforms that import RDF |
| `semantic-view` | The ontology as one Snowflake `CREATE OR REPLACE SEMANTIC VIEW` statement: tables, relationships, dimensions and metrics (see [below](#snowflake-semantic-view)) | Snowflake semantic views and Cortex Analyst |
| `metric-view` | The ontology as Databricks metric views, one `CREATE OR REPLACE VIEW ... WITH METRICS` statement per table (see [below](#databricks-metric-views)) | Unity Catalog metric views |
| `odcs` | The ontology as ODCS 3.2 data contracts, one YAML document per model, with verified statements only where a test or `konstruct verify` is the evidence (see [below](#data-contracts)) | Data contract tools and catalogues that read ODCS |

A [plugin](plugins.md#an-export-format) the project enables can add a format, which `konstruct export <format>` writes as it does these.

```bash
konstruct export openlineage -o lineage.jsonl --namespace duckdb://analytics
konstruct export mermaid -s orders.amount --columns > docs/orders-amount.mmd
```

The tests validate every OpenLineage event, and its column lineage and schema facets, against the OpenLineage 2-0-2 JSON schemas.

## The Ontology for Each Platform

`ossie`, `owl`, `semantic-view` and `metric-view` write the same [ontology](ontology.md), so one ontology, derived from the code and `definitions.yml`, feeds each platform in its own format. They carry the same entities: each relation that holds an entity is a dataset, a logical table or a metric view keyed by the entity's key, and in OWL the entity is a class. A project with no `definitions.yml` exports the entities and relationships Konstruct derives from keys, tests and joins, and the entities and measures of any [ontology pack](ontology-packs.md) it matches. Nothing is invented: a description, synonym, grain or filter is written only when the ontology holds it. Each export takes the whole ontology, so `--select` does not apply, and an ontology with no entities or measures, or whose every measure is left out, exports nothing, says why, and exits with status 1.

The `owl`, `semantic-view` and `metric-view` files open with comments that list what each leaves out and why; the `ossie` export names each measure it leaves out on standard error (see [Apache Ossie Models](ossie.md)). The `ossie`, `semantic-view` and `metric-view` exports leave out, and `owl` writes without its formula, a measure whose composed formula, by what the graph records of the queries on the way, would count other rows than the measure does. Each relation on the way from the measure's column down to source columns is checked:

- a `WHERE`, `HAVING`, `QUALIFY`, `LIMIT`, `OFFSET`, `DISTINCT`, anti join, `UNION` that removes duplicates, `INTERSECT` or `EXCEPT` changes the rows;
- a join keeps them only when the joined side's column is known to be unique (a `unique` test, a primary key or an identifier field), so that it matches at most one row;
- an inner join, or an outer join that reads the measure's table only on its nullable side (or joins it through such a table), also drops rows unless a `not_null` test and a `relationships` test on the foreign key, targeting the joined column, show that every row matches;
- a self join keeps them only when both its columns are unique, a join between two tables the measure reads is checked both ways, and a table read with no such join condition, as a cross join reads it, changes them;
- a source that makes rows of its own anywhere in the relation's query, a table function (`generate_series`, `range`, a Snowflake `TABLE(FLATTEN(...))`), `VALUES`, `UNNEST`, `LATERAL` or `LATERAL VIEW`, changes them.

Konstruct does not record the rows a `PIVOT` or `UNPIVOT` makes or a recursive CTE adds, so a measure composed through one is not left out for them; a query whose SQL Konstruct could not analyse records nothing to check: its formula does not reach source columns, so its measure is left out for that reason.

A measure is composed from another column bound to it when its first column's way down changes the rows. A measure that aggregates an aggregate (`AVG(SUM(...))`), as a formula composed across two grains does, or that combines the rows of a `UNION ALL`, is left out too.

```text
konstruct export owl -o shop.ttl --namespace https://example.com/shop
konstruct export semantic-view -o semantic_view.sql
konstruct export metric-view -o metric_views.sql
```

### OWL and Turtle

| Ontology | OWL |
|----------|-----|
| Entity | An `owl:Class`, labelled with its name (`rdfs:label`) and term (`skos:prefLabel`), with the term's definition (`skos:definition`), its key as `owl:hasKey` when a test, primary key or identifier field shows it to be unique, and the relations that hold it and the evidence for it as annotations |
| Property | An `owl:DatatypeProperty` for each column of the entity's relations, with the class as its domain, a range from the OWL 2 datatype map when every column of that name has the same known type, its description, and the columns it is read from |
| Relationship | An `owl:ObjectProperty` from one class to the other: functional when many-to-one, and inverse functional as well when one-to-one, with the joining columns, whether a test vouches for it, and its evidence |
| Measure | A named individual with its definition, synonyms (`skos:altLabel`), formula composed to source columns, grain, unit, currency, filters, status, owner, approval date and columns; its entity's class names it with `konstruct:measure` |
| Broader term | `rdfs:subClassOf`, when both terms are entities |

IRIs are stable: each name is under the ontology's IRI, `urn:konstruct:<project>` unless `--namespace` gives another, and an entity or measure whose term was read from a [SKOS or OWL vocabulary](definitions.md) keeps that concept's IRI. `--namespace` must be an absolute IRI. Konstruct's annotation properties are declared in the file under `https://kodexlabsio.github.io/konstruct/vocabulary#`. Left out: the columns of relations that hold no entity, which appear only in formulas; a key not known to be unique, such as one only a term names; the range of a date or time property, since the OWL 2 datatype map has no date or time; the ontology's rules and violations (`konstruct ontology` lists them); and a broader term that is not an entity. Konstruct reads the file back as a vocabulary, each class an entity term.

### Snowflake Semantic View

| Ontology | Semantic view |
|----------|---------------|
| Relation holding an entity, or read by a measure | A logical table under `TABLES`, with the entity's name and term as `WITH SYNONYMS` and the relation's description as `COMMENT`. A relation that holds the keys of several entities holds the one named for it (`orders` holds *order*), as relationships read it; that entity's key is its `PRIMARY KEY` when a test, primary key or identifier field shows it to be unique, and the other entities' unique keys are `UNIQUE` |
| Column | A dimension under `DIMENSIONS`, with its description |
| Relationship to a primary key or unique key | `RELATIONSHIPS`: `orders (customer_id) REFERENCES customers (customer_id)` |
| Aggregate measure that reads one table | A metric of that table under `METRICS`, its formula composed to source columns in Snowflake SQL, the term's name and synonyms as `WITH SYNONYMS`, and a `COMMENT` with its definition, grain, required filters and unit |

Names that differ only by case are kept apart, since unquoted Snowflake names are not case sensitive, and a reserved word is quoted. Left out: a key not known to be unique; a measure that is a per-row formula, whose formula does not reach source columns, that reads more than one table, or that the rule above leaves out; a relationship whose key is not known to be unique, that refers to a column the target does not key, that joins a table to itself, or that would close a cycle, which Snowflake does not allow; facts, since Konstruct does not tell facts from dimensions; and data types, owners, status and verification, which a semantic view has no place for.

### Databricks Metric Views

Each table above is the `source` of one metric view, `<table>_metrics`, written as YAML version 1.1 inside a `CREATE OR REPLACE VIEW ... WITH METRICS LANGUAGE YAML AS $$ ... $$` statement:

| Ontology | Metric view |
|----------|-------------|
| Relation | `source`, with its description as `comment` |
| Relationship to a primary key or unique key | A join under `joins` to each table the source refers to, named after that table (or after the referring column, such as `billing_customer`, when the source refers to the table more than once), `on` the joining columns, with `rely: at_most_one_match`; the tables a joined table refers to have views of their own and are not joined again |
| Column | A field under `fields`: each column of the source, as `source.<column>`, then each column of every joined table, named `<join>_<column>` |
| Aggregate measure that reads the source | A measure, its formula in Databricks SQL with each column as `source.<column>`, the term's name as `display_name`, up to ten synonyms, and a `comment` with its definition, grain, required filters and unit |

Names that differ only by case are kept apart, and keywords are quoted. Left out: the same measures and relationships as the semantic view, apart from cycles, since a view joins only the tables its source refers to; a synonym or display name over 255 characters; a comment, synonym or display name that holds `$$`, which would end the statement; and data types, owners, status and verification.

## Data Contracts

`odcs` writes the ontology as [ODCS](https://github.com/bitol-io/open-data-contract-standard) 3.2 data contracts: one YAML document per model that holds an entity or a measure, in one stream (`---` between them), each starting with comments that say what it leaves out. Each contract has the status `draft`, and nothing in it claims to be enforced: Konstruct runs none of its quality rules, and says so in the file.

```text
konstruct export odcs -o contracts.yml
konstruct contract check contracts.yml
```

| Ontology | ODCS field |
|----------|-----------|
| Relation | A `schema` object: `name`, `physicalName`, `physicalType` (`table` or `view`), `description`, and the entity's name and term as `businessName` and `synonyms` |
| Column | A property: `physicalType` and `logicalType` from the column's type, `description`, `unique` and `primaryKey` where a test, key or `konstruct verify` shows the column unique, `required` where a `not_null` test says so |
| Measure column | `semanticType: measure` when its composed formula is an aggregate, with `transformLogic` (the formula composed down to source columns), `transformSourceObjects` and the term's name as a synonym |
| Relationship | A `foreignKey` under the object's `relationships`, with its cardinality and grade as `customProperties` |
| `unique`, `not_null` and `relationships` tests | `quality` rules for the tools that run them: `duplicateValues`, `nullValues` (ODCS's predefined metrics, `mustBe: 0`) and a `sql` rule that counts values with no match, each naming its test |
| Evidence | The object's `context.verifiedStatements` |

Every fact carries its grade, `verified`, `declared` or `inferred` (see [Ontology](ontology.md)), in `customProperties` as `konstructGrade`. A *verified statement* is written only where Konstruct holds evidence for it, and names the evidence in `konstructEvidence` and its grade in `konstructGrade`:

- a column is unique, or never null: the id of the dbt `unique` or `not_null` test that requires it (grade `declared`), or the result of `konstruct verify`, which checked the data (grade `verified`);
- a relationship holds: the dbt `relationships` test (`declared`), or `konstruct verify`'s check of every value (`verified`); its cardinality is in a verified answer only when verify confirmed it (a one-to-one needs verify's own check of the referring column);
- a measure's lineage: only when `konstruct verify` confirmed every lineage edge from the column down to source (`verified`); the statement says its lineage was confirmed, and gives the formula.

A relationship read only from a join, an entity named only by a column's name, and a formula verify has not confirmed are written with their grade (`inferred`, or `declared` for a measure a person defined) and never as a statement. A key or uniqueness that `konstruct verify` found false is not written, and the file's comments say so. A dbt test is evidence that someone declared the claim, not that the data passes it; run `konstruct verify` to check the data.

A key is written as `primaryKey` once for an object: the primary key the relation declares, else the unique key of the entity the relation is named for, else of its first entity; any other unique column is `unique`. A test with a `where` holds only for some rows, so it gives no `unique`, `primaryKey`, `required` or statement, and nulls that `konstruct verify` found remove `required` and the not-null statement.

Not written, because the data stack does not hold them: `servers`, `team`, `roles`, SLAs, `support`, `price`, `slaProperties` and `domain`. A relationship whose names cannot stand in an ODCS reference (one that does not start with a letter or underscore, or holds a character other than letters, digits, `_` and `-`) is left out and listed in the file's comments. `--select` does not apply, and an ontology with no entity or measure in a model exports nothing and exits with status 1.

### Checking a Contract

`konstruct contract check FILE` reads a contract (one YAML document, or several) and tests the claims the graph can answer, without reading the warehouse:

| Claim | Finding |
|-------|---------|
| A table, or a column of it | `missing-table`, `missing-column`: unsupported when the project has no such relation, contradicted when a relation has no such column. A `physicalName` (also of a property) names the relation or column in place of `name`, and may be partly qualified (`main.customers`). A name qualified otherwise than the graph's (`prod.db.main.customers` or `prod_db.main.customers` for `db.main.customers`, where the trailing schema and table match) is `table-qualifier`, unsupported, and the graph's relation is checked in its place. Names are compared in Unicode normal form C, and without the quote characters of a part |
| `physicalType` or `logicalType` | `wrong-type`: contradicted when the type differs from the graph's, as the graph's dialect reads both: Snowflake's `NUMBER(38,0)` is `INTEGER`, `INT` and `BIGINT`, its `FLOAT` is `DOUBLE` and `REAL`, and its `TIMESTAMP_NTZ` is `TIMESTAMP`; BigQuery's `INT64` is every integer; Snowflake's `TEXT` is `VARCHAR`; BigQuery's `FLOAT64` is `FLOAT`; Databricks's `TIMESTAMP` is `TIMESTAMP_LTZ`; elsewhere `INTEGER` and `BIGINT` differ and `STRING` and `VARCHAR` do not. A nested type is compared with what it holds (`ARRAY<STRING>` is not `ARRAY<INT64>`, and `STRUCT` fields are compared by name and type). A precision, scale, length or element type given on one side only is no difference (`NUMERIC` is `NUMERIC(10,2)`, `VARCHAR` is `VARCHAR(20)`); given on both, it must be the same (`NUMERIC(12,2)` and `VARCHAR(50)` are not). A `number` may be claimed of an integer, and a decimal of scale zero is an integer |
| `primaryKey: true`, `unique: true` | `key`, `unique`: contradicted when `konstruct verify` found the column repeated, unsupported when no test, primary key or verify result shows it unique; the properties with `primaryKey: true` of one object are one key, compared with the primary key the graph declares |
| A claim in the wrong shape | `malformed`: contradicted (a property that is not a mapping, a flag that is not true or false, a relationship without `from` and `to`); the contract must have `kind: DataContract` |
| `required: true` | `required`: contradicted when `konstruct verify` found nulls, unsupported when no `not_null` test, primary key or verify result says so; a supported `primaryKey` implies it |
| A `relationships` entry | `relationship`: contradicted only on evidence: `konstruct verify` found the link false or the claimed target's values repeated; unsupported when nothing shows it, the graph has the referring column pointing elsewhere, or the claimed target is not known to be unique. A dbt `relationships` test between relations that hold no entity is a link the check knows |
| `semanticType: measure` | `measure`: contradicted when the column's formula is not an aggregate, unsupported when nothing in the graph computes it |

The command exits 1 when a claim is contradicted, or with `--strict` when any finding remains, and reads standard input when `FILE` is `-`. Konstruct runs no quality rule, which is text for the tools that run it: `contract check` lists each `sql` and predefined rule, at the object or a property, under "Quality rules, not run". `konstruct verify` checks keys and relationships against the data, but does not run a contract's rules. A relation whose text holds a lone surrogate is left out of the export and of the check, and named in the file's comments or the check's notes. Konstruct's own export checks with no findings. `--json` prints the result, and the `contract_check` MCP tool gives an agent the same.
