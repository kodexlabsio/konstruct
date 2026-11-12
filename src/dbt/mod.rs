//! dbt's manifest and catalog: read as JSON, and the nodes, columns and edges of a project put in
//! the graph, without a Python dict for any of its models.
//!
//! The model SQL that dbt has not compiled is rendered by the Jinja renderer of this crate
//! (`crate::jinja`), which takes its `ref` and `source` tables from the manifest read here.

#[cfg(feature = "python")]
pub mod python;
pub mod relation;
