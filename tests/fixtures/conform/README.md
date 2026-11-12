# Fixtures for `konstruct conform`

Each file is a semantic layer someone else authored, or written to follow what its authors
document, with divergences planted in a stated place. None was produced by Konstruct's exports.

| File | Origin | Planted divergences |
|------|--------|---------------------|
| `snowflake_reference.sql` | The `tpch_rev_analysis` example of Snowflake's `CREATE SEMANTIC VIEW` reference (docs.snowflake.com/en/sql-reference/sql/create-semantic-view), verbatim | none in the file; the test graph plants a required filter on an approved term |
| `snowflake_variant.sql` | The same example with a fact that is a key and a relationship written the other way round | a key under `FACTS`, a reversed relationship, a renamed column |
| `databricks_reference.yaml` | The metric views of Databricks' YAML reference (docs.databricks.com/aws/en/business-semantics/metric-views/yaml-reference): the lineitem view with joins, and the orders view with a view filter | none in the file |
| `databricks_fan_out.yaml` | The orders view of the same reference, joined to the line items by a Databricks join (many-to-one from the source) | a many-to-one join that the code's relationships test says is one-to-many |
| `tmdl_import_metric_view.yaml` | Written by hand as `/importBI` converts a Power BI TMDL measure `CALCULATE(SUM(Orders[o_totalprice]), Orders[o_orderstatus] = "F")`: the CALCULATE filter is not carried over | the dropped filter |
| `ossie_orders.ossie.yaml` | Written by hand to the Apache Ossie specification | none; the reference for `SUM(a * b)` |
| `dbt_semantic_view_jaffle.sql` | Written by hand to follow the output `dbt_semantic_view` generates from the jaffle_shop marts (the generator itself could not be run offline) | a key under `FACTS`, a name-only match |
| `snowflake_model.yaml` | The YAML `SYSTEM$READ_YAML_FROM_SEMANTIC_VIEW` returns, written to Snowflake's semantic model specification, with `verified_queries` | none |
