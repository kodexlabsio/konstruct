use super::{Macros, Renderer, Tables};

fn tables() -> Tables {
    let mut tables = Tables::default();
    tables
        .relations
        .insert("orders".into(), "\"db\".\"main\".\"orders\"".into());
    tables.by_package.insert(
        ("shop".into(), "orders".into()),
        "\"db\".\"main\".\"orders\"".into(),
    );
    tables.sources.insert(
        ("app".into(), "events".into()),
        "\"db\".\"raw\".\"events\"".into(),
    );
    tables
}

fn renderer(own: &str, packages: &[(&str, &str)]) -> Renderer {
    let macros = Macros {
        packages: packages
            .iter()
            .map(|(name, text)| ((*name).into(), (*text).into()))
            .collect(),
        own: own.into(),
        project_name: Some("shop".into()),
        variables: vec![("region".into(), "eu".into())],
    };
    Renderer::new(tables(), "duckdb", macros)
}

fn render(sql: &str) -> String {
    renderer("", &[])
        .render(sql)
        .unwrap_or_else(|error| format!("!{error}"))
}

fn fails(sql: &str) -> bool {
    renderer("", &[]).render(sql).is_err()
}

#[test]
fn ref_and_source_name_the_relations_of_the_manifest() {
    assert_eq!(
        render("{{ ref('orders') }}|{{ ref('shop', 'orders') }}"),
        "\"db\".\"main\".\"orders\"|\"db\".\"main\".\"orders\""
    );
    assert_eq!(
        render("{{ ref('nope') }}|{{ source('app', 'events') }}|{{ source('app', 'x') }}"),
        "nope|\"db\".\"raw\".\"events\"|app.x"
    );
}

#[test]
fn values_print_as_python_prints_them() {
    assert_eq!(
        render("{{ true }} {{ none }} {{ 1.0 }} {{ [1, 'a'] }} {{ {'k': (1,)} }}"),
        "True None 1.0 [1, 'a'] {'k': (1,)}"
    );
    assert_eq!(
        render("{{ 1e16 }} {{ 1.5e-7 }} {{ 0.0001 }} {{ 7 / 2 }} {{ 0.1 + 0.2 }}"),
        "1e+16 1.5e-07 0.0001 3.5 0.30000000000000004"
    );
}

#[test]
fn lists_and_dicts_change_in_place() {
    assert_eq!(
        render("{% set a = [1] %}{% set b = a %}{% do b.append(2) %}{{ a }}"),
        "[1, 2]"
    );
    assert_eq!(
        render("{% set d = {} %}{% do d.update(x=1) %}{% do d.setdefault('y', 2) %}{{ d }}"),
        "{'x': 1, 'y': 2}"
    );
}

#[test]
fn whitespace_control_strips_what_the_markers_ask() {
    assert_eq!(
        render("a  {{- 'b' -}}  c  {%- if true -%}  d  {%- endif -%}  e"),
        "abcde"
    );
    assert_eq!(render("a\n{% if true %}\nb\n{% endif %}\nc"), "a\n\nb\n\nc");
    assert_eq!(render("select 1\n"), "select 1");
    assert_eq!(render("a\r\nb\rc"), "a\nb\nc");
}

#[test]
fn raw_blocks_and_comments_are_not_rendered() {
    assert_eq!(
        render("{% raw %}{{ x }}{% endraw %}{# gone #}!"),
        "{{ x }}!"
    );
}

#[test]
fn a_for_loop_has_a_scope_and_a_loop_object() {
    assert_eq!(
        render("{% for i in range(3) %}{{ loop.index }}{{ ',' if not loop.last }}{% endfor %}"),
        "1,2,3"
    );
    assert_eq!(
        render("{% for i in range(2) %}{% set seen = i %}{% endfor %}[{{ seen }}]"),
        "[]"
    );
    assert_eq!(
        render("{% for k, v in {'a': 1}.items() %}{{ k }}{{ v }}{% else %}none{% endfor %}"),
        "a1"
    );
    assert_eq!(
        render("{% for x in [] %}{% else %}none{% endfor %}"),
        "none"
    );
}

#[test]
fn namespaces_carry_a_value_out_of_a_loop() {
    assert_eq!(
        render(
            "{% set ns = namespace(n=0) %}{% for i in range(4) %}{% set ns.n = ns.n + i %}{% endfor %}{{ ns.n }}"
        ),
        "6"
    );
}

#[test]
fn macros_take_defaults_keywords_and_a_caller() {
    assert_eq!(
        render("{% macro m(a, b=2) %}{{ a }}-{{ b }}{% endmacro %}{{ m(1) }} {{ m(1, b=5) }}"),
        "1-2 1-5"
    );
    assert_eq!(
        render("{% macro w() %}[{{ caller() }}]{% endmacro %}{% call w() %}x{% endcall %}"),
        "[x]"
    );
    assert!(fails("{% macro m(a) %}{% endmacro %}{{ m(1, 2) }}"));
    assert!(fails("{% macro m(a) %}{% endmacro %}{{ m(1, z=2) }}"));
}

#[test]
fn a_name_that_is_not_defined_can_be_called_and_keeps_its_arguments() {
    assert_eq!(
        render("{{ unknown() }}|{{ unknown('a', 1) }}|{{ unknown('day', ['x', 'y']) }}"),
        "|konstruct_macro(a, 1)|konstruct_macro('day', x, y)"
    );
    assert_eq!(
        render("{{ nothing.a.b }}|{{ nothing | default('d') }}|{{ nothing is defined }}"),
        "|d|False"
    );
    assert!(fails("{{ nothing + 1 }}"));
}

#[test]
fn var_gives_the_projects_vars_before_its_default() {
    assert_eq!(
        render("{{ var('region', 'us') }}{{ var('other', 'us') }}{{ var('none') }}"),
        "euusNone"
    );
}

#[test]
fn dbt_stand_ins_render_inert_values() {
    assert_eq!(
        render(
            "{{ config(materialized='table') }}{{ is_incremental() }}{{ this }}{{ env_var('HOME', 'x') }}{{ var('v', 3) }}"
        ),
        "False".to_string() + "x3"
    );
    assert_eq!(
        render("{% set r = run_query('delete from t') %}[{{ r }}]{% for row in r %}x{% endfor %}"),
        "[]"
    );
    assert_eq!(
        render("{{ target.type }}{{ adapter.quote('a') }}{{ dbt.date_trunc('day', 'c') }}"),
        "duckdb\"a\"date_trunc('day', c)"
    );
}

#[test]
fn the_sandbox_refuses_underscore_attributes_and_other_templates() {
    assert_eq!(
        render("[{{ ''.__class__ }}][{{ ref.__globals__ }}][{{ {}.__class__.__mro__ }}]"),
        "[][][]"
    );
    assert!(fails("{% import 'x' as y %}"));
    assert!(fails("{% include 'x' %}"));
    assert_eq!(render("{% if false %}{% include 'x' %}{% endif %}ok"), "ok");
    assert!(fails("{{ range(1000000000) | list }}"));
}

#[test]
fn syntax_errors_and_unknown_tags_fail() {
    for source in [
        "{% if %}",
        "{{ 1 + }}",
        "{% for x in y %}",
        "{% endif %}",
        "{% nonsense %}",
        "{# open",
        "{{ 'open }}",
    ] {
        assert!(fails(source), "{source}");
    }
}

#[test]
fn macros_come_from_the_project_and_its_packages() {
    let renderer = renderer(
        "{% macro own() %}O{% endmacro %}{% macro dup() %}OWN{% endmacro %}",
        &[
            (
                "one",
                "{% macro dup() %}ONE{% endmacro %}{% macro pk() %}P{% endmacro %}",
            ),
            ("two", "{% macro dup() %}TWO{% endmacro %}"),
        ],
    );
    assert_eq!(
        renderer
            .render(
                "{{ own() }}{{ pk() }}{{ dup() }}{{ one.dup() }}{{ two.dup() }}{{ shop.dup() }}"
            )
            .unwrap(),
        "OPOWNONETWOOWN"
    );
    assert_eq!(
        renderer.render("{{ one.nope('a') }}").unwrap(),
        "konstruct_macro(a)"
    );
}

#[test]
fn a_macro_file_that_does_not_parse_gives_no_macros() {
    let renderer = renderer("{% macro a() %}A{% endmacro %}{% if %}", &[]);
    assert_eq!(
        renderer.render("{{ a() }}|{{ a(1) }}").unwrap(),
        "|konstruct_macro(1)"
    );
}

#[test]
fn dispatch_prefers_the_adapter_then_default_then_the_plain_name() {
    let own = "{% macro go() %}{{ adapter.dispatch('f')() }}{% endmacro %}{% macro default__f() %}D{% endmacro %}{% macro duckdb__f() %}U{% endmacro %}";
    assert_eq!(renderer(own, &[]).render("{{ go() }}").unwrap(), "U");
    let own = "{% macro go() %}{{ adapter.dispatch('f')() }}{% endmacro %}{% macro f() %}PLAIN{% endmacro %}";
    assert_eq!(renderer(own, &[]).render("{{ go() }}").unwrap(), "PLAIN");
    let own = "{% macro go() %}{{ adapter.dispatch('missing')(1, 2) }}{% endmacro %}";
    assert_eq!(
        renderer(own, &[]).render("{{ go() }}").unwrap(),
        "konstruct_macro(1, 2)"
    );
}

#[test]
fn a_macro_in_a_package_sees_only_dbts_context() {
    let renderer = renderer(
        "",
        &[
            ("one", "{% macro a() %}{{ b() }}{% endmacro %}"),
            ("two", "{% macro b() %}B{% endmacro %}"),
        ],
    );
    assert_eq!(renderer.render("{{ a() }}|{{ b() }}").unwrap(), "|B");
}

#[test]
fn filters_and_tests_of_jinja_behave_as_jinja_does() {
    assert_eq!(
        render(
            "{{ ['b', 'A'] | sort | join(',') }}{{ 'a b' | title }}{{ [1, 2, 3] | sum }}{{ 2.5 | round }}"
        ),
        "A,bA B62.0"
    );
    assert_eq!(
        render("{{ [{'n': 1}, {'n': 2}] | selectattr('n', 'gt', 1) | map(attribute='n') | list }}"),
        "[2]"
    );
    assert_eq!(
        render("{{ 4 is divisibleby 2 }}{{ none is none }}{{ 'a' is string }}{{ 3 is not even }}"),
        "TrueTrueTrueTrue"
    );
    assert!(fails("{{ 1 | not_a_filter }}"));
}

#[test]
fn strings_have_the_methods_dbt_projects_call() {
    assert_eq!(
        render(
            "{{ 'a,b'.split(',') }}{{ ' x '.strip() }}{{ 'abc'.upper() }}{{ '{}-{}'.format(1, 'a') }}{{ '%s=%d' % ('k', 2) }}"
        ),
        "['a', 'b']xABC1-ak=2"
    );
    assert_eq!(
        render("{{ 'abcdef'[1:3] }}{{ 'abc'[::-1] }}{{ [1, 2, 3][-1] }}"),
        "bccba3"
    );
}

#[test]
fn a_render_that_does_too_much_work_stops() {
    assert!(fails(
        "{% for i in range(100000) %}{% for j in range(100000) %}x{% endfor %}{% endfor %}"
    ));
}

#[test]
fn macros_nested_too_deep_fail_instead_of_overflowing_the_stack() {
    let nested =
        "{% macro r(n) %}{% if n > 0 %}{{ r(n - 1) }}{% endif %}x{% endmacro %}{{ r(100000) }}";
    // A small stack, as a thread on some systems has, is enough: the render stops at its own limit.
    let outcome = std::thread::Builder::new()
        .stack_size(2 * 1024 * 1024)
        .spawn(move || render(nested))
        .map(|handle| handle.join());
    assert!(matches!(outcome, Ok(Ok(text)) if text.starts_with('!')));
}

#[test]
fn blocks_nested_too_deep_fail_to_parse() {
    let source = format!("{}x{}", "{% if 1 %}".repeat(500), "{% endif %}".repeat(500));
    assert!(fails(&source));
}
