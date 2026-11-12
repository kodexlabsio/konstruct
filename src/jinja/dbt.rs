//! dbt's side of the renderer: `ref`, `source`, `var`, `config` and the rest of what a model sees,
//! as inert stand-ins (nothing here reads a file, runs a query or reaches the environment), the
//! macros of the project and of its installed packages, and `adapter.dispatch`.

use std::cell::RefCell;
use std::collections::HashMap;
use std::rc::Rc;

use super::builtins;
use super::eval::{Budget, Callable, Error, Function, Host, Interp, Kwargs, Scope};
use super::parse::parse;
use super::value::{Object, Value};

/// The relations a manifest names.
#[derive(Default)]
pub struct Tables {
    /// A model's name, then the relation of the first model, seed or snapshot of that name.
    pub relations: HashMap<String, String>,
    /// A package and a name, then the relation.
    pub by_package: HashMap<(String, String), String>,
    /// A source's name and a table's, then the relation.
    pub sources: HashMap<(String, String), String>,
}

/// One macro file text per package, and the project's own.
pub struct Macros {
    /// The installed packages' names and the text of their macros, in the order they are read.
    pub packages: Vec<(String, String)>,
    pub own: String,
    pub project_name: Option<String>,
    /// What `var()` gives: the project's `vars`, by name.
    pub variables: Vec<(String, Value)>,
}

pub struct Renderer {
    host: Rc<Host>,
    /// What macros of the project and its packages were loaded with; kept so that the scopes
    /// their macros point to (weakly) live as long as the renderer.
    #[allow(dead_code)]
    base: Rc<Host>,
    #[allow(dead_code)]
    modules: Vec<Rc<Scope>>,
    budget: Rc<Budget>,
}

fn type_error(message: impl Into<String>) -> Error {
    Error::new(message)
}

/// The arguments of a call by position or name, for a stand-in that has a fixed signature.
fn bind<'a>(
    function: &str,
    params: &[&str],
    required: usize,
    args: &'a [Value],
    kwargs: &'a Kwargs,
) -> Result<Vec<Option<&'a Value>>, Error> {
    if args.len() > params.len() {
        return Err(type_error(format!(
            "{function}() takes at most {} arguments",
            params.len()
        )));
    }
    let mut bound: Vec<Option<&Value>> = params
        .iter()
        .enumerate()
        .map(|(at, _)| args.get(at))
        .collect();
    for (name, value) in kwargs {
        let at = params
            .iter()
            .position(|param| param == name)
            .ok_or_else(|| {
                type_error(format!(
                    "{function}() got an unexpected keyword argument '{name}'"
                ))
            })?;
        if bound[at].is_some() {
            return Err(type_error(format!(
                "{function}() got multiple values for '{name}'"
            )));
        }
        bound[at] = Some(value);
    }
    if bound.iter().take(required).any(Option::is_none) {
        return Err(type_error(format!(
            "{function}() is missing a required argument"
        )));
    }
    Ok(bound)
}

fn native(
    name: &str,
    call: impl Fn(&mut dyn Callable, &[Value], &Kwargs) -> Result<Value, Error> + 'static,
) -> Value {
    Function::native(name, Rc::new(call))
}

/// What a model that asks for `config` gets: a call that renders nothing, and attributes that are
/// not defined.
struct Config;

impl Object for Config {
    fn type_name(&self) -> &'static str {
        "function"
    }

    fn attr(&self, _: &str) -> Option<Value> {
        None
    }

    fn call(&self, _: &mut dyn Callable, _: &[Value], _: &Kwargs) -> Option<Result<Value, Error>> {
        Some(Ok(Value::from("")))
    }
}

/// `target`: what a model reads of the profile's target, which no model may change, so that one
/// model's `target.update(...)` is not another's.
struct Target {
    kind: String,
}

impl Target {
    fn entries(&self) -> [(&'static str, Value); 4] {
        [
            ("type", Value::from(self.kind.as_str())),
            ("name", Value::from("dev")),
            ("schema", Value::from("")),
            ("database", Value::from("")),
        ]
    }
}

impl Object for Target {
    fn type_name(&self) -> &'static str {
        "dict"
    }

    fn attr(&self, name: &str) -> Option<Value> {
        self.entries()
            .into_iter()
            .find(|(key, _)| *key == name)
            .map(|(_, value)| value)
    }

    fn items(&self) -> Option<Vec<Value>> {
        Some(
            self.entries()
                .into_iter()
                .map(|(key, _)| Value::from(key))
                .collect(),
        )
    }

    fn method(
        &self,
        _: &mut dyn Callable,
        name: &str,
        args: &[Value],
        kwargs: &Kwargs,
    ) -> Option<Result<Value, Error>> {
        Some(match name {
            "get" => bind("get", &["key", "default"], 1, args, kwargs).map(|bound| {
                let found = bound[0]
                    .and_then(Value::as_str)
                    .and_then(|key| self.attr(key));
                found.or_else(|| bound[1].cloned()).unwrap_or(Value::None)
            }),
            "keys" => Ok(Value::list(self.items().unwrap_or_default())),
            "values" => Ok(Value::list(
                self.entries().into_iter().map(|(_, v)| v).collect(),
            )),
            "items" => Ok(Value::list(
                self.entries()
                    .into_iter()
                    .map(|(k, v)| Value::tuple(vec![Value::from(k), v]))
                    .collect(),
            )),
            "copy" => Ok(Value::dict(
                self.entries()
                    .into_iter()
                    .map(|(k, v)| (Value::from(k), v))
                    .collect(),
            )),
            _ => return None,
        })
    }

    fn text(&self) -> String {
        let inner: Vec<String> = self
            .entries()
            .iter()
            .map(|(k, v)| format!("'{k}': {}", v.repr()))
            .collect();
        format!("{{{}}}", inner.join(", "))
    }
}

/// A package's macros, as `dbt_utils.star(...)`.
struct Package(HashMap<String, Value>);

impl Object for Package {
    fn type_name(&self) -> &'static str {
        "_Namespace"
    }

    fn attr(&self, name: &str) -> Option<Value> {
        self.0.get(name).cloned()
    }
}

struct Adapter {
    kind: String,
    macros: Rc<RefCell<HashMap<String, Value>>>,
}

impl Object for Adapter {
    fn type_name(&self) -> &'static str {
        "_Adapter"
    }

    fn attr(&self, name: &str) -> Option<Value> {
        (name == "type_").then(|| Value::from(self.kind.as_str()))
    }

    fn method(
        &self,
        _: &mut dyn Callable,
        name: &str,
        args: &[Value],
        kwargs: &Kwargs,
    ) -> Option<Result<Value, Error>> {
        Some(match name {
            "type" => bind("type", &[], 0, args, kwargs).map(|_| Value::from(self.kind.as_str())),
            "quote" => bind("quote", &["identifier"], 1, args, kwargs).map(|bound| {
                Value::from(format!(
                    "\"{}\"",
                    bound[0].map(Value::text).unwrap_or_default()
                ))
            }),
            "dispatch" => {
                bind("dispatch", &["name", "macro_namespace"], 1, args, kwargs).map(|bound| {
                    let name = bound[0].map(Value::text).unwrap_or_default();
                    let macros = self.macros.borrow();
                    [
                        format!("{}__{name}", self.kind),
                        format!("default__{name}"),
                        name,
                    ]
                    .iter()
                    .find_map(|candidate| macros.get(candidate).cloned())
                    .unwrap_or(Value::Undefined)
                })
            }
            _ => return None,
        })
    }
}

/// dbt's own cross-database macros (`dbt.date_trunc` and friends), as plain SQL.
struct DbtBuiltins;

impl Object for DbtBuiltins {
    fn type_name(&self) -> &'static str {
        "_DbtBuiltins"
    }

    fn attr(&self, _: &str) -> Option<Value> {
        None
    }

    fn method(
        &self,
        _: &mut dyn Callable,
        name: &str,
        args: &[Value],
        kwargs: &Kwargs,
    ) -> Option<Result<Value, Error>> {
        let (params, required): (&[&str], usize) = match name {
            "date_trunc" => (&["datepart", "date"], 2),
            "dateadd" => (&["datepart", "interval", "from_date_or_timestamp"], 3),
            "datediff" => (&["first_date", "second_date", "datepart"], 3),
            "last_day" => (&["date", "datepart"], 2),
            "cast" | "safe_cast" => (&["field", "type"], 2),
            "concat" => (&["fields"], 1),
            "hash" => (&["field"], 1),
            "split_part" => (&["string_text", "delimiter_text", "part_number"], 3),
            "replace" => (&["field", "old_chars", "new_chars"], 3),
            "length" | "any_value" | "bool_or" | "escape_single_quotes" => (&["expression"], 1),
            "right" => (&["string_text", "length_expression"], 2),
            "position" => (&["substring_text", "string_text"], 2),
            "listagg" => (
                &["measure", "delimiter_text", "order_by_clause", "limit_num"],
                1,
            ),
            "string_literal" => (&["value"], 1),
            "date_spine" => (&["datepart", "start_date", "end_date"], 3),
            "current_timestamp" | "type_string" | "type_int" | "type_bigint" | "type_numeric"
            | "type_float" | "type_boolean" | "type_timestamp" => (&[], 0),
            _ => return None,
        };
        Some(bind(name, params, required, args, kwargs).and_then(|bound| {
            let text = |at: usize| bound[at].map(Value::text).unwrap_or_default();
            Ok(Value::from(match name {
                "date_trunc" => format!("date_trunc('{}', {})", text(0), text(1)),
                "dateadd" => format!("date_add({}, interval ({}) {})", text(2), text(1), text(0)),
                "datediff" => format!("date_diff('{}', {}, {})", text(2), text(0), text(1)),
                "last_day" => format!("last_day({})", text(0)),
                "cast" => format!("cast({} as {})", text(0), text(1)),
                "safe_cast" => format!("try_cast({} as {})", text(0), text(1)),
                "concat" => {
                    let fields = bound[0].and_then(Value::items).ok_or_else(|| type_error("concat needs a list"))?;
                    fields.iter().map(Value::text).collect::<Vec<_>>().join(" || ")
                }
                "hash" => format!("md5(cast({} as varchar))", text(0)),
                "split_part" => format!("split_part({}, {}, {})", text(0), text(1), text(2)),
                "replace" => format!("replace({}, {}, {})", text(0), text(1), text(2)),
                "length" => format!("length({})", text(0)),
                "right" => format!("right({}, {})", text(0), text(1)),
                "position" => format!("position({} in {})", text(0), text(1)),
                "any_value" => format!("any_value({})", text(0)),
                "bool_or" => format!("bool_or({})", text(0)),
                "listagg" => {
                    let delimiter = bound[1].map_or_else(|| "','".to_string(), Value::text);
                    format!("string_agg({}, {delimiter})", text(0))
                }
                "string_literal" => format!("'{}'", text(0).replace('\'', "''")),
                "escape_single_quotes" => text(0).replace('\'', "''"),
                "date_spine" => format!(
                    "select cast(spine as date) as date_{} from generate_series(cast({} as timestamp), cast({} as timestamp), interval 1 {}) as t(spine)",
                    text(0),
                    text(1),
                    text(2),
                    text(0)
                ),
                "current_timestamp" => "current_timestamp".to_string(),
                "type_string" => "varchar".to_string(),
                "type_int" => "integer".to_string(),
                "type_bigint" => "bigint".to_string(),
                "type_numeric" => "numeric".to_string(),
                "type_float" => "float".to_string(),
                "type_boolean" => "boolean".to_string(),
                _ => "timestamp".to_string(),
            }))
        }))
    }
}

/// The names of the macros a text defines, run once with dbt's inert context, or none when it does
/// not parse or fails as it runs.
fn load(
    text: &str,
    host: &Rc<Host>,
    budget: &Rc<Budget>,
    modules: &mut Vec<Rc<Scope>>,
) -> HashMap<String, Value> {
    if text.trim().is_empty() {
        return HashMap::new();
    }
    let Ok(body) = parse(text) else {
        return HashMap::new();
    };
    let scope = Scope::root();
    let mut interp = Interp::new(Rc::clone(host), Rc::clone(budget));
    if interp.run(&body, &scope).is_err() {
        return HashMap::new();
    }
    modules.push(Rc::clone(&scope));
    scope
        .locals()
        .into_iter()
        .filter(|(name, _)| !name.starts_with('_'))
        .collect()
}

impl Renderer {
    pub fn new(tables: Tables, adapter_type: &str, macros: Macros) -> Renderer {
        let tables = Rc::new(tables);
        let flat: Rc<RefCell<HashMap<String, Value>>> = Rc::default();
        let variables: HashMap<String, Value> = macros.variables.iter().cloned().collect();
        let mut names = builtins::globals();
        let mut add = |name: &str, value: Value| {
            names.insert(name.to_string(), value);
        };
        let by = Rc::clone(&tables);
        add(
            "ref",
            native("ref", move |_, args, _| {
                let given: Vec<&str> = args.iter().filter_map(Value::as_str).collect();
                let found = match given.as_slice() {
                    [package, name, ..] => by
                        .by_package
                        .get(&((*package).to_string(), (*name).to_string()))
                        .cloned()
                        .unwrap_or_else(|| (*name).to_string()),
                    [name] => by
                        .relations
                        .get(*name)
                        .cloned()
                        .unwrap_or_else(|| (*name).to_string()),
                    [] => by.relations.get("").cloned().unwrap_or_default(),
                };
                Ok(Value::from(found))
            }),
        );
        let by = Rc::clone(&tables);
        add(
            "source",
            native("source", move |_, args, kwargs| {
                let bound = bind("source", &["source_name", "table"], 2, args, kwargs)?;
                let (source, table) = (
                    bound[0].map(Value::text).unwrap_or_default(),
                    bound[1].map(Value::text).unwrap_or_default(),
                );
                let found = by.sources.get(&(source.clone(), table.clone())).cloned();
                Ok(Value::from(
                    found.unwrap_or_else(|| format!("{source}.{table}")),
                ))
            }),
        );
        add(
            "var",
            native("var", move |_, args, kwargs| {
                let bound = bind("var", &["name", "default"], 1, args, kwargs)?;
                let known = bound[0]
                    .and_then(Value::as_str)
                    .and_then(|name| variables.get(name));
                Ok(known
                    .cloned()
                    .or_else(|| bound[1].cloned())
                    .unwrap_or(Value::None))
            }),
        );
        add("config", Value::Object(Rc::new(Config)));
        add(
            "is_incremental",
            native("is_incremental", |_, args, kwargs| {
                bind("is_incremental", &[], 0, args, kwargs)?;
                Ok(Value::Bool(false))
            }),
        );
        add(
            "env_var",
            native("env_var", |_, args, kwargs| {
                let bound = bind("env_var", &["name", "default"], 1, args, kwargs)?;
                Ok(bound[1].cloned().unwrap_or_else(|| Value::from("")))
            }),
        );
        add(
            "adapter",
            Value::Object(Rc::new(Adapter {
                kind: adapter_type.to_string(),
                macros: Rc::clone(&flat),
            })),
        );
        add(
            "target",
            Value::Object(Rc::new(Target {
                kind: adapter_type.to_string(),
            })),
        );
        add("execute", Value::Bool(false));
        add(
            "run_query",
            native("run_query", |_, _, _| Ok(Value::Undefined)),
        );
        add(
            "load_result",
            native("load_result", |_, _, _| Ok(Value::Undefined)),
        );
        add(
            "return",
            native("return", |_, args, kwargs| {
                let bound = bind("return", &["value"], 1, args, kwargs)?;
                Ok(bound[0].cloned().unwrap_or(Value::None))
            }),
        );
        add("log", native("log", |_, _, _| Ok(Value::from(""))));
        add("dbt_utils", Value::Object(Rc::new(Package(HashMap::new()))));
        add("dbt", Value::Object(Rc::new(DbtBuiltins)));
        for undefined in ["exceptions", "this", "modules"] {
            add(undefined, Value::Undefined);
        }
        let budget = Rc::new(Budget::default());
        let mut modules = Vec::new();
        let base = Rc::new(Host {
            names: names.clone(),
        });

        // The packages' macros, in the order they are read: the first package to define a name
        // keeps it, and the project's own macros replace any.
        let mut context = names;
        let mut packages: HashMap<String, Value> = HashMap::new();
        let mut macro_names: HashMap<String, Value> = HashMap::new();
        for (name, text) in &macros.packages {
            let defined = load(text, &base, &budget, &mut modules);
            for (macro_name, value) in &defined {
                macro_names
                    .entry(macro_name.clone())
                    .or_insert_with(|| value.clone());
            }
            packages.insert(name.clone(), Value::Object(Rc::new(Package(defined))));
        }
        let own = load(&macros.own, &base, &budget, &mut modules);
        macro_names.extend(
            own.iter()
                .map(|(name, value)| (name.clone(), value.clone())),
        );
        if let Some(project) = &macros.project_name {
            packages.insert(project.clone(), Value::Object(Rc::new(Package(own))));
        }
        *flat.borrow_mut() = macro_names.clone();
        context.extend(macro_names);
        context.extend(packages);
        Renderer {
            host: Rc::new(Host { names: context }),
            base,
            modules,
            budget,
        }
    }

    pub fn render(&self, sql: &str) -> Result<String, Error> {
        let body = parse(sql)?;
        let scope = Scope::root();
        Interp::new(Rc::clone(&self.host), Rc::clone(&self.budget)).run(&body, &scope)
    }
}
