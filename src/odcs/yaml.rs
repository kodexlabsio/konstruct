//! Writing the ODCS contracts as YAML.
//!
//! The values are a small tree of our own ([`Yaml`]), built in a fixed key order. A scalar is written plain only when it is a word that no YAML 1.1 or 1.2 reader
//! resolves to a number, a date, a flag or null, and otherwise in double quotes with every
//! character a reader could treat as a break or reject escaped. Reading is the Python side's
//! (`konstruct.core.yamlio`), which refuses a document whose aliases stand for too much.

/// A value of a contract: text, a number, a flag, a sequence or a mapping that keeps the order its
/// keys were put in.
#[derive(Debug, Clone, PartialEq)]
pub enum Yaml {
    String(String),
    Integer(i64),
    Boolean(bool),
    Array(Vec<Yaml>),
    Hash(Vec<(String, Yaml)>),
}

/// A YAML string.
pub fn text(value: &str) -> Yaml {
    Yaml::String(value.to_owned())
}

/// A YAML sequence of strings.
pub fn texts<S: AsRef<str>>(values: &[S]) -> Yaml {
    Yaml::Array(values.iter().map(|v| text(v.as_ref())).collect())
}

/// A mapping that keeps the order its keys were put in, so the output is stable.
#[derive(Default)]
pub struct Map(Vec<(String, Yaml)>);

impl Map {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn put(&mut self, key: &str, value: Yaml) -> &mut Self {
        match self.0.iter_mut().find(|(k, _)| k == key) {
            Some(entry) => entry.1 = value,
            None => self.0.push((key.to_owned(), value)),
        }
        self
    }

    pub fn put_text(&mut self, key: &str, value: &str) -> &mut Self {
        self.put(key, text(value))
    }

    /// Puts `value` unless it is absent.
    pub fn put_some(&mut self, key: &str, value: Option<&str>) -> &mut Self {
        if let Some(value) = value {
            self.put_text(key, value);
        }
        self
    }

    pub fn put_flag(&mut self, key: &str, value: Option<bool>) -> &mut Self {
        if let Some(value) = value {
            self.put(key, Yaml::Boolean(value));
        }
        self
    }

    /// Puts a sequence unless it is empty.
    pub fn put_list(&mut self, key: &str, items: Vec<Yaml>) -> &mut Self {
        if !items.is_empty() {
            self.put(key, Yaml::Array(items));
        }
        self
    }

    pub fn build(&self) -> Yaml {
        Yaml::Hash(self.0.clone())
    }
}

/// Words that a YAML 1.1 or 1.2 reader resolves to a flag, null or a special number.
const RESERVED: &[&str] = &[
    "true", "false", "yes", "no", "on", "off", "y", "n", "null", "nan", "inf", "infinity",
];

/// Whether `value` can be written unquoted: it starts with a letter or `_`, holds only letters,
/// digits, `_`, `-` and `.`, and is not a reserved word. Nothing so written can read as a number
/// (those start with a digit, a sign or a dot), a date, an indicator or a tag.
fn plain(value: &str) -> bool {
    let mut chars = value.chars();
    chars
        .next()
        .is_some_and(|c| c.is_ascii_alphabetic() || c == '_')
        && chars.all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '-' | '.'))
        && !RESERVED.iter().any(|w| value.eq_ignore_ascii_case(w))
}

/// Whether YAML allows `c` in a document unescaped, and no reader takes it for a line break.
fn printable(c: char) -> bool {
    matches!(c, ' '..='~' | '\u{A0}'..='\u{D7FF}' | '\u{E000}'..='\u{FFFD}' | '\u{10000}'..)
        && !matches!(c, '\u{2028}' | '\u{2029}' | '\u{FEFF}')
}

fn quoted(value: &str, out: &mut String) {
    out.push('"');
    for c in value.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if printable(c) => out.push(c),
            c if (c as u32) <= 0xFF => out.push_str(&format!("\\x{:02X}", c as u32)),
            c if (c as u32) <= 0xFFFF => out.push_str(&format!("\\u{:04X}", c as u32)),
            c => out.push_str(&format!("\\U{:08X}", c as u32)),
        }
    }
    out.push('"');
}

fn scalar(value: &str, out: &mut String) {
    if plain(value) {
        out.push_str(value);
    } else {
        quoted(value, out);
    }
}

fn write(node: &Yaml, indent: usize, out: &mut String) -> Result<(), String> {
    match node {
        Yaml::Hash(map) => {
            for (i, (key, value)) in map.iter().enumerate() {
                if i > 0 || indent > 0 && !out.ends_with("- ") {
                    out.push_str(&" ".repeat(indent));
                }
                scalar(key, out);
                out.push(':');
                match value {
                    Yaml::Hash(_) | Yaml::Array(_) => {
                        out.push('\n');
                        write(value, indent + 2, out)?;
                    }
                    _ => {
                        out.push(' ');
                        write(value, indent, out)?;
                        out.push('\n');
                    }
                }
            }
        }
        Yaml::Array(items) => {
            for item in items {
                out.push_str(&" ".repeat(indent));
                out.push_str("- ");
                match item {
                    Yaml::Hash(_) => write(item, indent + 2, out)?,
                    Yaml::Array(_) => return Err("a sequence in a sequence".into()),
                    _ => {
                        write(item, indent, out)?;
                        out.push('\n');
                    }
                }
            }
        }
        Yaml::String(value) => scalar(value, out),
        Yaml::Integer(value) => out.push_str(&value.to_string()),
        Yaml::Boolean(value) => out.push_str(if *value { "true" } else { "false" }),
    }
    Ok(())
}

/// `document`, a mapping, as YAML text starting with `---`.
pub fn emit(document: &Yaml) -> Result<String, String> {
    let mut out = String::from("---\n");
    write(document, 0, &mut out)?;
    Ok(out)
}

/// `text` as one line of a `#` comment: every line break (and any control character) a reader
/// could end the comment at becomes a space, so what is written there cannot become a key.
pub fn comment(text: &str) -> String {
    text.chars()
        .map(|c| if c == '\t' || printable(c) { c } else { ' ' })
        .collect()
}

/// What the tests look at the emitter's output with: the core's own YAML reader
/// (`crate::yaml`) composes it, and these walk the nodes. PyYAML and the ODCS JSON Schema, in
/// `tests/test_odcs.py`, are the independent checks that what is written is YAML.
#[cfg(test)]
pub(crate) mod inspect {
    use crate::yaml::Limits;
    use crate::yaml::compose::{Body, Composer, Document, NodeId};
    use crate::yaml::resolve::Tag;

    /// The documents of a stream, composed by the core's reader.
    pub fn read(source: &str) -> Result<Vec<Document<'_>>, String> {
        let mut composer = Composer::new(source, Limits::DEFAULT, true);
        let mut documents = Vec::new();
        while let Some(document) = composer.next_document().map_err(|f| format!("{f:?}"))? {
            documents.push(document);
        }
        Ok(documents)
    }

    /// A node of a document.
    #[derive(Clone, Copy)]
    pub struct View<'d, 'a> {
        document: &'d Document<'a>,
        id: NodeId,
    }

    impl<'d, 'a> View<'d, 'a> {
        pub fn root(document: &'d Document<'a>) -> Self {
            Self {
                document,
                id: document.root,
            }
        }

        fn at(&self, id: NodeId) -> Self {
            Self {
                document: self.document,
                id,
            }
        }

        fn body(&self) -> &'d Body<'a> {
            &self.document.nodes[self.id as usize].body
        }

        /// The text of a string scalar: a plain word the reader takes for a number or a flag is
        /// not one.
        pub fn as_str(&self) -> Option<&'d str> {
            match (self.body(), &self.document.nodes[self.id as usize].tag) {
                (Body::Scalar(text), Tag::Str) => Some(text),
                _ => None,
            }
        }

        pub fn member(&self, key: &str) -> Option<Self> {
            let Body::Mapping(pairs) = self.body() else {
                return None;
            };
            pairs
                .iter()
                .find(|(k, _)| self.at(*k).as_str() == Some(key))
                .map(|(_, v)| self.at(*v))
        }

        /// The members of a sequence, none for anything else.
        pub fn items(&self) -> Vec<Self> {
            match self.body() {
                Body::Sequence(items) => items.iter().map(|i| self.at(*i)).collect(),
                _ => Vec::new(),
            }
        }

        /// How many entries a mapping has.
        pub fn entries(&self) -> usize {
            match self.body() {
                Body::Mapping(pairs) => pairs.len(),
                _ => 0,
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::inspect::{View, read};
    use super::*;
    use proptest::prelude::*;

    fn string_of(source: &str, key: &str) -> Option<String> {
        let documents = read(source).unwrap();
        View::root(&documents[0])
            .member(key)
            .and_then(|v| v.as_str().map(str::to_owned))
    }

    fn round_trip(value: &str) -> String {
        let mut map = Map::new();
        map.put_text("k", value);
        let written = emit(&map.build()).unwrap();
        string_of(&written, "k").unwrap_or_else(|| panic!("no string k in {written:?}"))
    }

    #[test]
    fn strings_that_look_like_other_types_read_back_as_strings() {
        for value in [
            "true",
            "false",
            "null",
            "~",
            "1.0",
            "1",
            "0x1f",
            "v3.2.0",
            "1.0.0",
            "yes",
            "no",
            "Yes",
            "ON",
            "1_000",
            "2024-01-01",
            "0b101",
            "0o17",
            "+.inf",
            "-.INF",
            ".nan",
            "1e3",
            "12:30:45",
            "=",
            "<<",
            "",
            " lead",
            "trail ",
            "a: b",
            "# not a comment",
            "- item",
            "[x]",
            "{y}",
            "it's",
            "say \"hi\"",
            "a\\b",
            "é",
            "'",
            "%",
            "@x",
            "*star",
            "&anchor",
            "!tag",
            "|",
            ">",
            "key: ",
            "a #b",
            "\u{7}bell",
            "\u{1b}esc",
            "\u{7f}del",
            "\u{9f}c1",
            "\u{85}nel",
            "a\u{2028}b",
            "a\u{2029}b",
            "a\rb",
            "a\tb",
            "  first\nsecond",
            "ends\n\n",
            "\nstarts",
            "\u{feff}bom",
            "\u{10000}astral",
            "\u{fffe}",
        ] {
            assert_eq!(round_trip(value), value, "{value:?}");
        }
    }

    #[test]
    fn words_are_plain_and_everything_else_is_quoted() {
        let mut map = Map::new();
        map.put_text("name", "db.main.customers")
            .put_text("version", "1.0.0")
            .put_text("x y", "true");
        assert_eq!(
            emit(&map.build()).unwrap(),
            "---\nname: db.main.customers\nversion: \"1.0.0\"\n\"x y\": \"true\"\n"
        );
    }

    #[test]
    fn keys_keep_the_order_they_were_put_in_and_nesting_is_indented() {
        let mut inner = Map::new();
        inner.put_text("name", "x").put_flag("unique", Some(true));
        let mut other = Map::new();
        other.put_text("a", "b").put_list("c", vec![text("d")]);
        let mut outer = Map::new();
        outer
            .put_text("zeta", "1")
            .put_list("schema", vec![inner.build(), other.build()])
            .put("alpha", other.build());
        assert_eq!(
            emit(&outer.build()).unwrap(),
            "---\nzeta: \"1\"\nschema:\n  - name: x\n    unique: true\n  - a: b\n    c:\n      - d\n\
             alpha:\n  a: b\n  c:\n    - d\n"
        );
        let written = emit(&outer.build()).unwrap();
        assert_eq!(read(&written).unwrap().len(), 1);
    }

    #[test]
    fn absent_values_are_left_out() {
        let mut map = Map::new();
        map.put_some("a", None)
            .put_flag("b", None)
            .put_list("c", Vec::new())
            .put_some("d", Some("x"))
            .put_flag("e", Some(false));
        assert_eq!(emit(&map.build()).unwrap(), "---\nd: x\ne: false\n");
    }

    #[test]
    fn a_comment_has_no_line_break_of_any_kind() {
        for brk in [
            "\n", "\r", "\r\n", "\u{85}", "\u{2028}", "\u{2029}", "\u{b}", "\u{c}",
        ] {
            let line = comment(&format!("x{brk}status: active"));
            assert!(
                !line.contains(|c: char| c != '\t' && !printable(c)),
                "{line:?}"
            );
            let text = format!("---\n# {line}\nname: n\n");
            assert_eq!(string_of(&text, "status"), None, "{brk:?}");
        }
    }

    proptest! {
        #[test]
        fn any_text_reads_back_as_itself(value in any::<String>(), key in any::<String>()) {
            let mut inner = Map::new();
            inner.put_text(&key, &value);
            let mut map = Map::new();
            map.put_text(&key, &value).put_list("items", vec![inner.build(), text(&value)]);
            let written = emit(&map.build()).unwrap();
            let documents = read(&written).unwrap();
            let top = View::root(&documents[0]);
            prop_assert_eq!(string_of(&written, &key), Some(value.clone()), "{:?}", written);
            let items = top.member("items").unwrap().items();
            prop_assert_eq!(
                items[0].member(&key).and_then(|v| v.as_str().map(str::to_owned)),
                Some(value.clone())
            );
            prop_assert_eq!(items[1].as_str(), Some(value.as_str()));
            prop_assert_eq!(top.entries(), 2);
        }
    }
}
