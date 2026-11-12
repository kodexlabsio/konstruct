//! `konstruct._core.odcs_export` and `odcs_check`.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

/// The contracts of the facts (a JSON document), as a YAML stream. Raises ValueError when the
/// facts are malformed or hold nothing to export.
#[pyfunction]
pub(crate) fn odcs_export(py: Python<'_>, facts: &str) -> PyResult<String> {
    py.detach(|| super::export_contracts(facts))
        .map_err(PyValueError::new_err)
}

/// The findings of checking the contracts (a JSON array of parsed documents) against the facts,
/// as JSON. Raises ValueError when either is not JSON or a document is not a data contract.
#[pyfunction]
pub(crate) fn odcs_check(py: Python<'_>, facts: &str, contracts: &str) -> PyResult<String> {
    py.detach(|| super::check_contracts(facts, contracts))
        .map_err(PyValueError::new_err)
}
