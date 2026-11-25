//! The columns of a Parquet file: the top-level fields of the schema in its footer, read from
//! the Thrift compact encoding of `FileMetaData` with only what the schema needs, and typed as
//! DuckDB types them.
//!
//! A file ends with the footer, its length in four little-endian bytes and `PAR1`. The footer is
//! a `FileMetaData` struct whose field 2 is the schema: a list of `SchemaElement` in depth-first
//! order, the first being the root, whose `num_children` top-level fields follow, each with its
//! own children after it.

use std::fs::File;
use std::io::{Read, Seek, SeekFrom};

use super::Column;

/// The largest footer read: the schema of a file of thousands of columns fits many times over.
const MAX_FOOTER: u64 = 16 << 20;
/// The most schema elements read, however many the footer says it holds: each takes memory, and a
/// footer of empty ones is as small as one byte for each.
const MAX_ELEMENTS: usize = 100_000;
/// How deep structs may nest in a value the reader passes over before it is taken for damage.
const MAX_DEPTH: usize = 64;

/// The columns of the Parquet file `file`, `len` bytes long; `None` when it is not one.
pub fn columns(file: &mut File, len: u64) -> Option<Vec<Column>> {
    if len < 12 {
        return None;
    }
    let mut tail = [0u8; 8];
    file.seek(SeekFrom::Start(len - 8)).ok()?;
    file.read_exact(&mut tail).ok()?;
    if &tail[4..] != b"PAR1" {
        return None;
    }
    let size = u64::from(u32::from_le_bytes(tail[..4].try_into().ok()?));
    if size == 0 || size > MAX_FOOTER || size + 8 > len {
        return None;
    }
    let mut footer = vec![0u8; usize::try_from(size).ok()?];
    file.seek(SeekFrom::Start(len - 8 - size)).ok()?;
    file.read_exact(&mut footer).ok()?;
    schema_columns(&footer)
}

/// The top-level columns of the schema in an encoded `FileMetaData`.
pub fn schema_columns(footer: &[u8]) -> Option<Vec<Column>> {
    let mut reader = Reader {
        bytes: footer,
        at: 0,
    };
    let elements = reader.file_metadata()?;
    let root = elements.first()?;
    let mut found = Vec::new();
    let mut at = 1;
    for _ in 0..root.children {
        let element = elements.get(at)?;
        found.push((element.name.clone(), type_of(&elements, at)?));
        at = after(&elements, at)?;
    }
    Some(found)
}

/// One `SchemaElement`, with the fields the columns need.
#[derive(Default, Debug)]
struct Element {
    physical: Option<i32>,
    name: String,
    children: usize,
    converted: Option<i32>,
    scale: i32,
    precision: i32,
    /// The field of the `LogicalType` union that is set.
    logical: Option<i16>,
}

/// The index after the element at `at` and everything under it, found without recursion: a
/// schema may nest as deep as it has elements.
fn after(elements: &[Element], at: usize) -> Option<usize> {
    let mut next = at;
    let mut pending = 1usize;
    while pending > 0 {
        pending = pending
            .checked_sub(1)?
            .checked_add(elements.get(next)?.children)?;
        next += 1;
    }
    Some(next)
}

/// The DuckDB type of the element at `at`.
fn type_of(elements: &[Element], at: usize) -> Option<String> {
    let element = elements.get(at)?;
    if element.children > 0 {
        let kind = match (element.converted, element.logical) {
            (Some(3), _) | (_, Some(3)) => "LIST",
            (Some(1 | 2), _) | (_, Some(2)) => "MAP",
            _ => "STRUCT",
        };
        return Some(kind.to_owned());
    }
    let named = match (element.converted, element.logical) {
        (Some(0 | 4 | 19), _) | (_, Some(1 | 4 | 12)) => "VARCHAR",
        (Some(5), _) | (_, Some(5)) => {
            return Some(format!("DECIMAL({},{})", element.precision, element.scale));
        }
        (Some(6), _) | (_, Some(6)) => "DATE",
        (Some(7 | 8), _) | (_, Some(7)) => "TIME",
        (Some(9 | 10), _) | (_, Some(8)) => "TIMESTAMP",
        (Some(11), _) => "UTINYINT",
        (Some(12), _) => "USMALLINT",
        (Some(13), _) => "UINTEGER",
        (Some(14), _) => "UBIGINT",
        (Some(15), _) => "TINYINT",
        (Some(16), _) => "SMALLINT",
        (Some(17), _) => "INTEGER",
        (Some(18), _) => "BIGINT",
        (Some(20), _) => "BLOB",
        (Some(21), _) => "INTERVAL",
        (_, Some(14)) => "UUID",
        _ => match element.physical {
            Some(0) => "BOOLEAN",
            Some(1) => "INTEGER",
            Some(2) => "BIGINT",
            Some(3) => "TIMESTAMP",
            Some(4) => "FLOAT",
            Some(5) => "DOUBLE",
            Some(6 | 7) => "BLOB",
            _ => "",
        },
    };
    Some(named.to_owned())
}

/// Thrift's compact protocol, read as far as a schema needs.
struct Reader<'a> {
    bytes: &'a [u8],
    at: usize,
}

// Compact protocol type codes.
const STOP: u8 = 0;
const TRUE: u8 = 1;
const FALSE: u8 = 2;
const BYTE: u8 = 3;
const I16: u8 = 4;
const I32: u8 = 5;
const I64: u8 = 6;
const DOUBLE: u8 = 7;
const BINARY: u8 = 8;
const LIST: u8 = 9;
const SET: u8 = 10;
const MAP: u8 = 11;
const STRUCT: u8 = 12;

impl Reader<'_> {
    fn byte(&mut self) -> Option<u8> {
        let byte = *self.bytes.get(self.at)?;
        self.at += 1;
        Some(byte)
    }

    fn varint(&mut self) -> Option<u64> {
        let mut value = 0u64;
        for shift in (0..64).step_by(7) {
            let byte = self.byte()?;
            value |= u64::from(byte & 0x7f) << shift;
            if byte & 0x80 == 0 {
                return Some(value);
            }
        }
        None
    }

    fn zigzag(&mut self) -> Option<i64> {
        let raw = self.varint()?;
        Some((raw >> 1) as i64 ^ -((raw & 1) as i64))
    }

    fn i32(&mut self) -> Option<i32> {
        i32::try_from(self.zigzag()?).ok()
    }

    fn binary(&mut self) -> Option<&[u8]> {
        let len = usize::try_from(self.varint()?).ok()?;
        let end = self.at.checked_add(len)?;
        let bytes = self.bytes.get(self.at..end)?;
        self.at = end;
        Some(bytes)
    }

    /// A field header: its type and id, or `None` at the struct's end (and at a damaged one).
    fn field(&mut self, last: &mut i16) -> Option<Option<(u8, i16)>> {
        let header = self.byte()?;
        let kind = header & 0x0f;
        if kind == STOP {
            return Some(None);
        }
        let delta = header >> 4;
        let id = if delta == 0 {
            i16::try_from(self.zigzag()?).ok()?
        } else {
            last.checked_add(i16::from(delta))?
        };
        *last = id;
        Some(Some((kind, id)))
    }

    /// A list or set header: its element type and size.
    fn list_header(&mut self) -> Option<(u8, usize)> {
        let header = self.byte()?;
        let size = if header >> 4 == 15 {
            usize::try_from(self.varint()?).ok()?
        } else {
            usize::from(header >> 4)
        };
        // No element takes less than a byte, so a size beyond the bytes left is damage.
        (size <= self.bytes.len() - self.at).then_some((header & 0x0f, size))
    }

    /// An element of a list, set or map: as a field, but a boolean takes a byte of its own.
    fn skip_item(&mut self, kind: u8, depth: usize) -> Option<()> {
        if kind == TRUE || kind == FALSE {
            self.byte()?;
            Some(())
        } else {
            self.skip(kind, depth)
        }
    }

    fn skip(&mut self, kind: u8, depth: usize) -> Option<()> {
        if depth > MAX_DEPTH {
            return None;
        }
        match kind {
            TRUE | FALSE => {}
            BYTE => {
                self.byte()?;
            }
            I16 | I32 | I64 => {
                self.varint()?;
            }
            DOUBLE => {
                self.at = self.at.checked_add(8)?;
                if self.at > self.bytes.len() {
                    return None;
                }
            }
            BINARY => {
                self.binary()?;
            }
            LIST | SET => {
                let (element, size) = self.list_header()?;
                for _ in 0..size {
                    self.skip_item(element, depth + 1)?;
                }
            }
            MAP => {
                let size = usize::try_from(self.varint()?).ok()?;
                if size > 0 {
                    let kinds = self.byte()?;
                    // A key and a value take a byte at least, so more than the bytes left is damage.
                    if size > (self.bytes.len() - self.at) / 2 {
                        return None;
                    }
                    for _ in 0..size {
                        self.skip_item(kinds >> 4, depth + 1)?;
                        self.skip_item(kinds & 0x0f, depth + 1)?;
                    }
                }
            }
            STRUCT => {
                let mut last = 0;
                while let Some((kind, _)) = self.field(&mut last)? {
                    self.skip(kind, depth + 1)?;
                }
            }
            _ => return None,
        }
        Some(())
    }

    /// The schema of a `FileMetaData`, which stops being read once it is.
    fn file_metadata(&mut self) -> Option<Vec<Element>> {
        let mut last = 0;
        while let Some((kind, id)) = self.field(&mut last)? {
            if id == 2 && kind == LIST {
                let (element, size) = self.list_header()?;
                if element != STRUCT || size > MAX_ELEMENTS {
                    return None;
                }
                return (0..size).map(|_| self.element()).collect();
            }
            self.skip(kind, 0)?;
        }
        None
    }

    fn element(&mut self) -> Option<Element> {
        let mut element = Element::default();
        let mut last = 0;
        while let Some((kind, id)) = self.field(&mut last)? {
            match (id, kind) {
                (1, I32) => element.physical = Some(self.i32()?),
                (4, BINARY) => element.name = String::from_utf8(self.binary()?.to_vec()).ok()?,
                (5, I32) => element.children = usize::try_from(self.i32()?).ok()?,
                (6, I32) => element.converted = Some(self.i32()?),
                (7, I32) => element.scale = self.i32()?,
                (8, I32) => element.precision = self.i32()?,
                (10, STRUCT) => {
                    let mut inner = 0;
                    while let Some((kind, id)) = self.field(&mut inner)? {
                        element.logical.get_or_insert(id);
                        self.skip(kind, 1)?;
                    }
                }
                _ => self.skip(kind, 0)?,
            }
        }
        Some(element)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A writer of the compact encoding, enough to make schemas to read.
    #[derive(Default)]
    struct Writer {
        bytes: Vec<u8>,
        last: Vec<i16>,
    }

    impl Writer {
        fn varint(&mut self, mut value: u64) {
            loop {
                let byte = (value & 0x7f) as u8;
                value >>= 7;
                if value == 0 {
                    self.bytes.push(byte);
                    return;
                }
                self.bytes.push(byte | 0x80);
            }
        }

        fn header(&mut self, id: i16, kind: u8) {
            let delta = id - *self.last.last().unwrap();
            if (1..=15).contains(&delta) {
                self.bytes.push(((delta as u8) << 4) | kind);
            } else {
                self.bytes.push(kind);
                self.varint(((i64::from(id) << 1) ^ (i64::from(id) >> 63)) as u64);
            }
            *self.last.last_mut().unwrap() = id;
        }

        fn i32(&mut self, id: i16, value: i32) {
            self.header(id, I32);
            self.varint(((i64::from(value) << 1) ^ (i64::from(value) >> 63)) as u64);
        }

        fn string(&mut self, id: i16, value: &str) {
            self.header(id, BINARY);
            self.varint(value.len() as u64);
            self.bytes.extend_from_slice(value.as_bytes());
        }

        fn begin(&mut self) {
            self.last.push(0);
        }

        fn end(&mut self) {
            self.bytes.push(STOP);
            self.last.pop();
        }
    }

    /// `(name, physical, converted, children)` for each element, root first.
    fn footer(elements: &[(&str, Option<i32>, Option<i32>, i32)]) -> Vec<u8> {
        let mut writer = Writer::default();
        writer.begin();
        writer.i32(1, 2);
        writer.header(2, LIST);
        writer.bytes.push(0xf0 | STRUCT);
        writer.varint(elements.len() as u64);
        for (name, physical, converted, children) in elements {
            writer.begin();
            if let Some(physical) = physical {
                writer.i32(1, *physical);
            }
            // A field this reader does not need, to be passed over.
            writer.i32(3, 1);
            writer.string(4, name);
            if *children > 0 {
                writer.i32(5, *children);
            }
            if let Some(converted) = converted {
                writer.i32(6, *converted);
            }
            writer.end();
        }
        writer.i32(3, 100);
        writer.end();
        writer.bytes
    }

    #[test]
    fn the_top_level_fields_are_the_columns() {
        let bytes = footer(&[
            ("duckdb_schema", None, None, 4),
            ("country", Some(6), Some(0), 0),
            ("amount", Some(5), None, 0),
            ("address", None, None, 2),
            ("city", Some(6), Some(0), 0),
            ("zip", Some(1), None, 0),
            ("day", Some(1), Some(6), 0),
        ]);
        let found = schema_columns(&bytes).unwrap();
        assert_eq!(
            found,
            [
                ("country".into(), "VARCHAR".into()),
                ("amount".into(), "DOUBLE".into()),
                ("address".into(), "STRUCT".into()),
                ("day".into(), "DATE".into()),
            ]
        );
    }

    #[test]
    fn a_damaged_footer_gives_no_columns() {
        let bytes = footer(&[("root", None, None, 2), ("a", Some(1), None, 0)]);
        assert!(schema_columns(&bytes).is_none());
        for end in 0..bytes.len() {
            let _ = schema_columns(&bytes[..end]);
        }
        assert!(schema_columns(&[0x19, 0xfc, 0xff, 0xff, 0xff, 0x0f]).is_none());
    }

    /// Runs `f` on a thread of the default stack, and fails the test if it takes too long.
    fn within<T: Send + 'static>(f: impl FnOnce() -> T + Send + 'static) -> T {
        let (sender, receiver) = std::sync::mpsc::channel();
        std::thread::spawn(move || {
            let _ = sender.send(f());
        });
        receiver
            .recv_timeout(std::time::Duration::from_secs(20))
            .expect("took more than 20 seconds")
    }

    fn varint_bytes(mut value: u64) -> Vec<u8> {
        let mut out = Vec::new();
        loop {
            let byte = (value & 0x7f) as u8;
            value >>= 7;
            if value == 0 {
                out.push(byte);
                return out;
            }
            out.push(byte | 0x80);
        }
    }

    /// A footer whose schema is `count` elements, each given by `element`, then `tail`.
    fn schema_of(count: u64, element: &[u8], last: &[u8], tail: &[u8]) -> Vec<u8> {
        let mut bytes = vec![0x29, 0xf0 | STRUCT];
        bytes.extend(varint_bytes(count));
        for _ in 1..count {
            bytes.extend_from_slice(element);
        }
        bytes.extend_from_slice(last);
        bytes.extend_from_slice(tail);
        bytes.push(STOP);
        bytes
    }

    #[test]
    fn a_map_of_booleans_with_a_huge_size_is_damage_not_a_loop() {
        // Field 1, a map of 2^62 entries whose keys and values are booleans.
        let mut bytes = vec![0x1b];
        bytes.extend(varint_bytes(1 << 62));
        bytes.extend([0x11]);
        assert!(within(move || schema_columns(&bytes)).is_none());
        // A real one: booleans in a map take a byte each, as in a list.
        let mut small = vec![0x1b, 0x02, 0x11, 0x01, 0x02, 0x01, 0x02];
        small.extend([0x19, 0xf0 | STRUCT, 0x01, STOP, STOP]);
        assert_eq!(schema_columns(&small).unwrap(), []);
    }

    #[test]
    fn a_collection_larger_than_the_bytes_left_is_damage() {
        let mut list = vec![0x19, 0xf0 | TRUE];
        list.extend(varint_bytes(1 << 40));
        assert!(within(move || schema_columns(&list)).is_none());
        let mut set = vec![0x1a, 0xf0 | STRUCT];
        set.extend(varint_bytes(1 << 40));
        assert!(within(move || schema_columns(&set)).is_none());
    }

    #[test]
    fn a_schema_nested_a_hundred_thousand_deep_is_read_without_the_stack() {
        // Each element has one child: field 5 (num_children) = 1.
        let bytes = schema_of(100_000, &[0x55, 0x02, STOP], &[STOP], &[]);
        let found = within(move || schema_columns(&bytes)).unwrap();
        assert_eq!(found, [(String::new(), "STRUCT".to_owned())]);
    }

    #[test]
    fn a_schema_of_too_many_elements_gives_no_columns() {
        let bytes = schema_of(300_000, &[STOP], &[STOP], &[]);
        assert!(within(move || schema_columns(&bytes)).is_none());
        let bytes = schema_of(2_000, &[STOP], &[STOP], &[]);
        assert!(within(move || schema_columns(&bytes)).is_some());
    }

    #[test]
    fn a_footer_larger_than_the_cap_is_not_read() {
        use std::io::Write;
        let path =
            std::env::temp_dir().join(format!("konstruct-footer-{}.parquet", std::process::id()));
        // A valid schema of the root alone, then a created_by string that fills the footer.
        let size = 17 << 20;
        let mut footer = schema_of(1, &[], &[STOP], &[0x48]);
        footer.pop();
        footer.extend(varint_bytes((size - 64) as u64));
        footer.resize(size, b'x');
        let mut file = File::create(&path).unwrap();
        file.write_all(b"PAR1").unwrap();
        file.write_all(&footer).unwrap();
        file.write_all(&(footer.len() as u32).to_le_bytes())
            .unwrap();
        file.write_all(b"PAR1").unwrap();
        drop(file);
        let mut file = File::open(&path).unwrap();
        let len = file.metadata().unwrap().len();
        let found = columns(&mut file, len);
        let _ = std::fs::remove_file(&path);
        assert!(found.is_none());
    }

    proptest::proptest! {
        #[test]
        fn no_bytes_make_the_reader_panic_or_run_long(bytes in proptest::collection::vec(proptest::prelude::any::<u8>(), 0..512)) {
            let _ = schema_columns(&bytes);
        }

        #[test]
        fn a_footer_with_one_byte_changed_is_read_or_refused(at in 0usize..200, byte in proptest::prelude::any::<u8>()) {
            let mut bytes = footer(&[
                ("duckdb_schema", None, None, 3),
                ("country", Some(6), Some(0), 0),
                ("address", None, None, 1),
                ("city", Some(6), Some(0), 0),
            ]);
            if at < bytes.len() {
                bytes[at] = byte;
            }
            let _ = schema_columns(&bytes);
        }
    }
}
