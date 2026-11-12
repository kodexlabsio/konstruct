//! ODCS 3.2 data contracts: written from the ontology's facts, and the claims of a hand-written
//! contract tested against them. `konstruct.outputs.odcs` and `konstruct.analysis.contract` are
//! the Python callers.
//!
//! - [`facts`]: what the contracts are made from.
//! - [`contract`]: the contract model, its fields named as the ODCS 3.2 JSON Schema names them.
//! - [`yaml`]: writing the YAML, from a small value tree of our own. Reading is the YAML loader's
//!   (`konstruct.core.yamlio`), so that one reader limits what a document may expand to.
//! - [`export`]: facts to contracts, a verified statement only where evidence is held.
//! - [`check`]: a contract's claims against the facts.

pub mod check;
pub mod contract;
pub mod export;
pub mod facts;
#[cfg(feature = "python")]
pub(crate) mod python;
pub mod yaml;

use serde_json::{Value, json};

use facts::Facts;

/// The contracts of `facts`, as a YAML stream.
pub fn export_contracts(facts: &str) -> Result<String, String> {
    export::export(&Facts::parse(facts)?)
}

/// The findings of checking the contracts in `contracts` (a JSON array of the parsed YAML
/// documents) against `facts`, as JSON.
pub fn check_contracts(facts: &str, contracts: &str) -> Result<String, String> {
    let documents: Value =
        serde_json::from_str(contracts).map_err(|e| format!("contracts: {e}"))?;
    let Value::Array(documents) = documents else {
        return Err("contracts: expected a list of documents".into());
    };
    let report = check::check(&Facts::parse(facts)?, &documents)?;
    let findings: Vec<Value> = report
        .findings
        .iter()
        .map(|f| {
            json!({
                "severity": f.severity,
                "kind": f.kind,
                "contract": f.contract,
                "where": f.place,
                "message": f.message,
            })
        })
        .collect();
    Ok(json!({
        "contracts": report.contracts,
        "claims": report.claims,
        "findings": findings,
        "not_run": report.not_run,
        "notes": report.notes,
    })
    .to_string())
}
