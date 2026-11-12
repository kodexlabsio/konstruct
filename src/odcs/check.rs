//! The claims of a hand-written ODCS contract, tested against the graph's facts.
//!
//! Only claims the graph can answer are checked, and none runs SQL: a quality rule is text for
//! the tools that run it, so it is listed and not run. A finding is `contradicted` when the graph
//! holds the opposite (the column is missing, its type differs, `konstruct verify` found the key
//! repeated or nulls, the claim is in the wrong shape) and `unsupported` when nothing in the graph
//! says so (a key with no uniqueness test, a relationship nothing shows, a table that is not in
//! the project).
//!
//! A contract names things two ways: by `name`, and by `physicalName`, which wins where there is
//! one, and may be partly qualified (`main.customers`). A relationship's shorthand reference
//! (`order-items.order_id`) names an object and a property of the contract, and is resolved
//! through their physical names.

use std::collections::HashMap;

use serde_json::Value as Json;

use super::contract::{Contract, Link, Object, Property, RuleKind, logical_type};
use super::facts::{Column, Facts, Link as Fact, Lookup, Relation};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Finding {
    pub severity: &'static str,
    pub kind: &'static str,
    pub contract: String,
    pub place: String,
    pub message: String,
}

#[derive(Debug, Default)]
pub struct Report {
    pub contracts: usize,
    pub claims: usize,
    pub findings: Vec<Finding>,
    pub not_run: Vec<String>,
    pub notes: Vec<String>,
}

enum Resolved<'a> {
    Found(&'a Relation),
    /// The contract's name is more qualified than the graph's: the relation its tail names.
    Qualified(&'a Relation),
    Missing,
    Ambiguous,
}

/// One end of a relationship.
enum End<'a> {
    Column(&'a Relation, &'a Column),
    /// No such table, or none the contract can be tied to.
    NoTable,
    NoColumn(&'a Relation, String),
    /// A reference this check does not read.
    Skipped,
}

/// A contract's objects by name: the first of an exact name, else the first of a name in any case.
struct ObjectIndex {
    exact: HashMap<String, usize>,
    lower: HashMap<String, usize>,
}

impl ObjectIndex {
    fn new(contract: &Contract) -> Self {
        let mut index = Self {
            exact: HashMap::new(),
            lower: HashMap::new(),
        };
        for (i, object) in contract.schema.iter().enumerate() {
            index.exact.entry(object.name.clone()).or_insert(i);
            index.lower.entry(object.name.to_lowercase()).or_insert(i);
        }
        index
    }

    fn get(&self, name: &str) -> Option<usize> {
        self.exact
            .get(name)
            .or_else(|| self.lower.get(&name.to_lowercase()))
            .copied()
    }
}

struct Checker<'a> {
    facts: &'a Facts,
    /// The graph's relationships by their referring column (relation label, lower-case column).
    links: HashMap<(&'a str, String), Vec<&'a Fact>>,
    report: Report,
    contract: String,
}

impl<'a> Checker<'a> {
    fn new(facts: &'a Facts) -> Self {
        let mut links: HashMap<(&str, String), Vec<&Fact>> = HashMap::new();
        for link in &facts.links {
            links
                .entry((link.from.0.as_str(), link.from.1.to_lowercase()))
                .or_default()
                .push(link);
        }
        Self {
            facts,
            links,
            report: Report::default(),
            contract: String::new(),
        }
    }

    fn find(&mut self, severity: &'static str, kind: &'static str, place: &str, message: String) {
        self.report.findings.push(Finding {
            severity,
            kind,
            contract: self.contract.clone(),
            place: place.to_owned(),
            message,
        });
    }

    /// The relation a table is: its physical name where it has one, else its name (as a label, then
    /// as a physical name).
    fn table(&self, physical: Option<&str>, name: &str) -> Resolved<'a> {
        let facts = self.facts;
        let pick = |lookup: Lookup<'a>| match lookup {
            Lookup::One(relation) => Some(Resolved::Found(relation)),
            Lookup::Many => Some(Resolved::Ambiguous),
            Lookup::None => None,
        };
        let written = physical.unwrap_or(name);
        match physical {
            Some(physical) => pick(facts.by_physical(physical)),
            None => pick(facts.by_label(name)).or_else(|| pick(facts.by_physical(name))),
        }
        .or_else(|| match facts.by_tail(written) {
            Lookup::One(relation) => Some(Resolved::Qualified(relation)),
            Lookup::Many => Some(Resolved::Ambiguous),
            Lookup::None => None,
        })
        .unwrap_or(Resolved::Missing)
    }

    fn object(&mut self, object: &Object) {
        self.report.claims += 1;
        let relation = match self.table(object.physical_name.as_deref(), &object.name) {
            Resolved::Found(relation) => relation,
            Resolved::Qualified(relation) => {
                self.find(
                    "unsupported",
                    "table-qualifier",
                    &object.name,
                    format!(
                        "{} is more qualified than the graph's {}: nothing says the qualifier is right, so the graph's relation is checked in its place",
                        object.physical_name.as_deref().unwrap_or(&object.name),
                        relation.physical.as_deref().unwrap_or(&relation.label)
                    ),
                );
                relation
            }
            Resolved::Ambiguous => {
                self.find(
                    "unsupported",
                    "missing-table",
                    &object.name,
                    format!(
                        "{} matches more than one relation in the graph",
                        object.name
                    ),
                );
                return;
            }
            Resolved::Missing => {
                self.find(
                    "unsupported",
                    "missing-table",
                    &object.name,
                    format!(
                        "the graph has no relation {}",
                        object.physical_name.as_deref().unwrap_or(&object.name)
                    ),
                );
                return;
            }
        };
        let mut keys: Vec<(&Property, &'a Column)> = Vec::new();
        for property in &object.properties {
            if let Some(column) = self.property(relation, &object.name, property)
                && property.primary_key == Some(true)
            {
                keys.push((property, column));
            }
        }
        self.keys(relation, &object.name, &keys);
    }

    /// Checks one property against its column, and returns the column.
    fn property(
        &mut self,
        relation: &'a Relation,
        table: &str,
        property: &Property,
    ) -> Option<&'a Column> {
        let place = format!("{table}.{}", property.name);
        self.report.claims += 1;
        let wanted = property.physical_name.as_deref().unwrap_or(&property.name);
        let Some(column) = relation.column(wanted) else {
            self.find(
                "contradicted",
                "missing-column",
                &place,
                format!("{} has no column {wanted}", relation.label),
            );
            return None;
        };
        self.types(&place, property, column);
        let failed = column.check.as_ref().is_some_and(|c| c.state == "failed");
        let detail = column
            .check
            .as_ref()
            .map(|c| c.detail.as_str())
            .unwrap_or_default();
        if property.unique == Some(true) {
            self.report.claims += 1;
            if failed {
                self.find(
                    "contradicted",
                    "unique",
                    &place,
                    format!("konstruct verify found {place} is not unique: {detail}"),
                );
            } else if !column.unique {
                self.find(
                    "unsupported",
                    "unique",
                    &place,
                    format!("{place} is claimed to be unique, but no test, key or verify result says it is"),
                );
            }
        }
        if property.required == Some(true) {
            self.report.claims += 1;
            let confirmed = column
                .check
                .as_ref()
                .is_some_and(|c| c.state == "confirmed");
            // A primary key that is supported is never null.
            let by_key = property.primary_key == Some(true)
                && (column.unique || relation.in_primary_key(&column.name));
            if column.nulls {
                self.find(
                    "contradicted",
                    "required",
                    &place,
                    format!("konstruct verify found nulls in {place}: {detail}"),
                );
            } else if !(confirmed
                || by_key
                || !column.not_null_tests.is_empty()
                || relation.in_primary_key(&column.name))
            {
                self.find(
                    "unsupported",
                    "required",
                    &place,
                    format!("{place} is claimed never null, but no not_null test or verify result says so"),
                );
            }
        }
        if property.semantic_type.as_deref() == Some("measure") {
            self.report.claims += 1;
            let formula = column.formula.as_deref().or(column.expression.as_deref());
            match (column.aggregate, formula) {
                (Some(true), _) => {}
                (Some(false), formula) => self.find(
                    "contradicted",
                    "measure",
                    &place,
                    format!(
                        "{place} is claimed to be a measure, but its formula {} is not an aggregate",
                        formula.unwrap_or("here")
                    ),
                ),
                (None, _) => self.find(
                    "unsupported",
                    "measure",
                    &place,
                    format!("{place} is claimed to be a measure, but nothing computes it in the graph"),
                ),
            }
        }
        Some(column)
    }

    /// The columns a contract claims as its primary key, against the graph's.
    fn keys(&mut self, relation: &Relation, table: &str, keys: &[(&Property, &Column)]) {
        let place = |p: &Property| format!("{table}.{}", p.name);
        match keys {
            [] => {}
            [(property, column)] => {
                self.report.claims += 1;
                let failed = column.check.as_ref().is_some_and(|c| c.state == "failed");
                if failed {
                    let detail = column
                        .check
                        .as_ref()
                        .map(|c| c.detail.as_str())
                        .unwrap_or_default();
                    self.find(
                        "contradicted",
                        "key",
                        &place(property),
                        format!(
                            "konstruct verify found {} is not a key: {detail}",
                            place(property)
                        ),
                    );
                } else if !column.unique && !relation.in_primary_key(&column.name) {
                    self.find(
                        "unsupported",
                        "key",
                        &place(property),
                        format!(
                            "{} is claimed to be a primary key, but no test, key or verify result says it is unique",
                            place(property)
                        ),
                    );
                }
            }
            many => {
                self.report.claims += 1;
                let mut claimed: Vec<String> =
                    many.iter().map(|(_, c)| c.name.to_lowercase()).collect();
                let mut known: Vec<String> = relation
                    .primary_key
                    .iter()
                    .map(|k| k.to_lowercase())
                    .collect();
                claimed.sort();
                known.sort();
                if claimed == known {
                    return;
                }
                let names = many
                    .iter()
                    .map(|(p, _)| p.name.as_str())
                    .collect::<Vec<_>>()
                    .join(", ");
                if known.is_empty() {
                    self.find(
                        "unsupported",
                        "key",
                        table,
                        format!("({names}) is claimed to be the primary key, but the graph knows no primary key of {table}"),
                    );
                } else {
                    self.find(
                        "contradicted",
                        "key",
                        table,
                        format!(
                            "({names}) is claimed to be the primary key, but the graph's is ({})",
                            relation.primary_key.join(", ")
                        ),
                    );
                }
            }
        }
    }

    fn types(&mut self, place: &str, property: &Property, column: &Column) {
        let Some(actual) = column.data_type.as_deref() else {
            return;
        };
        if let Some(claimed) = &property.physical_type {
            self.report.claims += 1;
            if !self
                .facts
                .type_key(claimed)
                .same(&self.facts.type_key(actual))
            {
                self.find(
                    "contradicted",
                    "wrong-type",
                    place,
                    format!("claims type {claimed}, the graph has {actual}"),
                );
            }
        }
        if let (Some(claimed), Some(have)) = (&property.logical_type, logical_type(actual)) {
            self.report.claims += 1;
            if claimed != have && !(claimed == "number" && have == "integer") {
                self.find(
                    "contradicted",
                    "wrong-type",
                    place,
                    format!("claims logical type {claimed}, the graph has {actual} ({have})"),
                );
            }
        }
    }

    /// One end of a relationship, `table.column`, tied to the graph through the contract's own names.
    fn end(&mut self, contract: &Contract, index: &ObjectIndex, reference: &str) -> End<'a> {
        if reference.contains('/') {
            self.report.notes.push(format!(
                "{reference}: fully qualified references are not resolved"
            ));
            return End::Skipped;
        }
        let Some((table, column)) = reference.rsplit_once('.') else {
            return End::NoTable;
        };
        let own = index.get(table).map(|i| &contract.schema[i]);
        let (resolved, wanted) = match own {
            Some(object) => {
                let wanted = object
                    .properties
                    .iter()
                    .find(|p| p.name == column)
                    .and_then(|p| p.physical_name.as_deref())
                    .unwrap_or(column);
                (
                    self.table(object.physical_name.as_deref(), &object.name),
                    wanted,
                )
            }
            None => (self.table(None, table), column),
        };
        let (Resolved::Found(relation) | Resolved::Qualified(relation)) = resolved else {
            return End::NoTable;
        };
        match relation.column(wanted) {
            Some(column) => End::Column(relation, column),
            None => End::NoColumn(relation, wanted.to_owned()),
        }
    }

    fn link(&mut self, contract: &Contract, index: &ObjectIndex, link: &Link) {
        for (from, to) in link.from.iter().zip(&link.to) {
            self.report.claims += 1;
            let place = format!("{from} -> {to}");
            let (source, target) = (
                self.end(contract, index, from),
                self.end(contract, index, to),
            );
            let (End::Column(source, source_column), End::Column(target, target_column)) =
                (&source, &target)
            else {
                for end in [&source, &target] {
                    match end {
                        End::NoColumn(relation, column) => {
                            self.find(
                                "contradicted",
                                "relationship",
                                &place,
                                format!("{} has no column {column}", relation.label),
                            );
                        }
                        End::NoTable => self.find(
                            "unsupported",
                            "relationship",
                            &place,
                            "a table of the relationship is not in the graph".into(),
                        ),
                        _ => {}
                    }
                }
                continue;
            };
            let found = self
                .links
                .get(&(source.label.as_str(), source_column.name.to_lowercase()))
                .map_or(&[][..], Vec::as_slice);
            let same = |l: &Fact| {
                l.to.0 == target.label && l.to.1.eq_ignore_ascii_case(&target_column.name)
            };
            if let Some(held) = found.iter().find(|l| same(l)) {
                if held.check.as_deref() == Some("failed") {
                    let said = held
                        .verify
                        .last()
                        .map_or("konstruct verify: the check failed", String::as_str);
                    self.find("contradicted", "relationship", &place, said.to_owned());
                }
                continue;
            }
            // Only evidence contradicts a relationship: verify found the target's values repeated.
            if let Some(refuted) = target_column.check.as_ref().filter(|c| c.state == "failed") {
                self.find(
                    "contradicted",
                    "relationship",
                    &place,
                    format!(
                        "konstruct verify found {}.{} is not unique: {}, so nothing can refer to it as a key",
                        target.label, target_column.name, refuted.detail
                    ),
                );
                continue;
            }
            match found.first() {
                // The graph has the column referring elsewhere: a second valid target is possible,
                // and a target not known to be unique is not shown to be wrong.
                Some(other) if !target_column.unique => self.find(
                    "unsupported",
                    "relationship",
                    &place,
                    format!(
                        "the graph has {}.{} referring to {}.{}, and {}.{} is not known to be unique",
                        source.label, source_column.name, other.to.0, other.to.1, target.label, target_column.name
                    ),
                ),
                Some(other) => self.find(
                    "unsupported",
                    "relationship",
                    &place,
                    format!(
                        "the graph has {}.{} referring to {}.{}; nothing shows it also refers to {}.{}",
                        source.label, source_column.name, other.to.0, other.to.1, target.label, target_column.name
                    ),
                ),
                None => self.find(
                    "unsupported",
                    "relationship",
                    &place,
                    format!(
                        "nothing in the graph shows {}.{} referring to {}.{}",
                        source.label, source_column.name, target.label, target_column.name
                    ),
                ),
            }
        }
    }

    /// The quality rules of a contract are listed, never run.
    fn rules(&mut self, at: &str, rules: &[super::contract::Rule]) {
        for rule in rules {
            match &rule.kind {
                RuleKind::Sql(_) => self.report.not_run.push(format!(
                    "{at}: sql quality rule {}",
                    rule.name.as_deref().unwrap_or("(unnamed)")
                )),
                RuleKind::Library(metric) => self
                    .report
                    .not_run
                    .push(format!("{at}: quality rule {metric}")),
                RuleKind::Other(_) => {}
            }
        }
    }
}

/// Checks every contract in `documents`, the parsed YAML documents of a file.
pub fn check(facts: &Facts, documents: &[Json]) -> Result<Report, String> {
    let mut checker = Checker::new(facts);
    checker.report.notes.extend(facts.skipped.iter().cloned());
    let mut read = 0;
    for document in documents.iter().filter(|d| !d.is_null()) {
        let mut problems = Vec::new();
        let contract = Contract::read(document, &mut problems)?;
        read += 1;
        checker.contract = if contract.name.is_empty() {
            format!("contract {read}")
        } else {
            contract.name.clone()
        };
        for (place, message) in problems {
            checker.find("contradicted", "malformed", &place, message);
        }
        let index = ObjectIndex::new(&contract);
        for object in &contract.schema {
            checker.object(object);
            checker.rules(&object.name, &object.quality);
            for link in &object.relationships {
                checker.link(&contract, &index, link);
            }
            for property in &object.properties {
                checker.rules(
                    &format!("{}.{}", object.name, property.name),
                    &property.quality,
                );
                for link in &property.relationships {
                    checker.link(&contract, &index, link);
                }
            }
        }
    }
    if read == 0 {
        return Err("not a data contract: the file is empty".into());
    }
    checker.report.contracts = read;
    Ok(checker.report)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    const FACTS: &str = r#"{"relations": [
        {"label": "customers", "physical": "db.main.customers", "kind": "model", "columns": [
            {"name": "id", "type": "INTEGER", "unique": true},
            {"name": "name", "type": "VARCHAR"}]},
        {"label": "orders", "physical": "db.main.orders", "kind": "model", "primary_key": ["id", "customer_id"],
         "columns": [
            {"name": "id", "type": "INTEGER", "unique": true},
            {"name": "customer_id", "type": "INTEGER"},
            {"name": "amount", "type": "DECIMAL(18,2)", "formula": "SUM(x.amount)", "aggregate": true},
            {"name": "raw", "type": "DECIMAL(18,2)", "expression": "x.amount", "aggregate": false}]}],
        "relationships": [{"from": ["orders", "customer_id"], "to": ["customers", "id"]}],
        "types": {"INTEGER": ["INT", null], "integer": ["INT", null], "BIGINT": ["BIGINT", null],
                  "VARCHAR": ["VARCHAR", null], "DECIMAL(18,2)": ["DECIMAL", "18,2"],
                  "NUMBER(38,0)": ["DECIMAL", "38,0"]}}"#;

    fn run(contract: Json) -> Report {
        check(&Facts::parse(FACTS).unwrap(), &[contract]).unwrap()
    }

    fn kinds(report: &Report) -> Vec<(&str, &str)> {
        report
            .findings
            .iter()
            .map(|f| (f.kind, f.place.as_str()))
            .collect()
    }

    fn contract(schema: Json) -> Json {
        json!({"kind": "DataContract", "schema": schema})
    }

    #[test]
    fn a_true_claim_has_no_finding() {
        let report = run(contract(json!([{"name": "customers", "properties": [
            {"name": "id", "primaryKey": true, "physicalType": "integer"}]}])));
        assert!(report.findings.is_empty(), "{:?}", report.findings);
        assert!(report.claims >= 4);
    }

    #[test]
    fn contradicted_and_unsupported_claims_are_told_apart() {
        let report = run(contract(json!([{"name": "customers", "properties": [
            {"name": "name", "unique": true, "physicalType": "INTEGER"}, {"name": "nope"}]}])));
        assert_eq!(
            kinds(&report),
            [
                ("wrong-type", "customers.name"),
                ("unique", "customers.name"),
                ("missing-column", "customers.nope")
            ]
        );
        assert_eq!(report.findings[0].severity, "contradicted");
        assert_eq!(report.findings[1].severity, "unsupported");
    }

    #[test]
    fn widths_and_scale_zero_decimals() {
        let wide = run(contract(json!([{"name": "customers", "properties": [
            {"name": "id", "physicalType": "BIGINT"}]}])));
        assert_eq!(kinds(&wide), [("wrong-type", "customers.id")]);
        let number = run(contract(json!([{"name": "orders", "properties": [
            {"name": "id", "logicalType": "number"}, {"name": "amount", "logicalType": "integer"}]}])));
        assert_eq!(kinds(&number), [("wrong-type", "orders.amount")]);
    }

    #[test]
    fn a_measure_must_be_an_aggregate() {
        let report = run(contract(json!([{"name": "orders", "properties": [
            {"name": "amount", "semanticType": "measure"}, {"name": "raw", "semanticType": "measure"},
            {"name": "id", "semanticType": "measure"}]}])));
        assert_eq!(
            kinds(&report),
            [("measure", "orders.raw"), ("measure", "orders.id")]
        );
        assert_eq!(report.findings[0].severity, "contradicted");
        assert_eq!(report.findings[1].severity, "unsupported");
    }

    #[test]
    fn a_composite_key_is_compared_as_a_set() {
        let key = |names: &[&str]| {
            let properties: Vec<Json> = names
                .iter()
                .map(|n| json!({"name": n, "primaryKey": true}))
                .collect();
            run(contract(
                json!([{"name": "orders", "properties": properties}]),
            ))
        };
        assert!(key(&["customer_id", "id"]).findings.is_empty());
        let wrong = key(&["id", "amount"]);
        assert_eq!(
            (wrong.findings[0].kind, wrong.findings[0].severity),
            ("key", "contradicted")
        );
        // One column of a composite key is not a key on its own, though the graph says it is unique.
        assert!(key(&["id"]).findings.is_empty());
    }

    #[test]
    fn physical_names_win_and_suffixes_resolve() {
        let report = run(contract(
            json!([{"name": "orders", "physicalName": "main.customers", "properties": [
            {"name": "ident", "physicalName": "id"}, {"name": "customer_id"}]}]),
        ));
        assert_eq!(kinds(&report), [("missing-column", "orders.customer_id")]);
        let missing = run(contract(
            json!([{"name": "customers", "physicalName": "other.customers"}]),
        ));
        assert_eq!(kinds(&missing), [("missing-table", "customers")]);
    }

    #[test]
    fn relationships_are_supported_contradicted_or_unsupported() {
        let text = |to: &str| {
            contract(
                json!([{"name": "orders", "relationships": [{"from": "orders.customer_id", "to": to}]},
                            {"name": "customers"}]),
            )
        };
        assert!(run(text("customers.id")).findings.is_empty());
        // A target not known to be unique is not shown wrong: only evidence contradicts.
        let other = run(text("customers.name"));
        assert_eq!(other.findings[0].severity, "unsupported");
        let none = run(contract(json!([{"name": "orders", "relationships": [
            {"from": "orders.id", "to": "customers.id"}]}])));
        assert_eq!(none.findings[0].severity, "unsupported");
        let gone = run(text("ghosts.id"));
        assert_eq!(gone.findings[0].severity, "unsupported");
    }

    #[test]
    fn a_contract_of_many_objects_is_checked_in_linear_time() {
        // Objects, each with a relationship to the next: a search of the objects for each end of
        // each relationship grows with the square of their number. Ten times the objects must
        // take well under a hundred times as long. The ratio, not a wall-clock bound, holds on a
        // slow or emulated machine.
        fn time(count: usize) -> std::time::Duration {
            let schema: Vec<Json> = (0..count)
                .map(|i| {
                    json!({"name": format!("t{i}"), "relationships": [
                        {"from": format!("t{i}.a"), "to": format!("t{}.a", (i + 1) % count)}]})
                })
                .collect();
            let document = contract(Json::Array(schema));
            (0..3)
                .map(|_| {
                    let started = std::time::Instant::now();
                    run(document.clone());
                    started.elapsed()
                })
                .min()
                .unwrap_or_default()
        }
        let small = time(2_000);
        let large = time(20_000);
        assert!(
            large < small * 40,
            "{small:?} for 2,000 objects, {large:?} for 20,000"
        );
    }

    #[test]
    fn rules_are_listed_and_never_findings() {
        let report = run(contract(json!([{"name": "orders",
            "quality": [{"type": "sql", "name": "q", "query": "SELECT 1", "mustBe": 0}, {"metric": "rowCount"}],
            "properties": [{"name": "id", "quality": [{"metric": "nullValues"}]}]}])));
        assert!(report.findings.is_empty());
        assert_eq!(
            report.not_run,
            [
                "orders: sql quality rule q",
                "orders: quality rule rowCount",
                "orders.id: quality rule nullValues"
            ]
        );
    }

    #[test]
    fn malformed_claims_are_findings_and_a_non_contract_is_an_error() {
        let report = run(contract(json!([{"name": "orders", "properties": [
            {"name": "id", "primaryKey": "yes"}, "x"]}, "y"])));
        assert_eq!(
            report
                .findings
                .iter()
                .filter(|f| f.kind == "malformed")
                .count(),
            3
        );
        let facts = Facts::parse(FACTS).unwrap();
        assert!(check(&facts, &[json!({"a": 1})]).is_err());
        assert!(check(&facts, &[]).is_err());
        let two = check(&facts, &[contract(json!([])), contract(json!([]))]).unwrap();
        assert_eq!(two.contracts, 2);
    }
}
