//! A Snowflake `CREATE SEMANTIC VIEW` statement as a [`Doc`].
//!
//! sqlglot has no grammar for this statement (it reads only the `SEMANTIC_VIEW(...)` query
//! function), so the clauses are split here, at brackets and commas outside strings, and each
//! expression is handed on as text for sqlglot to parse: this module never reads an expression.
//! This splitter is not a second SQL tokeniser to keep: it moves onto the Rust SQL engine's
//! tokeniser (`src/sql/`, branch local/rust-sql) when that engine lands.
//!
//! The grammar is Snowflake's reference for `CREATE SEMANTIC VIEW`:
//! <https://docs.snowflake.com/en/sql-reference/sql/create-semantic-view>.

use super::{Doc, Field, FieldKind, Metric, Relationship, Table, Verified};

/// One lexical item of the statement, with where it is in the text.
#[derive(Clone, Debug, PartialEq)]
enum Tok {
    /// A bare word, a number or an operator character.
    Word(String),
    /// A quoted identifier, without its quotes.
    Quoted(String),
    /// A string literal, unescaped.
    Text(String),
    Open,
    Close,
    Comma,
    Equals,
    Dot,
}

/// A token and the byte range of the source it came from.
#[derive(Clone, Debug)]
struct Item {
    tok: Tok,
    from: usize,
    to: usize,
}

fn lex(source: &str) -> Result<Vec<Item>, String> {
    let bytes = source.as_bytes();
    let mut items = Vec::new();
    let mut i = 0;
    while i < bytes.len() {
        let c = bytes[i];
        let start = i;
        let tok = match c {
            _ if c.is_ascii_whitespace() => {
                i += 1;
                continue;
            }
            b'-' if bytes.get(i + 1) == Some(&b'-') => {
                while i < bytes.len() && bytes[i] != b'\n' {
                    i += 1;
                }
                continue;
            }
            b'/' if bytes.get(i + 1) == Some(&b'*') => {
                i += 2;
                while i + 1 < bytes.len() && !(bytes[i] == b'*' && bytes[i + 1] == b'/') {
                    i += 1;
                }
                i += 2;
                continue;
            }
            b'(' => {
                i += 1;
                Tok::Open
            }
            b')' => {
                i += 1;
                Tok::Close
            }
            b',' => {
                i += 1;
                Tok::Comma
            }
            b'=' => {
                i += 1;
                Tok::Equals
            }
            b'.' => {
                i += 1;
                Tok::Dot
            }
            b'\'' | b'"' => {
                let (text, next) = quoted(source, i)?;
                i = next;
                if c == b'"' {
                    Tok::Quoted(text)
                } else {
                    Tok::Text(text)
                }
            }
            b'$' if bytes.get(i + 1) == Some(&b'$') => {
                let end = source[i + 2..]
                    .find("$$")
                    .ok_or("a $$ string is not closed")?;
                let text = source[i + 2..i + 2 + end].to_string();
                i += end + 4;
                Tok::Text(text)
            }
            _ => {
                // A word runs to the next space or punctuation; any other character is a word.
                i += source[i..].chars().next().map_or(1, char::len_utf8);
                if is_word(c) {
                    while i < bytes.len() && is_word(bytes[i]) {
                        i += 1;
                    }
                }
                Tok::Word(source[start..i].to_string())
            }
        };
        items.push(Item {
            tok,
            from: start,
            to: i,
        });
    }
    Ok(items)
}

fn is_word(c: u8) -> bool {
    c.is_ascii_alphanumeric() || c == b'_' || c == b'$' || c >= 0x80
}

/// The text of the quoted string or identifier that opens at `start`, and where it ends.
fn quoted(source: &str, start: usize) -> Result<(String, usize), String> {
    let bytes = source.as_bytes();
    let quote = bytes[start];
    let mut text = String::new();
    let mut i = start + 1;
    let mut run = i;
    while i < bytes.len() {
        let c = bytes[i];
        if c == quote {
            text.push_str(&source[run..i]);
            if bytes.get(i + 1) == Some(&quote) {
                text.push(quote as char);
                i += 2;
                run = i;
                continue;
            }
            return Ok((text, i + 1));
        }
        if c == b'\\' && quote == b'\'' && i + 1 < bytes.len() {
            // Snowflake reads backslash escapes in single-quoted strings.
            text.push_str(&source[run..i]);
            let escaped = source[i + 1..].chars().next().unwrap_or('\\');
            text.push(match escaped {
                'n' => '\n',
                'r' => '\r',
                't' => '\t',
                other => other,
            });
            i += 1 + escaped.len_utf8();
            run = i;
            continue;
        }
        i += 1;
    }
    Err("a quoted string is not closed".to_string())
}

/// A cursor over the tokens.
struct Reader<'a> {
    source: &'a str,
    items: Vec<Item>,
    at: usize,
}

impl Reader<'_> {
    fn peek(&self) -> Option<&Tok> {
        self.items.get(self.at).map(|i| &i.tok)
    }

    fn word_is(&self, ahead: usize, keyword: &str) -> bool {
        matches!(self.items.get(self.at + ahead).map(|i| &i.tok),
            Some(Tok::Word(w)) if w.eq_ignore_ascii_case(keyword))
    }

    fn eat_word(&mut self, keyword: &str) -> bool {
        let found = self.word_is(0, keyword);
        if found {
            self.at += 1;
        }
        found
    }

    fn expect_word(&mut self, keyword: &str) -> Result<(), String> {
        if self.eat_word(keyword) {
            Ok(())
        } else {
            Err(format!(
                "expected {} near {}",
                keyword.to_uppercase(),
                self.near()
            ))
        }
    }

    fn eat(&mut self, tok: &Tok) -> bool {
        let found = self.peek() == Some(tok);
        if found {
            self.at += 1;
        }
        found
    }

    fn expect(&mut self, tok: &Tok, shown: &str) -> Result<(), String> {
        if self.eat(tok) {
            Ok(())
        } else {
            Err(format!("expected {shown} near {}", self.near()))
        }
    }

    fn near(&self) -> String {
        let from = self
            .items
            .get(self.at)
            .map_or(self.source.len(), |i| i.from);
        let text: String = self.source[from..].chars().take(40).collect();
        format!("{text:?}")
    }

    /// An identifier, quoted or not.
    fn name(&mut self) -> Result<String, String> {
        match self.peek() {
            Some(Tok::Word(w)) => {
                let w = w.clone();
                self.at += 1;
                Ok(w)
            }
            Some(Tok::Quoted(q)) => {
                let q = q.clone();
                self.at += 1;
                Ok(q)
            }
            _ => Err(format!("expected a name near {}", self.near())),
        }
    }

    /// A dotted name, as written (`db.schema.table`).
    fn dotted(&mut self) -> Result<Vec<String>, String> {
        let mut parts = vec![self.name()?];
        while self.eat(&Tok::Dot) {
            parts.push(self.name()?);
        }
        Ok(parts)
    }

    fn text(&mut self) -> Result<String, String> {
        match self.peek() {
            Some(Tok::Text(t)) => {
                let t = t.clone();
                self.at += 1;
                Ok(t)
            }
            _ => Err(format!("expected a string near {}", self.near())),
        }
    }

    /// `(a, b, c)` as names.
    fn names(&mut self) -> Result<Vec<String>, String> {
        self.expect(&Tok::Open, "(")?;
        let mut names = vec![self.name()?];
        while self.eat(&Tok::Comma) {
            names.push(self.name()?);
        }
        self.expect(&Tok::Close, ")")?;
        Ok(names)
    }

    /// The source between the current token and the next comma or closing bracket outside any
    /// brackets, or the next of `stops` (a word that starts a modifier), as written.
    fn expression(&mut self, stops: &[&str]) -> Result<String, String> {
        let start = self.at;
        let mut depth = 0usize;
        while let Some(tok) = self.peek() {
            match tok {
                Tok::Open => depth += 1,
                Tok::Close if depth == 0 => break,
                Tok::Close => depth -= 1,
                Tok::Comma if depth == 0 => break,
                // WITH SYNONYMS and WITH EXTENSION are modifiers; a WITH elsewhere is not.
                Tok::Word(w)
                    if depth == 0
                        && stops.iter().any(|s| w.eq_ignore_ascii_case(s))
                        && (!w.eq_ignore_ascii_case("with")
                            || self.word_is(1, "synonyms")
                            || self.word_is(1, "extension")) =>
                {
                    break;
                }
                _ => {}
            }
            self.at += 1;
        }
        if self.at == start {
            return Err(format!("expected an expression near {}", self.near()));
        }
        let from = self.items[start].from;
        let to = self.items[self.at - 1].to;
        Ok(self.source[from..to].trim().to_string())
    }

    /// Skips to the end of the bracketed group the cursor is in, leaving it on the closing
    /// bracket.
    fn skip_group(&mut self) {
        let mut depth = 0usize;
        while let Some(tok) = self.peek() {
            match tok {
                Tok::Open => depth += 1,
                Tok::Close if depth == 0 => return,
                Tok::Close => depth -= 1,
                _ => {}
            }
            self.at += 1;
        }
    }
}

const STOPS: &[&str] = &["with", "comment"];

/// What may follow a table, fact, dimension or metric: synonyms, a comment, an extension.
#[derive(Default)]
struct Modifiers {
    synonyms: Vec<String>,
    comment: Option<String>,
}

fn modifiers(r: &mut Reader) -> Result<Modifiers, String> {
    let mut found = Modifiers::default();
    loop {
        if r.word_is(0, "with") && r.word_is(1, "synonyms") {
            r.at += 2;
            r.eat(&Tok::Equals);
            r.expect(&Tok::Open, "(")?;
            loop {
                found.synonyms.push(r.text()?);
                if !r.eat(&Tok::Comma) {
                    break;
                }
            }
            r.expect(&Tok::Close, ")")?;
        } else if r.eat_word("comment") {
            r.eat(&Tok::Equals);
            found.comment = Some(r.text()?);
        } else if r.word_is(0, "with") && r.word_is(1, "extension") {
            r.at += 2;
            r.eat(&Tok::Open);
            r.skip_group();
            r.eat(&Tok::Close);
        } else {
            return Ok(found);
        }
    }
}

/// Runs `item` over each comma-separated entry of the parenthesised clause the cursor is at.
fn clause(
    r: &mut Reader,
    mut item: impl FnMut(&mut Reader) -> Result<(), String>,
) -> Result<(), String> {
    r.expect(&Tok::Open, "(")?;
    if r.eat(&Tok::Close) {
        return Ok(());
    }
    loop {
        item(r)?;
        if !r.eat(&Tok::Comma) {
            break;
        }
    }
    r.expect(&Tok::Close, ")")
}

/// Reads one `CREATE SEMANTIC VIEW` statement.
///
/// # Errors
///
/// A message naming what was expected and where, when the text is not a statement of that
/// grammar; a statement of another kind is an error too.
pub fn read(source: &str) -> Result<Doc, String> {
    let items = lex(source)?;
    let mut r = Reader {
        source,
        items,
        at: 0,
    };
    r.expect_word("create")?;
    if r.eat_word("or") {
        r.expect_word("replace")?;
    }
    r.expect_word("semantic")?;
    r.expect_word("view")?;
    if r.eat_word("if") {
        r.expect_word("not")?;
        r.expect_word("exists")?;
    }
    let name = r.dotted()?.pop().unwrap_or_default();
    let mut doc = Doc::new("snowflake", &name, "snowflake");
    while let Some(Tok::Word(w)) = r.peek() {
        if w == ";" {
            break;
        }
        let clause_name = w.to_ascii_lowercase();
        r.at += 1;
        match clause_name.as_str() {
            "tables" => clause(&mut r, |r| table(r, &mut doc))?,
            "relationships" => clause(&mut r, |r| relationship(r, &mut doc))?,
            "facts" => clause(&mut r, |r| field(r, &mut doc, FieldKind::Fact))?,
            "dimensions" => clause(&mut r, |r| field(r, &mut doc, FieldKind::Dimension))?,
            "metrics" => clause(&mut r, |r| metric(r, &mut doc))?,
            "comment" => {
                r.eat(&Tok::Equals);
                doc.description = Some(r.text()?);
            }
            "ai_verified_queries" => clause(&mut r, |r| verified(r, &mut doc))?,
            "ai_sql_generation" | "ai_question_categorization" => {
                r.text()?;
            }
            "copy" => {
                r.expect_word("grants")?;
            }
            "with" => {
                r.expect_word("extension")?;
                r.expect(&Tok::Open, "(")?;
                r.skip_group();
                r.expect(&Tok::Close, ")")?;
            }
            other => return Err(format!("unknown clause {}", other.to_uppercase())),
        }
    }
    r.eat(&Tok::Word(";".into()));
    Ok(doc)
}

fn table(r: &mut Reader, doc: &mut Doc) -> Result<(), String> {
    // `[alias AS] table`: without an alias the table is called by its own name.
    let first = r.dotted()?;
    let (name, source) = if r.eat_word("as") {
        (
            first.last().cloned().unwrap_or_default(),
            r.dotted()?.join("."),
        )
    } else {
        (first.last().cloned().unwrap_or_default(), first.join("."))
    };
    let mut table = Table::new(&name, &source);
    loop {
        if r.eat_word("primary") {
            r.expect_word("key")?;
            table.primary_key = r.names()?;
        } else if r.eat_word("unique") {
            table.unique.push(r.names()?);
        } else if r.word_is(0, "constraint") {
            // CONSTRAINT name DISTINCT RANGE BETWEEN a AND b: a time range, not a key.
            while !matches!(r.peek(), Some(Tok::Comma | Tok::Close) | None) {
                r.at += 1;
            }
        } else {
            break;
        }
    }
    let found = modifiers(r)?;
    table.synonyms = found.synonyms;
    table.description = found.comment;
    doc.tables.push(table);
    Ok(())
}

fn relationship(r: &mut Reader, doc: &mut Doc) -> Result<(), String> {
    // `[name AS] from (columns) REFERENCES to [(columns)]`
    let first = r.name()?;
    let (name, from) = if r.eat_word("as") {
        (Some(first), r.name()?)
    } else {
        (None, first)
    };
    let from_columns = r.names()?;
    r.expect_word("references")?;
    let to = r.name()?;
    let to_columns = if r.peek() == Some(&Tok::Open) {
        r.names()?
    } else {
        Vec::new()
    };
    let name = name.unwrap_or_else(|| format!("{from}_{to}"));
    doc.relationships.push(Relationship {
        name,
        from,
        to,
        from_columns,
        to_columns,
        cardinality: None,
    });
    Ok(())
}

/// `[PRIVATE | PUBLIC] [table.]name AS expression [modifiers]`; the table is the one before the
/// dot, or none for a derived metric.
fn qualified(r: &mut Reader) -> Result<(Option<String>, String), String> {
    if !r.eat_word("public") {
        r.eat_word("private");
    }
    let first = r.name()?;
    if r.eat(&Tok::Dot) {
        Ok((Some(first), r.name()?))
    } else {
        Ok((None, first))
    }
}

fn field(r: &mut Reader, doc: &mut Doc, kind: FieldKind) -> Result<(), String> {
    let (table, name) = qualified(r)?;
    let table = table.ok_or_else(|| format!("{name} has no table before it"))?;
    r.expect_word("as")?;
    let expr = r.expression(STOPS)?;
    let found = modifiers(r)?;
    let Some(owner) = doc
        .tables
        .iter_mut()
        .find(|t| t.name.eq_ignore_ascii_case(&table))
    else {
        return Err(format!(
            "{table}.{name} names a table that is not in TABLES"
        ));
    };
    owner.fields.push(Field {
        name,
        expr,
        kind,
        synonyms: found.synonyms,
        description: found.comment,
    });
    Ok(())
}

/// `NON ADDITIVE BY (dimension [ASC | DESC] [NULLS FIRST | LAST], ...)`: the dimensions a
/// metric is not summed across, which its expression does not say.
fn non_additive(r: &mut Reader) -> Result<Vec<String>, String> {
    r.expect(&Tok::Open, "(")?;
    let mut dimensions = Vec::new();
    loop {
        dimensions.push(r.dotted()?.join("."));
        if !r.eat_word("asc") {
            r.eat_word("desc");
        }
        if r.eat_word("nulls") && !r.eat_word("first") {
            r.expect_word("last")?;
        }
        if !r.eat(&Tok::Comma) {
            break;
        }
    }
    r.expect(&Tok::Close, ")")?;
    Ok(dimensions)
}

fn metric(r: &mut Reader, doc: &mut Doc) -> Result<(), String> {
    let (table, name) = qualified(r)?;
    let unsupported = if r.word_is(0, "non") && r.word_is(1, "additive") {
        r.at += 2;
        r.expect_word("by")?;
        let dimensions = non_additive(r)?;
        Some(format!(
            "it is non-additive by {}, which its expression does not hold",
            dimensions.join(", ")
        ))
    } else {
        None
    };
    r.expect_word("as")?;
    let expr = r.expression(STOPS)?;
    let found = modifiers(r)?;
    doc.metrics.push(Metric {
        name,
        table,
        expr,
        synonyms: found.synonyms,
        description: found.comment,
        unsupported,
    });
    Ok(())
}

fn verified(r: &mut Reader, doc: &mut Doc) -> Result<(), String> {
    let name = r.name()?;
    r.expect_word("as")?;
    r.expect(&Tok::Open, "(")?;
    let mut query = Verified {
        name,
        ..Verified::default()
    };
    loop {
        if r.eat_word("question") {
            query.question = Some(r.text()?);
        } else if r.eat_word("sql") {
            query.sql = Some(r.text()?);
        } else if r.eat_word("verified_at") {
            query.verified_at = Some(match r.peek() {
                Some(Tok::Word(w)) => w.clone(),
                _ => return Err(format!("expected a timestamp near {}", r.near())),
            });
            r.at += 1;
        } else if r.eat_word("verified_by") {
            query.verified_by = Some(r.text()?);
        } else if r.eat_word("onboarding_question") {
            // A flag with no value.
        } else {
            break;
        }
    }
    r.expect(&Tok::Close, ")")?;
    doc.verified_queries.push(query);
    Ok(())
}
