//! The columns of a data file a project ships: a Parquet or CSV file the repository tracks, that a
//! query reads with a table function (`read_parquet('data/sales/*.parquet')`).
//!
//! A pattern is a path from the repository's root, which may hold shell patterns in any of its
//! parts and `**` for any number of directories. It is matched against the names of the files the
//! repository tracks (`Tracked`, made once for a build), never against the file system, so a
//! file the repository does not hold is not reachable by any pattern and a walk of the tree costs
//! nothing: the names that start with the pattern's literal first parts are found by binary
//! search, and only those are matched. Of the files a pattern matches with the extension of the
//! format, the first in name order is read. The file is opened, then checked to be the one that
//! was looked at, a regular file under no link, so a link put in its place is not followed.

mod csv;
mod parquet;

#[cfg(feature = "python")]
pub(crate) mod python;

use std::fs;
use std::io::{Read, Seek, SeekFrom};
use std::path::{Component, Path, PathBuf};

use crate::discover::files::Identity;
use crate::discover::glob::Pattern;

/// How many files one pattern may match: the first in name order is the one read, so more are
/// no use, and a pattern such as `**/*.parquet` in a large repository returns a bounded list.
const MAX_ENTRIES: usize = 100_000;

/// The most parts a pattern may have, and the most bytes: the cost of matching a name grows with
/// both, and no path a project names comes near them.
const MAX_PARTS: usize = 64;
const MAX_PATTERN: usize = 1024;

/// How a data file is read.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Format {
    /// Parquet: the columns of the schema in the file's footer.
    Parquet,
    /// CSV: the first record's fields, or `column0`, `column1`, ... without a header; the
    /// delimiter is the one given, or the most frequent of `,`, tab, `;` and `|` in that record.
    Csv { delimiter: Option<u8>, header: bool },
}

/// A column: its name and its type as DuckDB names it, empty where the file does not say.
pub type Column = (String, String);

/// The files a repository tracks, as paths from its root with `/` between the parts, in name
/// order.
#[derive(Debug, Default)]
pub struct Tracked {
    names: Vec<String>,
}

/// Whether every part of a pattern is a plain name or a shell pattern: not empty, `.` or `..`, no
/// drive (`C:`), separator or NUL, whatever the platform reads a path as.
fn plain(parts: &[&str]) -> bool {
    parts.iter().all(|part| {
        let mut components = Path::new(part).components();
        matches!(
            (components.next(), components.next()),
            (Some(Component::Normal(_)), None)
        ) && !part.contains([':', '\\', '\0'])
    })
}

/// Whether a file system may reach `.git` by `name`: case-insensitive ones read `.GIT`, and
/// Windows drops trailing dots and spaces and has the short name `GIT~1` (and `GIT~2`, and so on,
/// where a second name takes the first's; the short forms of other names are not `.git`'s).
fn is_git(name: &str) -> bool {
    let name = name.trim_end_matches(['.', ' ']);
    name.eq_ignore_ascii_case(".git")
        || name
            .get(..4)
            .is_some_and(|stem| stem.eq_ignore_ascii_case("git~"))
            && name.len() > 4
            && name[4..].bytes().all(|b| b.is_ascii_digit())
}

/// One part of a pattern.
enum Part {
    Any,
    Literal(String),
    Glob(Pattern),
}

impl Part {
    fn of(text: &str) -> Self {
        if text == "**" {
            Self::Any
        } else if text.contains(['*', '?', '[']) {
            // DuckDB matches bytes, so `?` is one byte: a character of two is two of them.
            Self::Glob(Pattern::new(&as_bytes(text), false))
        } else {
            Self::Literal(text.to_owned())
        }
    }
}

/// `text` with each of its bytes as one character, so a pattern that reads characters reads bytes.
fn as_bytes(text: &str) -> String {
    text.bytes().map(char::from).collect()
}

/// Whether the parts of a name match the parts of a pattern, `**` standing for any number of
/// directories (none included). The pattern's positions reached after each part of the name are
/// followed together, so a pattern with many `**` costs its length for each part of the name.
fn fits(parts: &[Part], name: &[&str]) -> bool {
    // Moving over a `**` that takes no directory.
    fn skip(parts: &[Part], reached: &mut [bool]) {
        for at in 0..parts.len() {
            if reached[at] && matches!(parts[at], Part::Any) {
                reached[at + 1] = true;
            }
        }
    }
    // Each part but a `**` takes one part of the name, so a name too short for them cannot match.
    let needed = parts.iter().filter(|p| !matches!(p, Part::Any)).count();
    if needed > name.len() {
        return false;
    }
    let mut reached = vec![false; parts.len() + 1];
    reached[0] = true;
    skip(parts, &mut reached);
    for segment in name {
        let mut next = vec![false; parts.len() + 1];
        for (at, part) in parts.iter().enumerate() {
            if !reached[at] {
                continue;
            }
            match part {
                Part::Any => next[at] = true,
                Part::Literal(text) => next[at + 1] |= text == segment,
                Part::Glob(pattern) => {
                    next[at + 1] |= if segment.is_ascii() {
                        pattern.matches(segment)
                    } else {
                        pattern.matches(&as_bytes(segment))
                    };
                }
            }
        }
        skip(parts, &mut next);
        reached = next;
    }
    reached[parts.len()]
}

impl Tracked {
    /// The tracked files `names`: any that has a part that is empty, `.`, `..`, has a drive or
    /// a backslash, or that a file system reads as `.git`, is not kept.
    pub fn new(mut names: Vec<String>) -> Self {
        names.retain(|name| {
            let parts: Vec<&str> = name.split('/').collect();
            plain(&parts) && !parts.iter().any(|p| is_git(p))
        });
        names.sort();
        names.dedup();
        Self { names }
    }

    /// How many files are tracked.
    pub fn len(&self) -> usize {
        self.names.len()
    }

    /// Whether no file is tracked.
    pub fn is_empty(&self) -> bool {
        self.names.is_empty()
    }

    /// The tracked files `pattern` matches, in name order, up to `MAX_ENTRIES` of them; none for a
    /// pattern that is not a plain relative path (absolute, with `..`, `.`, a drive or an empty
    /// part), that DuckDB refuses (more than one `**`), or that is longer than the limits. A
    /// trailing `**` is every file below, as DuckDB reads it.
    pub fn matching(&self, pattern: &str) -> Vec<&str> {
        let mut found = Vec::new();
        self.scan(pattern, |name| {
            found.push(name);
            found.len() < MAX_ENTRIES
        });
        found
    }

    /// The first tracked file in name order that `pattern` matches and has one of `extensions`.
    pub fn first(&self, pattern: &str, extensions: &[&str]) -> Option<&str> {
        let mut first = None;
        self.scan(pattern, |name| {
            let extension = name.rsplit_once('.').map_or("", |(_, e)| e);
            if extensions
                .iter()
                .any(|wanted| extension.eq_ignore_ascii_case(wanted))
            {
                first = Some(name);
            }
            first.is_none()
        });
        first
    }

    /// Calls `keep` with each tracked file `pattern` matches, in name order, until it says no.
    fn scan<'a>(&'a self, pattern: &str, mut keep: impl FnMut(&'a str) -> bool) {
        let mut text: Vec<&str> = pattern.split('/').collect();
        if pattern.len() > MAX_PATTERN
            || !plain(&text)
            || text.iter().any(|p| is_git(p))
            || text.iter().filter(|p| **p == "**").count() > 1
        {
            return;
        }
        if text.last() == Some(&"**") {
            text.push("*");
        }
        if text.len() > MAX_PARTS {
            return;
        }
        let parts: Vec<Part> = text.iter().map(|p| Part::of(p)).collect();
        // The literal parts the pattern starts with name a folder: only the names in it are met.
        let literal = parts
            .iter()
            .take_while(|p| matches!(p, Part::Literal(_)))
            .count();
        let mut prefix = text[..literal].join("/");
        if literal == parts.len() {
            if let Ok(at) = self
                .names
                .binary_search_by(|n| n.as_str().cmp(prefix.as_str()))
            {
                keep(self.names[at].as_str());
            }
            return;
        }
        if literal > 0 {
            prefix.push('/');
        }
        let start = self.names.partition_point(|n| n.as_str() < prefix.as_str());
        for name in self.names[start..]
            .iter()
            .take_while(|n| n.starts_with(&prefix))
        {
            if fits(&parts, &name.split('/').collect::<Vec<_>>()) && !keep(name.as_str()) {
                return;
            }
        }
    }
}

/// The extensions of the files a format is read from.
fn extensions(format: &Format) -> &'static [&'static str] {
    match format {
        Format::Parquet => &["parquet"],
        Format::Csv { .. } => &["csv", "tsv"],
    }
}

/// The extensions of the files a format is read from, by the format's name (`parquet` or `csv`).
pub fn extensions_named(format: &str) -> Option<&'static [&'static str]> {
    match format {
        "parquet" => Some(extensions(&Format::Parquet)),
        "csv" => Some(extensions(&Format::Csv {
            delimiter: None,
            header: true,
        })),
        _ => None,
    }
}

/// The first file `pattern` matches among the `tracked` files that has an extension of `format`,
/// as a path from `root`, and its columns; `None` when there is none, or it cannot be read as
/// `format`. A file the repository does not hold (a secret in the workspace, a build's output) is
/// never read, whatever the pattern names, and neither is one by any other extension.
pub fn columns(
    root: &Path,
    tracked: &Tracked,
    pattern: &str,
    format: &Format,
) -> Option<(PathBuf, Vec<Column>)> {
    let first = tracked.first(pattern, extensions(format))?;
    let first = PathBuf::from(first);
    // Each part of the path is a real directory and the file a regular file: a link put there
    // since the repository was listed leads elsewhere.
    let mut at = root.to_path_buf();
    let count = first.components().count();
    for (index, part) in first.components().enumerate() {
        at.push(part);
        let kind = fs::symlink_metadata(&at).ok()?.file_type();
        if (index + 1 == count && !kind.is_file()) || (index + 1 < count && !kind.is_dir()) {
            return None;
        }
    }
    let path = root.join(&first);
    let before = fs::symlink_metadata(&path).ok()?;
    let mut file = fs::File::open(&path).ok()?;
    let after = file.metadata().ok()?;
    // The file opened is the one looked at, not a link put in its place since.
    if !after.is_file() || Identity::of(&after) != Identity::of(&before) {
        return None;
    }
    let found = match format {
        Format::Parquet => parquet::columns(&mut file, after.len())?,
        Format::Csv { delimiter, header } => {
            let mut head = Vec::new();
            file.seek(SeekFrom::Start(0)).ok()?;
            file.take(csv::MAX_HEAD as u64)
                .read_to_end(&mut head)
                .ok()?;
            csv::columns(&head, *delimiter, *header)?
        }
    };
    Some((first, found))
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Scratch(PathBuf);

    impl Scratch {
        fn new(label: &str) -> Self {
            let path = std::env::temp_dir()
                .join(format!("konstruct-datafile-{label}-{}", std::process::id()));
            let _ = fs::remove_dir_all(&path);
            fs::create_dir_all(&path).unwrap();
            Self(path)
        }

        fn file(&self, relative: &str, content: &[u8]) {
            let path = self.0.join(relative);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(path, content).unwrap();
        }
    }

    impl Drop for Scratch {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    fn tracked(paths: &[&str]) -> Tracked {
        Tracked::new(paths.iter().map(|p| (*p).to_owned()).collect())
    }

    /// Every entry under `root` but directories, as git lists a repository's files (a link too).
    fn walk(root: &Path, at: &Path, found: &mut Vec<String>) {
        for entry in fs::read_dir(root.join(at)).unwrap().flatten() {
            let path = at.join(entry.file_name());
            if entry.file_type().unwrap().is_dir() {
                walk(root, &path, found);
            } else {
                found.push(names(&[path])[0].clone());
            }
        }
    }

    /// The files `pattern` matches among all the files under `root`.
    fn matching(root: &Path, pattern: &str) -> Vec<PathBuf> {
        let mut found = Vec::new();
        walk(root, Path::new(""), &mut found);
        Tracked::new(found)
            .matching(pattern)
            .into_iter()
            .map(PathBuf::from)
            .collect()
    }

    fn names(found: &[PathBuf]) -> Vec<String> {
        found
            .iter()
            .map(|p| p.to_string_lossy().replace('\\', "/"))
            .collect()
    }

    #[test]
    fn a_pattern_matches_regular_files_in_name_order() {
        let scratch = Scratch::new("order");
        scratch.file("data/b.csv", b"x\n");
        scratch.file("data/a.csv", b"x\n");
        scratch.file("data/a.txt", b"x\n");
        scratch.file("data/sub/c.csv", b"x\n");
        assert_eq!(
            names(&matching(&scratch.0, "data/*.csv")),
            ["data/a.csv", "data/b.csv"]
        );
        assert_eq!(
            names(&matching(&scratch.0, "data/**/*.csv")),
            ["data/a.csv", "data/b.csv", "data/sub/c.csv"]
        );
        assert_eq!(names(&matching(&scratch.0, "data/a.csv")), ["data/a.csv"]);
        assert!(matching(&scratch.0, "data").is_empty());
    }

    #[test]
    fn partitions_match_as_directories() {
        let scratch = Scratch::new("hive");
        scratch.file("silver/year_month=2024-01/data.parquet", b"x");
        scratch.file("silver/year_month=2024-02/data.parquet", b"x");
        assert_eq!(
            names(&matching(&scratch.0, "silver/year_month=*/data.parquet")),
            [
                "silver/year_month=2024-01/data.parquet",
                "silver/year_month=2024-02/data.parquet"
            ]
        );
    }

    #[test]
    fn a_path_that_leaves_the_root_or_names_git_matches_nothing() {
        let scratch = Scratch::new("leave");
        scratch.file("a/x.csv", b"x\n");
        scratch.file(".git/x.csv", b"x\n");
        for pattern in [
            "../a/x.csv",
            "a/../a/x.csv",
            "/a/x.csv",
            "./a/x.csv",
            "a//x.csv",
        ] {
            assert!(matching(&scratch.0, pattern).is_empty(), "{pattern}");
        }
        assert!(matching(&scratch.0, ".git/x.csv").is_empty());
        assert!(
            matching(&scratch.0, "*/x.csv")
                .iter()
                .all(|p| !p.starts_with(".git"))
        );
        assert!(
            matching(&scratch.0, "**/x.csv")
                .iter()
                .all(|p| !p.starts_with(".git"))
        );
    }

    #[cfg(unix)]
    #[test]
    fn no_link_is_followed() {
        use std::os::unix::fs::symlink;
        let outside = Scratch::new("outside");
        outside.file("secret.csv", b"password\n");
        let scratch = Scratch::new("links");
        scratch.file("data/real.csv", b"a\n");
        symlink(
            outside.0.join("secret.csv"),
            scratch.0.join("data/link.csv"),
        )
        .unwrap();
        symlink(&outside.0, scratch.0.join("away")).unwrap();
        let format = Format::Csv {
            delimiter: None,
            header: true,
        };
        // Tracked as a link, or as a file under a directory that has become a link since: refused.
        let held = tracked(&["data/link.csv", "away/secret.csv", "data/real.csv"]);
        for pattern in ["data/link.csv", "away/secret.csv", "away/*.csv"] {
            assert!(
                columns(&scratch.0, &held, pattern, &format).is_none(),
                "{pattern}"
            );
        }
        assert!(columns(&scratch.0, &held, "data/real.csv", &format).is_some());
    }

    #[test]
    fn a_csv_files_header_gives_its_columns() {
        let scratch = Scratch::new("csv");
        scratch.file("x.csv", b"id,\"full name\",amount\n1,a,2\n");
        let format = Format::Csv {
            delimiter: None,
            header: true,
        };
        let (path, found) = columns(&scratch.0, &tracked(&["x.csv"]), "x.csv", &format).unwrap();
        assert_eq!(path, PathBuf::from("x.csv"));
        let found: Vec<&str> = found.iter().map(|(n, _)| n.as_str()).collect();
        assert_eq!(found, ["id", "full name", "amount"]);
    }

    #[test]
    fn only_a_tracked_file_of_the_format_s_extension_is_read() {
        let scratch = Scratch::new("tracked");
        scratch.file(".env", b"AWS_SECRET_ACCESS_KEY=abc\n");
        scratch.file("gha-creds-1a2b.json", b"{\"private_key\": 1}\n");
        scratch.file("data/a.csv", b"secret\n1\n");
        scratch.file("data/b.csv", b"id\n1\n");
        scratch.file("data/notes.txt", b"id\n1\n");
        let format = Format::Csv {
            delimiter: None,
            header: true,
        };
        let held = tracked(&[
            "data/b.csv",
            "data/notes.txt",
            ".env",
            "gha-creds-1a2b.json",
        ]);
        // Not a file git tracks: skipped for the next one that is.
        let (path, found) = columns(&scratch.0, &held, "data/*.csv", &format).unwrap();
        assert_eq!(
            (path, found[0].0.as_str()),
            (PathBuf::from("data/b.csv"), "id")
        );
        // Tracked, but not a data file.
        for pattern in [".env", "gha-creds-*.json", "data/notes.txt", "*"] {
            assert!(
                columns(&scratch.0, &held, pattern, &format).is_none(),
                "{pattern}"
            );
        }
        // Nothing tracked, nothing read.
        assert!(columns(&scratch.0, &Tracked::default(), "data/b.csv", &format).is_none());
    }

    #[test]
    fn a_drive_or_a_part_that_is_not_a_plain_name_matches_nothing() {
        let scratch = Scratch::new("drive");
        scratch.file("C:/x.csv", b"x\n");
        scratch.file("a/b:c.csv", b"x\n");
        for pattern in ["C:/x.csv", "a/b:c.csv", "C:/*.csv", "a/*:c.csv", "c:x.csv"] {
            assert!(matching(&scratch.0, pattern).is_empty(), "{pattern}");
        }
    }

    #[test]
    fn git_is_not_entered_by_any_name_a_file_system_reads_as_it() {
        let scratch = Scratch::new("gitnames");
        scratch.file("a/x.csv", b"x\n");
        for name in [".git", ".GIT", ".Git", ".git.", ".git ", "GIT~1"] {
            scratch.file(&format!("{name}/x.csv"), b"x\n");
        }
        for pattern in [
            ".GIT/x.csv",
            ".Git/x.csv",
            ".git./x.csv",
            ".git /x.csv",
            "GIT~1/x.csv",
            "*/x.csv",
            "**/x.csv",
        ] {
            assert!(
                matching(&scratch.0, pattern)
                    .iter()
                    .all(|p| p.starts_with("a")),
                "{pattern}"
            );
        }
        for pattern in [".GIT/x.csv", ".git./x.csv", "GIT~1/x.csv"] {
            assert!(matching(&scratch.0, pattern).is_empty(), "{pattern}");
        }
        assert_eq!(names(&matching(&scratch.0, "**/x.csv")), ["a/x.csv"]);
    }

    #[test]
    fn more_than_one_double_star_is_an_error_as_in_duckdb_so_matches_nothing() {
        let held = tracked(&["a/b/x.csv", "x.csv"]);
        assert_eq!(held.matching("**/x.csv"), ["a/b/x.csv", "x.csv"]);
        assert!(held.matching("**/b/**/x.csv").is_empty());
        assert!(held.matching("**/**/x.csv").is_empty());
        assert!(
            held.matching(&format!("{}x.csv", "**/*/".repeat(40)))
                .is_empty()
        );
    }

    #[test]
    fn a_trailing_double_star_is_every_file_below() {
        let held = tracked(&["d/a.csv", "d/s/b.csv", "e/c.csv"]);
        assert_eq!(held.matching("d/**"), ["d/a.csv", "d/s/b.csv"]);
        assert_eq!(held.matching("**"), ["d/a.csv", "d/s/b.csv", "e/c.csv"]);
    }

    #[test]
    fn a_question_mark_is_one_byte_as_in_duckdb() {
        let held = tracked(&["d/\u{fc}.csv", "d/u.csv"]);
        assert_eq!(held.matching("d/?.csv"), ["d/u.csv"]);
        assert_eq!(held.matching("d/??.csv"), ["d/\u{fc}.csv"]);
        assert_eq!(held.matching("d/\u{fc}.csv"), ["d/\u{fc}.csv"]);
        assert_eq!(held.matching("d/[a-z\u{fc}].csv"), ["d/u.csv"]);
    }

    #[test]
    fn a_pattern_of_more_than_64_parts_matches_nothing_and_costs_nothing() {
        let (deep, deeper) = (
            format!("{}x.csv", "a/".repeat(63)),
            format!("{}x.csv", "a/".repeat(64)),
        );
        let held = tracked(&[&deep, &deeper]);
        assert_eq!(held.matching(&deep), [deep.as_str()]);
        assert!(held.matching(&deeper).is_empty());
        let wide = format!("{}x.csv", "*/".repeat(12_000));
        let names: Vec<String> = (0..100_000).map(|i| format!("d/f{i}.csv")).collect();
        let held = Tracked::new(names);
        let started = std::time::Instant::now();
        assert!(held.matching(&wide).is_empty());
        assert!(held.matching("d/*/*/*/*/*/*/*/*/*/*/x.csv").is_empty());
        let long = format!("d/{}x.csv", "*a".repeat(12_000));
        assert!(held.matching(&long).is_empty());
        assert!(started.elapsed().as_secs() < 2, "{:?}", started.elapsed());
    }

    #[test]
    fn the_cap_is_on_the_names_a_pattern_matches_not_on_those_it_looks_at() {
        let mut names: Vec<String> = (0..MAX_ENTRIES + 1)
            .map(|i| format!("d/a{i:06}.txt"))
            .collect();
        names.push("d/z.csv".to_owned());
        let held = Tracked::new(names);
        assert_eq!(held.matching("d/*.csv"), ["d/z.csv"]);
    }

    #[test]
    fn a_pattern_in_a_folder_costs_a_search_not_a_pass_over_every_name() {
        let names: Vec<String> = (0..500_000)
            .map(|i| format!("d{:03}/f{i}.csv", i % 500))
            .collect();
        // A pass over every name, timed in the same process, is the unit: an emulator or a debug
        // build slows the search and the pass alike, so the bound holds wherever the test runs.
        let started = std::time::Instant::now();
        let folder = names
            .iter()
            .filter(|name| name.starts_with("d007/"))
            .count();
        let pass = started.elapsed();
        assert_eq!(folder, 1000);
        let held = Tracked::new(names);
        assert_eq!(held.len(), 500_000);
        let started = std::time::Instant::now();
        for _ in 0..200 {
            assert_eq!(held.matching("d007/f7.csv"), ["d007/f7.csv"]);
            assert_eq!(held.matching("d007/f*.csv").len(), 1000);
        }
        let searches = started.elapsed();
        // 400 matches that each read every name take at least 400 passes; the search takes about
        // 20 to 30.
        assert!(
            searches < pass * 200,
            "{searches:?} for 400 matches, {pass:?} for one pass"
        );
    }

    #[test]
    fn short_names_of_git_are_not_files_but_other_short_names_are() {
        for name in ["GIT~1", "git~2", "GIT~12.", ".GIT "] {
            assert!(is_git(name), "{name}");
        }
        for name in ["PROGRA~1", "git~", "git~x", "gitx"] {
            assert!(!is_git(name), "{name}");
        }
        let held = tracked(&["GIT~2/x.csv", "ok/x.csv"]);
        assert_eq!(held.len(), 1);
    }
}
