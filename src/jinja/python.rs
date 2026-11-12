//! `konstruct.inputs.jinja.Renderer`: the renderer for the Python surface. It takes a project's
//! manifest as the dict Python read, finds the macro files itself, and raises `ValueError` for a
//! template that does not render, which the build answers by falling back to a plainer reading.

use std::path::{Path, PathBuf};

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

use super::dbt::{Macros, Renderer as Engine, Tables};
use super::value::Value;

/// The `.sql` files under `base`, in the order `sorted(base.rglob('*.sql'))` has them.
fn sql_files(base: &Path) -> Vec<PathBuf> {
    fn walk(folder: &Path, found: &mut Vec<PathBuf>) {
        let Ok(entries) = std::fs::read_dir(folder) else {
            return;
        };
        for entry in entries.flatten() {
            let path = entry.path();
            if path.is_dir() {
                walk(&path, found);
            } else if path.extension().is_some_and(|ext| ext == "sql") {
                found.push(path);
            }
        }
    }
    let mut found = Vec::new();
    walk(base, &mut found);
    found.sort();
    found
}

/// A file as Python's `read_text(errors='replace')` reads it: lossy UTF-8, universal newlines.
fn read_text(path: &Path) -> String {
    let bytes = std::fs::read(path).unwrap_or_default();
    String::from_utf8_lossy(&bytes)
        .replace("\r\n", "\n")
        .replace('\r', "\n")
}

/// The macros of the folders in `bases`, one text, as the files in sorted order joined by newlines.
fn macro_text(
    py: Python<'_>,
    bases: &[PathBuf],
    keep: Option<&Bound<'_, PyAny>>,
) -> PyResult<String> {
    let mut texts = Vec::new();
    for base in bases {
        if !base.is_dir() {
            continue;
        }
        let mut files = sql_files(base);
        if let Some(keep) = keep {
            let path_class = py.import("pathlib")?.getattr("Path")?;
            let given = PyList::new(
                py,
                files
                    .iter()
                    .map(|f| path_class.call1((f,)))
                    .collect::<PyResult<Vec<_>>>()?,
            )?;
            files = keep
                .call1((given,))?
                .try_iter()?
                .map(|item| item?.str()?.extract::<String>().map(PathBuf::from))
                .collect::<PyResult<Vec<_>>>()?;
        }
        texts.extend(files.iter().map(|file| read_text(file)));
    }
    Ok(texts.join("\n"))
}

fn text_of(item: &Bound<'_, PyAny>, key: &str) -> Option<String> {
    item.get_item(key)
        .ok()?
        .extract::<String>()
        .ok()
        .filter(|text| !text.is_empty())
}

fn tables(manifest: &Bound<'_, PyDict>) -> PyResult<Tables> {
    let mut tables = Tables::default();
    if let Some(nodes) = manifest
        .get_item("nodes")?
        .and_then(|n| n.cast_into::<PyDict>().ok())
    {
        for (_, node) in nodes.iter() {
            let kind = text_of(&node, "resource_type").unwrap_or_default();
            if !matches!(kind.as_str(), "model" | "seed" | "snapshot") {
                continue;
            }
            let (Some(relation), Some(name)) =
                (text_of(&node, "relation_name"), text_of(&node, "name"))
            else {
                continue;
            };
            let package = node
                .get_item("package_name")
                .ok()
                .and_then(|p| p.extract::<String>().ok())
                .unwrap_or_default();
            tables
                .relations
                .entry(name.clone())
                .or_insert_with(|| relation.clone());
            tables.by_package.insert((package, name), relation);
        }
    }
    if let Some(sources) = manifest
        .get_item("sources")?
        .and_then(|n| n.cast_into::<PyDict>().ok())
    {
        for (_, source) in sources.iter() {
            if let (Some(relation), Some(group), Some(name)) = (
                text_of(&source, "relation_name"),
                text_of(&source, "source_name"),
                text_of(&source, "name"),
            ) {
                tables.sources.insert((group, name), relation);
            }
        }
    }
    Ok(tables)
}

/// A Python value as a template sees it: `None`, numbers, text, lists and dicts as themselves, and
/// anything else (a date from YAML) as its text.
pub(crate) fn to_value(item: &Bound<'_, PyAny>) -> PyResult<Value> {
    to_value_at(item, 0)
}

fn to_value_at(item: &Bound<'_, PyAny>, depth: usize) -> PyResult<Value> {
    if depth > 100 {
        return Err(PyValueError::new_err("a value of vars nests too deeply"));
    }
    use pyo3::types::{PyBool, PyFloat, PyInt, PyString, PyTuple};
    Ok(if item.is_none() {
        Value::None
    } else if let Ok(flag) = item.cast::<PyBool>() {
        Value::Bool(flag.is_true())
    } else if item.is_instance_of::<PyInt>() {
        item.extract::<i128>()
            .map_or_else(|_| Value::from(item.to_string()), Value::Int)
    } else if item.is_instance_of::<PyFloat>() {
        Value::Float(item.extract()?)
    } else if let Ok(text) = item.cast::<PyString>() {
        Value::from(text.to_string())
    } else if let Ok(list) = item.cast::<PyList>() {
        Value::list(
            list.iter()
                .map(|i| to_value_at(&i, depth + 1))
                .collect::<PyResult<_>>()?,
        )
    } else if let Ok(tuple) = item.cast::<PyTuple>() {
        Value::tuple(
            tuple
                .iter()
                .map(|i| to_value_at(&i, depth + 1))
                .collect::<PyResult<_>>()?,
        )
    } else if let Ok(dict) = item.cast::<PyDict>() {
        let mut entries = Vec::new();
        for (key, value) in dict.iter() {
            entries.push((
                to_value_at(&key, depth + 1)?,
                to_value_at(&value, depth + 1)?,
            ));
        }
        Value::dict(entries)
    } else {
        Value::from(item.str()?.to_string())
    })
}

/// The project's `vars`, by name.
pub(crate) fn variables_of(found: Option<&Bound<'_, PyDict>>) -> PyResult<Vec<(String, Value)>> {
    let Some(found) = found else {
        return Ok(Vec::new());
    };
    found
        .iter()
        .map(|(name, value)| Ok((name.str()?.to_string(), to_value(&value)?)))
        .collect()
}

/// Renders models of one dbt project, with its macros and its installed packages' macros.
#[pyclass(module = "konstruct.inputs.jinja", unsendable)]
pub struct Renderer {
    engine: Engine,
    #[pyo3(get)]
    adapter_type: String,
}

#[pymethods]
impl Renderer {
    #[new]
    #[pyo3(signature = (project_root, manifest, *, adapter_type=None, macro_paths=None, keep=None, variables=None))]
    fn new(
        py: Python<'_>,
        project_root: PathBuf,
        manifest: &Bound<'_, PyDict>,
        adapter_type: Option<String>,
        macro_paths: Option<Vec<PathBuf>>,
        keep: Option<Bound<'_, PyAny>>,
        variables: Option<Bound<'_, PyDict>>,
    ) -> PyResult<Self> {
        let metadata = manifest
            .get_item("metadata")?
            .and_then(|m| m.cast_into::<PyDict>().ok());
        let from_metadata = |key: &str| {
            metadata
                .as_ref()
                .and_then(|m| m.get_item(key).ok().flatten())
                .and_then(|value| value.extract::<String>().ok())
                .filter(|text| !text.is_empty())
        };
        let adapter_type = adapter_type
            .filter(|text| !text.is_empty())
            .or_else(|| from_metadata("adapter_type"))
            .unwrap_or_else(|| "default".to_string());
        let mut packages = Vec::new();
        let packages_dir = project_root.join("dbt_packages");
        if packages_dir.is_dir() {
            let mut found: Vec<PathBuf> = std::fs::read_dir(&packages_dir)
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
                packages.push((
                    name,
                    macro_text(py, &[package.join("macros")], keep.as_ref())?,
                ));
            }
        }
        let bases = macro_paths.unwrap_or_else(|| vec![project_root.join("macros")]);
        let macros = Macros {
            packages,
            own: macro_text(py, &bases, keep.as_ref())?,
            project_name: from_metadata("project_name"),
            variables: variables_of(variables.as_ref())?,
        };
        let engine = Engine::new(tables(manifest)?, &adapter_type, macros);
        Ok(Renderer {
            engine,
            adapter_type,
        })
    }

    /// The SQL of a model with its Jinja rendered. Raises ``ValueError`` for a template that
    /// does not parse or fails as it runs.
    fn render(&self, sql: &str) -> PyResult<String> {
        self.engine
            .render(sql)
            .map_err(|error| PyValueError::new_err(error.to_string()))
    }
}
