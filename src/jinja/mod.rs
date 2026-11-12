//! Rendering of dbt model SQL without dbt: Jinja2's language with dbt's `ref`, `source`, `var`,
//! `config`, the macros of the project and of its installed packages, and `adapter.dispatch`.
//!
//! Konstruct uses the SQL dbt compiled when it has it; this renderer is for SQL dbt has not
//! compiled (the other side of a `konstruct diff`, or a project whose artefacts are missing). It
//! is a tree-walking interpreter over a Python-like value model, because dbt macros lean on what
//! Python does: lists and dicts that `{% do l.append(x) %}` changes in place, `True` and `None`
//! printed as Python prints them, and names that are not defined but can be called. It has no
//! access to a file system, a network, a database or the process's environment: `run_query`,
//! `statement` and `env_var` stand in for what would reach them, and a name that starts with an
//! underscore is not an attribute.

mod builtins;
pub mod dbt;
pub mod eval;
mod parse;
#[cfg(feature = "python")]
pub mod python;
pub mod value;

pub use dbt::{Macros, Renderer, Tables};
pub use eval::Error;

#[cfg(test)]
mod tests;
