//! The columns of a CSV file, from its first record, named as DuckDB's reader names them: a field
//! with no name is `column<i>`, a name met again gets `_1`, `_2`, ... after it, and a file read
//! without a header has `column0`, `column1`, ...

use std::collections::{HashMap, HashSet};

use super::Column;

/// How much of a file is read for its first record.
pub const MAX_HEAD: usize = 1 << 20;
/// The most fields a header has, and the most bytes it takes, before the file is taken for no CSV.
pub const MAX_FIELDS: usize = 20_000;
pub const MAX_RECORD: usize = 1 << 18;

/// The delimiters a file is read with when none is given, in the order a tie is broken.
const CANDIDATES: [u8; 4] = *b",\t;|";

/// The columns of the CSV text that starts with `head`; `None` when it has no complete first
/// record, or that record is not text.
pub fn columns(head: &[u8], delimiter: Option<u8>, header: bool) -> Option<Vec<Column>> {
    let head = head.strip_prefix(b"\xef\xbb\xbf").unwrap_or(head);
    let line = first_record(head)?;
    if line.len() > MAX_RECORD {
        return None;
    }
    let delimiter = delimiter.unwrap_or_else(|| sniff(line));
    let fields = fields(line, delimiter)?;
    if fields.is_empty() || (fields.len() == 1 && fields[0].is_empty()) || fields.len() > MAX_FIELDS
    {
        return None;
    }
    // The name of each field, as DuckDB gives it: without control characters, which no column of
    // a table has and a terminal or a log would act on.
    let names = fields.into_iter().enumerate().map(|(i, field)| {
        let field: String = field.chars().filter(|c| !c.is_control()).collect();
        if !header || field.is_empty() {
            format!("column{i}")
        } else {
            field
        }
    });
    // The numbered forms taken so far, in lower case, and how far each name has been numbered, so
    // that a name met again costs the numbers it has not tried, not a scan of every name before it.
    let mut taken: HashSet<String> = HashSet::new();
    let mut counts: HashMap<String, usize> = HashMap::new();
    let mut found = Vec::new();
    for name in names {
        let mut unique = name.clone();
        let count = counts.entry(name.to_ascii_lowercase()).or_insert(0);
        while !taken.insert(unique.to_ascii_lowercase()) {
            *count += 1;
            unique = format!("{name}_{count}");
        }
        found.push((unique, String::new()));
    }
    Some(found)
}

/// The bytes of the first record, without its line end: a line end inside quotes belongs to the
/// record. `None` when the record does not end within `head` and `head` is a cut-off prefix.
fn first_record(head: &[u8]) -> Option<&[u8]> {
    let mut quoted = false;
    for (at, &byte) in head.iter().enumerate() {
        match byte {
            b'"' => quoted = !quoted,
            b'\n' | b'\r' if !quoted => return Some(&head[..at]),
            _ => {}
        }
    }
    (head.len() < MAX_HEAD && !quoted).then_some(head)
}

/// The candidate delimiter that occurs most often outside quotes in `line`.
fn sniff(line: &[u8]) -> u8 {
    let mut counts = [0usize; CANDIDATES.len()];
    let mut quoted = false;
    for &byte in line {
        if byte == b'"' {
            quoted = !quoted;
        } else if !quoted && let Some(i) = CANDIDATES.iter().position(|&c| c == byte) {
            counts[i] += 1;
        }
    }
    let mut best = 0;
    for i in 1..CANDIDATES.len() {
        if counts[i] > counts[best] {
            best = i;
        }
    }
    CANDIDATES[best]
}

/// The fields of one record: a quoted field may hold the delimiter and `""` for a quote.
fn fields(line: &[u8], delimiter: u8) -> Option<Vec<String>> {
    let mut found = Vec::new();
    let mut field = Vec::new();
    let mut quoted = false;
    let mut at = 0;
    while at < line.len() {
        let byte = line[at];
        if quoted {
            if byte == b'"' {
                if line.get(at + 1) == Some(&b'"') {
                    field.push(b'"');
                    at += 1;
                } else {
                    quoted = false;
                }
            } else {
                field.push(byte);
            }
        } else if byte == b'"' {
            quoted = true;
        } else if byte == delimiter {
            found.push(String::from_utf8(std::mem::take(&mut field)).ok()?);
        } else {
            field.push(byte);
        }
        at += 1;
    }
    found.push(String::from_utf8(field).ok()?);
    Some(found.into_iter().map(|f| f.trim().to_owned()).collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn names(head: &[u8], delimiter: Option<u8>, header: bool) -> Vec<String> {
        columns(head, delimiter, header)
            .unwrap()
            .into_iter()
            .map(|(n, _)| n)
            .collect()
    }

    #[test]
    fn the_header_names_the_columns_as_duckdb_does() {
        assert_eq!(
            names(b"a,b,,a\n1,2,3,4\n", None, true),
            ["a", "b", "column2", "a_1"]
        );
        assert_eq!(names(b"a,b\r\n1,2\r\n", None, true), ["a", "b"]);
        assert_eq!(names(b"\xef\xbb\xbfid;name\n", None, true), ["id", "name"]);
        assert_eq!(names(b"x|y|z", None, true), ["x", "y", "z"]);
        assert_eq!(names(b"x\ty", None, true), ["x", "y"]);
        assert_eq!(names(b"a,b", None, false), ["column0", "column1"]);
        assert_eq!(names(b"a;b,c", Some(b';'), true), ["a", "b,c"]);
    }

    #[test]
    fn a_quoted_name_may_hold_the_delimiter_and_a_quote() {
        assert_eq!(
            names(
                b"\"Station Name\",\"a,b\",\"say \"\"hi\"\"\"\n1,2,3",
                None,
                true
            ),
            ["Station Name", "a,b", "say \"hi\""]
        );
    }

    #[test]
    fn no_record_gives_no_columns() {
        assert!(columns(b"", None, true).is_none());
        assert!(columns(b"\n", None, true).is_none());
        assert!(columns(b"\"open", None, true).is_none());
        assert!(columns(b"\xff\xfe,a\n", None, true).is_none());
    }

    fn within<T: Send + 'static>(f: impl FnOnce() -> T + Send + 'static) -> T {
        let (sender, receiver) = std::sync::mpsc::channel();
        std::thread::spawn(move || {
            let _ = sender.send(f());
        });
        receiver
            .recv_timeout(std::time::Duration::from_secs(20))
            .expect("took more than 20 seconds")
    }

    #[test]
    fn thousands_of_repeated_names_are_numbered_without_a_scan_for_each() {
        let mut head = vec![b'a'; 1];
        for _ in 1..4_000 {
            head.extend_from_slice(b",a");
        }
        head.push(b'\n');
        let found = within(move || names(&head, None, true));
        assert_eq!(found.len(), 4_000);
        assert_eq!(&found[..3], ["a", "a_1", "a_2"]);
        assert_eq!(found[3_999], "a_3999");
        // A name that is another's numbered form is numbered in its turn.
        assert_eq!(
            names(b"a,a,a_1,a_1\n", None, true),
            ["a", "a_1", "a_1_1", "a_1_2"]
        );
        assert_eq!(names(b"a,A,a\n", None, true), ["a", "A_1", "a_2"]);
    }

    #[test]
    fn a_header_with_too_many_fields_or_bytes_gives_no_columns() {
        let wide: Vec<u8> = (0..MAX_FIELDS + 1)
            .map(|i| format!("c{i}"))
            .collect::<Vec<_>>()
            .join(",")
            .into_bytes();
        assert!(within(move || columns(&wide, None, true)).is_none());
        let mut long = vec![b'x'; MAX_RECORD + 1];
        long.push(b'\n');
        assert!(columns(&long, None, true).is_none());
        let fine: Vec<u8> = (0..MAX_FIELDS)
            .map(|i| format!("c{i}"))
            .collect::<Vec<_>>()
            .join(",")
            .into_bytes();
        assert_eq!(names(&fine, None, true).len(), MAX_FIELDS);
    }

    #[test]
    fn control_bytes_are_not_part_of_a_name() {
        assert_eq!(
            names(b"a\x00b,c\x07d,\x01\n", None, true),
            ["ab", "cd", "column2"]
        );
        assert_eq!(
            names(b"\"two\nlines\",ok\n", None, true),
            ["twolines", "ok"]
        );
        assert_eq!(names(b"a\x1b[31mred,b\n", None, true), ["a[31mred", "b"]);
    }

    proptest::proptest! {
        #[test]
        fn no_bytes_make_the_reader_panic(bytes in proptest::collection::vec(proptest::prelude::any::<u8>(), 0..2048), delimiter in proptest::option::of(proptest::prelude::any::<u8>()), header in proptest::prelude::any::<bool>()) {
            let _ = columns(&bytes, delimiter, header);
        }
    }
}
