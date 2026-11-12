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
    /// A doubled `"` inside `"..."` is one quote.
    double_escapes: bool,
    backtick: bool,
    bracket: bool,
    /// Quoted dots split the name, as BigQuery's do.
    splits_quoted: bool,
    /// BigQuery reads names the others refuse (`my-project.dataset.table`).
    lenient: bool,
    /// A part may start with a digit (`1z`), as MySQL's and Hive's families allow.
    digit_start: bool,
    /// `$` may be in a part of a name of four parts or more, and (but for ClickHouse) of fewer.
    dollar: bool,
    clickhouse: bool,
    /// Hive's family reads `1Y`, `1d` and `1bd` as numbers.
    number_suffixes: bool,
}

fn quoting(dialect: Option<&str>) -> Quoting {
    let name = dialect.unwrap_or("");
    Quoting {
        double: name != "bigquery",
        double_escapes: !matches!(name, "spark" | "databricks" | "hive"),
        backtick: matches!(
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
                | "sqlite"
        ),
        bracket: matches!(name, "tsql" | "sqlite"),
        splits_quoted: name == "bigquery",
        lenient: name == "bigquery",
        clickhouse: name == "clickhouse",
        number_suffixes: matches!(name, "spark" | "databricks" | "hive"),
        dollar: !matches!(name, "clickhouse" | "databricks" | "spark"),
        digit_start: matches!(
            name,
            "bigquery"
                | "clickhouse"
                | "databricks"
                | "mysql"
                | "spark"
                | "hive"
                | "doris"
                | "starrocks"
        ),
    }
}

/// BigQuery reads a quoted part with dots in it as the parts it holds, in a name of at most three
/// parts as written.
fn split_quoted(found: Vec<Part>, how: &Quoting) -> Vec<Part> {
    if !how.splits_quoted || found.len() > 3 {
        return found;
    }
    found
        .into_iter()
        .flat_map(|part| {
            if part.quoted {
                part.text
                    .split('.')
                    .map(|piece| Part {
                        text: piece.to_string(),
                        quoted: true,
                    })
                    .collect::<Vec<_>>()
            } else {
                vec![part]
            }
        })
        .collect()
}

/// A part of a name, and whether it was written in quotes.
struct Part {
    text: String,
    quoted: bool,
}

/// The parts of `name`, unquoted, or `None` when a quote is never closed.
fn parts(name: &str, how: &Quoting) -> Option<Vec<Part>> {
    let mut found = Vec::new();
    let mut part = String::new();
    let mut quoted = false;
    let mut chars = name.trim().chars().peekable();
    while let Some(c) = chars.next() {
        let closing = match c {
            '"' if how.double => Some('"'),
            '`' if how.backtick => Some('`'),
            '[' if how.bracket => Some(']'),
            _ => None,
        };
        match closing {
            Some(close) => {
                quoted = true;
                loop {
                    match chars.next()? {
                        next if next == close => {
                            let doubles = close != '"' || how.double_escapes;
                            if doubles && chars.peek() == Some(&close) {
                                part.push(close);
                                chars.next();
                            } else if !doubles && !matches!(chars.peek(), None | Some('.')) {
                                // Where a quote does not escape itself, it is part of the name
                                // unless it ends it.
                                part.push(close);
                            } else {
                                break;
                            }
                        }
                        next => part.push(next),
                    }
                }
            }
            None if c == '.' => {
                found.push(Part {
                    text: std::mem::take(&mut part),
                    quoted,
                });
                quoted = false;
            }
            None => part.push(c),
        }
    }
    found.push(Part { text: part, quoted });
    Some(found)
}

/// Whether a SQL parser would read an unquoted part as one identifier: letters, digits and `_`
/// and `$`, and not starting with a digit unless the dialect allows it.
fn is_identifier(text: &str, how: &Quoting) -> bool {
    let digit_start = true;
    text.chars()
        .next()
        .is_some_and(|c| c.is_alphabetic() || c == '_' || (digit_start && c.is_ascii_digit()))
        && text
            .chars()
            .all(|c| c.is_alphanumeric() || c == '_' || (how.dollar && c == '$'))
}

/// Whether the name is one a SQL parser refuses, so that the name is read as what lies between
/// its dots: more than three parts of which one is not a plain identifier, and a number with a
/// quoted part after it.
fn refused(found: &[Part], how: &Quoting) -> bool {
    if how.lenient {
        return false;
    }
    let digits =
        |p: &Part| !p.quoted && !p.text.is_empty() && p.text.chars().all(|c| c.is_ascii_digit());
    // A part that starts with a digit after an unquoted name is read as a number with a dot.
    let any_quoted = found.iter().any(|p| p.quoted);
    let number_after_name = |at: usize| {
        let part = &found[at];
        // Where parts are quoted too, the parser's rule is not known; those names are read as
        // they are written.
        !any_quoted && !part.quoted && part.text.starts_with(|c: char| c.is_ascii_digit())
    };
    // ClickHouse reads no `$` in a name.
    let dollar = found.iter().any(|p| !p.quoted && p.text.contains('$'));
    let dotted_quote = found.iter().any(|p| p.quoted && p.text.contains('.'));
    if dollar && (how.clickhouse || (!how.dollar && dotted_quote)) {
        return true;
    }
    let long = found.len() >= 4;
    let tail_refused = long
        && if how.digit_start {
            found.last().is_some_and(digits) && number_after_name(found.len() - 1)
        } else {
            (0..found.len()).any(number_after_name)
        };
    // `1$a` is a number and a parameter.
    let number_then_dollar = long
        && found.iter().any(|p| {
            !p.quoted
                && p.text
                    .trim_start_matches(|c: char| c.is_ascii_digit())
                    .starts_with('$')
                && p.text.starts_with(|c: char| c.is_ascii_digit())
        });
    let suffixed = how.number_suffixes
        && long
        && found.iter().any(|p| {
            let digits = p.text.trim_end_matches(|c: char| c.is_ascii_alphabetic());
            let suffix = &p.text[digits.len()..];
            !p.quoted
                && !digits.is_empty()
                && digits.chars().all(|c| c.is_ascii_digit())
                && ["y", "s", "l", "d", "f", "bd"].contains(&suffix.to_lowercase().as_str())
        });
    tail_refused
        || suffixed
        || number_then_dollar
        || (long
            && found
                .iter()
                .any(|p| !p.quoted && !is_identifier(&p.text, how)))
        || (found.len() >= 2 && digits(&found[0]) && found[1].quoted)
        || (found.windows(2).any(|pair| {
            // A quoted part with a dot in it, next to a part that starts with a digit, lexes as
            // a number.
            let digit_part =
                |p: &Part| !p.quoted && p.text.starts_with(|c: char| c.is_ascii_digit());
            let dotted = |p: &Part| p.quoted && p.text.contains('.');
            (!how.digit_start && dotted(&pair[0]) && digit_part(&pair[1]))
                || (digit_part(&pair[0]) && dotted(&pair[1]))
        }))
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
    let how = quoting(dialect);
    let naive = || -> Vec<String> {
        name.replace(['"', '`'], "")
            .split('.')
            .map(str::to_string)
            .collect()
    };
    let found: Vec<String> = match parts(name, &how).map(|found| split_quoted(found, &how)) {
        Some(found) if !refused(&found, &how) => found.into_iter().map(|p| p.text).collect(),
        // A quote that never closes, or a name the parser refuses: every part is kept, as what
        // lies between the dots with the quotes removed.
        _ => {
            let every = naive();
            return Some(key(every.iter().map(|part| Some(part.as_str()))));
        }
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
    fn a_name_the_sql_parser_refuses_keeps_every_part() {
        // More than three parts of which one starts with a digit, a number before a quoted part,
        // and backticks in a dialect that has none.
        is("a.b.1z.c", None, "a.b.1z.c");
        is("a.b.c.1d", Some("snowflake"), "a.b.c.1d");
        is("0.\"x\".\"y\"", None, "0.x.y");
        is("`a.b`.c.d", Some("duckdb"), "a.b.c.d");
        // BigQuery reads them.
        is("a.b.1z.c", Some("bigquery"), "a.b.c");
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
