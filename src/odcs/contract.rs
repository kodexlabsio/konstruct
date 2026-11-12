//! The ODCS 3.2 contract as Konstruct writes and reads it. The field names are those of the
//! `odcs-json-schema-v3.2.0.json` the tests validate against; only the fields Konstruct writes or
//! checks are modelled, and the reader ignores the rest.

use super::yaml::Yaml;
use serde_json::Value as Json;

use super::yaml::{Map, text, texts};

pub const API_VERSION: &str = "v3.2.0";

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Value {
    Text(String),
    List(Vec<String>),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Custom {
    pub property: String,
    pub value: Value,
}

impl Custom {
    pub fn text(property: &str, value: &str) -> Self {
        Self {
            property: property.to_owned(),
            value: Value::Text(value.to_owned()),
        }
    }

    pub fn list(property: &str, values: &[String]) -> Self {
        Self {
            property: property.to_owned(),
            value: Value::List(values.to_vec()),
        }
    }

    fn yaml(&self) -> Yaml {
        let mut map = Map::new();
        map.put_text("property", &self.property);
        map.put(
            "value",
            match &self.value {
                Value::Text(value) => text(value),
                Value::List(values) => texts(values),
            },
        );
        map.build()
    }
}

/// `raw` as an ODCS stable id, which may not hold `.`, `#`, `/`, `\`, `@`, `!`, `%`, `&`, `^` or
/// white space.
pub fn stable_id(raw: &str) -> String {
    raw.chars()
        .map(|c| {
            if c.is_whitespace() || ".#/\\@!%&^".contains(c) {
                '_'
            } else {
                c
            }
        })
        .collect()
}

/// Whether `name` can stand in a shorthand reference such as `orders.customer_id`.
pub fn referable(name: &str) -> bool {
    let mut chars = name.chars();
    chars
        .next()
        .is_some_and(|c| c.is_ascii_alphabetic() || c == '_')
        && chars.all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
}

fn customs(map: &mut Map, items: &[Custom]) {
    map.put_list("customProperties", items.iter().map(Custom::yaml).collect());
}

/// A foreign key: `from` columns refer to `to` columns, each as `table.column`.
#[derive(Debug, Clone, Default)]
pub struct Link {
    pub from: Vec<String>,
    pub to: Vec<String>,
    pub custom: Vec<Custom>,
}

fn reference(items: &[String]) -> Yaml {
    match items {
        [one] => text(one),
        many => texts(many),
    }
}

impl Link {
    fn yaml(&self, id: &str, implicit_from: bool) -> Yaml {
        let mut map = Map::new();
        map.put_text("id", &stable_id(id))
            .put_text("type", "foreignKey");
        if !implicit_from {
            map.put("from", reference(&self.from));
        }
        map.put("to", reference(&self.to));
        customs(&mut map, &self.custom);
        map.build()
    }
}

#[derive(Debug, Clone)]
pub enum RuleKind {
    /// A predefined metric: `nullValues`, `duplicateValues`, ...
    Library(String),
    Sql(String),
    /// Any other type, as written.
    Other(String),
}

#[derive(Debug, Clone)]
pub struct Rule {
    pub name: Option<String>,
    pub description: Option<String>,
    pub dimension: Option<String>,
    pub severity: Option<String>,
    pub kind: RuleKind,
    pub must_be: Option<i64>,
    pub custom: Vec<Custom>,
}

impl Rule {
    fn yaml(&self) -> Yaml {
        let mut map = Map::new();
        map.put_some("name", self.name.as_deref())
            .put_some("description", self.description.as_deref());
        match &self.kind {
            RuleKind::Library(metric) => {
                map.put_text("type", "library").put_text("metric", metric);
            }
            RuleKind::Sql(query) => {
                map.put_text("type", "sql").put_text("query", query);
            }
            RuleKind::Other(kind) => {
                map.put_text("type", kind);
            }
        }
        map.put_some("dimension", self.dimension.as_deref())
            .put_some("severity", self.severity.as_deref());
        if let Some(value) = self.must_be {
            map.put("mustBe", Yaml::Integer(value));
        }
        customs(&mut map, &self.custom);
        map.build()
    }
}

#[derive(Debug, Clone)]
pub struct Statement {
    pub id: String,
    pub question: String,
    pub answer: String,
    pub custom: Vec<Custom>,
}

#[derive(Debug, Clone, Default)]
pub struct Property {
    pub name: String,
    /// The column's name where it differs from `name` (read from a contract).
    pub physical_name: Option<String>,
    pub physical_type: Option<String>,
    pub logical_type: Option<String>,
    pub description: Option<String>,
    pub primary_key: Option<bool>,
    pub primary_key_position: Option<i64>,
    pub required: Option<bool>,
    pub unique: Option<bool>,
    pub semantic_type: Option<String>,
    pub transform_logic: Option<String>,
    pub transform_sources: Vec<String>,
    pub synonyms: Vec<String>,
    pub relationships: Vec<Link>,
    pub quality: Vec<Rule>,
    pub custom: Vec<Custom>,
}

fn synonyms(map: &mut Map, names: &[String]) {
    map.put_list(
        "synonyms",
        names
            .iter()
            .map(|name| {
                let mut entry = Map::new();
                entry
                    .put_text("synonym", name)
                    .put_text("source", "konstruct");
                entry.build()
            })
            .collect(),
    );
}

impl Property {
    fn yaml(&self, table: &str) -> Yaml {
        let mut map = Map::new();
        map.put_text("name", &self.name)
            .put_some("physicalType", self.physical_type.as_deref())
            .put_some("logicalType", self.logical_type.as_deref())
            .put_some("description", self.description.as_deref())
            .put_flag("primaryKey", self.primary_key);
        if let (Some(true), Some(position)) = (self.primary_key, self.primary_key_position) {
            map.put("primaryKeyPosition", Yaml::Integer(position));
        }
        map.put_flag("required", self.required)
            .put_flag("unique", self.unique)
            .put_some("semanticType", self.semantic_type.as_deref())
            .put_some("transformLogic", self.transform_logic.as_deref())
            .put_list(
                "transformSourceObjects",
                self.transform_sources.iter().map(|s| text(s)).collect(),
            );
        synonyms(&mut map, &self.synonyms);
        map.put_list(
            "relationships",
            self.relationships
                .iter()
                .enumerate()
                .map(|(i, link)| link.yaml(&format!("{table}_{}_{}", self.name, i + 1), true))
                .collect(),
        );
        map.put_list("quality", self.quality.iter().map(Rule::yaml).collect());
        customs(&mut map, &self.custom);
        map.build()
    }
}

#[derive(Debug, Clone, Default)]
pub struct Object {
    pub name: String,
    pub physical_name: Option<String>,
    pub physical_type: Option<String>,
    pub description: Option<String>,
    pub business_name: Option<String>,
    pub synonyms: Vec<String>,
    pub properties: Vec<Property>,
    pub relationships: Vec<Link>,
    pub quality: Vec<Rule>,
    pub statements: Vec<Statement>,
    pub custom: Vec<Custom>,
}

impl Object {
    fn yaml(&self) -> Yaml {
        let mut map = Map::new();
        map.put_text("name", &self.name)
            .put_some("physicalName", self.physical_name.as_deref())
            .put_some("physicalType", self.physical_type.as_deref())
            .put_some("description", self.description.as_deref())
            .put_some("businessName", self.business_name.as_deref());
        synonyms(&mut map, &self.synonyms);
        map.put_list(
            "properties",
            self.properties.iter().map(|p| p.yaml(&self.name)).collect(),
        );
        map.put_list(
            "relationships",
            self.relationships
                .iter()
                .enumerate()
                .map(|(i, link)| link.yaml(&format!("{}_link_{}", self.name, i + 1), false))
                .collect(),
        );
        map.put_list("quality", self.quality.iter().map(Rule::yaml).collect());
        if !self.statements.is_empty() {
            let mut context = Map::new();
            context.put_list(
                "verifiedStatements",
                self.statements
                    .iter()
                    .map(|s| {
                        let mut entry = Map::new();
                        entry
                            .put_text("id", &stable_id(&s.id))
                            .put_text("question", &s.question)
                            .put_text("answer", &s.answer);
                        customs(&mut entry, &s.custom);
                        entry.build()
                    })
                    .collect(),
            );
            map.put("context", context.build());
        }
        customs(&mut map, &self.custom);
        map.build()
    }
}

#[derive(Debug, Clone, Default)]
pub struct Contract {
    pub id: String,
    pub version: String,
    pub name: String,
    pub status: Option<String>,
    pub purpose: Option<String>,
    pub instructions: Option<String>,
    pub schema: Vec<Object>,
}

impl Contract {
    pub fn yaml(&self) -> Yaml {
        let mut map = Map::new();
        map.put_text("apiVersion", API_VERSION)
            .put_text("kind", "DataContract")
            .put_text("id", &self.id)
            .put_text("version", &self.version)
            .put_text("name", &self.name)
            .put_some("status", self.status.as_deref());
        if let Some(purpose) = &self.purpose {
            let mut description = Map::new();
            description.put_text("purpose", purpose);
            map.put("description", description.build());
        }
        if let Some(instructions) = &self.instructions {
            let mut context = Map::new();
            context.put_text("instructions", instructions);
            map.put("context", context.build());
        }
        map.put_list("schema", self.schema.iter().map(Object::yaml).collect());
        map.build()
    }

    /// Reads the claims of a contract that PyYAML parsed (as JSON). A claim in the wrong shape is
    /// not dropped: it is recorded in `problems` as a place and what is wrong with it.
    pub fn read(document: &Json, problems: &mut Vec<(String, String)>) -> Result<Self, String> {
        let Some(map) = document.as_object() else {
            return Err("not a data contract: it is not a mapping".into());
        };
        if map.get("kind").and_then(Json::as_str) != Some("DataContract") {
            return Err("not a data contract: `kind` must be DataContract".into());
        }
        let mut contract = Self {
            id: json_text(document, "id").unwrap_or_default(),
            version: json_text(document, "version").unwrap_or_default(),
            name: json_text(document, "name").unwrap_or_default(),
            ..Self::default()
        };
        let Some(schema) = list(document, "schema", "schema", problems) else {
            return Ok(contract);
        };
        for (i, item) in schema.iter().enumerate() {
            let at = format!("schema[{i}]");
            if !item.is_object() {
                problems.push((at, "is not a mapping".into()));
                continue;
            }
            let physical_name = json_text(item, "physicalName");
            let Some(name) = json_text(item, "name").or_else(|| physical_name.clone()) else {
                problems.push((at, "has no name".into()));
                continue;
            };
            let mut object = Object {
                name: name.clone(),
                physical_name,
                ..Object::default()
            };
            if let Some(properties) = list(item, "properties", &name, problems) {
                for (j, entry) in properties.iter().enumerate() {
                    if let Some(read) = read_property(entry, &name, j, problems) {
                        object.properties.push(read);
                    }
                }
            }
            object.relationships = read_links(item, &name, None, problems);
            object.quality = read_rules(item, &name, problems);
            contract.schema.push(object);
        }
        Ok(contract)
    }
}

fn json_text(node: &Json, key: &str) -> Option<String> {
    match node.get(key)? {
        Json::String(value) => Some(value.clone()),
        Json::Number(value) => Some(value.to_string()),
        Json::Bool(value) => Some(value.to_string()),
        _ => None,
    }
}

/// The list at `key`, or None (after recording it) when it is something else; absent is empty.
fn list<'a>(
    node: &'a Json,
    key: &str,
    at: &str,
    problems: &mut Vec<(String, String)>,
) -> Option<&'a Vec<Json>> {
    match node.get(key) {
        None | Some(Json::Null) => None,
        Some(Json::Array(items)) => Some(items),
        Some(_) => {
            problems.push((at.to_owned(), format!("`{key}` is not a list")));
            None
        }
    }
}

/// A true or false claim; one that is anything else is recorded.
fn flag(node: &Json, key: &str, at: &str, problems: &mut Vec<(String, String)>) -> Option<bool> {
    match node.get(key) {
        None | Some(Json::Null) => None,
        Some(Json::Bool(value)) => Some(*value),
        Some(_) => {
            problems.push((at.to_owned(), format!("`{key}` is not true or false")));
            None
        }
    }
}

fn read_property(
    entry: &Json,
    table: &str,
    index: usize,
    problems: &mut Vec<(String, String)>,
) -> Option<Property> {
    if !entry.is_object() {
        problems.push((
            table.to_owned(),
            format!("property {} is not a mapping", index + 1),
        ));
        return None;
    }
    let physical_name = json_text(entry, "physicalName");
    let Some(name) = json_text(entry, "name").or_else(|| physical_name.clone()) else {
        problems.push((
            table.to_owned(),
            format!("property {} has no name", index + 1),
        ));
        return None;
    };
    let at = format!("{table}.{name}");
    let mut read = Property {
        physical_name,
        physical_type: json_text(entry, "physicalType"),
        logical_type: json_text(entry, "logicalType"),
        primary_key: flag(entry, "primaryKey", &at, problems),
        primary_key_position: entry.get("primaryKeyPosition").and_then(Json::as_i64),
        required: flag(entry, "required", &at, problems),
        unique: flag(entry, "unique", &at, problems),
        semantic_type: json_text(entry, "semanticType"),
        quality: read_rules(entry, &at, problems),
        ..Property::default()
    };
    read.relationships = read_links(entry, &at, Some(&at), problems);
    read.name = name;
    Some(read)
}

/// The references of a link side: one text, or a list of them.
fn references(link: &Json, key: &str) -> Option<Vec<String>> {
    match link.get(key)? {
        Json::String(one) => Some(vec![one.clone()]),
        Json::Array(many) if !many.is_empty() => {
            many.iter().map(|y| y.as_str().map(str::to_owned)).collect()
        }
        _ => None,
    }
}

/// The relationships of an object, or of a property (whose `from` is the property itself).
fn read_links(
    node: &Json,
    at: &str,
    implicit: Option<&str>,
    problems: &mut Vec<(String, String)>,
) -> Vec<Link> {
    let mut links = Vec::new();
    let Some(items) = list(node, "relationships", at, problems) else {
        return links;
    };
    for (i, item) in items.iter().enumerate() {
        let from = match implicit {
            Some(own) => Some(vec![own.to_owned()]),
            None => references(item, "from"),
        };
        let to = references(item, "to");
        match (from, to) {
            (Some(from), Some(to)) if item.is_object() && from.len() == to.len() => {
                links.push(Link {
                    from,
                    to,
                    custom: Vec::new(),
                });
            }
            (Some(_), Some(_)) => problems.push((
                at.to_owned(),
                format!(
                    "relationship {} has `from` and `to` of different lengths",
                    i + 1
                ),
            )),
            _ => problems.push((
                at.to_owned(),
                format!(
                    "relationship {} needs a `from` and a `to`, each text or a list of text",
                    i + 1
                ),
            )),
        }
    }
    links
}

fn read_rules(node: &Json, at: &str, problems: &mut Vec<(String, String)>) -> Vec<Rule> {
    let mut rules = Vec::new();
    let Some(items) = list(node, "quality", at, problems) else {
        return rules;
    };
    for item in items {
        if item.is_object() {
            rules.push(read_rule(item));
        } else {
            problems.push((at.to_owned(), "a quality rule is not a mapping".into()));
        }
    }
    rules
}

fn read_rule(entry: &Json) -> Rule {
    let kind = match json_text(entry, "type").as_deref() {
        Some("sql") => RuleKind::Sql(json_text(entry, "query").unwrap_or_default()),
        Some("library") | None if entry.get("metric").is_some() => {
            RuleKind::Library(json_text(entry, "metric").unwrap_or_default())
        }
        other => RuleKind::Other(other.unwrap_or("text").to_owned()),
    };
    Rule {
        name: json_text(entry, "name"),
        description: json_text(entry, "description"),
        dimension: json_text(entry, "dimension"),
        severity: json_text(entry, "severity"),
        kind,
        must_be: entry.get("mustBe").and_then(Json::as_i64),
        custom: Vec::new(),
    }
}

/// The ODCS logical type of a warehouse type, when it has one: `string`, `integer`, `number`,
/// `boolean`, `date`, `timestamp`, `time`, `array`, `object` or `map`. A decimal with no scale
/// (`NUMBER(38,0)`, `DECIMAL(10)`) is an integer.
pub fn logical_type(physical: &str) -> Option<&'static str> {
    let upper = physical.trim().to_ascii_uppercase();
    if upper.ends_with("[]") || upper.starts_with("ARRAY") {
        return Some("array");
    }
    let base = upper
        .split(['(', '<', ' '])
        .next()
        .unwrap_or_default()
        .trim_end_matches("[]");
    if upper.starts_with("TIMESTAMP") || upper.starts_with("TIME WITH") || base == "DATETIME" {
        return Some(if base == "TIME" { "time" } else { "timestamp" });
    }
    if matches!(base, "DECIMAL" | "NUMERIC" | "NUMBER") && scale_zero(&upper) {
        return Some("integer");
    }
    Some(match base {
        "VARCHAR" | "CHAR" | "CHARACTER" | "NVARCHAR" | "NCHAR" | "TEXT" | "STRING" | "CLOB"
        | "BPCHAR" | "UUID" | "CITEXT" => "string",
        "INT" | "INTEGER" | "BIGINT" | "SMALLINT" | "TINYINT" | "HUGEINT" | "INT2" | "INT4"
        | "INT8" | "INT16" | "INT32" | "INT64" | "LONG" | "UBIGINT" | "UINTEGER" | "USMALLINT"
        | "UTINYINT" | "SERIAL" | "BIGSERIAL" | "BYTEINT" => "integer",
        "DECIMAL" | "NUMERIC" | "NUMBER" | "FLOAT" | "FLOAT4" | "FLOAT8" | "FLOAT32"
        | "FLOAT64" | "DOUBLE" | "REAL" | "MONEY" | "BIGNUMERIC" | "BIGDECIMAL" => "number",
        "BOOLEAN" | "BOOL" => "boolean",
        "DATE" => "date",
        "TIME" => "time",
        "TIMESTAMP" | "TIMESTAMPTZ" | "TIMESTAMP_NTZ" | "TIMESTAMP_LTZ" | "TIMESTAMP_TZ" => {
            "timestamp"
        }
        "STRUCT" | "OBJECT" | "JSON" | "JSONB" | "VARIANT" | "ROW" => "object",
        "MAP" => "map",
        _ => return None,
    })
}

/// Whether a decimal type's parameters say a scale of zero: `(p)` or `(p, 0)`.
fn scale_zero(upper: &str) -> bool {
    let Some((_, rest)) = upper.split_once('(') else {
        return false;
    };
    let inside = rest.split(')').next().unwrap_or_default();
    let mut parts = inside.split(',').map(str::trim);
    let precision = parts.next().is_some_and(|p| p.parse::<u32>().is_ok());
    precision && parts.next().is_none_or(|s| s == "0") && parts.next().is_none()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::odcs::yaml::emit;
    use serde_json::json;

    #[test]
    fn logical_types_cover_the_common_warehouse_types() {
        for (physical, logical) in [
            ("VARCHAR(20)", "string"),
            ("text", "string"),
            ("INTEGER", "integer"),
            ("BIGINT", "integer"),
            ("DECIMAL(18,2)", "number"),
            ("DECIMAL(18, 0)", "integer"),
            ("NUMBER(38,0)", "integer"),
            ("NUMBER(10)", "integer"),
            ("NUMBER", "number"),
            ("DOUBLE", "number"),
            ("BOOLEAN", "boolean"),
            ("DATE", "date"),
            ("TIMESTAMP", "timestamp"),
            ("TIMESTAMP WITH TIME ZONE", "timestamp"),
            ("TIMESTAMP_NTZ", "timestamp"),
            ("INTEGER[]", "array"),
            ("ARRAY<STRING>", "array"),
            ("STRUCT(a INTEGER)", "object"),
            ("TIME", "time"),
        ] {
            assert_eq!(logical_type(physical), Some(logical), "{physical}");
        }
        assert_eq!(logical_type("GEOMETRY"), None);
    }

    #[test]
    fn a_written_contract_reads_back_with_its_claims() {
        let contract = Contract {
            id: "urn:x".into(),
            version: "1.0.0".into(),
            name: "orders".into(),
            status: Some("draft".into()),
            schema: vec![Object {
                name: "orders".into(),
                properties: vec![Property {
                    name: "id".into(),
                    physical_type: Some("INTEGER".into()),
                    primary_key: Some(true),
                    primary_key_position: Some(1),
                    unique: Some(true),
                    quality: vec![Rule {
                        name: None,
                        description: None,
                        dimension: Some("uniqueness".into()),
                        severity: None,
                        kind: RuleKind::Library("duplicateValues".into()),
                        must_be: Some(0),
                        custom: vec![Custom::list("konstructEvidence", &["t".to_owned()])],
                    }],
                    ..Property::default()
                }],
                relationships: vec![Link {
                    from: vec!["orders.c".into()],
                    to: vec!["customers.id".into()],
                    custom: Vec::new(),
                }],
                ..Object::default()
            }],
            ..Contract::default()
        };
        let written = emit(&contract.yaml()).unwrap();
        assert!(crate::odcs::yaml::inspect::read(&written).is_ok());
        // The Python side parses the text; the same document, as JSON, is what the reader gets.
        let document = json!({
            "kind": "DataContract", "name": "orders",
            "schema": [{"name": "orders",
                "properties": [{"name": "id", "physicalType": "INTEGER", "primaryKey": true,
                    "unique": true, "quality": [{"metric": "duplicateValues", "mustBe": 0}]}],
                "relationships": [{"from": "orders.c", "to": "customers.id"}]}],
        });
        let mut problems = Vec::new();
        let read = Contract::read(&document, &mut problems).unwrap();
        assert!(problems.is_empty(), "{problems:?}");
        let property = &read.schema[0].properties[0];
        assert_eq!(property.primary_key, Some(true));
        assert_eq!(property.physical_type.as_deref(), Some("INTEGER"));
        assert!(
            matches!(&property.quality[0].kind, RuleKind::Library(m) if m == "duplicateValues")
        );
        assert_eq!(read.schema[0].relationships[0].to, ["customers.id"]);
    }

    #[test]
    fn property_level_relationships_get_their_implicit_from() {
        let document = json!({"kind": "DataContract", "schema": [{"name": "t",
            "properties": [{"name": "c", "relationships": [{"to": "u.id"}]}]}]});
        let mut problems = Vec::new();
        let read = Contract::read(&document, &mut problems).unwrap();
        assert!(problems.is_empty());
        assert_eq!(read.schema[0].properties[0].relationships[0].from, ["t.c"]);
    }

    #[test]
    fn wrong_shapes_are_recorded_not_dropped() {
        let document = json!({"kind": "DataContract", "schema": [
            "text",
            {"name": "t", "properties": ["x", {"name": "a", "primaryKey": "yes"}, {"physicalType": "INT"}],
             "relationships": [{"from": "t.a"}, {"from": ["t.a"], "to": ["u.a", "u.b"]}],
             "quality": ["rule"]},
            {"physicalName": "main.u"}]});
        let mut problems = Vec::new();
        let read = Contract::read(&document, &mut problems).unwrap();
        assert_eq!(read.schema.len(), 2);
        assert_eq!(problems.len(), 7, "{problems:?}");
    }

    #[test]
    fn something_that_is_not_a_contract_is_refused() {
        let mut problems = Vec::new();
        for document in [
            json!({"a": 1}),
            json!([1]),
            json!({"kind": "Other", "schema": []}),
            json!({"schema": []}),
        ] {
            let error = Contract::read(&document, &mut problems).unwrap_err();
            assert!(error.contains("not a data contract"), "{error}");
        }
    }
}
