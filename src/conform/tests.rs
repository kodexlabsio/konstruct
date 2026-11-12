//! Each reader on statements and YAML written by the platforms' own documentation, and each
//! verdict.

use proptest::prelude::Strategy as _;
use serde_json::json;

use super::*;

/// The shape of the example in Snowflake's `CREATE SEMANTIC VIEW` reference: a table with a
/// key and synonyms, a relationship, a fact, a dimension, a metric over a fact and a derived
/// metric, a comment and a verified query.
const SNOWFLAKE_REFERENCE: &str = r#"
-- A semantic view
CREATE OR REPLACE SEMANTIC VIEW tpch_rev_analysis
  TABLES (
    orders AS SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.ORDERS
      PRIMARY KEY (o_orderkey)
      WITH SYNONYMS = ('sales orders', 'it''s')
      COMMENT = 'All orders',
    customers AS SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.CUSTOMER PRIMARY KEY (c_custkey)
  )
  RELATIONSHIPS (
    orders_to_customers AS orders (o_custkey) REFERENCES customers,
    orders (o_x, o_y) REFERENCES customers (c_x, c_y)
  )
  FACTS (
    orders.order_amount AS o_totalprice COMMENT = 'The price; a, b',
    PRIVATE orders.cents AS o_totalprice * 100
  )
  DIMENSIONS (
    customers.customer_name AS c_name WITH SYNONYMS = ('name'),
    orders.order_date AS o_orderdate
  )
  METRICS (
    orders.total_revenue AS SUM(orders.order_amount) COMMENT = 'Revenue',
    orders.refunds AS SUM(CASE WHEN orders.o_status = 'R' THEN orders.order_amount END),
    average_order AS orders.total_revenue / COUNT(orders.o_orderkey)
  )
  COMMENT = 'Revenue analysis'
  AI_VERIFIED_QUERIES (
    revenue_by_name AS (QUESTION 'Revenue by customer' SQL 'SELECT 1; -- not a split' VERIFIED_AT 1700000000 VERIFIED_BY 'priya' ONBOARDING_QUESTION)
  );
"#;

#[test]
fn a_semantic_view_is_read_clause_by_clause() {
    let doc = ddl::read(SNOWFLAKE_REFERENCE).unwrap();
    assert_eq!(doc.name, "tpch_rev_analysis");
    assert_eq!(doc.description.as_deref(), Some("Revenue analysis"));
    let orders = &doc.tables[0];
    assert_eq!(orders.source, "SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.ORDERS");
    assert_eq!(orders.primary_key, ["o_orderkey"]);
    assert_eq!(orders.synonyms, ["sales orders", "it's"]);
    assert_eq!(orders.description.as_deref(), Some("All orders"));
    let amount = &orders.fields[0];
    assert_eq!(
        (amount.name.as_str(), amount.expr.as_str()),
        ("order_amount", "o_totalprice")
    );
    assert_eq!(amount.kind, FieldKind::Fact);
    assert_eq!(amount.description.as_deref(), Some("The price; a, b"));
    assert_eq!(orders.fields[1].expr, "o_totalprice * 100");
    assert_eq!(doc.tables[1].fields[0].kind, FieldKind::Dimension);
    assert_eq!(doc.relationships[0].from_columns, ["o_custkey"]);
    assert_eq!(doc.relationships[0].to, "customers");
    assert_eq!(doc.relationships[1].to_columns, ["c_x", "c_y"]);
    assert_eq!(doc.metrics.len(), 3);
    assert_eq!(doc.metrics[0].expr, "SUM(orders.order_amount)");
    assert_eq!(
        doc.metrics[1].expr,
        "SUM(CASE WHEN orders.o_status = 'R' THEN orders.order_amount END)"
    );
    assert_eq!(doc.metrics[2].table, None);
    let query = &doc.verified_queries[0];
    assert_eq!(query.sql.as_deref(), Some("SELECT 1; -- not a split"));
    assert_eq!(query.verified_at.as_deref(), Some("1700000000"));
}

#[test]
fn a_statement_that_is_not_a_semantic_view_is_refused_with_where() {
    let error = ddl::read("CREATE TABLE t (a int)").unwrap_err();
    assert!(error.contains("expected SEMANTIC"), "{error}");
    let error = ddl::read("CREATE SEMANTIC VIEW v TABLES (t AS x.y PRIMARY (a))").unwrap_err();
    assert!(error.contains("KEY"), "{error}");
    let error = ddl::read("CREATE SEMANTIC VIEW v TABLES (t AS x.y) FACTS (u.a AS b)").unwrap_err();
    assert!(error.contains("not in TABLES"), "{error}");
    assert!(ddl::read("CREATE SEMANTIC VIEW v TABLES (t AS 'x").is_err());
    assert!(
        ddl::read("CREATE SEMANTIC VIEW v WHATEVER (x)")
            .unwrap_err()
            .contains("WHATEVER")
    );
}

#[test]
fn quoted_names_and_the_exporters_own_form_are_read() {
    let doc = ddl::read(
        "CREATE OR REPLACE SEMANTIC VIEW \"My View\"\n  TABLES (\n    \"Order\" AS \"db\".\"s\".\"ORDER\"\n      PRIMARY KEY (\"id\")\n  )\n  DIMENSIONS (\n    \"Order\".\"id\" AS \"Order\".\"id\"\n  )\n  COMMENT = 'x';",
    )
    .unwrap();
    assert_eq!(doc.name, "My View");
    assert_eq!(doc.tables[0].fields[0].expr, "\"Order\".\"id\"");
    assert_eq!(doc.tables[0].source, "db.s.ORDER");
}

#[test]
fn snowflake_yaml_is_read_with_verified_queries() {
    let doc = yaml::snowflake(&json!({
        "name": "sales",
        "tables": [{
            "name": "orders",
            "base_table": {"database": "DB", "schema": "S", "table": "ORDERS"},
            "primary_key": {"columns": ["order_id"]},
            "dimensions": [{"name": "status", "expr": "status", "synonyms": ["state"]}],
            "time_dimensions": [{"name": "placed", "expr": "placed_at"}],
            "facts": [{"name": "amount", "expr": "amount_cents / 100"}],
            "metrics": [{"name": "revenue", "expr": "SUM(amount)"}]
        }],
        "metrics": [{"name": "avg", "expr": "revenue / 2"}],
        "relationships": [{
            "name": "o_c", "left_table": "orders", "right_table": "customers",
            "relationship_columns": [{"left_column": "customer_id", "right_column": "id"}],
            "relationship_type": "many_to_one"
        }],
        "verified_queries": [{"name": "q", "question": "How much?", "sql": "select 1", "verified_at": 1700000000}]
    }))
    .unwrap();
    assert_eq!(doc.tables[0].source, "DB.S.ORDERS");
    assert_eq!(doc.tables[0].fields.len(), 3);
    assert_eq!(doc.metrics[0].table.as_deref(), Some("orders"));
    assert_eq!(doc.metrics[1].table, None);
    assert_eq!(
        doc.relationships[0].cardinality.as_deref(),
        Some("many_to_one")
    );
    assert_eq!(
        doc.verified_queries[0].verified_at.as_deref(),
        Some("1700000000")
    );
    assert!(yaml::snowflake(&json!({"name": "x"})).is_err());
}

#[test]
fn a_metric_view_is_read_with_its_filter_and_joins() {
    let doc = yaml::databricks(
        &json!({
            "version": 1.1,
            "source": "main.sales.orders",
            "filter": "status = 'completed'",
            "joins": [{"name": "customer", "source": "main.sales.customers",
                       "true": "source.customer_id = customer.id"}],
            "fields": [{"name": "status", "expr": "source.status"}],
            "measures": [{"name": "revenue", "expr": "SUM(source.amount)", "comment": "c"}]
        }),
        "orders_metrics",
    )
    .unwrap();
    assert_eq!(doc.filter.as_deref(), Some("status = 'completed'"));
    assert_eq!(doc.tables[0].source, "main.sales.orders");
    assert_eq!(doc.tables[1].name, "customer");
    assert_eq!(doc.relationships[0].from_columns, ["customer_id"]);
    assert_eq!(doc.relationships[0].to_columns, ["id"]);
    assert_eq!(doc.metrics[0].table.as_deref(), Some("source"));
    assert_eq!(doc.metrics[0].unsupported, None);
    assert!(yaml::databricks(&json!({"measures": []}), "x").is_err());
}

#[test]
fn a_window_measure_is_marked_as_one_its_expression_does_not_hold() {
    let doc = yaml::databricks(
        &json!({"source": "t", "measures": [{"name": "rolling", "expr": "COUNT(DISTINCT c)",
                "window": [{"order": "d", "range": "trailing 7 day"}]}]}),
        "v",
    )
    .unwrap();
    assert!(
        doc.metrics[0]
            .unsupported
            .as_deref()
            .unwrap()
            .contains("window")
    );
}

#[test]
fn statements_are_split_outside_strings_and_classified() {
    let text = "SELECT 1; -- CREATE SEMANTIC VIEW no;\nCREATE SEMANTIC VIEW IF NOT EXISTS a.b.v TABLES (t AS x.y);\n\
        CREATE OR REPLACE VIEW main.s.m WITH METRICS LANGUAGE YAML AS $$\nversion: 1.1\nsource: t;\n$$;\nCREATE VIEW plain AS SELECT 1";
    let found = statements(text);
    assert_eq!(found.len(), 2);
    assert_eq!(
        (found[0].kind, found[0].name.as_str()),
        (StatementKind::SnowflakeView, "v")
    );
    assert_eq!(
        (found[1].kind, found[1].name.as_str()),
        (StatementKind::DatabricksMetricView, "m")
    );
    assert!(found[1].body.contains("source: t;"));
}

#[test]
fn a_table_with_no_alias_is_named_by_its_own_name() {
    let doc = ddl::read(
        "CREATE SEMANTIC VIEW v TABLES (db.s.orders PRIMARY KEY (o_id), c AS db.s.customer) \
         METRICS (orders.n AS COUNT(orders.o_id));",
    )
    .unwrap();
    assert_eq!(
        (doc.tables[0].name.as_str(), doc.tables[0].source.as_str()),
        ("orders", "db.s.orders")
    );
    assert_eq!(doc.tables[0].primary_key, ["o_id"]);
    assert_eq!(doc.tables[1].name, "c");
    assert_eq!(doc.metrics[0].table.as_deref(), Some("orders"));
}

#[test]
fn a_metric_non_additive_by_a_dimension_says_its_expression_does_not_hold_that() {
    let doc = ddl::read(
        "CREATE SEMANTIC VIEW v TABLES (t AS db.s.balances) METRICS (\
         t.balance NON ADDITIVE BY (t.day DESC NULLS LAST, t.region) AS SUM(t.amount) COMMENT = 'c', \
         t.total AS SUM(t.amount));",
    )
    .unwrap();
    let balance = &doc.metrics[0];
    assert_eq!(balance.expr, "SUM(t.amount)");
    assert_eq!(balance.description.as_deref(), Some("c"));
    assert_eq!(
        balance.unsupported.as_deref(),
        Some("it is non-additive by t.day, t.region, which its expression does not hold")
    );
    assert_eq!(doc.metrics[1].unsupported, None);
}

// The binding rule and its verdicts.

/// Definitions 0 to 3: SUM(a * b), SUM(a) * SUM(b), SUM(a * b) under a condition, and the
/// condition status = 'completed'.
fn formulas() -> Formulas {
    let mut f = Formulas::with_key(b"conform tests");
    let col = |n: &str| format!("(col \"{n}\")");
    let sum = format!("(sum (mul {} {}))", col("a"), col("b"));
    let product = format!("(mul (sum {}) (sum {}))", col("a"), col("b"));
    f.add(&sum, "").unwrap();
    f.add(&product, "").unwrap();
    f.add(&sum, "status").unwrap();
    f.add(
        &format!(
            "(sym \"EQ\" {} (lit \"string\" \"completed\"))",
            col("status")
        ),
        "",
    )
    .unwrap();
    f
}

fn claim(name: &str, document: &str, formula: u32) -> serde_json::Value {
    json!({"document": document, "path": format!("{document}.sql"), "name": name, "synonyms": [],
           "state": "bound", "formula": formula, "columns": ["c.a", "c.b"]})
}

fn term(grade: &str, formula: Option<u32>, filters: serde_json::Value) -> serde_json::Value {
    let (defined, called) = match grade {
        "columns" => (json!(["c.a"]), "Other"),
        "name" => (json!([]), "Net"),
        _ => (json!(["c.a"]), "Net"),
    };
    let reads = if grade == "name" {
        json!([])
    } else {
        json!(["c.a"])
    };
    json!({"name": called, "names": [called], "source": "term", "approved": true, "defined": defined,
           "reads": reads, "formula": formula, "filters": filters, "path": "definitions.yml"})
}

/// A filter a term requires: its text, the keys of its conditions and, as a layer's own filter
/// would say it, the definitions of its parts.
fn filter(text: &str, keys: &[&str]) -> serde_json::Value {
    json!({"text": text, "keys": keys, "definitions": []})
}

/// `judge` where the conditions that hold on every row of a claim's columns are `held`.
fn run(request: &serde_json::Value, held: &[(&str, &str)]) -> serde_json::Value {
    let held: Vec<(String, String)> = held
        .iter()
        .map(|(k, t)| ((*k).to_string(), (*t).to_string()))
        .collect();
    judge(&mut formulas(), request, &mut |_| held.clone())
}

fn first(request: &serde_json::Value) -> serde_json::Value {
    run(request, &[])["findings"][0].clone()
}

/// A request of one claim and the terms that might define it.
fn one(claim: serde_json::Value, terms: serde_json::Value) -> serde_json::Value {
    json!({"claims": [claim], "terms": terms})
}

#[test]
fn a_metric_with_nothing_to_compare_with_is_unbound() {
    let report = run(&one(claim("net", "d", 0), json!([])), &[]);
    assert_eq!(report["findings"][0]["verdict"], "unbound");
    assert_eq!(report["summary"]["unbound"], 1);
    assert_eq!(report["documents"]["d"]["metrics"], 1);
}

#[test]
fn a_metric_equal_to_its_term_conforms_and_reports_the_error_bound() {
    let found = first(&one(
        claim("net", "d", 0),
        json!([term("columns+name", Some(0), json!([]))]),
    ));
    assert_eq!(found["verdict"], "conforms");
    assert!(found["error_bound"].as_f64().unwrap() < 1e-30, "{found}");
}

#[test]
fn a_different_formula_is_an_error_when_approved_or_named_and_a_warning_otherwise() {
    let approved = first(&one(
        claim("x", "d", 0),
        json!([term("columns", Some(1), json!([]))]),
    ));
    assert_eq!(approved["severity"], "error");
    assert_eq!(approved["compare"], json!([0, 1]));
    let named = first(&one(
        claim("net", "d", 0),
        json!([term("name", Some(1), json!([]))]),
    ));
    assert_eq!(named["severity"], "error");
    assert_eq!(named["against"][0]["grade"], "name");
}

#[test]
fn filters_alone_never_conform_and_only_warn_when_missing() {
    let required = json!([filter("status = 'completed'", &["k1"])]);
    let request = one(
        claim("net", "d", 0),
        json!([term("columns+name", None, required.clone())]),
    );
    let found = run(&request, &[])["findings"][0].clone();
    // The approved term is the metric's by name: a missing filter is an error.
    assert_eq!(
        (found["verdict"].as_str(), found["severity"].as_str()),
        (Some("differs"), Some("error"))
    );
    // Matched through the columns alone it is a candidate, and only a warning.
    let by_columns = one(
        claim("net", "d", 0),
        json!([term("columns", None, required.clone())]),
    );
    let candidate = run(&by_columns, &[])["findings"][0].clone();
    assert_eq!(
        (
            candidate["verdict"].as_str(),
            candidate["severity"].as_str()
        ),
        (Some("differs"), Some("warning"))
    );
    assert_eq!(
        found["missing_filters"][0]["filter"],
        "status = 'completed'"
    );
    assert_eq!(
        found["missing_filters"][0]["against"],
        "approved definition"
    );
    assert_eq!(found["missing_filters"][0]["by"], "Net");
    assert_eq!(found["held"], false);
    // The condition holds on the rows of the columns the metric reads.
    let request = one(
        claim("net", "d", 0),
        json!([term("columns", None, required)]),
    );
    let found = run(&request, &[("k1", "status = 'completed'")])["findings"][0].clone();
    assert_eq!(found["verdict"], "unbound");
    assert_eq!(found["held"], true);
    assert_eq!(
        found["detail"],
        "its required filters hold; no formula to compare"
    );
    assert!(found.get("error_bound").is_none());
}

#[test]
fn a_filter_of_no_conditions_is_not_held_and_one_the_layer_filters_by_is() {
    let request = |filters: serde_json::Value| {
        let mut c = claim("net", "d", 0);
        c["view"] = json!([3]);
        one(c, json!([term("columns", None, filters)]))
    };
    let unparsed = json!([filter("status = 'completed'", &[])]);
    assert_eq!(
        run(&request(unparsed), &[("k1", "x")])["findings"][0]["verdict"],
        "differs"
    );
    // The layer's own filter is the condition (definition 3).
    let by_view = json!([{"text": "status = 'completed'", "keys": [], "definitions": [3]}]);
    assert_eq!(
        run(&request(by_view), &[])["findings"][0]["verdict"],
        "unbound"
    );
}

#[test]
fn a_term_is_a_candidate_through_a_column_or_a_name_and_never_through_the_file_read() {
    let mut own = term("columns+name", Some(1), json!([]));
    own["path"] = json!("d.sql");
    let found = first(&one(claim("net", "d", 0), json!([own])));
    assert_eq!(
        found["verdict"], "unbound",
        "the file being checked is no target: {found}"
    );
    // Neither the columns nor the name match.
    let mut other = term("columns+name", Some(1), json!([]));
    other["defined"] = json!(["z.z"]);
    other["names"] = json!(["Gross"]);
    assert_eq!(
        first(&one(claim("net", "d", 0), json!([other])))["verdict"],
        "unbound"
    );
    // A synonym is a name.
    let mut synonym = claim("revenue", "d", 0);
    synonym["synonyms"] = json!(["Net"]);
    assert_eq!(
        first(&one(synonym, json!([term("name", Some(1), json!([]))])))["verdict"],
        "differs"
    );
}

#[test]
fn a_match_through_columns_alone_is_at_most_a_warning() {
    let mut candidate = term("columns", Some(1), json!([]));
    candidate["approved"] = json!(false);
    let found = first(&one(claim("open revenue", "d", 0), json!([candidate])));
    assert_eq!(
        (found["verdict"].as_str(), found["severity"].as_str()),
        (Some("differs"), Some("warning"))
    );
    // The name with a formula, or an approved formula, is a binding.
    let named = first(&one(
        claim("net", "d", 0),
        json!([term("name", Some(1), json!([]))]),
    ));
    assert_eq!(named["severity"], "error");
}

#[test]
fn an_approved_definition_with_no_formula_or_filter_gives_nothing_to_compare() {
    let found = first(&one(
        claim("net", "d", 0),
        json!([term("columns", None, json!([]))]),
    ));
    assert_eq!(found["verdict"], "unbound");
}

#[test]
fn a_disagreement_between_layers_is_one_pair_whatever_the_read_order() {
    let a = claim("net", "one", 0);
    let b = claim("Net", "two", 1);
    let forward = run(&json!({"claims": [a.clone(), b.clone()]}), &[]);
    let backward = run(&json!({"claims": [b, a]}), &[]);
    let pairs = |r: &serde_json::Value| -> Vec<serde_json::Value> {
        r["findings"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|f| f["kind"] == "pair")
            .cloned()
            .collect()
    };
    assert_eq!(pairs(&forward).len(), 1);
    assert_eq!(pairs(&forward), pairs(&backward));
    let pair = &pairs(&forward)[0];
    assert_eq!(pair["severity"], "error");
    assert_eq!(pair["layers"][0]["document"], "one");
    assert_eq!(pair["layers"][1]["document"], "two");
    assert_eq!(forward["summary"]["pairs"], 1);
}

#[test]
fn layers_that_agree_conform_and_a_filter_one_applies_is_missing_in_the_other() {
    let agree = run(
        &json!({"claims": [claim("net", "one", 0), claim("net", "two", 0)]}),
        &[],
    );
    assert_eq!(agree["findings"][0]["verdict"], "conforms");
    assert_eq!(agree["summary"]["pairs"], 0);
    // What holds depends on the columns asked about: layer one's rows are filtered, two's not.
    let mut a = claim("net", "one", 0);
    a["columns"] = json!(["one.a"]);
    let mut b = claim("net", "two", 0);
    b["columns"] = json!(["two.a"]);
    let request = json!({"claims": [a, b]});
    let found = judge(&mut formulas(), &request, &mut |columns| {
        if columns == ["one.a"] {
            vec![("k1".to_string(), "status = 'completed'".to_string())]
        } else {
            Vec::new()
        }
    });
    let pair = found["findings"]
        .as_array()
        .unwrap()
        .iter()
        .find(|f| f["kind"] == "pair")
        .unwrap()
        .clone();
    let missing = &pair["missing_filters"][0];
    assert_eq!(missing["against"], "sibling implementation");
    // `by` is what requires the filter, as it is in a metric's finding.
    assert_eq!(missing["by"], "net (one)");
    assert!(missing.get("applied_by").is_none(), "{missing}");
    assert_eq!(missing["filter"], "status = 'completed'");
    assert_eq!(pair["compare"], serde_json::Value::Null);
}

#[test]
fn what_holds_is_asked_once_for_a_set_of_columns_and_only_when_needed() {
    let mut asked = 0;
    let request =
        json!({"claims": [claim("net", "d", 0)], "terms": [term("columns", Some(0), json!([]))]});
    let _ = judge(&mut formulas(), &request, &mut |_| {
        asked += 1;
        Vec::new()
    });
    assert_eq!(
        asked, 0,
        "no filter is required and no layer shares the name"
    );
    let request = json!({"claims": [claim("net", "one", 0), claim("net", "two", 0)]});
    let mut asked = Vec::new();
    let _ = judge(&mut formulas(), &request, &mut |columns| {
        asked.push(columns.to_vec());
        Vec::new()
    });
    assert_eq!(
        asked.len(),
        1,
        "the two claims read the same columns: {asked:?}"
    );
}

#[test]
fn a_metric_is_unbound_by_the_graph_only_when_the_graph_lacks_what_it_reads() {
    let cause = |resolution: serde_json::Value| {
        let mut c = claim("m", "d", 0);
        c["resolution"] = resolution;
        let found = first(&one(c, json!([])));
        (found["verdict"].clone(), found.get("cause").cloned())
    };
    assert_eq!(
        cause(json!({"parsed": true, "unbound_tables": "db.s.t"})),
        (json!("unbound"), Some(json!("graph")))
    );
    // What the layer writes, and nothing to compare with, are not the graph's doing.
    assert_eq!(
        cause(json!({"parsed": true, "derived": "built on other metrics"})),
        (json!("unbound"), None)
    );
    assert_eq!(
        cause(json!({"parsed": true, "portable": true})),
        (json!("unbound"), None)
    );
}

#[test]
fn a_metric_that_could_not_be_read_is_never_conforms() {
    for state in ["unparsed", "unbound", "column-missing"] {
        let mut c = claim("m", "d", 0);
        c["state"] = json!(state);
        let request = one(c, json!([term("columns", Some(0), json!([]))]));
        assert_eq!(first(&request)["verdict"], state);
    }
}

#[test]
fn how_reading_a_metric_went_decides_what_is_wrong_with_it() {
    let verdict = |resolution: serde_json::Value| {
        let mut c = claim("m", "d", 0);
        c["resolution"] = resolution;
        let found = first(&one(c, json!([term("columns", Some(0), json!([]))])));
        (
            found["verdict"].as_str().unwrap().to_string(),
            found["detail"].as_str().unwrap().to_string(),
        )
    };
    // A fact it reads does not parse: unparsed, not a missing column.
    assert_eq!(
        verdict(json!({"parsed": true, "unreadable": "o.broken", "failed": true})),
        (
            "unparsed".to_string(),
            "it reads o.broken, whose expression does not parse".to_string()
        )
    );
    assert_eq!(
        verdict(json!({"parsed": true, "fan_out": "it sums a parent", "portable": true})),
        ("unbound".to_string(), "it sums a parent".to_string())
    );
    assert_eq!(
        verdict(json!({"parsed": true, "portable": true})).0,
        "conforms"
    );
}

#[test]
fn a_name_is_matched_without_case_or_separators_and_by_one_normaliser() {
    assert_eq!(norm("Net_revenue"), norm("net revenue"));
    assert_eq!(norm("Order-ID"), "orderid");
    // A letter that lowercases to a letter and a mark is the letter.
    assert_eq!(norm("\u{130}D"), "id");
}

#[test]
fn keys_relationships_and_columns_are_judged_from_the_facts() {
    let report = run(
        &json!({
            "keys": [
                {"document": "d", "name": "orders.order_id", "declared": "fact", "unique": true, "entity": "order"},
                {"document": "d", "name": "orders.id", "declared": "primary key", "unique": false},
                {"document": "d", "name": "orders.ok", "declared": "primary key", "unique": true},
                {"document": "d", "name": "orders.x", "declared": "fact", "unique": false},
                {"document": "d", "name": "orders.y", "declared": "fact", "unique": true}
            ],
            "code_joins": [
                {"from": "a1", "to": "a2", "cardinality": "many-to-one"},
                {"from": "c1", "to": "c2", "cardinality": "one-to-many"},
                {"from": "e1", "to": "e2", "cardinality": "one-to-one"},
                {"from": "f1", "to": "f2", "cardinality": ""},
                {"from": "g1", "to": "g2", "cardinality": "many-to-one"},
                {"from": "h1", "to": "h2", "cardinality": "many-to-one"}
            ],
            "joins": [
                {"document": "d", "name": "reversed", "from": "a2", "to": "a1"},
                {"document": "d", "name": "none", "from": "b1", "to": "b2"},
                {"document": "d", "name": "c", "from": "c1", "to": "c2", "declared": "many_to_one"},
                {"document": "d", "name": "e", "from": "e1", "to": "e2", "declared": "many-to-one"},
                {"document": "d", "name": "f", "from": "f1", "to": "f2", "declared": "many-to-one"},
                {"document": "d", "name": "g", "from": "g1", "to": "g2", "declared": "one-to-one"},
                {"document": "d", "name": "default", "from": "h1", "to": "h2"}
            ],
            "columns": [
                {"document": "d", "kind": "dimension", "name": "t.a", "state": "column-missing", "detail": "x"},
                {"document": "d", "kind": "fact", "name": "t.b", "state": "ok"}
            ]
        }),
        &[],
    );
    let verdicts: Vec<&str> = report["findings"]
        .as_array()
        .unwrap()
        .iter()
        .map(|f| f["verdict"].as_str().unwrap())
        .collect();
    assert_eq!(
        verdicts,
        [
            "column-missing",
            "key-misfiled",
            "key-untested",
            "cardinality-contradicted",
            "unknown-join",
            "cardinality-contradicted",
            "cardinality-contradicted",
        ]
    );
    // The one-to-many code that a many_to_one declaration contradicts (c) and the other way round
    // (reversed), and a one-to-one declared where the code says many-to-one (g).
    let detail = |name: &str| {
        report["findings"]
            .as_array()
            .unwrap()
            .iter()
            .find(|f| f["name"] == name)
            .map(|f| f["detail"].as_str().unwrap().to_string())
    };
    assert_eq!(
        detail("c").unwrap(),
        "is many-to-one, but the code says one-to-many"
    );
    assert_eq!(
        detail("g").unwrap(),
        "is one-to-one, but the code says many-to-one"
    );
    assert_eq!(
        detail("reversed").unwrap(),
        "refers the other way round to the code's relationship"
    );
    assert!(
        detail("default").is_none(),
        "no cardinality declared is many-to-one"
    );
    assert_eq!(report["summary"]["key_misfiled"], 1);
    assert_eq!(report["format_version"], 1);
}

// The report does not depend on the order the layers were read in.

fn layer_claims() -> Vec<serde_json::Value> {
    let mut claims = Vec::new();
    for (document, name, formula, columns) in [
        ("one", "net", 0, ["c.a", "c.b"]),
        ("two", "Net", 1, ["c.a", "c.b"]),
        ("three", "net_", 0, ["c.a", "c.b"]),
        ("two", "gross", 0, ["c.a", "x.y"]),
        ("three", "Gross", 0, ["c.a", "x.y"]),
        ("one", "unrelated", 2, ["z.z", "z.w"]),
        ("four", "NET", 0, ["c.a", "c.b"]),
    ] {
        let mut c = claim(name, document, formula);
        c["columns"] = json!(columns);
        claims.push(c);
    }
    claims
}

fn judged_in_order(order: &[usize]) -> serde_json::Value {
    let all = layer_claims();
    let claims: Vec<serde_json::Value> = order.iter().map(|&i| all[i].clone()).collect();
    let request = json!({
        "claims": claims,
        "terms": [term("columns+name", Some(0), json!([filter("status = 'completed'", &["k1"])]))],
    });
    // A filter holds on the rows of the columns of the second layer only.
    let mut report = judge(&mut formulas(), &request, &mut |columns| {
        if columns.contains(&"x.y".to_string()) {
            vec![("k1".to_string(), "status = 'completed'".to_string())]
        } else {
            Vec::new()
        }
    });
    if let Some(found) = report["findings"].as_array_mut() {
        found.sort_by_key(ToString::to_string);
    }
    report
}

proptest::proptest! {
    #[test]
    fn the_report_is_the_same_whatever_order_the_layers_are_read_in(
        order in proptest::strategy::Just((0..7usize).collect::<Vec<_>>()).prop_shuffle()
    ) {
        proptest::prop_assert_eq!(judged_in_order(&order), judged_in_order(&[0, 1, 2, 3, 4, 5, 6]));
    }
}
