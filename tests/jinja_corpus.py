"""Real dbt template patterns for the Jinja renderer: each case is a model's SQL and, optionally, the
macro files of the project and of its installed packages. The expected output of each is in
``tests/golden/jinja_corpus.json``, taken from the Jinja2 renderer Konstruct used before the Rust one
(kept as the oracle in ``tests/jinja_oracle.py``)."""

from __future__ import annotations

from typing import Any

MANIFEST: dict[str, Any] = {
    'metadata': {'adapter_type': 'duckdb', 'project_name': 'shop'},
    'nodes': {
        'model.shop.stg_orders': {
            'resource_type': 'model',
            'name': 'stg_orders',
            'package_name': 'shop',
            'relation_name': '"db"."main"."stg_orders"',
        },
        'model.shop.customers': {
            'resource_type': 'model',
            'name': 'customers',
            'package_name': 'shop',
            'relation_name': '"db"."main"."customers"',
        },
        'seed.shop.countries': {
            'resource_type': 'seed',
            'name': 'countries',
            'package_name': 'shop',
            'relation_name': '"db"."main"."countries"',
        },
        'model.other.customers': {
            'resource_type': 'model',
            'name': 'customers',
            'package_name': 'other',
            'relation_name': '"db"."other"."customers"',
        },
        'test.shop.unique': {'resource_type': 'test', 'name': 'customers', 'relation_name': '"x"'},
        'model.shop.norel': {'resource_type': 'model', 'name': 'norel', 'package_name': 'shop'},
    },
    'sources': {
        'source.shop.app.events': {
            'source_name': 'app',
            'name': 'events',
            'relation_name': '"db"."raw"."events"',
        },
        'source.shop.app.norel': {'source_name': 'app', 'name': 'norel'},
    },
}

OWN = {
    'macros/money.sql': (
        "{% macro cents_to_dollars(col, precision=2) %}{{ return(adapter.dispatch('cents_to_dollars')"
        '(col, precision)) }}{% endmacro %}\n'
        '{% macro default__cents_to_dollars(col, precision) %}round({{ col }} / 100, {{ precision }})'
        '{% endmacro %}\n'
        '{% macro duckdb__cents_to_dollars(col, precision) %}({{ col }} / 100.0){% endmacro %}\n'
    ),
    'macros/util.sql': (
        '{% macro pivot_sum(column, values) -%}\n'
        '  {%- for v in values -%}\n'
        "    sum(case when {{ column }} = '{{ v }}' then 1 else 0 end) as {{ v | lower }}"
        '{{ "," if not loop.last }}\n'
        '  {%- endfor -%}\n'
        '{%- endmacro %}\n'
        '{% macro greet(name="world") %}hello {{ name }}{% endmacro %}\n'
        '{% macro wrap(x) %}[{{ caller() if caller else x }}]{% endmacro %}\n'
        '{% macro calls_ref(name) %}select * from {{ ref(name) }}{% endmacro %}\n'
        '{% macro uses_other() %}{{ greet("there") }}!{% endmacro %}\n'
    ),
}

PACKAGE = {
    'dbt_packages/dbt_utils/macros/keys.sql': (
        '{% macro generate_surrogate_key(field_list) %}'
        'md5(cast(concat({{ field_list | join(", \'-\', ") }}) as varchar))'
        '{% endmacro %}\n'
        '{% macro star(from, except=[]) %}* exclude ({{ except | join(", ") }}){% endmacro %}\n'
        '{% macro default__helper(x) %}default_helper({{ x }}){% endmacro %}\n'
        '{% macro helper(x) %}{{ return(adapter.dispatch("helper", "dbt_utils")(x)) }}{% endmacro %}\n'
    ),
    'dbt_packages/dbt_date/macros/dates.sql': (
        '{% macro today() %}current_date{% endmacro %}\n'
        '{% macro n_days_ago(n, date=None) %}'
        "{{ dbt.dateadd('day', -n, date or dbt_date.today()) }}"
        '{% endmacro %}\n'
    ),
}

# name -> (sql, macro files)
CASES: dict[str, tuple[str, dict[str, str]]] = {
    'plain': ('select 1 as one', {}),
    'empty': ('', {}),
    'ref_one': ("select * from {{ ref('stg_orders') }}", {}),
    'ref_double_quotes': ('select * from {{ ref("stg_orders") }}', {}),
    'ref_unknown': ("select * from {{ ref('nope') }}", {}),
    'ref_package': ("select * from {{ ref('other', 'customers') }}", {}),
    'ref_package_unknown': ("select * from {{ ref('other', 'nope') }}", {}),
    'ref_first_wins': ("select * from {{ ref('customers') }}", {}),
    'ref_seed': ("select * from {{ ref('countries') }}", {}),
    'ref_without_relation': ("select * from {{ ref('norel') }}", {}),
    'ref_version': ("select * from {{ ref('stg_orders', v=2) }}", {}),
    'ref_keyword_version': ("select * from {{ ref('other', 'customers', version=3) }}", {}),
    'source_known': ("select * from {{ source('app', 'events') }}", {}),
    'source_unknown': ("select * from {{ source('app', 'nope') }}", {}),
    'source_without_relation': ("select * from {{ source('app', 'norel') }}", {}),
    'var_default': ("select {{ var('vat', 0.2) }} as vat", {}),
    'var_none': ("select '{{ var('x') }}' as x", {}),
    'var_string_default': ("select '{{ var('region', 'eu') }}' as r", {}),
    'var_bool_default': ("select {{ var('flag', true) }} as f, {{ var('g', false) }} as g", {}),
    'var_int_default': ("select {{ var('limit', 100) }} as n", {}),
    'var_list_default': ("select {{ var('cols', ['a', 'b']) }} as c", {}),
    'var_in_if': ("{% if var('full_refresh', false) %}select 1{% else %}select 2{% endif %}", {}),
    'env_var_default': ("select '{{ env_var('HOME_NOT_SET', 'fallback') }}' as e", {}),
    'env_var_no_default': ("select '{{ env_var('SECRET') }}' as e", {}),
    'env_var_never_reads': ("select '{{ env_var('PATH', 'sandboxed') }}' as e", {}),
    'config_call': ("{{ config(materialized='table', tags=['a']) }}select 1", {}),
    'config_multiline': (
        "{{\n  config(\n    materialized='incremental',\n    unique_key='id'\n  )\n}}\nselect 1 as id",
        {},
    ),
    'config_get': ("select {{ config.get('materialized', 'view') }} as m", {}),
    'config_require': ("select {{ config.require('meta') }} as m", {}),
    'is_incremental_false': (
        "select * from {{ ref('stg_orders') }}\n{% if is_incremental() %}where t > (select max(t) from {{ this }}){% endif %}",
        {},
    ),
    'is_incremental_else': ('{% if is_incremental() %}a{% else %}b{% endif %}', {}),
    'is_incremental_not': ('{% if not is_incremental() %}full{% endif %}', {}),
    'this_plain': ('select max(t) from {{ this }}', {}),
    'this_attrs': ("select '{{ this.schema }}' as s, '{{ this.name }}' as n", {}),
    'this_in_string': ('select * from {{ this }} where 1 = 0', {}),
    'target_type': ("select '{{ target.type }}' as t, '{{ target.name }}' as n", {}),
    'target_missing': ("select '{{ target.threads }}' as t", {}),
    'target_if': ("{% if target.type == 'duckdb' %}select 1{% else %}select 2{% endif %}", {}),
    'target_schema': ("select '{{ target.schema }}.{{ target.database }}'", {}),
    'execute_flag': ('{% if execute %}select 1{% else %}select 2{% endif %}', {}),
    'run_query_not_run': (
        "{% set results = run_query('select distinct x from t') %}"
        '{% if execute %}{% set values = results.columns[0].values() %}{% else %}{% set values = [] %}{% endif %}'
        'select {{ values | length }} as n',
        {},
    ),
    'run_query_result': ("{% set r = run_query('delete from t') %}select '{{ r }}' as r", {}),
    'run_query_loop': (
        "{% set rows = run_query('select 1') %}{% for row in rows %}x{{ row }}{% endfor %}select 1",
        {},
    ),
    'statement_block': (
        "{% call statement('s', fetch_result=True) %}select 1{% endcall %}"
        "{% set v = load_result('s') %}select '{{ v }}' as v",
        {},
    ),
    'load_result_index': ("{% set v = load_result('s')['data'] %}select '{{ v }}' as v", {}),
    'log_call': ("{{ log('hello', info=True) }}select 1", {}),
    'exceptions_raise': (
        "{% if false %}{{ exceptions.raise_compiler_error('bad') }}{% endif %}select 1",
        {},
    ),
    'exceptions_raise_called': ("select {{ exceptions.raise_compiler_error('bad') }}", {}),
    'modules_call': ("select '{{ modules.datetime.datetime.now() }}' as t", {}),
    'adapter_type': ("select '{{ adapter.type() }}' as t", {}),
    'adapter_quote': ("select {{ adapter.quote('my col') }} from t", {}),
    'adapter_unknown_method': ('select {{ adapter.get_columns_in_relation(this) }} from t', {}),
    'builtin_date_trunc': ("select {{ dbt.date_trunc('month', 'created_at') }}", {}),
    'builtin_dateadd': ("select {{ dbt.dateadd('day', 7, 'created_at') }}", {}),
    'builtin_datediff': ("select {{ dbt.datediff('a', 'b', 'day') }}", {}),
    'builtin_last_day': ("select {{ dbt.last_day('d', 'month') }}", {}),
    'builtin_cast': (
        "select {{ dbt.cast('x', dbt.type_string()) }}, {{ dbt.safe_cast('y', dbt.type_int()) }}",
        {},
    ),
    'builtin_concat': ("select {{ dbt.concat(['a', 'b', 'c']) }}", {}),
    'builtin_hash': ("select {{ dbt.hash('x') }}", {}),
    'builtin_string_ops': (
        "select {{ dbt.split_part('s', \"','\", 1) }}, {{ dbt.replace('f', \"'a'\", \"'b'\") }}, "
        "{{ dbt.length('x') }}, {{ dbt.right('x', 3) }}, {{ dbt.position(\"'a'\", 'x') }}",
        {},
    ),
    'builtin_aggregates': ("select {{ dbt.any_value('x') }}, {{ dbt.bool_or('flag') }}", {}),
    'builtin_listagg': (
        "select {{ dbt.listagg('x', \"', '\", 'order by x', 5) }}, {{ dbt.listagg('y') }}",
        {},
    ),
    'builtin_quoting': (
        'select {{ dbt.string_literal("it\'s") }}, {{ dbt.escape_single_quotes("it\'s") }}',
        {},
    ),
    'builtin_date_spine': (
        "{{ dbt.date_spine('day', \"cast('2020-01-01' as date)\", \"cast('2021-01-01' as date)\") }}",
        {},
    ),
    'builtin_types': (
        'select {{ dbt.type_string() }}, {{ dbt.type_int() }}, {{ dbt.type_bigint() }}, '
        '{{ dbt.type_numeric() }}, {{ dbt.type_float() }}, {{ dbt.type_boolean() }}, '
        '{{ dbt.type_timestamp() }}, {{ dbt.current_timestamp() }}',
        {},
    ),
    'builtin_unknown': ("select {{ dbt.some_new_macro('a', 'b') }}", {}),
    'builtin_unknown_noargs': ('select {{ dbt.some_new_macro() }} x', {}),
    'dbt_utils_unknown': ("select {{ dbt_utils.generate_surrogate_key(['a', 'b']) }} as id", {}),
    'dbt_utils_package': ("select {{ dbt_utils.generate_surrogate_key(['a', 'b']) }} as id", PACKAGE),
    'dbt_utils_star': ("select {{ dbt_utils.star(ref('stg_orders'), except=['a', 'b']) }}", PACKAGE),
    'dbt_utils_dispatch': ("select {{ dbt_utils.helper('x') }}", PACKAGE),
    'package_macro_unqualified': ("select {{ generate_surrogate_key(['a']) }}", PACKAGE),
    'dbt_date_today': ('select {{ dbt_date.today() }}', PACKAGE),
    'dbt_date_n_days_ago': ("select {{ dbt_date.n_days_ago(7, 'ts') }}", PACKAGE),
    'own_dispatch': ("select {{ cents_to_dollars('amount') }} as d", OWN),
    'own_dispatch_arg': ("select {{ cents_to_dollars('amount', 4) }} as d", OWN),
    'own_dispatch_kwarg': ("select {{ cents_to_dollars('amount', precision=0) }} as d", OWN),
    'own_pivot': ("select {{ pivot_sum('method', ['Card', 'Cash']) }} from t", OWN),
    'own_default_args': ("select '{{ greet() }}', '{{ greet('x') }}', '{{ greet(name='y') }}'", OWN),
    'own_nested_macro': ("select '{{ uses_other() }}'", OWN),
    'own_macro_calls_ref': ("{{ calls_ref('stg_orders') }}", OWN),
    'own_macro_namespace': ("select '{{ shop.greet('ns') }}'", OWN),
    'own_call_block': ("{% call wrap('z') %}inner {{ 1 + 1 }}{% endcall %}", OWN),
    'own_macro_no_caller': ("{{ wrap('z') }}", OWN),
    'own_unknown_macro': ("select {{ not_defined('a', 'b', 1) }} as x", OWN),
    'unknown_macro_noargs': ('select {{ not_defined() }} as x', {}),
    'unknown_macro_dateparts': ("select {{ not_defined('day', 'created_at', 'month') }}", {}),
    'unknown_macro_kwargs': ("select {{ not_defined(a='x', b=2) }}", {}),
    'unknown_macro_list': ("select {{ not_defined(['x', 'y'], 'week') }}", {}),
    'unknown_macro_attr': ('select {{ not_defined.a.b(1) }}', {}),
    'unknown_macro_index': ("select {{ not_defined['a'](1) }}", {}),
    'unknown_variable': ("select '{{ nothing }}' as x", {}),
    'unknown_variable_in_if': ('{% if nothing %}a{% else %}b{% endif %}', {}),
    'unknown_variable_loop': ('{% for x in nothing %}a{% endfor %}b', {}),
    'unknown_variable_filter': ("select '{{ nothing | default('d') }}' as x", {}),
    'unknown_variable_length': ('select {{ nothing | length }}', {}),
    'unknown_variable_attr': ("select '{{ nothing.a.b }}'", {}),
    'unknown_in_macro_arg': ('select {{ greet(nothing) }}', OWN),
    'loop_basic': (
        "{% for c in ['a', 'b', 'c'] %}{{ c }}{% if not loop.last %}, {% endif %}{% endfor %}",
        {},
    ),
    'loop_vars': (
        "{% for c in ['a', 'b'] %}{{ loop.index }}/{{ loop.index0 }}/{{ loop.first }}/{{ loop.last }}/{{ loop.length }} {% endfor %}",
        {},
    ),
    'loop_dict_items': (
        "{% set m = {'a': 1, 'b': 2} %}{% for k, v in m.items() %}{{ k }}={{ v }};{% endfor %}",
        {},
    ),
    'loop_range': ('{% for i in range(3) %}c{{ i }},{% endfor %}', {}),
    'loop_range_start': ('{% for i in range(1, 4) %}{{ i }}{% endfor %}', {}),
    'loop_else': ('{% for i in [] %}x{% else %}none{% endfor %}', {}),
    'loop_nested': (
        "{% for a in ['x', 'y'] %}{% for b in [1, 2] %}{{ a }}{{ b }} {% endfor %}{% endfor %}",
        {},
    ),
    'loop_filter': ('{% for i in range(6) if i % 2 == 0 %}{{ i }}{% endfor %}', {}),
    'loop_cycle': ("{% for i in range(4) %}{{ loop.cycle('a', 'b') }}{% endfor %}", {}),
    'loop_revindex': ('{% for i in range(3) %}{{ loop.revindex }}{% endfor %}', {}),
    'loop_previtem': ('{% for i in range(3) %}{{ loop.previtem }}|{% endfor %}', {}),
    'set_basic': ('{% set x = 5 %}select {{ x }}', {}),
    'set_list': ("{% set cols = ['a', 'b'] %}select {{ cols | join(', ') }}", {}),
    'set_multi': ('{% set a, b = 1, 2 %}{{ a }}{{ b }}', {}),
    'set_block': ('{% set body %}select 1{% endset %}{{ body }} as x', {}),
    'set_scope_loop': ('{% set x = 1 %}{% for i in range(3) %}{% set x = i %}{% endfor %}{{ x }}', {}),
    'namespace': (
        '{% set ns = namespace(total=0) %}{% for i in range(4) %}{% set ns.total = ns.total + i %}{% endfor %}{{ ns.total }}',
        {},
    ),
    'do_statement': ('{% set l = [] %}{% do l.append(1) %}{% do l.append(2) %}{{ l }}', {}),
    'do_dict_update': ("{% set d = {} %}{% do d.update({'a': 1}) %}{{ d }}", {}),
    'if_elif': (
        '{% set n = 2 %}{% if n == 1 %}one{% elif n == 2 %}two{% else %}many{% endif %}',
        {},
    ),
    'if_in': ("{% if 'a' in ['a', 'b'] %}yes{% endif %}", {}),
    'if_is_defined': ('{% if nothing is defined %}d{% else %}u{% endif %}', {}),
    'if_is_none': ("{% if var('x') is none %}n{% else %}v{% endif %}", {}),
    'if_is_string': ("{% if 'a' is string %}s{% endif %}{% if 1 is number %}n{% endif %}", {}),
    'if_is_sequence': ('{% if [1] is sequence %}s{% endif %}{% if {} is mapping %}m{% endif %}', {}),
    'if_ternary': ("{{ 'a' if true else 'b' }}{{ 'c' if false else 'd' }}", {}),
    'if_and_or': ('{% if true and not false or false %}ok{% endif %}', {}),
    'inline_if_no_else': ("a{{ 'b' if false }}c", {}),
    'comparison_ops': ("{{ 1 < 2 }} {{ 2 <= 2 }} {{ 3 != 3 }} {{ 'a' == 'a' }}", {}),
    'arith': ('{{ 1 + 2 }} {{ 7 // 2 }} {{ 7 % 3 }} {{ 2 ** 3 }} {{ 7 / 2 }} {{ 6 / 3 }} {{ 1.5 * 2 }}', {}),
    'concat_tilde': ("{{ 'a' ~ 1 ~ 'b' }}", {}),
    'string_concat_plus': ("{{ 'a' + 'b' }}", {}),
    'literals': (
        "{{ true }} {{ false }} {{ none }} {{ 1 }} {{ 1.0 }} {{ 'x' }} {{ [1, 'a'] }} {{ {'k': 1} }}",
        {},
    ),
    'literal_tuple': ("{{ (1, 2) }} {{ ('a',) }}", {}),
    'literal_nested': ("{{ [[1, 2], {'a': [true, none]}] }}", {}),
    'float_repr': ('{{ 0.1 }} {{ 1e3 }} {{ 1.5e-7 }} {{ 100.0 }} {{ 1 / 3 }}', {}),
    'string_repr_quotes': ("{{ ['it\\'s', 'say \"hi\"', 'a\\nb'] }}", {}),
    'filters_case': (
        "{{ 'aBc' | upper }} {{ 'aBc' | lower }} {{ 'abc' | capitalize }} {{ 'abc def' | title }}",
        {},
    ),
    'filters_string': ("{{ '  x ' | trim }}|{{ 'abc' | replace('b', 'z') }}|{{ 'abc' | length }}", {}),
    'filters_default': ("{{ none | default('d') }} {{ '' | default('e', true) }} {{ x | default('f') }}", {}),
    'filters_join': ("{{ ['a', 'b'] | join }} {{ [1, 2] | join('-') }}", {}),
    'filters_list': (
        '{{ [3, 1, 2] | sort }} {{ [1, 2, 3] | first }} {{ [1, 2, 3] | last }} {{ [1, 2] | reverse | list }}',
        {},
    ),
    'filters_numbers': (
        "{{ -3 | abs }} {{ 3.7 | round }} {{ 3.14159 | round(2) }} {{ '5' | int + 1 }} {{ '2.5' | float }}",
        {},
    ),
    'filters_sum_min_max': ('{{ [1, 2, 3] | sum }} {{ [4, 2] | min }} {{ [4, 2] | max }}', {}),
    'filters_unique': ('{{ [1, 1, 2] | unique | list }}', {}),
    'filters_map_select': (
        "{{ ['a', 'b'] | map('upper') | join(',') }} {{ [1, 2, 3, 4] | select('odd') | list }} {{ [1, 2, 3] | reject('even') | list }}",
        {},
    ),
    'filters_selectattr': (
        "{% set cs = [{'n': 'a', 'k': true}, {'n': 'b', 'k': false}] %}{{ cs | selectattr('k') | map(attribute='n') | list }}",
        {},
    ),
    'filters_dictsort': ("{{ {'b': 1, 'a': 2} | dictsort }}", {}),
    'filters_tojson': ("{{ [1, 'a'] | tojson }} {{ {'a': 1} | tojson }}", {}),
    'filters_string_to_str': ('{{ 5 | string }}{{ [1] | string }}', {}),
    'filters_batch_slice': ('{{ [1, 2, 3, 4, 5] | batch(2) | list }}', {}),
    'filters_indent': ("{{ 'a\\nb' | indent(2) }}", {}),
    'filters_truncate': ("{{ 'hello world' | truncate(8) }}", {}),
    'filters_wordcount': ("{{ 'a b c' | wordcount }}", {}),
    'filters_center': ("{{ 'ab' | center(6) }}|", {}),
    'filters_format': ("{{ '%s-%s' | format('a', 'b') }} {{ '%05.1f' | format(3.14159) }}", {}),
    'filters_escape': ("{{ '<a>&' | escape }} {{ '<a>' | e }}", {}),
    'filters_safe': ("{{ '<a>' | safe }}", {}),
    'filters_urlencode': ("{{ 'a b&c' | urlencode }}", {}),
    'filters_trim_chars': ("{{ 'xxaxx' | trim('x') }}", {}),
    'filters_list_string': ("{{ 'abc' | list }}", {}),
    'filters_attr': ("{% set d = {'a': 1} %}{{ d | attr('a') }}", {}),
    'filters_items': ("{{ {'a': 1} | items | list }}", {}),
    'filters_groupby': (
        "{% for g in [{'k': 1, 'v': 'a'}, {'k': 1, 'v': 'b'}, {'k': 2, 'v': 'c'}] | groupby('k') %}{{ g.grouper }}:{{ g.list | length }} {% endfor %}",
        {},
    ),
    'filters_lower_loop': ("{% for c in ['A', 'B'] %}{{ c | lower }}{% endfor %}", {}),
    'filters_unknown': ("{{ 'x' | made_up_filter }}", {}),
    'tests_unknown': ('{% if 1 is made_up_test %}x{% endif %}', {}),
    'tests_divisibleby': ('{% if 4 is divisibleby 2 %}x{% endif %}', {}),
    'tests_in_eq': (
        '{% if 3 is eq 3 %}a{% endif %}{% if 3 is ne 4 %}b{% endif %}{% if 3 is gt 2 %}c{% endif %}',
        {},
    ),
    'method_upper': ("{{ 'abc'.upper() }} {{ 'ABC'.lower() }} {{ 'a b'.title() }}", {}),
    'method_strip': ("{{ ' a '.strip() }}|{{ 'xa'.lstrip('x') }}|{{ 'ax'.rstrip('x') }}", {}),
    'method_split': ("{{ 'a,b'.split(',') }} {{ 'a b'.split() }}", {}),
    'method_replace': ("{{ 'abab'.replace('a', 'z') }}", {}),
    'method_startswith': ("{{ 'abc'.startswith('a') }} {{ 'abc'.endswith('x') }}", {}),
    'method_format': ("{{ '{}-{}'.format(1, 2) }}", {}),
    'method_join': ("{{ ', '.join(['a', 'b']) }}", {}),
    'method_find_count': ("{{ 'abca'.find('c') }} {{ 'abca'.count('a') }}", {}),
    'method_dict_get': (
        "{% set d = {'a': 1} %}{{ d.get('a') }} {{ d.get('b', 'dflt') }} {{ d.keys() | list }} {{ d.values() | list }}",
        {},
    ),
    'method_list_append': ('{% set l = [1] %}{% do l.append(2) %}{{ l }}{{ l.pop() }}{{ l }}', {}),
    'method_dict_items_sorted': ("{% for k, v in {'b': 2, 'a': 1}.items() %}{{ k }}{{ v }}{% endfor %}", {}),
    'method_zfill': ("{{ '5'.zfill(3) }}", {}),
    'method_capitalize': ("{{ 'abc'.capitalize() }}", {}),
    'method_isdigit': ("{{ '5'.isdigit() }} {{ 'a'.isalpha() }}", {}),
    'method_unknown': ("{{ 'abc'.not_a_method() }}", {}),
    'subscript_slice': ("{{ 'abcdef'[1:3] }} {{ [1, 2, 3][::-1] }} {{ 'abc'[-1] }}", {}),
    'subscript_dict': ("{% set d = {'a': {'b': 1}} %}{{ d['a']['b'] }}{{ d.a.b }}", {}),
    'subscript_missing': ("{% set d = {'a': 1} %}[{{ d.z }}][{{ d['z'] }}]", {}),
    'subscript_missing_chain': ("{% set d = {'a': 1} %}[{{ d.z.y }}]", {}),
    'subscript_missing_call': ("{% set d = {'a': 1} %}[{{ d.z() }}]", {}),
    'macro_basic': (
        '{% macro m(a, b=2) %}{{ a }}-{{ b }}{% endmacro %}{{ m(1) }} {{ m(1, 3) }} {{ m(b=5, a=0) }}',
        {},
    ),
    'macro_varargs': ('{% macro m() %}{{ varargs }}{{ kwargs }}{% endmacro %}{{ m(1, 2, k=3) }}', {}),
    'macro_recursive': (
        '{% macro fact(n) %}{% if n <= 1 %}1{% else %}{{ n * fact(n - 1) | int }}{% endif %}{% endmacro %}{{ fact(4) }}',
        {},
    ),
    'macro_caller': (
        '{% macro box() %}<{{ caller() }}>{% endmacro %}{% call box() %}content{% endcall %}',
        {},
    ),
    'macro_caller_args': (
        '{% macro each(items) %}{% for i in items %}{{ caller(i) }}{% endfor %}{% endmacro %}{% call(x) each([1, 2]) %}[{{ x }}]{% endcall %}',
        {},
    ),
    'macro_in_macro': (
        '{% macro a() %}A{{ b() }}{% endmacro %}{% macro b() %}B{% endmacro %}{{ a() }}',
        {},
    ),
    'macro_scope_outer_var': ("{% set x = 'outer' %}{% macro m() %}{{ x }}{% endmacro %}{{ m() }}", {}),
    'macro_return_in_model': (
        '{% macro m(x) %}{{ return(x * 2) }}{% endmacro %}{{ m(4) }}',
        {},
    ),
    'macro_returns_list': (
        "{% macro cols() %}{{ return(['a', 'b']) }}{% endmacro %}{% for c in cols() %}{{ c }}{% endfor %}",
        {},
    ),
    'macro_docs_block': ('{% docs foo %}text{% enddocs %}select 1', {}),
    'macro_test_block': ('{% test t(model) %}select 1{% endtest %}select 2', {}),
    'macro_materialization_block': ('{% materialization m, default %}select 1{% endmaterialization %}x', {}),
    'macro_snapshot_block': ('{% snapshot s %}select 1{% endsnapshot %}x', {}),
    'whitespace_minus': ("a  {{- 'b' -}}  c  {%- if true -%}  d  {%- endif -%}  e", {}),
    'whitespace_trim_blocks': ('a\n{% if true %}\nb\n{% endif %}\nc', {}),
    'whitespace_lstrip_only': ('a\n  {%- if true %}\nb\n  {%- endif %}\nc', {}),
    'whitespace_plus': ('a\n{%+ if true %}b{% endif %}\nc', {}),
    'whitespace_trailing_newline': ('select 1\n', {}),
    'whitespace_two_newlines': ('select 1\n\n', {}),
    'whitespace_crlf': ('select 1\r\nfrom t\r\n', {}),
    'whitespace_expression_edges': ("{{ 'a' }}\n{{ 'b' }}\n", {}),
    'raw_block': ('{% raw %}{{ not_rendered }} {% if %}{% endraw %}x', {}),
    'raw_block_trim': ('a {%- raw -%} {{ x }} {%- endraw -%} b', {}),
    'comment_block': ('a{# a comment #}b{#- trimmed -#}c', {}),
    'comment_multiline': ('a{#\nmulti\nline\n#}b', {}),
    'comment_with_jinja': ("a{# {{ ref('x') }} #}b", {}),
    'sql_comment_with_jinja': (
        "select 1 -- {{ ref('stg_orders') }}\n/* {{ source('app', 'events') }} */",
        {},
    ),
    'line_statement_markers_literal': ("select '#' as h -- {# nope\n", {}),
    'jinja_in_string_literal': ('select \'{{ ref("stg_orders") }}\' as r', {}),
    'curly_in_sql': ("select '{' as a, '}' as b, '{x}' as c", {}),
    'percent_in_sql': ("select 5 % 2 as m, 'a%' like 'a%' as l", {}),
    'hash_in_sql': ("select '#x' as h", {}),
    'unicode': ("select 'café ☃' as u, {{ 'naïve' }} as v", {}),
    'tab_indent': ("select\n\t{{ 'a' }},\n\t{{ 'b' }}\nfrom t", {}),
    'escape_in_string': ("{{ 'a\\'b' }} {{ \"c\\\\d\" }} {{ 'tab\\there' }}", {}),
    'autoescape_off': ("{{ '<b>' }} {{ ['<b>'] | join }}", {}),
    'include_nothing': ("select 1 {% include 'x.sql' %}", {}),
    'import_nothing': ("{% import 'x.sql' as x %}select 1", {}),
    'extends_nothing': ("{% extends 'base.sql' %}select 1", {}),
    'with_block': ('{% with a = 1 %}{{ a }}{% endwith %}', {}),
    'filter_block': ('{% filter upper %}abc{% endfilter %}', {}),
    'call_unknown_in_set': ("{% set x = not_defined('a') %}select '{{ x }}'", {}),
    'incremental_typical': (
        "{{ config(materialized='incremental', unique_key='id') }}\n"
        "select id, amount\nfrom {{ source('app', 'events') }}\n"
        '{% if is_incremental() %}\nwhere updated_at > (select max(updated_at) from {{ this }})\n{% endif %}\n',
        {},
    ),
    'typical_staging': (
        "with source as (\n    select * from {{ source('app', 'events') }}\n),\nrenamed as (\n"
        '    select id as event_id, {{ cents_to_dollars("amount") }} as amount\n    from source\n)\nselect * from renamed\n',
        OWN,
    ),
    'typical_pivot': (
        "{%- set methods = ['credit_card', 'bank_transfer'] -%}\nselect\n    order_id,\n"
        '    {% for m in methods -%}\n    sum(case when method = \'{{ m }}\' then amount else 0 end) as {{ m }}_amount{{ "," if not loop.last }}\n'
        "    {% endfor %}\nfrom {{ ref('stg_orders') }}\ngroup by 1\n",
        {},
    ),
    'typical_union': (
        "{% set tables = ['stg_orders', 'customers'] %}{% for t in tables %}select * from {{ ref(t) }}{% if not loop.last %}\nunion all\n{% endif %}{% endfor %}",
        {},
    ),
    'typical_dispatch_package': (
        "select {{ dbt_utils.helper('x') }}, {{ dbt_utils.star(from=ref('stg_orders'), except=['id']) }}",
        PACKAGE,
    ),
    'syntax_error_unclosed_block': ('{% if true %}x', {}),
    'syntax_error_unclosed_expr': ('select {{ 1 + }}', {}),
    'syntax_error_bad_tag': ('{% nonsense %}', {}),
    'syntax_error_unclosed_for': ('{% for x in y %}', {}),
    'syntax_error_stray_end': ('{% endif %}', {}),
    'syntax_error_empty_if': ('{% if %}', {}),
    'runtime_error_type': ("{{ 1 + 'a' }}", {}),
    'runtime_error_divzero': ('{{ 1 / 0 }}', {}),
    'runtime_error_none_call': ('{{ none() }}', {}),
    'runtime_error_int_iter': ('{% for x in 5 %}a{% endfor %}', {}),
    'runtime_error_attr_on_int': ('{{ (1).foo.bar }}x', {}),
    'runtime_error_keyerror': ("{{ {}['a'] }}x", {}),
    'sandbox_private_attr': ("{{ ''.__class__ }}", {}),
    'sandbox_mro': ("{{ ''.__class__.__mro__ }}", {}),
    'sandbox_subclasses': ('{{ ().__class__.__bases__[0].__subclasses__() }}', {}),
    'sandbox_globals': ('{{ ref.__globals__ }}', {}),
    'sandbox_func_code': ('{{ greet.__code__ }}', OWN),
    'sandbox_format_str': ("{{ '{0.__class__}'.format('x') }}", {}),
    'sandbox_format_map': ("{{ '{a.__class__}'.format_map({'a': 1}) }}", {}),
    'sandbox_unsafe_method': ('{% set l = [] %}{{ l.append.__self__ }}', {}),
    'sandbox_cycler': ('{{ cycler.__init__.__globals__ }}', {}),
    'sandbox_import_builtin': ("{{ __import__('os').system('true') }}", {}),
    'sandbox_lipsum': ('{{ lipsum.__globals__ }}', {}),
    'sandbox_range_huge': ('{{ range(100000000000) | list | length }}', {}),
    'sandbox_power_huge': ('{{ 2 ** 100000000 }}', {}),
    'sandbox_string_mul_huge': ("{{ 'a' * 100000000000 | length }}", {}),
    'sandbox_attr_filter': ("{{ ''|attr('__class__') }}", {}),
    'sandbox_getattr_mro': ("{{ ''['__class__'] }}x", {}),
    'sandbox_env_var_leak': ("{{ env_var('HOME') }}", {}),
    'sandbox_os_environ': ('{{ os.environ }}x', {}),
    'sandbox_open_file': ("{{ open('/etc/passwd') }}x", {}),
    'sandbox_dunder_dict_key': ('{{ {}.__class__ }}', {}),
    'sandbox_ref_func_name': ('{{ ref.__name__ }}x', {}),
    'recursion_macro': ('{% macro r() %}{{ r() }}{% endmacro %}{{ r() }}', {}),
    'cycler_joiner': ("{% set c = cycler('a', 'b') %}{{ c.next() }}{{ c.next() }}{{ c.next() }}", {}),
    'joiner': ("{% set j = joiner(', ') %}{% for i in range(3) %}{{ j() }}{{ i }}{% endfor %}", {}),
    'dict_function': ('{{ dict(a=1, b=2) }}', {}),
    'range_function_step': ('{{ range(0, 10, 3) | list }}', {}),
    'lipsum_absent': ('{{ lipsum is defined }}', {}),
    'zip_function': ("{{ zip([1, 2], ['a', 'b']) | list }}", {}),
    'enumerate_function': ("{% for i, x in enumerate(['a', 'b']) %}{{ i }}{{ x }}{% endfor %}", {}),
    'tojson_function': ("{{ tojson({'a': 1}) }}", {}),
    'fromjson_function': ('{{ fromjson(\'{"a": 1}\').a }}', {}),
    'toyaml_function': ("{{ toyaml({'a': 1}) }}", {}),
    'return_function_direct': ('{{ return(3) }}', {}),
    'invocation_id': ("select '{{ invocation_id }}' as i", {}),
    'thread_id': ("select '{{ thread_id }}' as i", {}),
    'flags': ('{% if flags.FULL_REFRESH %}a{% else %}b{% endif %}', {}),
    'model_attr': ("select '{{ model.name }}' as n", {}),
    'graph_attr': ('{% for n in graph.nodes.values() %}{{ n.name }}{% endfor %}select 1', {}),
    'project_name_var': ("select '{{ project_name }}' as p", {}),
    'builtins_ref': ("select {{ builtins.ref('x') }}", {}),
    'context_var': ('{{ context.foo }}', {}),
    'selected_resources': ("{% if 'model.x' in selected_resources %}a{% endif %}b", {}),
    'local_md5': ("select '{{ local_md5('x') }}'", {}),
    'api_relation': ("select {{ api.Relation.create(schema='s', identifier='i') }}", {}),
    'relation_methods': ("select {{ ref('stg_orders').schema }}", {}),
    'ref_attr_call': ("select {{ ref('stg_orders').include(database=false) }}", {}),
    'ref_in_set': ("{% set rel = ref('stg_orders') %}select * from {{ rel }}", {}),
    'ref_in_loop_set': ("{% set rels = [ref('stg_orders'), ref('customers')] %}{{ rels | join(', ') }}", {}),
    'ref_in_ternary': ("select * from {{ ref('stg_orders') if true else ref('customers') }}", {}),
    'ref_dynamic_name': ("{% set n = 'stg_' ~ 'orders' %}select * from {{ ref(n) }}", {}),
    'ref_nested_call': ("select * from {{ ref(var('model', 'stg_orders')) }}", {}),
    'source_dynamic': ("{% for t in ['events'] %}select * from {{ source('app', t) }}{% endfor %}", {}),
}
