//! Whether the semantic layers deployed on data platforms are what the code computes.
//!
//! A platform's semantic view (Snowflake), metric view (Databricks) or Ossie model is read into a
//! [`Doc`] (the readers are [`ddl`] and [`yaml`]; expressions stay text, which Python parses with
//! sqlglot and resolves against the graph). [`judge`] then gives each metric a verdict from the
//! claims Python resolved, and each key, relationship and column one from the facts the code
//! holds, comparing formulas with the identity test ([`crate::identity`]).
//!
//! The binding rule decides what a metric is compared with:
//!
//! 1. the metric's formula is composed through the graph from its table's relation (Python);
//! 2. it is compared with each approved term or Ossie metric that defines a column it reads or
//!    answers to its name or a synonym (never the file the metric was read from), and with the
//!    metrics of other layers that implement the same concept (reported once, as a pair that
//!    names both layers, whatever order they were read in), graded by how the match was made. A match
//!    through the columns the metric reads is `columns`; one through the name or a synonym alone
//!    is `name`;
//! 3. a difference with a `columns` target is an error, and one with a `name` target only a
//!    warning: a name is a guess;
//! 4. with no target, the verdict is `unbound`, never `conforms`.
//!
//! A missing filter is reported only against a target's own filters: an approved definition's
//! required filters, or the conditions a sibling's formula is aggregated under. Identity says
//! "the same" with an error bound, which the report carries, and "differs" with a witness;
//! an expression that did not parse is `unparsed`.

pub mod ddl;
#[cfg(feature = "python")]
pub mod python;
pub mod yaml;

use std::collections::HashMap;

use serde_json::{Map, Value, json};

use crate::identity::Formulas;

/// The version of the `conform.json` document.
pub const FORMAT_VERSION: u32 = 1;

/// A semantic layer as one platform describes it.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Doc {
    /// `snowflake` or `databricks`.
    pub platform: String,
    pub name: String,
    /// The sqlglot dialect its expressions are written in.
    pub dialect: String,
    pub description: Option<String>,
    pub tables: Vec<Table>,
    pub relationships: Vec<Relationship>,
    pub metrics: Vec<Metric>,
    pub verified_queries: Vec<Verified>,
    /// A metric view's filter, which every measure runs under.
    pub filter: Option<String>,
}

/// Whether a field is a fact (a row-level value) or a dimension (what to group by).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FieldKind {
    Fact,
    Dimension,
}

impl FieldKind {
    fn name(self) -> &'static str {
        match self {
            Self::Fact => "fact",
            Self::Dimension => "dimension",
        }
    }
}

/// A logical table over one physical relation.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Table {
    pub name: String,
    /// The relation it reads, as written.
    pub source: String,
    pub primary_key: Vec<String>,
    pub unique: Vec<Vec<String>>,
    pub synonyms: Vec<String>,
    pub description: Option<String>,
    pub fields: Vec<Field>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Field {
    pub name: String,
    pub expr: String,
    pub kind: FieldKind,
    pub synonyms: Vec<String>,
    pub description: Option<String>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Relationship {
    pub name: String,
    /// The table holding the foreign key, then the one it refers to.
    pub from: String,
    pub to: String,
    pub from_columns: Vec<String>,
    pub to_columns: Vec<String>,
    pub cardinality: Option<String>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Metric {
    pub name: String,
    /// The table it belongs to; None for a metric over other metrics.
    pub table: Option<String>,
    pub expr: String,
    pub synonyms: Vec<String>,
    pub description: Option<String>,
    /// Why the expression alone does not say what the metric is, such as a window it runs over.
    pub unsupported: Option<String>,
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct Verified {
    pub name: String,
    pub question: Option<String>,
    pub sql: Option<String>,
    pub verified_at: Option<String>,
    pub verified_by: Option<String>,
}

impl Doc {
    #[must_use]
    pub fn new(platform: &str, name: &str, dialect: &str) -> Self {
        Self {
            platform: platform.to_string(),
            name: name.to_string(),
            dialect: dialect.to_string(),
            ..Self::default()
        }
    }
}

impl Table {
    #[must_use]
    pub fn new(name: &str, source: &str) -> Self {
        Self {
            name: name.to_string(),
            source: source.to_string(),
            ..Self::default()
        }
    }
}

fn opt(text: &Option<String>) -> Value {
    text.as_ref().map_or(Value::Null, |t| json!(t))
}

impl Doc {
    /// The document as JSON, the plain data Python reads.
    #[must_use]
    pub fn to_json(&self) -> Value {
        json!({
            "platform": self.platform,
            "name": self.name,
            "dialect": self.dialect,
            "description": opt(&self.description),
            "filter": opt(&self.filter),
            "tables": self.tables.iter().map(|t| json!({
                "name": t.name,
                "source": t.source,
                "primary_key": t.primary_key,
                "unique": t.unique,
                "synonyms": t.synonyms,
                "description": opt(&t.description),
                "fields": t.fields.iter().map(|f| json!({
                    "name": f.name,
                    "expr": f.expr,
                    "kind": f.kind.name(),
                    "synonyms": f.synonyms,
                    "description": opt(&f.description),
                })).collect::<Vec<_>>(),
            })).collect::<Vec<_>>(),
            "relationships": self.relationships.iter().map(|r| json!({
                "name": r.name,
                "from": r.from,
                "to": r.to,
                "from_columns": r.from_columns,
                "to_columns": r.to_columns,
                "cardinality": opt(&r.cardinality),
            })).collect::<Vec<_>>(),
            "metrics": self.metrics.iter().map(|m| json!({
                "name": m.name,
                "table": opt(&m.table),
                "expr": m.expr,
                "synonyms": m.synonyms,
                "description": opt(&m.description),
                "unsupported": opt(&m.unsupported),
            })).collect::<Vec<_>>(),
            "verified_queries": self.verified_queries.iter().map(|q| json!({
                "name": q.name,
                "question": opt(&q.question),
                "sql": opt(&q.sql),
                "verified_at": opt(&q.verified_at),
                "verified_by": opt(&q.verified_by),
            })).collect::<Vec<_>>(),
        })
    }
}

// Statements

/// The kind of a statement of a SQL file that holds a platform's semantic layer.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StatementKind {
    /// `CREATE SEMANTIC VIEW`, read by [`ddl::read`].
    SnowflakeView,
    /// `CREATE VIEW ... WITH METRICS LANGUAGE YAML AS $$ ... $$`, whose body is YAML.
    DatabricksMetricView,
}

/// A statement of a platform's semantic layer found in SQL text: its kind, its view's name and
/// its text, or for a metric view the YAML between the `$$`.
#[derive(Clone, Debug, PartialEq)]
pub struct Statement {
    pub kind: StatementKind,
    pub name: String,
    pub body: String,
}

/// Splits `text` at semicolons outside strings and comments, and keeps the statements that
/// define a semantic view or a metric view; others are not this feature's.
#[must_use]
pub fn statements(text: &str) -> Vec<Statement> {
    let mut found = Vec::new();
    for statement in split(text) {
        let head = uncommented(&statement);
        let words: Vec<String> = head
            .split(|c: char| !(c.is_alphanumeric() || c == '_' || c == '.' || c == '`' || c == '"'))
            .filter(|w| !w.is_empty())
            .take(12)
            .map(str::to_ascii_lowercase)
            .collect();
        let has = |w: &str| words.iter().any(|x| x == w);
        let name = || {
            words
                .iter()
                .skip_while(|w| *w != "view")
                .skip(1)
                .find(|w| !["if", "not", "exists"].contains(&w.as_str()))
                .map(|w| {
                    w.rsplit('.')
                        .next()
                        .unwrap_or(w)
                        .trim_matches(['`', '"'])
                        .to_string()
                })
                .unwrap_or_default()
        };
        if has("create") && has("semantic") && has("view") {
            found.push(Statement {
                kind: StatementKind::SnowflakeView,
                name: name(),
                body: statement,
            });
        } else if has("create") && has("view") && has("metrics") && statement.contains("$$") {
            let mut parts = statement.splitn(3, "$$");
            if let (_, Some(body), _) = (parts.next(), parts.next(), parts.next()) {
                found.push(Statement {
                    kind: StatementKind::DatabricksMetricView,
                    name: name(),
                    body: body.to_string(),
                });
            }
        }
    }
    found
}

/// `statement` up to its first `$$` string, without comments.
fn uncommented(statement: &str) -> String {
    let before = statement.split("$$").next().unwrap_or_default();
    let mut out = String::new();
    let mut rest = before;
    while !rest.is_empty() {
        if let Some(tail) = rest.strip_prefix("--") {
            rest = tail.find('\n').map_or("", |n| &tail[n..]);
        } else if let Some(tail) = rest.strip_prefix("/*") {
            rest = tail.find("*/").map_or("", |n| &tail[n + 2..]);
        } else {
            let width = rest.chars().next().map_or(1, char::len_utf8);
            out.push_str(&rest[..width]);
            rest = &rest[width..];
        }
    }
    out
}

/// Statements, split at semicolons outside quotes, `$$` strings and comments.
fn split(text: &str) -> Vec<String> {
    let bytes = text.as_bytes();
    let mut out = Vec::new();
    let (mut start, mut i) = (0, 0);
    while i < bytes.len() {
        match bytes[i] {
            b'\'' | b'"' | b'`' => {
                let quote = bytes[i];
                i += 1;
                while i < bytes.len() && bytes[i] != quote {
                    i += usize::from(bytes[i] == b'\\' && quote == b'\'') + 1;
                }
            }
            b'$' if bytes.get(i + 1) == Some(&b'$') => {
                i += 2;
                while i + 1 < bytes.len() && !(bytes[i] == b'$' && bytes[i + 1] == b'$') {
                    i += 1;
                }
                i += 1;
            }
            b'-' if bytes.get(i + 1) == Some(&b'-') => {
                while i < bytes.len() && bytes[i] != b'\n' {
                    i += 1;
                }
            }
            b'/' if bytes.get(i + 1) == Some(&b'*') => {
                i += 2;
                while i + 1 < bytes.len() && !(bytes[i] == b'*' && bytes[i + 1] == b'/') {
                    i += 1;
                }
                i += 1;
            }
            b';' => {
                out.push(text[start..i].to_string());
                start = i + 1;
            }
            _ => {}
        }
        i += 1;
    }
    out.push(text[start..].to_string());
    out.into_iter().filter(|s| !s.trim().is_empty()).collect()
}

// Judging

/// How the code answers a platform's claim.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Verdict {
    Conforms,
    Differs,
    Unbound,
    Unparsed,
    KeyMisfiled,
    KeyUntested,
    CardinalityContradicted,
    UnknownJoin,
    ColumnMissing,
}

impl Verdict {
    #[must_use]
    pub fn name(self) -> &'static str {
        match self {
            Self::Conforms => "conforms",
            Self::Differs => "differs",
            Self::Unbound => "unbound",
            Self::Unparsed => "unparsed",
            Self::KeyMisfiled => "key-misfiled",
            Self::KeyUntested => "key-untested",
            Self::CardinalityContradicted => "cardinality-contradicted",
            Self::UnknownJoin => "unknown-join",
            Self::ColumnMissing => "column-missing",
        }
    }
}

fn s(value: &Value, key: &str) -> String {
    value
        .get(key)
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string()
}

fn so(value: &Value, key: &str) -> Value {
    value.get(key).cloned().unwrap_or(Value::Null)
}

fn items<'a>(value: &'a Value, key: &str) -> &'a [Value] {
    value
        .get(key)
        .and_then(Value::as_array)
        .map_or(&[], Vec::as_slice)
}

fn index(value: &Value, key: &str) -> Option<u32> {
    value.get(key)?.as_u64().and_then(|n| u32::try_from(n).ok())
}

fn texts(value: &Value, key: &str) -> Vec<String> {
    items(value, key)
        .iter()
        .filter_map(|v| v.as_str().map(str::to_string))
        .collect()
}

/// A name as concepts are matched by it: lower case, without separators. The one normaliser:
/// Python reads it through `konstruct._core.conform_norm`. Lower case comes first, so that a
/// letter that lowercases to a letter and a mark (`İ`) is the letter.
#[must_use]
pub fn norm(name: &str) -> String {
    name.chars()
        .flat_map(char::to_lowercase)
        .filter(|c| c.is_alphanumeric())
        .collect()
}

/// A finding with the fields every kind has.
fn finding(
    kind: &str,
    claim: &Value,
    verdict: Verdict,
    severity: &str,
    detail: &str,
) -> Map<String, Value> {
    let mut found = Map::new();
    found.insert("document".into(), so(claim, "document"));
    found.insert("kind".into(), json!(kind));
    found.insert("name".into(), so(claim, "name"));
    found.insert("verdict".into(), json!(verdict.name()));
    found.insert("severity".into(), json!(severity));
    found.insert("detail".into(), json!(detail));
    found
}

/// Whether `a` and `b` are shown to be the same, and the bound on the error of that.
fn same(formulas: &mut Formulas, a: u32, b: u32) -> (bool, f64) {
    let verdict = formulas.compare(a, b);
    (verdict.equal, verdict.error_bound)
}

/// The names a claim answers to.
fn names(claim: &Value) -> Vec<String> {
    let mut found = vec![norm(&s(claim, "name"))];
    found.extend(texts(claim, "synonyms").iter().map(|n| norm(n)));
    found
}

/// The state of a claim, with why: from how Python's reading of it went (`resolution`), or the
/// `state` it was given.
fn classify(claim: &Value) -> (String, String) {
    let Some(r) = claim.get("resolution") else {
        return (s(claim, "state"), s(claim, "detail"));
    };
    let flag = |k: &str| r.get(k).and_then(Value::as_bool).unwrap_or(false);
    let text = |k: &str| r.get(k).and_then(Value::as_str).filter(|t| !t.is_empty());
    let unbound = |detail: String| ("unbound".to_string(), detail);
    if !flag("parsed") {
        (
            "unparsed".to_string(),
            "its expression does not parse as SQL".to_string(),
        )
    } else if let Some(names) = text("unreadable") {
        (
            "unparsed".to_string(),
            format!("it reads {names}, whose expression does not parse"),
        )
    } else if let Some(why) = text("derived")
        .or_else(|| text("unsupported"))
        .or_else(|| text("fan_out"))
    {
        unbound(why.to_string())
    } else if let Some(tables) = text("unbound_tables") {
        unbound(format!("it reads {tables}, which the graph does not have"))
    } else if flag("portable") {
        match text("formula_error") {
            Some(error) => ("unparsed".to_string(), error.to_string()),
            None => ("bound".to_string(), String::new()),
        }
    } else if flag("failed") {
        let problem = text("problem").unwrap_or("it reads nothing the graph has");
        let problem = problem.split_once(": ").map_or(problem, |(_, rest)| rest);
        if flag("table_unbound") {
            unbound(problem.to_string())
        } else {
            ("column-missing".to_string(), problem.to_string())
        }
    } else {
        let missing = texts(r, "missing").join(", ");
        let missing = if missing.is_empty() {
            "a column".to_string()
        } else {
            missing
        };
        (
            "column-missing".to_string(),
            format!("it reads {missing}, which the code no longer has"),
        )
    }
}

/// Whether a metric is `unbound` because the graph lacks a table it reads, as opposed to what the
/// layer itself writes (a derived metric, a window) or because there is nothing to compare it with.
fn graph_cause(claim: &Value) -> bool {
    let Some(r) = claim.get("resolution") else {
        return false;
    };
    let text = |k: &str| {
        r.get(k)
            .and_then(Value::as_str)
            .is_some_and(|t| !t.is_empty())
    };
    let flag = |k: &str| r.get(k).and_then(Value::as_bool).unwrap_or(false);
    flag("parsed")
        && !text("unreadable")
        && !(text("derived") || text("unsupported") || text("fan_out"))
        && (text("unbound_tables")
            || (!flag("portable") && flag("failed") && flag("table_unbound")))
}

fn bound(claim: &Value) -> bool {
    classify(claim).0 == "bound"
}

/// What the caller says holds on the rows of a set of columns, as `(key, text)` of each
/// condition, by key: the filters every one of the columns is computed under.
pub type Held<'a> = &'a mut dyn FnMut(&[String]) -> Vec<(String, String)>;

/// [`Held`], asked once for each set of columns.
struct Rows<'a> {
    ask: Held<'a>,
    known: HashMap<Vec<String>, Vec<(String, String)>>,
}

impl Rows<'_> {
    fn held(&mut self, columns: &[String]) -> Vec<(String, String)> {
        if let Some(found) = self.known.get(columns) {
            return found.clone();
        }
        let found = (self.ask)(columns);
        self.known.insert(columns.to_vec(), found.clone());
        found
    }
}

/// The approved terms and Ossie metrics of the project, indexed by what a claim can be matched
/// through: the columns a term defines and the names it answers to.
struct Terms<'a> {
    all: &'a [Value],
    by_column: HashMap<&'a str, Vec<usize>>,
    by_name: HashMap<String, Vec<usize>>,
}

impl<'a> Terms<'a> {
    fn new(all: &'a [Value]) -> Self {
        let mut by_column: HashMap<&str, Vec<usize>> = HashMap::new();
        let mut by_name: HashMap<String, Vec<usize>> = HashMap::new();
        for (k, term) in all.iter().enumerate() {
            for column in items(term, "defined").iter().filter_map(Value::as_str) {
                by_column.entry(column).or_default().push(k);
            }
            for name in texts(term, "names") {
                by_name.entry(norm(&name)).or_default().push(k);
            }
        }
        Self {
            all,
            by_column,
            by_name,
        }
    }

    /// The terms that might define a claim: those that define a column it reads or answer to its
    /// name or a synonym, but not the file the claim was read from, in the order they are
    /// listed. Each filter a term requires is `held` when it holds on the rows of every column the
    /// claim reads (the term's `keys` are all among the conditions that hold), or when the layer's
    /// own `view` filter says it (a metric view's filter, as identity compares conditions).
    fn of(&self, formulas: &mut Formulas, claim: &Value, rows: &mut Rows<'_>) -> Vec<Value> {
        let columns = texts(claim, "columns");
        let mut found: Vec<usize> = columns
            .iter()
            .filter_map(|c| self.by_column.get(c.as_str()))
            .chain(names(claim).iter().filter_map(|n| self.by_name.get(n)))
            .flatten()
            .copied()
            .collect();
        found.sort_unstable();
        found.dedup();
        let path = s(claim, "path");
        let view: Vec<u32> = items(claim, "view")
            .iter()
            .filter_map(|v| v.as_u64().and_then(|n| u32::try_from(n).ok()))
            .collect();
        let mut out = Vec::new();
        for k in found {
            let term = &self.all[k];
            let own = s(term, "path");
            if !own.is_empty() && path.ends_with(&own) {
                continue; // the file being checked
            }
            let mut bound = term.clone();
            let mut filters = Vec::new();
            for filter in items(term, "filters") {
                let keys = texts(filter, "keys");
                let mut held = !keys.is_empty() && {
                    let on_route = rows.held(&columns);
                    keys.iter()
                        .all(|k| on_route.iter().any(|(held, _)| held == k))
                };
                if !held {
                    let parts: Vec<u32> = items(filter, "definitions")
                        .iter()
                        .filter_map(|v| v.as_u64().and_then(|n| u32::try_from(n).ok()))
                        .collect();
                    held = !parts.is_empty()
                        && parts
                            .iter()
                            .all(|&part| view.iter().any(|&v| same(formulas, part, v).0));
                }
                filters.push(json!({"text": s(filter, "text"), "held": held}));
            }
            bound["filters"] = Value::Array(filters);
            out.push(bound);
        }
        out
    }
}

/// What a claim's formula is compared with after the others: the metrics of other layers that
/// implement the same concept, once for each pair, whatever order the layers were read in.
struct Pairs {
    findings: Vec<Value>,
    /// For each claim that agrees with a sibling, the bound on the error of that.
    agreed: Vec<Option<(f64, Vec<Value>)>>,
}

fn pairs(formulas: &mut Formulas, claims: &[Value], rows: &mut Rows<'_>) -> Pairs {
    use std::collections::{BTreeSet, HashMap};

    let mut out = Pairs {
        findings: Vec::new(),
        agreed: vec![None; claims.len()],
    };
    // Claims by each name they answer to, so that only those that share one are paired.
    let mut by_name: HashMap<String, Vec<usize>> = HashMap::new();
    for (k, claim) in claims.iter().enumerate() {
        if bound(claim) {
            for name in names(claim) {
                by_name.entry(name).or_default().push(k);
            }
        }
    }
    let mut candidates: BTreeSet<(usize, usize)> = BTreeSet::new();
    for group in by_name.values().filter(|g| g.len() > 1) {
        for (x, &i) in group.iter().enumerate() {
            for &j in &group[x + 1..] {
                candidates.insert((i.min(j), i.max(j)));
            }
        }
    }
    for (i, j) in candidates {
        {
            let (mut a, mut b) = (i, j);
            if claims[a]["document"] == claims[b]["document"] {
                continue;
            }
            let (Some(fa), Some(fb)) = (index(&claims[a], "formula"), index(&claims[b], "formula"))
            else {
                continue;
            };
            // The same finding whichever layer came first.
            let key = |k: usize| (s(&claims[k], "document"), s(&claims[k], "name"));
            if key(b) < key(a) {
                std::mem::swap(&mut a, &mut b);
            }
            let (ca, cb) = (&claims[a], &claims[b]);
            let (fa, fb) = if a == i { (fa, fb) } else { (fb, fa) };
            let shared = texts(ca, "columns")
                .iter()
                .any(|c| texts(cb, "columns").contains(c));
            let mut missing = Vec::new();
            let (held_a, held_b) = (
                rows.held(&texts(ca, "columns")),
                rows.held(&texts(cb, "columns")),
            );
            for (x, applied, kept) in [(ca, &held_a, &held_b), (cb, &held_b, &held_a)] {
                for (key, text) in applied {
                    if !kept.iter().any(|(k, _)| k == key) {
                        // `by` is what requires the filter, as in a metric's finding.
                        let by = format!("{} ({})", s(x, "name"), s(x, "document"));
                        missing.push(json!({
                            "filter": text,
                            "against": "sibling implementation",
                            "by": by,
                        }));
                    }
                }
            }
            let (equal, error) = same(formulas, fa, fb);
            if equal && missing.is_empty() {
                for (k, other) in [(i, j), (j, i)] {
                    let slot = out.agreed[k].get_or_insert((0.0, Vec::new()));
                    slot.0 = slot.0.max(error);
                    slot.1.push(json!({
                        "name": s(&claims[other], "name"),
                        "source": "sibling",
                        "document": s(&claims[other], "document"),
                        "grade": if shared { "columns+name" } else { "name" },
                    }));
                }
                continue;
            }
            let (severity, detail) = if shared {
                (
                    "error",
                    "two layers compute the same concept differently, and read the same columns",
                )
            } else {
                (
                    "warning",
                    "two layers matched by name alone compute it differently",
                )
            };
            let mut f = finding("pair", ca, Verdict::Differs, severity, detail);
            f.insert(
                "layers".into(),
                json!([
                    {"document": s(ca, "document"), "name": s(ca, "name")},
                    {"document": s(cb, "document"), "name": s(cb, "name")},
                ]),
            );
            f.insert("missing_filters".into(), Value::Array(missing));
            f.insert(
                "compare".into(),
                if equal { Value::Null } else { json!([fa, fb]) },
            );
            f.insert(
                "grade".into(),
                json!(if shared { "columns+name" } else { "name" }),
            );
            out.findings.push(Value::Object(f));
        }
    }
    // Which sibling was met first depends on the order the layers were read in; the report must not.
    for (_, siblings) in out.agreed.iter_mut().flatten() {
        siblings.sort_by_key(|a| (s(a, "document"), s(a, "name")));
    }
    out
}

/// The verdict on one metric: see the module for the binding rule.
fn metric(
    formulas: &mut Formulas,
    claim: &Value,
    agreed: Option<&(f64, Vec<Value>)>,
    terms: &Terms<'_>,
    rows: &mut Rows<'_>,
) -> Value {
    let (state, detail) = classify(claim);
    let mut out = match state.as_str() {
        "unparsed" => finding("metric", claim, Verdict::Unparsed, "warning", &detail),
        "column-missing" => finding("metric", claim, Verdict::ColumnMissing, "error", &detail),
        "unbound" => finding("metric", claim, Verdict::Unbound, "info", &detail),
        _ => {
            let terms = terms.of(formulas, claim, rows);
            return bound_metric(formulas, claim, agreed, &terms);
        }
    };
    if graph_cause(claim) {
        // The graph, not the layer, is why it cannot be compared: the gate holds a change to that.
        out.insert("cause".into(), json!("graph"));
    }
    out.insert("code".into(), so(claim, "code"));
    Value::Object(out)
}

fn bound_metric(
    formulas: &mut Formulas,
    claim: &Value,
    agreed: Option<&(f64, Vec<Value>)>,
    terms: &[Value],
) -> Value {
    let own = index(claim, "formula");
    let wanted = names(claim);
    let columns = texts(claim, "columns");
    let mut reasons: Vec<Value> = Vec::new();
    let mut error_bound = agreed.map_or(0.0, |a| a.0);
    // Whether identity compared formulas, and whether only filters were checked.
    let mut formulas_compared = agreed.is_some();
    let (mut strong, mut weak, mut filters_checked) = (false, false, false);
    let mut against: Vec<Value> = agreed.map_or_else(Vec::new, |a| a.1.clone());
    let mut missing = Vec::new();
    let mut compare = Value::Null;
    for term in terms {
        let called: Vec<String> = texts(term, "names").iter().map(|n| norm(n)).collect();
        let by_name = wanted.iter().any(|n| called.contains(n));
        let approved = term
            .get("approved")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let formula = index(term, "formula");
        let mut by_columns = texts(term, "defined").iter().any(|c| columns.contains(c));
        if by_name && formula.is_some() {
            by_columns |= texts(term, "reads").iter().any(|c| columns.contains(c));
        }
        let filters = items(term, "filters");
        if !(by_columns || by_name) || (formula.is_none() && filters.is_empty()) {
            continue;
        }
        let grade = match (by_columns, by_name) {
            (true, true) => "columns+name",
            (true, false) => "columns",
            _ => "name",
        };
        // A match through columns alone is a candidate: an error needs the name or a synonym with
        // a formula, or an approved formula.
        let binding = by_name || approved;
        let mut differs = false;
        let mut error = false;
        // A required filter missing from an approved term the metric answers to by name is an
        // error; a candidate matched through columns alone is not.
        let filter_error = by_name && approved;
        if let (Some(a), Some(b)) = (own, formula) {
            formulas_compared = true;
            let (equal, bound) = same(formulas, a, b);
            if equal {
                error_bound = error_bound.max(bound);
            } else {
                differs = true;
                error |= binding;
                if compare.is_null() {
                    compare = json!([a, b]);
                }
                reasons.push(json!({"reason": "formula", "against": s(term, "name"), "source": s(term, "source")}));
            }
        }
        for filter in filters {
            filters_checked = true;
            if !filter.get("held").and_then(Value::as_bool).unwrap_or(false) {
                differs = true;
                error |= filter_error;
                missing.push(json!({"filter": s(filter, "text"), "against": "approved definition", "by": s(term, "name")}));
                reasons.push(json!({"reason": "filter", "against": s(term, "name"), "source": s(term, "source")}));
            }
        }
        if differs {
            strong |= error;
            weak |= !error;
        }
        against.push(json!({
            "name": s(term, "name"),
            "source": s(term, "source"),
            "owner": so(term, "owner"),
            "grade": grade,
            "approved": approved,
        }));
    }
    let (verdict, severity, detail) = if strong {
        (
            Verdict::Differs,
            "error",
            "differs from what the code and its approved definition say",
        )
    } else if weak {
        (
            Verdict::Differs,
            "warning",
            "differs from a definition matched through its columns or its name alone",
        )
    } else if formulas_compared {
        (Verdict::Conforms, "info", "is what the code computes")
    } else if filters_checked {
        (
            Verdict::Unbound,
            "info",
            "its required filters hold; no formula to compare",
        )
    } else {
        (
            Verdict::Unbound,
            "info",
            "nothing approved or implemented elsewhere to compare it with",
        )
    };
    let mut out = finding("metric", claim, verdict, severity, detail);
    // Whether its required filters were held and that is all there was to check: the gate holds
    // a change to it as it does to a metric that conforms.
    out.insert(
        "held".into(),
        json!(verdict == Verdict::Unbound && filters_checked),
    );
    out.insert("code".into(), so(claim, "code"));
    out.insert("against".into(), Value::Array(against));
    out.insert("reasons".into(), Value::Array(reasons));
    out.insert("missing_filters".into(), Value::Array(missing));
    out.insert("compare".into(), compare);
    if verdict == Verdict::Conforms {
        out.insert("error_bound".into(), json!(error_bound));
    }
    Value::Object(out)
}

/// A key a layer declares, or a fact it declares that is a key: `unique` says whether a test or
/// the source's own key makes the column unique, and `entity` which entity's key it is.
fn key(fact: &Value) -> Option<Value> {
    let declared = s(fact, "declared");
    let unique = fact.get("unique").and_then(Value::as_bool).unwrap_or(false);
    let entity = s(fact, "entity");
    let (verdict, severity, detail) = match declared.as_str() {
        "fact" if unique && !entity.is_empty() => (
            Verdict::KeyMisfiled,
            "warning",
            format!("is the key of {entity}, a key and not a fact"),
        ),
        "primary key" | "unique key" if !unique => (
            Verdict::KeyUntested,
            "warning",
            "is declared a key, and no unique test in the code says so".to_string(),
        ),
        _ => return None,
    };
    let mut out = finding("key", fact, verdict, severity, &detail);
    out.insert("evidence".into(), so(fact, "evidence"));
    Some(Value::Object(out))
}

/// The relationships of the code, by the column that refers and the one it refers to, with the
/// cardinality the code gives each.
fn code_joins(request: &Value) -> HashMap<(String, String), String> {
    items(request, "code_joins")
        .iter()
        .map(|j| ((s(j, "from"), s(j, "to")), s(j, "cardinality")))
        .collect()
}

/// A relationship a layer declares from the column `from` to the column `to`, compared with the
/// code's: the way the code relates the two (the same way, the other way or not at all), and,
/// when the same way, whether the cardinality the layer declares (many-to-one when it says
/// none) is what the code says.
fn join(fact: &Value, code: &HashMap<(String, String), String>) -> Option<Value> {
    let (from, to) = (s(fact, "from"), s(fact, "to"));
    let (verdict, severity, detail) = if let Some(code) = code.get(&(from.clone(), to.clone())) {
        let declared = Some(s(fact, "declared"))
            .filter(|d| !d.is_empty())
            .unwrap_or_else(|| "many_to_one".to_string())
            .replace('_', "-");
        // Whatever the layer declares, the code may say another; a many-to-one that is also
        // one-to-one is true of the code.
        let fine = code.is_empty()
            || *code == declared
            || (declared == "many-to-one" && code == "one-to-one");
        if fine {
            return None;
        }
        (
            Verdict::CardinalityContradicted,
            "error",
            format!("is {declared}, but the code says {code}"),
        )
    } else if code.contains_key(&(to, from)) {
        (
            Verdict::CardinalityContradicted,
            "error",
            "refers the other way round to the code's relationship".to_string(),
        )
    } else {
        (
            Verdict::UnknownJoin,
            "warning",
            "joins columns no join, test or relationship in the code relates".to_string(),
        )
    };
    let mut out = finding("relationship", fact, verdict, severity, &detail);
    out.insert("evidence".into(), so(fact, "evidence"));
    Some(Value::Object(out))
}

fn column(fact: &Value) -> Option<Value> {
    let (verdict, severity) = match s(fact, "state").as_str() {
        "column-missing" => (Verdict::ColumnMissing, "error"),
        "unbound" => (Verdict::Unbound, "info"),
        "unparsed" => (Verdict::Unparsed, "warning"),
        _ => return None,
    };
    Some(Value::Object(finding(
        &s(fact, "kind"),
        fact,
        verdict,
        severity,
        &s(fact, "detail"),
    )))
}

/// Judges the claims and facts Python resolved, as `{claims, terms, keys, joins, code_joins,
/// columns}`, with the formulas they refer to in `formulas`; returns the findings and their
/// counts, the body of the document `conform` reports.
///
/// A claim is a metric with the file it was read from, the columns it reads, its formula and
/// how reading it went; the `terms` are those of the project that might define it, which this
/// binds (see the module), asking `held` which filters hold on a claim's rows only when a term
/// requires one or another layer has a metric of the same name. A fact is a key (with whether
/// its column is unique and which entity it is the key of), a relationship (the columns it
/// joins, with the code's own in `code_joins`) or a column. Each finding keeps what it was made
/// from, so the caller adds only text.
#[must_use]
pub fn judge(formulas: &mut Formulas, request: &Value, held: Held<'_>) -> Value {
    let claims = items(request, "claims");
    let mut rows = Rows {
        ask: held,
        known: HashMap::new(),
    };
    let terms = Terms::new(items(request, "terms"));
    let siblings = pairs(formulas, claims, &mut rows);
    let mut findings: Vec<Value> = Vec::new();
    findings.extend(
        claims
            .iter()
            .zip(&siblings.agreed)
            .map(|(c, a)| metric(formulas, c, a.as_ref(), &terms, &mut rows)),
    );
    findings.extend(siblings.findings);
    findings.extend(items(request, "columns").iter().filter_map(column));
    findings.extend(items(request, "keys").iter().filter_map(key));
    let code = code_joins(request);
    findings.extend(
        items(request, "joins")
            .iter()
            .filter_map(|j| join(j, &code)),
    );
    let of = |f: &Value, k: &str| {
        f.get(k)
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string()
    };
    let count = |verdict: &str| {
        findings
            .iter()
            .filter(|f| of(f, "verdict") == verdict)
            .count()
    };
    let is_metric = |f: &&Value| of(f, "kind") == "metric";
    let metrics = |verdict: &str| {
        findings
            .iter()
            .filter(is_metric)
            .filter(|f| of(f, "verdict") == verdict)
            .count()
    };
    let mut documents = Map::new();
    for f in findings.iter().filter(is_metric) {
        let entry = documents.entry(of(f, "document")).or_insert_with(
            || json!({"metrics": 0, "conforms": 0, "differs": 0, "unbound": 0, "unparsed": 0}),
        );
        for k in ["metrics", of(f, "verdict").as_str()] {
            if let Some(n) = entry.get(k).and_then(Value::as_u64) {
                entry[k] = json!(n + 1);
            }
        }
    }
    let summary = json!({
        "metrics": findings.iter().filter(is_metric).count(),
        "conforms": metrics("conforms"),
        "differs": metrics("differs"),
        "unbound": metrics("unbound"),
        "unparsed": metrics("unparsed"),
        "pairs": findings.iter().filter(|f| of(f, "kind") == "pair").count(),
        "key_misfiled": count("key-misfiled"),
        "key_untested": count("key-untested"),
        "cardinality_contradicted": count("cardinality-contradicted"),
        "unknown_join": count("unknown-join"),
        "column_missing": count("column-missing"),
    });
    json!({"format_version": FORMAT_VERSION, "findings": findings, "summary": summary, "documents": documents})
}

#[cfg(test)]
mod tests;
