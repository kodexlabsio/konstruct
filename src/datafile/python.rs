//! `konstruct._core.data_file_columns`: the columns of a data file a project ships.

use std::path::PathBuf;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

use super::{Column, Format, Tracked};

/// The files a repository tracks, made once for a build: `TrackedFiles(names)` takes their paths
/// from its root, with `/` between the parts. A name that is not a plain relative path, or that a
/// file system reads as `.git`, is not kept.
#[pyclass(frozen, name = "TrackedFiles")]
pub struct TrackedFiles(Tracked);

#[pymethods]
impl TrackedFiles {
    #[new]
    fn new(names: Vec<String>) -> Self {
        Self(Tracked::new(names))
    }

    fn __len__(&self) -> usize {
        self.0.len()
    }

    /// The tracked files `pattern` matches, in name order.
    fn matching(&self, pattern: &str) -> Vec<String> {
        self.0
            .matching(pattern)
            .into_iter()
            .map(str::to_owned)
            .collect()
    }

    /// The first tracked file `pattern` matches that has an extension of `format` (`parquet`, or
    /// `csv` for `.csv` and `.tsv`); None when there is none.
    fn first(&self, pattern: &str, format: &str) -> PyResult<Option<String>> {
        let extensions = super::extensions_named(format)
            .ok_or_else(|| PyValueError::new_err(format!("unknown data file format {format:?}")))?;
        Ok(self.0.first(pattern, extensions).map(str::to_owned))
    }
}

/// The first file `pattern` (a path from `root`, with shell patterns and `**` in its parts)
/// matches among the `tracked` files that has the extension of `format` (`parquet`: `.parquet`;
/// `csv`: `.csv` and `.tsv`), as a path from `root` with `/` between its parts, and its columns,
/// each a name and its DuckDB type (empty where the file does not say). None when there is none,
/// or it is not a regular file under real directories, or it cannot be read as `format`. A CSV
/// file is read with `delimiter`, by default the one its first record suggests, and with its
/// first record as names unless `header` is false.
#[pyfunction]
#[pyo3(signature = (root, pattern, format, tracked, *, delimiter=None, header=true))]
pub fn data_file_columns(
    py: Python<'_>,
    root: PathBuf,
    pattern: &str,
    format: &str,
    tracked: Py<TrackedFiles>,
    delimiter: Option<&str>,
    header: bool,
) -> PyResult<Option<(String, Vec<Column>)>> {
    let format = match format {
        "parquet" => Format::Parquet,
        "csv" => {
            let delimiter = match delimiter.map(str::as_bytes) {
                None => None,
                Some([byte]) => Some(*byte),
                // DuckDB's names for a tab, and anything longer it reads as its first byte.
                Some(b"\\t") => Some(b'\t'),
                Some(other) => other.first().copied(),
            };
            Format::Csv { delimiter, header }
        }
        other => {
            return Err(PyValueError::new_err(format!(
                "unknown data file format {other:?}"
            )));
        }
    };
    Ok(py
        .detach(|| super::columns(&root, &tracked.get().0, pattern, &format))
        .map(|(path, columns)| {
            let path = path
                .components()
                .map(|c| c.as_os_str().to_string_lossy().into_owned())
                .collect::<Vec<_>>()
                .join("/");
            (path, columns)
        }))
}
