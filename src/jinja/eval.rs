//! The interpreter: statements and expressions over [`Value`], Jinja2's scoping (a `for` or a macro
//! body has a scope of its own, a macro sees the scope it was defined in), macros and `caller`
//! blocks, and the sandbox: a name that starts with an underscore is not an attribute.

use std::cell::{Cell, RefCell};
use std::collections::HashMap;
use std::rc::{Rc, Weak};

use super::builtins;
use super::parse::{Arg, Bin, Cmp, Esc, Expr, ForDef, MacroDef, Stmt, Target};
use super::stack::deeper;
use super::value::{Object, Value, range_len};

#[derive(Debug, Clone)]
pub struct Error(pub String);

impl Error {
    pub fn new(message: impl Into<String>) -> Self {
        Error(message.into())
    }
}

impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for Error {}

pub type Kwargs = [(String, Value)];

/// Positional and named arguments, evaluated.
type Arguments = (Vec<Value>, Vec<(String, Value)>);

pub type NativeFn = Rc<dyn Fn(&mut dyn Callable, &[Value], &Kwargs) -> Result<Value, Error>>;

/// What an object needs from the interpreter to call a value it was handed.
pub trait Callable {
    fn call_value(
        &mut self,
        function: &Value,
        args: &[Value],
        kwargs: &Kwargs,
    ) -> Result<Value, Error>;
}

enum Kind {
    Macro {
        def: Rc<MacroDef>,
        // Weak: the scope holds the macro, so a strong one would make a cycle that nothing frees.
        scope: Weak<Scope>,
        host: Weak<Host>,
        varargs: bool,
        kwargs: bool,
    },
    Native(NativeFn),
    /// `loop` of a recursive `for`: called with children, it runs the body over them.
    Loop {
        def: Rc<ForDef>,
        scope: Weak<Scope>,
        host: Weak<Host>,
        depth: usize,
    },
}

pub struct Function {
    name: Option<String>,
    kind: Kind,
}

impl Function {
    pub fn native(name: &str, call: NativeFn) -> Value {
        Value::Callable(Rc::new(Function {
            name: Some(name.to_string()),
            kind: Kind::Native(call),
        }))
    }

    /// `macro.name` and `macro.arguments`.
    pub fn attr(&self, attribute: &str) -> Option<Value> {
        match (&self.kind, attribute) {
            (_, "name") => self.name.as_deref().map(Value::from),
            (Kind::Macro { def, .. }, "arguments") => Some(Value::tuple(
                def.params
                    .iter()
                    .map(|p| Value::from(p.name.as_str()))
                    .collect(),
            )),
            _ => None,
        }
    }

    pub fn text(&self) -> String {
        match (&self.kind, &self.name) {
            (Kind::Macro { .. }, Some(name)) => format!("<Macro '{name}'>"),
            (Kind::Macro { .. }, None) => "<Macro anonymous>".to_string(),
            (Kind::Native(_), Some(name)) => format!("<function {name}>"),
            (Kind::Native(_), None) => "<function>".to_string(),
            (Kind::Loop { .. }, _) => "<function loop>".to_string(),
        }
    }
}

/// The names a render can see besides its own: dbt's context, the macros, and Jinja2's globals.
#[derive(Default)]
pub struct Host {
    pub names: HashMap<String, Value>,
}

#[derive(Default)]
pub struct Scope {
    vars: RefCell<HashMap<String, Value>>,
    parent: Option<Rc<Scope>>,
}

impl Scope {
    pub fn root() -> Rc<Scope> {
        Rc::new(Scope::default())
    }

    pub fn child(parent: &Rc<Scope>) -> Rc<Scope> {
        Rc::new(Scope {
            vars: RefCell::default(),
            parent: Some(Rc::clone(parent)),
        })
    }

    pub fn set(&self, name: &str, value: Value) {
        self.vars.borrow_mut().insert(name.to_string(), value);
    }

    fn get(&self, name: &str) -> Option<Value> {
        if let Some(value) = self.vars.borrow().get(name) {
            return Some(value.clone());
        }
        self.parent.as_ref().and_then(|parent| parent.get(name))
    }

    /// The names set directly in this scope, as a module's exports.
    pub fn locals(&self) -> Vec<(String, Value)> {
        self.vars
            .borrow()
            .iter()
            .map(|(k, v)| (k.clone(), v.clone()))
            .collect()
    }
}

/// The most evaluation steps one render takes, and the most all the renders of one renderer take:
/// a loop, a call or a builtin on a large value costs steps in proportion to its size.
const MAX_STEPS: u64 = 5_000_000;
const MAX_TOTAL_STEPS: u64 = 100_000_000;
/// The most bytes one render writes or builds in all, and the most one string or list may hold.
const MAX_BYTES: usize = 64 * 1024 * 1024;
pub(crate) const MAX_ALLOC: usize = 16 * 1024 * 1024;
/// How many macros, loops and blocks may be open inside one another.
const MAX_CALL_DEPTH: usize = 200;

/// What every render of one renderer may do in all, so that models times macros do not multiply
/// the limit of one.
pub struct Budget {
    steps: std::cell::Cell<u64>,
}

impl Default for Budget {
    fn default() -> Self {
        Budget {
            steps: std::cell::Cell::new(MAX_TOTAL_STEPS),
        }
    }
}

pub struct Interp {
    host: Rc<Host>,
    steps: u64,
    bytes: usize,
    /// How many macros and recursive loops are open.
    depth: usize,
    budget: Rc<Budget>,
    /// Whether an `{% autoescape true %}` block is running, for the filters that escape.
    pub(crate) escape: bool,
    /// The text a `{% filter %}` block filters.
    hole: Option<Value>,
}

/// Whether `name` is used anywhere in these statements: a macro takes `caller`, `varargs` and
/// `kwargs` only when its body mentions them.
fn mentions(body: &[Stmt], name: &str) -> bool {
    body.iter().any(|stmt| stmt_mentions(stmt, name))
}

fn stmt_mentions(stmt: &Stmt, name: &str) -> bool {
    let any = |exprs: &[&Expr]| exprs.iter().any(|e| expr_mentions(e, name));
    match stmt {
        Stmt::Text(_) | Stmt::NoTemplates(_) => false,
        Stmt::Output(e, _) | Stmt::Do(e) => expr_mentions(e, name),
        Stmt::If(branches, otherwise) => {
            branches
                .iter()
                .any(|(test, body)| expr_mentions(test, name) || mentions(body, name))
                || mentions(otherwise, name)
        }
        Stmt::For(def) => {
            expr_mentions(&def.iter, name)
                || def.filter.as_ref().is_some_and(|f| expr_mentions(f, name))
                || mentions(&def.body, name)
                || mentions(&def.otherwise, name)
        }
        Stmt::Set(_, e) => expr_mentions(e, name),
        Stmt::SetBlock(_, filter, body) => {
            filter.as_ref().is_some_and(|f| expr_mentions(f, name)) || mentions(body, name)
        }
        Stmt::Macro(def) => mentions(&def.body, name),
        Stmt::CallBlock(def, call) => mentions(&def.body, name) || any(&[call]),
        Stmt::FilterBlock(e, body) => expr_mentions(e, name) || mentions(body, name),
        Stmt::With(pairs, body) => {
            pairs.iter().any(|(_, e)| expr_mentions(e, name)) || mentions(body, name)
        }
        Stmt::Block(body) => mentions(body, name),
        Stmt::Autoescape(flag, body) => expr_mentions(flag, name) || mentions(body, name),
    }
}

fn arg_mentions(arg: &Arg, name: &str) -> bool {
    match arg {
        Arg::Pos(e) | Arg::Named(_, e) | Arg::Star(e) | Arg::StarStar(e) => expr_mentions(e, name),
    }
}

fn expr_mentions(expr: &Expr, name: &str) -> bool {
    let sub = |e: &Expr| expr_mentions(e, name);
    match expr {
        Expr::Const(_) => false,
        Expr::Name(found) => found == name,
        Expr::Tuple(items) | Expr::List(items) | Expr::Concat(items, _) => items.iter().any(sub),
        Expr::Dict(pairs) => pairs.iter().any(|(k, v)| sub(k) || sub(v)),
        Expr::Neg(e) | Expr::Pos(e) | Expr::Not(e) | Expr::Attr(e, _) => sub(e),
        Expr::And(a, b) | Expr::Or(a, b) | Expr::Bin(_, a, b) | Expr::Item(a, b) => {
            sub(a) || sub(b)
        }
        Expr::Compare(first, rest) => sub(first) || rest.iter().any(|(_, e)| sub(e)),
        Expr::Cond(test, then, otherwise) => {
            sub(test) || sub(then) || otherwise.as_ref().is_some_and(|e| sub(e))
        }
        Expr::Slice(a, b, c) => [a, b, c]
            .iter()
            .any(|part| part.as_ref().is_some_and(|e| sub(e))),
        Expr::Call(callee, args) => sub(callee) || args.iter().any(|a| arg_mentions(a, name)),
        Expr::Filter(subject, _, args) => {
            subject.as_ref().is_some_and(|e| sub(e)) || args.iter().any(|a| arg_mentions(a, name))
        }
        Expr::Test(subject, _, args, _) => {
            sub(subject) || args.iter().any(|a| arg_mentions(a, name))
        }
    }
}

impl Interp {
    pub fn new(host: Rc<Host>, budget: Rc<Budget>) -> Self {
        Interp {
            host,
            steps: 0,
            bytes: 0,
            depth: 0,
            budget,
            escape: false,
            hole: None,
        }
    }

    fn tick(&mut self) -> Result<(), Error> {
        self.charge(1)
    }

    /// Takes `cost` steps of this render's and the renderer's.
    pub(crate) fn charge(&mut self, cost: usize) -> Result<(), Error> {
        let cost = cost as u64;
        self.steps = self.steps.saturating_add(cost);
        let left = self.budget.steps.get();
        self.budget.steps.set(left.saturating_sub(cost));
        if self.steps > MAX_STEPS || left < cost {
            return Err(Error::new("the template does too much work"));
        }
        Ok(())
    }

    /// Takes `size` of the bytes this render may build, and says no to one string or list that is
    /// larger than any may be.
    pub(crate) fn reserve(&mut self, size: usize) -> Result<(), Error> {
        if size > MAX_ALLOC {
            return Err(Error::new("the value is too large"));
        }
        self.bytes = self.bytes.saturating_add(size);
        if self.bytes > MAX_BYTES {
            return Err(Error::new("the template builds too much"));
        }
        self.charge(size / 64)
    }

    pub fn run(&mut self, body: &[Stmt], scope: &Rc<Scope>) -> Result<String, Error> {
        let mut out = String::new();
        self.exec_all(body, scope, &mut out)?;
        Ok(out)
    }

    fn exec_all(
        &mut self,
        body: &[Stmt],
        scope: &Rc<Scope>,
        out: &mut String,
    ) -> Result<(), Error> {
        for stmt in body {
            self.exec(stmt, scope, out)?;
        }
        Ok(())
    }

    fn lookup(&self, name: &str, scope: &Rc<Scope>) -> Value {
        scope
            .get(name)
            .or_else(|| self.host.names.get(name).cloned())
            .unwrap_or(Value::Undefined)
    }

    fn exec(&mut self, stmt: &Stmt, scope: &Rc<Scope>, out: &mut String) -> Result<(), Error> {
        deeper(|| self.exec_in(stmt, scope, out))
    }

    fn exec_in(&mut self, stmt: &Stmt, scope: &Rc<Scope>, out: &mut String) -> Result<(), Error> {
        self.tick()?;
        match stmt {
            Stmt::Text(text) => {
                self.reserve(text.len())?;
                out.push_str(text);
            }
            Stmt::Output(expr, escape) => {
                let value = self.eval(expr, scope)?;
                let escaped = match escape {
                    Esc::Off => false,
                    Esc::On => true,
                    Esc::Dynamic => self.escape,
                };
                let text = if escaped && !matches!(value, Value::Safe(_)) {
                    builtins::html_escape(&value.text())
                } else {
                    value.text()
                };
                self.reserve(text.len())?;
                out.push_str(&text);
            }
            Stmt::Autoescape(flag, body) => {
                let flag = self.eval(flag, scope)?.truthy();
                let saved = std::mem::replace(&mut self.escape, flag);
                // The block is a scope of its own.
                let result = self.exec_all(body, &Scope::child(scope), out);
                self.escape = saved;
                result?;
            }
            Stmt::If(branches, otherwise) => {
                for (test, body) in branches {
                    if self.eval(test, scope)?.truthy() {
                        return self.exec_all(body, scope, out);
                    }
                }
                self.exec_all(otherwise, scope, out)?;
            }
            Stmt::For(def) => {
                let source = self.eval(&def.iter, scope)?;
                let items = source.items().ok_or_else(|| {
                    Error::new(format!("'{}' object is not iterable", source.type_name()))
                })?;
                self.run_loop(def, &source, items, scope, 0, out)?;
            }
            Stmt::Set(target, expr) => {
                let value = self.eval(expr, scope)?;
                self.assign(target, value, scope)?;
            }
            Stmt::SetBlock(target, filter, body) => {
                let text = self.run(body, scope)?;
                let text = if self.escape {
                    Value::Safe(text.into())
                } else {
                    Value::from(text)
                };
                let value = match filter {
                    Some(chain) => self.with_hole(text, |me| me.eval(chain, scope))?,
                    None => text,
                };
                self.assign(target, value, scope)?;
            }
            Stmt::Macro(def) => {
                let function = self.make_macro(def, Some(def.name.clone()), scope);
                scope.set(&def.name, function);
            }
            Stmt::CallBlock(def, call) => {
                let caller = self.make_macro(def, None, scope);
                let Expr::Call(callee, args) = call else {
                    return Err(Error::new("a call block needs a call"));
                };
                let (positional, mut named) = self.eval_args(args, scope)?;
                named.push(("caller".to_string(), caller));
                let value = self.call_expr(callee, &positional, &named, scope)?;
                out.push_str(&value.text());
            }
            Stmt::FilterBlock(chain, body) => {
                let text = Value::from(self.run(body, scope)?);
                let value = self.with_hole(text, |me| me.eval(chain, scope))?;
                out.push_str(&value.text());
            }
            Stmt::With(pairs, body) => {
                let inner = Scope::child(scope);
                for (target, expr) in pairs {
                    let value = self.eval(expr, scope)?;
                    self.assign(target, value, &inner)?;
                }
                self.exec_all(body, &inner, out)?;
            }
            Stmt::Do(expr) => {
                self.eval(expr, scope)?;
            }
            Stmt::Block(body) => self.exec_all(body, scope, out)?,
            Stmt::NoTemplates(tag) => {
                return Err(Error::new(format!(
                    "{tag}: there are no other templates to read"
                )));
            }
        }
        Ok(())
    }

    fn with_hole<T>(
        &mut self,
        hole: Value,
        run: impl FnOnce(&mut Self) -> Result<T, Error>,
    ) -> Result<T, Error> {
        let saved = self.hole.replace(hole);
        let result = run(self);
        self.hole = saved;
        result
    }

    fn make_macro(&mut self, def: &Rc<MacroDef>, name: Option<String>, scope: &Rc<Scope>) -> Value {
        let kind = Kind::Macro {
            def: Rc::clone(def),
            scope: Rc::downgrade(scope),
            host: Rc::downgrade(&self.host),
            varargs: mentions(&def.body, "varargs"),
            kwargs: mentions(&def.body, "kwargs"),
        };
        Value::Callable(Rc::new(Function { name, kind }))
    }

    /// The body of a `for` over `items`, `depth` loops inside a recursive one.
    fn run_loop(
        &mut self,
        def: &Rc<ForDef>,
        source: &Value,
        items: Vec<Value>,
        scope: &Rc<Scope>,
        depth: usize,
        out: &mut String,
    ) -> Result<(), Error> {
        let (target, filter, body, otherwise) =
            (&def.target, def.filter.as_ref(), &def.body, &def.otherwise);
        let inner = Scope::child(scope);
        let items = match filter {
            None => items,
            Some(test) => {
                let mut kept = Vec::new();
                for item in items {
                    self.assign(target, item.clone(), &inner)?;
                    if self.eval(test, &inner)?.truthy() {
                        kept.push(item);
                    }
                }
                kept
            }
        };
        if items.is_empty() {
            return self.exec_all(otherwise, scope, out);
        }
        self.charge(items.len())?;
        // A list that the body appends to is read as it grows, as Python's iterator reads it.
        let live = match (source, filter) {
            (Value::List(list), None) => Some(Rc::clone(list)),
            _ => None,
        };
        let looping = Rc::new(Loop {
            items: match &live {
                Some(list) => Items::Live(Rc::clone(list)),
                None => Items::Owned(items),
            },
            index: Cell::new(0),
            changed: RefCell::new(None),
            depth,
            recursor: def.recursive.then(|| {
                Value::Callable(Rc::new(Function {
                    name: Some("loop".to_string()),
                    kind: Kind::Loop {
                        def: Rc::clone(def),
                        scope: Rc::downgrade(scope),
                        host: Rc::downgrade(&self.host),
                        depth,
                    },
                }))
            }),
        });
        inner.set("loop", Value::Object(looping.clone()));
        let mut at = 0;
        while let Some(item) = looping.item(at) {
            self.tick()?;
            looping.index.set(at);
            self.assign(target, item, &inner)?;
            self.exec_all(body, &inner, out)?;
            at += 1;
        }
        Ok(())
    }

    fn assign(&mut self, target: &Target, value: Value, scope: &Rc<Scope>) -> Result<(), Error> {
        match target {
            Target::Name(name) => scope.set(name, value),
            Target::Tuple(targets) => {
                let items = value.items().ok_or_else(|| {
                    Error::new(format!("cannot unpack a '{}'", value.type_name()))
                })?;
                if items.len() != targets.len() {
                    return Err(Error::new(format!(
                        "cannot unpack {} values into {} names",
                        items.len(),
                        targets.len()
                    )));
                }
                for (target, item) in targets.iter().zip(items) {
                    self.assign(target, item, scope)?;
                }
            }
            Target::Attr(name, attribute) => match &self.lookup(name, scope) {
                Value::Object(object) => object.set_attr(attribute, value)?,
                _ => return Err(Error::new("only a namespace takes an attribute")),
            },
        }
        Ok(())
    }

    pub fn eval(&mut self, expr: &Expr, scope: &Rc<Scope>) -> Result<Value, Error> {
        deeper(|| self.eval_in(expr, scope))
    }

    fn eval_in(&mut self, expr: &Expr, scope: &Rc<Scope>) -> Result<Value, Error> {
        self.tick()?;
        Ok(match expr {
            Expr::Const(value) => value.clone(),
            Expr::Name(name) => self.lookup(name, scope),
            Expr::Tuple(items) => Value::tuple(self.eval_all(items, scope)?),
            Expr::List(items) => Value::list(self.eval_all(items, scope)?),
            Expr::Dict(pairs) => {
                let mut entries = Vec::with_capacity(pairs.len());
                for (key, value) in pairs {
                    entries.push((self.eval(key, scope)?, self.eval(value, scope)?));
                }
                let dict = Value::dict(Vec::new());
                if let Value::Dict(cell) = &dict {
                    for (key, value) in entries {
                        cell.borrow_mut().insert(key, value)?;
                    }
                }
                dict
            }
            Expr::Neg(inner) => builtins::negate(&self.eval(inner, scope)?)?,
            Expr::Pos(inner) => builtins::positive(&self.eval(inner, scope)?)?,
            Expr::Not(inner) => Value::Bool(!self.eval(inner, scope)?.truthy()),
            Expr::And(a, b) => {
                let left = self.eval(a, scope)?;
                if left.truthy() {
                    self.eval(b, scope)?
                } else {
                    left
                }
            }
            Expr::Or(a, b) => {
                let left = self.eval(a, scope)?;
                if left.truthy() {
                    left
                } else {
                    self.eval(b, scope)?
                }
            }
            Expr::Bin(op, a, b) => {
                let (left, right) = (self.eval(a, scope)?, self.eval(b, scope)?);
                builtins::binary(self, *op, &left, &right)?
            }
            Expr::Compare(first, rest) => {
                let mut left = self.eval(first, scope)?;
                let mut result = true;
                for (op, operand) in rest {
                    let right = self.eval(operand, scope)?;
                    if matches!(op, Cmp::In | Cmp::NotIn) {
                        // Looking in a collection reads it.
                        self.charge(1 + right.length().unwrap_or(0) / 8)?;
                    }
                    if !builtins::compare(*op, &left, &right)? {
                        result = false;
                        break;
                    }
                    left = right;
                }
                Value::Bool(result)
            }
            Expr::Concat(parts, constant) => {
                let mut values = Vec::with_capacity(parts.len());
                for part in parts {
                    values.push(self.eval(part, scope)?);
                }
                // Where output is escaped, a join that has Markup in it is Markup, and escapes
                // the rest.
                let markup =
                    self.escape && !*constant && values.iter().any(|v| matches!(v, Value::Safe(_)));
                let mut text = String::new();
                for value in &values {
                    let piece = if markup {
                        builtins::escaped_text(value)
                    } else {
                        value.text()
                    };
                    self.reserve(piece.len())?;
                    text.push_str(&piece);
                }
                if markup {
                    Value::Safe(text.into())
                } else {
                    Value::from(text)
                }
            }
            Expr::Cond(test, then, otherwise) => {
                if self.eval(test, scope)?.truthy() {
                    self.eval(then, scope)?
                } else if let Some(otherwise) = otherwise {
                    self.eval(otherwise, scope)?
                } else {
                    Value::Undefined
                }
            }
            Expr::Attr(object, name) => {
                let object = self.eval(object, scope)?;
                builtins::get_attr(&object, name)
            }
            Expr::Item(object, key) => {
                let object = self.eval(object, scope)?;
                if let Expr::Slice(start, stop, step) = key.as_ref() {
                    let mut part = |expr: &Option<Box<Expr>>| -> Result<Option<Value>, Error> {
                        expr.as_ref().map(|e| self.eval(e, scope)).transpose()
                    };
                    let (start, stop, step) = (part(start)?, part(stop)?, part(step)?);
                    builtins::slice(&object, start.as_ref(), stop.as_ref(), step.as_ref())?
                } else {
                    let key = self.eval(key, scope)?;
                    if let Value::Str(text) = &object {
                        self.charge(text.len() / 64)?;
                    }
                    builtins::get_item(&object, &key)?
                }
            }
            Expr::Slice(..) => return Err(Error::new("a slice is only a subscript")),
            Expr::Call(callee, args) => {
                let (positional, named) = self.eval_args(args, scope)?;
                self.call_expr(callee, &positional, &named, scope)?
            }
            Expr::Filter(subject, name, args) => {
                let value = match subject {
                    Some(subject) => self.eval(subject, scope)?,
                    None => self.hole.clone().unwrap_or(Value::Undefined),
                };
                let (positional, named) = self.eval_args(args, scope)?;
                builtins::filter(self, name, &value, &positional, &named)?
            }
            Expr::Test(subject, name, args, negated) => {
                let value = self.eval(subject, scope)?;
                let (positional, named) = self.eval_args(args, scope)?;
                let result = builtins::test(self, name, &value, &positional, &named)?;
                Value::Bool(result != *negated)
            }
        })
    }

    fn eval_all(&mut self, items: &[Expr], scope: &Rc<Scope>) -> Result<Vec<Value>, Error> {
        items.iter().map(|item| self.eval(item, scope)).collect()
    }

    fn eval_args(&mut self, args: &[Arg], scope: &Rc<Scope>) -> Result<Arguments, Error> {
        let mut positional = Vec::new();
        let mut named = Vec::new();
        for arg in args {
            match arg {
                Arg::Pos(expr) => positional.push(self.eval(expr, scope)?),
                Arg::Named(name, expr) => named.push((name.clone(), self.eval(expr, scope)?)),
                Arg::Star(expr) => {
                    let value = self.eval(expr, scope)?;
                    positional.extend(
                        value
                            .items()
                            .ok_or_else(|| Error::new("* needs an iterable"))?,
                    );
                }
                Arg::StarStar(expr) => {
                    if let Value::Dict(dict) = &self.eval(expr, scope)? {
                        for (key, value) in dict.borrow().iter() {
                            named.push((key.text(), value.clone()));
                        }
                    }
                }
            }
        }
        Ok((positional, named))
    }

    fn call_expr(
        &mut self,
        callee: &Expr,
        args: &[Value],
        kwargs: &Kwargs,
        scope: &Rc<Scope>,
    ) -> Result<Value, Error> {
        if let Expr::Attr(object, name) = callee {
            let object = self.eval(object, scope)?;
            return builtins::call_method(self, &object, name, args, kwargs);
        }
        let function = self.eval(callee, scope)?;
        self.call_value(&function, args, kwargs)
    }

    fn call_macro(
        &mut self,
        def: &Rc<MacroDef>,
        defined_in: &Rc<Scope>,
        host: &Rc<Host>,
        (varargs, uses_kwargs): (bool, bool),
        args: &[Value],
        kwargs: &Kwargs,
    ) -> Result<Value, Error> {
        if self.depth >= MAX_CALL_DEPTH {
            return Err(Error::new("macros nest too deeply"));
        }
        let scope = Scope::child(defined_in);
        if args.len() > def.params.len() && !varargs {
            return Err(Error::new(format!(
                "macro '{}' takes not more than {} argument(s)",
                def.name,
                def.params.len()
            )));
        }
        if varargs {
            let extra = args.get(def.params.len()..).unwrap_or_default().to_vec();
            scope.set("varargs", Value::tuple(extra));
        }
        let mut extra_kwargs = Vec::new();
        for (name, value) in kwargs {
            if name == "caller" {
                scope.set("caller", value.clone());
            } else if def.params.iter().any(|p| &p.name == name) {
                if def
                    .params
                    .iter()
                    .position(|p| &p.name == name)
                    .is_some_and(|at| at < args.len())
                {
                    return Err(Error::new(format!(
                        "macro '{}' got multiple values for '{name}'",
                        def.name
                    )));
                }
            } else if uses_kwargs {
                extra_kwargs.push((Value::from(name.as_str()), value.clone()));
            } else {
                return Err(Error::new(format!(
                    "macro '{}' takes no keyword argument '{name}'",
                    def.name
                )));
            }
        }
        if uses_kwargs {
            scope.set("kwargs", Value::dict(extra_kwargs));
        }
        // The text of a macro is Markup when it is called where output is escaped.
        let escaping = self.escape;
        let saved = std::mem::replace(&mut self.host, Rc::clone(host));
        self.depth += 1;
        let result = (|| {
            for (at, param) in def.params.iter().enumerate() {
                let given = args.get(at).cloned().or_else(|| {
                    kwargs
                        .iter()
                        .find(|(name, _)| name == &param.name)
                        .map(|(_, value)| value.clone())
                });
                let value = match (given, &param.default) {
                    (Some(value), _) => value,
                    (None, Some(default)) => self.eval(default, &scope)?,
                    (None, None) => Value::Undefined,
                };
                scope.set(&param.name, value);
            }
            self.run(&def.body, &scope)
        })();
        self.host = saved;
        self.depth -= 1;
        result.map(|text| {
            if escaping {
                Value::Safe(text.into())
            } else {
                Value::from(text)
            }
        })
    }
}

impl Callable for Interp {
    fn call_value(
        &mut self,
        function: &Value,
        args: &[Value],
        kwargs: &Kwargs,
    ) -> Result<Value, Error> {
        match function {
            Value::Callable(function) => match &function.kind {
                Kind::Macro {
                    def,
                    scope,
                    host,
                    varargs,
                    kwargs: uses_kwargs,
                } => {
                    let (def, scope, host) = (Rc::clone(def), upgrade(scope)?, upgrade_host(host)?);
                    self.call_macro(&def, &scope, &host, (*varargs, *uses_kwargs), args, kwargs)
                }
                Kind::Native(call) => {
                    let call = Rc::clone(call);
                    call(self, args, kwargs)
                }
                Kind::Loop {
                    def,
                    scope,
                    host,
                    depth,
                } => {
                    let (def, scope, host, depth) =
                        (Rc::clone(def), upgrade(scope)?, upgrade_host(host)?, *depth);
                    let children = args.first().cloned().unwrap_or(Value::Undefined);
                    let items = children
                        .items()
                        .ok_or_else(|| Error::new("loop needs an iterable"))?;
                    if self.depth >= MAX_CALL_DEPTH {
                        return Err(Error::new("loops nest too deeply"));
                    }
                    let saved = std::mem::replace(&mut self.host, host);
                    self.depth += 1;
                    let mut out = String::new();
                    let result = self.run_loop(&def, &children, items, &scope, depth + 1, &mut out);
                    self.depth -= 1;
                    self.host = saved;
                    result.map(|()| Value::from(out))
                }
            },
            Value::Bound(bound) => {
                let (object, name) = (bound.0.clone(), bound.1.clone());
                builtins::call_method(self, &object, &name, args, kwargs)
            }
            Value::Object(object) => {
                let object = Rc::clone(object);
                match object.call(self, args, kwargs) {
                    Some(result) => result,
                    None => Err(Error::new(format!(
                        "'{}' object is not callable",
                        object.type_name()
                    ))),
                }
            }
            Value::Undefined => Ok(builtins::lenient_call(args, kwargs)),
            other => Err(Error::new(format!(
                "'{}' object is not callable",
                other.type_name()
            ))),
        }
    }
}

/// What a `for` loops over: its own copy, or a list that the body may change, which the loop reads
/// as it is, as Python's iterator does.
enum Items {
    Owned(Vec<Value>),
    Live(Rc<RefCell<Vec<Value>>>),
}

/// `loop` inside a `for`.
struct Loop {
    items: Items,
    index: Cell<usize>,
    changed: RefCell<Option<Vec<Value>>>,
    depth: usize,
    recursor: Option<Value>,
}

impl Loop {
    fn length(&self) -> usize {
        match &self.items {
            Items::Owned(items) => items.len(),
            Items::Live(list) => list.borrow().len(),
        }
    }

    fn item(&self, at: usize) -> Option<Value> {
        match &self.items {
            Items::Owned(items) => items.get(at).cloned(),
            Items::Live(list) => list.borrow().get(at).cloned(),
        }
    }
}

impl Object for Loop {
    fn type_name(&self) -> &'static str {
        "LoopContext"
    }

    fn attr(&self, name: &str) -> Option<Value> {
        let at = self.index.get();
        let length = self.length();
        Some(match name {
            "index" => Value::Int(at as i128 + 1),
            "index0" => Value::Int(at as i128),
            "revindex" => Value::Int(length.saturating_sub(at) as i128),
            "revindex0" => Value::Int(length.saturating_sub(at + 1) as i128),
            "first" => Value::Bool(at == 0),
            "last" => Value::Bool(at + 1 >= length),
            "length" => Value::Int(length as i128),
            "depth" => Value::Int(self.depth as i128 + 1),
            "depth0" => Value::Int(self.depth as i128),
            "previtem" => {
                if at == 0 {
                    Value::Undefined
                } else {
                    self.item(at - 1).unwrap_or(Value::Undefined)
                }
            }
            "nextitem" => self.item(at + 1).unwrap_or(Value::Undefined),
            _ => return None,
        })
    }

    fn call(
        &self,
        call: &mut dyn Callable,
        args: &[Value],
        kwargs: &Kwargs,
    ) -> Option<Result<Value, Error>> {
        let recursor = self.recursor.as_ref()?;
        Some(call.call_value(recursor, args, kwargs))
    }

    fn method(
        &self,
        _call: &mut dyn Callable,
        name: &str,
        args: &[Value],
        _kwargs: &Kwargs,
    ) -> Option<Result<Value, Error>> {
        match name {
            "cycle" => {
                if args.is_empty() {
                    return Some(Err(Error::new("no items for cycling given")));
                }
                Some(Ok(args[self.index.get() % args.len()].clone()))
            }
            "changed" => {
                let mut last = self.changed.borrow_mut();
                let same = last.as_ref().is_some_and(|old| {
                    old.len() == args.len() && old.iter().zip(args).all(|(a, b)| a.equals(b))
                });
                *last = Some(args.to_vec());
                Some(Ok(Value::Bool(!same)))
            }
            _ => None,
        }
    }

    fn text(&self) -> String {
        format!("<LoopContext {}/{}>", self.index.get() + 1, self.length())
    }
}

/// Number of items of a range value, for the sandbox's limit.
pub fn range_size(start: i128, stop: i128, step: i128) -> i128 {
    range_len(start, stop, step)
}

#[allow(dead_code)]
fn unused(_: Cmp, _: Bin) {}

fn upgrade(scope: &Weak<Scope>) -> Result<Rc<Scope>, Error> {
    scope
        .upgrade()
        .ok_or_else(|| Error::new("the scope of this macro is gone"))
}

fn upgrade_host(host: &Weak<Host>) -> Result<Rc<Host>, Error> {
    host.upgrade()
        .ok_or_else(|| Error::new("the context of this macro is gone"))
}
