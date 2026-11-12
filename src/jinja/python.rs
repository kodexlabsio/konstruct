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
fn sql_files(base: &Path, root: &Path) -> Vec<PathBuf> {
    fn walk(folder: &Path, root: &Path, found: &mut Vec<PathBuf>) {
        let Ok(entries) = std::fs::read_dir(folder) else {
            return;
        };
        for entry in entries.flatten() {
            let path = entry.path();
            // `file_type` does not follow a link, so a link to a folder is not walked, as the
            // build's own walk does not walk it: `a -> .` would be a loop and `r -> /` the disk.
            let Ok(kind) = entry.file_type() else {
                continue;
            };
            if kind.is_dir() {
                walk(&path, root, found);
            } else if path.extension().is_some_and(|ext| ext == "sql")
                && (kind.is_file() || kind.is_symlink())
                && regular_inside(&path, root)
            {
                found.push(path);
            }
        }
    }
    let mut found = Vec::new();
    walk(base, root, &mut found);
    found.sort();
    found
}

/// Whether `path` is a regular file that is in the project, once its links are followed: a macro
/// file that is a link out of the project, a pipe or a device (`/dev/zero`) is not read.
pub(crate) fn regular_inside(path: &Path, root: &Path) -> bool {
    let (Ok(real), Ok(root)) = (std::fs::canonicalize(path), std::fs::canonicalize(root)) else {
        return false;
    };
    real.starts_with(root) && std::fs::metadata(&real).is_ok_and(|meta| meta.is_file())
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

/// `work`, with a panic in it turned into the error the renderer gives for a template it cannot
/// render, so that nothing a template or a manifest says reaches Python as a panic. This is a guard
/// for what was missed, not a way to go on.
pub(crate) fn guarded<T>(work: impl FnOnce() -> PyResult<T>) -> PyResult<T> {
    std::panic::catch_unwind(std::panic::AssertUnwindSafe(work)).unwrap_or_else(|_| {
        Err(PyValueError::new_err(
            "an internal error stopped the render",
        ))
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

/// A file as Python's `read_text(errors='replace')` reads it: lossy UTF-8, universal newlines.
fn read_text(path: &Path) -> String {
    use std::io::Read;
    let mut bytes = Vec::new();
    if let Ok(file) = std::fs::File::open(path) {
        let _ = file.take(MAX_MACRO_FILE).read_to_end(&mut bytes);
    }
    String::from_utf8_lossy(&bytes)
        .replace("\r\n", "\n")
        .replace('\r', "\n")
}

/// The most of one macro file that is read.
const MAX_MACRO_FILE: u64 = 16 * 1024 * 1024;

/// The macros of the folders in `bases`, one text, as the files in sorted order joined by newlines.
pub(crate) fn macro_text(
    py: Python<'_>,
    root: &Path,
    bases: &[PathBuf],
    keep: Option<&Bound<'_, PyAny>>,
) -> PyResult<String> {
    let mut texts = Vec::new();
    for base in bases {
        // A folder outside the project (absolute, or reached by `..`) is not read.
        let inside = std::fs::canonicalize(base)
            .is_ok_and(|real| std::fs::canonicalize(root).is_ok_and(|r| real.starts_with(r)));
        if !inside || !base.is_dir() {
            continue;
        }
        let mut files = sql_files(base, root);
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
        texts.extend(
            files
                .iter()
                .filter(|file| regular_inside(file, root))
                .map(|file| read_text(file)),
        );
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
        guarded(|| {
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
                        macro_text(py, &project_root, &[package.join("macros")], keep.as_ref())?,
                    ));
                }
            }
            let bases = macro_paths.unwrap_or_else(|| vec![project_root.join("macros")]);
            let macros = Macros {
                packages,
                own: macro_text(py, &project_root, &bases, keep.as_ref())?,
                project_name: from_metadata("project_name"),
                variables: variables_of(variables.as_ref())?,
            };
            let engine = Engine::new(tables(manifest)?, &adapter_type, macros);
            Ok(Renderer {
                engine,
                adapter_type,
            })
        })
    }

    /// The SQL of a model with its Jinja rendered. Raises ``ValueError`` for a template that
    /// does not parse or fails as it runs.
    fn render(&self, sql: &str) -> PyResult<String> {
        guarded(|| {
            self.engine
                .render(sql)
                .map_err(|error| PyValueError::new_err(error.to_string()))
        })
    }
}
