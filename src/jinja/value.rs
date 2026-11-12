//! The values a template computes, with Python's semantics where dbt macros lean on them: lists and
//! dicts are shared and mutable (`{% do rows.append(x) %}`), `True` prints as `True`, `None` as
//! `None`, a float as `repr` writes it, and a name that is not defined is a value that can be
//! called, indexed and iterated.

use std::cell::RefCell;
use std::cmp::Ordering;
use std::collections::HashMap;
use std::fmt::Write as _;
use std::rc::Rc;

use super::eval::{Callable, Error};
use super::stack::deeper;

/// A key of a dict: what Python hashes, with `True` equal to `1` and `1.0` to `1`.
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum Key {
    None,
    Int(i128),
    Float(u64),
    Str(Rc<str>),
    Tuple(Vec<Key>),
}

/// A dict that keeps the order its keys were added in.
#[derive(Clone, Debug, Default)]
pub struct Dict {
    entries: Vec<(Value, Value)>,
    index: HashMap<Key, usize>,
}

impl Dict {
    pub fn get(&self, key: &Value) -> Option<&Value> {
        let at = self.index.get(&key.key()?)?;
        Some(&self.entries[*at].1)
    }

    pub fn insert(&mut self, key: Value, value: Value) -> Result<(), Error> {
        let hashed = key
            .key()
            .ok_or_else(|| Error::new(format!("unhashable type: '{}'", key.type_name())))?;
        match self.index.get(&hashed) {
            Some(&at) => self.entries[at].1 = value,
            None => {
                self.index.insert(hashed, self.entries.len());
                self.entries.push((key, value));
            }
        }
        Ok(())
    }

    pub fn remove(&mut self, key: &Value) -> Option<Value> {
        let at = self.index.remove(&key.key()?)?;
        let (_, value) = self.entries.remove(at);
        for slot in self.index.values_mut() {
            if *slot > at {
                *slot -= 1;
            }
        }
        Some(value)
    }

    pub fn len(&self) -> usize {
        self.entries.len()
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    pub fn iter(&self) -> impl Iterator<Item = &(Value, Value)> {
        self.entries.iter()
    }

    pub fn clear(&mut self) {
        self.entries.clear();
        self.index.clear();
    }
}

/// An object with attributes and methods of its own: the stand-ins for dbt's `adapter`, `dbt`
/// and the packages' namespaces, `loop`, `namespace`, `cycler` and `joiner`.
pub trait Object {
    fn type_name(&self) -> &'static str;
    /// An attribute, or `None` when there is none.
    fn attr(&self, name: &str) -> Option<Value>;
    /// Sets an attribute (only a namespace accepts it).
    fn set_attr(&self, name: &str, _value: Value) -> Result<(), Error> {
        Err(Error::new(format!("cannot assign attribute '{name}'")))
    }
    /// Calls a method: `None` when the object has no such method.
    fn method(
        &self,
        _call: &mut dyn Callable,
        _name: &str,
        _args: &[Value],
        _kwargs: &[(String, Value)],
    ) -> Option<Result<Value, Error>> {
        None
    }
    /// Calls the object itself.
    fn call(
        &self,
        _call: &mut dyn Callable,
        _args: &[Value],
        _kwargs: &[(String, Value)],
    ) -> Option<Result<Value, Error>> {
        None
    }
    fn truthy(&self) -> bool {
        true
    }
    fn items(&self) -> Option<Vec<Value>> {
        None
    }
    fn text(&self) -> String {
        format!("<{}>", self.type_name())
    }
}

#[derive(Clone)]
pub enum Value {
    /// A name that is not defined. Called, it renders as a function call that keeps its arguments
    /// so that the SQL still parses, and iterated or indexed it is empty.
    Undefined,
    None,
    Bool(bool),
    Int(i128),
    Float(f64),
    Str(Rc<str>),
    /// Text that is already HTML, which an autoescaped output leaves as it is (Jinja2's `Markup`).
    Safe(Rc<str>),
    List(Rc<RefCell<Vec<Value>>>),
    Tuple(Rc<Vec<Value>>),
    Dict(Rc<RefCell<Dict>>),
    Range(i128, i128, i128),
    Object(Rc<dyn Object>),
    /// A macro defined in a template, a caller block or a function the host provides.
    Callable(Rc<super::eval::Function>),
    /// `value.name` taken without calling it.
    Bound(Rc<(Value, String)>),
}

impl std::fmt::Debug for Value {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.repr())
    }
}

impl From<&str> for Value {
    fn from(text: &str) -> Self {
        Value::Str(text.into())
    }
}

impl From<String> for Value {
    fn from(text: String) -> Self {
        Value::Str(text.into())
    }
}

impl From<bool> for Value {
    fn from(flag: bool) -> Self {
        Value::Bool(flag)
    }
}

impl From<i64> for Value {
    fn from(number: i64) -> Self {
        Value::Int(i128::from(number))
    }
}

impl Value {
    pub fn list(items: Vec<Value>) -> Value {
        Value::List(Rc::new(RefCell::new(items)))
    }

    pub fn tuple(items: Vec<Value>) -> Value {
        Value::Tuple(Rc::new(items))
    }

    pub fn dict(entries: Vec<(Value, Value)>) -> Value {
        let mut dict = Dict::default();
        for (key, value) in entries {
            // Keys here come from strings the host wrote.
            let _ = dict.insert(key, value);
        }
        Value::Dict(Rc::new(RefCell::new(dict)))
    }

    pub fn type_name(&self) -> &'static str {
        match self {
            Value::Undefined => "Undefined",
            Value::None => "NoneType",
            Value::Bool(_) => "bool",
            Value::Int(_) => "int",
            Value::Float(_) => "float",
            Value::Str(_) => "str",
            Value::Safe(_) => "Markup",
            Value::List(_) => "list",
            Value::Tuple(_) => "tuple",
            Value::Dict(_) => "dict",
            Value::Range(..) => "range",
            Value::Object(object) => object.type_name(),
            Value::Callable(_) | Value::Bound(_) => "function",
        }
    }

    pub fn is_str(&self) -> bool {
        matches!(self, Value::Str(_) | Value::Safe(_))
    }

    pub fn as_str(&self) -> Option<&str> {
        match self {
            Value::Str(text) | Value::Safe(text) => Some(text),
            _ => None,
        }
    }

    pub fn key(&self) -> Option<Key> {
        // A tuple nested deeper than a hundred levels is not a key.
        let _level = descend()?;
        Some(match self {
            Value::None => Key::None,
            Value::Bool(flag) => Key::Int(i128::from(*flag)),
            Value::Int(number) => Key::Int(*number),
            Value::Float(number) => {
                if number.fract() == 0.0 && number.abs() < 1e30 {
                    Key::Int(*number as i128)
                } else {
                    Key::Float(number.to_bits())
                }
            }
            Value::Str(text) | Value::Safe(text) => Key::Str(Rc::clone(text)),
            Value::Tuple(items) => Key::Tuple(items.iter().map(Value::key).collect::<Option<_>>()?),
            Value::Undefined => Key::Str("".into()),
            _ => return None,
        })
    }

    pub fn truthy(&self) -> bool {
        match self {
            Value::Undefined | Value::None => false,
            Value::Bool(flag) => *flag,
            Value::Int(number) => *number != 0,
            Value::Float(number) => *number != 0.0,
            Value::Str(text) | Value::Safe(text) => !text.is_empty(),
            Value::List(items) => !items.borrow().is_empty(),
            Value::Tuple(items) => !items.is_empty(),
            Value::Dict(entries) => !entries.borrow().is_empty(),
            Value::Range(start, stop, step) => range_len(*start, *stop, *step) > 0,
            Value::Object(object) => object.truthy(),
            Value::Callable(_) | Value::Bound(_) => true,
        }
    }

    /// The items of what can be iterated, or `None` for a value that cannot.
    pub fn items(&self) -> Option<Vec<Value>> {
        match self {
            Value::Undefined => Some(Vec::new()),
            Value::List(items) => Some(items.borrow().clone()),
            Value::Tuple(items) => Some(items.as_ref().clone()),
            Value::Dict(entries) => Some(entries.borrow().iter().map(|(k, _)| k.clone()).collect()),
            Value::Str(text) | Value::Safe(text) => {
                Some(text.chars().map(|c| Value::from(c.to_string())).collect())
            }
            Value::Range(start, stop, step) => {
                let count = range_len(*start, *stop, *step);
                Some((0..count).map(|n| Value::Int(start + n * step)).collect())
            }
            Value::Object(object) => object.items(),
            _ => None,
        }
    }

    pub fn length(&self) -> Option<usize> {
        match self {
            Value::Undefined => Some(0),
            Value::List(items) => Some(items.borrow().len()),
            Value::Tuple(items) => Some(items.len()),
            Value::Dict(entries) => Some(entries.borrow().len()),
            Value::Str(text) | Value::Safe(text) => Some(text.chars().count()),
            Value::Range(start, stop, step) => Some(range_len(*start, *stop, *step) as usize),
            Value::Object(object) => object.items().map(|items| items.len()),
            _ => None,
        }
    }

    /// `str(value)`.
    pub fn text(&self) -> String {
        match self {
            Value::Str(text) | Value::Safe(text) => text.to_string(),
            Value::Undefined => String::new(),
            Value::Object(object) => object.text(),
            _ => self.repr(),
        }
    }

    /// `repr(value)`.
    pub fn repr(&self) -> String {
        match descend() {
            None => "...".to_string(),
            Some(_guard) => deeper(|| self.repr_inner()),
        }
    }

    fn repr_inner(&self) -> String {
        match self {
            Value::Undefined => "Undefined".into(),
            Value::None => "None".into(),
            Value::Bool(true) => "True".into(),
            Value::Bool(false) => "False".into(),
            Value::Int(number) => number.to_string(),
            Value::Float(number) => float_repr(*number),
            Value::Str(text) => string_repr(text),
            Value::Safe(text) => format!("Markup({})", string_repr(text)),
            Value::List(items) => {
                let Some(_open) = Open::new(Rc::as_ptr(items) as usize) else {
                    return "[...]".to_string();
                };
                let items = items.borrow();
                let inner: Vec<String> = items.iter().map(Value::repr).collect();
                format!("[{}]", inner.join(", "))
            }
            Value::Tuple(items) => match items.as_slice() {
                [one] => format!("({},)", one.repr()),
                _ => format!(
                    "({})",
                    items.iter().map(Value::repr).collect::<Vec<_>>().join(", ")
                ),
            },
            Value::Dict(entries) => {
                let Some(_open) = Open::new(Rc::as_ptr(entries) as usize) else {
                    return "{...}".to_string();
                };
                let entries = entries.borrow();
                let inner: Vec<String> = entries
                    .iter()
                    .map(|(k, v)| format!("{}: {}", k.repr(), v.repr()))
                    .collect();
                format!("{{{}}}", inner.join(", "))
            }
            Value::Range(start, stop, step) => {
                if *step == 1 {
                    format!("range({start}, {stop})")
                } else {
                    format!("range({start}, {stop}, {step})")
                }
            }
            Value::Object(object) => object.text(),
            Value::Callable(function) => function.text(),
            Value::Bound(bound) => format!("<bound method {} of {}>", bound.1, bound.0.repr()),
        }
    }

    pub fn as_number(&self) -> Option<Number> {
        match self {
            Value::Bool(flag) => Some(Number::Int(i128::from(*flag))),
            Value::Int(number) => Some(Number::Int(*number)),
            Value::Float(number) => Some(Number::Float(*number)),
            _ => None,
        }
    }

    pub fn equals(&self, other: &Value) -> bool {
        match descend() {
            None => false,
            Some(_guard) => deeper(|| self.equals_inner(other)),
        }
    }

    fn equals_inner(&self, other: &Value) -> bool {
        match (self, other) {
            (Value::Undefined, Value::Undefined) | (Value::None, Value::None) => true,
            (Value::Str(a) | Value::Safe(a), Value::Str(b) | Value::Safe(b)) => a == b,
            (Value::List(a), Value::List(b)) => {
                Rc::ptr_eq(a, b) || {
                    let (a, b) = (a.borrow(), b.borrow());
                    a.len() == b.len() && a.iter().zip(b.iter()).all(|(x, y)| x.equals(y))
                }
            }
            (Value::Tuple(a), Value::Tuple(b)) => {
                a.len() == b.len() && a.iter().zip(b.iter()).all(|(x, y)| x.equals(y))
            }
            (Value::Dict(a), Value::Dict(b)) => {
                Rc::ptr_eq(a, b) || {
                    let (a, b) = (a.borrow(), b.borrow());
                    a.len() == b.len()
                        && a.iter()
                            .all(|(k, v)| b.get(k).is_some_and(|other| v.equals(other)))
                }
            }
            (Value::Range(..), Value::Range(..)) => {
                let (a, b) = (
                    self.items().unwrap_or_default(),
                    other.items().unwrap_or_default(),
                );
                a.len() == b.len() && a.iter().zip(b.iter()).all(|(x, y)| x.equals(y))
            }
            (Value::Object(a), Value::Object(b)) => Rc::ptr_eq(a, b),
            (Value::Callable(a), Value::Callable(b)) => Rc::ptr_eq(a, b),
            (a, b) => match (a.as_number(), b.as_number()) {
                (Some(x), Some(y)) => x.compare(y) == Some(Ordering::Equal),
                _ => false,
            },
        }
    }

    /// Python's ordering of two values, or an error for a pair it refuses to order.
    pub fn compare(&self, other: &Value) -> Result<Ordering, Error> {
        match descend() {
            None => Err(Error::new("maximum recursion depth exceeded in comparison")),
            Some(_guard) => deeper(|| self.compare_inner(other)),
        }
    }

    fn compare_inner(&self, other: &Value) -> Result<Ordering, Error> {
        match (self, other) {
            (Value::Str(a) | Value::Safe(a), Value::Str(b) | Value::Safe(b)) => Ok(a.cmp(b)),
            (Value::List(a), Value::List(b)) => {
                let (a, b) = (a.borrow().clone(), b.borrow().clone());
                sequence_order(&a, &b)
            }
            (Value::Tuple(a), Value::Tuple(b)) => sequence_order(a, b),
            (a, b) => match (a.as_number(), b.as_number()) {
                (Some(x), Some(y)) => x.compare(y).ok_or_else(|| Error::new("cannot order nan")),
                _ => Err(Error::new(format!(
                    "'<' not supported between instances of '{}' and '{}'",
                    a.type_name(),
                    b.type_name()
                ))),
            },
        }
    }
}

fn sequence_order(a: &[Value], b: &[Value]) -> Result<Ordering, Error> {
    for (x, y) in a.iter().zip(b) {
        if !x.equals(y) {
            return x.compare(y);
        }
    }
    Ok(a.len().cmp(&b.len()))
}

#[derive(Clone, Copy)]
pub enum Number {
    Int(i128),
    Float(f64),
}

impl Number {
    pub fn as_f64(self) -> f64 {
        match self {
            Number::Int(number) => number as f64,
            Number::Float(number) => number,
        }
    }

    pub fn compare(self, other: Number) -> Option<Ordering> {
        match (self, other) {
            (Number::Int(a), Number::Int(b)) => Some(a.cmp(&b)),
            (Number::Int(a), Number::Float(b)) => int_against_float(a, b),
            (Number::Float(a), Number::Int(b)) => int_against_float(b, a).map(Ordering::reverse),
            (Number::Float(a), Number::Float(b)) => a.partial_cmp(&b),
        }
    }
}

/// An integer against a float, exactly: neither is rounded to the other's precision.
fn int_against_float(int: i128, float: f64) -> Option<Ordering> {
    if float.is_nan() {
        return None;
    }
    if float.is_infinite() {
        return Some(if float > 0.0 {
            Ordering::Less
        } else {
            Ordering::Greater
        });
    }
    let whole = float.trunc();
    // 2**127, which no i128 reaches.
    const LIMIT: f64 = 1.7014118346046923e38;
    if whole >= LIMIT {
        return Some(Ordering::Less);
    }
    if whole <= -LIMIT {
        return Some(Ordering::Greater);
    }
    match int.cmp(&(whole as i128)) {
        Ordering::Equal => {
            let fraction = float - whole;
            Some(if fraction > 0.0 {
                Ordering::Less
            } else if fraction < 0.0 {
                Ordering::Greater
            } else {
                Ordering::Equal
            })
        }
        other => Some(other),
    }
}

pub fn range_len(start: i128, stop: i128, step: i128) -> i128 {
    let (low, high) = if step > 0 {
        (start, stop)
    } else {
        (stop, start)
    };
    if step == 0 || low >= high {
        return 0;
    }
    let span = high.abs_diff(low);
    let length = (span - 1) / step.unsigned_abs() + 1;
    i128::try_from(length).unwrap_or(i128::MAX)
}

/// `repr` of a float: the shortest digits that read back, in positional notation from 1e-4 up to
/// but not including 1e16 and with an exponent beyond.
pub fn float_repr(number: f64) -> String {
    if number.is_nan() {
        return "nan".into();
    }
    if number.is_infinite() {
        return if number > 0.0 { "inf" } else { "-inf" }.into();
    }
    // Rust's `{:e}` is the shortest representation that reads back: d.ddde<exp>.
    let scientific = format!("{number:e}");
    let (mantissa, exponent) = scientific.split_once('e').unwrap_or((&scientific, "0"));
    let exponent: i32 = exponent.parse().unwrap_or(0);
    let (sign, mantissa) = match mantissa.strip_prefix('-') {
        Some(rest) => ("-", rest),
        None => ("", mantissa),
    };
    let digits: String = mantissa.chars().filter(|c| *c != '.').collect();
    if !(-4..16).contains(&exponent) {
        let mut out = String::from(sign);
        out.push_str(&digits[..1]);
        if digits.len() > 1 {
            out.push('.');
            out.push_str(&digits[1..]);
        }
        let _ = write!(
            out,
            "e{}{:02}",
            if exponent < 0 { '-' } else { '+' },
            exponent.abs()
        );
        return out;
    }
    let mut out = String::from(sign);
    if exponent < 0 {
        out.push_str("0.");
        for _ in 0..(-exponent - 1) {
            out.push('0');
        }
        out.push_str(&digits);
    } else {
        let whole = exponent as usize + 1;
        if digits.len() <= whole {
            out.push_str(&digits);
            for _ in 0..(whole - digits.len()) {
                out.push('0');
            }
            out.push_str(".0");
        } else {
            out.push_str(&digits[..whole]);
            out.push('.');
            out.push_str(&digits[whole..]);
        }
    }
    out
}

/// `repr` of a string: single quotes unless the text has one and no double quote.
pub fn string_repr(text: &str) -> String {
    let quote = if text.contains('\'') && !text.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::with_capacity(text.len() + 2);
    out.push(quote);
    for c in text.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if (c as u32) < 0x20 || c as u32 == 0x7f => {
                let _ = write!(out, "\\x{:02x}", c as u32);
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

thread_local! {
    /// How many values deep a traversal of values is, and the containers it has open.
    static DEPTH: std::cell::Cell<usize> = const { std::cell::Cell::new(0) };
    static OPEN: RefCell<Vec<usize>> = const { RefCell::new(Vec::new()) };
}

/// How deep a traversal of nested values (printing, comparing) goes before it stops.
const MAX_VALUE_DEPTH: usize = 200;

/// One level of a traversal of values, given back when it is dropped.
struct Descent;

fn descend() -> Option<Descent> {
    DEPTH.with(|depth| {
        if depth.get() >= MAX_VALUE_DEPTH {
            None
        } else {
            depth.set(depth.get() + 1);
            Some(Descent)
        }
    })
}

impl Drop for Descent {
    fn drop(&mut self) {
        DEPTH.with(|depth| depth.set(depth.get().saturating_sub(1)));
    }
}

/// A container being printed, so that one that holds itself prints `[...]`.
struct Open;

impl Open {
    fn new(id: usize) -> Option<Open> {
        OPEN.with(|open| {
            let mut open = open.borrow_mut();
            if open.contains(&id) {
                None
            } else {
                open.push(id);
                Some(Open)
            }
        })
    }
}

impl Drop for Open {
    fn drop(&mut self) {
        OPEN.with(|open| {
            open.borrow_mut().pop();
        });
    }
}

impl Dict {
    /// The keys and values, taken out (for dropping a deep value without recursion).
    fn take_all(&mut self) -> Vec<Value> {
        self.index.clear();
        std::mem::take(&mut self.entries)
            .into_iter()
            .flat_map(|(k, v)| [k, v])
            .collect()
    }
}

impl Value {
    /// What a container holds, taken out of it when nothing else holds the container.
    fn take_children(&mut self, into: &mut Vec<Value>) {
        match self {
            Value::List(items) => {
                if let Some(cell) = Rc::get_mut(items) {
                    into.append(cell.get_mut());
                }
            }
            Value::Tuple(items) => {
                if let Some(vec) = Rc::get_mut(items) {
                    into.append(vec);
                }
            }
            Value::Dict(entries) => {
                if let Some(cell) = Rc::get_mut(entries) {
                    into.append(&mut cell.get_mut().take_all());
                }
            }
            _ => {}
        }
    }
}

/// A value nested a hundred thousand deep is dropped a level at a time, not by a hundred thousand
/// calls: what a container holds is moved to a list before the container goes.
impl Drop for Value {
    fn drop(&mut self) {
        let mut pending = Vec::new();
        self.take_children(&mut pending);
        while let Some(mut value) = pending.pop() {
            value.take_children(&mut pending);
        }
    }
}
