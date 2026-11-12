"""The ontology as ODCS 3.2 data contracts (Open Data Contract Standard, Bitol).

One contract per model that holds an entity or a measure, as a stream of YAML documents. The
engine is Rust (``src/odcs/``): this module gathers the facts it needs from the graph and the
ontology, and hands them over as one JSON document.

A fact is written in the field of the contract that says it, with the grade it earned
(``verified``, ``declared`` or ``inferred``). The contract's ``context`` block lists *verified
statements* only where Konstruct holds evidence, and each statement names it: a dbt test, or a
``konstruct verify`` result. An inferred fact is never one; it carries its grade in
``customProperties``. Quality rules are written as ODCS's predefined rules and SQL for other tools
to run: Konstruct never runs them, and nothing here claims a contract is enforced.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Collection
from typing import Any

import sqlglot
from sqlglot import exp

from konstruct import _core
from konstruct.core.graph import RELATION_KINDS, Graph
from konstruct.core.sql import PARSE_ERRORS
from konstruct.semantics import ossie
from konstruct.semantics.ontology import Ontology, link_key


def _aggregates(graph: Graph) -> Callable[[str | None], bool | None]:
    """Whether a SQL expression aggregates rows, by the one reading of it (``ossie.aggregate``); None
    when there is no expression or it does not parse."""
    dialect = graph.meta.get('dialect')
    cache: dict[str, bool | None] = {}

    def aggregates(expression: str | None) -> bool | None:
        if not expression:
            return None
        if expression not in cache:
            try:
                tree = sqlglot.parse_one(expression, read=dialect if isinstance(dialect, str) else None)
                cache[expression] = ossie.aggregate(tree) if isinstance(tree, exp.Expression) else None
            except PARSE_ERRORS:
                cache[expression] = None
        return cache[expression]

    return aggregates


_TYPE = exp.DataType.Type
_TIMESTAMPS = (_TYPE.TIMESTAMPNTZ, _TYPE.TIMESTAMP, _TYPE.DATETIME)
_SIZED = (_TYPE.VARCHAR, _TYPE.CHAR, _TYPE.NCHAR, _TYPE.NVARCHAR, _TYPE.TEXT)
_NESTED = (_TYPE.ARRAY, _TYPE.MAP, _TYPE.STRUCT, _TYPE.OBJECT, _TYPE.LIST)


_Key = tuple[str, 'str | list[list[Any]] | None']


def _inner(part: exp.Expr, dialect: str | None) -> list[Any]:
    """One type argument of a nested type, as ``[name, key]``: an element type has no name, a field
    has its name in lower case."""
    if isinstance(part, exp.DataType):
        return [None, list(_key(part, dialect))]
    kind = part.args.get('kind')
    if isinstance(part, exp.ColumnDef) and isinstance(kind, exp.DataType):
        return [part.name.lower(), list(_key(kind, dialect))]
    return [None, [part.sql(), None]]


def _key(tree: exp.DataType, dialect: str | None) -> _Key:
    kind = tree.this
    if not isinstance(kind, _TYPE):
        raise ValueError(str(kind))
    base: str = kind.value
    params: str | list[list[Any]] | None = None
    parts = tree.expressions
    if kind == _TYPE.DECIMAL:
        sizes = [e.sql() for e in parts]
        # DECIMAL(p) is DECIMAL(p, 0).
        params = ','.join(sizes if len(sizes) != 1 else [*sizes, '0']) or None
    elif kind in _SIZED:
        params = parts[0].sql() if parts and parts[0].sql().isdigit() else None
    elif kind in _NESTED and parts:
        params = [_inner(p, dialect) for p in parts]
    if dialect == 'snowflake':
        if kind in exp.DataType.INTEGER_TYPES:
            base, params = 'DECIMAL', '38,0'
        elif kind in (_TYPE.FLOAT, _TYPE.DOUBLE):
            base = 'DOUBLE'
        elif kind in _TIMESTAMPS:
            base = 'TIMESTAMP'
        elif kind in (_TYPE.CHAR, _TYPE.NCHAR):
            base = 'VARCHAR'
    elif dialect == 'bigquery':
        if kind in exp.DataType.INTEGER_TYPES:
            base = 'INT64'
        elif kind in (_TYPE.FLOAT, _TYPE.DOUBLE):
            base = 'FLOAT64'
    elif dialect in ('databricks', 'spark') and kind in (_TYPE.TIMESTAMPTZ, _TYPE.TIMESTAMPLTZ):
        # Spark's TIMESTAMP is the one with a session time zone.
        base = 'TIMESTAMPLTZ'
    # The spellings every dialect shares, whatever else it folds.
    if base in ('TEXT', 'NVARCHAR'):
        base = 'VARCHAR'
    elif base in ('TIMESTAMPNTZ', 'DATETIME'):
        base = 'TIMESTAMP'
    return base, params


def type_key(written: str, dialect: str | None) -> list[Any]:
    """A warehouse type as ``dialect`` reads it, as ``[type, parameters]``: two spellings of one
    type give one key. ``parameters`` is a decimal's precision and scale (``'38,0'``), a string's
    length, the keys of the element or field types of a nested type (each ``[name, key]``), or None. Snowflake has one integer,
    ``NUMBER(38,0)``, one float and one ``TIMESTAMP``; BigQuery has ``INT64`` and ``FLOAT64``; a
    type sqlglot cannot read stands for its own name. Two keys are one type when their types are
    and their parameters are, or either has none."""
    try:
        base, params = _key(exp.DataType.build(written, dialect=dialect), dialect)
    except (ValueError, *PARSE_ERRORS):  # type: ignore[misc]
        return [written.strip().upper(), None]
    return [base, params]


def _lone_surrogate(value: Any) -> bool:
    """Whether ``value`` holds text JSON cannot carry to the engine: a lone surrogate."""
    if isinstance(value, str):
        return not value.isascii() and any('\ud800' <= c <= '\udfff' for c in value)
    if isinstance(value, dict):
        return any(_lone_surrogate(k) or _lone_surrogate(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(_lone_surrogate(v) for v in value)
    return False


def facts(
    graph: Graph,
    ontology: Ontology,
    name: str | None = None,
    measures: Collection[str] = (),
    types: Collection[str] = (),
) -> dict[str, Any]:
    """What the contracts are made from, as the engine reads it: every relation with its columns,
    tests and verify results, and the ontology's entities, measures and relationships.

    ``measures`` are the (lower-case) names of columns a contract claims to be measures: whether
    each aggregates is worked out for those, and for the columns that hold a measure. ``types`` are
    the warehouse types a contract claims; each, and every column's, is read in the graph's dialect."""
    tests = ontology.tests
    integrity = graph.meta.get('integrity')
    keys = integrity.get('keys', {}) if isinstance(integrity, dict) else {}
    links = integrity.get('links', {}) if isinstance(integrity, dict) else {}
    aggregates = _aggregates(graph)
    # The composed formula belongs to the column that holds the measure.
    formulas: dict[str, str] = {
        held.id: held.formula
        for measure in ontology.measures
        for held in measure.columns
        if held.id and held.formula
    }
    wanted = {m.lower() for m in measures}

    def place(column_id: str) -> list[str] | None:
        column = graph.nodes.get(column_id)
        parent = graph.parent_of(column) if column is not None else None
        return [graph.label(parent), column.name] if column is not None and parent is not None else None

    written: set[str] = set(types)
    relations: list[dict[str, Any]] = []
    for relation in graph.nodes.values():
        if relation.kind not in RELATION_KINDS:
            continue
        columns = []
        for column in graph.columns_of(relation.id):
            if column.data_type:
                written.add(column.data_type)
            checked = keys.get(column.id)
            check = (
                {'state': checked.get('state'), 'detail': str(checked.get('detail', ''))}
                if isinstance(checked, dict) and checked.get('state') in ('confirmed', 'failed')
                else None
            )
            expression = column.attrs.get('expression')
            expression = expression if isinstance(expression, str) else None
            formula = formulas.get(column.id)
            aggregate = (
                aggregates(formula or expression) if formula or column.name.lower() in wanted else None
            )
            columns.append(
                {
                    'name': column.name,
                    'type': column.data_type,
                    'description': column.description,
                    'unique': column.id in ontology.unique_ids or (check or {}).get('state') == 'confirmed',
                    'unique_tests': tests.unique.get(column.id, []),
                    'not_null_tests': tests.not_null.get(column.id, []),
                    'check': check,
                    # Verify found nulls: a failed check whose detail counts them.
                    'nulls': bool(
                        isinstance(checked, dict)
                        and (checked.get('nulls') or 'null' in str(checked.get('detail', '')).lower())
                        and checked.get('state') == 'failed'
                    ),
                    'expression': expression,
                    'formula': formula,
                    'aggregate': aggregate,
                }
            )
        primary_key = relation.attrs.get('primary_key')
        relations.append(
            {
                'label': graph.label(relation),
                'physical': relation.relation,
                'kind': relation.kind,
                'materialized': relation.attrs.get('materialized'),
                'description': relation.description,
                'primary_key': [str(k) for k in primary_key] if isinstance(primary_key, list) else [],
                'columns': columns,
            }
        )
    entities: list[dict[str, Any]] = [
        {
            'name': entity.name,
            'term': entity.term,
            'confidence': entity.confidence,
            'relations': entity.relations,
            'unique_keys': [
                p for i in entity.key_ids if i in ontology.unique_ids and (p := place(i)) is not None
            ],
        }
        for entity in ontology.entities
    ]
    held_measures: list[dict[str, Any]] = []
    for measure in ontology.measures:
        held_columns = []
        for held in measure.columns:
            where = place(held.id) if held.id else None
            if where is not None:
                held_columns.append(
                    {
                        'relation': where[0],
                        'column': where[1],
                        'formula': held.formula,
                        'sources': held.sources,
                        'complete': held.complete,
                        'verified': held.verified,
                        'edges': held.edges,
                        'aggregate': aggregates(held.formula),
                    }
                )
        held_measures.append(
            {'name': measure.name, 'confidence': measure.confidence, 'columns': held_columns}
        )
    relationships: list[dict[str, Any]] = []
    for link in ontology.relationships:
        if link.columns is None:
            continue
        source, target = place(link.columns[0]), place(link.columns[1])
        if source is None or target is None:
            continue
        found = links.get(link_key(*link.columns))
        relationships.append(
            {
                'from': source,
                'to': target,
                'cardinality': link.cardinality,
                'confidence': link.confidence,
                'check': link.check,
                'verify': [e for e in link.evidence if e.startswith('konstruct verify: ')],
                'tests': tests.links.get(link.columns, []),
                'one_to_one_confirmed': isinstance(found, dict) and found.get('one_to_one') == 'confirmed',
            }
        )
    # A dbt relationships test between relations that hold no entity has no ontology relationship:
    # it is a declared link all the same, with what verify said of it.
    derived = {link.columns for link in ontology.relationships}
    for ends, test_ids in tests.links.items():
        source, target = place(ends[0]), place(ends[1])
        if ends in derived or source is None or target is None:
            continue
        found = links.get(link_key(*ends))
        said: dict[str, Any] = found if isinstance(found, dict) else {}
        state = said.get('state')
        relationships.append(
            {
                'from': source,
                'to': target,
                'cardinality': None,
                'confidence': 'declared',
                'check': state if state in ('confirmed', 'failed') else None,
                'verify': [f'konstruct verify: {said["detail"]}'] if state and 'detail' in said else [],
                'tests': test_ids,
                'one_to_one_confirmed': said.get('one_to_one') == 'confirmed',
            }
        )
    dialect = graph.meta.get('dialect')
    result: dict[str, Any] = {
        'project': name,
        'skipped': [],
        'types': {t: type_key(t, dialect if isinstance(dialect, str) else None) for t in sorted(written)},
        'relations': relations,
        'entities': entities,
        'measures': held_measures,
        'relationships': relationships,
    }
    try:
        json.dumps(result, ensure_ascii=False).encode('utf-8')
    except UnicodeEncodeError:
        return _without_surrogates(result)
    return result


def _without_surrogates(result: dict[str, Any]) -> dict[str, Any]:
    """``result`` as JSON can carry it to the engine, which a lone surrogate defeats: a relation
    whose text holds one is left out and named, and so is whatever refers to it (its place in an
    entity, a relationship to it, a measure's column in it) or holds one itself."""
    kept = dict(result)
    gone = {r['label'] for r in result['relations'] if _lone_surrogate(r)}
    kept['skipped'] = [
        f'relation {label!a}: its text holds a lone surrogate, which is not valid Unicode'
        for label in sorted(gone)
    ]
    kept['types'] = {t: k for t, k in result['types'].items() if not _lone_surrogate(t)}
    kept['relations'] = [r for r in result['relations'] if r['label'] not in gone]
    kept['relationships'] = [
        r
        for r in result['relationships']
        if r['from'][0] not in gone and r['to'][0] not in gone and not _lone_surrogate(r)
    ]
    kept['measures'] = [
        {**m, 'columns': [c for c in m['columns'] if c['relation'] not in gone and not _lone_surrogate(c)]}
        for m in result['measures']
        if not _lone_surrogate([m['name'], m['confidence']])
    ]
    kept['entities'] = [
        {
            **e,
            'relations': [r for r in e['relations'] if r not in gone and not _lone_surrogate(r)],
            'unique_keys': [k for k in e['unique_keys'] if k[0] not in gone and not _lone_surrogate(k)],
        }
        for e in result['entities']
        if not _lone_surrogate([e['name'], e['term'], e['confidence']])
    ]
    return kept


def export(graph: Graph, ontology: Ontology, name: str | None = None) -> str:
    """The ontology as ODCS 3.2 contracts, a stream of YAML documents.

    Args:
        graph: The graph the ontology was derived from.
        ontology: The ontology.
        name: The project's name, in each contract's id (``urn:konstruct:<name>:<model>``).

    Raises:
        ValueError: No model holds an entity or a measure.
    """
    return _core.odcs_export(json.dumps(facts(graph, ontology, name)))
