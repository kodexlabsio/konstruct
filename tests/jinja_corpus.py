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
    'real_pivot_macro': (
        "{% macro pivot(column, values, agg='sum', then_value=1, else_value=0, prefix='', suffix='') %}\n"
        '{% for v in values %}\n'
        "{{ agg }}(case when {{ column }} = '{{ v }}' then {{ then_value }} else {{ else_value }} end) as {{ adapter.quote(prefix ~ v ~ suffix) }}{% if not loop.last %},{% endif %}\n"
        '{% endfor %}\n'
        "{% endmacro %}select {{ pivot('status', ['a', 'b'], prefix='s_') }} from {{ ref('stg_orders') }}",
        {},
    ),
    'real_union_relations': (
        "{%- set rels = [ref('stg_orders'), ref('customers')] -%}\n"
        '{%- set cols = [] -%}\n'
        "{%- for r in rels -%}{%- do cols.append(r | string | replace('\"', '')) -%}{%- endfor -%}\n"
        "{%- for r in rels -%}select '{{ cols[loop.index0] }}' as src from {{ r }}{{ ' union all ' if not loop.last }}{%- endfor -%}",
        {},
    ),
    'real_get_column_values': (
        "{% set values = [] %}{% if execute %}{% set rs = run_query('select distinct c from t') %}{% set values = rs.columns[0].values() %}{% endif %}\n"
        "select {% for v in values %}'{{ v }}'{% else %}null{% endfor %} as c",
        {},
    ),
    'real_namespace_total': (
        "{% set ns = namespace(found=false, items=[]) %}{% for c in ['a', 'b', 'c'] %}{% if c == 'b' %}{% set ns.found = true %}{% endif %}{% do ns.items.append(c | upper) %}{% endfor %}{{ ns.found }} {{ ns.items }}",
        {},
    ),
    'real_dict_building': (
        "{% set cfg = {} %}{% for k in ['a', 'b'] %}{% do cfg.update({k: k ~ '_v'}) %}{% endfor %}{% for k, v in cfg.items() %}{{ k }}={{ v }};{% endfor %}{{ cfg | length }}",
        {},
    ),
    'real_kwargs_macro': (
        '{% macro m(a, b=2) %}{{ a }}|{{ b }}|{{ kwargs }}|{{ varargs }}{% endmacro %}{{ m(1, 3, 4, x=5) }}',
        {},
    ),
    'real_kwargs_unknown': ('{% macro m(a) %}{{ a }}{% endmacro %}{{ m(1, z=2) }}', {}),
    'real_too_many_args': ('{% macro m(a) %}{{ a }}{% endmacro %}{{ m(1, 2) }}', {}),
    'real_missing_arg': ('{% macro m(a, b) %}[{{ a }}][{{ b }}]{% endmacro %}{{ m(1) }}', {}),
    'real_dup_arg': ('{% macro m(a) %}{{ a }}{% endmacro %}{{ m(1, a=2) }}', {}),
    'real_default_uses_other': ("{% macro m(a, b=a ~ '!') %}{{ b }}{% endmacro %}{{ m('x') }}", {}),
    'real_call_nested': (
        "{% macro w(t) %}<{{ t }}>{{ caller() }}</{{ t }}>{% endmacro %}{% call w('a') %}{% call w('b') %}x{% endcall %}{% endcall %}",
        {},
    ),
    'real_caller_with_args': (
        "{% macro lst(items) %}{% for i in items %}{{ caller(i, loop.index) }}{% endfor %}{% endmacro %}{% call(item, n) lst(['p', 'q']) %}{{ n }}:{{ item }} {% endcall %}",
        {},
    ),
    'real_star_args': (
        "{% macro m(a, b, c) %}{{ a }}{{ b }}{{ c }}{% endmacro %}{{ m(*[1, 2, 3]) }}{{ m(**{'a': 4, 'b': 5, 'c': 6}) }}",
        {},
    ),
    'scope_set_in_for_not_visible': ('{% for i in range(2) %}{% set seen = i %}{% endfor %}[{{ seen }}]', {}),
    'scope_set_in_if_visible': ('{% if true %}{% set seen = 1 %}{% endif %}[{{ seen }}]', {}),
    'scope_with': ('{% set a = 1 %}{% with a = 2 %}{{ a }}{% endwith %}{{ a }}', {}),
    'scope_macro_local': (
        '{% macro m() %}{% set inner = 5 %}{{ inner }}{% endmacro %}{{ m() }}[{{ inner }}]',
        {},
    ),
    'scope_loop_var_leak': ('{% for i in range(2) %}{% endfor %}[{{ i }}]', {}),
    'scope_loop_in_macro_closure': (
        '{% for i in range(2) %}{% macro m() %}{{ i }}{% endmacro %}{{ m() }}{% endfor %}',
        {},
    ),
    'scope_shadow_global': ('{% set range = 3 %}{{ range }}', {}),
    'scope_shadow_ref': ("{% set ref = 'x' %}{{ ref }}", {}),
    'alias_list_mutation': ('{% set a = [1] %}{% set b = a %}{% do b.append(2) %}{{ a }}{{ b }}', {}),
    'alias_dict_mutation': ('{% set a = {} %}{% set b = a %}{% do b.update(x=1) %}{{ a }}', {}),
    'list_methods': (
        '{% set l = [3, 1, 2] %}{% do l.sort() %}{{ l }}{% do l.reverse() %}{{ l }}{% do l.insert(0, 9) %}{{ l }}{{ l.pop(0) }}{{ l.index(2) }}{{ l.count(1) }}{% do l.extend([7, 7]) %}{% do l.remove(7) %}{{ l }}',
        {},
    ),
    'list_pop_empty': ('{% set l = [] %}{{ l.pop() }}', {}),
    'list_ops': (
        '{{ [1, 2] + [3] }}{{ [0] * 3 }}{{ [1, 2, 3][1:] }}{{ [1, 2, 3][-1] }}{{ [1, 2, 3][5] }}x',
        {},
    ),
    'list_equality': ('{{ [1, 2] == [1, 2] }}{{ [1] == [2] }}{{ [1, 2] < [1, 3] }}', {}),
    'list_in': ("{{ 2 in [1, 2] }}{{ 3 not in [1, 2] }}{{ 'a' in 'abc' }}{{ 'z' in 'abc' }}", {}),
    'dict_methods': (
        "{% set d = {'a': 1} %}{{ d.get('a') }}{{ d.get('z') }}{{ d.get('z', 0) }}{{ d.setdefault('b', 2) }}{{ d }}{{ d.pop('a') }}{{ d }}{{ d.keys() | list }}{{ d.items() | list }}",
        {},
    ),
    'dict_in_and_len': (
        "{% set d = {'a': 1, 'b': 2} %}{{ 'a' in d }}{{ 'z' in d }}{{ d | length }}{{ d | list }}",
        {},
    ),
    'dict_equality': ("{{ {'a': 1} == {'a': 1} }}{{ {'a': 1} == {'a': 2} }}", {}),
    'dict_int_keys': ("{% set d = {1: 'a', 2: 'b'} %}{{ d[1] }}{{ d[2] }}{{ d }}", {}),
    'dict_nested': ("{% set d = {'a': {'b': [1, {'c': 'deep'}]}} %}{{ d.a.b[1].c }}{{ d['a']['b'][0] }}", {}),
    'dict_sorted_loop': ("{% for k, v in {'b': 1, 'a': 2} | dictsort %}{{ k }}{{ v }}{% endfor %}", {}),
    'dict_key_method_name': ("{% set d = {'items': 5} %}{{ d.items() | list }}x{{ d['items'] }}", {}),
    'dict_values_sum': ("{{ {'a': 1, 'b': 2}.values() | sum }}", {}),
    'dict_update_kwargs': ('{% set d = {} %}{% do d.update(a=1, b=2) %}{{ d }}', {}),
    'dict_from_pairs': ("{{ dict([('a', 1), ('b', 2)]) }}{{ dict({'x': 1}, y=2) }}", {}),
    'tuple_unpack_for': ('{% for a, b in [(1, 2), (3, 4)] %}{{ a + b }}{% endfor %}', {}),
    'tuple_unpack_nested': ("{% for k, (a, b) in [('x', (1, 2))] %}{{ k }}{{ a }}{{ b }}{% endfor %}", {}),
    'tuple_unpack_wrong_count': ('{% for a, b in [(1, 2, 3)] %}{{ a }}{% endfor %}', {}),
    'tuple_set_swap': ('{% set a, b = [1, 2] %}{% set a, b = b, a %}{{ a }}{{ b }}', {}),
    'num_floor_division': (
        '{{ -7 // 2 }} {{ 7 // -2 }} {{ -7 % 3 }} {{ 7 % -3 }} {{ 7.5 // 2 }} {{ -7.5 % 2 }}',
        {},
    ),
    'num_division': ('{{ 1 / 4 }} {{ 10 / 2 }} {{ 2 ** -1 }} {{ 2 ** 0.5 }} {{ 3 * 1.5 }}', {}),
    'num_bools': ('{{ true + true }} {{ true * 5 }} {{ true == 1 }} {{ false == 0 }}', {}),
    'num_big': ('{{ 2 ** 64 }} {{ 10 ** 30 }} {{ 123456789 * 987654321 }}', {}),
    'num_float_forms': (
        '{{ 1e22 }} {{ 0.00001 }} {{ 123456789.0 * 10 }} {{ 1e16 }} {{ 1e15 }} {{ 0.0001 }} {{ -0.0 }} {{ 1.5e300 }}',
        {},
    ),
    'num_float_arith': ('{{ 0.1 + 0.2 }} {{ 1.1 * 3 }} {{ 100 * 1.1 }} {{ 5 / 3 }}', {}),
    'num_round_half_even': (
        '{{ 2.5 | round }} {{ 3.5 | round }} {{ 0.125 | round(2) }} {{ 2.675 | round(2) }} {{ 1234 | round(-2) }} {{ 5 | round }}',
        {},
    ),
    'num_round_methods': (
        "{{ 2.1 | round(0, 'ceil') }} {{ 2.9 | round(0, 'floor') }} {{ 2.567 | round(1, 'ceil') }}",
        {},
    ),
    'num_int_float_filters': (
        "{{ '12' | int }} {{ '1_0' | int }} {{ 'x' | int }} {{ 'x' | int(7) }} {{ '3.9' | int }} {{ 3.9 | int }} {{ '2' | float }} {{ 'y' | float }} {{ none | int }} {{ true | int }}",
        {},
    ),
    'num_int_literals': ('{{ 0x1F }} {{ 0b101 }} {{ 0o17 }} {{ 1_000 }}', {}),
    'num_compare_mixed': ("{{ 1 == 1.0 }} {{ 1 < 1.5 }} {{ 2 > true }} {{ 'a' < 'b' }}", {}),
    'num_compare_error': ("{{ 1 < 'a' }}", {}),
    'num_abs_neg': ('{{ -3 | abs }} {{ -2.5 | abs }} {{ -(3) }} {{ - 3 }}', {}),
    'num_unary_pow': ('{{ -2 ** 2 }} {{ 2 ** 3 ** 2 }}', {}),
    'op_chained_compare': ('{{ 1 < 2 < 3 }} {{ 1 < 3 < 2 }} {{ 1 == 1 == 1 }}', {}),
    'op_and_or_values': (
        "{{ 'a' and 'b' }} {{ '' and 'b' }} {{ '' or 'c' }} {{ 0 or none }} {{ 'x' or 'y' }}",
        {},
    ),
    'op_not_precedence': ('{{ not 1 == 2 }} {{ not true and false }} {{ not (true and false) }}', {}),
    'op_nested_ternary': (
        "{{ 'a' if false else 'b' if false else 'c' }}{{ 'x' if true else 'y' if false }}",
        {},
    ),
    'op_concat_precedence': ("{{ 1 + 2 ~ 3 }} {{ 'a' ~ 1 + 2 }}", {}),
    'op_concat_none': ("{{ none ~ 'x' }}{{ true ~ 1 }}{{ [1] ~ 'a' }}", {}),
    'op_filter_precedence': ("{{ 1 + 2 | string }}{{ 'a' ~ 'b' | upper }}{{ -3 | abs + 1 }}", {}),
    'op_is_not': ('{{ 1 is not none }}{{ 1 is not string }}{{ x is not defined }}', {}),
    'op_is_in_expressions': (
        "{% if 3 is in([1, 2, 3]) %}y{% endif %}{% if 'a' is in 'abc' %}y{% endif %}",
        {},
    ),
    'op_is_eq_args': (
        '{{ 3 is eq(3) }}{{ 3 is ne 3 }}{{ 4 is divisibleby(2) }}{{ 3 is lt 4 }}{{ 3 is ge 4 }}',
        {},
    ),
    'op_is_sameas': ('{{ none is sameas none }}{{ true is sameas false }}', {}),
    'op_is_odd_even': ('{{ 3 is odd }}{{ 4 is even }}{{ 3 is even }}', {}),
    'op_is_upper_lower': ("{{ 'A' is upper }}{{ 'a' is lower }}{{ 'Ab' is upper }}", {}),
    'op_is_callable': (
        "{% macro m() %}{% endmacro %}{{ m is callable }}{{ 'x' is callable }}{{ ref is callable }}",
        {},
    ),
    'op_is_iterable': (
        "{{ [1] is iterable }}{{ 1 is iterable }}{{ 'a' is iterable }}{{ {} is iterable }}",
        {},
    ),
    'op_is_float_integer': ('{{ 1 is integer }}{{ 1.0 is float }}{{ 1 is float }}{{ true is integer }}', {}),
    'op_is_boolean_tests': ('{{ true is true }}{{ false is false }}{{ 1 is true }}{{ 0 is false }}', {}),
    'slice_cases': (
        "{{ 'abcdef'[::2] }} {{ 'abcdef'[::-1] }} {{ 'abcdef'[-3:] }} {{ 'abcdef'[1:-1] }} {{ [1,2,3,4][1:3] }} {{ [1,2,3][10:] }} {{ 'abc'[:1] }}",
        {},
    ),
    'slice_string_idx': ("{{ 'abc'[0] }}{{ 'abc'[2] }}{{ 'abc'[3] }}x", {}),
    'attr_on_none': ('[{{ none.foo }}][{{ none.foo.bar }}]', {}),
    'attr_on_number': ('[{{ (5).foo }}][{{ (5.5).foo }}]', {}),
    'attr_on_string_unknown': ("[{{ 'a'.nope }}][{{ 'a'.nope() }}][{{ 'a'.nope(1, 'x') }}]", {}),
    'attr_on_list_unknown': ('[{{ [1].nope }}][{{ [1].nope(2) }}]', {}),
    'attr_dunder_dict': ("[{{ {'a': 1}.__len__ }}][{{ {'__x': 1}.__x }}][{{ {'__x': 1}['__x'] }}]", {}),
    'attr_dunder_call': ("[{{ ''.__class__() }}][{{ 'a'.__len__() }}]", {}),
    'undefined_arith': ('{{ nothing + 1 }}', {}),
    'undefined_compare': ('{{ nothing < 1 }}', {}),
    'undefined_eq': ('[{{ nothing == nothing }}][{{ nothing == 1 }}][{{ nothing == none }}]', {}),
    'undefined_concat': ("[{{ nothing ~ 'x' }}][{{ 'x' ~ nothing }}]", {}),
    'undefined_iterate_items': ('{% for k in nothing.items() %}x{% endfor %}y', {}),
    'undefined_in': ('[{{ 1 in nothing }}]', {}),
    'undefined_len': ("{{ nothing | length }}{{ nothing | list }}{{ nothing | join(',') }}|", {}),
    'undefined_default_chain': ("{{ nothing.a.b | default('z') }}", {}),
    'undefined_is_tests': (
        '{{ nothing is defined }}{{ nothing is undefined }}{{ nothing is none }}{{ nothing is string }}',
        {},
    ),
    'undefined_filters': (
        '[{{ nothing | upper }}][{{ nothing | string }}][{{ nothing | int }}][{{ nothing | first }}]',
        {},
    ),
    'undefined_macro_arg': ('{% macro m(a) %}[{{ a }}]{{ a is defined }}{% endmacro %}{{ m() }}', {}),
    'undefined_call_in_filter': ("{{ nothing('x') | upper }}", {}),
    'undefined_as_dict_key': ('{% set d = {nothing: 1} %}{{ d }}', {}),
    'undefined_not': ('{{ not nothing }}', {}),
    'undefined_bool_ops': ("{{ nothing and 1 }}|{{ nothing or 'o' }}", {}),
    'strfmt_basic': (
        "{{ '{}-{}'.format('a', 'b') }}{{ '{0}{1}{0}'.format(1, 2) }}{{ '{n}'.format(n='x') }}",
        {},
    ),
    'strfmt_spec': (
        "{{ '{:>5}'.format('a') }}|{{ '{:<5}'.format('a') }}|{{ '{:^5}'.format('a') }}|{{ '{:05.2f}'.format(3.14159) }}|{{ '{:,}'.format(1234567) }}|{{ '{:*>4}'.format(7) }}|{{ '{:d}'.format(5) }}|{{ '{:x}'.format(255) }}|{{ '{:+d}'.format(5) }}",
        {},
    ),
    'strfmt_conv': ("{{ '{!r}'.format('a') }}{{ '{!s}'.format(1) }}{{ '{{literal}}'.format() }}", {}),
    'strfmt_attr_index': (
        "{{ '{0[1]}'.format([5, 6]) }}{{ '{0[k]}'.format({'k': 'v'}) }}{{ '{0.grouper}'.format('x') }}",
        {},
    ),
    'strfmt_errors': ("{{ '{}'.format() }}", {}),
    'percent_basic': (
        "{{ '%s=%d' % ('a', 1) }}{{ '%05.1f' % 3.14159 }}{{ '%(a)s' % {'a': 1} }}{{ '%s' % 'x' }}{{ '%5s|%-5s|' % ('a', 'b') }}{{ '%%' % () }}",
        {},
    ),
    'percent_repr_hex': ("{{ '%r' % 'a' }}{{ '%x' % 255 }}{{ '%03d' % 7 }}{{ '%.3s' % 'abcdef' }}", {}),
    'str_methods_more': (
        "{{ 'a-b-c'.split('-', 1) }}{{ 'a-b-c'.rsplit('-', 1) }}{{ ' a b '.split(' ') }}{{ 'abc'.partition('b') }}{{ 'abc'.rpartition('x') }}{{ 'ab'.ljust(4, '.') }}{{ 'ab'.rjust(4) }}{{ 'ab'.center(5, '*') }}{{ 'abc'.removeprefix('a') }}{{ 'abc'.removesuffix('c') }}{{ 'aXb'.swapcase() }}{{ 'a\\nb'.splitlines() }}{{ 'hello world'.title() }}{{ 'abc'.index('c') }}{{ 'abcabc'.rfind('b') }}{{ 'aaa'.count('a') }}",
        {},
    ),
    'str_methods_is': (
        "{{ 'abc'.isalpha() }}{{ 'a1'.isalnum() }}{{ ' '.isspace() }}{{ ''.isdigit() }}{{ 'ABC'.isupper() }}{{ 'abc'.islower() }}{{ 'é'.isalpha() }}",
        {},
    ),
    'str_join_errors': ("{{ ','.join([1, 2]) }}", {}),
    'str_join_dict': ("{{ ','.join({'a': 1, 'b': 2}) }}", {}),
    'str_in_loop_methods': ("{% for w in 'a b c'.split() %}{{ w | upper }}{% endfor %}", {}),
    'str_startswith_tuple': ("{{ 'abc'.startswith(('x', 'a')) }}{{ 'abc'.endswith(('c',)) }}", {}),
    'str_multiply': ("{{ 'ab' * 3 }}{{ 3 * 'ab' }}{{ 'a' * 0 }}{{ 'a' * -1 }}|", {}),
    'str_escapes': ("{{ '\\x41\\u00e9\\n' | length }}{{ 'a\\\\b' }}{{ '\\q' }}", {}),
    'str_adjacent_literals': ("{{ 'a' 'b' \"c\" }}", {}),
    'str_unicode_filters': (
        "{{ 'straße'.upper() }}{{ 'ÀÉ' | lower }}{{ 'éa' | capitalize }}{{ 'ǆ' | upper }}",
        {},
    ),
    'filter_chain_args': (
        "{{ ['b', 'a', 'C'] | sort | join('-') }}{{ ['b', 'a', 'C'] | sort(case_sensitive=true) | join('-') }}{{ ['b', 'a'] | sort(reverse=true) | join }}",
        {},
    ),
    'filter_sort_attr': (
        "{% set rows = [{'n': 2, 'k': 'b'}, {'n': 1, 'k': 'a'}] %}{{ rows | sort(attribute='n') | map(attribute='k') | join }}{{ rows | map(attribute='n') | max }}{{ rows | sum(attribute='n') }}",
        {},
    ),
    'filter_selectattr_tests': (
        "{% set rows = [{'n': 2}, {'n': 5}, {'n': 8}] %}{{ rows | selectattr('n', 'gt', 3) | map(attribute='n') | list }}{{ rows | rejectattr('n', 'equalto', 5) | map(attribute='n') | list }}{{ rows | selectattr('n', 'odd') | list | length }}",
        {},
    ),
    'filter_select_reject_plain': (
        "{{ [0, 1, '', 'a', none] | select | list }}{{ [0, 1, '', 'a', none] | reject | list }}{{ [1, 2, 3, 4] | select('even') | list }}{{ ['a', 'B'] | select('lower') | list }}",
        {},
    ),
    'filter_map_with_args': (
        "{{ ['a', 'b'] | map('replace', 'a', 'z') | list }}{{ [1, 2] | map('string') | join('+') }}{{ [[1, 2], [3]] | map('length') | list }}",
        {},
    ),
    'filter_groupby_attr': (
        "{% for g in [{'k': 'a', 'v': 1}, {'k': 'b', 'v': 2}, {'k': 'a', 'v': 3}] | groupby('k') %}{{ g.grouper }}:{% for r in g.list %}{{ r.v }}{% endfor %} {% endfor %}",
        {},
    ),
    'filter_groupby_unpack': (
        "{% for key, items in [{'k': 1}, {'k': 2}, {'k': 1}] | groupby('k') %}{{ key }}={{ items | length }};{% endfor %}",
        {},
    ),
    'filter_unique_attr': (
        "{{ [{'a': 1}, {'a': 1}, {'a': 2}] | unique(attribute='a') | list | length }}{{ ['a', 'A', 'b'] | unique | list }}{{ ['a', 'A'] | unique(case_sensitive=true) | list }}",
        {},
    ),
    'filter_batch_fill': ("{{ [1, 2, 3] | batch(2, 'x') | list }}", {}),
    'filter_slice': ('{{ [1, 2, 3, 4, 5] | slice(2) | list }}', {}),
    'filter_first_last_empty': (
        "[{{ [] | first }}][{{ [] | last }}][{{ '' | first }}][{{ 'abc' | last }}]",
        {},
    ),
    'filter_min_max_empty': ("[{{ [] | min }}][{{ ['b', 'A'] | min }}][{{ [3, 9, 1] | max }}]", {}),
    'filter_sum_start': ('{{ [1, 2] | sum(start=10) }}{{ [0.5, 0.25] | sum }}{{ [] | sum }}', {}),
    'filter_join_attr': ("{{ [{'a': 'x'}, {'a': 'y'}] | join(',', attribute='a') }}", {}),
    'filter_join_none_items': ("{{ [1, none, 'a', true] | join('|') }}", {}),
    'filter_default_boolean': (
        "{{ '' | default('d') }}|{{ '' | default('d', true) }}|{{ 0 | default('d', true) }}|{{ false | default('d', boolean=true) }}|{{ x | d('q') }}",
        {},
    ),
    'filter_replace_count': ("{{ 'aaa' | replace('a', 'b', 2) }}{{ 'aaa' | replace('a', '') }}", {}),
    'filter_trim_chars2': ("{{ '  x  ' | trim }}|{{ '--x--' | trim('-') }}|", {}),
    'filter_title_cases': (
        "{{ 'hello wORLD-foo_bar (baz)' | title }}|{{ 'a  b' | title }}|{{ \"it's\" | title }}",
        {},
    ),
    'filter_capitalize': ("{{ 'hELLO wORLD' | capitalize }}{{ '' | capitalize }}|", {}),
    'filter_indent_variants': (
        "{{ 'a\\nb\\n\\nc' | indent(2) }}|{{ 'a\\nb' | indent(2, true) }}|{{ 'a\\n\\nb' | indent(2, false, true) }}|{{ 'a\\nb' | indent('--') }}",
        {},
    ),
    'filter_truncate_variants': (
        "{{ 'hello world foo bar' | truncate(10) }}|{{ 'hello world foo bar' | truncate(10, true) }}|{{ 'hello world foo bar baz qux' | truncate(12, false, '..', 0) }}|{{ 'short' | truncate(10) }}",
        {},
    ),
    'filter_center_wordcount': (
        "{{ 'ab' | center(7) }}|{{ 'abc' | center(8) }}|{{ 'a b  c' | wordcount }}|{{ 'x_y z' | wordcount }}",
        {},
    ),
    'filter_urlencode_dict': ("{{ {'a': 'x y', 'b': 'é'} | urlencode }}|{{ 'a/b c' | urlencode }}", {}),
    'filter_tojson_nested': ("{{ {'b': [1, 2.5, true, none], 'a': 'é<>&\\'\"'} | tojson }}", {}),
    'filter_format_pct': ("{{ '%s and %s' | format('a', 'b') }}{{ '%d%%' | format(50) }}", {}),
    'filter_striptags_escape': ("{{ '<b>hi</b>  there' | striptags }}{{ '\"q\" & <x>' | e }}", {}),
    'filter_dictsort_opts': (
        "{{ {'b': 1, 'A': 2, 'c': 0} | dictsort }}{{ {'b': 1, 'A': 2} | dictsort(true) }}{{ {'b': 1, 'a': 2} | dictsort(by='value', reverse=true) }}",
        {},
    ),
    'filter_items_list': ("{{ {'a': 1} | items | list }}{{ [] | list }}{{ (1, 2) | list }}", {}),
    'filter_pprint_attr': ("{{ {'a': 1} | pprint }}{{ [1, 'a'] | string }}", {}),
    'filter_length_types': (
        "{{ 'abc' | length }}{{ [1, 2] | length }}{{ {'a': 1} | length }}{{ range(5) | length }}{{ [1, 2] | count }}",
        {},
    ),
    'filter_length_int': ('{{ 5 | length }}', {}),
    'filter_reverse_forms': ("{{ 'abc' | reverse }}{{ [1, 2, 3] | reverse | list }}", {}),
    'filter_range_list': (
        '{{ range(3) | list }}{{ range(2, 8, 3) | list }}{{ range(5, 0, -2) | list }}{{ range(0) | list }}',
        {},
    ),
    'filter_range_print': ('{{ range(3) }}', {}),
    'filter_in_for_expr': (
        '{% for x in [3, 1, 2] | sort %}{{ x }}{% endfor %}{% for x in range(3) | reverse %}{{ x }}{% endfor %}',
        {},
    ),
    'filter_block_chain': ("{% filter upper | replace('A', '_') %}banana{% endfilter %}", {}),
    'filter_block_args': ('{% filter indent(2, true) %}a\nb{% endfilter %}', {}),
    'filter_set_block': ('{% set x | upper %}shout{% endset %}{{ x }}', {}),
    'loop_changed': ("{% for x in [1, 1, 2, 2, 3] %}{{ 'c' if loop.changed(x) else 's' }}{% endfor %}", {}),
    'loop_nextitem': ('{% for x in [1, 2, 3] %}{{ loop.nextitem }}|{% endfor %}', {}),
    'loop_depth': (
        '{% for x in [1] %}{% for y in [2] %}{{ loop.depth }}{{ loop.depth0 }}{% endfor %}{% endfor %}',
        {},
    ),
    'loop_inner_shadow': (
        "{% for x in [1, 2] %}{% for y in ['a'] %}{{ loop.length }}{% endfor %}{{ loop.length }}{% endfor %}",
        {},
    ),
    'loop_over_string': ("{% for c in 'abc' %}{{ c }}-{% endfor %}", {}),
    'loop_over_dict_keys': ("{% for k in {'a': 1, 'b': 2} %}{{ k }}{% endfor %}", {}),
    'loop_with_else_filter': ('{% for x in [1, 2] if x > 5 %}{{ x }}{% else %}none{% endfor %}', {}),
    'loop_filter_length': (
        '{% for x in range(10) if x % 3 == 0 %}{{ loop.length }}{{ loop.last }} {% endfor %}',
        {},
    ),
    'loop_non_iterable': ('{% for x in 5 %}a{% endfor %}', {}),
    'loop_none_iter': ('{% for x in none %}a{% endfor %}b', {}),
    'loop_mutation_during': (
        '{% set l = [1, 2] %}{% for x in l %}{% if loop.first %}{% do l.append(3) %}{% endif %}{{ x }}{% endfor %}',
        {},
    ),
    'loop_set_persists_in_iteration': (
        '{% for i in range(3) %}{% if loop.first %}{% set acc = [] %}{% endif %}{% do acc.append(i) %}{% endfor %}x',
        {},
    ),
    'ws_lstrip_multi': ('a\n\n   {%- if true %}b{% endif %}', {}),
    'ws_rstrip_multi': ('{% if true -%}\n\n   b{% endif %}', {}),
    'ws_comment_strip': ('a  {#- c -#}  b', {}),
    'ws_expression_strip': ('a  {{- 1 -}}  b', {}),
    'ws_between_blocks': ('{% if true %} {% endif %} x', {}),
    'ws_raw_with_trim': ('a\n  {%- raw %} {{ x }} {% endraw -%}\n  b', {}),
    'ws_only_whitespace': ('   \n  ', {}),
    'ws_trailing_in_macro': ('{% macro m() %}\n  x\n{% endmacro %}[{{ m() }}]', {}),
    'ws_crlf_expression': ('{{ 1 +\r\n 2 }}\r\nx', {}),
    'ws_cr_only': ('a\rb', {}),
    'ws_trailing_newline_after_tag': ('{% if true %}x{% endif %}\n', {}),
    'ws_plus_after_tag': ('{% if true +%}\nx{% endif %}', {}),
    'syntax_unterminated_string': ("{{ 'abc }}", {}),
    'syntax_unterminated_comment': ('{# never closed', {}),
    'syntax_bad_char': ('{{ 1 $ 2 }}', {}),
    'syntax_missing_endraw': ('{% raw %}abc', {}),
    'syntax_else_without_if': ('{% else %}', {}),
    'syntax_elif_after_else': ('{% if a %}x{% else %}y{% elif b %}z{% endif %}', {}),
    'syntax_set_no_target': ('{% set = 1 %}', {}),
    'syntax_macro_no_name': ('{% macro %}{% endmacro %}', {}),
    'syntax_nested_depth': ('{{ ' + '(' * 200 + '1' + ')' * 200 + ' }}', {}),
    'syntax_unknown_filter_in_dead_branch': ('{% if false %}{{ 1 | nope }}{% endif %}x', {}),
    'syntax_unknown_test_in_dead_branch': ('{% if false %}{{ 1 is nope }}{% endif %}x', {}),
    'syntax_dead_branch_runtime_error': ('{% if false %}{{ 1 / 0 }}{% endif %}x', {}),
    'syntax_loopcontrols_off': ('{% for i in range(3) %}{% break %}{% endfor %}', {}),
    'syntax_continue_off': ('{% for i in range(3) %}{% continue %}{% endfor %}', {}),
    'syntax_import_in_dead_branch': ("{% if false %}{% import 'x' as y %}{% endif %}ok", {}),
    'syntax_block_inline': ('a{% block b %}B{% endblock %}c{% block d %}D{% endblock d %}', {}),
    'syntax_print_tag': ('{% print 1 %}', {}),
    'syntax_set_attr_non_namespace': ('{% set d = {} %}{% set d.x = 1 %}', {}),
    'syntax_subscript_comma': ('{{ [[1, 2]][0, ] }}x', {}),
    'macro_scope_sees_later_set': (
        "{% macro m() %}{{ later }}{% endmacro %}{% set later = 'L' %}{{ m() }}",
        {},
    ),
    'macro_does_not_see_caller_scope': (
        "{% macro m() %}[{{ outer }}]{% endmacro %}{% for outer in ['o'] %}{{ m() }}{% endfor %}",
        {},
    ),
    'macro_redefined': ('{% macro m() %}1{% endmacro %}{% macro m() %}2{% endmacro %}{{ m() }}', {}),
    'macro_returns_text_len': ('{% macro m() %}abc{% endmacro %}{{ m() | length }}{{ m() | upper }}', {}),
    'macro_as_value': (
        '{% macro m(x) %}<{{ x }}>{% endmacro %}{% set f = m %}{{ f(1) }}{{ [m] | length }}',
        {},
    ),
    'macro_in_dict': ("{% macro m() %}M{% endmacro %}{% set d = {'f': m} %}{{ d.f() }}{{ d['f']() }}", {}),
    'macro_repr_print': ('{% macro m() %}{% endmacro %}{{ m }}', {}),
    'macro_caller_repr': ('{% macro m() %}{{ caller }}{% endmacro %}{% call m() %}x{% endcall %}', {}),
    'macro_name_attrs': ('{% macro m(a, b) %}{% endmacro %}{{ m.name }}{{ m.arguments }}x', {}),
    'macro_deep_recursion': (
        '{% macro r(n) %}{% if n > 0 %}{{ r(n - 1) }}{% endif %}x{% endmacro %}{{ r(40) | length }}',
        {},
    ),
    'macro_too_deep_recursion': (
        '{% macro r(n) %}{% if n > 0 %}{{ r(n - 1) }}{% endif %}x{% endmacro %}{{ r(5000) | length }}',
        {},
    ),
    'macro_using_global_dbt': ('{% macro m() %}{{ dbt.type_string() }}{% endmacro %}{{ m() }}', {}),
    'macro_using_ref_inside': ("{% macro m(n) %}{{ ref(n) }}{% endmacro %}{{ m('stg_orders') }}", {}),
    'pkg_macro_sees_only_base': (
        "select {{ pkg_cross('a') }}",
        {
            'dbt_packages/one/macros/a.sql': '{% macro pkg_cross(x) %}{{ other_pkg_macro(x) }}{% endmacro %}',
            'dbt_packages/two/macros/b.sql': '{% macro other_pkg_macro(x) %}OTHER({{ x }}){% endmacro %}',
        },
    ),
    'pkg_namespace_missing': (
        "select {{ one.nope('a') }}|{{ one.here() }}",
        {
            'dbt_packages/one/macros/a.sql': '{% macro here() %}H{% endmacro %}',
        },
    ),
    'pkg_first_wins': (
        'select {{ dup() }}',
        {
            'dbt_packages/aaa/macros/a.sql': '{% macro dup() %}A{% endmacro %}',
            'dbt_packages/bbb/macros/b.sql': '{% macro dup() %}B{% endmacro %}',
        },
    ),
    'pkg_own_overrides': (
        'select {{ dup() }}|{{ aaa.dup() }}',
        {
            'dbt_packages/aaa/macros/a.sql': '{% macro dup() %}A{% endmacro %}',
            'macros/own.sql': '{% macro dup() %}OWN{% endmacro %}',
        },
    ),
    'pkg_dispatch_cross_package': (
        'select {{ go() }}',
        {
            'dbt_packages/aaa/macros/a.sql': "{% macro go() %}{{ adapter.dispatch('impl')() }}{% endmacro %}",
            'dbt_packages/bbb/macros/b.sql': '{% macro duckdb__impl() %}B-duck{% endmacro %}{% macro default__impl() %}B-default{% endmacro %}',
        },
    ),
    'pkg_dispatch_prefers_adapter': (
        'select {{ go() }}',
        {
            'macros/own.sql': "{% macro go() %}{{ adapter.dispatch('f')() }}{% endmacro %}{% macro default__f() %}D{% endmacro %}{% macro duckdb__f() %}U{% endmacro %}",
        },
    ),
    'pkg_dispatch_plain_name': (
        'select {{ go() }}',
        {
            'macros/own.sql': "{% macro go() %}{{ adapter.dispatch('f')() }}{% endmacro %}{% macro f() %}PLAIN{% endmacro %}",
        },
    ),
    'pkg_dispatch_missing': (
        'select {{ go() }}',
        {
            'macros/own.sql': "{% macro go() %}{{ adapter.dispatch('nope')(1, 2) }}{% endmacro %}",
        },
    ),
    'pkg_dispatch_with_namespace_arg': (
        'select {{ go() }}',
        {
            'macros/own.sql': "{% macro go() %}{{ adapter.dispatch('f', 'dbt_utils')(1) }}{% endmacro %}{% macro default__f(x) %}F{{ x }}{% endmacro %}",
        },
    ),
    'pkg_top_level_set': (
        'select {{ later_macro() }}',
        {
            'macros/own.sql': "{% set _hidden = 1 %}{% macro later_macro() %}{{ _hidden }}|{{ shared }}{% endmacro %}{% set shared = 's' %}",
        },
    ),
    'pkg_empty_macro_files': ('select {{ x(1) }}', {'macros/a.sql': '', 'macros/b.sql': '   \n'}),
    'pkg_macro_project_namespace': (
        'select {{ shop.m() }}|{{ other.m() }}',
        {
            'macros/a.sql': '{% macro m() %}M{% endmacro %}',
        },
    ),
    'pkg_namespace_named_like_macro': (
        'select {{ dbt_utils.here() }}',
        {
            'dbt_packages/dbt_utils/macros/a.sql': '{% macro here() %}H{% endmacro %}',
        },
    ),
    'pkg_overrides_dbt_builtin_name': (
        "select {{ dbt.date_trunc('day', 'x') }}|{{ dbt.mine() }}",
        {
            'dbt_packages/dbt/macros/a.sql': '{% macro mine() %}MINE{% endmacro %}',
        },
    ),
    'pkg_macro_name_shadows_global': (
        "select {{ ref('x') }}|{{ var('y') }}",
        {
            'macros/a.sql': '{% macro ref(n) %}REF({{ n }}){% endmacro %}',
        },
    ),
    'pkg_macro_uses_loop_and_set': (
        "select {{ cols_sql(['a', 'b']) }}",
        {
            'macros/a.sql': "{% macro cols_sql(cs) %}{%- set out = [] -%}{%- for c in cs -%}{%- do out.append(c ~ '_x') -%}{%- endfor -%}{{ out | join(', ') }}{% endmacro %}",
        },
    ),
    'pkg_macro_returns_via_return': (
        '{% set cols = get_cols() %}{{ cols }}|{{ cols | length }}',
        {
            'macros/a.sql': "{% macro get_cols() %}{{ return(['a', 'b']) }}{% endmacro %}",
        },
    ),
    'pkg_macro_with_docs_block_breaks_file': (
        'select {{ a() }}',
        {
            'macros/a.sql': '{% macro a() %}A{% endmacro %}{% docs x %}doc{% enddocs %}',
        },
    ),
    'pkg_macro_test_block_breaks_file': (
        'select {{ a() }}',
        {
            'macros/a.sql': '{% macro a() %}A{% endmacro %}{% test t(model) %}select 1{% endtest %}',
        },
    ),
    'pkg_materialization_breaks_file': (
        'select {{ a() }}',
        {
            'macros/a.sql': '{% macro a() %}A{% endmacro %}{% materialization m, default %}x{% endmaterialization %}',
        },
    ),
    'dbt_exceptions_warn': ("{% do exceptions.warn('careful') %}select 1", {}),
    'dbt_raise_in_dead_branch': (
        "{% if var('strict', false) %}{{ exceptions.raise_compiler_error('no') }}{% endif %}select 1",
        {},
    ),
    'dbt_return_none': ('{{ return(none) }}|', {}),
    'dbt_return_no_args': ('{{ return() }}', {}),
    'dbt_config_in_macro': (
        "{% macro m() %}{{ config(x=1) }}[{{ config.get('y') }}]{% endmacro %}{{ m() }}",
        {},
    ),
    'dbt_this_in_macro': ('{% macro m() %}{{ this }}|{{ this.identifier }}{% endmacro %}{{ m() }}', {}),
    'dbt_target_dict_ops': (
        "{{ target.get('type') }}{{ target.get('nope', 'dflt') }}{{ target.keys() | list | length }}{{ target['name'] }}",
        {},
    ),
    'dbt_target_modify': ("{% do target.update({'x': 1}) %}{{ target.x }}", {}),
    'dbt_var_kw_default': ("{{ var('a', default='d') }}", {}),
    'dbt_var_no_args': ('{{ var() }}', {}),
    'dbt_var_is_defined': (
        "{% if var('x', none) is none %}n{% endif %}{% if var('x') %}t{% else %}f{% endif %}",
        {},
    ),
    'dbt_env_var_kw_default': ("{{ env_var('X', default='kw') }}", {}),
    'dbt_env_var_no_args': ('{{ env_var() }}', {}),
    'dbt_ref_no_args': ('{{ ref() }}|', {}),
    'dbt_ref_three_args': ("{{ ref('other', 'customers', 'extra') }}", {}),
    'dbt_ref_non_string': ("{{ ref(1) }}|{{ ref('stg_orders', 3) }}", {}),
    'dbt_source_kwargs': ("{{ source(source_name='app', table='events') }}", {}),
    'dbt_source_wrong_arity': ("{{ source('app') }}", {}),
    'dbt_source_three': ("{{ source('app', 'events', 'x') }}", {}),
    'dbt_is_incremental_args': ('{{ is_incremental(1) }}', {}),
    'dbt_adapter_type_attr': ('{{ adapter.type_ }}', {}),
    'dbt_adapter_quote_numbers': ("{{ adapter.quote(5) }}{{ adapter.quote('a\"b') }}", {}),
    'dbt_adapter_dispatch_no_args': ('{{ adapter.dispatch() }}', {}),
    'dbt_dbt_date_trunc_kwargs': ("{{ dbt.date_trunc(date='d', datepart='month') }}", {}),
    'dbt_dbt_wrong_arity': ("{{ dbt.date_trunc('day') }}", {}),
    'dbt_dbt_unknown_kwarg': ("{{ dbt.hash('x', salt=1) }}", {}),
    'dbt_dbt_concat_string': ("{{ dbt.concat('abc') }}", {}),
    'dbt_dbt_dateadd_expression': ("{{ dbt.dateadd('day', -1 * 7, 'ts') }}", {}),
    'dbt_dbt_unknown_with_kwargs': ("{{ dbt.future_macro(a='x', b=['p', 'q'], c='week') }}", {}),
    'dbt_dbt_attr_no_call': ('[{{ dbt.future_macro }}][{{ dbt.future_macro.x }}]', {}),
    'dbt_dbt_listagg_defaults': ("{{ dbt.listagg('m', order_by_clause='o') }}", {}),
    'dbt_dbt_string_literal_nonstr': ('{{ dbt.string_literal(5) }}{{ dbt.string_literal(none) }}', {}),
    'json_in_template': ("{% set j = fromjson('{}') %}x", {}),
    'sql_comment_dashes': ("select 1 --{{ ref('stg_orders') }}\nfrom t", {}),
    'sql_string_with_braces': ("select '{{ \"x\" }}' as a, '{% if true %}y{% endif %}' as b", {}),
    'sql_jinja_in_identifier': ("select {{ 'col' }}_1 as {{ 'a' }}b from t", {}),
    'sql_quote_nesting': ('select "{{ \'a\' }}" as x, \'{{ "b" }}\' as y', {}),
    'sql_semicolons_and_hash': ('select 1; -- {# note #}\nselect 2 # no', {}),
    'sql_percent_braces_in_text': ("select '%}' as a, '}}' as b", {}),
    'sql_open_brace_pair_in_text': ("select '{ {' as a, '{}' as b", {}),
    'sql_dollar_and_backtick': ('select `a`, $1, $$x$$ from t', {}),
    'sql_lone_open_brace_hash': ('select {# c #} 1', {}),
    'sql_unclosed_brace_pair': ("select '{{' as a", {}),
    'sql_unclosed_block_pair': ("select '{%' as a", {}),
    'sql_hash_brace': ("select '{#' as a", {}),
    'sql_non_ascii': ("select 'ñ{{ 1 }}é日本' as x", {}),
    'sql_long_template': (
        'select ' + ', '.join(f'{{{{ {n} }}}} as c{n}' for n in range(300)) + ' from t',
        {},
    ),
    'source_dynamic': ("{% for t in ['events'] %}select * from {{ source('app', t) }}{% endfor %}", {}),
}
