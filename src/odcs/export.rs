//! The ontology's facts as ODCS 3.2 contracts, one per relation that holds an entity or a measure.
//!
//! A fact is written in the field of the contract that says it. It is written as a *verified
//! statement* only when Konstruct holds evidence for it, and the statement names that evidence:
//!
//! - a key is unique, or a column never null: a dbt test says so (grade `declared`), or
//!   `konstruct verify` found it in the data (grade `verified`);
//! - a relationship holds: a dbt `relationships` test (`declared`) or a `konstruct verify` check
//!   of the data (`verified`);
//! - a measure's formula, composed from the SQL down to source: only when `konstruct verify`
//!   confirmed every lineage edge on the way (`verified`).
//!
//! Anything weaker, such as a relationship read from a join or an entity named by a column's name,
//! is written with its grade in `customProperties` and never as a statement. A key or uniqueness
//! that `konstruct verify` found false is not written, and the file says so.

use std::collections::{HashMap, HashSet};

use super::contract::{
    Contract, Custom, Link, Object, Property, Rule, RuleKind, Statement, logical_type, referable,
};
use super::facts::{Column, Entity, Facts, Link as Fact, Measure, MeasureColumn, Relation};
use super::yaml::{comment, emit};

const GRADE: &str = "konstructGrade";
const EVIDENCE: &str = "konstructEvidence";

fn statement(
    seen: &mut HashSet<String>,
    kind: &str,
    label: &str,
    question: String,
    answer: String,
    grade: &str,
    evidence: &[String],
) -> Statement {
    let base = format!("{kind}_{label}");
    let mut id = base.clone();
    let mut n = 1;
    while !seen.insert(super::contract::stable_id(&id)) {
        n += 1;
        id = format!("{base}_{n}");
    }
    Statement {
        id,
        question,
        answer,
        custom: vec![Custom::text(GRADE, grade), Custom::list(EVIDENCE, evidence)],
    }
}

fn verify_line(detail: &str) -> String {
    format!("konstruct verify: {detail}")
}

fn rule(metric: &str, dimension: &str, test: &str) -> Rule {
    Rule {
        name: None,
        description: Some(format!("dbt test {test}")),
        dimension: Some(dimension.to_owned()),
        severity: None,
        kind: RuleKind::Library(metric.to_owned()),
        must_be: Some(0),
        custom: vec![Custom::list(EVIDENCE, &[test.to_owned()])],
    }
}

fn source_tables(sources: &[String]) -> Vec<String> {
    let mut tables: Vec<String> = Vec::new();
    for source in sources {
        let table = source.rsplit_once('.').map_or(source.as_str(), |(t, _)| t);
        if !tables.iter().any(|t| t == table) {
            tables.push(table.to_owned());
        }
    }
    tables
}

/// The measure columns, the entities and the relationships each relation has, found once.
struct Index<'a> {
    measures: HashMap<&'a str, Vec<(&'a Measure, &'a MeasureColumn)>>,
    entities: HashMap<&'a str, Vec<&'a Entity>>,
    links: HashMap<&'a str, Vec<&'a Fact>>,
}

impl<'a> Index<'a> {
    fn new(facts: &'a Facts) -> Self {
        let mut index = Self {
            measures: HashMap::new(),
            entities: HashMap::new(),
            links: HashMap::new(),
        };
        for measure in &facts.measures {
            for held in &measure.columns {
                index
                    .measures
                    .entry(&held.relation)
                    .or_default()
                    .push((measure, held));
            }
        }
        for entity in &facts.entities {
            for relation in &entity.relations {
                index.entities.entry(relation).or_default().push(entity);
            }
        }
        for link in &facts.links {
            index.links.entry(&link.from.0).or_default().push(link);
        }
        index
    }
}

struct Writer<'a> {
    facts: &'a Facts,
    index: &'a Index<'a>,
    omitted: Vec<String>,
    seen: HashSet<String>,
}

impl Writer<'_> {
    fn property(&mut self, relation: &Relation, column: &Column) -> (Property, Vec<Statement>) {
        let label = &relation.label;
        let place = format!("{label}.{}", column.name);
        let mut property = Property {
            name: column.name.clone(),
            physical_type: column.data_type.clone(),
            logical_type: column
                .data_type
                .as_deref()
                .and_then(logical_type)
                .map(str::to_owned),
            description: column.description.clone(),
            ..Property::default()
        };
        let mut statements = Vec::new();
        let failed = column.check.as_ref().is_some_and(|c| c.state == "failed");
        if failed {
            let detail = column.check.as_ref().map_or("", |c| c.detail.as_str());
            self.omitted.push(format!(
                "{place}: konstruct verify found it not unique or null ({detail}), so no key or uniqueness is written"
            ));
        } else if column.unique {
            property.unique = Some(true);
            let question = format!("Is {place} unique?");
            if let Some(check) = column.check.as_ref().filter(|c| c.state == "confirmed") {
                let line = verify_line(&check.detail);
                statements.push(statement(
                    &mut self.seen,
                    "unique",
                    &place,
                    question,
                    format!("Yes. {line}"),
                    "verified",
                    &[line],
                ));
            } else if !column.unique_tests.is_empty() {
                let answer = format!(
                    "Yes. The dbt test {} requires it.",
                    column.unique_tests.join(", ")
                );
                statements.push(statement(
                    &mut self.seen,
                    "unique",
                    &place,
                    question,
                    answer,
                    "declared",
                    &column.unique_tests,
                ));
            }
        }
        for test in &column.unique_tests {
            property
                .quality
                .push(rule("duplicateValues", "uniqueness", test));
        }
        if column.nulls {
            self.omitted.push(format!(
                "{place}: konstruct verify found nulls, so it is not written as never null"
            ));
        } else {
            // A declared primary key is never null; a test says so too.
            if !column.not_null_tests.is_empty() || relation.in_primary_key(&column.name) {
                property.required = Some(true);
            }
            if !column.not_null_tests.is_empty() {
                statements.push(statement(
                    &mut self.seen,
                    "not_null",
                    &place,
                    format!("Is {place} ever null?"),
                    format!(
                        "No. The dbt test {} requires it.",
                        column.not_null_tests.join(", ")
                    ),
                    "declared",
                    &column.not_null_tests,
                ));
            }
        }
        for test in &column.not_null_tests {
            property
                .quality
                .push(rule("nullValues", "completeness", test));
        }
        for &(measure, held) in self
            .index
            .measures
            .get(label.as_str())
            .map_or(&[][..], Vec::as_slice)
        {
            if held.column != column.name {
                continue;
            }
            property.synonyms.push(measure.name.clone());
            property
                .custom
                .push(Custom::text("konstructMeasure", &measure.name));
            property
                .custom
                .push(Custom::text(GRADE, &measure.confidence));
            let Some(formula) = held.formula.as_ref().filter(|_| held.complete) else {
                continue;
            };
            // A column that only copies another is not an aggregate, and `contract check` would
            // refuse it as a measure, so it stays a column.
            if held.aggregate == Some(true) {
                property.semantic_type = Some("measure".into());
            } else {
                self.omitted.push(format!(
                    "{place}: the measure {} is not an aggregate here, so it is written as a column",
                    measure.name
                ));
            }
            property.transform_logic = Some(formula.clone());
            property.transform_sources = source_tables(&held.sources);
            if held.edges > 0 && held.verified == held.edges {
                let line = verify_line(&format!(
                    "{} of {} lineage edges confirmed",
                    held.verified, held.edges
                ));
                statements.push(statement(
                    &mut self.seen,
                    "formula",
                    &place,
                    format!("How is {} computed in {label}?", measure.name),
                    format!(
                        "Its lineage down to {} was confirmed ({line}); its formula is {formula}.",
                        held.sources.join(", ")
                    ),
                    "verified",
                    &[line],
                ));
            }
        }
        (property, statements)
    }

    fn link(
        &mut self,
        fact: &Fact,
        statements: &mut Vec<Statement>,
        quality: &mut Vec<Rule>,
    ) -> Option<Link> {
        let from = format!("{}.{}", fact.from.0, fact.from.1);
        let to = format!("{}.{}", fact.to.0, fact.to.1);
        let names = [&fact.from.0, &fact.from.1, &fact.to.0, &fact.to.1];
        if !names.iter().all(|n| referable(n)) {
            self.omitted.push(format!(
                "{from} -> {to}: a name is not valid in an ODCS reference"
            ));
            return None;
        }
        let mut custom = vec![Custom::text(GRADE, &fact.confidence)];
        // Verify confirms many-to-one with the link, and one-to-one only by its own check of the
        // referring column.
        let one_to_one = fact.cardinality.as_deref() == Some("one-to-one");
        if let Some(cardinality) = &fact.cardinality {
            custom.push(Custom::text("konstructCardinality", cardinality));
            if one_to_one {
                let grade = if fact.one_to_one_confirmed {
                    "verified"
                } else {
                    "declared"
                };
                custom.push(Custom::text("konstructCardinalityGrade", grade));
            }
        }
        let question = format!("Does {from} refer to {to}?");
        let shape = |confirmed: bool| {
            fact.cardinality
                .as_deref()
                .filter(|_| !one_to_one || confirmed)
                .map(|c| format!(" ({c})"))
                .unwrap_or_default()
        };
        let lines: Vec<String> = fact.verify.clone();
        match fact.check.as_deref() {
            Some("confirmed") => {
                custom.push(Custom::text("konstructCheck", "confirmed"));
                let said = lines
                    .last()
                    .cloned()
                    .unwrap_or_else(|| verify_line("every value found"));
                let evidence = if lines.is_empty() {
                    vec![said.clone()]
                } else {
                    lines
                };
                statements.push(statement(
                    &mut self.seen,
                    "link",
                    &format!("{from}_{to}"),
                    question,
                    format!("Yes{}. {said}", shape(fact.one_to_one_confirmed)),
                    "verified",
                    &evidence,
                ));
            }
            Some(_) => {
                custom.push(Custom::text("konstructCheck", "failed"));
                let said = lines
                    .last()
                    .cloned()
                    .unwrap_or_else(|| verify_line("the check failed"));
                self.omitted.push(format!(
                    "{from} -> {to}: {said}, so no statement is written"
                ));
            }
            None if !fact.tests.is_empty() => {
                let answer = format!(
                    "Yes{}. The dbt relationships test {} requires every value to be found.",
                    shape(true),
                    fact.tests.join(", ")
                );
                statements.push(statement(
                    &mut self.seen,
                    "link",
                    &format!("{from}_{to}"),
                    question,
                    answer,
                    "declared",
                    &fact.tests,
                ));
            }
            None => {}
        }
        if !fact.tests.is_empty() {
            let physical = |label: &str| {
                self.facts
                    .relation(label)
                    .and_then(|r| r.physical.clone())
                    .unwrap_or_else(|| label.to_owned())
            };
            let (src, tgt) = (physical(&fact.from.0), physical(&fact.to.0));
            let (c, tc) = (&fact.from.1, &fact.to.1);
            quality.push(Rule {
                name: Some(format!("{}_{c}_in_{}_{tc}", fact.from.0, fact.to.0)),
                description: Some(format!("dbt relationships test {}", fact.tests.join(", "))),
                dimension: Some("consistency".into()),
                severity: None,
                kind: RuleKind::Sql(format!(
                    "SELECT COUNT(*) FROM {src} AS s LEFT JOIN {tgt} AS t ON s.{c} = t.{tc} \
                     WHERE s.{c} IS NOT NULL AND t.{tc} IS NULL"
                )),
                must_be: Some(0),
                custom: vec![Custom::list(EVIDENCE, &fact.tests)],
            });
        }
        Some(Link {
            from: vec![from],
            to: vec![to],
            custom,
        })
    }

    /// One primary key: the key the relation declares, else the unique key of the entity the
    /// relation is named for, else of the first entity. Every other unique column stays `unique`.
    fn primary_key(&self, relation: &Relation, entities: &[&Entity], object: &mut Object) {
        let usable = |name: &str| {
            relation
                .column(name)
                .is_some_and(|c| !c.check.as_ref().is_some_and(|k| k.state == "failed"))
        };
        let named_for = |entity: &Entity| {
            let tidy = |text: &str| {
                let kept: String = text
                    .chars()
                    .filter(|c| c.is_alphanumeric())
                    .collect::<String>()
                    .to_lowercase();
                kept.strip_suffix('s').unwrap_or(&kept).to_owned()
            };
            let (label, name) = (tidy(&relation.label), tidy(&entity.name));
            !name.is_empty() && label.ends_with(&name)
        };
        let mut ordered: Vec<&&Entity> = entities.iter().collect();
        ordered.sort_by_key(|e| !named_for(e));
        let names: Vec<String> =
            if !relation.primary_key.is_empty() && relation.primary_key.iter().all(|k| usable(k)) {
                relation.primary_key.clone()
            } else {
                ordered
                    .iter()
                    .find_map(|e| {
                        e.unique_keys.iter().find(|(r, c)| {
                            *r == relation.label
                                && relation.column(c).is_some_and(|c| c.unique)
                                && usable(c)
                        })
                    })
                    .map(|(_, c)| vec![c.clone()])
                    .unwrap_or_default()
            };
        for (position, name) in names.iter().enumerate() {
            if let Some(property) = object
                .properties
                .iter_mut()
                .find(|p| p.name.eq_ignore_ascii_case(name))
            {
                property.primary_key = Some(true);
                property.primary_key_position = Some(position as i64 + 1);
                property.unique = if names.len() == 1 {
                    Some(true)
                } else {
                    property.unique
                };
            }
        }
    }

    fn contract(&mut self, relation: &Relation) -> Contract {
        let label = &relation.label;
        let entities: Vec<&Entity> = self
            .index
            .entities
            .get(label.as_str())
            .cloned()
            .unwrap_or_default();
        let mut object = Object {
            name: label.clone(),
            physical_name: relation.physical.clone(),
            physical_type: relation
                .materialized
                .as_deref()
                .filter(|m| matches!(*m, "table" | "view"))
                .map(str::to_owned),
            description: relation.description.clone(),
            ..Object::default()
        };
        if let Some(entity) = entities.first() {
            object.business_name = entity.term.clone();
            for name in std::iter::once(&entity.name).chain(entity.term.iter()) {
                if !name.eq_ignore_ascii_case(label)
                    && !object.synonyms.iter().any(|s| s.eq_ignore_ascii_case(name))
                {
                    object.synonyms.push(name.clone());
                }
            }
            object
                .custom
                .push(Custom::text("konstructEntity", &entity.name));
            object.custom.push(Custom::text(GRADE, &entity.confidence));
        }
        let mut statements = Vec::new();
        for column in &relation.columns {
            let (property, found) = self.property(relation, column);
            object.properties.push(property);
            statements.extend(found);
        }
        self.primary_key(relation, &entities, &mut object);
        let mut quality = Vec::new();
        for &fact in self
            .index
            .links
            .get(label.as_str())
            .map_or(&[][..], Vec::as_slice)
        {
            if let Some(link) = self.link(fact, &mut statements, &mut quality) {
                object.relationships.push(link);
            }
        }
        object.quality = quality;
        object.statements = statements;
        let project = self.facts.project.as_deref().unwrap_or("project");
        Contract {
            id: format!("urn:konstruct:{project}:{label}"),
            version: "1.0.0".into(),
            name: label.clone(),
            status: Some("draft".into()),
            purpose: None,
            instructions: Some(
                "Derived by Konstruct from the data stack. Each verified statement names the dbt test or \
                 konstruct verify result behind it and carries its grade; a fact graded inferred is never \
                 among them."
                    .into(),
            ),
            schema: vec![object],
        }
    }
}

/// The relations that get a contract: those holding an entity or a measure, built here and not
/// raw sources or seeds.
fn selected<'a>(facts: &'a Facts, index: &Index<'_>) -> Vec<&'a Relation> {
    facts
        .relations
        .iter()
        .filter(|r| !matches!(r.kind.as_str(), "source" | "seed"))
        .filter(|r| {
            index.entities.contains_key(r.label.as_str())
                || index.measures.contains_key(r.label.as_str())
        })
        .collect()
}

/// Every contract, as a stream of YAML documents.
pub fn export(facts: &Facts) -> Result<String, String> {
    let index = Index::new(facts);
    let relations = selected(facts, &index);
    if relations.is_empty() {
        return Err("nothing to export: the ontology has no entity or measure in a model".into());
    }
    let mut out = String::new();
    for item in &facts.skipped {
        out.push_str(&format!("# Left out, {}\n", comment(item)));
    }
    for relation in relations {
        let mut writer = Writer {
            facts,
            index: &index,
            omitted: Vec::new(),
            seen: HashSet::new(),
        };
        let contract = writer.contract(relation);
        let document = emit(&contract.yaml())?;
        let body = document.strip_prefix("---\n").unwrap_or(&document);
        out.push_str("---\n");
        out.push_str(&format!(
            "# ODCS v3.2.0 data contract for {}, derived by Konstruct from the data stack.\n",
            comment(&relation.label)
        ));
        out.push_str("# A contract is not enforced by Konstruct: the quality rules are text for the tools that run them.\n");
        out.push_str("# Not written: servers, team, roles, SLAs, support and pricing, which the data stack does not hold.\n");
        for item in &writer.omitted {
            out.push_str(&format!("# Left out, {}\n", comment(item)));
        }
        out.push_str(body);
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::odcs::yaml::inspect::{View, read};

    fn member<'d, 'a>(node: View<'d, 'a>, key: &str) -> Option<View<'d, 'a>> {
        node.member(key)
    }

    fn member_text(node: View<'_, '_>, key: &str) -> Option<String> {
        node.member(key).and_then(|v| v.as_str().map(str::to_owned))
    }

    fn items<'d, 'a>(node: View<'d, 'a>, key: &str) -> Vec<View<'d, 'a>> {
        node.member(key).map_or_else(Vec::new, |v| v.items())
    }

    const FACTS: &str = r#"{"project": "shop",
        "relations": [
          {"label": "customers", "physical": "db.main.customers", "kind": "model", "columns": [
            {"name": "id", "type": "INTEGER", "unique": true, "unique_tests": ["test.u"],
             "not_null_tests": ["test.n"]},
            {"name": "name", "type": "VARCHAR"}]},
          {"label": "orders", "physical": "db.main.orders", "kind": "model", "columns": [
            {"name": "id", "type": "INTEGER", "unique": true,
             "check": {"state": "failed", "detail": "2 duplicates"}},
            {"name": "customer_id", "type": "INTEGER"},
            {"name": "total", "type": "DOUBLE", "formula": "SUM(raw.amount)"},
            {"name": "copy", "type": "DOUBLE", "formula": "raw.amount"}]},
          {"label": "raw", "physical": "db.main.raw", "kind": "source", "columns": [{"name": "amount"}]}],
        "entities": [{"name": "customer", "confidence": "declared", "relations": ["customers"],
                      "unique_keys": [["customers", "id"]]},
                     {"name": "order", "confidence": "inferred", "relations": ["orders"]}],
        "measures": [{"name": "Revenue", "confidence": "verified", "columns": [
            {"relation": "orders", "column": "total", "formula": "SUM(raw.amount)",
             "sources": ["raw.amount"], "complete": true, "aggregate": true, "verified": 2, "edges": 2},
            {"relation": "orders", "column": "copy", "formula": "raw.amount",
             "sources": ["raw.amount"], "complete": true, "aggregate": false, "verified": 1, "edges": 1}]}],
        "relationships": [
          {"from": ["orders", "customer_id"], "to": ["customers", "id"], "confidence": "inferred"},
          {"from": ["orders", "bad col"], "to": ["customers", "id"], "confidence": "inferred"}]}"#;

    fn written() -> String {
        export(&Facts::parse(FACTS).unwrap()).unwrap()
    }

    #[test]
    fn a_contract_for_each_model_with_an_entity_or_measure_and_none_for_a_source() {
        let text = written();
        let docs = read(&text).unwrap();
        let names: Vec<_> = docs
            .iter()
            .map(|d| member_text(View::root(d), "name").unwrap())
            .collect();
        assert_eq!(names, ["customers", "orders"]);
    }

    #[test]
    fn statements_come_only_from_tests_and_verify_results() {
        let text = written();
        let docs = read(&text).unwrap();
        let customers = items(View::root(&docs[0]), "schema")[0];
        let context = member(customers, "context").unwrap();
        let questions: Vec<_> = items(context, "verifiedStatements")
            .into_iter()
            .map(|s| member_text(s, "question").unwrap())
            .collect();
        assert_eq!(
            questions,
            ["Is customers.id unique?", "Is customers.id ever null?"]
        );
        // The orders contract has a measure verify confirmed, and nothing else to say.
        let orders = items(View::root(&docs[1]), "schema")[0];
        let statements = items(member(orders, "context").unwrap(), "verifiedStatements");
        assert_eq!(statements.len(), 2);
        assert!(
            member_text(statements[0], "question")
                .unwrap()
                .starts_with("How is Revenue")
        );
    }

    #[test]
    fn a_key_verify_found_repeated_is_not_written_and_the_file_says_so() {
        let text = written();
        let docs = read(&text).unwrap();
        let orders = items(View::root(&docs[1]), "schema")[0];
        let id = items(orders, "properties")[0];
        assert!(member(id, "unique").is_none() && member(id, "primaryKey").is_none());
        assert!(text.contains(
            "# Left out, orders.id: konstruct verify found it not unique or null (2 duplicates)"
        ));
    }

    #[test]
    fn an_aggregate_is_a_measure_and_a_copy_stays_a_column() {
        let text = written();
        let docs = read(&text).unwrap();
        let orders = items(View::root(&docs[1]), "schema")[0];
        let properties = items(orders, "properties");
        assert_eq!(
            member_text(properties[2], "semanticType").as_deref(),
            Some("measure")
        );
        assert!(member(properties[3], "semanticType").is_none());
        assert!(text.contains("orders.copy: the measure Revenue is not an aggregate here"));
    }

    #[test]
    fn an_inferred_relationship_has_its_grade_and_no_statement_and_a_bad_name_is_left_out() {
        let text = written();
        let docs = read(&text).unwrap();
        let orders = items(View::root(&docs[1]), "schema")[0];
        let links = items(orders, "relationships");
        assert_eq!(links.len(), 1);
        assert_eq!(
            member_text(items(links[0], "customProperties")[0], "value").as_deref(),
            Some("inferred")
        );
        assert!(!text.contains("Does orders.customer_id refer"));
        assert!(
            text.contains(
                "orders.bad col -> customers.id: a name is not valid in an ODCS reference"
            )
        );
    }

    #[test]
    fn nothing_to_export_is_an_error() {
        let facts = Facts::parse(r#"{"relations": [{"label": "t", "kind": "model"}]}"#).unwrap();
        assert!(export(&facts).unwrap_err().contains("nothing to export"));
    }

    #[test]
    fn hostile_text_in_a_name_or_description_reads_back_as_itself() {
        let nasty = "x\u{85}status: active\r\n\u{2028}1_000 \u{7}";
        let facts = serde_json::json!({
            "project": nasty,
            "relations": [{"label": "t", "physical": nasty, "kind": "model", "description": nasty,
                "columns": [{"name": "c", "description": nasty}]}],
            "entities": [{"name": "t", "relations": ["t"]}]
        });
        let text = export(&Facts::parse(&facts.to_string()).unwrap()).unwrap();
        let docs = read(&text).unwrap();
        assert_eq!(docs.len(), 1);
        let contract = View::root(&docs[0]);
        assert_eq!(member_text(contract, "status").as_deref(), Some("draft"));
        let table = items(contract, "schema")[0];
        assert_eq!(member_text(table, "description").as_deref(), Some(nasty));
        assert_eq!(member_text(table, "physicalName").as_deref(), Some(nasty));
        assert_eq!(
            member_text(contract, "id").as_deref(),
            Some(format!("urn:konstruct:{nasty}:t").as_str())
        );
        let column = items(table, "properties")[0];
        assert_eq!(member_text(column, "description").as_deref(), Some(nasty));
    }

    #[test]
    fn the_output_is_the_same_each_time() {
        assert_eq!(written(), written());
    }
}
