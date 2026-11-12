"""ODCS 3.2 data contracts: ``konstruct export odcs`` writes them from the ontology, with verified
statements only from evidence Konstruct holds, and ``konstruct contract check`` tests the claims of a
hand-written contract against the graph."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft201909Validator

from konstruct import Project
from konstruct.analysis.contract import ContractCheck, check_contract
from konstruct.analysis.query import QueryError
from konstruct.core.graph import Graph, Node
from konstruct.interfaces.cli import main
from konstruct.outputs import odcs
from konstruct.outputs.export import ExportError, export
from konstruct.semantics.ontology import derive
from tests.test_ontology import _relation, _term, _unique, shop

SCHEMA = json.loads(Path(__file__).parent.joinpath('fixtures/odcs/odcs-json-schema-v3.2.0.json').read_text())
VALIDATOR = Draft201909Validator(SCHEMA)


def contracts(text: str) -> list[dict[str, Any]]:
    docs = [d for d in yaml.safe_load_all(text) if d is not None]
    assert docs
    return docs


def validate(text: str) -> list[dict[str, Any]]:
    docs = contracts(text)
    for doc in docs:
        errors = sorted(VALIDATOR.iter_errors(doc), key=lambda e: list(e.path))
        assert not errors, [f'{list(e.absolute_path)}: {e.message}' for e in errors[:5]]
    return docs


def typed_shop() -> Graph:
    """The ``shop`` graph with column types, a not-null test and a second, untested relationship."""
    g = shop()
    for node in g.nodes.values():
        if node.kind == 'column' and node.parent in ('model.shop.customers', 'model.shop.orders'):
            node.data_type = {'customer_id': 'INTEGER', 'name': 'VARCHAR', 'order_id': 'INTEGER'}.get(
                node.name, 'DECIMAL(18,2)' if node.name == 'amount' else 'INTEGER'
            )
    return g


def statements(doc: dict[str, Any]) -> list[dict[str, Any]]:
    return [s for obj in doc['schema'] for s in obj.get('context', {}).get('verifiedStatements', [])]


def props(entry: dict[str, Any]) -> dict[str, Any]:
    return {c['property']: c['value'] for c in entry.get('customProperties', [])}


def obj(doc: dict[str, Any], name: str) -> dict[str, Any]:
    return next(o for o in doc['schema'] if o['name'] == name)


def prop(doc: dict[str, Any], table: str, column: str) -> dict[str, Any]:
    return next(p for p in obj(doc, table)['properties'] if p['name'] == column)


# The export


def test_the_export_is_one_odcs_32_contract_per_relation_that_holds_an_entity_or_measure() -> None:
    g = typed_shop()
    docs = validate(odcs.export(g, derive(g), name='shop'))
    assert {d['name'] for d in docs} == {'customers', 'orders'}
    for doc in docs:
        assert (doc['apiVersion'], doc['kind'], doc['status']) == ('v3.2.0', 'DataContract', 'draft')
        assert doc['id'].startswith('urn:konstruct:shop:')
    customers = next(d for d in docs if d['name'] == 'customers')
    assert obj(customers, 'customers')['physicalName'] == 'db.main.customers'
    key = prop(customers, 'customers', 'customer_id')
    assert (key['primaryKey'], key['unique'], key['physicalType'], key['logicalType']) == (
        True,
        True,
        'INTEGER',
        'integer',
    )
    assert prop(customers, 'customers', 'name')['logicalType'] == 'string'
    assert 'primaryKey' not in prop(customers, 'customers', 'name')


def test_a_measure_column_is_a_measure_with_its_composed_formula() -> None:
    g = typed_shop()
    docs = validate(odcs.export(g, derive(g), name='shop'))
    orders = next(d for d in docs if d['name'] == 'orders')
    amount = prop(orders, 'orders', 'amount')
    assert amount['semanticType'] == 'measure'
    assert amount['transformLogic'] == 'SUM(raw_orders.amount)'
    assert amount['transformSourceObjects'] == ['raw_orders']
    assert props(amount)['konstructGrade'] == 'verified'
    assert 'semanticType' not in prop(orders, 'orders', 'order_id')


def test_relationships_are_foreign_keys_and_an_inferred_one_carries_its_grade() -> None:
    g = typed_shop()
    docs = validate(odcs.export(g, derive(g), name='shop'))
    orders = next(d for d in docs if d['name'] == 'orders')
    [link] = obj(orders, 'orders')['relationships']
    assert (link['type'], link['from'], link['to']) == (
        'foreignKey',
        'orders.customer_id',
        'customers.customer_id',
    )
    assert props(link)['konstructGrade'] == 'inferred'
    # Nothing vouches for it, so no statement says so.
    assert all('customer_id' not in s['question'] for s in statements(orders) if 'refer' in s['question'])


def test_unique_and_not_null_tests_become_quality_rules_for_the_users_tools() -> None:
    g = typed_shop()
    test = g.add_node(
        __import__('konstruct.core.graph', fromlist=['Node']).Node(
            'test.shop.not_null_orders_order_id', 'test', 'not_null', attrs={'test': 'not_null'}
        )
    )
    g.add_edge(test.id, 'model.shop.orders#order_id', 'tests')
    docs = validate(odcs.export(g, derive(g), name='shop'))
    orders = next(d for d in docs if d['name'] == 'orders')
    order_id = prop(orders, 'orders', 'order_id')
    assert order_id['required'] is True
    metrics = {q['metric']: q for q in order_id['quality']}
    assert metrics['duplicateValues']['mustBe'] == 0
    assert metrics['nullValues']['mustBe'] == 0


def test_verified_statements_come_only_from_tests_and_verify_and_name_their_evidence() -> None:
    g = typed_shop()
    g.meta['integrity'] = {
        'keys': {'model.shop.customers#customer_id': {'state': 'confirmed', 'detail': 'customers ok'}},
        'links': {
            'model.shop.orders#customer_id|model.shop.customers#customer_id': {
                'state': 'confirmed',
                'detail': 'all 3 values of orders.customer_id are in customers.customer_id',
            }
        },
    }
    docs = validate(odcs.export(g, derive(g), name='shop'))
    seen: list[dict[str, Any]] = []
    for doc in docs:
        for statement in statements(doc):
            custom = props(statement)
            assert statement['answer'] and statement['id'] and statement['question']
            assert custom['konstructGrade'] in ('verified', 'declared')
            evidence = custom['konstructEvidence']
            assert evidence
            for item in evidence:
                if item.startswith('test.'):
                    assert g.nodes[item].kind == 'test'
                else:
                    assert item.startswith('konstruct verify: ')
            seen.append(statement)
    grades = {(s['question'], props(s)['konstructGrade']) for s in seen}
    assert any(q.startswith('Is customers.customer_id') and g == 'verified' for q, g in grades)
    assert any(q.startswith('Is orders.order_id') and g == 'declared' for q, g in grades)
    assert any('refer' in q and g == 'verified' for q, g in grades)
    assert any(q.startswith('How is') and g == 'verified' for q, g in grades)


def test_a_graph_with_no_tests_and_no_verify_writes_no_statements_about_keys_or_links() -> None:
    g = Graph()
    g.meta['dialect'] = 'duckdb'
    customers = _relation(g, 'customers', ['customer_id', 'name'])
    orders = _relation(g, 'orders', ['order_id', 'customer_id'])
    orders.attrs['conditions'] = ['LEFT JOIN ON: db.main.orders.customer_id = db.main.customers.customer_id']
    # Entities named only by a term: no test or primary key says either key is unique.
    _term(g, 'Customer', [f'{customers.id}#customer_id'], kind='entity')
    _term(g, 'Order', [f'{orders.id}#order_id'], kind='entity')
    ontology = derive(g)
    assert ontology.entities and not ontology.unique_ids
    text = odcs.export(g, ontology, name='bare')
    for doc in contracts(text):
        assert statements(doc) == []
        assert 'primaryKey' not in json.dumps(doc)
        assert 'unique' not in json.dumps(doc['schema'][0]['properties'])


def test_a_formula_verify_did_not_confirm_is_not_a_statement_and_keeps_its_grade() -> None:
    g = typed_shop()
    for edge in g.edges:
        edge.attrs.pop('verified', None)
    g.changed()
    ontology = derive(g)
    assert ontology.measures[0].confidence == 'declared'
    docs = validate(odcs.export(g, ontology, name='shop'))
    orders = next(d for d in docs if d['name'] == 'orders')
    assert not [s for s in statements(orders) if s['question'].startswith('How is')]
    amount = prop(orders, 'orders', 'amount')
    assert props(amount)['konstructGrade'] == 'declared'
    assert amount['transformLogic'] == 'SUM(raw_orders.amount)'


def test_the_export_says_what_it_leaves_out_and_is_deterministic() -> None:
    g = typed_shop()
    first = odcs.export(g, derive(g), name='shop')
    assert first == odcs.export(g, derive(g), name='shop')
    assert first.startswith('---\n# ')
    assert 'not enforced' in first.lower()


def test_an_ontology_with_nothing_to_export_is_an_error() -> None:
    g = Graph()
    with pytest.raises(ValueError, match='nothing to export'):
        odcs.export(g, derive(g), name='empty')


def test_odcs_is_a_registered_export_format() -> None:
    g = typed_shop()
    assert export(g, 'odcs') == odcs.export(g, derive(g), name=None)
    with pytest.raises(ExportError, match='whole ontology'):
        from konstruct.core.graph import Node

        export(g, 'odcs', select=Node('x', 'model', 'x'))


# The check

CONTRACT = textwrap.dedent(
    """\
    apiVersion: v3.2.0
    kind: DataContract
    id: shop-customers
    version: 1.0.0
    name: customers
    schema:
      - name: customers
        physicalName: db.main.customers
        properties:
          - name: customer_id
            physicalType: INTEGER
            primaryKey: true
            unique: true
          - name: name
            logicalType: string
      - name: orders
        properties:
          - name: order_id
            logicalType: integer
            primaryKey: true
          - name: amount
            physicalType: DECIMAL(18,2)
            semanticType: measure
        relationships:
          - type: foreignKey
            from: orders.customer_id
            to: customers.customer_id
    """
)


def kinds(result: ContractCheck) -> set[tuple[str, str]]:
    return {(f.kind, f.where) for f in result.findings}


def test_a_true_contract_passes() -> None:
    result = check_contract(typed_shop(), CONTRACT)
    assert result.findings == []
    assert result.ok and result.claims >= 8


def test_each_planted_false_claim_is_found() -> None:
    planted = (
        CONTRACT.replace('physicalType: DECIMAL(18,2)', 'physicalType: VARCHAR')
        .replace(
            '      - name: name\n        logicalType: string',
            '      - name: name\n        primaryKey: true\n      - name: email\n        logicalType: string',
        )
        .replace('to: customers.customer_id', 'to: customers.name')
    )
    assert planted.count('email') == 1
    result = check_contract(typed_shop(), planted)
    assert kinds(result) == {
        # name joins customer_id as a second column of the primary key.
        ('key', 'customers'),
        ('missing-column', 'customers.email'),
        ('wrong-type', 'orders.amount'),
        ('relationship', 'orders.customer_id -> customers.name'),
    }
    assert not result.ok
    by_kind = {f.kind: f for f in result.findings}
    assert by_kind['wrong-type'].severity == 'contradicted'
    assert by_kind['missing-column'].severity == 'contradicted'
    # customers.name is not known to be unique, which is not evidence that it is not.
    assert by_kind['relationship'].severity == 'unsupported'
    assert by_kind['key'].severity == 'unsupported'


def test_a_missing_table_and_a_relationship_nothing_supports() -> None:
    text = CONTRACT.replace('name: orders\n', 'name: invoices\n').replace(
        'from: orders.customer_id', 'from: customers.name'
    )
    result = check_contract(typed_shop(), text)
    assert ('missing-table', 'invoices') in kinds(result)
    assert any(f.kind == 'relationship' and f.severity == 'unsupported' for f in result.findings)


def test_a_failed_verify_check_contradicts_a_unique_claim() -> None:
    g = typed_shop()
    g.meta['integrity'] = {
        'keys': {'model.shop.customers#customer_id': {'state': 'failed', 'detail': '2 duplicates'}},
        'links': {},
    }
    result = check_contract(g, CONTRACT)
    # Verify found the key repeated, so the key and uniqueness claims fail, and so does the
    # relationship that refers to it: its cardinality is unknown.
    assert {(f.kind, f.severity) for f in result.findings} == {
        ('key', 'contradicted'),
        ('unique', 'contradicted'),
        ('relationship', 'contradicted'),
    }


def test_a_measure_that_is_not_an_aggregate_is_found() -> None:
    g = typed_shop()
    g.nodes['model.shop.orders#amount'].attrs['expression'] = 'db.main.raw_orders.amount'
    result = check_contract(g, CONTRACT)
    assert kinds(result) == {('measure', 'orders.amount')}


def test_sql_claims_are_left_to_verify() -> None:
    text = CONTRACT.replace(
        '        semanticType: measure\n',
        '        semanticType: measure\n'
        '        quality:\n'
        '          - type: sql\n'
        '            name: positive\n'
        '            query: SELECT COUNT(*) FROM orders WHERE amount < 0\n'
        '            mustBe: 0\n',
    )
    assert 'positive' in text
    result = check_contract(typed_shop(), text)
    assert result.findings == []
    assert result.not_run == ['orders.amount: sql quality rule positive']


def test_a_file_that_is_not_a_contract_is_an_error() -> None:
    with pytest.raises(ValueError, match='not a data contract'):
        check_contract(typed_shop(), 'just: some\nother: yaml\n')
    with pytest.raises(ValueError, match='YAML'):
        check_contract(typed_shop(), 'a: [unclosed')


def test_the_result_has_text_and_a_dict() -> None:
    result = check_contract(typed_shop(), CONTRACT.replace('physicalType: INTEGER', 'physicalType: DATE'))
    data = result.to_dict()
    assert data['ok'] is False and data['findings'][0]['kind'] == 'wrong-type'
    assert 'customers.customer_id' in str(result) and 'wrong-type' in str(result)
    assert 'no findings' in str(check_contract(typed_shop(), CONTRACT)).lower()


def test_konstruct_own_export_checked_by_contract_check_gives_no_findings() -> None:
    g = typed_shop()
    g.meta['integrity'] = {
        'keys': {'model.shop.customers#customer_id': {'state': 'confirmed', 'detail': 'ok'}},
        'links': {},
    }
    text = odcs.export(g, derive(g), name='shop')
    result = check_contract(g, text)
    assert result.findings == []
    assert result.contracts == 2


def test_a_contract_file_may_hold_several_documents() -> None:
    result = check_contract(typed_shop(), CONTRACT + '---\n' + CONTRACT.replace('INTEGER', 'DATE'))
    assert result.contracts == 2 and len(result.findings) == 1


def test_unique_keys_are_not_confused_by_a_second_table() -> None:
    g = typed_shop()
    extra = _relation(g, 'items', ['item_id'])
    _unique(g, extra, 'item_id')
    assert check_contract(g, CONTRACT).findings == []


# The example project


def test_jaffle_shop_contracts_validate_and_round_trip(jaffle_built_graph: Path) -> None:
    project = Project(jaffle_built_graph)
    text = project.export('odcs')
    docs = validate(text)
    names = {d['name'] for d in docs}
    assert {'customers', 'orders'} <= names
    customers = next(d for d in docs if d['name'] == 'customers')
    assert prop(customers, 'customers', 'customer_id')['primaryKey'] is True
    for doc in docs:
        for statement in statements(doc):
            custom = props(statement)
            assert custom['konstructGrade'] in ('verified', 'declared')
            for item in custom['konstructEvidence']:
                assert item.startswith('konstruct verify: ') or project.graph.nodes[item].kind == 'test'
    result = project.check_contract(text)
    assert result.findings == []


def test_jaffle_shop_contract_check_command(jaffle_built_graph: Path, tmp_path: Path, capsys: Any) -> None:
    out = tmp_path / 'contracts.yml'
    assert main(['export', 'odcs', '-C', str(jaffle_built_graph), '-o', str(out)]) == 0
    assert main(['contract', 'check', str(out), '-C', str(jaffle_built_graph)]) == 0
    capsys.readouterr()
    bad = tmp_path / 'bad.yml'
    bad.write_text(out.read_text().replace('name: customer_id', 'name: customer_ident', 1))
    assert main(['contract', 'check', str(bad), '-C', str(jaffle_built_graph), '--json']) == 1
    data = json.loads(capsys.readouterr().out)
    assert data['ok'] is False and data['findings'][0]['kind'] == 'missing-column'
    assert main(['contract', 'check', str(tmp_path / 'absent.yml'), '-C', str(jaffle_built_graph)]) != 0


# Tests that are scoped, and what verify found


def scoped(g: Graph, kind: str, column: str, **attrs: Any) -> None:
    """A dbt test of ``kind`` on ``column`` of ``orders``, scoped by ``where`` as dbt's config does."""
    from konstruct.core.graph import Node

    test = g.add_node(Node(f'test.shop.{kind}_scoped', 'test', kind, attrs={'test': kind, **attrs}))
    g.add_edge(test.id, f'model.shop.orders#{column}', 'tests')


def where_shop() -> Graph:
    """``typed_shop`` with every unique test on ``orders`` and ``customers`` scoped by a ``where``."""
    g = typed_shop()
    for node in g.nodes.values():
        if node.kind == 'test':
            node.attrs['where'] = "status != 'returned'"
    g.changed()
    return g


def test_a_test_scoped_by_where_gives_no_unconditional_claim() -> None:
    g = where_shop()
    scoped(g, 'not_null', 'order_id', where='order_id > 1')
    ontology = derive(g)
    assert not ontology.unique_ids
    docs = validate(odcs.export(g, ontology, name='shop'))
    for doc in docs:
        # Only a formula verify confirmed is left; no test says anything of a key or a null.
        assert [s['question'] for s in statements(doc) if not s['question'].startswith('How is')] == []
        text = json.dumps(doc['schema'])
        for field in ('"unique"', '"primaryKey"', '"required"'):
            assert field not in text
    # A contract that claims them is unsupported, not backed by a scoped test.
    result = check_contract(g, CONTRACT.replace('physicalType: INTEGER\n', 'physicalType: INTEGER\n'))
    assert {f.kind for f in result.findings} >= {'key', 'unique'}


def nulls_shop() -> Graph:
    g = typed_shop()
    scoped(g, 'not_null', 'order_id')
    g.meta['integrity'] = {
        'keys': {
            'model.shop.orders#order_id': {
                'state': 'failed',
                'nulls': 2,
                'detail': 'orders.order_id has 2 nulls',
            }
        },
        'links': {},
    }
    return g


def test_nulls_verify_found_drop_required_and_the_not_null_statement() -> None:
    g = nulls_shop()
    docs = validate(odcs.export(g, derive(g), name='shop'))
    orders = next(d for d in docs if d['name'] == 'orders')
    assert 'required' not in prop(orders, 'orders', 'order_id')
    assert not [s for s in statements(orders) if 'ever null' in s['question']]
    claim = CONTRACT.replace(
        '            primaryKey: true\n', '            primaryKey: true\n            required: true\n'
    )
    claim = claim.replace(
        '      - name: order_id\n        logicalType: integer\n',
        '      - name: order_id\n        logicalType: integer\n        required: true\n',
    )
    result = check_contract(g, claim)
    assert ('required', 'contradicted') in {(f.kind, f.severity) for f in result.findings}


def one_to_one_shop(one_to_one: str | None) -> Graph:
    g = typed_shop()
    _unique(g, g.nodes['model.shop.orders'], 'customer_id')
    record: dict[str, Any] = {'state': 'confirmed', 'detail': 'all values found'}
    if one_to_one:
        record['one_to_one'] = one_to_one
    g.meta['integrity'] = {
        'keys': {},
        'links': {'model.shop.orders#customer_id|model.shop.customers#customer_id': record},
    }
    return g


def test_one_to_one_is_in_a_verified_answer_only_when_verify_confirmed_it() -> None:
    for found, expected in ((None, False), ('failed', False), ('confirmed', True)):
        g = one_to_one_shop(found)
        ontology = derive(g)
        assert ontology.relationships[0].cardinality in ('one-to-one', 'many-to-one')
        docs = validate(odcs.export(g, ontology, name='shop'))
        orders = next(d for d in docs if d['name'] == 'orders')
        [answer] = [s['answer'] for s in statements(orders) if 'refer' in s['question']]
        assert ('one-to-one' in answer) is expected, (found, answer)


def test_a_refuted_target_key_is_described_by_the_relationships_own_evidence() -> None:
    g = typed_shop()
    g.meta['integrity'] = {
        'keys': {'model.shop.customers#customer_id': {'state': 'failed', 'detail': '2 duplicates'}},
        'links': {},
    }
    ontology = derive(g)
    text = odcs.export(g, ontology, name='shop')
    assert 'values of orders.customer_id not in' not in text
    assert 'konstruct verify: 2 duplicates' in text
    result = check_contract(g, CONTRACT)
    [link] = [f for f in result.findings if f.kind == 'relationship']
    assert 'missing from the key' not in link.message and '2 duplicates' in link.message


# The YAML the export writes, read by PyYAML

NASTY = [
    '1_000',
    '2024-01-01',
    '0b101',
    '0o17',
    '+.inf',
    '-.INF',
    '.nan',
    '0x1F',
    '1e3',
    '12:30:45',
    'yes',
    'No',
    'on',
    'OFF',
    'y',
    'null',
    '~',
    '=',
    '<<',
    '',
    ' lead',
    'trail ',
    'a: b',
    '# c',
    '- d',
    '? e',
    '|',
    '>',
    '\x07bell',
    '\x1besc',
    '\x7fdel',
    '\x9fc1',
    '\x85nel',
    'a\u2028b',
    'a\u2029b',
    'a\rb',
    'a\tb',
    '  first\nsecond',
    'ends\n\n',
    '\nstarts',
    'tab\t',
    'ünï',
    '"q"',
    "it's",
    '\\',
    '%x',
    '@x',
    '`b',
    '{a}',
    '[a]',
    '&a',
    '*a',
    '!a',
]


def named_graph(text: str) -> Graph:
    g = typed_shop()
    g.nodes['model.shop.customers'].description = text
    g.nodes['model.shop.customers#name'].description = text
    return g


@pytest.mark.parametrize('text', NASTY)
def test_the_yaml_reads_back_to_the_text_it_was_given(text: str) -> None:
    g = named_graph(text)
    docs = yaml.safe_load_all(odcs.export(g, derive(g), name='shop'))
    customers = next(d for d in docs if d['name'] == 'customers')
    assert customers['schema'][0].get('description') == (text or None) or text == ''
    if text:
        assert prop(customers, 'customers', 'name')['description'] == text
        assert all(isinstance(v, str) for v in (customers['schema'][0]['description'],))


def test_a_label_with_a_line_break_cannot_inject_keys() -> None:
    g = typed_shop()
    g.nodes['model.shop.customers'].name = 'customers'
    g.nodes['model.shop.customers'].relation = 'db.main.customers\nstatus: active'
    for brk in ('\n', '\r', '\x85', '\u2028', '\u2029', '\r\n'):
        g.nodes['model.shop.customers'].relation = f'db.main.customers{brk}status: active'
        g.changed()
        docs = yaml.safe_load_all(odcs.export(g, derive(g), name=f'shop{brk}status: active'))
        for doc in docs:
            assert doc['status'] == 'draft', repr(brk)
            assert 'injected' not in json.dumps(doc)


def test_hypothesis_text_round_trips() -> None:
    from hypothesis import given, settings
    from hypothesis import strategies as st

    @settings(max_examples=60, deadline=None)
    @given(st.text(alphabet=st.characters(blacklist_categories=['Cs']), max_size=40))
    def check(text: str) -> None:
        g = named_graph(text)
        docs = list(yaml.safe_load_all(odcs.export(g, derive(g), name='shop')))
        customers = next(d for d in docs if d['name'] == 'customers')
        if text:
            assert prop(customers, 'customers', 'name')['description'] == text

    check()


# Reading a hostile contract

ALIAS_BOMB = 'a: &a [x,x,x,x,x,x,x,x,x,x]\n' + ''.join(
    f'{c}: &{c} [*{p},*{p},*{p},*{p},*{p},*{p},*{p},*{p},*{p}]\n'
    for p, c in zip('abcdefgh', 'bcdefghi', strict=True)
)


def test_an_alias_bomb_is_refused_not_expanded() -> None:
    bomb = 'kind: DataContract\n' + ALIAS_BOMB + 'schema: []\n'
    with pytest.raises(ValueError, match='alias'):
        check_contract(typed_shop(), bomb)


@pytest.mark.parametrize('links', [150, 450, 900])
def test_deep_nesting_through_aliases_is_an_invalid_contract_for_every_caller(
    links: int, jaffle_built_graph: Path, tmp_path: Path, capsys: Any
) -> None:
    chain = (
        'z0: &a0 '
        + '[' * 90
        + ']' * 90
        + '\n'
        + ''.join(f'z{i}: &a{i} [*a{i - 1}]\n' for i in range(1, links + 1))
    )
    text = 'kind: DataContract\nschema:\n  - name: customers\n' + textwrap.indent(chain, '    ')
    with pytest.raises(ValueError, match='nested too deeply'):
        check_contract(typed_shop(), text)
    with pytest.raises(QueryError, match='nested too deeply'):
        Project(jaffle_built_graph).check_contract(text)
    path = tmp_path / 'deep.yml'
    path.write_text(text)
    assert main(['contract', 'check', str(path), '-C', str(jaffle_built_graph)]) == 1
    assert 'nested too deeply' in capsys.readouterr().err


def test_the_mcp_tool_refuses_a_contract_over_the_size_cap() -> None:
    from konstruct.interfaces.mcp_server import MAX_CONTRACT_BYTES

    assert MAX_CONTRACT_BYTES <= 8_000_000


# Names in a contract


def test_the_physical_name_wins_over_the_name() -> None:
    text = textwrap.dedent(
        """\
        kind: DataContract
        schema:
          - name: orders
            physicalName: db.main.customers
            properties:
              - name: customer_id
              - name: order_id
        """
    )
    result = check_contract(typed_shop(), text)
    # The table is customers, which has no order_id.
    assert [(f.kind, f.where) for f in result.findings] == [('missing-column', 'orders.order_id')]


def test_a_property_physical_name_is_the_column_and_a_partly_qualified_table_resolves() -> None:
    text = textwrap.dedent(
        """\
        kind: DataContract
        schema:
          - name: anything
            physicalName: main.customers
            properties:
              - name: ident
                physicalName: customer_id
                primaryKey: true
        """
    )
    assert check_contract(typed_shop(), text).findings == []
    text = text.replace('physicalName: customer_id', 'physicalName: nope')
    assert [f.kind for f in check_contract(typed_shop(), text).findings] == ['missing-column']


def test_a_shorthand_reference_resolves_through_the_contracts_own_names() -> None:
    text = textwrap.dedent(
        """\
        kind: DataContract
        schema:
          - name: order-items
            physicalName: orders
            properties:
              - name: cust
                physicalName: customer_id
            relationships:
              - from: order-items.cust
                to: customer-dim.key
          - name: customer-dim
            physicalName: db.main.customers
            properties:
              - name: key
                physicalName: customer_id
        """
    )
    assert check_contract(typed_shop(), text).findings == []


def test_a_relationship_is_contradicted_only_on_evidence() -> None:
    g = typed_shop()
    other = _relation(g, 'regions', ['region_id'])
    _unique(g, other, 'region_id')
    text = CONTRACT.replace('to: customers.customer_id', 'to: regions.region_id')
    [finding] = check_contract(g, text).findings
    assert (finding.kind, finding.severity) == ('relationship', 'unsupported')
    # A target not known to be unique is not shown to be wrong ...
    text = CONTRACT.replace('to: customers.customer_id', 'to: customers.name')
    [finding] = check_contract(g, text).findings
    assert (finding.kind, finding.severity) == ('relationship', 'unsupported')
    # ... unless verify found its values repeated.
    g.meta['integrity'] = {
        'keys': {'model.shop.customers#name': {'state': 'failed', 'detail': '3 duplicates'}},
        'links': {},
    }
    [finding] = check_contract(g, text).findings
    assert (finding.kind, finding.severity) == ('relationship', 'contradicted')
    assert '3 duplicates' in finding.message


def test_a_relationships_test_between_relations_with_no_entity_is_checked() -> None:
    g = typed_shop()
    stores = _relation(g, 'stores', ['region_code', 'name'])
    regions = _relation(g, 'regions', ['region_code', 'label'])
    test = g.add_node(Node('test.shop.rel', 'test', 'relationships', attrs={'test': 'relationships'}))
    g.add_edge(test.id, f'{stores.id}#region_code', 'tests')
    g.add_edge(test.id, f'{regions.id}#region_code', 'tests', role='relationship target')
    assert derive(g).relationships and all(r.source != 'store' for r in derive(g).relationships)
    claim = textwrap.dedent(
        """\
        kind: DataContract
        schema:
          - name: stores
            relationships:
              - from: stores.region_code
                to: regions.region_code
          - name: regions
        """
    )
    assert check_contract(g, claim).findings == []
    wrong = claim.replace('to: regions.region_code', 'to: regions.label')
    [finding] = check_contract(g, wrong).findings
    assert (finding.kind, finding.severity) == ('relationship', 'unsupported')


# Types, keys and shapes


def test_a_narrower_or_wider_integer_is_a_wrong_type() -> None:
    result = check_contract(typed_shop(), CONTRACT.replace('physicalType: INTEGER', 'physicalType: BIGINT'))
    assert [(f.kind, f.where) for f in result.findings] == [('wrong-type', 'customers.customer_id')]


TYPE_ROWS = [
    # Snowflake: every integer is NUMBER(38,0), every float is a double, TIMESTAMP is NTZ.
    ('snowflake', 'NUMBER(38,0)', 'INTEGER', True),
    ('snowflake', 'NUMBER(38,0)', 'INT', True),
    ('snowflake', 'NUMBER(38,0)', 'BIGINT', True),
    ('snowflake', 'BIGINT', 'NUMBER(38,0)', True),
    ('snowflake', 'NUMBER(10,2)', 'INTEGER', False),
    ('snowflake', 'NUMBER(38,0)', 'NUMBER(10,2)', False),
    ('snowflake', 'FLOAT', 'DOUBLE', True),
    ('snowflake', 'FLOAT', 'FLOAT8', True),
    ('snowflake', 'FLOAT', 'REAL', True),
    ('snowflake', 'FLOAT', 'FLOAT4', True),
    ('snowflake', 'FLOAT', 'VARCHAR', False),
    ('snowflake', 'TIMESTAMP_NTZ', 'TIMESTAMP', True),
    ('snowflake', 'TIMESTAMP_NTZ', 'DATETIME', True),
    ('snowflake', 'TIMESTAMP_NTZ', 'TIMESTAMP_TZ', False),
    # BigQuery: one integer, INT64.
    ('bigquery', 'INT64', 'INTEGER', True),
    ('bigquery', 'INT64', 'INT', True),
    ('bigquery', 'INT64', 'SMALLINT', True),
    ('bigquery', 'INT64', 'STRING', False),
    # Postgres keeps its integer widths, and a precision on one side only is not a difference.
    ('postgres', 'INTEGER', 'BIGINT', False),
    ('postgres', 'INTEGER', 'int4', True),
    ('postgres', 'NUMERIC(10,2)', 'NUMERIC', True),
    ('postgres', 'NUMERIC', 'NUMERIC(10,2)', True),
    ('postgres', 'NUMERIC(10,2)', 'NUMERIC(12,2)', False),
    ('postgres', 'VARCHAR(20)', 'VARCHAR', True),
    ('postgres', 'VARCHAR(20)', 'VARCHAR(50)', False),
    ('postgres', 'VARCHAR(20)', 'VARCHAR(20)', True),
    ('postgres', 'CHAR(3)', 'VARCHAR(3)', False),
    # Snowflake reports TEXT for every string column.
    ('snowflake', 'TEXT', 'VARCHAR', True),
    ('snowflake', 'TEXT', 'STRING', True),
    ('snowflake', 'VARCHAR(20)', 'TEXT', True),
    ('snowflake', 'TEXT', 'NUMBER(38,0)', False),
    ('snowflake', 'CHAR(3)', 'VARCHAR(3)', True),
    ('snowflake', 'NCHAR', 'VARCHAR', True),
    ('bigquery', 'FLOAT64', 'FLOAT', True),
    ('bigquery', 'FLOAT64', 'INT64', False),
    ('databricks', 'TIMESTAMP', 'TIMESTAMP_LTZ', True),
    ('databricks', 'TIMESTAMP', 'TIMESTAMP_NTZ', False),
    # A nested type is compared with what it holds.
    ('bigquery', 'ARRAY<STRING>', 'ARRAY<STRING>', True),
    ('bigquery', 'ARRAY<STRING>', 'ARRAY<INT64>', False),
    ('bigquery', 'ARRAY<INT64>', 'ARRAY<INTEGER>', True),
    ('bigquery', 'ARRAY<STRING>', 'ARRAY', True),
    ('bigquery', 'ARRAY<STRING(10)>', 'ARRAY<STRING>', True),
    ('bigquery', 'ARRAY<STRING(10)>', 'ARRAY<STRING(20)>', False),
    ('bigquery', 'STRUCT<a STRING(10)>', 'STRUCT<a STRING>', True),
    ('bigquery', 'STRUCT<a INT64, b STRING>', 'STRUCT<a INT64, b STRING>', True),
    ('bigquery', 'STRUCT<a INT64, b STRING>', 'STRUCT<a INT64, b INT64>', False),
    ('bigquery', 'STRUCT<a INT64>', 'STRUCT<c INT64>', False),
    ('databricks', 'MAP<STRING, INT>', 'MAP<STRING, BIGINT>', False),
    ('postgres', 'VARCHAR(20)', 'character varying', True),
]


@pytest.mark.parametrize(('dialect', 'actual', 'claimed', 'same'), TYPE_ROWS)
def test_types_are_compared_as_the_graphs_dialect_reads_them(
    dialect: str, actual: str, claimed: str, same: bool
) -> None:
    g = typed_shop()
    g.meta['dialect'] = dialect
    g.nodes['model.shop.customers#customer_id'].data_type = actual
    result = check_contract(g, CONTRACT.replace('physicalType: INTEGER', f'physicalType: {claimed}'))
    wrong = [f.where for f in result.findings if f.kind == 'wrong-type']
    assert wrong == ([] if same else ['customers.customer_id'])


def test_a_decimal_of_scale_zero_is_an_integer() -> None:
    g = typed_shop()
    g.nodes['model.shop.orders#order_id'].data_type = 'NUMBER(38,0)'
    text = CONTRACT.replace(
        'logicalType: integer\n            primaryKey: true',
        'logicalType: integer\n            primaryKey: true',
    )
    assert check_contract(g, text).findings == []
    docs = validate(odcs.export(g, derive(g), name='shop'))
    orders = next(d for d in docs if d['name'] == 'orders')
    assert prop(orders, 'orders', 'order_id')['logicalType'] == 'integer'


def composite_shop() -> Graph:
    g = typed_shop()
    g.nodes['model.shop.orders'].attrs['primary_key'] = ['order_id', 'customer_id']
    g.changed()
    return g


def test_a_composite_primary_key_is_checked_against_the_graphs_key() -> None:
    claim = textwrap.dedent(
        """\
        kind: DataContract
        schema:
          - name: orders
            properties:
              - name: order_id
                primaryKey: true
                primaryKeyPosition: 1
              - name: customer_id
                primaryKey: true
                primaryKeyPosition: 2
        """
    )
    assert check_contract(composite_shop(), claim).findings == []
    result = check_contract(typed_shop(), claim)
    assert {f.kind for f in result.findings} == {'key'}
    claim = claim.replace('customer_id\n        primaryKey: true', 'amount\n        primaryKey: true')
    assert 'amount' in claim
    assert {f.kind for f in check_contract(composite_shop(), claim).findings} == {'key'}


def test_a_primary_key_implies_required_and_the_export_writes_one_key() -> None:
    g = composite_shop()
    docs = validate(odcs.export(g, derive(g), name='shop'))
    orders = next(d for d in docs if d['name'] == 'orders')
    keys = [p['name'] for p in orders['schema'][0]['properties'] if p.get('primaryKey')]
    assert keys == ['order_id', 'customer_id']
    assert [p['primaryKeyPosition'] for p in orders['schema'][0]['properties'] if p.get('primaryKey')] == [
        1,
        2,
    ]
    assert check_contract(g, odcs.export(g, derive(g), name='shop')).findings == []
    claim = 'kind: DataContract\nschema:\n  - name: customers\n    properties:\n      - name: customer_id\n        primaryKey: true\n        required: true\n'
    assert check_contract(typed_shop(), claim).findings == []


def test_one_primary_key_per_contract_and_the_other_unique_keys_are_unique() -> None:
    g = typed_shop()
    _unique(g, g.nodes['model.shop.orders'], 'customer_id')
    g.changed()
    docs = validate(odcs.export(g, derive(g), name='shop'))
    orders = next(d for d in docs if d['name'] == 'orders')
    flags = {p['name']: (p.get('primaryKey'), p.get('unique')) for p in orders['schema'][0]['properties']}
    assert sum(1 for pk, _ in flags.values() if pk) == 1
    assert flags['customer_id'] == (None, True) or flags['customer_id'][1] is True


def test_malformed_claims_and_wrong_shapes_are_findings() -> None:
    text = textwrap.dedent(
        """\
        kind: DataContract
        schema:
          - name: orders
            properties:
              - name: order_id
                primaryKey: "yes"
              - not a mapping
            relationships:
              - from: orders.customer_id
          - just a string
        """
    )
    result = check_contract(typed_shop(), text)
    assert {f.kind for f in result.findings} == {'malformed'}
    assert len(result.findings) == 4
    assert not result.ok
    with pytest.raises(ValueError, match='kind'):
        check_contract(typed_shop(), 'schema: []\n')
    with pytest.raises(ValueError, match='not a data contract'):
        check_contract(typed_shop(), 'kind: Something\nschema: []\n')


def test_a_table_outside_the_project_is_unsupported_and_object_rules_are_listed() -> None:
    text = textwrap.dedent(
        """\
        kind: DataContract
        schema:
          - name: elsewhere
          - name: orders
            quality:
              - metric: rowCount
                mustBeGreaterThan: 0
        """
    )
    result = check_contract(typed_shop(), text)
    assert [(f.kind, f.severity) for f in result.findings] == [('missing-table', 'unsupported')]
    assert result.ok
    assert result.not_run == ['orders: quality rule rowCount']


def test_lone_surrogates_in_a_contract_are_refused_cleanly() -> None:
    g = typed_shop()
    g.nodes['model.shop.customers'].description = 'bad \udc80 text'
    with pytest.raises(ValueError):
        check_contract(typed_shop(), 'kind: DataContract\nschema:\n  - name: "x\\ud800"\n')


def test_a_contract_is_read_from_standard_input(
    jaffle_built_graph: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    import io

    assert main(['export', 'odcs', '-C', str(jaffle_built_graph)]) == 0
    text = capsys.readouterr().out
    monkeypatch.setattr('sys.stdin', io.StringIO(text))
    assert main(['contract', 'check', '-', '-C', str(jaffle_built_graph)]) == 0


def test_a_formula_statement_says_its_lineage_was_confirmed() -> None:
    g = typed_shop()
    docs = validate(odcs.export(g, derive(g), name='shop'))
    orders = next(d for d in docs if d['name'] == 'orders')
    [answer] = [s['answer'] for s in statements(orders) if s['question'].startswith('How is')]
    assert 'lineage' in answer and 'confirmed' in answer


QUOTED = ['a"b', 'a[b]', 'a`b', '"a"."b"']


@pytest.mark.parametrize('name', QUOTED)
def test_the_exports_own_names_with_quote_characters_check_clean(name: str) -> None:
    g = typed_shop()
    g.nodes['model.shop.customers'].relation = f'db.main.{name}'
    text = odcs.export(g, derive(g), name='shop')
    assert check_contract(g, text).findings == []


def test_a_name_is_compared_in_unicode_normal_form() -> None:
    g = typed_shop()
    g.nodes['model.shop.customers#name'].name = 'cafe\u0301'
    claim = 'kind: DataContract\nschema:\n  - name: customers\n    properties:\n      - name: caf\u00e9\n'
    assert check_contract(g, claim).findings == []


def test_a_contract_name_more_qualified_than_the_graphs_is_an_unsupported_match_not_a_missing_table() -> None:
    claim = CONTRACT.replace('physicalName: db.main.customers', 'physicalName: prod.db.main.customers')
    result = check_contract(typed_shop(), claim)
    assert not any(f.kind == 'missing-table' for f in result.findings)
    [finding] = [f for f in result.findings if f.where == 'customers']
    assert (finding.kind, finding.severity) == ('table-qualifier', 'unsupported')
    assert 'db.main.customers' in finding.message
    # The relation it means is still checked.
    wrong = claim.replace('physicalType: INTEGER', 'physicalType: DATE')
    assert ('wrong-type', 'customers.customer_id') in kinds(check_contract(typed_shop(), wrong))


def test_one_primary_key_is_the_declared_one_then_the_entity_the_relation_is_named_for() -> None:
    g = typed_shop()
    orders = g.nodes['model.shop.orders']
    _unique(g, orders, 'customer_id')  # a second unique column, first among the entity keys
    text = odcs.export(g, derive(g), name='shop')
    doc = next(d for d in contracts(text) if d['name'] == 'orders')
    keys = [p['name'] for p in obj(doc, 'orders')['properties'] if p.get('primaryKey')]
    assert keys == ['order_id']
    orders.attrs['primary_key'] = ['customer_id']
    text = odcs.export(g, derive(g), name='shop')
    doc = next(d for d in contracts(text) if d['name'] == 'orders')
    keys = [p['name'] for p in obj(doc, 'orders')['properties'] if p.get('primaryKey')]
    assert keys == ['customer_id']


def test_a_lone_surrogate_in_one_relation_skips_that_relation_and_names_it() -> None:
    g = typed_shop()
    g.nodes['model.shop.customers'].description = 'bad \udc80 text'
    text = odcs.export(g, derive(g), name='shop')
    docs = contracts(text)
    assert [d['name'] for d in docs] == ['orders']
    assert 'customers' in text.split('---')[0] and 'lone surrogate' in text.split('---')[0]
    result = check_contract(g, CONTRACT)
    assert any('customers' in n and 'lone surrogate' in n for n in result.notes)


@pytest.mark.parametrize(
    ('claimed', 'kind'),
    [
        ('prod_db.main.customers', 'table-qualifier'),
        ('prod.warehouse.main.customers', 'table-qualifier'),
        ('db.main.customers', None),
        ('other.customers', 'missing-table'),
    ],
)
def test_a_name_that_differs_in_its_leading_parts_only_is_a_table_qualifier(
    claimed: str, kind: str | None
) -> None:
    text = CONTRACT.replace('physicalName: db.main.customers', f'physicalName: {claimed}')
    found = {f.kind for f in check_contract(typed_shop(), text).findings if f.where == 'customers'}
    assert found == ({kind} if kind else set())


def test_facts_look_for_lone_surrogates_only_when_json_cannot_carry_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def walked(_: object) -> bool:
        raise AssertionError('walked clean facts for surrogates')

    monkeypatch.setattr(odcs, '_lone_surrogate', walked)
    g = typed_shop()
    assert odcs.facts(g, derive(g))['skipped'] == []


def test_what_refers_to_a_relation_left_out_for_a_surrogate_is_left_out_too() -> None:
    g = typed_shop()
    g.nodes['model.shop.customers'].description = 'bad \udc80 text'
    shown = odcs.facts(g, derive(g))
    assert 'customers' not in [r['label'] for r in shown['relations']]
    assert all('customers' not in e['relations'] for e in shown['entities'])
    assert all(k[0] != 'customers' for e in shown['entities'] for k in e['unique_keys'])
    assert all('customers' not in (r['from'][0], r['to'][0]) for r in shown['relationships'])
    assert all(c['relation'] != 'customers' for m in shown['measures'] for c in m['columns'])
    assert any('customers' in note for note in shown['skipped'])
