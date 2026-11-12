//! What the contracts are made from: the relations of the graph with their columns, tests and
//! verify results, and the ontology's entities, measures and relationships, each with the grade
//! it earned. The Python side gathers them from the graph and hands them over as one JSON
//! document.

use std::collections::HashMap;

use serde_json::Value;

/// What `konstruct verify` found of a column, a key check.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Check {
    pub state: String,
    pub detail: String,
}

#[derive(Debug, Clone, Default)]
pub struct Column {
    pub name: String,
    pub data_type: Option<String>,
    pub description: Option<String>,
    /// A test, primary key or identifier field shows it unique, or verify confirmed it.
    pub unique: bool,
    /// The ids of the dbt `unique` tests on it.
    pub unique_tests: Vec<String>,
    /// The ids of the dbt `not_null` tests on it.
    pub not_null_tests: Vec<String>,
    pub check: Option<Check>,
    /// `konstruct verify` found nulls in it.
    pub nulls: bool,
    /// Whether its formula (or its own expression) aggregates rows, when that was worked out.
    pub aggregate: Option<bool>,
    /// The formula composed down to source, for a column that holds a measure.
    pub formula: Option<String>,
    /// The column's own expression, as the graph holds it.
    pub expression: Option<String>,
}

#[derive(Debug, Clone, Default)]
pub struct Relation {
    pub label: String,
    pub physical: Option<String>,
    pub kind: String,
    pub materialized: Option<String>,
    pub description: Option<String>,
    /// The names of the key columns of a primary key the relation declares.
    pub primary_key: Vec<String>,
    pub columns: Vec<Column>,
}

impl Relation {
    pub fn column(&self, name: &str) -> Option<&Column> {
        self.columns.iter().find(|c| c.name == name).or_else(|| {
            self.columns
                .iter()
                .find(|c| c.name.eq_ignore_ascii_case(name))
        })
    }

    /// Whether `name` is a column of the relation's declared primary key.
    pub fn in_primary_key(&self, name: &str) -> bool {
        self.primary_key
            .iter()
            .any(|k| k.eq_ignore_ascii_case(name))
    }
}

#[derive(Debug, Clone, Default)]
pub struct Entity {
    pub name: String,
    pub term: Option<String>,
    pub confidence: String,
    pub relations: Vec<String>,
    /// The key columns known to be unique, as (relation label, column).
    pub unique_keys: Vec<(String, String)>,
}

#[derive(Debug, Clone, Default)]
pub struct MeasureColumn {
    pub relation: String,
    pub column: String,
    pub formula: Option<String>,
    pub sources: Vec<String>,
    pub complete: bool,
    pub aggregate: Option<bool>,
    pub verified: u64,
    pub edges: u64,
}

#[derive(Debug, Clone, Default)]
pub struct Measure {
    pub name: String,
    pub confidence: String,
    pub columns: Vec<MeasureColumn>,
}

/// A relationship between two columns, as (relation label, column).
#[derive(Debug, Clone, Default)]
pub struct Link {
    pub from: (String, String),
    pub to: (String, String),
    pub cardinality: Option<String>,
    pub confidence: String,
    /// `confirmed` or `failed`, from `konstruct verify`.
    pub check: Option<String>,
    /// What `konstruct verify` said, as the ontology records it.
    pub verify: Vec<String>,
    /// The ids of the dbt `relationships` tests that vouch for it.
    pub tests: Vec<String>,
    /// `konstruct verify` confirmed that the referring column is unique too.
    pub one_to_one_confirmed: bool,
}

/// A warehouse type as the Python side reads it in the graph's dialect: the type, and the
/// precision and scale of a decimal. Types that are one in the dialect have one key
/// (`NUMBER(38,0)`, `INT` and `BIGINT` in Snowflake).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TypeKey {
    pub base: String,
    pub params: Option<Params>,
}

/// What a type is parameterised by.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Params {
    /// A decimal's precision and scale, or a string's length.
    Text(String),
    /// The element or field types of a nested type, each with its field's name, if it has one.
    Nested(Vec<(Option<String>, TypeKey)>),
}

impl Params {
    fn same(&self, other: &Self) -> bool {
        match (self, other) {
            (Self::Text(a), Self::Text(b)) => a == b,
            (Self::Nested(a), Self::Nested(b)) => {
                a.len() == b.len() && a.iter().zip(b).all(|((n, x), (m, y))| n == m && x.same(y))
            }
            _ => false,
        }
    }

    fn read(value: &Value) -> Option<Self> {
        match value {
            Value::String(text) => Some(Self::Text(text.clone())),
            Value::Array(items) => Some(Self::Nested(
                items
                    .iter()
                    .filter_map(|item| {
                        Some((
                            item.get(0)?.as_str().map(str::to_owned),
                            TypeKey::read(item.get(1)?)?,
                        ))
                    })
                    .collect(),
            )),
            _ => None,
        }
    }
}

impl TypeKey {
    /// The same type: one base, and the same parameters unless either side has none, as `NUMERIC`
    /// is every `NUMERIC(p, s)`.
    pub fn same(&self, other: &Self) -> bool {
        self.base == other.base
            && match (&self.params, &other.params) {
                (Some(a), Some(b)) => a.same(b),
                _ => true,
            }
    }

    fn read(value: &Value) -> Option<Self> {
        Some(Self {
            base: value.get(0)?.as_str()?.to_owned(),
            params: value.get(1).and_then(Params::read),
        })
    }
}

#[derive(Debug, Default)]
pub struct Facts {
    /// The type of every type name the facts and the contracts use, by the name as written.
    types: HashMap<String, TypeKey>,
    /// The relations by label, by lower-case label, and by each lower-case dotted suffix of the
    /// physical name (`db.main.t`, `main.t`, `t`).
    labels: HashMap<String, Vec<usize>>,
    suffixes: HashMap<String, Vec<usize>>,
    /// The relations by the whole of their physical name, or by label where they have none.
    whole: HashMap<String, Vec<usize>>,
    /// The relations by the last two parts of their physical name (`main.customers`).
    schema_table: HashMap<String, Vec<usize>>,
    pub project: Option<String>,
    /// The relations the Python side left out, each with why, for the caller to tell its reader.
    pub skipped: Vec<String>,
    pub relations: Vec<Relation>,
    pub entities: Vec<Entity>,
    pub measures: Vec<Measure>,
    pub links: Vec<Link>,
}

/// The relations a name can mean.
pub enum Lookup<'a> {
    One(&'a Relation),
    Many,
    None,
}

/// A name as the suffix index holds it: in lower case, without the characters that quote a part.
fn plain(name: &str) -> String {
    name.chars()
        .filter(|c| !matches!(c, '"' | '`' | '[' | ']'))
        .collect::<String>()
        .to_lowercase()
}

fn string(value: &Value, key: &str) -> Option<String> {
    value.get(key)?.as_str().map(str::to_owned)
}

fn required(value: &Value, key: &str) -> Result<String, String> {
    string(value, key).ok_or_else(|| format!("facts: missing {key}"))
}

fn strings(value: &Value, key: &str) -> Vec<String> {
    value
        .get(key)
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(|v| v.as_str().map(str::to_owned))
                .collect()
        })
        .unwrap_or_default()
}

fn array<'a>(value: &'a Value, key: &str) -> &'a [Value] {
    value
        .get(key)
        .and_then(Value::as_array)
        .map_or(&[], Vec::as_slice)
}

fn pair(value: &Value) -> Option<(String, String)> {
    let items = value.as_array()?;
    Some((
        items.first()?.as_str()?.to_owned(),
        items.get(1)?.as_str()?.to_owned(),
    ))
}

fn column(value: &Value) -> Result<Column, String> {
    Ok(Column {
        name: required(value, "name")?,
        data_type: string(value, "type"),
        description: string(value, "description"),
        unique: value
            .get("unique")
            .and_then(Value::as_bool)
            .unwrap_or(false),
        unique_tests: strings(value, "unique_tests"),
        not_null_tests: strings(value, "not_null_tests"),
        nulls: value.get("nulls").and_then(Value::as_bool).unwrap_or(false),
        aggregate: value.get("aggregate").and_then(Value::as_bool),
        check: value.get("check").and_then(|c| {
            Some(Check {
                state: string(c, "state")?,
                detail: string(c, "detail").unwrap_or_default(),
            })
        }),
        formula: string(value, "formula"),
        expression: string(value, "expression"),
    })
}

impl Facts {
    /// The type a name stands for. A name the Python side gave no reading of (one it could not
    /// parse) stands for itself, in upper case.
    pub fn type_key(&self, written: &str) -> TypeKey {
        self.types.get(written).cloned().unwrap_or_else(|| TypeKey {
            base: written.trim().to_ascii_uppercase(),
            params: None,
        })
    }

    /// Reads the JSON document the Python side builds.
    pub fn parse(source: &str) -> Result<Self, String> {
        let root: Value = serde_json::from_str(source).map_err(|e| format!("facts: {e}"))?;
        let mut facts = Self {
            project: string(&root, "project"),
            skipped: strings(&root, "skipped"),
            ..Self::default()
        };
        if let Some(types) = root.get("types").and_then(Value::as_object) {
            for (written, key) in types {
                if let Some(key) = TypeKey::read(key) {
                    facts.types.insert(written.clone(), key);
                }
            }
        }
        for item in array(&root, "relations") {
            facts.relations.push(Relation {
                label: required(item, "label")?,
                physical: string(item, "physical"),
                kind: string(item, "kind").unwrap_or_default(),
                materialized: string(item, "materialized"),
                description: string(item, "description"),
                primary_key: strings(item, "primary_key"),
                columns: array(item, "columns")
                    .iter()
                    .map(column)
                    .collect::<Result<_, _>>()?,
            });
        }
        for item in array(&root, "entities") {
            facts.entities.push(Entity {
                name: required(item, "name")?,
                term: string(item, "term"),
                confidence: string(item, "confidence").unwrap_or_else(|| "inferred".into()),
                relations: strings(item, "relations"),
                unique_keys: array(item, "unique_keys").iter().filter_map(pair).collect(),
            });
        }
        for item in array(&root, "measures") {
            let mut columns = Vec::new();
            for c in array(item, "columns") {
                columns.push(MeasureColumn {
                    relation: required(c, "relation")?,
                    column: required(c, "column")?,
                    formula: string(c, "formula"),
                    sources: strings(c, "sources"),
                    complete: c.get("complete").and_then(Value::as_bool).unwrap_or(false),
                    aggregate: c.get("aggregate").and_then(Value::as_bool),
                    verified: c.get("verified").and_then(Value::as_u64).unwrap_or(0),
                    edges: c.get("edges").and_then(Value::as_u64).unwrap_or(0),
                });
            }
            facts.measures.push(Measure {
                name: required(item, "name")?,
                confidence: string(item, "confidence").unwrap_or_else(|| "inferred".into()),
                columns,
            });
        }
        for item in array(&root, "relationships") {
            let (Some(from), Some(to)) = (
                item.get("from").and_then(pair),
                item.get("to").and_then(pair),
            ) else {
                continue;
            };
            facts.links.push(Link {
                from,
                to,
                cardinality: string(item, "cardinality"),
                confidence: string(item, "confidence").unwrap_or_else(|| "inferred".into()),
                check: string(item, "check"),
                verify: strings(item, "verify"),
                tests: strings(item, "tests"),
                one_to_one_confirmed: item
                    .get("one_to_one_confirmed")
                    .and_then(Value::as_bool)
                    .unwrap_or(false),
            });
        }
        for (i, r) in facts.relations.iter().enumerate() {
            facts.labels.entry(r.label.clone()).or_default().push(i);
            if r.label.to_lowercase() != r.label {
                facts
                    .labels
                    .entry(r.label.to_lowercase())
                    .or_default()
                    .push(i);
            }
            let whole = r
                .physical
                .as_deref()
                .map_or_else(|| r.label.to_lowercase(), plain);
            facts.whole.entry(whole).or_default().push(i);
            if let Some(physical) = &r.physical {
                let lower = plain(physical);
                let parts: Vec<&str> = lower.split('.').collect();
                if parts.len() > 2 {
                    facts
                        .schema_table
                        .entry(parts[parts.len() - 2..].join("."))
                        .or_default()
                        .push(i);
                }
                let mut rest = lower.as_str();
                loop {
                    facts.suffixes.entry(rest.to_owned()).or_default().push(i);
                    match rest.split_once('.') {
                        Some((_, tail)) => rest = tail,
                        None => break,
                    }
                }
            }
        }
        Ok(facts)
    }

    /// The relation with this label.
    pub fn relation(&self, label: &str) -> Option<&Relation> {
        self.labels
            .get(label)
            .and_then(|v| v.first())
            .map(|&i| &self.relations[i])
    }

    /// The relation a contract's table means by `name`, a label (in any case).
    pub fn by_label(&self, name: &str) -> Lookup<'_> {
        let hit = self
            .labels
            .get(name)
            .or_else(|| self.labels.get(&name.to_lowercase()));
        self.one(hit)
    }

    /// The relation a physical name means: the whole name, or a dotted suffix of the relation's,
    /// so `main.customers` is `analytics.main.customers`. Quotes around parts are ignored.
    pub fn by_physical(&self, name: &str) -> Lookup<'_> {
        self.one(self.suffixes.get(&plain(name)))
    }

    /// The relation a name qualified otherwise than the graph's means. From the most specific
    /// reading to the least, the first that matches decides: a whole physical name (or label, where
    /// it has no physical name) that is a tail of `name` of two parts or more, so
    /// `prod.db.main.customers` means `db.main.customers`; a relation whose schema and table are
    /// the last two parts of `name`, so `prod_db.main.customers` means `jaffle_shop.main.customers`;
    /// and last a bare name that is the last part. Within one reading, two relations are `Many`.
    pub fn by_tail(&self, name: &str) -> Lookup<'_> {
        let whole = plain(name);
        let parts: Vec<&str> = whole.split('.').collect();
        // Tails of two parts or more, longest first, without the whole name itself.
        for from in 1..parts.len().saturating_sub(1) {
            if let hit @ Some(_) = self.whole.get(&parts[from..].join(".")) {
                return self.one(hit);
            }
        }
        if parts.len() > 2
            && let hit @ Some(_) = self.schema_table.get(&parts[parts.len() - 2..].join("."))
        {
            return self.one(hit);
        }
        match parts.last().filter(|_| parts.len() > 1) {
            Some(bare) => self.one(self.whole.get(*bare)),
            None => Lookup::None,
        }
    }

    fn one(&self, hit: Option<&Vec<usize>>) -> Lookup<'_> {
        let mut distinct: Vec<usize> = hit.cloned().unwrap_or_default();
        distinct.sort_unstable();
        distinct.dedup();
        match distinct.as_slice() {
            [] => Lookup::None,
            [one] => Lookup::One(&self.relations[*one]),
            _ => Lookup::Many,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_type_is_the_same_unless_both_sides_give_other_parameters() {
        let facts = Facts::parse(
            r#"{"types": {"NUMERIC(10,2)": ["DECIMAL", "10,2"], "NUMERIC": ["DECIMAL", null],
                          "NUMERIC(12,2)": ["DECIMAL", "12,2"]}}"#,
        )
        .unwrap();
        let (a, b, c) = (
            facts.type_key("NUMERIC(10,2)"),
            facts.type_key("NUMERIC"),
            facts.type_key("NUMERIC(12,2)"),
        );
        assert!(a.same(&b) && b.same(&a) && !a.same(&c));
        // A name nobody read stands for itself.
        assert!(facts.type_key("geo").same(&facts.type_key("GEO")));
        assert!(!facts.type_key("geo").same(&a));
    }

    #[test]
    fn reads_the_document_the_python_side_writes() {
        let facts = Facts::parse(
            r#"{"project": "shop",
                "relations": [{"label": "orders", "kind": "model", "columns": [
                    {"name": "id", "type": "INTEGER", "unique": true, "unique_tests": ["test.a"],
                     "check": {"state": "confirmed", "detail": "ok"}}]}],
                "entities": [{"name": "order", "relations": ["orders"], "unique_keys": [["orders", "id"]]}],
                "measures": [{"name": "Revenue", "confidence": "declared", "columns": [
                    {"relation": "orders", "column": "amount", "formula": "SUM(x)", "complete": true,
                     "verified": 1, "edges": 1}]}],
                "relationships": [{"from": ["orders", "c"], "to": ["customers", "id"], "tests": ["t"]}]}"#,
        )
        .unwrap();
        assert_eq!(facts.project.as_deref(), Some("shop"));
        let column = facts.relation("orders").unwrap().column("ID").unwrap();
        assert!(column.unique);
        assert_eq!(column.check.as_ref().unwrap().state, "confirmed");
        assert_eq!(
            facts.entities[0].unique_keys,
            [("orders".to_owned(), "id".to_owned())]
        );
        assert_eq!(facts.measures[0].columns[0].verified, 1);
        assert_eq!(facts.links[0].confidence, "inferred");
        assert_eq!(facts.links[0].tests, ["t"]);
        assert!(matches!(facts.by_physical("main.orders"), Lookup::None));
    }

    #[test]
    fn quotes_are_ignored_on_both_sides_and_a_longer_name_means_the_whole_one() {
        let facts = Facts::parse(
            r#"{"relations": [{"label": "t", "physical": "\"db\".`main`.[t]", "columns": []},
                              {"label": "u", "columns": []}]}"#,
        )
        .unwrap();
        assert!(matches!(facts.by_physical("main.t"), Lookup::One(_)));
        assert!(matches!(
            facts.by_physical("\"main\".\"t\""),
            Lookup::One(_)
        ));
        assert!(matches!(facts.by_tail("prod.db.main.t"), Lookup::One(r) if r.label == "t"));
        // Another schema's name is no longer name of the graph's, and a label is a whole name only
        // for a relation with no physical name.
        assert!(matches!(facts.by_tail("prod.other.t"), Lookup::None));
        assert!(matches!(facts.by_tail("prod.u"), Lookup::One(r) if r.label == "u"));
        assert!(matches!(facts.by_tail("prod.t"), Lookup::None));
        // The same schema and table in another database is a qualifier that differs.
        assert!(matches!(facts.by_tail("prod_db.main.t"), Lookup::One(r) if r.label == "t"));
        assert!(matches!(facts.by_tail("prod_db.other.t"), Lookup::None));
    }

    #[test]
    fn a_schema_and_table_match_comes_before_a_bare_name() {
        let facts = Facts::parse(
            r#"{"relations": [{"label": "customers", "columns": []},
                              {"label": "dim", "physical": "db.main.customers", "columns": []}]}"#,
        )
        .unwrap();
        assert!(matches!(facts.by_tail("x.main.customers"), Lookup::One(r) if r.label == "dim"));
        // With no schema to go by, the bare name is what is left.
        assert!(matches!(facts.by_tail("x.y.customers"), Lookup::One(r) if r.label == "customers"));
        // Two relations that the same schema and table name are not told apart.
        let twice = Facts::parse(
            r#"{"relations": [{"label": "a", "physical": "p.main.t", "columns": []},
                              {"label": "b", "physical": "q.main.t", "columns": []}]}"#,
        )
        .unwrap();
        assert!(matches!(twice.by_tail("x.main.t"), Lookup::Many));
    }

    #[test]
    fn a_missing_label_or_bad_json_is_an_error() {
        assert!(Facts::parse("{").is_err());
        assert!(Facts::parse(r#"{"relations": [{"kind": "model"}]}"#).is_err());
    }
}
