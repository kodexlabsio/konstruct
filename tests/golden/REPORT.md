# Konstruct report

Built <time> from `<root>` by Konstruct <version> in <seconds>s. SQL dialect: duckdb.

## Summary

| Input | Count |
|-------|-------|
| dbt projects | 1 |
| warehouses | 1 |
| dbt models | 6 |
| airflow dags | 1 |
| dagster assets | 0 |
| dagster ops | 0 |
| sql files | 0 |
| definitions | 6 |

| Node kind | Count |
|-----------|-------|
| column | 48 |
| dag | 1 |
| exposure | 1 |
| model | 6 |
| seed | 3 |
| table | 1 |
| task | 6 |
| term | 6 |
| test | 26 |

| Edge kind | Count |
|-----------|-------|
| contains | 48 |
| defines | 8 |
| derived_from | 40 |
| feeds | 28 |
| precedes | 5 |
| relates_to | 3 |
| runs | 40 |
| tests | 29 |

Column lineage covers 36 of 36 columns of models and SQL-built tables (100%).

## Most used columns

Columns with the most direct dependants (derived columns and joins).

| Column | Dependants |
|--------|------------|
| `stg_payments.amount` | 7 |
| `stg_orders.order_id` | 6 |
| `stg_orders.order_date` | 5 |
| `stg_orders.customer_id` | 4 |
| `stg_payments.payment_method` | 4 |
| `stg_payments.order_id` | 3 |
| `customers.customer_lifetime_value` | 2 |
| `customers.number_of_orders` | 2 |
| `stg_customers.customer_id` | 2 |
| `stg_orders.status` | 2 |

## Business terms

From definitions.yml. Problems with them are listed under Warnings.

| Term | Status | Owner | Columns |
|------|--------|-------|---------|
| Completed order | agreed | Tom Okafor (Operations Manager) | `orders.status` |
| Customer | approved | Tom Okafor (Operations Manager) | `customers.customer_id` |
| Customer lifetime value | approved | Priya Shah (Head of Finance) | `customer_ltv.lifetime_value`, `customers.customer_lifetime_value` |
| Customer name | agreed | Tom Okafor (Operations Manager) | `raw_customers.first_name`, `raw_customers.last_name` |
| Order amount | approved | Priya Shah (Head of Finance) | `orders.amount` |
| Payment method | draft | none | `stg_payments.payment_method` |

4 columns hold an approved definition.

## Orphans

Relations with nothing upstream or downstream, and no task that runs them.

None.

## Untested models

dbt models and snapshots with no test on the model or any of its columns.

None.

## Columns with no lineage

Columns of models and SQL-built tables with no upstream column: constants, columns Konstruct could not resolve, or SQL it could not parse (see Warnings).

None.

## Documented columns missing from the warehouse

Columns described in dbt YAML that the warehouse does not have.

- `customers.total_order_amount`

## Warnings

None.
