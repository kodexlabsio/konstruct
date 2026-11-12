//! Snowflake semantic view YAML and Databricks metric view YAML as a [`Doc`].
//!
//! The YAML is parsed by `konstruct.core.yamlio`, the one reader, and arrives here as JSON; this
//! module only gives each platform's keys their place in the document. The shapes are those of
//! Snowflake's semantic model specification (what `SYSTEM$READ_YAML_FROM_SEMANTIC_VIEW` returns,
//! <https://docs.snowflake.com/en/user-guide/views-semantic/semantic-view-yaml-spec>) and of the
//! Databricks metric view YAML reference
//! (<https://docs.databricks.com/aws/en/business-semantics/metric-views/yaml-reference>).

use serde_json::Value;

use super::{Doc, Field, FieldKind, Metric, Relationship, Table, Verified};

fn text(value: &Value, key: &str) -> Option<String> {
    match value.get(key)? {
        Value::String(s) => Some(s.clone()),
        Value::Number(n) => Some(n.to_string()),
        _ => None,
    }
}

fn list<'a>(value: &'a Value, key: &str) -> &'a [Value] {
    value
        .get(key)
        .and_then(Value::as_array)
        .map_or(&[], Vec::as_slice)
}

fn strings(value: &Value, key: &str) -> Vec<String> {
    list(value, key)
        .iter()
        .filter_map(|v| v.as_str().map(str::to_string))
        .collect()
}

/// The dotted name of a Snowflake `base_table`: `{database, schema, table}`.
fn base_table(value: &Value) -> String {
    match value.get("base_table") {
        Some(Value::Object(parts)) => ["database", "schema", "table"]
            .iter()
            .filter_map(|k| parts.get(*k).and_then(Value::as_str))
            .collect::<Vec<_>>()
            .join("."),
        Some(Value::String(s)) => s.clone(),
        _ => String::new(),
    }
}

/// Reads a Snowflake semantic model.
///
/// # Errors
///
/// When the value has no `name` or `tables`, so is not a semantic model.
pub fn snowflake(value: &Value) -> Result<Doc, String> {
    let name = text(value, "name").ok_or("a semantic model has a name")?;
    if !value.get("tables").is_some_and(Value::is_array) {
        return Err("a semantic model has tables".to_string());
    }
    let mut doc = Doc::new("snowflake", &name, "snowflake");
    doc.description = text(value, "description");
    for t in list(value, "tables") {
        let Some(table_name) = text(t, "name") else {
            continue;
        };
        let mut table = Table::new(&table_name, &base_table(t));
        table.description = text(t, "description");
        table.synonyms = strings(t, "synonyms");
        if let Some(key) = t.get("primary_key") {
            table.primary_key = strings(key, "columns");
        }
        for (clause, kind) in [
            ("dimensions", FieldKind::Dimension),
            ("time_dimensions", FieldKind::Dimension),
            ("facts", FieldKind::Fact),
        ] {
            for f in list(t, clause) {
                if let (Some(n), Some(expr)) = (text(f, "name"), text(f, "expr")) {
                    table.fields.push(Field {
                        name: n,
                        expr,
                        kind,
                        synonyms: strings(f, "synonyms"),
                        description: text(f, "description"),
                    });
                }
            }
        }
        for m in list(t, "metrics") {
            if let (Some(n), Some(expr)) = (text(m, "name"), text(m, "expr")) {
                doc.metrics.push(Metric {
                    name: n,
                    table: Some(table_name.clone()),
                    expr,
                    synonyms: strings(m, "synonyms"),
                    description: text(m, "description"),
                    unsupported: None,
                });
            }
        }
        doc.tables.push(table);
    }
    for m in list(value, "metrics") {
        if let (Some(n), Some(expr)) = (text(m, "name"), text(m, "expr")) {
            doc.metrics.push(Metric {
                name: n,
                table: None,
                expr,
                synonyms: strings(m, "synonyms"),
                description: text(m, "description"),
                unsupported: None,
            });
        }
    }
    for rel in list(value, "relationships") {
        let (Some(from), Some(to)) = (text(rel, "left_table"), text(rel, "right_table")) else {
            continue;
        };
        let pairs = list(rel, "relationship_columns");
        let column = |key: &str| {
            pairs
                .iter()
                .filter_map(|p| text(p, key))
                .collect::<Vec<String>>()
        };
        doc.relationships.push(Relationship {
            name: text(rel, "name").unwrap_or_else(|| format!("{from}_{to}")),
            from,
            to,
            from_columns: column("left_column"),
            to_columns: column("right_column"),
            cardinality: text(rel, "relationship_type"),
        });
    }
    for q in list(value, "verified_queries") {
        doc.verified_queries.push(Verified {
            name: text(q, "name").unwrap_or_default(),
            question: text(q, "question"),
            sql: text(q, "sql"),
            verified_at: text(q, "verified_at"),
            verified_by: text(q, "verified_by"),
        });
    }
    Ok(doc)
}

/// Reads a Databricks metric view, called `name` (the view's name, which its YAML does not
/// hold). The one source table is the table `source`; a field or measure over a joined table
/// reads the join by name.
///
/// # Errors
///
/// When the value has no `source`, so is not a metric view.
pub fn databricks(value: &Value, name: &str) -> Result<Doc, String> {
    let source = text(value, "source").ok_or("a metric view has a source")?;
    let mut doc = Doc::new("databricks", name, "databricks");
    doc.description = text(value, "comment");
    doc.filter = text(value, "filter");
    let mut table = Table::new("source", &source);
    // Version 1.1 calls dimensions `fields`.
    for (clause, kind) in [
        ("dimensions", FieldKind::Dimension),
        ("fields", FieldKind::Dimension),
    ] {
        for f in list(value, clause) {
            if let (Some(n), Some(expr)) = (text(f, "name"), text(f, "expr")) {
                table.fields.push(Field {
                    name: n,
                    expr,
                    kind,
                    synonyms: strings(f, "synonyms"),
                    description: text(f, "comment"),
                });
            }
        }
    }
    for m in list(value, "measures") {
        if let (Some(n), Some(expr)) = (text(m, "name"), text(m, "expr")) {
            doc.metrics.push(Metric {
                name: n,
                table: Some("source".to_string()),
                expr,
                synonyms: strings(m, "synonyms"),
                description: text(m, "comment"),
                unsupported: m.get("window").map(|_| {
                    "it runs over a window, which its expression does not hold".to_string()
                }),
            });
        }
    }
    doc.tables.push(table);
    for join in list(value, "joins") {
        let Some(join_name) = text(join, "name") else {
            continue;
        };
        let mut joined = Table::new(&join_name, &text(join, "source").unwrap_or_default());
        joined.description = text(join, "comment");
        doc.tables.push(joined);
        let (from_columns, to_columns) = join_columns(join, &join_name);
        doc.relationships.push(Relationship {
            name: join_name.clone(),
            from: "source".to_string(),
            to: join_name,
            from_columns,
            to_columns,
            // Databricks joins are many-to-one.
            cardinality: Some("many_to_one".to_string()),
        });
    }
    Ok(doc)
}

/// The columns a join of a metric view equates: `on: source.a = j.b`, or `using: [a]`.
fn join_columns(join: &Value, name: &str) -> (Vec<String>, Vec<String>) {
    let using = strings(join, "using");
    if !using.is_empty() {
        return (using.clone(), using);
    }
    // YAML 1.1 reads an unquoted key `on` as the boolean true, so yamlio hands it over as the
    // key "true" once written as JSON; the reference writes both `on:` and `'on':`.
    let Some(on) = text(join, "on").or_else(|| text(join, "true")) else {
        return (Vec::new(), Vec::new());
    };
    let mut from = Vec::new();
    let mut to = Vec::new();
    for clause in on.split(" AND ").flat_map(|c| c.split(" and ")) {
        let Some((left, right)) = clause.split_once('=') else {
            continue;
        };
        let side = |text: &str| text.trim().trim_matches(['(', ')', '`']).to_string();
        let (left, right) = (side(left), side(right));
        let column = |text: &str, table: &str| {
            text.strip_prefix(&format!("{table}."))
                .map(|c| c.trim_matches('`').to_string())
        };
        if let (Some(a), Some(b)) = (column(&left, "source"), column(&right, name)) {
            from.push(a);
            to.push(b);
        } else if let (Some(b), Some(a)) = (column(&left, name), column(&right, "source")) {
            from.push(a);
            to.push(b);
        }
    }
    (from, to)
}
