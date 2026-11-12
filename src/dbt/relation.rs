//! A relation's name as a dbt manifest writes it (`"db"."main"."orders"`, `` `project`.`dataset`.`t` ``,
//! `[db].[schema].[t]`) as the canonical key the graph uses: the catalog, schema and name with
//! their quotes taken off, in lower case, joined by dots.
//!
//! Only the rules of quoting are needed, so they are owned here rather than asked of a SQL parser:
//! a quoted part may hold a dot, a doubled quote inside it is one quote, and BigQuery reads a
//! dotted name in quotes as the parts it holds. More than three parts keep the first two and the
//! last.

/// The quotes `dialect` writes identifiers in.
struct Quoting {
    double: bool,
    backtick: bool,
    bracket: bool,
    /// Quoted dots split the name, as BigQuery's do.
    splits_quoted: bool,
}

fn quoting(dialect: Option<&str>) -> Quoting {
    let name = dialect.unwrap_or("");
    let backtick = matches!(
        name,
        "bigquery"
            | "mysql"
            | "spark"
            | "databricks"
            | "clickhouse"
            | "hive"
            | "doris"
            | "starrocks"
            | "drill"
    );
    Quoting {
        double: !matches!(name, "bigquery" | "spark" | "databricks" | "hive"),
        backtick,
        bracket: name == "tsql",
        splits_quoted: name == "bigquery",
    }
}

/// The parts of `name`, unquoted, or `None` when a quote is never closed.
fn parts(name: &str, how: &Quoting) -> Option<Vec<String>> {
    let mut found = Vec::new();
    let mut part = String::new();
    let mut chars = name.trim().chars().peekable();
    while let Some(c) = chars.next() {
        let closing = match c {
            '"' if how.double => Some('"'),
            '`' if how.backtick => Some('`'),
            '[' if how.bracket => Some(']'),
            _ => None,
        };
        match closing {
            Some(close) if !how.splits_quoted => loop {
                match chars.next()? {
                    next if next == close => {
                        if chars.peek() == Some(&close) {
                            part.push(close);
                            chars.next();
                        } else {
                            break;
                        }
                    }
                    next => part.push(next),
                }
            },
            Some(close) => loop {
                // BigQuery's quotes only mark the part; the dots in them still split it.
                match chars.next()? {
                    next if next == close => break,
                    '.' => found.push(std::mem::take(&mut part)),
                    next => part.push(next),
                }
            },
            None if c == '.' => found.push(std::mem::take(&mut part)),
            None => part.push(c),
        }
    }
    found.push(part);
    Some(found)
}

/// One part of a key: stripped of quotes at its ends and in lower case; empty parts are dropped.
fn clean(part: &str) -> String {
    part.trim_matches(['"', '`', '[', ']']).to_lowercase()
}

/// `relation_key`: the non-empty parts, cleaned, joined by dots.
pub fn key<'a>(parts: impl IntoIterator<Item = Option<&'a str>>) -> String {
    let kept: Vec<String> = parts
        .into_iter()
        .flatten()
        .filter(|part| !part.is_empty())
        .map(clean)
        .filter(|part| !part.is_empty())
        .collect();
    kept.join(".")
}

/// The key of a relation name in `dialect`, or `None` for an empty name.
pub fn parse(name: &str, dialect: Option<&str>) -> Option<String> {
    if name.is_empty() {
        return None;
    }
    let found = match parts(name, &quoting(dialect)) {
        Some(found) => found,
        // A quote that never closes: the parts are what is between the dots, quotes removed.
        None => name
            .replace(['"', '`'], "")
            .split('.')
            .map(str::to_string)
            .collect(),
    };
    let count = found.len();
    let (catalog, schema, table) = match count {
        1 => (None, None, Some(&found[0])),
        2 => (None, Some(&found[0]), Some(&found[1])),
        3 => (Some(&found[0]), Some(&found[1]), Some(&found[2])),
        _ => (Some(&found[0]), Some(&found[1]), found.last()),
    };
    Some(key(
        [catalog, schema, table].map(|part| part.map(String::as_str))
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn is(name: &str, dialect: Option<&str>, expected: &str) {
        assert_eq!(
            parse(name, dialect).as_deref(),
            Some(expected),
            "{name} in {dialect:?}"
        );
    }

    #[test]
    fn quoted_and_plain_names_become_lower_case_keys() {
        is("\"DB\".\"main\".\"Stg\"", None, "db.main.stg");
        is("db.s.Ünï", Some("snowflake"), "db.s.ünï");
        is("x", None, "x");
        is("\"x\"", Some("duckdb"), "x");
        is("a..b", None, "a.b");
        is("\"db\".\"s\".\"t\" ", None, "db.s.t");
    }

    #[test]
    fn each_dialect_reads_its_own_quotes() {
        is("`p`.`d`.`t`", Some("bigquery"), "p.d.t");
        is("`p`.`d`.`t`", Some("mysql"), "p.d.t");
        is("[db].[s].[t]", Some("tsql"), "db.s.t");
        is("`a-b`.c.d", Some("bigquery"), "a-b.c.d");
        is("my-proj.ds.t", Some("bigquery"), "my-proj.ds.t");
    }

    #[test]
    fn a_quoted_part_may_hold_a_dot_and_a_doubled_quote() {
        is("\"a.b\".\"c\"", None, "a.b.c");
        is("\"a\"\"b\".c", Some("postgres"), "a\"b.c");
        // BigQuery reads a dotted name in quotes as the parts it holds.
        is("\"a.b\".\"c\"", Some("bigquery"), "a.b.c");
    }

    #[test]
    fn more_than_three_parts_keep_the_first_two_and_the_last() {
        is("a.b.c.d", None, "a.b.d");
    }

    #[test]
    fn an_unclosed_quote_is_read_without_quotes_and_nothing_is_nothing() {
        is("\"abc.d", None, "abc.d");
        assert_eq!(parse("", None), None);
    }

    #[test]
    fn a_key_drops_parts_that_are_empty_once_cleaned() {
        assert_eq!(key([Some("DB"), None, Some("\"\""), Some("T")]), "db.t");
    }
}
