//! The conformance check as `konstruct._core.conform_*`; `konstruct.analysis.conflicts` is its
//! caller. Plain data crosses the boundary as text (JSON), so no sqlglot tree and no graph
//! object does.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

use super::{StatementKind, ddl, judge, norm, statements, yaml};
use crate::identity::python::FormulaSet;

fn value(text: &str) -> PyResult<serde_json::Value> {
    serde_json::from_str(text).map_err(|e| PyValueError::new_err(format!("not JSON: {e}")))
}

/// The statements of SQL text that define a platform's semantic layer, each as its kind
/// (`snowflake-view` or `databricks-metric-view`), the view's name and its text, or for a metric
/// view its YAML.
#[pyfunction]
pub fn conform_statements(text: &str) -> Vec<(&'static str, String, String)> {
    statements(text)
        .into_iter()
        .map(|s| {
            let kind = match s.kind {
                StatementKind::SnowflakeView => "snowflake-view",
                StatementKind::DatabricksMetricView => "databricks-metric-view",
            };
            (kind, s.name, s.body)
        })
        .collect()
}

/// A `CREATE SEMANTIC VIEW` statement as a document, in JSON.
#[pyfunction]
pub fn conform_read_ddl(text: &str) -> PyResult<String> {
    ddl::read(text)
        .map(|doc| doc.to_json().to_string())
        .map_err(PyValueError::new_err)
}

/// A platform's YAML, as JSON, as a document: `kind` is `snowflake-yaml` or `databricks-yaml`,
/// and `name` the metric view's name, which Databricks' YAML does not hold.
#[pyfunction]
pub fn conform_read_yaml(kind: &str, document: &str, name: &str) -> PyResult<String> {
    let document = value(document)?;
    let doc = match kind {
        "snowflake-yaml" => yaml::snowflake(&document),
        "databricks-yaml" => yaml::databricks(&document, name),
        _ => Err(format!("no reader for {kind}")),
    };
    doc.map(|doc| doc.to_json().to_string())
        .map_err(PyValueError::new_err)
}

/// A name as concepts are matched by it, the one normaliser (see [`norm`]).
#[pyfunction]
#[pyo3(name = "conform_norm")]
pub fn conform_norm(name: &str) -> String {
    norm(name)
}

/// Judges resolved claims and facts (JSON) against the formulas in `formulas`; returns the
/// findings as JSON. `held` is called with a list of column ids and returns the `(key, text)` of
/// each condition that holds on the rows of all of them.
#[pyfunction]
pub fn conform_judge(
    request: &str,
    mut formulas: PyRefMut<'_, FormulaSet>,
    held: &Bound<'_, PyAny>,
) -> PyResult<String> {
    let request = value(request)?;
    let known = formulas.formulas().len();
    unknown_formula(&request, known)?;
    let failure = std::cell::RefCell::new(None);
    let answer = judge(
        formulas.formulas(),
        &request,
        &mut |columns: &[String]| match held
            .call1((columns.to_vec(),))
            .and_then(|found| found.extract::<Vec<(String, String)>>())
        {
            Ok(found) => found,
            Err(error) => {
                failure.borrow_mut().get_or_insert(error);
                Vec::new()
            }
        },
    );
    match failure.into_inner() {
        Some(error) => Err(error),
        None => Ok(answer.to_string()),
    }
}

/// Refuses a request that names a formula the set does not hold, which would be a panic.
fn unknown_formula(value: &serde_json::Value, known: usize) -> PyResult<()> {
    match value {
        serde_json::Value::Object(map) => {
            for (key, inner) in map {
                let missing = (key == "formula")
                    .then(|| inner.as_u64())
                    .flatten()
                    .filter(|&n| usize::try_from(n).is_ok_and(|n| n >= known));
                if let Some(n) = missing {
                    return Err(PyValueError::new_err(format!("no formula {n}")));
                }
                unknown_formula(inner, known)?;
            }
            Ok(())
        }
        serde_json::Value::Array(items) => items.iter().try_for_each(|i| unknown_formula(i, known)),
        _ => Ok(()),
    }
}
