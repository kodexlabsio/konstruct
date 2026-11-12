//! What Jinja2 and Python provide to a template: operators, attributes and subscripts, the filters
//! and tests of Jinja2, and the methods of `str`, `list`, `dict` and `tuple` that dbt projects call.
//!
//! A name that is not an attribute or a method is not an error: it is an undefined value, as in
//! the renderer's sandbox, so that a call to it keeps its arguments in the SQL.

use std::cmp::Ordering;
use std::collections::HashMap;
use std::rc::Rc;

use super::eval::{Callable, Error, Function, Interp, Kwargs};
use super::parse::{Bin, Cmp};
use super::value::{Dict, Number, Object, Value, float_repr, range_len};

/// The sandbox's limit on the length of a `range`.
const MAX_RANGE: i128 = 100_000;

const DATEPARTS: [&str; 11] = [
    "year",
    "quarter",
    "month",
    "week",
    "day",
    "hour",
    "minute",
    "second",
    "millisecond",
    "microsecond",
    "dayofweek",
];

pub const FILTERS: &[&str] = &[
    "abs",
    "attr",
    "batch",
    "capitalize",
    "center",
    "count",
    "d",
    "default",
    "dictsort",
    "e",
    "escape",
    "first",
    "float",
    "forceescape",
    "format",
    "groupby",
    "indent",
    "int",
    "items",
    "join",
    "last",
    "length",
    "list",
    "lower",
    "map",
    "max",
    "min",
    "pprint",
    "reject",
    "rejectattr",
    "replace",
    "reverse",
    "round",
    "safe",
    "select",
    "selectattr",
    "slice",
    "sort",
    "string",
    "striptags",
    "sum",
    "title",
    "tojson",
    "trim",
    "truncate",
    "unique",
    "upper",
    "urlencode",
    "wordcount",
];

pub const TESTS: &[&str] = &[
    "callable",
    "defined",
    "divisibleby",
    "eq",
    "==",
    "equalto",
    "escaped",
    "even",
    "false",
    "filter",
    "float",
    "ge",
    ">=",
    "gt",
    ">",
    "greaterthan",
    "in",
    "integer",
    "iterable",
    "le",
    "<=",
    "lower",
    "lt",
    "<",
    "lessthan",
    "mapping",
    "ne",
    "!=",
    "none",
    "number",
    "odd",
    "sameas",
    "sequence",
    "string",
    "test",
    "true",
    "undefined",
    "upper",
];

fn type_error(message: impl Into<String>) -> Error {
    Error::new(message)
}

/// The arguments of a call: by position or by name.
struct Args<'a> {
    pos: &'a [Value],
    kw: &'a Kwargs,
}

impl<'a> Args<'a> {
    fn new(pos: &'a [Value], kw: &'a Kwargs) -> Self {
        Args { pos, kw }
    }

    fn get(&self, at: usize, name: &str) -> Option<&'a Value> {
        self.pos.get(at).or_else(|| {
            self.kw
                .iter()
                .find(|(key, _)| key == name)
                .map(|(_, value)| value)
        })
    }

    fn need(&self, at: usize, name: &str) -> Result<&'a Value, Error> {
        self.get(at, name)
            .ok_or_else(|| type_error(format!("missing argument '{name}'")))
    }

    fn text(&self, at: usize, name: &str, default: &str) -> String {
        self.get(at, name)
            .map_or_else(|| default.to_string(), Value::text)
    }

    fn int(&self, at: usize, name: &str, default: i128) -> Result<i128, Error> {
        match self.get(at, name) {
            None | Some(Value::None) => Ok(default),
            Some(value) => as_int(value),
        }
    }

    fn flag(&self, at: usize, name: &str, default: bool) -> bool {
        self.get(at, name).map_or(default, Value::truthy)
    }
}

fn as_int(value: &Value) -> Result<i128, Error> {
    match value.as_number() {
        Some(Number::Int(number)) => Ok(number),
        Some(Number::Float(number)) if number.fract() == 0.0 => Ok(number as i128),
        _ => Err(type_error(format!(
            "'{}' object cannot be interpreted as an integer",
            value.type_name()
        ))),
    }
}

fn as_index(value: &Value) -> Result<usize, Error> {
    usize::try_from(as_int(value)?).map_err(|_| type_error("a negative count"))
}

fn items_of(value: &Value) -> Result<Vec<Value>, Error> {
    value
        .items()
        .ok_or_else(|| type_error(format!("'{}' object is not iterable", value.type_name())))
}

pub fn negate(value: &Value) -> Result<Value, Error> {
    match value.as_number() {
        Some(Number::Int(number)) => number
            .checked_neg()
            .map(Value::Int)
            .ok_or_else(|| type_error("overflow")),
        Some(Number::Float(number)) => Ok(Value::Float(-number)),
        None => Err(type_error(format!(
            "bad operand type for unary -: '{}'",
            value.type_name()
        ))),
    }
}

pub fn positive(value: &Value) -> Result<Value, Error> {
    match value.as_number() {
        Some(Number::Int(number)) => Ok(Value::Int(number)),
        Some(Number::Float(number)) => Ok(Value::Float(number)),
        None => Err(type_error(format!(
            "bad operand type for unary +: '{}'",
            value.type_name()
        ))),
    }
}

fn overflow() -> Error {
    type_error("the number is too large")
}

pub fn binary(op: Bin, left: &Value, right: &Value) -> Result<Value, Error> {
    if let (Some(a), Some(b)) = (left.as_number(), right.as_number()) {
        return arithmetic(op, a, b);
    }
    match (op, left, right) {
        (Bin::Add, Value::Str(a), Value::Str(b)) => Ok(Value::from(format!("{a}{b}"))),
        (Bin::Add, Value::List(a), Value::List(b)) => {
            let mut joined = a.borrow().clone();
            joined.extend(b.borrow().iter().cloned());
            Ok(Value::list(joined))
        }
        (Bin::Add, Value::Tuple(a), Value::Tuple(b)) => {
            let mut joined = a.as_ref().clone();
            joined.extend(b.iter().cloned());
            Ok(Value::tuple(joined))
        }
        (Bin::Mul, Value::Str(text), count) | (Bin::Mul, count, Value::Str(text))
            if count.as_number().is_some() =>
        {
            let times = as_int(count)?.max(0);
            if times * (text.len() as i128) > 10_000_000 {
                return Err(type_error("the string is too long"));
            }
            Ok(Value::from(text.repeat(times as usize)))
        }
        (Bin::Mul, Value::List(items), count) | (Bin::Mul, count, Value::List(items))
            if count.as_number().is_some() =>
        {
            let times = as_int(count)?.max(0);
            let items = items.borrow();
            if times * (items.len() as i128) > 10_000_000 {
                return Err(type_error("the list is too long"));
            }
            let mut repeated = Vec::new();
            for _ in 0..times {
                repeated.extend(items.iter().cloned());
            }
            Ok(Value::list(repeated))
        }
        (Bin::Mod, Value::Str(format), args) => Ok(Value::from(percent_format(format, args)?)),
        _ => Err(type_error(format!(
            "unsupported operand types: '{}' and '{}'",
            left.type_name(),
            right.type_name()
        ))),
    }
}

fn arithmetic(op: Bin, a: Number, b: Number) -> Result<Value, Error> {
    if let (Number::Int(x), Number::Int(y)) = (a, b) {
        return Ok(match op {
            Bin::Add => Value::Int(x.checked_add(y).ok_or_else(overflow)?),
            Bin::Sub => Value::Int(x.checked_sub(y).ok_or_else(overflow)?),
            Bin::Mul => Value::Int(x.checked_mul(y).ok_or_else(overflow)?),
            Bin::Div => {
                if y == 0 {
                    return Err(type_error("division by zero"));
                }
                Value::Float(x as f64 / y as f64)
            }
            Bin::FloorDiv => {
                if y == 0 {
                    return Err(type_error("integer division or modulo by zero"));
                }
                Value::Int(x.div_euclid(y) - i128::from(y < 0 && x.rem_euclid(y) != 0))
            }
            Bin::Mod => {
                if y == 0 {
                    return Err(type_error("integer division or modulo by zero"));
                }
                let remainder = x % y;
                Value::Int(if remainder != 0 && (remainder < 0) != (y < 0) {
                    remainder + y
                } else {
                    remainder
                })
            }
            Bin::Pow => {
                if y < 0 {
                    Value::Float((x as f64).powf(y as f64))
                } else {
                    let exponent = u32::try_from(y).map_err(|_| overflow())?;
                    Value::Int(x.checked_pow(exponent).ok_or_else(overflow)?)
                }
            }
        });
    }
    let (x, y) = (a.as_f64(), b.as_f64());
    Ok(Value::Float(match op {
        Bin::Add => x + y,
        Bin::Sub => x - y,
        Bin::Mul => x * y,
        Bin::Div => {
            if y == 0.0 {
                return Err(type_error("float division by zero"));
            }
            x / y
        }
        Bin::FloorDiv => {
            if y == 0.0 {
                return Err(type_error("float floor division by zero"));
            }
            (x / y).floor()
        }
        Bin::Mod => {
            if y == 0.0 {
                return Err(type_error("float modulo"));
            }
            let remainder = x % y;
            if remainder != 0.0 && (remainder < 0.0) != (y < 0.0) {
                remainder + y
            } else {
                remainder
            }
        }
        Bin::Pow => x.powf(y),
    }))
}

pub fn compare(op: Cmp, left: &Value, right: &Value) -> Result<bool, Error> {
    Ok(match op {
        Cmp::Eq => left.equals(right),
        Cmp::Ne => !left.equals(right),
        Cmp::Lt => left.compare(right)? == Ordering::Less,
        Cmp::Le => left.compare(right)? != Ordering::Greater,
        Cmp::Gt => left.compare(right)? == Ordering::Greater,
        Cmp::Ge => left.compare(right)? != Ordering::Less,
        Cmp::In => contains(right, left)?,
        Cmp::NotIn => !contains(right, left)?,
    })
}

fn contains(container: &Value, item: &Value) -> Result<bool, Error> {
    match container {
        Value::Str(text) => match item {
            Value::Str(part) => Ok(text.contains(part.as_ref())),
            other => Err(type_error(format!(
                "'in <string>' requires string as left operand, not {}",
                other.type_name()
            ))),
        },
        Value::Dict(entries) => Ok(entries.borrow().get(item).is_some()),
        Value::Undefined => Ok(false),
        other => Ok(items_of(other)?
            .iter()
            .any(|candidate| candidate.equals(item))),
    }
}

const DICT_METHODS: &[&str] = &[
    "get",
    "keys",
    "values",
    "items",
    "update",
    "pop",
    "setdefault",
    "copy",
    "clear",
];
const LIST_METHODS: &[&str] = &[
    "append", "extend", "insert", "pop", "remove", "index", "count", "reverse", "sort", "copy",
    "clear",
];
const TUPLE_METHODS: &[&str] = &["index", "count"];
const STR_METHODS: &[&str] = &[
    "capitalize",
    "casefold",
    "center",
    "count",
    "endswith",
    "find",
    "format",
    "format_map",
    "index",
    "isalnum",
    "isalpha",
    "isascii",
    "isdecimal",
    "isdigit",
    "islower",
    "isnumeric",
    "isspace",
    "isupper",
    "join",
    "ljust",
    "lower",
    "lstrip",
    "partition",
    "removeprefix",
    "removesuffix",
    "replace",
    "rfind",
    "rindex",
    "rjust",
    "rpartition",
    "rsplit",
    "rstrip",
    "split",
    "splitlines",
    "startswith",
    "strip",
    "swapcase",
    "title",
    "upper",
    "zfill",
];

fn has_method(value: &Value, name: &str) -> bool {
    let table: &[&str] = match value {
        Value::Dict(_) => DICT_METHODS,
        Value::List(_) => LIST_METHODS,
        Value::Tuple(_) => TUPLE_METHODS,
        Value::Str(_) => STR_METHODS,
        _ => return false,
    };
    table.contains(&name)
}

/// `value.name` taken without a call.
pub fn get_attr(value: &Value, name: &str) -> Value {
    if name.starts_with('_') {
        // Python's own attributes (`__class__`) are refused; a dict's key that starts with an
        // underscore is an item and still reads.
        if let (Value::Dict(entries), false) =
            (value, name.starts_with("__") && name.ends_with("__"))
        {
            return entries
                .borrow()
                .get(&Value::from(name))
                .cloned()
                .unwrap_or(Value::Undefined);
        }
        return Value::Undefined;
    }
    match value {
        Value::Object(object) => object.attr(name).unwrap_or(Value::Undefined),
        Value::Callable(function) => function.attr(name).unwrap_or(Value::Undefined),
        Value::Dict(entries) => {
            if has_method(value, name) {
                return Value::Bound(Rc::new((value.clone(), name.to_string())));
            }
            entries
                .borrow()
                .get(&Value::from(name))
                .cloned()
                .unwrap_or(Value::Undefined)
        }
        Value::Tuple(items) if items.len() == 2 && (name == "grouper" || name == "list") => {
            items[usize::from(name == "list")].clone()
        }
        _ if has_method(value, name) => Value::Bound(Rc::new((value.clone(), name.to_string()))),
        _ => Value::Undefined,
    }
}

fn index_of(length: usize, key: &Value) -> Result<Option<usize>, Error> {
    let at = as_int(key)?;
    let at = if at < 0 { at + length as i128 } else { at };
    Ok(usize::try_from(at).ok().filter(|at| *at < length))
}

pub fn get_item(value: &Value, key: &Value) -> Result<Value, Error> {
    Ok(match value {
        Value::Undefined => Value::Undefined,
        Value::Dict(entries) => match entries.borrow().get(key) {
            Some(found) => found.clone(),
            None => match key {
                Value::Str(name) => get_attr(value, name),
                _ => Value::Undefined,
            },
        },
        Value::List(items) if key.as_number().is_some() => {
            let items = items.borrow();
            index_of(items.len(), key)?.map_or(Value::Undefined, |at| items[at].clone())
        }
        Value::Tuple(items) if key.as_number().is_some() => {
            index_of(items.len(), key)?.map_or(Value::Undefined, |at| items[at].clone())
        }
        Value::Str(text) if key.as_number().is_some() => {
            let chars: Vec<char> = text.chars().collect();
            index_of(chars.len(), key)?
                .map_or(Value::Undefined, |at| Value::from(chars[at].to_string()))
        }
        Value::Range(..) if key.as_number().is_some() => {
            let items = items_of(value)?;
            index_of(items.len(), key)?.map_or(Value::Undefined, |at| items[at].clone())
        }
        Value::Object(object) => match key {
            Value::Str(name) if !name.starts_with('_') => {
                object.attr(name).unwrap_or(Value::Undefined)
            }
            _ => Value::Undefined,
        },
        Value::Str(_) | Value::List(_) | Value::Tuple(_) => match key {
            Value::Str(name) => get_attr(value, name),
            _ => return Err(type_error("indices must be integers")),
        },
        _ => {
            return Err(type_error(format!(
                "'{}' object is not subscriptable",
                value.type_name()
            )));
        }
    })
}

pub fn slice(
    value: &Value,
    start: Option<&Value>,
    stop: Option<&Value>,
    step: Option<&Value>,
) -> Result<Value, Error> {
    if matches!(value, Value::Undefined) {
        return Ok(Value::Undefined);
    }
    let items: Vec<Value> = match value {
        Value::Str(text) => text.chars().map(|c| Value::from(c.to_string())).collect(),
        Value::List(_) | Value::Tuple(_) | Value::Range(..) => items_of(value)?,
        _ => {
            return Err(type_error(format!(
                "'{}' object is not subscriptable",
                value.type_name()
            )));
        }
    };
    let length = items.len() as i128;
    let step = match step {
        None | Some(Value::None) => 1,
        Some(step) => as_int(step)?,
    };
    if step == 0 {
        return Err(type_error("slice step cannot be zero"));
    }
    let bound =
        |given: Option<&Value>, default: i128, low: i128, high: i128| -> Result<i128, Error> {
            Ok(match given {
                None | Some(Value::None) => default,
                Some(at) => {
                    let at = as_int(at)?;
                    (if at < 0 { at + length } else { at }).clamp(low, high)
                }
            })
        };
    let picked: Vec<Value> = if step > 0 {
        let (from, to) = (bound(start, 0, 0, length)?, bound(stop, length, 0, length)?);
        (from..to)
            .step_by(step as usize)
            .map(|at| items[at as usize].clone())
            .collect()
    } else {
        let (from, to) = (
            bound(start, length - 1, -1, length - 1)?,
            bound(stop, -1, -1, length - 1)?,
        );
        let mut taken = Vec::new();
        let mut at = from;
        while at > to {
            taken.push(items[at as usize].clone());
            at += step;
        }
        taken
    };
    Ok(match value {
        Value::Str(_) => Value::from(picked.iter().map(Value::text).collect::<String>()),
        Value::Tuple(_) => Value::tuple(picked),
        _ => Value::list(picked),
    })
}

fn sql_argument(value: &Value) -> String {
    match value {
        Value::List(_) | Value::Tuple(_) => value
            .items()
            .unwrap_or_default()
            .iter()
            .map(sql_argument)
            .collect::<Vec<_>>()
            .join(", "),
        other => {
            let text = other.text();
            if DATEPARTS.contains(&text.to_lowercase().as_str()) {
                format!("'{text}'")
            } else {
                text
            }
        }
    }
}

/// An undefined name, called: a function call that keeps its arguments, so that the SQL still
/// parses and the columns passed to an unknown macro still appear in the lineage.
pub fn lenient_call(args: &[Value], kwargs: &Kwargs) -> Value {
    let values: Vec<String> = args
        .iter()
        .chain(kwargs.iter().map(|(_, value)| value))
        .map(sql_argument)
        .collect();
    if values.is_empty() {
        Value::from("")
    } else {
        Value::from(format!("konstruct_macro({})", values.join(", ")))
    }
}

pub fn call_method(
    interp: &mut Interp,
    object: &Value,
    name: &str,
    args: &[Value],
    kwargs: &Kwargs,
) -> Result<Value, Error> {
    if name.starts_with('_') || matches!(object, Value::Undefined) {
        return Ok(lenient_call(args, kwargs));
    }
    let known = match object {
        Value::Str(text) => str_method(interp, text, name, args, kwargs).transpose()?,
        Value::List(items) => list_method(items, name, args, kwargs).transpose()?,
        Value::Tuple(items) => tuple_method(items, name, args).transpose()?,
        Value::Dict(entries) => dict_method(object, entries, name, args, kwargs).transpose()?,
        Value::Object(handle) => {
            let handle = Rc::clone(handle);
            if let Some(result) = handle.method(interp, name, args, kwargs) {
                return result;
            }
            None
        }
        _ => None,
    };
    if let Some(value) = known {
        return Ok(value);
    }
    match get_attr(object, name) {
        Value::Undefined => Ok(lenient_call(args, kwargs)),
        stored => interp.call_value(&stored, args, kwargs),
    }
}

fn tuple_method(items: &[Value], name: &str, args: &[Value]) -> Option<Result<Value, Error>> {
    match name {
        "index" => Some(
            args.first()
                .and_then(|wanted| items.iter().position(|item| item.equals(wanted)))
                .map(|at| Value::Int(at as i128))
                .ok_or_else(|| type_error("tuple.index(x): x not in tuple")),
        ),
        "count" => Some(Ok(Value::Int(args.first().map_or(0, |wanted| {
            items.iter().filter(|item| item.equals(wanted)).count()
        }) as i128))),
        _ => None,
    }
}

fn list_method(
    items: &Rc<std::cell::RefCell<Vec<Value>>>,
    name: &str,
    args: &[Value],
    kwargs: &Kwargs,
) -> Option<Result<Value, Error>> {
    let given = Args::new(args, kwargs);
    let run = || -> Result<Value, Error> {
        let mut list = items.borrow_mut();
        Ok(match name {
            "append" => {
                list.push(given.need(0, "object")?.clone());
                Value::None
            }
            "extend" => {
                let more = items_of(given.need(0, "iterable")?)?;
                list.extend(more);
                Value::None
            }
            "insert" => {
                let at = as_int(given.need(0, "index")?)?;
                let at = if at < 0 {
                    (at + list.len() as i128).max(0)
                } else {
                    at.min(list.len() as i128)
                };
                list.insert(at as usize, given.need(1, "object")?.clone());
                Value::None
            }
            "pop" => {
                if list.is_empty() {
                    return Err(type_error("pop from empty list"));
                }
                let at = match given.get(0, "index") {
                    Some(at) => index_of(list.len(), at)?
                        .ok_or_else(|| type_error("pop index out of range"))?,
                    None => list.len() - 1,
                };
                list.remove(at)
            }
            "remove" => {
                let wanted = given.need(0, "value")?;
                let at = list
                    .iter()
                    .position(|item| item.equals(wanted))
                    .ok_or_else(|| type_error("list.remove(x): x not in list"))?;
                list.remove(at);
                Value::None
            }
            "index" => {
                let wanted = given.need(0, "value")?;
                let at = list
                    .iter()
                    .position(|item| item.equals(wanted))
                    .ok_or_else(|| type_error("value is not in list"))?;
                Value::Int(at as i128)
            }
            "count" => {
                let wanted = given.need(0, "value")?;
                Value::Int(list.iter().filter(|item| item.equals(wanted)).count() as i128)
            }
            "reverse" => {
                list.reverse();
                Value::None
            }
            "sort" => {
                let reverse = given.flag(usize::MAX, "reverse", false);
                let mut failure = None;
                list.sort_by(|a, b| {
                    a.compare(b).unwrap_or_else(|error| {
                        failure = Some(error);
                        Ordering::Equal
                    })
                });
                if let Some(error) = failure {
                    return Err(error);
                }
                if reverse {
                    list.reverse();
                }
                Value::None
            }
            "copy" => Value::list(list.clone()),
            "clear" => {
                list.clear();
                Value::None
            }
            _ => return Err(type_error("no such method")),
        })
    };
    LIST_METHODS.contains(&name).then(run)
}

fn dict_method(
    this: &Value,
    entries: &Rc<std::cell::RefCell<Dict>>,
    name: &str,
    args: &[Value],
    kwargs: &Kwargs,
) -> Option<Result<Value, Error>> {
    if !DICT_METHODS.contains(&name) {
        return None;
    }
    let given = Args::new(args, kwargs);
    let run = || -> Result<Value, Error> {
        Ok(match name {
            "get" => {
                let found = entries.borrow().get(given.need(0, "key")?).cloned();
                found.unwrap_or_else(|| given.get(1, "default").cloned().unwrap_or(Value::None))
            }
            "keys" => Value::list(entries.borrow().iter().map(|(k, _)| k.clone()).collect()),
            "values" => Value::list(entries.borrow().iter().map(|(_, v)| v.clone()).collect()),
            "items" => Value::list(
                entries
                    .borrow()
                    .iter()
                    .map(|(k, v)| Value::tuple(vec![k.clone(), v.clone()]))
                    .collect(),
            ),
            "update" => {
                let mut incoming: Vec<(Value, Value)> = Vec::new();
                if let Some(other) = given.get(0, "other") {
                    match other {
                        Value::Dict(source) => incoming.extend(source.borrow().iter().cloned()),
                        pairs => {
                            for pair in items_of(pairs)? {
                                let pair = items_of(&pair)?;
                                if pair.len() != 2 {
                                    return Err(type_error(
                                        "dictionary update sequence element is not a pair",
                                    ));
                                }
                                incoming.push((pair[0].clone(), pair[1].clone()));
                            }
                        }
                    }
                }
                incoming.extend(
                    kwargs
                        .iter()
                        .map(|(k, v)| (Value::from(k.as_str()), v.clone())),
                );
                let mut dict = entries.borrow_mut();
                for (key, value) in incoming {
                    dict.insert(key, value)?;
                }
                Value::None
            }
            "pop" => {
                let removed = entries.borrow_mut().remove(given.need(0, "key")?);
                match (removed, given.get(1, "default")) {
                    (Some(value), _) => value,
                    (None, Some(default)) => default.clone(),
                    (None, None) => return Err(type_error("key not found")),
                }
            }
            "setdefault" => {
                let key = given.need(0, "key")?;
                let existing = entries.borrow().get(key).cloned();
                match existing {
                    Some(value) => value,
                    None => {
                        let default = given.get(1, "default").cloned().unwrap_or(Value::None);
                        entries.borrow_mut().insert(key.clone(), default.clone())?;
                        default
                    }
                }
            }
            "copy" => {
                let copy = entries.borrow().clone();
                Value::Dict(Rc::new(std::cell::RefCell::new(copy)))
            }
            "clear" => {
                entries.borrow_mut().clear();
                Value::None
            }
            _ => return Err(type_error("no such method")),
        })
    };
    let _ = this;
    Some(run())
}

fn char_find(text: &str, needle: &str, from: usize) -> Option<usize> {
    let skip: usize = text.chars().take(from).map(char::len_utf8).sum();
    text.get(skip..)?
        .find(needle)
        .map(|byte| text[..skip + byte].chars().count())
}

fn char_rfind(text: &str, needle: &str) -> Option<usize> {
    text.rfind(needle).map(|byte| text[..byte].chars().count())
}

fn strip_set(args: &Args) -> Option<Vec<char>> {
    match args.get(0, "chars") {
        None | Some(Value::None) => None,
        Some(chars) => Some(chars.text().chars().collect()),
    }
}

fn split_on(
    text: &str,
    separator: Option<String>,
    limit: i128,
    from_right: bool,
) -> Result<Vec<Value>, Error> {
    let limit = if limit < 0 {
        usize::MAX
    } else {
        limit as usize
    };
    let parts: Vec<String> = match separator {
        None => {
            let words: Vec<&str> = text.split_whitespace().collect();
            if limit >= words.len() || limit == usize::MAX {
                words.iter().map(|w| (*w).to_string()).collect()
            } else if from_right {
                let head = words.len() - limit;
                let mut parts = vec![words[..head].join(" ")];
                parts.extend(words[head..].iter().map(|w| (*w).to_string()));
                parts
            } else {
                let mut parts: Vec<String> =
                    words[..limit].iter().map(|w| (*w).to_string()).collect();
                let mut rest = text.trim_start();
                for word in &words[..limit] {
                    rest = rest[word.len()..].trim_start();
                }
                parts.push(rest.to_string());
                parts
            }
        }
        Some(separator) => {
            if separator.is_empty() {
                return Err(type_error("empty separator"));
            }
            if from_right {
                text.rsplitn(limit.saturating_add(1), separator.as_str())
                    .map(str::to_string)
                    .collect::<Vec<_>>()
                    .into_iter()
                    .rev()
                    .collect()
            } else {
                text.splitn(limit.saturating_add(1), separator.as_str())
                    .map(str::to_string)
                    .collect()
            }
        }
    };
    Ok(parts.into_iter().map(Value::from).collect())
}

fn python_title(text: &str) -> String {
    let mut out = String::new();
    let mut previous_letter = false;
    for c in text.chars() {
        if c.is_alphabetic() {
            if previous_letter {
                out.extend(c.to_lowercase());
            } else {
                out.extend(c.to_uppercase());
            }
            previous_letter = true;
        } else {
            out.push(c);
            previous_letter = false;
        }
    }
    out
}

fn str_method(
    interp: &mut Interp,
    text: &Rc<str>,
    name: &str,
    args: &[Value],
    kwargs: &Kwargs,
) -> Option<Result<Value, Error>> {
    if !STR_METHODS.contains(&name) {
        return None;
    }
    let given = Args::new(args, kwargs);
    let flag = |test: fn(char) -> bool| Value::Bool(!text.is_empty() && text.chars().all(test));
    let mut run = || -> Result<Value, Error> {
        Ok(match name {
            "upper" => Value::from(text.to_uppercase()),
            "lower" | "casefold" => Value::from(text.to_lowercase()),
            "capitalize" => {
                let mut chars = text.chars();
                let head: String = chars
                    .next()
                    .map(|c| c.to_uppercase().collect())
                    .unwrap_or_default();
                Value::from(head + &chars.as_str().to_lowercase())
            }
            "title" => Value::from(python_title(text)),
            "swapcase" => Value::from(
                text.chars()
                    .map(|c| {
                        if c.is_uppercase() {
                            c.to_lowercase().collect::<String>()
                        } else {
                            c.to_uppercase().collect()
                        }
                    })
                    .collect::<String>(),
            ),
            "strip" => Value::from(match strip_set(&given) {
                None => text.trim().to_string(),
                Some(set) => text.trim_matches(|c| set.contains(&c)).to_string(),
            }),
            "lstrip" => Value::from(match strip_set(&given) {
                None => text.trim_start().to_string(),
                Some(set) => text.trim_start_matches(|c| set.contains(&c)).to_string(),
            }),
            "rstrip" => Value::from(match strip_set(&given) {
                None => text.trim_end().to_string(),
                Some(set) => text.trim_end_matches(|c| set.contains(&c)).to_string(),
            }),
            "split" | "rsplit" => {
                let separator = match given.get(0, "sep") {
                    None | Some(Value::None) => None,
                    Some(sep) => Some(sep.text()),
                };
                Value::list(split_on(
                    text,
                    separator,
                    given.int(1, "maxsplit", -1)?,
                    name == "rsplit",
                )?)
            }
            "splitlines" => Value::list(text.lines().map(Value::from).collect()),
            "replace" => {
                let (old, new) = (given.need(0, "old")?.text(), given.need(1, "new")?.text());
                let count = given.int(2, "count", -1)?;
                Value::from(if count < 0 {
                    text.replace(&old, &new)
                } else {
                    text.replacen(&old, &new, count as usize)
                })
            }
            "startswith" | "endswith" => {
                let prefixes: Vec<String> = match given.need(0, "prefix")? {
                    Value::Tuple(options) => options.iter().map(Value::text).collect(),
                    one => vec![one.text()],
                };
                Value::Bool(prefixes.iter().any(|p| {
                    if name == "startswith" {
                        text.starts_with(p)
                    } else {
                        text.ends_with(p)
                    }
                }))
            }
            "find" | "index" => {
                let needle = given.need(0, "sub")?.text();
                let from = given.int(1, "start", 0)?.max(0) as usize;
                match char_find(text, &needle, from) {
                    Some(at) => Value::Int(at as i128),
                    None if name == "find" => Value::Int(-1),
                    None => return Err(type_error("substring not found")),
                }
            }
            "rfind" | "rindex" => {
                let needle = given.need(0, "sub")?.text();
                match char_rfind(text, &needle) {
                    Some(at) => Value::Int(at as i128),
                    None if name == "rfind" => Value::Int(-1),
                    None => return Err(type_error("substring not found")),
                }
            }
            "count" => {
                let needle = given.need(0, "sub")?.text();
                Value::Int(if needle.is_empty() {
                    text.chars().count() as i128 + 1
                } else {
                    text.matches(&needle).count() as i128
                })
            }
            "format" => Value::from(str_format(interp, text, args, kwargs)?),
            "format_map" => {
                let mapping = given.need(0, "mapping")?;
                let named: Vec<(String, Value)> = match mapping {
                    Value::Dict(entries) => entries
                        .borrow()
                        .iter()
                        .map(|(k, v)| (k.text(), v.clone()))
                        .collect(),
                    _ => Vec::new(),
                };
                Value::from(str_format(interp, text, &[], &named)?)
            }
            "join" => {
                let mut parts = Vec::new();
                for item in items_of(given.need(0, "iterable")?)? {
                    match item {
                        Value::Str(part) => parts.push(part.to_string()),
                        other => {
                            return Err(type_error(format!(
                                "sequence item: expected str, {} found",
                                other.type_name()
                            )));
                        }
                    }
                }
                Value::from(parts.join(text))
            }
            "ljust" | "rjust" | "center" => {
                let width = given.int(0, "width", 0)?.max(0) as usize;
                let fill = given.text(1, "fillchar", " ").chars().next().unwrap_or(' ');
                let length = text.chars().count();
                if width <= length {
                    return Ok(Value::Str(Rc::clone(text)));
                }
                let pad = width - length;
                let left = match name {
                    "ljust" => 0,
                    "rjust" => pad,
                    _ => pad / 2 + (pad & width & 1),
                };
                let make = |n: usize| std::iter::repeat_n(fill, n).collect::<String>();
                Value::from(format!("{}{}{}", make(left), text, make(pad - left)))
            }
            "zfill" => {
                let width = given.int(0, "width", 0)?.max(0) as usize;
                let length = text.chars().count();
                if width <= length {
                    return Ok(Value::Str(Rc::clone(text)));
                }
                let (sign, digits) = match text.chars().next() {
                    Some(c @ ('+' | '-')) => (c.to_string(), &text[1..]),
                    _ => (String::new(), &text[..]),
                };
                Value::from(format!("{sign}{}{digits}", "0".repeat(width - length)))
            }
            "partition" | "rpartition" => {
                let separator = given.need(0, "sep")?.text();
                let found = if name == "partition" {
                    text.find(&separator)
                } else {
                    text.rfind(&separator)
                };
                match found {
                    Some(at) => Value::tuple(vec![
                        Value::from(&text[..at]),
                        Value::from(separator.as_str()),
                        Value::from(&text[at + separator.len()..]),
                    ]),
                    None if name == "partition" => Value::tuple(vec![
                        Value::Str(Rc::clone(text)),
                        Value::from(""),
                        Value::from(""),
                    ]),
                    None => Value::tuple(vec![
                        Value::from(""),
                        Value::from(""),
                        Value::Str(Rc::clone(text)),
                    ]),
                }
            }
            "removeprefix" => {
                let prefix = given.need(0, "prefix")?.text();
                Value::from(text.strip_prefix(prefix.as_str()).unwrap_or(text))
            }
            "removesuffix" => {
                let suffix = given.need(0, "suffix")?.text();
                Value::from(text.strip_suffix(suffix.as_str()).unwrap_or(text))
            }
            "isdigit" | "isdecimal" | "isnumeric" => flag(|c| c.is_numeric()),
            "isalpha" => flag(char::is_alphabetic),
            "isalnum" => flag(char::is_alphanumeric),
            "isspace" => flag(char::is_whitespace),
            "isascii" => Value::Bool(text.is_ascii()),
            "islower" => {
                Value::Bool(text.chars().any(char::is_alphabetic) && text.to_lowercase() == **text)
            }
            "isupper" => {
                Value::Bool(text.chars().any(char::is_alphabetic) && text.to_uppercase() == **text)
            }
            _ => return Err(type_error("no such method")),
        })
    };
    Some(run())
}

/// `str.format`: `{}`, `{0}`, `{name}`, `{0.attr}`, `{0[1]}`, `!r`, `!s` and a format spec.
fn str_format(
    interp: &mut Interp,
    template: &str,
    args: &[Value],
    kwargs: &Kwargs,
) -> Result<String, Error> {
    let chars: Vec<char> = template.chars().collect();
    let mut out = String::new();
    let mut at = 0;
    let mut automatic = 0;
    while at < chars.len() {
        let c = chars[at];
        if c == '{' && chars.get(at + 1) == Some(&'{') {
            out.push('{');
            at += 2;
        } else if c == '}' && chars.get(at + 1) == Some(&'}') {
            out.push('}');
            at += 2;
        } else if c == '{' {
            let mut depth = 1;
            let mut end = at + 1;
            while end < chars.len() && depth > 0 {
                match chars[end] {
                    '{' => depth += 1,
                    '}' => depth -= 1,
                    _ => {}
                }
                end += 1;
            }
            if depth > 0 {
                return Err(type_error("expected '}' before end of string"));
            }
            let field: String = chars[at + 1..end - 1].iter().collect();
            out.push_str(&format_field(interp, &field, args, kwargs, &mut automatic)?);
            at = end;
        } else if c == '}' {
            return Err(type_error("single '}' encountered in format string"));
        } else {
            out.push(c);
            at += 1;
        }
    }
    Ok(out)
}

fn format_field(
    interp: &mut Interp,
    field: &str,
    args: &[Value],
    kwargs: &Kwargs,
    automatic: &mut usize,
) -> Result<String, Error> {
    let (head, spec) = match field.split_once(':') {
        Some((head, spec)) => (head, spec),
        None => (field, ""),
    };
    let (name, conversion) = match head.split_once('!') {
        Some((name, conversion)) => (name, Some(conversion)),
        None => (head, None),
    };
    let split = name.find(['.', '[']).unwrap_or(name.len());
    let (first, rest) = name.split_at(split);
    let mut value = if first.is_empty() {
        *automatic += 1;
        args.get(*automatic - 1)
            .cloned()
            .ok_or_else(|| type_error("replacement index out of range"))?
    } else if let Ok(position) = first.parse::<usize>() {
        args.get(position)
            .cloned()
            .ok_or_else(|| type_error("replacement index out of range"))?
    } else {
        kwargs
            .iter()
            .find(|(key, _)| key == first)
            .map(|(_, value)| value.clone())
            .ok_or_else(|| type_error(format!("'{first}'")))?
    };
    let mut remaining = rest;
    while !remaining.is_empty() {
        if let Some(after) = remaining.strip_prefix('.') {
            let end = after.find(['.', '[']).unwrap_or(after.len());
            value = get_attr(&value, &after[..end]);
            remaining = &after[end..];
        } else if let Some(after) = remaining.strip_prefix('[') {
            let end = after.find(']').ok_or_else(|| type_error("expected ']'"))?;
            let key = &after[..end];
            let key = key
                .parse::<i64>()
                .map_or_else(|_| Value::from(key), Value::from);
            value = get_item(&value, &key)?;
            remaining = &after[end + 1..];
        } else {
            return Err(type_error("bad field name"));
        }
    }
    let _ = interp;
    let value = match conversion {
        Some("r" | "a") => Value::from(value.repr()),
        Some("s") => Value::from(value.text()),
        _ => value,
    };
    format_spec(&value, spec)
}

/// The format-spec mini-language: fill, align, sign, `0`, width, `,`, precision and type.
fn format_spec(value: &Value, spec: &str) -> Result<String, Error> {
    if spec.is_empty() {
        return Ok(value.text());
    }
    let chars: Vec<char> = spec.chars().collect();
    let mut at = 0;
    let (mut fill, mut align) = (' ', None);
    if chars.len() > 1 && matches!(chars[1], '<' | '>' | '^' | '=') {
        fill = chars[0];
        align = Some(chars[1]);
        at = 2;
    } else if matches!(chars.first(), Some('<' | '>' | '^' | '=')) {
        align = Some(chars[0]);
        at = 1;
    }
    let mut sign = '-';
    if matches!(chars.get(at), Some('+' | '-' | ' ')) {
        sign = chars[at];
        at += 1;
    }
    if chars.get(at) == Some(&'#') {
        at += 1;
    }
    if chars.get(at) == Some(&'0') {
        if align.is_none() {
            fill = '0';
            align = Some('=');
        }
        at += 1;
    }
    let digits = |at: &mut usize| {
        let start = *at;
        while chars.get(*at).is_some_and(char::is_ascii_digit) {
            *at += 1;
        }
        chars[start..*at]
            .iter()
            .collect::<String>()
            .parse::<usize>()
            .ok()
    };
    let width = digits(&mut at);
    let grouping = chars.get(at) == Some(&',');
    if grouping {
        at += 1;
    }
    let precision = if chars.get(at) == Some(&'.') {
        at += 1;
        digits(&mut at)
    } else {
        None
    };
    let kind = chars.get(at).copied();
    let (negative, body) = match (value.as_number(), kind) {
        (Some(Number::Int(number)), None | Some('d' | 'n')) => {
            (number < 0, number.unsigned_abs().to_string())
        }
        (Some(Number::Int(number)), Some('x')) => {
            (number < 0, format!("{:x}", number.unsigned_abs()))
        }
        (Some(Number::Int(number)), Some('X')) => {
            (number < 0, format!("{:X}", number.unsigned_abs()))
        }
        (Some(Number::Int(number)), Some('o')) => {
            (number < 0, format!("{:o}", number.unsigned_abs()))
        }
        (Some(Number::Int(number)), Some('b')) => {
            (number < 0, format!("{:b}", number.unsigned_abs()))
        }
        (Some(number), Some('f' | 'F')) => {
            let number = number.as_f64();
            (
                number < 0.0,
                format!("{:.*}", precision.unwrap_or(6), number.abs()),
            )
        }
        (Some(number), Some('e' | 'E')) => {
            let number = number.as_f64();
            let shown = format!("{:.*e}", precision.unwrap_or(6), number.abs());
            let (mantissa, exponent) = shown.split_once('e').unwrap_or((&shown, "0"));
            let exponent: i32 = exponent.parse().unwrap_or(0);
            (
                number < 0.0,
                format!(
                    "{mantissa}e{}{:02}",
                    if exponent < 0 { '-' } else { '+' },
                    exponent.abs()
                ),
            )
        }
        (Some(number), Some('%')) => {
            let number = number.as_f64() * 100.0;
            (
                number < 0.0,
                format!("{:.*}%", precision.unwrap_or(6), number.abs()),
            )
        }
        (Some(Number::Float(number)), None | Some('g')) => (number < 0.0, float_repr(number.abs())),
        (None, None | Some('s')) => {
            let mut text = value.text();
            if let Some(limit) = precision {
                text = text.chars().take(limit).collect();
            }
            return Ok(pad(&text, width, fill, align.or(Some('<'))));
        }
        _ => {
            return Err(type_error(format!(
                "unknown format code for object of type '{}'",
                value.type_name()
            )));
        }
    };
    let body = if grouping {
        let (whole, fraction) = body
            .split_once('.')
            .map_or((body.as_str(), None), |(w, f)| (w, Some(f)));
        let mut grouped = String::new();
        for (n, c) in whole.chars().enumerate() {
            if n > 0 && (whole.len() - n) % 3 == 0 {
                grouped.push(',');
            }
            grouped.push(c);
        }
        match fraction {
            Some(fraction) => format!("{grouped}.{fraction}"),
            None => grouped,
        }
    } else {
        body
    };
    let sign_text = if negative {
        "-"
    } else if sign == '+' {
        "+"
    } else if sign == ' ' {
        " "
    } else {
        ""
    };
    if align == Some('=') {
        let length = sign_text.len() + body.chars().count();
        let padding = width.map_or(0, |w| w.saturating_sub(length));
        return Ok(format!(
            "{sign_text}{}{body}",
            fill.to_string().repeat(padding)
        ));
    }
    Ok(pad(
        &format!("{sign_text}{body}"),
        width,
        fill,
        align.or(Some('>')),
    ))
}

fn pad(text: &str, width: Option<usize>, fill: char, align: Option<char>) -> String {
    let length = text.chars().count();
    let Some(width) = width.filter(|w| *w > length) else {
        return text.to_string();
    };
    let gap = width - length;
    let (left, right) = match align {
        Some('>') => (gap, 0),
        Some('^') => (gap / 2, gap - gap / 2),
        _ => (0, gap),
    };
    format!(
        "{}{text}{}",
        fill.to_string().repeat(left),
        fill.to_string().repeat(right)
    )
}

/// printf-style `%`: `%s`, `%r`, `%d`, `%f`, `%x`, flags, width and precision, `%(name)s`.
fn percent_format(format: &str, args: &Value) -> Result<String, Error> {
    let positional: Vec<Value> = match args {
        Value::Tuple(items) => items.as_ref().clone(),
        other => vec![other.clone()],
    };
    let chars: Vec<char> = format.chars().collect();
    let mut out = String::new();
    let (mut at, mut used) = (0, 0);
    while at < chars.len() {
        if chars[at] != '%' {
            out.push(chars[at]);
            at += 1;
            continue;
        }
        at += 1;
        let mut key = None;
        if chars.get(at) == Some(&'(') {
            let end = chars[at..]
                .iter()
                .position(|c| *c == ')')
                .ok_or_else(|| type_error("incomplete format key"))?;
            key = Some(chars[at + 1..at + end].iter().collect::<String>());
            at += end + 1;
        }
        let mut spec = String::new();
        while at < chars.len() && "-+ #0123456789.".contains(chars[at]) {
            spec.push(chars[at]);
            at += 1;
        }
        let kind = *chars
            .get(at)
            .ok_or_else(|| type_error("incomplete format"))?;
        at += 1;
        if kind == '%' {
            out.push('%');
            continue;
        }
        let value = match &key {
            Some(key) => match args {
                Value::Dict(entries) => entries
                    .borrow()
                    .get(&Value::from(key.as_str()))
                    .cloned()
                    .ok_or_else(|| type_error(format!("'{key}'")))?,
                _ => return Err(type_error("format requires a mapping")),
            },
            None => {
                used += 1;
                positional
                    .get(used - 1)
                    .cloned()
                    .ok_or_else(|| type_error("not enough arguments for format string"))?
            }
        };
        let left = spec.contains('-');
        let zero = spec.starts_with('0') || spec.contains("+0") || spec.contains("-0");
        let spec_text = spec.trim_start_matches(['-', '+', ' ', '#', '0']);
        let (width, precision) = match spec_text.split_once('.') {
            Some((w, p)) => (w.parse::<usize>().ok(), p.parse::<usize>().ok().or(Some(0))),
            None => (spec_text.parse::<usize>().ok(), None),
        };
        let piece = match kind {
            's' => {
                let text = value.text();
                precision.map_or(text.clone(), |p| text.chars().take(p).collect())
            }
            'r' | 'a' => value.repr(),
            'd' | 'i' | 'u' => {
                let number = match value.as_number() {
                    Some(Number::Float(float)) => float.trunc() as i128,
                    _ => as_int(&value)?,
                };
                let sign = if spec.contains('+') && number >= 0 {
                    "+"
                } else {
                    ""
                };
                format!("{sign}{number}")
            }
            'f' | 'F' => {
                let number = value
                    .as_number()
                    .ok_or_else(|| type_error("a number is required"))?
                    .as_f64();
                let sign = if spec.contains('+') && number >= 0.0 {
                    "+"
                } else {
                    ""
                };
                format!("{sign}{:.*}", precision.unwrap_or(6), number)
            }
            'x' => format!("{:x}", as_int(&value)?),
            'X' => format!("{:X}", as_int(&value)?),
            'o' => format!("{:o}", as_int(&value)?),
            other => {
                return Err(type_error(format!(
                    "unsupported format character '{other}'"
                )));
            }
        };
        let length = piece.chars().count();
        match width.filter(|w| *w > length) {
            None => out.push_str(&piece),
            Some(width) if left => out.push_str(&format!("{piece}{}", " ".repeat(width - length))),
            Some(width) if zero && kind != 's' => {
                let (sign, digits) = match piece.chars().next() {
                    Some(c @ ('-' | '+')) => (c.to_string(), &piece[1..]),
                    _ => (String::new(), &piece[..]),
                };
                out.push_str(&format!("{sign}{}{digits}", "0".repeat(width - length)));
            }
            Some(width) => out.push_str(&format!("{}{piece}", " ".repeat(width - length))),
        }
    }
    Ok(out)
}

// Filters

fn sort_key(
    value: &Value,
    case_sensitive: bool,
    attribute: Option<&Value>,
) -> Result<Value, Error> {
    let key = match attribute {
        Some(path) => lookup_path(value, &path.text())?,
        None => value.clone(),
    };
    Ok(match key {
        Value::Str(text) if !case_sensitive => Value::from(text.to_lowercase()),
        other => other,
    })
}

fn lookup_path(value: &Value, path: &str) -> Result<Value, Error> {
    let mut current = value.clone();
    for part in path.split('.') {
        current = match part.parse::<i64>() {
            Ok(at) => get_item(&current, &Value::from(at))?,
            Err(_) => match &current {
                Value::Dict(entries) => entries
                    .borrow()
                    .get(&Value::from(part))
                    .cloned()
                    .unwrap_or(Value::Undefined),
                other => get_attr(other, part),
            },
        };
    }
    Ok(current)
}

fn sorted(items: Vec<Value>, keys: Vec<Value>, reverse: bool) -> Result<Vec<Value>, Error> {
    let mut order: Vec<usize> = (0..items.len()).collect();
    let mut failure = None;
    order.sort_by(|a, b| {
        keys[*a].compare(&keys[*b]).unwrap_or_else(|error| {
            failure = Some(error);
            Ordering::Equal
        })
    });
    if let Some(error) = failure {
        return Err(error);
    }
    if reverse {
        order.reverse();
        // Python's reverse sort is stable: equal keys keep their order.
        let mut at = 0;
        while at < order.len() {
            let mut end = at + 1;
            while end < order.len() && keys[order[at]].equals(&keys[order[end]]) {
                end += 1;
            }
            order[at..end].sort_unstable();
            at = end;
        }
    }
    Ok(order.into_iter().map(|at| items[at].clone()).collect())
}

fn round_to(number: f64, precision: i128) -> f64 {
    if precision >= 0 {
        let shown = format!("{:.*}", precision as usize, number);
        shown.parse().unwrap_or(number)
    } else {
        let scale = 10f64.powi((-precision) as i32);
        let scaled = number / scale;
        let rounded = format!("{scaled:.0}").parse::<f64>().unwrap_or(scaled);
        rounded * scale
    }
}

fn html_escape(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for c in text.chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '\'' => out.push_str("&#39;"),
            '"' => out.push_str("&#34;"),
            c => out.push(c),
        }
    }
    out
}

fn json_text(value: &Value, out: &mut String) -> Result<(), Error> {
    match value {
        Value::None | Value::Undefined => out.push_str("null"),
        Value::Bool(true) => out.push_str("true"),
        Value::Bool(false) => out.push_str("false"),
        Value::Int(number) => out.push_str(&number.to_string()),
        Value::Float(number) => out.push_str(&float_repr(*number)),
        Value::Str(text) => json_string(text, out),
        Value::List(_) | Value::Tuple(_) | Value::Range(..) => {
            out.push('[');
            for (n, item) in items_of(value)?.iter().enumerate() {
                if n > 0 {
                    out.push_str(", ");
                }
                json_text(item, out)?;
            }
            out.push(']');
        }
        Value::Dict(entries) => {
            let mut pairs: Vec<(String, Value)> = entries
                .borrow()
                .iter()
                .map(|(k, v)| (k.text(), v.clone()))
                .collect();
            pairs.sort_by(|a, b| a.0.cmp(&b.0));
            out.push('{');
            for (n, (key, item)) in pairs.iter().enumerate() {
                if n > 0 {
                    out.push_str(", ");
                }
                json_string(key, out);
                out.push_str(": ");
                json_text(item, out)?;
            }
            out.push('}');
        }
        other => {
            return Err(type_error(format!(
                "Object of type {} is not JSON serializable",
                other.type_name()
            )));
        }
    }
    Ok(())
}

fn json_string(text: &str, out: &mut String) {
    out.push('"');
    for c in text.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '<' | '>' | '&' | '\'' => out.push_str(&format!("\\u{:04x}", c as u32)),
            c if (c as u32) < 0x20 || (c as u32) > 0x7e => {
                let mut units = [0u16; 2];
                for unit in c.encode_utf16(&mut units) {
                    out.push_str(&format!("\\u{unit:04x}"));
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

fn url_quote(text: &str, for_query: bool, out: &mut String) {
    for byte in text.bytes() {
        if byte.is_ascii_alphanumeric()
            || matches!(byte, b'_' | b'.' | b'-' | b'~')
            || (byte == b'/' && !for_query)
        {
            out.push(byte as char);
        } else if byte == b' ' && for_query {
            out.push('+');
        } else {
            out.push_str(&format!("%{byte:02X}"));
        }
    }
}

pub fn filter(
    interp: &mut Interp,
    name: &str,
    value: &Value,
    pos: &[Value],
    kw: &Kwargs,
) -> Result<Value, Error> {
    let given = Args::new(pos, kw);
    if matches!(value, Value::Undefined)
        && matches!(name, "int" | "float" | "abs" | "round" | "tojson")
    {
        return Err(type_error("'undefined' is undefined"));
    }
    Ok(match name {
        "abs" => match value.as_number() {
            Some(Number::Int(number)) => Value::Int(number.abs()),
            Some(Number::Float(number)) => Value::Float(number.abs()),
            None => return Err(type_error("bad operand type for abs()")),
        },
        "attr" => {
            // Python's `getattr`: a dict's keys are not attributes.
            let attribute = given.need(0, "name")?.text();
            match value {
                Value::Dict(_) if !has_method(value, &attribute) => Value::Undefined,
                other => get_attr(other, &attribute),
            }
        }
        "batch" => {
            let size = as_index(given.need(0, "linecount")?)?.max(1);
            let fill = given.get(1, "fill_with");
            let mut rows: Vec<Vec<Value>> = items_of(value)?
                .chunks(size)
                .map(<[Value]>::to_vec)
                .collect();
            if let (Some(fill), Some(last)) = (fill, rows.last_mut()) {
                while last.len() < size {
                    last.push(fill.clone());
                }
            }
            Value::list(rows.into_iter().map(Value::list).collect())
        }
        "capitalize" => {
            let text = value.text();
            let mut chars = text.chars();
            let head: String = chars
                .next()
                .map(|c| c.to_uppercase().collect())
                .unwrap_or_default();
            Value::from(head + &chars.as_str().to_lowercase())
        }
        "center" => {
            let text = value.text();
            let width = given.int(0, "width", 80)?.max(0) as usize;
            let length = text.chars().count();
            if width <= length {
                Value::from(text)
            } else {
                let pad = width - length;
                let left = pad / 2 + (pad & width & 1);
                Value::from(format!(
                    "{}{text}{}",
                    " ".repeat(left),
                    " ".repeat(pad - left)
                ))
            }
        }
        "count" | "length" => Value::Int(value.length().ok_or_else(|| {
            type_error(format!(
                "object of type '{}' has no len()",
                value.type_name()
            ))
        })? as i128),
        "d" | "default" => {
            let default = given
                .get(0, "default_value")
                .cloned()
                .unwrap_or_else(|| Value::from(""));
            let boolean = given.flag(1, "boolean", false);
            if matches!(value, Value::Undefined) || (boolean && !value.truthy()) {
                default
            } else {
                value.clone()
            }
        }
        "dictsort" => {
            let case_sensitive = given.flag(0, "case_sensitive", false);
            let by_value = given.text(1, "by", "key") == "value";
            let reverse = given.flag(2, "reverse", false);
            let Value::Dict(entries) = value else {
                return Err(type_error("dictsort needs a dict"));
            };
            let pairs: Vec<Value> = entries
                .borrow()
                .iter()
                .map(|(k, v)| Value::tuple(vec![k.clone(), v.clone()]))
                .collect();
            let keys = pairs
                .iter()
                .map(|pair| {
                    let part = pair.items().unwrap_or_default()[usize::from(by_value)].clone();
                    sort_key(&part, case_sensitive, None)
                })
                .collect::<Result<Vec<_>, _>>()?;
            Value::list(sorted(pairs, keys, reverse)?)
        }
        "e" | "escape" | "forceescape" => Value::from(html_escape(&value.text())),
        "first" => match value {
            Value::Str(text) => text
                .chars()
                .next()
                .map_or(Value::Undefined, |c| Value::from(c.to_string())),
            other => items_of(other)?
                .into_iter()
                .next()
                .unwrap_or(Value::Undefined),
        },
        "last" => match value {
            Value::Str(text) => text
                .chars()
                .last()
                .map_or(Value::Undefined, |c| Value::from(c.to_string())),
            other => items_of(other)?.pop().unwrap_or(Value::Undefined),
        },
        "float" => {
            let default = given
                .get(0, "default")
                .cloned()
                .unwrap_or(Value::Float(0.0));
            match value {
                Value::Str(text) => text.trim().parse::<f64>().map_or(default, Value::Float),
                other => other
                    .as_number()
                    .map_or(default, |n| Value::Float(n.as_f64())),
            }
        }
        "int" => {
            let default = given.get(0, "default").cloned().unwrap_or(Value::Int(0));
            match value {
                Value::Str(text) => {
                    let trimmed = text.trim();
                    trimmed
                        .replace('_', "")
                        .parse::<i128>()
                        .ok()
                        .or_else(|| {
                            trimmed
                                .parse::<f64>()
                                .ok()
                                .filter(|f| f.is_finite())
                                .map(|f| f.trunc() as i128)
                        })
                        .map_or(default, Value::Int)
                }
                other => match other.as_number() {
                    Some(Number::Float(float)) if float.is_finite() => {
                        Value::Int(float.trunc() as i128)
                    }
                    Some(Number::Int(number)) => Value::Int(number),
                    _ => default,
                },
            }
        }
        "format" => {
            let source = value.text();
            let arguments = if kw.is_empty() {
                Value::tuple(pos.to_vec())
            } else {
                Value::dict(
                    kw.iter()
                        .map(|(k, v)| (Value::from(k.as_str()), v.clone()))
                        .collect(),
                )
            };
            Value::from(percent_format(&source, &arguments)?)
        }
        "groupby" => {
            let attribute = given.need(0, "attribute")?.clone();
            let default = given.get(1, "default");
            let case_sensitive = given.flag(2, "case_sensitive", false);
            let items = items_of(value)?;
            let key_of = |item: &Value| -> Result<Value, Error> {
                let found = lookup_path(item, &attribute.text())?;
                Ok(match (found, default) {
                    (Value::Undefined, Some(default)) => default.clone(),
                    (found, _) => found,
                })
            };
            let keys = items
                .iter()
                .map(|item| sort_key(&key_of(item)?, case_sensitive, None))
                .collect::<Result<Vec<_>, _>>()?;
            let order = sorted(items.clone(), keys.clone(), false)?;
            let mut groups: Vec<(Value, Vec<Value>)> = Vec::new();
            for item in order {
                let key = key_of(&item)?;
                let comparable = sort_key(&key, case_sensitive, None)?;
                match groups.last_mut() {
                    Some((last, members))
                        if sort_key(last, case_sensitive, None)?.equals(&comparable) =>
                    {
                        members.push(item);
                    }
                    _ => groups.push((key, vec![item])),
                }
            }
            Value::list(
                groups
                    .into_iter()
                    .map(|(key, members)| Value::tuple(vec![key, Value::list(members)]))
                    .collect(),
            )
        }
        "indent" => {
            let text = value.text();
            let indentation = match given.get(0, "width") {
                Some(Value::Str(custom)) => custom.to_string(),
                other => " ".repeat(other.map_or(Ok(4), as_int)?.max(0) as usize),
            };
            let first = given.flag(1, "first", false);
            let blank = given.flag(2, "blank", false);
            let lines: Vec<&str> = text.lines().collect();
            let mut result = if blank {
                lines.join(&format!("\n{indentation}"))
            } else {
                let mut parts = lines.iter();
                let mut head = parts.next().map(|l| (*l).to_string()).unwrap_or_default();
                for line in parts {
                    head.push('\n');
                    if !line.is_empty() {
                        head.push_str(&indentation);
                    }
                    head.push_str(line);
                }
                head
            };
            if first {
                result = indentation + &result;
            }
            Value::from(result)
        }
        "items" => match value {
            Value::Dict(entries) => Value::list(
                entries
                    .borrow()
                    .iter()
                    .map(|(k, v)| Value::tuple(vec![k.clone(), v.clone()]))
                    .collect(),
            ),
            Value::Undefined => Value::list(Vec::new()),
            _ => return Err(type_error("items needs a dict")),
        },
        "join" => {
            let separator = given.text(0, "d", "");
            let attribute = given.get(1, "attribute");
            let mut parts = Vec::new();
            for item in items_of(value)? {
                parts.push(match attribute {
                    Some(path) => lookup_path(&item, &path.text())?.text(),
                    None => item.text(),
                });
            }
            Value::from(parts.join(&separator))
        }
        "list" => Value::list(items_of(value)?),
        "lower" => Value::from(value.text().to_lowercase()),
        "upper" => Value::from(value.text().to_uppercase()),
        "map" => {
            let items = items_of(value)?;
            let mut mapped = Vec::with_capacity(items.len());
            if let Some(attribute) = kw.iter().find(|(k, _)| k == "attribute").map(|(_, v)| v) {
                let default = kw
                    .iter()
                    .find(|(k, _)| k == "default")
                    .map(|(_, v)| v.clone());
                for item in &items {
                    let found = lookup_path(item, &attribute.text())?;
                    mapped.push(match (found, &default) {
                        (Value::Undefined, Some(default)) => default.clone(),
                        (found, _) => found,
                    });
                }
            } else {
                let filter_name = given.need(0, "filter")?.text();
                let rest = pos.get(1..).unwrap_or_default();
                for item in &items {
                    mapped.push(filter(interp, &filter_name, item, rest, kw)?);
                }
            }
            Value::list(mapped)
        }
        "min" | "max" => {
            let case_sensitive = given.flag(0, "case_sensitive", false);
            let attribute = given.get(1, "attribute");
            let items = items_of(value)?;
            let mut best: Option<(Value, Value)> = None;
            for item in items {
                let key = sort_key(&item, case_sensitive, attribute)?;
                let better = match &best {
                    None => true,
                    Some((_, current)) => {
                        let order = key.compare(current)?;
                        if name == "min" {
                            order == Ordering::Less
                        } else {
                            order == Ordering::Greater
                        }
                    }
                };
                if better {
                    best = Some((item, key));
                }
            }
            best.map_or(Value::Undefined, |(item, _)| item)
        }
        "pprint" => Value::from(value.repr()),
        "reject" | "select" | "rejectattr" | "selectattr" => {
            let by_attribute = name.ends_with("attr");
            let wanted = name.starts_with("select");
            let (attribute, rest) = if by_attribute {
                (Some(given.need(0, "attr")?.text()), 1)
            } else {
                (None, 0)
            };
            let test_name = match given.get(rest, "test") {
                Some(test) if !matches!(test, Value::None) => Some(test.text()),
                _ => None,
            };
            let extra = pos.get(rest + 1..).unwrap_or_default();
            let mut kept = Vec::new();
            for item in items_of(value)? {
                let probe = match &attribute {
                    Some(path) => lookup_path(&item, path)?,
                    None => item.clone(),
                };
                let passes = match &test_name {
                    Some(test_name) => test(interp, test_name, &probe, extra, kw)?,
                    None => probe.truthy(),
                };
                if passes == wanted {
                    kept.push(item);
                }
            }
            Value::list(kept)
        }
        "replace" => {
            let (old, new) = (given.need(0, "old")?.text(), given.need(1, "new")?.text());
            let text = value.text();
            Value::from(match given.get(2, "count") {
                Some(Value::None) | None => text.replace(&old, &new),
                Some(count) => text.replacen(&old, &new, as_index(count)?),
            })
        }
        "reverse" => match value {
            Value::Str(text) => Value::from(text.chars().rev().collect::<String>()),
            other => {
                let mut items = items_of(other)?;
                items.reverse();
                Value::list(items)
            }
        },
        "round" => {
            let precision = given.int(0, "precision", 0)?;
            let method = given.text(1, "method", "common");
            match value.as_number() {
                Some(Number::Int(number)) if precision >= 0 => Value::Int(number),
                Some(Number::Int(number)) => {
                    let scale = 10i128
                        .checked_pow((-precision) as u32)
                        .ok_or_else(overflow)?;
                    let (quotient, remainder) =
                        (number.div_euclid(scale), number.rem_euclid(scale));
                    let doubled = remainder * 2;
                    let up = doubled > scale || (doubled == scale && quotient % 2 != 0);
                    Value::Int((quotient + i128::from(up)) * scale)
                }
                Some(number) => {
                    let number = number.as_f64();
                    Value::Float(match method.as_str() {
                        "ceil" => {
                            (number * 10f64.powi(precision as i32)).ceil()
                                / 10f64.powi(precision as i32)
                        }
                        "floor" => {
                            (number * 10f64.powi(precision as i32)).floor()
                                / 10f64.powi(precision as i32)
                        }
                        _ => round_to(number, precision),
                    })
                }
                None => return Err(type_error("a number is required")),
            }
        }
        "safe" => Value::from(value.text()),
        "slice" => {
            let slices = as_index(given.need(0, "slices")?)?.max(1);
            let items = items_of(value)?;
            let fill = given.get(1, "fill_with");
            let (each, extra) = (items.len() / slices, items.len() % slices);
            let mut offset = 0;
            let mut groups = Vec::new();
            for n in 0..slices {
                let size = each + usize::from(n < extra);
                let mut group = items[offset..offset + size].to_vec();
                offset += size;
                if let (Some(fill), true) = (fill, n >= extra && extra > 0) {
                    group.push(fill.clone());
                }
                groups.push(Value::list(group));
            }
            Value::list(groups)
        }
        "sort" => {
            let reverse = given.flag(0, "reverse", false);
            let case_sensitive = given.flag(1, "case_sensitive", false);
            let attribute = given.get(2, "attribute");
            let items = items_of(value)?;
            let keys = items
                .iter()
                .map(|i| sort_key(i, case_sensitive, attribute))
                .collect::<Result<Vec<_>, _>>()?;
            Value::list(sorted(items, keys, reverse)?)
        }
        "string" => Value::from(value.text()),
        "striptags" => {
            let text = value.text();
            let mut plain = String::new();
            let mut inside = false;
            for c in text.chars() {
                match c {
                    '<' => inside = true,
                    '>' if inside => inside = false,
                    c if !inside => plain.push(c),
                    _ => {}
                }
            }
            Value::from(plain.split_whitespace().collect::<Vec<_>>().join(" "))
        }
        "sum" => {
            let attribute = given.get(0, "attribute");
            let mut total = given.get(1, "start").cloned().unwrap_or(Value::Int(0));
            for item in items_of(value)? {
                let item = match attribute {
                    Some(path) => lookup_path(&item, &path.text())?,
                    None => item,
                };
                total = binary(Bin::Add, &total, &item)?;
            }
            total
        }
        "title" => {
            let text = value.text();
            let mut out = String::new();
            let mut word = String::new();
            let flush = |word: &mut String, out: &mut String| {
                let mut chars = word.chars();
                if let Some(head) = chars.next() {
                    out.extend(head.to_uppercase());
                    out.push_str(&chars.as_str().to_lowercase());
                }
                word.clear();
            };
            let mut separator = String::new();
            for c in text.chars() {
                let splits = c == '-' || c.is_whitespace() || matches!(c, '(' | '{' | '[' | '<');
                if splits {
                    if !word.is_empty() {
                        flush(&mut word, &mut out);
                    }
                    separator.push(c);
                } else {
                    if !separator.is_empty() {
                        let mut chars = separator.chars();
                        if let Some(head) = chars.next() {
                            out.extend(head.to_uppercase());
                            out.push_str(&chars.as_str().to_lowercase());
                        }
                        separator.clear();
                    }
                    word.push(c);
                }
            }
            if !word.is_empty() {
                flush(&mut word, &mut out);
            }
            out.push_str(&separator);
            Value::from(out)
        }
        "tojson" => {
            let mut out = String::new();
            json_text(value, &mut out)?;
            Value::from(out)
        }
        "trim" => {
            let text = value.text();
            Value::from(match given.get(0, "chars") {
                None | Some(Value::None) => text.trim().to_string(),
                Some(chars) => {
                    let set: Vec<char> = chars.text().chars().collect();
                    text.trim_matches(|c| set.contains(&c)).to_string()
                }
            })
        }
        "truncate" => {
            let text = value.text();
            let length = given.int(0, "length", 255)?.max(0) as usize;
            let killwords = given.flag(1, "killwords", false);
            let end = given.text(2, "end", "...");
            let leeway = given.int(3, "leeway", 5)?.max(0) as usize;
            let count = text.chars().count();
            if count <= length + leeway {
                Value::from(text)
            } else {
                let keep = length.saturating_sub(end.chars().count());
                let head: String = text.chars().take(keep).collect();
                if killwords {
                    Value::from(head + &end)
                } else {
                    let cut = head
                        .rsplit_once(' ')
                        .map_or(head.as_str(), |(before, _)| before);
                    Value::from(format!("{cut}{end}"))
                }
            }
        }
        "unique" => {
            let case_sensitive = given.flag(0, "case_sensitive", false);
            let attribute = given.get(1, "attribute");
            let mut seen: Vec<Value> = Vec::new();
            let mut kept = Vec::new();
            for item in items_of(value)? {
                let key = sort_key(&item, case_sensitive, attribute)?;
                if !seen.iter().any(|known| known.equals(&key)) {
                    seen.push(key);
                    kept.push(item);
                }
            }
            Value::list(kept)
        }
        "urlencode" => {
            let mut out = String::new();
            match value {
                Value::Dict(entries) => {
                    for (n, (k, v)) in entries.borrow().iter().enumerate() {
                        if n > 0 {
                            out.push('&');
                        }
                        url_quote(&k.text(), true, &mut out);
                        out.push('=');
                        url_quote(&v.text(), true, &mut out);
                    }
                }
                other => url_quote(&other.text(), false, &mut out),
            }
            Value::from(out)
        }
        "wordcount" => {
            let text = value.text();
            let mut words = 0;
            let mut inside = false;
            for c in text.chars() {
                let word = c.is_alphanumeric() || c == '_';
                if word && !inside {
                    words += 1;
                }
                inside = word;
            }
            Value::Int(words)
        }
        other => return Err(type_error(format!("no filter named '{other}'"))),
    })
}

pub fn test(
    interp: &mut Interp,
    name: &str,
    value: &Value,
    pos: &[Value],
    kw: &Kwargs,
) -> Result<bool, Error> {
    let given = Args::new(pos, kw);
    let other = || given.need(0, "other");
    Ok(match name {
        "callable" => {
            matches!(value, Value::Callable(_) | Value::Bound(_))
                || matches!(value, Value::Object(o) if o.type_name() == "Macro")
        }
        "defined" => !matches!(value, Value::Undefined),
        "undefined" => matches!(value, Value::Undefined),
        "divisibleby" => {
            let by = as_int(other()?)?;
            by != 0 && as_int(value)? % by == 0
        }
        "eq" | "==" | "equalto" | "sameas" => value.equals(other()?),
        "ne" | "!=" => !value.equals(other()?),
        "ge" | ">=" => value.compare(other()?)? != Ordering::Less,
        "gt" | ">" | "greaterthan" => value.compare(other()?)? == Ordering::Greater,
        "le" | "<=" => value.compare(other()?)? != Ordering::Greater,
        "lt" | "<" | "lessthan" => value.compare(other()?)? == Ordering::Less,
        "escaped" => false,
        "even" => as_int(value)? % 2 == 0,
        "odd" => as_int(value)? % 2 != 0,
        "false" => matches!(value, Value::Bool(false)),
        "true" => matches!(value, Value::Bool(true)),
        "float" => matches!(value, Value::Float(_)),
        "integer" => matches!(value, Value::Int(_)),
        "number" => matches!(value, Value::Int(_) | Value::Float(_) | Value::Bool(_)),
        "in" => contains(other()?, value)?,
        "iterable" => value.items().is_some(),
        "lower" => {
            matches!(value, Value::Str(text) if text.chars().any(char::is_alphabetic) && text.to_lowercase() == **text)
        }
        "upper" => {
            matches!(value, Value::Str(text) if text.chars().any(char::is_alphabetic) && text.to_uppercase() == **text)
        }
        "mapping" => matches!(value, Value::Dict(_)),
        "none" => matches!(value, Value::None),
        "sequence" => matches!(
            value,
            Value::List(_) | Value::Tuple(_) | Value::Str(_) | Value::Range(..)
        ),
        "string" => value.is_str(),
        "filter" => FILTERS.contains(&value.text().as_str()),
        "test" => TESTS.contains(&value.text().as_str()),
        other => {
            return Err(type_error(format!("no test named '{other}'"))).inspect(|_| {
                let _ = interp;
            });
        }
    })
}

// Globals

struct Namespace(std::cell::RefCell<Dict>);

impl Object for Namespace {
    fn type_name(&self) -> &'static str {
        "Namespace"
    }

    fn attr(&self, name: &str) -> Option<Value> {
        self.0.borrow().get(&Value::from(name)).cloned()
    }

    fn set_attr(&self, name: &str, value: Value) -> Result<(), Error> {
        self.0.borrow_mut().insert(Value::from(name), value)
    }

    fn text(&self) -> String {
        let entries = self.0.borrow();
        let inner: Vec<String> = entries
            .iter()
            .map(|(k, v)| format!("{}={}", k.text(), v.repr()))
            .collect();
        format!("<Namespace {}>", inner.join(", "))
    }
}

struct Cycler {
    items: Vec<Value>,
    at: std::cell::Cell<usize>,
}

impl Object for Cycler {
    fn type_name(&self) -> &'static str {
        "Cycler"
    }

    fn attr(&self, name: &str) -> Option<Value> {
        match name {
            "current" => self.items.get(self.at.get()).cloned(),
            _ => None,
        }
    }

    fn method(
        &self,
        _: &mut dyn Callable,
        name: &str,
        _: &[Value],
        _: &Kwargs,
    ) -> Option<Result<Value, Error>> {
        match name {
            "next" => {
                let item = self.items.get(self.at.get())?.clone();
                self.at.set((self.at.get() + 1) % self.items.len());
                Some(Ok(item))
            }
            "reset" => {
                self.at.set(0);
                Some(Ok(Value::None))
            }
            _ => None,
        }
    }
}

struct Joiner {
    separator: Value,
    used: std::cell::Cell<bool>,
}

impl Object for Joiner {
    fn type_name(&self) -> &'static str {
        "Joiner"
    }

    fn attr(&self, _: &str) -> Option<Value> {
        None
    }

    fn call(&self, _: &mut dyn Callable, _: &[Value], _: &Kwargs) -> Option<Result<Value, Error>> {
        Some(Ok(if self.used.replace(true) {
            self.separator.clone()
        } else {
            Value::from("")
        }))
    }
}

/// Jinja2's own globals: `range`, `dict`, `namespace`, `cycler`, `joiner` and `lipsum`.
pub fn globals() -> HashMap<String, Value> {
    let mut names = HashMap::new();
    let mut add =
        |name: &str, call: fn(&mut dyn Callable, &[Value], &Kwargs) -> Result<Value, Error>| {
            names.insert(name.to_string(), Function::native(name, Rc::new(call)));
        };
    add("range", |_, args, _| {
        let numbers = args.iter().map(as_int).collect::<Result<Vec<_>, _>>()?;
        let (start, stop, step) = match numbers.as_slice() {
            [stop] => (0, *stop, 1),
            [start, stop] => (*start, *stop, 1),
            [start, stop, step] => (*start, *stop, *step),
            _ => return Err(type_error("range expected 1 to 3 arguments")),
        };
        if step == 0 {
            return Err(type_error("range() arg 3 must not be zero"));
        }
        if range_len(start, stop, step) > MAX_RANGE {
            return Err(type_error("the range is too large"));
        }
        Ok(Value::Range(start, stop, step))
    });
    add("dict", |_, args, kwargs| {
        let result = Value::dict(Vec::new());
        if let Value::Dict(entries) = &result {
            if let Some(source) = args.first() {
                match source {
                    Value::Dict(other) => {
                        for (key, value) in other.borrow().iter() {
                            entries.borrow_mut().insert(key.clone(), value.clone())?;
                        }
                    }
                    pairs => {
                        for pair in items_of(pairs)? {
                            let pair = items_of(&pair)?;
                            if pair.len() != 2 {
                                return Err(type_error(
                                    "dictionary update sequence element is not a pair",
                                ));
                            }
                            entries
                                .borrow_mut()
                                .insert(pair[0].clone(), pair[1].clone())?;
                        }
                    }
                }
            }
            for (key, value) in kwargs {
                entries
                    .borrow_mut()
                    .insert(Value::from(key.as_str()), value.clone())?;
            }
        }
        Ok(result)
    });
    add("namespace", |_, args, kwargs| {
        let mut dict = Dict::default();
        if let Some(Value::Dict(source)) = args.first() {
            for (key, value) in source.borrow().iter() {
                dict.insert(key.clone(), value.clone())?;
            }
        }
        for (key, value) in kwargs {
            dict.insert(Value::from(key.as_str()), value.clone())?;
        }
        Ok(Value::Object(Rc::new(Namespace(std::cell::RefCell::new(
            dict,
        )))))
    });
    add("cycler", |_, args, _| {
        if args.is_empty() {
            return Err(type_error("cycler needs items"));
        }
        Ok(Value::Object(Rc::new(Cycler {
            items: args.to_vec(),
            at: std::cell::Cell::new(0),
        })))
    });
    add("joiner", |_, args, _| {
        let separator = args.first().cloned().unwrap_or_else(|| Value::from(", "));
        Ok(Value::Object(Rc::new(Joiner {
            separator,
            used: std::cell::Cell::new(false),
        })))
    });
    add("lipsum", |_, _, _| {
        Ok(Value::from("Lorem ipsum dolor sit amet"))
    });
    names
}
