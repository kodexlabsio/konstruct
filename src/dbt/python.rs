//! `konstruct.inputs.dbt`'s reader: `read_artefact` opens a manifest or a catalog, and
//! `Artefact.load` adds a manifest's relations, sources, exposures and tests to a graph.
//!
//! What the graph receives is what `dbt.load` put in it when it was Python: the same nodes in the
//! same order, with the same attributes, so graph.json is byte for byte what it was.

use std::borrow::Cow;
use std::collections::HashMap;
use std::collections::hash_map::Entry;
use std::path::{Component, Path, PathBuf};

use pyo3::exceptions::{PyKeyError, PyTypeError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PySet, PyString};

use crate::jinja::python::{guarded, macro_text, variables_of};
use crate::jinja::{Macros, Renderer, Tables};
use crate::json::tape::{Doc, Kind, Number, Tape};
use crate::model::{Graph, Node};

use super::relation;

/// A model to analyse: its id, relation, SQL (None when its template did not render), whether dbt
/// compiled it, and its upstream ids.
type Model<'py> = (Bound<'py, PyAny>, String, Option<String>, bool, Vec<String>);

/// A database and a schema, as a node names them.
type SchemaKey = (Option<String>, Option<String>);

/// A manifest or catalog: the file's bytes and where every value in it starts.
#[pyclass(frozen, module = "konstruct.inputs.dbt")]
pub struct Artefact {
    bytes: Vec<u8>,
    tape: Tape,
}

impl Artefact {
    fn doc(&self) -> Doc<'_> {
        self.tape.doc(&self.bytes)
    }
}

/// The artefact at `path`, or None when the file cannot be read, is not JSON or is not an object.
#[pyfunction]
pub fn read_artefact(path: PathBuf) -> Option<Artefact> {
    let bytes = std::fs::read(path).ok()?;
    // dbt writes its artefacts with Python's json, which writes NaN and Infinity.
    let tape = Tape::parse_python(&bytes).ok()?;
    (tape.doc(&bytes).kind(0) == Kind::Object).then_some(Artefact { bytes, tape })
}

/// The key of a relation name as a manifest writes it, in the SQL dialect of its adapter; None for
/// an empty name.
#[pyfunction]
#[pyo3(signature = (name, dialect=None))]
pub fn relation_name(name: &str, dialect: Option<&str>) -> Option<String> {
    relation::parse(name, dialect)
}

/// The member `key` of the object at `object`.
fn get(doc: Doc<'_>, object: usize, key: &str) -> Option<usize> {
    if doc.kind(object) != Kind::Object {
        return None;
    }
    // A key that is repeated has its last value, as `json.loads` has it.
    doc.members(object)
        .filter(|(name, _)| doc.is(*name, key))
        .map(|(_, value)| value)
        .last()
}

/// The members of an object as `json.loads` makes its dict: a key repeated keeps its first place
/// and its last value.
fn unique_members(doc: Doc<'_>, object: usize) -> Vec<(usize, usize)> {
    let mut found: Vec<(usize, usize)> = Vec::new();
    let mut places: HashMap<Cow<'_, str>, usize> = HashMap::new();
    for (name, value) in doc.members(object) {
        match places.entry(doc.text(name)) {
            Entry::Occupied(place) => found[*place.get()].1 = value,
            Entry::Vacant(place) => {
                place.insert(found.len());
                found.push((name, value));
            }
        }
    }
    found
}

/// A catalog, with its nodes and sources by id so that each model finds its entry at once.
struct CatalogView<'a> {
    doc: Doc<'a>,
    /// Python's truth of the whole catalog: an empty one lists nothing.
    content: bool,
    nodes: HashMap<Cow<'a, str>, usize>,
    sources: HashMap<Cow<'a, str>, usize>,
}

impl<'a> CatalogView<'a> {
    fn new(doc: Doc<'a>) -> Self {
        let group = |name: &str| -> HashMap<Cow<'a, str>, usize> {
            get(doc, 0, name)
                .map(|g| {
                    unique_members(doc, g)
                        .into_iter()
                        .map(|(key, value)| (doc.text(key), value))
                        .collect()
                })
                .unwrap_or_default()
        };
        CatalogView {
            doc,
            content: truthy(doc, 0),
            nodes: group("nodes"),
            sources: group("sources"),
        }
    }
}

/// `get`, but a null is as good as a missing member, as `dict.get(key) or default` has it.
fn present(doc: Doc<'_>, object: usize, key: &str) -> Option<usize> {
    get(doc, object, key).filter(|value| doc.kind(*value) != Kind::Null)
}

/// Python's truth of a JSON value.
fn truthy(doc: Doc<'_>, value: usize) -> bool {
    match doc.kind(value) {
        Kind::Null | Kind::False => false,
        Kind::True => true,
        Kind::Number => match doc.number(value) {
            Number::Int(n) => n != 0,
            Number::UInt(n) => n != 0,
            Number::Float(n) => n != 0.0,
        },
        Kind::String => !doc.raw(value).is_empty(),
        Kind::Array => doc.items(value).next().is_some(),
        Kind::Object => doc.members(value).next().is_some(),
    }
}

fn string<'a>(doc: Doc<'a>, value: usize) -> Option<Cow<'a, str>> {
    (doc.kind(value) == Kind::String).then(|| doc.text(value))
}

/// The text of the member `key`, when it is a string.
fn text<'a>(doc: Doc<'a>, object: usize, key: &str) -> Option<Cow<'a, str>> {
    get(doc, object, key).and_then(|value| string(doc, value))
}

/// The text of the member `key` when it is a string that is not empty.
fn nonempty<'a>(doc: Doc<'a>, object: usize, key: &str) -> Option<Cow<'a, str>> {
    text(doc, object, key).filter(|found| !found.is_empty())
}

/// A description: a string with something in it, stripped.
fn description(doc: Doc<'_>, object: usize, key: &str) -> Option<String> {
    let found = text(doc, object, key)?;
    let stripped = found.trim();
    (!stripped.is_empty()).then(|| stripped.to_string())
}

/// A JSON value as the Python object `json.loads` makes of it.
fn to_py<'py>(py: Python<'py>, doc: Doc<'_>, value: usize) -> PyResult<Bound<'py, PyAny>> {
    Ok(match doc.kind(value) {
        Kind::Null => py.None().into_bound(py),
        Kind::True => PyBool::new(py, true).to_owned().into_any(),
        Kind::False => PyBool::new(py, false).to_owned().into_any(),
        Kind::String => PyString::new(py, &doc.text(value)).into_any(),
        Kind::Number => match doc.number(value) {
            Number::Int(n) => PyInt::new(py, n).into_any(),
            Number::UInt(n) => PyInt::new(py, n).into_any(),
            Number::Float(n) => {
                let (text, integer) = doc.number_text(value);
                if integer {
                    // An integer too large for 64 bits is still an integer, as in Python.
                    py.get_type::<PyInt>().call1((text,))?
                } else {
                    PyFloat::new(py, n).into_any()
                }
            }
        },
        Kind::Array => {
            let list = PyList::empty(py);
            for item in doc.items(value) {
                list.append(to_py(py, doc, item)?)?;
            }
            list.into_any()
        }
        Kind::Object => {
            let dict = PyDict::new(py);
            for (name, member) in doc.members(value) {
                dict.set_item(PyString::new(py, &doc.text(name)), to_py(py, doc, member)?)?;
            }
            dict.into_any()
        }
    })
}

/// A node's id, which must be text.
fn node_id<'py>(py: Python<'py>, doc: Doc<'_>, item: usize) -> PyResult<Bound<'py, PyAny>> {
    match get(doc, item, "unique_id") {
        Some(id) if doc.kind(id) == Kind::String => to_py(py, doc, id),
        Some(_) => Err(PyTypeError::new_err("a node's unique_id is not text")),
        None => Err(PyKeyError::new_err("unique_id")),
    }
}

/// `str(value)` of a JSON value.
fn str_of<'py>(py: Python<'py>, doc: Doc<'_>, value: usize) -> PyResult<String> {
    to_py(py, doc, value)?.str()?.extract()
}

/// The member `key` as a Python object, or `default` when there is none.
fn member_or<'py>(
    py: Python<'py>,
    doc: Doc<'_>,
    object: usize,
    key: &str,
    default: impl FnOnce() -> Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    match get(doc, object, key) {
        Some(value) => to_py(py, doc, value),
        None => Ok(default()),
    }
}

fn member<'py>(
    py: Python<'py>,
    doc: Doc<'_>,
    object: usize,
    key: &str,
) -> PyResult<Bound<'py, PyAny>> {
    member_or(py, doc, object, key, || py.None().into_bound(py))
}

/// `os.path.relpath(path, start)`, which does not look at the file system.
fn relpath(path: &Path, start: &Path) -> String {
    fn normal(path: &Path) -> Vec<Component<'_>> {
        let mut kept: Vec<Component<'_>> = Vec::new();
        for part in path.components() {
            match part {
                Component::CurDir => {}
                Component::ParentDir if matches!(kept.last(), Some(Component::Normal(_))) => {
                    kept.pop();
                }
                other => kept.push(other),
            }
        }
        kept
    }
    let here = std::env::current_dir().unwrap_or_default();
    let (path, start) = (here.join(path), here.join(start));
    let (path, start) = (normal(&path), normal(&start));
    let common = path.iter().zip(&start).take_while(|(a, b)| a == b).count();
    let mut out = PathBuf::new();
    for _ in common..start.len() {
        out.push("..");
    }
    for part in &path[common..] {
        out.push(part.as_os_str());
    }
    if out.as_os_str().is_empty() {
        ".".to_string()
    } else {
        out.to_string_lossy().into_owned()
    }
}

/// What a load needs besides the manifest: where the project is, what its graph is relative to,
/// the SQL dialect of its relation names, and the folders its macros are read from.
struct Context<'a> {
    root: &'a Path,
    graph_root: &'a Path,
    dialect: Option<&'a str>,
}

impl Context<'_> {
    /// The file of an item, relative to the graph's root.
    fn path(&self, doc: Doc<'_>, item: usize) -> Option<String> {
        let found = get(doc, item, "original_file_path").filter(|value| truthy(doc, *value))?;
        let file = string(doc, found).map_or_else(String::new, Cow::into_owned);
        Some(relpath(&self.root.join(file), self.graph_root))
    }

    /// A relation's key from its `relation_name`, or else from its database, schema and name.
    fn relation(&self, doc: Doc<'_>, item: usize) -> Option<String> {
        if let Some(parsed) =
            nonempty(doc, item, "relation_name").and_then(|n| relation::parse(&n, self.dialect))
            && !parsed.is_empty()
        {
            return Some(parsed);
        }
        let name = ["alias", "identifier", "name"]
            .iter()
            .find_map(|key| nonempty(doc, item, key))?;
        let part = |key: &str| text(doc, item, key);
        let key = relation::key([
            part("database").as_deref(),
            part("schema").as_deref(),
            Some(name.as_ref()),
        ]);
        Some(key)
    }
}

#[allow(clippy::too_many_arguments)]
fn node<'py>(
    py: Python<'py>,
    id: Bound<'py, PyAny>,
    kind: &str,
    name: Bound<'py, PyAny>,
    relation: Option<String>,
    description: Option<String>,
    path: Option<String>,
    attrs: Bound<'py, PyDict>,
) -> PyResult<Bound<'py, Node>> {
    let none = || py.None();
    let optional = |text: Option<String>| {
        text.map_or_else(none, |t| PyString::new(py, &t).into_any().unbind())
    };
    Bound::new(
        py,
        Node {
            id: id.unbind(),
            kind: PyString::new(py, kind).into_any().unbind(),
            name: name.unbind(),
            parent: none(),
            relation: optional(relation),
            description: optional(description),
            data_type: none(),
            path: optional(path),
            origins: PyList::new(py, ["dbt"])?.into_any().unbind(),
            attrs: attrs.into_any().unbind(),
        },
    )
}

/// Adds nodes and edges to a graph, through the graph itself unless it is of a subclass, which
/// is asked, as it always was.
struct Edit<'a, 'py> {
    graph: &'a Bound<'py, Graph>,
    exact: bool,
    origin: Bound<'py, PyDict>,
}

impl<'py> Edit<'_, 'py> {
    fn node(&self, node: &Bound<'py, Node>) -> PyResult<Bound<'py, Node>> {
        let py = self.graph.py();
        if self.exact {
            Ok(self.graph.get().merge_node(py, node)?.into_bound(py))
        } else {
            Ok(self
                .graph
                .call_method1("add_node", (node,))?
                .cast_into::<Node>()?)
        }
    }

    fn edge(
        &self,
        source: &Bound<'py, PyAny>,
        target: &Bound<'py, PyAny>,
        kind: &str,
        attrs: Option<&Bound<'py, PyDict>>,
    ) -> PyResult<()> {
        let py = self.graph.py();
        if self.exact {
            self.graph.get().add_edge(
                py,
                source.cast::<PyString>()?,
                target.cast::<PyString>()?,
                PyString::new(py, kind).as_any(),
                attrs,
            )?;
        } else {
            self.graph
                .call_method("add_edge", (source, target, kind), attrs)?;
        }
        Ok(())
    }

    fn feeds(&self, parent: &Bound<'py, PyAny>, node: &Bound<'py, PyAny>) -> PyResult<()> {
        self.edge(parent, node, "feeds", Some(&self.origin))
    }
}

/// A column's id: `<relation id>#<name in lower case>`.
fn column_id<'py>(py: Python<'py>, parent: &str, column: &str) -> Bound<'py, PyAny> {
    PyString::new(py, &format!("{parent}#{}", column.to_lowercase())).into_any()
}

/// The columns of a model's `primary_key` constraint, declared on the model or on its columns.
fn primary_key(py: Python<'_>, doc: Doc<'_>, item: usize) -> PyResult<Vec<String>> {
    if let Some(constraints) =
        present(doc, item, "constraints").filter(|c| doc.kind(*c) == Kind::Array)
    {
        for constraint in doc.items(constraints) {
            if doc.kind(constraint) == Kind::Object
                && text(doc, constraint, "type").as_deref() == Some("primary_key")
            {
                let mut found = Vec::new();
                if let Some(columns) = present(doc, constraint, "columns") {
                    for column in doc.items(columns) {
                        found.push(str_of(py, doc, column)?);
                    }
                }
                return Ok(found);
            }
        }
    }
    let mut found = Vec::new();
    let Some(columns) = present(doc, item, "columns").filter(|c| doc.kind(*c) == Kind::Object)
    else {
        return Ok(found);
    };
    for (name, column) in doc.members(columns) {
        if doc.kind(column) != Kind::Object {
            continue;
        }
        let declared = present(doc, column, "constraints")
            .filter(|c| doc.kind(*c) == Kind::Array)
            .is_some_and(|c| {
                doc.items(c).any(|k| {
                    doc.kind(k) == Kind::Object
                        && text(doc, k, "type").as_deref() == Some("primary_key")
                })
            });
        if declared {
            found.push(match get(doc, column, "name") {
                Some(own) => str_of(py, doc, own)?,
                None => doc.text(name).into_owned(),
            });
        }
    }
    Ok(found)
}

/// The columns the catalog lists for `unique_id`, in the order of their index: name, type and
/// comment.
fn catalog_columns<'a>(
    catalog: Option<&CatalogView<'a>>,
    unique_id: &str,
) -> Vec<(Cow<'a, str>, Option<usize>, Option<String>)> {
    let Some(catalog) = catalog else {
        return Vec::new();
    };
    let doc = catalog.doc;
    let entry = [&catalog.nodes, &catalog.sources].iter().find_map(|group| {
        let found = *group.get(unique_id)?;
        truthy(doc, found).then_some(found)
    });
    let Some(columns) = entry
        .and_then(|e| get(doc, e, "columns"))
        .filter(|c| doc.kind(*c) == Kind::Object)
    else {
        return Vec::new();
    };
    let mut listed: Vec<usize> = unique_members(doc, columns)
        .into_iter()
        .map(|(_, column)| column)
        .filter(|c| doc.kind(*c) == Kind::Object)
        .collect();
    let index = |column: usize| match get(doc, column, "index")
        .filter(|i| doc.kind(*i) == Kind::Number)
        .map(|i| doc.number(i))
    {
        Some(Number::Int(n)) => n,
        Some(Number::UInt(n)) => i64::try_from(n).unwrap_or(i64::MAX),
        Some(Number::Float(n)) => n as i64,
        None => 0,
    };
    listed.sort_by_key(|column| index(*column));
    listed
        .into_iter()
        .filter_map(|column| {
            let name = string(doc, get(doc, column, "name").filter(|n| truthy(doc, *n))?)?;
            Some((
                name,
                get(doc, column, "type"),
                description(doc, column, "comment"),
            ))
        })
        .collect()
}

/// Adds a node's columns; whether the catalog lists them, rather than only its documentation.
fn add_columns<'py>(
    graph: &Bound<'py, Graph>,
    parent: &Bound<'py, Node>,
    doc: Doc<'_>,
    item: usize,
    catalog: Option<&CatalogView<'_>>,
) -> PyResult<bool> {
    let catalog_has_content = catalog.is_some_and(|c| c.content);
    let py = graph.py();
    let keys = primary_key(py, doc, item)?;
    if !keys.is_empty() {
        let attrs = parent.borrow().attrs.bind(py).clone();
        attrs.set_item("primary_key", PyList::new(py, &keys)?)?;
        // A dbt constraint reaches the warehouse only under an enforced contract.
        let enforced = present(doc, item, "config")
            .and_then(|config| get(doc, config, "contract"))
            .filter(|contract| truthy(doc, *contract))
            .and_then(|contract| get(doc, contract, "enforced"))
            .is_some_and(|flag| truthy(doc, flag));
        if enforced {
            attrs.set_item("primary_key_enforced", true)?;
        }
    }
    // The documented columns by lower-case name, the first of a name keeping its place.
    let mut documented: Vec<(String, usize)> = Vec::new();
    let mut place: HashMap<String, usize> = HashMap::new();
    if let Some(columns) = present(doc, item, "columns").filter(|c| doc.kind(*c) == Kind::Object) {
        for (name, column) in unique_members(doc, columns) {
            if doc.kind(column) != Kind::Object {
                continue;
            }
            let lower = doc.text(name).to_lowercase();
            match place.entry(lower.clone()) {
                Entry::Occupied(slot) => documented[*slot.get()].1 = column,
                Entry::Vacant(slot) => {
                    slot.insert(documented.len());
                    documented.push((lower, column));
                }
            }
        }
    }
    let unique_id = text(doc, item, "unique_id").unwrap_or_default();
    let mut seen: std::collections::HashSet<String> = std::collections::HashSet::new();
    for (name, data_type, comment) in catalog_columns(catalog, &unique_id) {
        let lower = name.to_lowercase();
        let doc_column = place.get(&lower).map(|at| documented[*at].1);
        // The type is a value of the catalog; the documentation's is the manifest's.
        let from_catalog =
            catalog.and_then(|c| data_type.filter(|t| truthy(c.doc, *t)).map(|t| (c.doc, t)));
        let data_type = match from_catalog {
            Some((c, found)) => Some(to_py(py, c, found)?),
            None => match doc_column.and_then(|c| get(doc, c, "data_type")) {
                Some(found) => Some(to_py(py, doc, found)?),
                None => None,
            },
        };
        let described = doc_column
            .and_then(|c| description(doc, c, "description"))
            .or(comment);
        Graph::add_column(
            graph,
            parent,
            &PyString::new(py, &name),
            data_type.map(Bound::unbind),
            described.map(|t| PyString::new(py, &t).into_any().unbind()),
            Some(PyString::new(py, "dbt").into_any().unbind()),
            None,
        )?;
        seen.insert(lower);
    }
    for (lower, column) in &documented {
        if seen.contains(lower) {
            continue;
        }
        let name = match get(doc, *column, "name") {
            Some(own) => to_py(py, doc, own)?,
            None => PyString::new(py, lower).into_any(),
        };
        let name = name.cast_into::<PyString>()?;
        let data_type = get(doc, *column, "data_type")
            .map(|t| to_py(py, doc, t))
            .transpose()?;
        let attrs = PyDict::new(py);
        if catalog_has_content {
            attrs.set_item("documented_only", true)?;
        }
        Graph::add_column(
            graph,
            parent,
            &name,
            data_type.map(Bound::unbind),
            description(doc, *column, "description")
                .map(|t| PyString::new(py, &t).into_any().unbind()),
            Some(PyString::new(py, "dbt").into_any().unbind()),
            Some(attrs.into_any()),
        )?;
    }
    Ok(!seen.is_empty())
}

/// The attributes of a model, seed or snapshot.
fn relation_attrs<'py>(
    py: Python<'py>,
    doc: Doc<'_>,
    item: usize,
    config: Option<usize>,
) -> PyResult<Bound<'py, PyDict>> {
    let attrs = PyDict::new(py);
    attrs.set_item("package", member(py, doc, item, "package_name")?)?;
    attrs.set_item(
        "fqn",
        member_or(py, doc, item, "fqn", || PyList::empty(py).into_any())?,
    )?;
    let setting = |key: &str| config.and_then(|c| get(doc, c, key));
    attrs.set_item(
        "materialized",
        setting("materialized")
            .map(|v| to_py(py, doc, v))
            .transpose()?,
    )?;
    attrs.set_item(
        "tags",
        member_or(py, doc, item, "tags", || PyList::empty(py).into_any())?,
    )?;
    attrs.set_item("dbt_name", member(py, doc, item, "name")?)?;
    let checksum = present(doc, item, "checksum").and_then(|c| get(doc, c, "checksum"));
    attrs.set_item("checksum", checksum.map(|v| to_py(py, doc, v)).transpose()?)?;
    // An incremental model's key and strategy, which decide what a run keeps of earlier rows.
    // A seed's delimiter and column types, which say how its CSV file is read.
    if setting("materialized").is_some_and(|m| doc.is(m, "seed")) {
        for key in ["delimiter", "column_types"] {
            if let Some(value) = setting(key).filter(|v| truthy(doc, *v)) {
                attrs.set_item(key, to_py(py, doc, value)?)?;
            }
        }
    }
    if setting("materialized").is_some_and(|m| doc.is(m, "incremental")) {
        for key in ["unique_key", "incremental_strategy"] {
            if let Some(value) = setting(key).filter(|v| truthy(doc, *v)) {
                if key == "unique_key" && doc.kind(value) == Kind::Array {
                    let keys = doc
                        .items(value)
                        .map(|k| str_of(py, doc, k))
                        .collect::<PyResult<Vec<_>>>()?;
                    attrs.set_item(key, PyList::new(py, keys)?)?;
                } else {
                    attrs.set_item(key, to_py(py, doc, value)?)?;
                }
            }
        }
    }
    Ok(attrs)
}

/// A test node, and the edges that say what it tests.
fn add_test<'py>(
    edit: &Edit<'_, 'py>,
    doc: Doc<'_>,
    item: usize,
    ctx: &Context<'_>,
) -> PyResult<()> {
    let py = edit.graph.py();
    let meta = present(doc, item, "test_metadata");
    let config = present(doc, item, "config");
    let kwargs = meta.and_then(|m| present(doc, m, "kwargs"));
    let attrs = PyDict::new(py);
    let test = meta
        .and_then(|m| get(doc, m, "name"))
        .filter(|n| truthy(doc, *n));
    attrs.set_item(
        "test",
        match test {
            Some(name) => to_py(py, doc, name)?,
            None => PyString::new(py, "singular").into_any(),
        },
    )?;
    for (key, value) in [
        ("severity", config.and_then(|c| get(doc, c, "severity"))),
        ("column", get(doc, item, "column_name")),
        ("where", config.and_then(|c| get(doc, c, "where"))),
        (
            "columns",
            kwargs.and_then(|k| get(doc, k, "combination_of_columns")),
        ),
    ] {
        if key == "column" {
            // A threshold other than dbt's own lets some rows fail the test.
            let limit = config.and_then(|c| present(doc, c, "error_if"));
            if let Some(limit) = limit {
                let text = str_of(py, doc, limit)?;
                let text = text.trim();
                if !text.is_empty()
                    && !matches!(text.replace(' ', "").as_str(), "!=0" | ">0" | ">=1")
                {
                    attrs.set_item("error_if", text)?;
                }
            }
        }
        if let Some(value) = value.filter(|v| truthy(doc, *v)) {
            attrs.set_item(key, to_py(py, doc, value)?)?;
        }
    }
    let created = node(
        py,
        node_id(py, doc, item)?,
        "test",
        member(py, doc, item, "name")?,
        None,
        description(doc, item, "description"),
        ctx.path(doc, item),
        attrs,
    )?;
    let created = edit.node(&created)?;
    let id = created.borrow().id.bind(py).clone();
    let targets: Vec<String> = match get(doc, item, "depends_on").and_then(|d| get(doc, d, "nodes"))
    {
        Some(nodes) => doc
            .items(nodes)
            .filter_map(|t| string(doc, t).map(Cow::into_owned))
            .collect(),
        None => Vec::new(),
    };
    let attached: Option<String> = match nonempty(doc, item, "attached_node") {
        Some(found) => Some(found.into_owned()),
        None if targets.len() == 1 => Some(targets[0].clone()),
        None => None,
    };
    let column = nonempty(doc, item, "column_name").map(Cow::into_owned);
    let find = |parent: &str, name: &str| -> PyResult<bool> {
        let found = edit.graph.get().column_node(
            py,
            PyString::new(py, parent).as_any(),
            PyString::new(py, name).as_any(),
        )?;
        Ok(found.is_some())
    };
    if let Some(attached) = attached.as_deref().filter(|a| !a.is_empty()) {
        match column.as_deref() {
            Some(column) if find(attached, column)? => {
                edit.edge(&id, &column_id(py, attached, column), "tests", None)?;
            }
            _ => edit.edge(&id, PyString::new(py, attached).as_any(), "tests", None)?,
        }
    }
    let relationship =
        meta.is_some_and(|m| text(doc, m, "name").as_deref() == Some("relationships"));
    let field = kwargs.and_then(|k| nonempty(doc, k, "field"));
    for target in &targets {
        if Some(target.as_str()) == attached.as_deref() {
            continue;
        }
        match field.as_deref() {
            Some(field) if relationship && find(target, field)? => {
                let role = PyDict::new(py);
                role.set_item("role", "relationship target")?;
                edit.edge(&id, &column_id(py, target, field), "tests", Some(&role))?;
            }
            _ => edit.edge(&id, PyString::new(py, target).as_any(), "tests", None)?,
        }
    }
    Ok(())
}

#[pymethods]
impl Artefact {
    /// The adapter the manifest was made for.
    fn adapter_type(&self) -> Option<String> {
        let doc = self.doc();
        let metadata = present(doc, 0, "metadata")?;
        nonempty(doc, metadata, "adapter_type").map(Cow::into_owned)
    }

    /// The project's name as the manifest's metadata gives it.
    fn project_name(&self) -> Option<String> {
        let doc = self.doc();
        let metadata = present(doc, 0, "metadata")?;
        nonempty(doc, metadata, "project_name").map(Cow::into_owned)
    }

    /// The packages the manifest's macros come from.
    fn macro_packages(&self) -> Vec<String> {
        let doc = self.doc();
        let Some(macros) = get(doc, 0, "macros").filter(|m| doc.kind(*m) == Kind::Object) else {
            return Vec::new();
        };
        doc.members(macros)
            .filter(|(_, m)| doc.kind(*m) == Kind::Object)
            .map(|(_, m)| match get(doc, m, "package_name") {
                Some(name) => str_of_plain(doc, name),
                None => "None".to_string(),
            })
            .collect()
    }

    /// The project's overview text, stripped, when it has one.
    fn overview(&self, project: &str) -> Option<String> {
        let doc = self.doc();
        let block = get(doc, 0, "docs")
            .and_then(|d| get(doc, d, &format!("doc.{project}.__overview__")))?;
        description(doc, block, "block_contents")
    }

    /// The top-level value `name` as Python objects, or None when there is none.
    fn section<'py>(&self, py: Python<'py>, name: &str) -> PyResult<Option<Bound<'py, PyAny>>> {
        let doc = self.doc();
        get(doc, 0, name)
            .map(|value| to_py(py, doc, value))
            .transpose()
    }

    /// Adds the manifest's nodes, sources, exposures and tests to `graph`. Returns the models to
    /// analyse (each as node id, relation, SQL or None when its template did not render, whether
    /// dbt compiled it, and its upstream ids), the tables a plain reading of the uncompiled ones
    /// needs, the nodes the catalog lists the columns of, the counts of each kind, the most common
    /// database and schema, and the manifest's invocation id.
    #[pyo3(signature = (graph, catalog, project_name, root, graph_root, dialect, macro_paths, keep, variables=None))]
    #[allow(clippy::too_many_arguments)]
    fn load<'py>(
        &self,
        py: Python<'py>,
        graph: &Bound<'py, Graph>,
        catalog: Option<&Artefact>,
        project_name: &str,
        root: PathBuf,
        graph_root: PathBuf,
        dialect: Option<String>,
        macro_paths: Option<Vec<PathBuf>>,
        keep: Option<Bound<'py, PyAny>>,
        variables: Option<Bound<'py, PyDict>>,
    ) -> PyResult<Bound<'py, PyDict>> {
        guarded(|| {
            let _ = project_name;
            let doc = self.doc();
            let view = catalog.map(|c| CatalogView::new(c.doc()));
            let view = view.as_ref();
            let ctx = Context {
                root: &root,
                graph_root: &graph_root,
                dialect: dialect.as_deref(),
            };
            let edit = Edit {
                graph,
                exact: Graph::is_exact(graph),
                origin: {
                    let origin = PyDict::new(py);
                    origin.set_item("origin", "dbt")?;
                    origin
                },
            };
            let nodes = get(doc, 0, "nodes").filter(|n| doc.kind(*n) == Kind::Object);
            let members: Vec<usize> = nodes
                .map(|n| {
                    unique_members(doc, n)
                        .into_iter()
                        .map(|(_, v)| v)
                        .filter(|v| doc.kind(*v) == Kind::Object)
                        .collect()
                })
                .unwrap_or_default();

            let mut models: Vec<Model<'py>> = Vec::new();
            let mut renderer: Option<Renderer> = None;
            let mut catalogued: Vec<Bound<'py, PyAny>> = Vec::new();
            let mut schemas: Vec<(SchemaKey, usize)> = Vec::new();
            let mut unrendered: Vec<usize> = Vec::new();
            let mut raws: Vec<(usize, String)> = Vec::new();
            for &item in &members {
                let resource = text(doc, item, "resource_type").unwrap_or_default();
                let kind = match resource.as_ref() {
                    "model" => "model",
                    "seed" => "seed",
                    "snapshot" => "snapshot",
                    _ => continue,
                };
                let relation = ctx.relation(doc, item);
                let config = present(doc, item, "config");
                let name = match get(doc, item, "alias").filter(|a| truthy(doc, *a)) {
                    Some(alias) => to_py(py, doc, alias)?,
                    None => to_py(
                        py,
                        doc,
                        get(doc, item, "name").ok_or_else(|| PyKeyError::new_err("name"))?,
                    )?,
                };
                let id =
                    get(doc, item, "unique_id").ok_or_else(|| PyKeyError::new_err("unique_id"))?;
                if doc.kind(id) != Kind::String {
                    return Err(PyTypeError::new_err("a node's unique_id is not text"));
                }
                let created = node(
                    py,
                    to_py(py, doc, id)?,
                    kind,
                    name,
                    relation.clone(),
                    description(doc, item, "description"),
                    ctx.path(doc, item),
                    relation_attrs(py, doc, item, config)?,
                )?;
                let created = edit.node(&created)?;
                let node_id = created.borrow().id.bind(py).clone();
                if add_columns(graph, &created, doc, item, view)? {
                    catalogued.push(node_id.clone());
                }
                let upstream: Vec<String> =
                    match get(doc, item, "depends_on").and_then(|d| get(doc, d, "nodes")) {
                        Some(listed) => doc
                            .items(listed)
                            .filter_map(|n| string(doc, n))
                            .filter(|n| !n.starts_with("macro."))
                            .map(Cow::into_owned)
                            .collect(),
                        None => Vec::new(),
                    };
                for parent in &upstream {
                    edit.feeds(PyString::new(py, parent).as_any(), &node_id)?;
                }
                let key = (
                    nonempty_or_none(doc, item, "database"),
                    nonempty_or_none(doc, item, "schema"),
                );
                match schemas.iter_mut().find(|(known, _)| *known == key) {
                    Some(slot) => slot.1 += 1,
                    None => schemas.push((key, 1)),
                }
                let Some(relation) = relation.filter(|r| kind != "seed" && !r.is_empty()) else {
                    continue;
                };
                if let Some(compiled) = get(doc, item, "compiled_code").filter(|c| truthy(doc, *c))
                {
                    let sql = string(doc, compiled).map_or_else(String::new, Cow::into_owned);
                    models.push((node_id, relation, Some(sql), true, upstream));
                } else if let Some(raw) = nonempty(doc, item, "raw_code") {
                    let is_sql = get(doc, item, "language").is_none_or(|l| doc.is(l, "sql"));
                    if !is_sql {
                        continue;
                    }
                    if renderer.is_none() {
                        renderer = Some(self.renderer(
                            py,
                            &ctx,
                            &root,
                            macro_paths.as_deref(),
                            keep.as_ref(),
                            variables.as_ref(),
                        )?);
                    }
                    let rendered = renderer.as_ref().and_then(|r| r.render(&raw).ok());
                    if rendered.is_none() {
                        unrendered.push(models.len());
                        raws.push((models.len(), raw.into_owned()));
                    }
                    models.push((node_id, relation, rendered, false, upstream));
                }
            }

            let sources = get(doc, 0, "sources").filter(|s| doc.kind(*s) == Kind::Object);
            for item in sources
                .iter()
                .flat_map(|s| unique_members(doc, *s).into_iter().map(|(_, v)| v))
                .filter(|v| doc.kind(*v) == Kind::Object)
            {
                let attrs = PyDict::new(py);
                attrs.set_item("source_name", member(py, doc, item, "source_name")?)?;
                attrs.set_item("package", member(py, doc, item, "package_name")?)?;
                // What the project says lands the table (`loader:` in sources.yml).
                if let Some(loader) = get(doc, item, "loader").filter(|l| truthy(doc, *l)) {
                    attrs.set_item("declared_loader", to_py(py, doc, loader)?)?;
                }
                let name = match get(doc, item, "identifier").filter(|i| truthy(doc, *i)) {
                    Some(identifier) => to_py(py, doc, identifier)?,
                    None => to_py(
                        py,
                        doc,
                        get(doc, item, "name").ok_or_else(|| PyKeyError::new_err("name"))?,
                    )?,
                };
                let created = node(
                    py,
                    node_id(py, doc, item)?,
                    "source",
                    name,
                    ctx.relation(doc, item),
                    description(doc, item, "description")
                        .or_else(|| description(doc, item, "source_description")),
                    ctx.path(doc, item),
                    attrs,
                )?;
                let created = edit.node(&created)?;
                if add_columns(graph, &created, doc, item, view)? {
                    catalogued.push(created.borrow().id.bind(py).clone());
                }
            }

            let exposures = get(doc, 0, "exposures").filter(|s| doc.kind(*s) == Kind::Object);
            for item in exposures
                .iter()
                .flat_map(|s| unique_members(doc, *s).into_iter().map(|(_, v)| v))
                .filter(|v| doc.kind(*v) == Kind::Object)
            {
                let owner = present(doc, item, "owner");
                let attrs = PyDict::new(py);
                for (key, value) in [
                    ("type", get(doc, item, "type")),
                    ("label", get(doc, item, "label")),
                    ("maturity", get(doc, item, "maturity")),
                    ("owner", owner.and_then(|o| get(doc, o, "name"))),
                    ("url", get(doc, item, "url")),
                ] {
                    if let Some(value) = value.filter(|v| truthy(doc, *v)) {
                        attrs.set_item(key, to_py(py, doc, value)?)?;
                    }
                }
                let created = node(
                    py,
                    node_id(py, doc, item)?,
                    "exposure",
                    member(py, doc, item, "name")?,
                    None,
                    description(doc, item, "description"),
                    ctx.path(doc, item),
                    attrs,
                )?;
                let created = edit.node(&created)?;
                let id = created.borrow().id.bind(py).clone();
                if let Some(listed) =
                    get(doc, item, "depends_on").and_then(|d| get(doc, d, "nodes"))
                {
                    for parent in doc.items(listed) {
                        edit.feeds(&to_py(py, doc, parent)?, &id)?;
                    }
                }
            }

            for &item in &members {
                if text(doc, item, "resource_type").as_deref() == Some("test") {
                    add_test(&edit, doc, item, &ctx)?;
                }
            }

            let result = PyDict::new(py);
            let rows = PyList::empty(py);
            for (id, relation, sql, compiled, upstream) in models {
                rows.append((id, relation, sql, compiled, upstream))?;
            }
            result.set_item("models", rows)?;
            let failed = PyDict::new(py);
            for (at, raw) in raws {
                failed.set_item(at, raw)?;
            }
            result.set_item("unrendered", failed)?;
            if !unrendered.is_empty() {
                result.set_item("tables", self.tables(py, doc)?)?;
            }
            result.set_item("catalogued", PySet::new(py, catalogued)?)?;
            let best = schemas
                .iter()
                .enumerate()
                .fold(None::<usize>, |best, (at, (_, count))| match best {
                    Some(held) if schemas[held].1 >= *count => Some(held),
                    _ => Some(at),
                });
            let (catalog_name, schema_name) = best.map_or((None, None), |at| schemas[at].0.clone());
            result.set_item("default_catalog", catalog_name)?;
            result.set_item("default_schema", schema_name)?;
            result.set_item(
                "manifest_checksum",
                present(doc, 0, "metadata")
                    .and_then(|m| get(doc, m, "invocation_id"))
                    .map(|v| to_py(py, doc, v))
                    .transpose()?,
            )?;
            let counts = PyDict::new(py);
            for (kind, group) in [
                ("model", "nodes"),
                ("seed", "nodes"),
                ("snapshot", "nodes"),
                ("test", "nodes"),
                ("source", "sources"),
                ("exposure", "exposures"),
            ] {
                let count = get(doc, 0, group)
                    .filter(|g| doc.kind(*g) == Kind::Object)
                    .map_or(0, |g| {
                        doc.members(g)
                            .filter(|(_, v)| {
                                text(doc, *v, "resource_type").as_deref() == Some(kind)
                            })
                            .count()
                    });
                counts.set_item(kind, count)?;
            }
            result.set_item("counts", counts)?;
            Ok(result)
        })
    }
}

/// `str()` of a JSON scalar as Python writes it (a package name is text; null is `None`).
fn str_of_plain(doc: Doc<'_>, value: usize) -> String {
    match doc.kind(value) {
        Kind::String => doc.text(value).into_owned(),
        Kind::Null => "None".to_string(),
        Kind::True => "True".to_string(),
        Kind::False => "False".to_string(),
        Kind::Number => match doc.number(value) {
            Number::Int(n) => n.to_string(),
            Number::UInt(n) => n.to_string(),
            Number::Float(n) => crate::jinja::value::float_repr(n),
        },
        Kind::Array | Kind::Object => String::new(),
    }
}

fn nonempty_or_none(doc: Doc<'_>, item: usize, key: &str) -> Option<String> {
    get(doc, item, key)
        .and_then(|v| string(doc, v))
        .map(Cow::into_owned)
}

impl Artefact {
    /// The renderer of this manifest's project: its `ref` and `source` tables, and its macros.
    fn renderer(
        &self,
        py: Python<'_>,
        ctx: &Context<'_>,
        root: &Path,
        macro_paths: Option<&[PathBuf]>,
        keep: Option<&Bound<'_, PyAny>>,
        variables: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<Renderer> {
        let doc = self.doc();
        let metadata = present(doc, 0, "metadata");
        let from_metadata = |key: &str| {
            metadata
                .and_then(|m| nonempty(doc, m, key))
                .map(Cow::into_owned)
        };
        let adapter = from_metadata("adapter_type").unwrap_or_else(|| "default".to_string());
        let mut packages = Vec::new();
        let installed = root.join("dbt_packages");
        if installed.is_dir() {
            let mut found: Vec<PathBuf> = std::fs::read_dir(&installed)
                .map(|entries| {
                    entries
                        .flatten()
                        .map(|e| e.path())
                        .filter(|p| p.is_dir())
                        .collect()
                })
                .unwrap_or_default();
            found.sort();
            for package in found {
                let name = package
                    .file_name()
                    .map(|n| n.to_string_lossy().into_owned())
                    .unwrap_or_default();
                packages.push((name, macro_text(py, root, &[package.join("macros")], keep)?));
            }
        }
        let bases = macro_paths.map_or_else(|| vec![root.join("macros")], <[PathBuf]>::to_vec);
        let macros = Macros {
            packages,
            own: macro_text(py, root, &bases, keep)?,
            project_name: from_metadata("project_name"),
            variables: variables_of(variables)?,
        };
        let _ = ctx;
        Ok(Renderer::new(self.tables_of(doc), &adapter, macros))
    }

    /// The names of the manifest's relations, as the renderer looks them up.
    fn tables_of(&self, doc: Doc<'_>) -> Tables {
        let mut tables = Tables::default();
        for (_, item) in get(doc, 0, "nodes")
            .into_iter()
            .flat_map(|n| doc.members(n))
        {
            if !matches!(
                text(doc, item, "resource_type").as_deref(),
                Some("model" | "seed" | "snapshot")
            ) {
                continue;
            }
            let (Some(relation), Some(name)) = (
                nonempty(doc, item, "relation_name"),
                nonempty(doc, item, "name"),
            ) else {
                continue;
            };
            let package = text(doc, item, "package_name").unwrap_or_default();
            tables
                .relations
                .entry(name.to_string())
                .or_insert_with(|| relation.to_string());
            tables.by_package.insert(
                (package.into_owned(), name.into_owned()),
                relation.into_owned(),
            );
        }
        for (_, item) in get(doc, 0, "sources")
            .into_iter()
            .flat_map(|s| unique_members(doc, s))
        {
            if let (Some(relation), Some(group), Some(name)) = (
                nonempty(doc, item, "relation_name"),
                nonempty(doc, item, "source_name"),
                nonempty(doc, item, "name"),
            ) {
                tables.sources.insert(
                    (group.into_owned(), name.into_owned()),
                    relation.into_owned(),
                );
            }
        }
        tables
    }

    /// The tables for a plain reading of SQL that could not be rendered: relations by name, by
    /// package and name, and sources, as Python dicts.
    fn tables<'py>(&self, py: Python<'py>, doc: Doc<'_>) -> PyResult<Bound<'py, PyAny>> {
        let tables = self.tables_of(doc);
        let by_name = PyDict::new(py);
        for (name, relation) in &tables.relations {
            by_name.set_item(name, relation)?;
        }
        let by_package = PyDict::new(py);
        for ((package, name), relation) in &tables.by_package {
            by_package.set_item((package, name), relation)?;
        }
        let sources = PyDict::new(py);
        for ((group, name), relation) in &tables.sources {
            sources.set_item((group, name), relation)?;
        }
        Ok((by_name, by_package, sources).into_pyobject(py)?.into_any())
    }
}
