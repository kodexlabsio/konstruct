//! Jinja2's syntax: the lexer (delimiters, whitespace control, raw blocks, comments) and the
//! parser, with Jinja2's precedence of operators and its statements: `if`, `for`, `set`, `macro`,
//! `call`, `filter`, `with`, `do`, `raw`, `block`, and the `import` family, which cannot read a
//! file here and fails when it runs.

use super::eval::Error;
use super::stack::deeper;
use super::value::Value;

#[derive(Clone, Debug, PartialEq)]
enum Tok {
    Data(String),
    BlockBegin,
    BlockEnd,
    VarBegin,
    VarEnd,
    Name(String),
    Int(i128),
    Float(f64),
    Str(String),
    Op(&'static str),
    Eof,
}

const OPERATORS: [&str; 24] = [
    "**", "//", "==", "!=", ">=", "<=", "+", "-", "*", "/", "%", "~", "[", "]", "(", ")", "{", "}",
    "<", ">", "=", ".", ":", ",",
];

fn lex_error(message: &str) -> Error {
    Error::new(format!("syntax error: {message}"))
}

/// Splits `source` into data and the tokens of its blocks and expressions.
fn tokenize(source: &str) -> Result<Vec<Tok>, Error> {
    // One trailing newline is dropped and every kind of newline is `\n`, as Jinja2 does.
    let mut text = source.replace("\r\n", "\n").replace('\r', "\n");
    if text.ends_with('\n') {
        text.pop();
    }
    let bytes = text.as_bytes();
    let mut tokens = Vec::new();
    let mut at = 0;
    let mut strip_next = false;
    let mut data_start = 0;
    while at < bytes.len() {
        let start = match find_start(bytes, at) {
            Some(start) => start,
            None => break,
        };
        let kind = bytes[start + 1];
        let mut after = start + 2;
        let mut strip_before = false;
        if matches!(bytes.get(after), Some(b'-')) {
            strip_before = true;
            after += 1;
        } else if matches!(bytes.get(after), Some(b'+')) {
            after += 1;
        }
        let mut data = &text[data_start..start];
        if strip_next {
            data = data.trim_start();
        }
        if strip_before {
            data = data.trim_end();
        }
        if !data.is_empty() {
            tokens.push(Tok::Data(data.to_string()));
        }
        match kind {
            b'#' => {
                let end = find_comment_end(&text, after)?;
                strip_next = end.1;
                at = end.0;
            }
            b'{' => {
                tokens.push(Tok::VarBegin);
                let (end, strip) = lex_expression(&text, after, "}}", &mut tokens)?;
                tokens.push(Tok::VarEnd);
                strip_next = strip;
                at = end;
            }
            _ => {
                let mark = tokens.len();
                tokens.push(Tok::BlockBegin);
                let (end, strip) = lex_expression(&text, after, "%}", &mut tokens)?;
                tokens.push(Tok::BlockEnd);
                strip_next = strip;
                at = end;
                if tokens.get(mark + 1) == Some(&Tok::Name("raw".into()))
                    && tokens.len() == mark + 3
                {
                    tokens.truncate(mark);
                    let (raw, end, strip_after_raw) = raw_block(&text, at, strip_next)?;
                    if !raw.is_empty() {
                        tokens.push(Tok::Data(raw));
                    }
                    strip_next = strip_after_raw;
                    at = end;
                }
            }
        }
        data_start = at;
    }
    let mut data = &text[data_start.min(text.len())..];
    if strip_next {
        data = data.trim_start();
    }
    if !data.is_empty() {
        tokens.push(Tok::Data(data.to_string()));
    }
    tokens.push(Tok::Eof);
    Ok(tokens)
}

fn find_start(bytes: &[u8], from: usize) -> Option<usize> {
    let mut at = from;
    while at + 1 < bytes.len() {
        if bytes[at] == b'{' && matches!(bytes[at + 1], b'{' | b'%' | b'#') {
            return Some(at);
        }
        at += 1;
    }
    None
}

fn find_comment_end(text: &str, from: usize) -> Result<(usize, bool), Error> {
    let rest = &text[from..];
    match rest.find("#}") {
        Some(offset) => Ok((
            from + offset + 2,
            offset > 0 && rest.as_bytes()[offset - 1] == b'-',
        )),
        None => Err(lex_error("unclosed comment")),
    }
}

/// The text of a raw block from `from` to its `endraw`, where the block ends and whether the
/// whitespace after it is stripped.
fn raw_block(text: &str, from: usize, strip_start: bool) -> Result<(String, usize, bool), Error> {
    let bytes = text.as_bytes();
    let mut at = from;
    while let Some(start) = find_start(bytes, at) {
        if bytes[start + 1] == b'%' {
            let mut cursor = start + 2;
            let strip_before = matches!(bytes.get(cursor), Some(b'-'));
            if strip_before || matches!(bytes.get(cursor), Some(b'+')) {
                cursor += 1;
            }
            let rest = text[cursor..].trim_start();
            if let Some(after_name) = rest.strip_prefix("endraw") {
                let trimmed = after_name.trim_start();
                let (strip_after, trimmed) = match trimmed.strip_prefix('-') {
                    Some(rest) => (true, rest),
                    None => (false, trimmed.strip_prefix('+').unwrap_or(trimmed)),
                };
                if let Some(closed) = trimmed.strip_prefix("%}") {
                    let mut raw = &text[from..start];
                    if strip_start {
                        raw = raw.trim_start();
                    }
                    if strip_before {
                        raw = raw.trim_end();
                    }
                    return Ok((raw.to_string(), text.len() - closed.len(), strip_after));
                }
            }
        }
        at = start + 2;
    }
    Err(lex_error("unclosed raw block"))
}

/// Tokenises up to the end delimiter `close` (`%}` or `}}`) outside brackets; returns where the
/// text goes on and whether the delimiter was `-%}` or `-}}`.
fn lex_expression(
    text: &str,
    from: usize,
    close: &str,
    tokens: &mut Vec<Tok>,
) -> Result<(usize, bool), Error> {
    let bytes = text.as_bytes();
    let mut at = from;
    let mut depth = 0usize;
    loop {
        while at < bytes.len() && (bytes[at] as char).is_whitespace() {
            at += 1;
        }
        if at >= bytes.len() {
            return Err(lex_error("unexpected end of template"));
        }
        if depth == 0 {
            if text[at..].starts_with(close) {
                return Ok((at + 2, false));
            }
            if bytes[at] == b'-' && text[at + 1..].starts_with(close) {
                return Ok((at + 3, true));
            }
            if bytes[at] == b'+' && text[at + 1..].starts_with(close) {
                return Ok((at + 3, false));
            }
        }
        let c = text[at..].chars().next().unwrap_or(' ');
        if c == '\'' || c == '"' {
            let (value, end) = lex_string(text, at)?;
            tokens.push(Tok::Str(value));
            at = end;
        } else if c.is_ascii_digit() {
            let after_dot = matches!(tokens.last(), Some(Tok::Op(".")));
            let (token, end) = lex_number(text, at, after_dot)?;
            tokens.push(token);
            at = end;
        } else if c.is_alphabetic() || c == '_' {
            let end = text[at..]
                .char_indices()
                .find(|(_, ch)| !(ch.is_alphanumeric() || *ch == '_'))
                .map_or(text.len(), |(i, _)| at + i);
            tokens.push(Tok::Name(text[at..end].to_string()));
            at = end;
        } else if c == '|' {
            tokens.push(Tok::Op("|"));
            at += 1;
        } else {
            let Some(op) = OPERATORS.iter().find(|op| text[at..].starts_with(**op)) else {
                return Err(lex_error(&format!("unexpected character '{c}'")));
            };
            match *op {
                "(" | "[" | "{" => depth += 1,
                ")" | "]" | "}" => depth = depth.saturating_sub(1),
                _ => {}
            }
            tokens.push(Tok::Op(op));
            at += op.len();
        }
    }
}

fn lex_string(text: &str, from: usize) -> Result<(String, usize), Error> {
    let quote = text[from..].chars().next().unwrap_or('\'');
    let mut out = String::new();
    let mut chars = text[from + 1..].char_indices();
    while let Some((offset, c)) = chars.next() {
        if c == quote {
            return Ok((out, from + 1 + offset + 1));
        }
        if c != '\\' {
            out.push(c);
            continue;
        }
        match chars.next().map(|(_, e)| e) {
            Some('n') => out.push('\n'),
            Some('t') => out.push('\t'),
            Some('r') => out.push('\r'),
            Some('0') => out.push('\0'),
            Some('\\') => out.push('\\'),
            Some('\'') => out.push('\''),
            Some('"') => out.push('"'),
            Some(kind @ ('x' | 'u')) => {
                let width = if kind == 'x' { 2 } else { 4 };
                let hex: String = (0..width)
                    .filter_map(|_| chars.next().map(|(_, h)| h))
                    .collect();
                let code = u32::from_str_radix(&hex, 16).map_err(|_| lex_error("bad escape"))?;
                out.push(char::from_u32(code).ok_or_else(|| lex_error("bad escape"))?);
            }
            Some(other) => {
                out.push('\\');
                out.push(other);
            }
            None => break,
        }
    }
    Err(lex_error("unterminated string"))
}

fn lex_number(text: &str, from: usize, integer_only: bool) -> Result<(Tok, usize), Error> {
    let bytes = text.as_bytes();
    if bytes[from] == b'0'
        && matches!(
            bytes.get(from + 1),
            Some(b'x' | b'X' | b'o' | b'O' | b'b' | b'B')
        )
    {
        let radix = match bytes[from + 1].to_ascii_lowercase() {
            b'x' => 16,
            b'o' => 8,
            _ => 2,
        };
        let end = text[from + 2..]
            .char_indices()
            .find(|(_, c)| !(c.is_ascii_hexdigit() || *c == '_'))
            .map_or(text.len(), |(i, _)| from + 2 + i);
        let digits: String = text[from + 2..end].chars().filter(|c| *c != '_').collect();
        let number = i128::from_str_radix(&digits, radix).map_err(|_| lex_error("bad number"))?;
        return Ok((Tok::Int(number), end));
    }
    let digits = |mut at: usize| {
        while at < bytes.len() && (bytes[at].is_ascii_digit() || (bytes[at] == b'_' && at > from)) {
            at += 1;
        }
        at
    };
    let mut end = digits(from);
    let mut float = false;
    if !integer_only {
        if bytes.get(end) == Some(&b'.') && bytes.get(end + 1).is_some_and(u8::is_ascii_digit) {
            end = digits(end + 1);
            float = true;
        }
        if matches!(bytes.get(end), Some(b'e' | b'E')) {
            let mut at = end + 1;
            if matches!(bytes.get(at), Some(b'+' | b'-')) {
                at += 1;
            }
            if bytes.get(at).is_some_and(u8::is_ascii_digit) {
                end = digits(at);
                float = true;
            }
        }
    }
    let literal: String = text[from..end].chars().filter(|c| *c != '_').collect();
    if float {
        let number = literal
            .parse::<f64>()
            .map_err(|_| lex_error("bad number"))?;
        Ok((Tok::Float(number), end))
    } else {
        let number = literal
            .parse::<i128>()
            .map_err(|_| lex_error("bad number"))?;
        Ok((Tok::Int(number), end))
    }
}

impl Expr {
    /// Whether the value is known from the template alone, which Jinja2 works out as it compiles.
    pub fn is_const(&self) -> bool {
        match self {
            Expr::Const(_) => true,
            Expr::Tuple(items) | Expr::List(items) | Expr::Concat(items, _) => {
                items.iter().all(Expr::is_const)
            }
            Expr::Dict(pairs) => pairs.iter().all(|(k, v)| k.is_const() && v.is_const()),
            Expr::Neg(e) | Expr::Pos(e) | Expr::Not(e) => e.is_const(),
            Expr::And(a, b) | Expr::Or(a, b) | Expr::Bin(_, a, b) => a.is_const() && b.is_const(),
            Expr::Compare(first, rest) => {
                first.is_const() && rest.iter().all(|(_, e)| e.is_const())
            }
            Expr::Cond(test, then, otherwise) => {
                test.is_const()
                    && then.is_const()
                    && otherwise.as_ref().is_none_or(|e| e.is_const())
            }
            Expr::Filter(Some(subject), _, args) => {
                subject.is_const()
                    && args.iter().all(|a| match a {
                        Arg::Pos(e) | Arg::Named(_, e) => e.is_const(),
                        _ => false,
                    })
            }
            _ => false,
        }
    }
}

#[derive(Clone, Debug)]
pub enum Arg {
    Pos(Expr),
    Named(String, Expr),
    Star(Expr),
    StarStar(Expr),
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Cmp {
    Eq,
    Ne,
    Lt,
    Le,
    Gt,
    Ge,
    In,
    NotIn,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Bin {
    Add,
    Sub,
    Mul,
    Div,
    FloorDiv,
    Mod,
    Pow,
}

#[derive(Clone, Debug)]
pub enum Expr {
    Const(Value),
    Name(String),
    Tuple(Vec<Expr>),
    List(Vec<Expr>),
    Dict(Vec<(Expr, Expr)>),
    Neg(Box<Expr>),
    Pos(Box<Expr>),
    Not(Box<Expr>),
    And(Box<Expr>, Box<Expr>),
    Or(Box<Expr>, Box<Expr>),
    Bin(Bin, Box<Expr>, Box<Expr>),
    Compare(Box<Expr>, Vec<(Cmp, Expr)>),
    /// `a ~ b`; the flag says every part is a constant, which Jinja2 joins as plain text before
    /// anything is escaped.
    Concat(Vec<Expr>, bool),
    Cond(Box<Expr>, Box<Expr>, Option<Box<Expr>>),
    Attr(Box<Expr>, String),
    Item(Box<Expr>, Box<Expr>),
    Slice(Option<Box<Expr>>, Option<Box<Expr>>, Option<Box<Expr>>),
    Call(Box<Expr>, Vec<Arg>),
    /// A filter; `None` is the body of a `{% filter %}` block.
    Filter(Option<Box<Expr>>, String, Vec<Arg>),
    Test(Box<Expr>, String, Vec<Arg>, bool),
}

#[derive(Clone, Debug)]
pub enum Target {
    Name(String),
    Tuple(Vec<Target>),
    Attr(String, String),
}

#[derive(Clone, Debug)]
pub struct Param {
    pub name: String,
    pub default: Option<Expr>,
}

/// Whether an output is escaped: known where it is written, or decided as it runs when the
/// `autoescape` that governs it is not a constant.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Esc {
    Off,
    On,
    Dynamic,
}

#[derive(Clone, Debug)]
pub enum Stmt {
    Text(String),
    /// An expression's output, escaped when written inside `{% autoescape true %}`.
    Output(Expr, Esc),
    /// `{% autoescape %}`: its value, which the filters that escape see while the body runs.
    Autoescape(Expr, Vec<Stmt>),
    If(Vec<(Expr, Vec<Stmt>)>, Vec<Stmt>),
    For(std::rc::Rc<ForDef>),
    Set(Target, Expr),
    SetBlock(Target, Option<Expr>, Vec<Stmt>),
    Macro(std::rc::Rc<MacroDef>),
    CallBlock(std::rc::Rc<MacroDef>, Expr),
    FilterBlock(Expr, Vec<Stmt>),
    With(Vec<(Target, Expr)>, Vec<Stmt>),
    Do(Expr),
    Block(Vec<Stmt>),
    /// `include`, `import`, `from` and `extends`: there are no other templates to read.
    NoTemplates(String),
}

#[derive(Debug)]
pub struct ForDef {
    pub target: Target,
    pub iter: Expr,
    pub filter: Option<Expr>,
    pub body: Vec<Stmt>,
    pub otherwise: Vec<Stmt>,
    /// `loop(children)` runs the body again over `children`.
    pub recursive: bool,
}

#[derive(Debug)]
pub struct MacroDef {
    /// Whether it was written inside `{% autoescape true %}`: its text is then Markup.
    pub escape: Esc,
    pub name: String,
    pub params: Vec<Param>,
    pub body: Vec<Stmt>,
}

pub fn parse(source: &str) -> Result<Vec<Stmt>, Error> {
    let tokens = tokenize(source)?;
    let mut parser = Parser {
        escape: Esc::Off,
        tokens,
        at: 0,
        depth: 0,
    };
    let (body, end) = parser.statements(&[])?;
    match end {
        None => Ok(body),
        Some(tag) => Err(lex_error(&format!("unexpected '{tag}'"))),
    }
}

struct Parser {
    tokens: Vec<Tok>,
    at: usize,
    depth: usize,
    /// Whether the text being parsed is inside `{% autoescape true %}`.
    escape: Esc,
}

/// How deep an expression or a block may nest, counting each link of a chain (`a + b + c`, `a.b.c`,
/// `x|f|g`) as a level, since a chain is a tree as deep as it is long.
const MAX_DEPTH: usize = 200;

impl Parser {
    fn peek(&self) -> &Tok {
        &self.tokens[self.at.min(self.tokens.len() - 1)]
    }

    fn look(&self, ahead: usize) -> &Tok {
        &self.tokens[(self.at + ahead).min(self.tokens.len() - 1)]
    }

    fn next(&mut self) -> Tok {
        let token = self.peek().clone();
        self.at += 1;
        token
    }

    fn is_op(&self, op: &str) -> bool {
        matches!(self.peek(), Tok::Op(found) if *found == op)
    }

    fn is_name(&self, name: &str) -> bool {
        matches!(self.peek(), Tok::Name(found) if found == name)
    }

    fn skip_op(&mut self, op: &str) -> bool {
        let found = self.is_op(op);
        if found {
            self.at += 1;
        }
        found
    }

    fn skip_name(&mut self, name: &str) -> bool {
        let found = self.is_name(name);
        if found {
            self.at += 1;
        }
        found
    }

    fn expect_op(&mut self, op: &str) -> Result<(), Error> {
        if self.skip_op(op) {
            Ok(())
        } else {
            Err(lex_error(&format!("expected '{op}'")))
        }
    }

    fn name(&mut self) -> Result<String, Error> {
        match self.next() {
            Tok::Name(name) => Ok(name),
            _ => Err(lex_error("expected a name")),
        }
    }

    fn block_end(&mut self) -> Result<(), Error> {
        match self.next() {
            Tok::BlockEnd => Ok(()),
            _ => Err(lex_error("expected the end of the block")),
        }
    }

    /// Statements up to one of the `ends` tags (the tag's name is consumed, and the caller reads
    /// the rest of its block), or to the end of the template when `ends` is empty.
    fn statements(&mut self, ends: &[&str]) -> Result<(Vec<Stmt>, Option<String>), Error> {
        self.enter()?;
        let found = deeper(|| self.statements_in(ends));
        self.depth -= 1;
        found
    }

    fn statements_in(&mut self, ends: &[&str]) -> Result<(Vec<Stmt>, Option<String>), Error> {
        let mut body = Vec::new();
        loop {
            match self.next() {
                Tok::Eof => {
                    return if ends.is_empty() {
                        Ok((body, None))
                    } else {
                        Err(lex_error("unexpected end of template"))
                    };
                }
                Tok::Data(text) => body.push(Stmt::Text(text)),
                Tok::VarBegin => {
                    let expr = self.expression()?;
                    if self.next() != Tok::VarEnd {
                        return Err(lex_error("expected '}}'"));
                    }
                    body.push(Stmt::Output(expr, self.escape));
                }
                Tok::BlockBegin => {
                    let tag = self.name()?;
                    if ends.contains(&tag.as_str()) {
                        return Ok((body, Some(tag)));
                    }
                    body.push(self.statement(&tag)?);
                }
                _ => return Err(lex_error("unexpected token")),
            }
        }
    }

    fn statement(&mut self, tag: &str) -> Result<Stmt, Error> {
        match tag {
            "if" => self.if_statement(),
            "for" => self.for_statement(),
            "set" => self.set_statement(),
            "macro" => {
                let name = self.name()?;
                let params = self.params()?;
                self.block_end()?;
                let (body, _) = self.statements(&["endmacro"])?;
                self.skip_name_then_end()?;
                Ok(Stmt::Macro(std::rc::Rc::new(MacroDef {
                    escape: self.escape,
                    name,
                    params,
                    body,
                })))
            }
            "call" => {
                let params = if self.is_op("(") {
                    self.params()?
                } else {
                    Vec::new()
                };
                let call = self.expression()?;
                self.block_end()?;
                let (body, _) = self.statements(&["endcall"])?;
                self.block_end()?;
                let def = MacroDef {
                    escape: self.escape,
                    name: "caller".into(),
                    params,
                    body,
                };
                Ok(Stmt::CallBlock(std::rc::Rc::new(def), call))
            }
            "filter" => {
                let filter = self.filter_chain(None)?;
                self.block_end()?;
                let (body, _) = self.statements(&["endfilter"])?;
                self.block_end()?;
                Ok(Stmt::FilterBlock(filter, body))
            }
            "with" => {
                let mut pairs = Vec::new();
                while !matches!(self.peek(), Tok::BlockEnd) {
                    if !pairs.is_empty() {
                        self.expect_op(",")?;
                    }
                    let target = self.target()?;
                    self.expect_op("=")?;
                    pairs.push((target, self.expression()?));
                }
                self.block_end()?;
                let (body, _) = self.statements(&["endwith"])?;
                self.block_end()?;
                Ok(Stmt::With(pairs, body))
            }
            "do" => {
                let expr = self.expression()?;
                self.block_end()?;
                Ok(Stmt::Do(expr))
            }
            "print" => {
                let expr = self.expression()?;
                self.block_end()?;
                Ok(Stmt::Output(expr, self.escape))
            }
            "block" => {
                self.name()?;
                self.block_end()?;
                let (body, _) = self.statements(&["endblock"])?;
                if matches!(self.peek(), Tok::Name(_)) {
                    self.at += 1;
                }
                self.block_end()?;
                Ok(Stmt::Block(body))
            }
            "autoescape" => {
                let flag = self.expression()?;
                let mode = match &flag {
                    Expr::Const(value) if value.truthy() => Esc::On,
                    Expr::Const(_) => Esc::Off,
                    _ => Esc::Dynamic,
                };
                self.block_end()?;
                let saved = std::mem::replace(&mut self.escape, mode);
                let found = self.statements(&["endautoescape"]);
                self.escape = saved;
                let (body, _) = found?;
                self.block_end()?;
                Ok(Stmt::Autoescape(flag, body))
            }
            "include" | "import" | "from" | "extends" => {
                while !matches!(self.peek(), Tok::BlockEnd | Tok::Eof) {
                    self.at += 1;
                }
                self.block_end()?;
                Ok(Stmt::NoTemplates(tag.to_string()))
            }
            other => Err(lex_error(&format!("unknown tag '{other}'"))),
        }
    }

    fn skip_name_then_end(&mut self) -> Result<(), Error> {
        if matches!(self.peek(), Tok::Name(_)) {
            self.at += 1;
        }
        self.block_end()
    }

    fn if_statement(&mut self) -> Result<Stmt, Error> {
        let mut branches = Vec::new();
        let mut otherwise = Vec::new();
        loop {
            let test = self.expression()?;
            self.block_end()?;
            let (body, end) = self.statements(&["elif", "else", "endif"])?;
            branches.push((test, body));
            match end.as_deref() {
                Some("elif") => continue,
                Some("else") => {
                    self.block_end()?;
                    let (rest, _) = self.statements(&["endif"])?;
                    otherwise = rest;
                    self.block_end()?;
                }
                _ => self.block_end()?,
            }
            return Ok(Stmt::If(branches, otherwise));
        }
    }

    fn for_statement(&mut self) -> Result<Stmt, Error> {
        let target = self.target_until_in()?;
        if !self.skip_name("in") {
            return Err(lex_error("expected 'in'"));
        }
        let iter = self.or_expression()?;
        let filter = if self.skip_name("if") {
            Some(self.or_expression()?)
        } else {
            None
        };
        let recursive = self.skip_name("recursive");
        self.block_end()?;
        let (body, end) = self.statements(&["endfor", "else"])?;
        let mut otherwise = Vec::new();
        if end.as_deref() == Some("else") {
            self.block_end()?;
            otherwise = self.statements(&["endfor"])?.0;
        }
        self.block_end()?;
        Ok(Stmt::For(std::rc::Rc::new(ForDef {
            target,
            iter,
            filter,
            body,
            otherwise,
            recursive,
        })))
    }

    fn set_statement(&mut self) -> Result<Stmt, Error> {
        let target = self.target()?;
        if self.is_op("=") {
            self.at += 1;
            let value = self.expression_list()?;
            self.block_end()?;
            return Ok(Stmt::Set(target, value));
        }
        let filter = if self.skip_op("|") {
            Some(self.filter_chain(None)?)
        } else {
            None
        };
        self.block_end()?;
        let (body, _) = self.statements(&["endset"])?;
        self.block_end()?;
        Ok(Stmt::SetBlock(target, filter, body))
    }

    fn params(&mut self) -> Result<Vec<Param>, Error> {
        self.expect_op("(")?;
        let mut params = Vec::new();
        while !self.is_op(")") {
            if !params.is_empty() {
                self.expect_op(",")?;
                if self.is_op(")") {
                    break;
                }
            }
            let name = self.name()?;
            let default = if self.skip_op("=") {
                Some(self.expression()?)
            } else {
                None
            };
            params.push(Param { name, default });
        }
        self.expect_op(")")?;
        Ok(params)
    }

    fn target(&mut self) -> Result<Target, Error> {
        let first = self.single_target()?;
        if !self.is_op(",") {
            return Ok(first);
        }
        let mut items = vec![first];
        while self.skip_op(",") {
            if matches!(self.peek(), Tok::Op("=")) || matches!(self.peek(), Tok::BlockEnd) {
                break;
            }
            items.push(self.single_target()?);
        }
        Ok(Target::Tuple(items))
    }

    fn target_until_in(&mut self) -> Result<Target, Error> {
        let first = self.single_target()?;
        if !self.is_op(",") {
            return Ok(first);
        }
        let mut items = vec![first];
        while self.skip_op(",") {
            if self.is_name("in") {
                break;
            }
            items.push(self.single_target()?);
        }
        Ok(Target::Tuple(items))
    }

    fn single_target(&mut self) -> Result<Target, Error> {
        self.enter()?;
        let found = deeper(|| self.single_target_in());
        self.depth -= 1;
        found
    }

    fn single_target_in(&mut self) -> Result<Target, Error> {
        if self.skip_op("(") {
            let mut items = Vec::new();
            while !self.is_op(")") {
                if !items.is_empty() {
                    self.expect_op(",")?;
                    if self.is_op(")") {
                        break;
                    }
                }
                items.push(self.single_target()?);
            }
            self.expect_op(")")?;
            return Ok(Target::Tuple(items));
        }
        let name = self.name()?;
        if self.skip_op(".") {
            let attribute = self.name()?;
            return Ok(Target::Attr(name, attribute));
        }
        Ok(Target::Name(name))
    }

    /// `a, b` without parentheses is a tuple, as on the right of `{% set %}`.
    fn expression_list(&mut self) -> Result<Expr, Error> {
        let first = self.expression()?;
        if !self.is_op(",") {
            return Ok(first);
        }
        let mut items = vec![first];
        while self.skip_op(",") {
            if matches!(self.peek(), Tok::BlockEnd) {
                break;
            }
            items.push(self.expression()?);
        }
        Ok(Expr::Tuple(items))
    }

    /// One more link in a chain of operators, attributes or filters.
    fn link(&mut self) -> Result<(), Error> {
        self.enter()
    }

    fn enter(&mut self) -> Result<(), Error> {
        self.depth += 1;
        if self.depth > MAX_DEPTH {
            return Err(lex_error("expression nested too deeply"));
        }
        Ok(())
    }

    pub fn expression(&mut self) -> Result<Expr, Error> {
        deeper(|| self.expression_in())
    }

    fn expression_in(&mut self) -> Result<Expr, Error> {
        self.enter()?;
        let base = self.depth;
        let mut expr = self.or_expression()?;
        while self.skip_name("if") {
            self.link()?;
            let test = self.or_expression()?;
            let otherwise = if self.skip_name("else") {
                Some(Box::new(self.expression()?))
            } else {
                None
            };
            expr = Expr::Cond(Box::new(test), Box::new(expr), otherwise);
        }
        self.depth = base - 1;
        Ok(expr)
    }

    fn or_expression(&mut self) -> Result<Expr, Error> {
        let base = self.depth;
        let mut left = self.and_expression()?;
        while self.skip_name("or") {
            self.link()?;
            left = Expr::Or(Box::new(left), Box::new(self.and_expression()?));
        }
        self.depth = base;
        Ok(left)
    }

    fn and_expression(&mut self) -> Result<Expr, Error> {
        let base = self.depth;
        let mut left = self.not_expression()?;
        while self.skip_name("and") {
            self.link()?;
            left = Expr::And(Box::new(left), Box::new(self.not_expression()?));
        }
        self.depth = base;
        Ok(left)
    }

    fn not_expression(&mut self) -> Result<Expr, Error> {
        if self.skip_name("not") {
            self.enter()?;
            let inner = deeper(|| self.not_expression());
            self.depth -= 1;
            let inner = inner?;
            return Ok(Expr::Not(Box::new(inner)));
        }
        self.compare()
    }

    fn compare(&mut self) -> Result<Expr, Error> {
        let first = self.sum()?;
        let mut operands = Vec::new();
        loop {
            let op = match self.peek() {
                Tok::Op("==") => Cmp::Eq,
                Tok::Op("!=") => Cmp::Ne,
                Tok::Op("<") => Cmp::Lt,
                Tok::Op("<=") => Cmp::Le,
                Tok::Op(">") => Cmp::Gt,
                Tok::Op(">=") => Cmp::Ge,
                Tok::Name(name) if name == "in" => Cmp::In,
                Tok::Name(name)
                    if name == "not" && matches!(self.look(1), Tok::Name(n) if n == "in") =>
                {
                    self.at += 1;
                    Cmp::NotIn
                }
                _ => break,
            };
            self.at += 1;
            operands.push((op, self.sum()?));
        }
        Ok(if operands.is_empty() {
            first
        } else {
            Expr::Compare(Box::new(first), operands)
        })
    }

    fn sum(&mut self) -> Result<Expr, Error> {
        let base = self.depth;
        let mut left = self.concat()?;
        loop {
            let op = if self.is_op("+") {
                Bin::Add
            } else if self.is_op("-") {
                Bin::Sub
            } else {
                self.depth = base;
                return Ok(left);
            };
            self.link()?;
            self.at += 1;
            left = Expr::Bin(op, Box::new(left), Box::new(self.concat()?));
        }
    }

    fn concat(&mut self) -> Result<Expr, Error> {
        let mut parts = vec![self.product()?];
        while self.skip_op("~") {
            parts.push(self.product()?);
        }
        Ok(if parts.len() == 1 {
            parts.remove(0)
        } else {
            let constant = parts.iter().all(Expr::is_const);
            Expr::Concat(parts, constant)
        })
    }

    fn product(&mut self) -> Result<Expr, Error> {
        let base = self.depth;
        let mut left = self.power()?;
        loop {
            let op = match self.peek() {
                Tok::Op("*") => Bin::Mul,
                Tok::Op("/") => Bin::Div,
                Tok::Op("//") => Bin::FloorDiv,
                Tok::Op("%") => Bin::Mod,
                _ => {
                    self.depth = base;
                    return Ok(left);
                }
            };
            self.link()?;
            self.at += 1;
            left = Expr::Bin(op, Box::new(left), Box::new(self.power()?));
        }
    }

    fn power(&mut self) -> Result<Expr, Error> {
        let base = self.depth;
        let mut left = self.unary(true)?;
        while self.skip_op("**") {
            self.link()?;
            left = Expr::Bin(Bin::Pow, Box::new(left), Box::new(self.unary(true)?));
        }
        self.depth = base;
        Ok(left)
    }

    fn unary(&mut self, with_filter: bool) -> Result<Expr, Error> {
        deeper(|| self.unary_in(with_filter))
    }

    fn unary_in(&mut self, with_filter: bool) -> Result<Expr, Error> {
        self.enter()?;
        let base = self.depth;
        let mut node = if self.skip_op("-") {
            Expr::Neg(Box::new(self.unary(false)?))
        } else if self.skip_op("+") {
            Expr::Pos(Box::new(self.unary(false)?))
        } else {
            self.primary()?
        };
        node = self.postfix(node)?;
        if with_filter {
            node = self.filter_chain(Some(node))?;
        }
        self.depth = base - 1;
        Ok(node)
    }

    fn primary(&mut self) -> Result<Expr, Error> {
        deeper(|| self.primary_in())
    }

    fn primary_in(&mut self) -> Result<Expr, Error> {
        match self.next() {
            Tok::Name(name) => Ok(match name.as_str() {
                "true" | "True" => Expr::Const(Value::Bool(true)),
                "false" | "False" => Expr::Const(Value::Bool(false)),
                "none" | "None" => Expr::Const(Value::None),
                _ => Expr::Name(name),
            }),
            Tok::Str(mut text) => {
                while let Tok::Str(more) = self.peek().clone() {
                    text.push_str(&more);
                    self.at += 1;
                }
                Ok(Expr::Const(Value::from(text)))
            }
            Tok::Int(number) => Ok(Expr::Const(Value::Int(number))),
            Tok::Float(number) => Ok(Expr::Const(Value::Float(number))),
            Tok::Op("(") => {
                let mut items = Vec::new();
                let mut comma = false;
                while !self.is_op(")") {
                    if !items.is_empty() {
                        self.expect_op(",")?;
                        comma = true;
                        if self.is_op(")") {
                            break;
                        }
                    }
                    items.push(self.expression()?);
                }
                self.expect_op(")")?;
                Ok(if items.len() == 1 && !comma {
                    items.remove(0)
                } else {
                    Expr::Tuple(items)
                })
            }
            Tok::Op("[") => {
                let mut items = Vec::new();
                while !self.is_op("]") {
                    if !items.is_empty() {
                        self.expect_op(",")?;
                        if self.is_op("]") {
                            break;
                        }
                    }
                    items.push(self.expression()?);
                }
                self.expect_op("]")?;
                Ok(Expr::List(items))
            }
            Tok::Op("{") => {
                let mut pairs = Vec::new();
                while !self.is_op("}") {
                    if !pairs.is_empty() {
                        self.expect_op(",")?;
                        if self.is_op("}") {
                            break;
                        }
                    }
                    let key = self.expression()?;
                    self.expect_op(":")?;
                    pairs.push((key, self.expression()?));
                }
                self.expect_op("}")?;
                Ok(Expr::Dict(pairs))
            }
            _ => Err(lex_error("unexpected token in an expression")),
        }
    }

    fn postfix(&mut self, mut node: Expr) -> Result<Expr, Error> {
        let base = self.depth;
        loop {
            if self.is_op(".") || self.is_op("[") || self.is_op("(") {
                self.link()?;
            }
            if self.skip_op(".") {
                match self.next() {
                    Tok::Name(name) => node = Expr::Attr(Box::new(node), name),
                    Tok::Int(number) => {
                        node =
                            Expr::Item(Box::new(node), Box::new(Expr::Const(Value::Int(number))));
                    }
                    _ => return Err(lex_error("expected an attribute name")),
                }
            } else if self.skip_op("[") {
                let index = self.subscript()?;
                self.expect_op("]")?;
                node = Expr::Item(Box::new(node), Box::new(index));
            } else if self.is_op("(") {
                let args = self.call_args()?;
                node = Expr::Call(Box::new(node), args);
            } else {
                self.depth = base;
                return Ok(node);
            }
        }
    }

    fn subscript(&mut self) -> Result<Expr, Error> {
        let mut parts: Vec<Option<Box<Expr>>> = Vec::new();
        let mut colons = 0;
        loop {
            if self.is_op(":") {
                self.at += 1;
                colons += 1;
                if parts.len() < colons {
                    parts.push(None);
                }
                continue;
            }
            if self.is_op("]") {
                break;
            }
            if parts.len() > colons {
                self.expect_op(",")?;
                continue;
            }
            parts.push(Some(Box::new(self.expression()?)));
            if !self.is_op(":") && !self.is_op("]") {
                return Err(lex_error("expected ']'"));
            }
        }
        if colons == 0 {
            return parts
                .into_iter()
                .next()
                .flatten()
                .map(|boxed| *boxed)
                .ok_or_else(|| lex_error("empty subscript"));
        }
        parts.resize(3, None);
        let step = parts.pop().flatten();
        let stop = parts.pop().flatten();
        let start = parts.pop().flatten();
        Ok(Expr::Slice(start, stop, step))
    }

    fn call_args(&mut self) -> Result<Vec<Arg>, Error> {
        self.expect_op("(")?;
        let mut args = Vec::new();
        while !self.is_op(")") {
            if !args.is_empty() {
                self.expect_op(",")?;
                if self.is_op(")") {
                    break;
                }
            }
            if self.skip_op("**") {
                args.push(Arg::StarStar(self.expression()?));
            } else if self.skip_op("*") {
                args.push(Arg::Star(self.expression()?));
            } else if matches!(self.peek(), Tok::Name(_)) && matches!(self.look(1), Tok::Op("=")) {
                let name = self.name()?;
                self.at += 1;
                args.push(Arg::Named(name, self.expression()?));
            } else {
                args.push(Arg::Pos(self.expression()?));
            }
        }
        self.expect_op(")")?;
        Ok(args)
    }

    /// Filters (`| name(args)`) and tests (`is [not] name arg`) after an expression; with `None`
    /// the chain is the one of a `{% filter %}` block and starts at the filter's name.
    fn filter_chain(&mut self, start: Option<Expr>) -> Result<Expr, Error> {
        let base = self.depth;
        let mut node = start;
        let mut first = node.is_none();
        loop {
            if first || self.is_op("|") || self.is_name("is") {
                self.link()?;
            }
            if first || self.is_op("|") {
                if !first {
                    self.at += 1;
                }
                first = false;
                let mut name = self.name()?;
                while self.skip_op(".") {
                    name.push('.');
                    name.push_str(&self.name()?);
                }
                let args = if self.is_op("(") {
                    self.call_args()?
                } else {
                    Vec::new()
                };
                node = Some(Expr::Filter(node.map(Box::new), name, args));
            } else if self.is_name("is") {
                self.at += 1;
                let negated = self.skip_name("not");
                let mut name = self.name()?;
                while self.skip_op(".") {
                    name.push('.');
                    name.push_str(&self.name()?);
                }
                let args = if self.is_op("(") {
                    self.call_args()?
                } else if matches!(
                    self.peek(),
                    Tok::Name(_)
                        | Tok::Str(_)
                        | Tok::Int(_)
                        | Tok::Float(_)
                        | Tok::Op("(" | "[" | "{")
                ) && !["else", "or", "and", "if", "is"]
                    .iter()
                    .any(|word| self.is_name(word))
                {
                    vec![Arg::Pos(self.primary_with_postfix()?)]
                } else {
                    Vec::new()
                };
                let Some(subject) = node.take() else {
                    return Err(lex_error("a test needs a value"));
                };
                node = Some(Expr::Test(Box::new(subject), name, args, negated));
            } else if self.is_op("(") && node.is_some() && matches!(node, Some(Expr::Filter(..))) {
                let args = self.call_args()?;
                let callee = node.take().ok_or_else(|| lex_error("nothing to call"))?;
                node = Some(Expr::Call(Box::new(callee), args));
            } else {
                self.depth = base;
                return node.ok_or_else(|| lex_error("expected a filter"));
            }
        }
    }

    fn primary_with_postfix(&mut self) -> Result<Expr, Error> {
        let primary = self.primary()?;
        self.postfix(primary)
    }
}
