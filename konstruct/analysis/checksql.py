"""`konstruct check-sql`: an ad-hoc query checked against what the project defines.

An agent or an analyst writes SQL to answer a business question; this module reads it, without
running it, and says where it disagrees with the project. Every finding must be one an analyst
would accept: an agent told that its correct SQL is wrong will break it. A check never claims a
filter is applied unless it can show it is, and when it cannot show it, it reports the filter
missing, and says so when what it cannot show is the graph's (one an earlier Konstruct built).

The query is qualified once and read scope by scope. Each output column that aggregates is a
*measure*: the aggregate that produces it (followed through CTEs, subqueries, wrappers such as
``ROUND`` or ``COALESCE(..., 0)``, each branch of a UNION and each scalar subquery), the rows it
reads (the WHERE, ON and HAVING clauses on the way, the condition of a ``FILTER`` or ``CASE`` inside
the aggregate, and the filters that hold on every source row of the columns it reads in the graph,
as :meth:`Checker.held_filters` gives them) and its formula
composed down to source columns, as :mod:`konstruct.analysis.conflicts` composes the graph's. The
checks:

* **definitions**: a measure computed without a filter its term in ``definitions.yml`` (or an
  ontology pack) requires; a measure named for a term, or for a column of the project, that
  computes it differently, leaves out a filter the column applies or excludes rows the column
  keeps; a measure recomputed that a column already holds; a column of a retired term. A term
  matches only an output named exactly for it or a declared synonym, and a match by name alone is
  never an error; a measure named for a term that reads only other terms' columns;
  a column that computes a measure term's formula over some of its rows needs the term's filters;
* **units**: a result still in the minor units of a currency (cents) of a column the project reads
  in whole units, and a filter comparing such a column with the value a definition requires of it
  in whole units;
* **joins**: a sum, count or average over a join that repeats the rows it aggregates (from the
  keys, unique tests, composite and grouped keys and relationships of the derived ontology), unless
  a measure's definition binds a copy of the value on rows of the grain it repeats on; a result that repeats a measure of a coarser grain on every row of a
  finer one; two related relations joined on columns their relationship does not use; a join
  nothing vouches for;
* **names**: a relation or column the project does not have.

Each finding carries its fix. Where a fix changes the measure and no other column of the result
(a filter added where only the measure reads the rows it removes, or written into the measure's
aggregate otherwise; a join put back on its relationship's columns; a retired column swapped for
its replacement under the same output name), the result carries the corrected SQL. A filter is
not added where the measure ranks or limits the rows (QUALIFY, ORDER BY with LIMIT, a window over
it), since the rows kept would change too.

The same checks run over warehouse query history (:func:`check_history`): queries that compute an
approved measure differently are shadow metrics, counted by queries, runs and people.
"""

from __future__ import annotations

import difflib
import hashlib
import hmac
import math
import re
import secrets
from collections import defaultdict
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import sqlglot
from sqlglot import exp
from sqlglot.dialects.dialect import Dialect
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, build_scope
from sqlglot.schema import MappingSchema

from konstruct.analysis.conflicts import top_function
from konstruct.analysis.explain import Explainer
from konstruct.core.graph import Graph, Node
from konstruct.core.sql import (
    PARSE_ERRORS,
    Canonical,
    StatementLineage,
    analyse,
    build_schema,
    normal_condition,
    split_conjuncts,
    table_key,
    without_readings,
)
from konstruct.inputs.usage import HistoryQuery, QueryHistory
from konstruct.semantics.ontology import (
    Relationship,
    derivation,
    enforced_key,
    entity_name,
    id_like,
    name_matches,
)

if TYPE_CHECKING:
    from konstruct.interfaces.layout import Style

SEVERITIES = ('error', 'warning', 'info')

RULES: dict[str, str] = {
    'missing-filter': 'A measure is computed without a filter its definition, or the column that holds it, applies.',
    'formula-differs': 'A column computes a measure differently from the column that holds it.',
    'recomputes-measure': 'A column recomputes a measure that a column of the project already holds.',
    'retired-column': 'The query reads a column bound to a retired term.',
    'conflicting-column': "A measure named for a term reads only other terms' columns.",
    'fan-out': 'A sum, count or average runs over a join that repeats the rows it aggregates.',
    'mixed-grain': 'The result repeats a measure of a coarser grain on every row of a finer one.',
    'minor-units': 'A result in hundredths (cents) of the unit the project reads its column in.',
    'join-off-relationship': 'Two related relations are joined on columns their relationship does not use.',
    'unknown-join': 'A join that no relationship or unique key in the project vouches for.',
    'presumed-key': 'A join the check took as safe because an ontology pack presumes a column unique.',
    'unknown-relation': 'The query reads a relation the project does not have.',
    'unknown-column': 'The query reads a column its relation does not have.',
    'unparsed': 'The SQL could not be parsed.',
    'unchecked': 'A statement could be parsed but not analysed, so it was not checked.',
}
"""Every rule, with what it finds."""

SHADOW_RULES = frozenset({'missing-filter', 'formula-differs'})
"""The rules that make a query in history a shadow metric: it computes a measure differently."""

_FILTER_LABELS = ('WHERE', 'HAVING', 'QUALIFY')
_QUALIFY_ERRORS: tuple[type[Exception], ...] = (*PARSE_ERRORS, KeyError, IndexError, AttributeError)
_ANALYSIS_ERRORS: tuple[type[Exception], ...] = (*_QUALIFY_ERRORS, AssertionError)
_NOT_ADDITIVE = (exp.Min, exp.Max)
_RANGES = (exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between)
_MOST_NAMED = 20
"""A name more columns than this share is too generic to stand for one idea."""
_VERBS = {'SUM': 'adds', 'AVG': 'averages', 'COUNT': 'counts'}
_TOP_AGGREGATE = re.compile(r'(?:SUM|AVG|COUNT)\(', re.IGNORECASE)
_AGGREGATE_CALL = re.compile(
    r'\b(?:SUM|AVG|COUNT|COUNT_IF|COUNTIF|MIN|MAX|ANY_VALUE|ARRAY_AGG|STRING_AGG|LISTAGG|MEDIAN|'
    r'STDDEV\w*|VARIANCE|VAR_\w+|BOOL_\w+|APPROX_\w+)\s*\(',
    re.IGNORECASE,
)
_WINDOW = re.compile(r'\bOVER\s*\(', re.IGNORECASE)
_MINOR_WORDS = frozenset({'cent', 'cents', 'pence', 'penny', 'pennies', 'minor'})
"""Words of a column name that say it holds minor units of a currency."""
_FRACTIONS = {100: 'hundredths', 1000: 'thousandths'}
"""The scales a column in minor units is read at, with what its values are of the whole unit."""
_COMPARISONS: dict[type[exp.Expr], type[exp.Expr]] = {
    exp.GT: exp.LT,
    exp.GTE: exp.LTE,
    exp.LT: exp.GT,
    exp.LTE: exp.GTE,
    exp.EQ: exp.EQ,
    exp.NEQ: exp.NEQ,
}
"""Each comparison, with the one it becomes when its sides swap."""


@dataclass
class Finding:
    """One thing the query gets wrong, or may, with what to do about it."""

    rule: str
    """One of :data:`RULES`."""
    severity: str
    """``error`` (an approved definition is broken), ``warning`` or ``info``."""
    message: str
    """What is wrong."""
    fix: str
    """What to change."""
    column: str | None = None
    """The output column it concerns, when there is one."""
    term: str | None = None
    """The business term it concerns, when there is one."""
    detail: str | None = None
    """The filter that is missing or extra, or the formula the query uses: what groups findings
    in history."""
    rewrite: str | None = None
    """The change made in the corrected SQL, when the fix was safe to make."""
    evidence: list[str] = field(default_factory=list)
    """Why: the definitions, keys, tests and relationships the finding rests on."""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SqlCheck:
    """The result of checking one piece of SQL against the project."""

    sql: str
    """The SQL as given."""
    findings: list[Finding] = field(default_factory=list)
    """Everything found, errors first."""
    corrected: str | None = None
    """The SQL with every safe fix applied, when there was one to apply."""
    statements: int = 0
    """Statements checked."""
    terms: list[str] = field(default_factory=list)
    """Business terms the query reads through their own columns."""
    dialect: str | None = None
    """The SQL dialect the query was read in."""

    def count(self, severity: str) -> int:
        return sum(1 for f in self.findings if f.severity == severity)

    @property
    def checked(self) -> bool:
        """Whether every statement was analysed: one that parses but cannot be analysed has an
        ``unchecked`` note, and makes this false."""
        return not any(f.rule == 'unchecked' for f in self.findings)

    @property
    def passed(self) -> bool:
        """Whether every statement was checked and nothing breaks an approved definition (no
        finding of severity ``error``): a statement never analysed does not pass."""
        return self.checked and self.count('error') == 0

    def failed(self, *, strict: bool = False) -> bool:
        """Whether the check fails: on an error, or with ``strict`` on a warning as well."""
        return not self.passed or (strict and self.count('warning') > 0)

    def summary(self) -> str:
        if not self.findings:
            return f'No findings in {_plural(self.statements, "statement")}.'
        parts = [
            _plural(self.count('error'), 'error'),
            _plural(self.count('warning'), 'warning'),
            _plural(self.count('info'), 'note'),
        ]
        return f'{", ".join(parts)} in {_plural(self.statements, "statement")}.'

    def text(self, style: Style | None = None) -> str:
        """The result as the CLI prints it: plain text, as ``str()`` gives, or drawn for a
        terminal with ``style``."""
        from konstruct.interfaces.render import show, sql_check_lines

        return show(sql_check_lines, self, style)

    def __str__(self) -> str:
        return self.text()

    def markdown(self) -> str:
        """The result as markdown, for a pull request comment or an agent's notes."""
        lines = [f'**{self.summary()}**']
        if self.findings:
            lines += ['', '| Severity | Rule | Finding | Fix |', '|---|---|---|---|']
            for finding in self.findings:
                where = f'`{finding.column}`: ' if finding.column else ''
                message = f'{where}{finding.message}'.replace('|', '\\|')
                fix = finding.fix.replace('|', '\\|')
                lines.append(f'| {finding.severity} | `{finding.rule}` | {message} | {fix} |')
        if self.corrected:
            lines += ['', '```sql', self.corrected, '```']
        return '\n'.join(lines) + '\n'

    def to_dict(self) -> dict[str, Any]:
        return {
            'passed': self.passed,
            'checked': self.checked,
            'summary': self.summary(),
            'statements': self.statements,
            'dialect': self.dialect,
            'terms': self.terms,
            'findings': [f.to_dict() for f in self.findings],
            'corrected': self.corrected,
        }


_PLURALS = {'query': 'queries', 'person': 'people'}


def _plural(count: int, word: str) -> str:
    return f'{count} {word if count == 1 else _PLURALS.get(word, word + "s")}'


def _capped(severity: str) -> str:
    """A severity no stronger than a warning: what a match by name alone can claim."""
    return 'warning' if severity == 'error' else severity


# The project as the checks see it


@dataclass
class _Term:
    node: Node
    status: str
    measure: bool
    """Whether the ontology derives a measure from it, with a formula to compare."""
    bound: list[Node]
    roots: list[Node]
    """The bound columns not derived from another bound column: the ones that define it."""

    @property
    def name(self) -> str:
        return self.node.name

    @property
    def names(self) -> list[str]:
        return [self.node.name, *self.node.attrs.get('synonyms', [])]

    @property
    def severity(self) -> str:
        return 'error' if self.status == 'approved' else 'warning'

    @property
    def where(self) -> str:
        line = self.node.attrs.get('line')
        return f'{self.node.path}:{line}' if self.node.path and line else self.node.path or self.node.id


@dataclass
class _Condition:
    """One conjunct of a filter, normalised so equal filters compare equal however they are written."""

    text: str
    """As shown to people."""
    key: str
    """Columns replaced by the column whose values they copy, then put in one form
    (:func:`konstruct.core.sql.normal_condition`)."""
    tree: exp.Expr
    """As written, for rewriting."""
    columns: dict[str, Node]
    """Each column in ``tree`` (by its SQL) to the graph column it names."""
    label: str = 'WHERE'
    """The clause it comes from."""
    source: str | None = None
    """Where it is applied: the term that requires it, or the relation that applies it."""
    written: bool = False
    """Whether the query itself applies it, rather than the route of a column it reads."""
    own: bool = True
    """Whether it filters the relations the measure's own columns come from, rather than a relation
    joined to them."""
    opposite: str = ''
    """The key of the condition that keeps exactly the rows this one leaves out."""
    limited: frozenset[str] = frozenset()
    """For a filter a model applies on the nullable side of an outer join (``LEFT JOIN WHERE``, a
    filter in a ``LEFT JOIN``'s ON), the relations whose rows alone it restricts; empty for one that
    restricts every row."""


@dataclass
class _Form:
    """A formula composed down to source columns, without value-preserving wrappers and with the
    condition written into its aggregate taken out as a filter."""

    formula: str | None
    sources: frozenset[str]
    complete: bool
    conditions: list[_Condition] = field(default_factory=list)


@dataclass
class _Fan:
    """Why the rows of one source of a scope repeat."""

    relation: str
    by: str
    evidence: str
    pre_aggregate: str | None = None
    severity: str = 'warning'
    certain: bool = False
    """Whether the SQL itself repeats the rows (an UNNEST), rather than nothing showing it does not."""
    """``info`` when only the absence of a known key on the other side suggests it."""


@dataclass
class _Minor:
    """How a column in minor units of a currency is read in whole units."""

    scale: int
    """What the project divides it by."""
    reader: Node
    """A column of the project that reads it divided by ``scale``."""
    evidence: list[str]
    """Why it is in minor units: how the project reads it, and what names it or holds it as money."""


@dataclass
class _ScopeInfo:
    unique: list[frozenset[str]] = field(default_factory=list)
    """Sets of output columns known to hold one row per value; an empty set means one row."""
    physical: dict[str, Node] = field(default_factory=dict)
    """Output columns that pass a graph column through unchanged."""
    fanned: dict[str, _Fan] = field(default_factory=dict)
    """Output columns whose rows repeat, with why."""
    grouped: list[frozenset[str]] = field(default_factory=list)
    """The sets of ``unique`` that a DISTINCT or a GROUP BY makes unique, which hold one row for
    NULL too (both put NULLs together), so that IS NOT DISTINCT FROM on them matches one row."""


@dataclass
class _Join:
    """An equality between columns of two sources in one scope."""

    left: str
    left_column: str
    right: str
    right_column: str
    node: exp.Expr
    join: int | None
    """The index of the join whose ON clause holds it, or None for the WHERE clause."""
    conjunct: int


@dataclass
class _Rows:
    """The rows a measure reads: the filters on them and the relations they come from."""

    conditions: dict[str, _Condition] = field(default_factory=dict)
    tables: list[tuple[Scope, str, Node]] = field(default_factory=list)
    """Each scope, alias and relation the rows are read from, where a filter could be added."""
    nearby: set[str] = field(default_factory=set)
    """Columns (by the column they copy) that the query filters on rows tied to the measure's (a
    semi-join, an anti-join, a self-join on another column) without filtering the measure's own."""

    def merge(self, other: _Rows) -> None:
        for key, condition in other.conditions.items():
            self.conditions.setdefault(key, condition)
        self.tables += [t for t in other.tables if t not in self.tables]
        self.nearby |= other.nearby


@dataclass
class _Site:
    """One measure of the query: the aggregate that produces an output column, in its scope."""

    output: str
    scope: Scope
    core: exp.Expr
    aggregates: list[exp.Expr]
    rows: _Rows
    form: _Form
    shown: str | None
    """The formula as people read it."""
    reads: list[Node]
    """The graph columns the aggregate's value reads."""


def _from_pack(relationship: Relationship) -> str:
    """Where a relationship comes from, when an ontology pack alone gives it: the pack bound it by
    the names of the tables, and nothing in the project shows it."""
    packs = [
        e.removeprefix('ontology pack ') for e in relationship.evidence if e.startswith('ontology pack ')
    ]
    if not packs or len(packs) != len(relationship.evidence):
        return ''
    return f', as the {", ".join(sorted(packs))} pack gives it'


class Checker:
    """Checks SQL against one graph; built once, so that many queries (query history) share the
    derived ontology and composed formulas."""

    def __init__(self, graph: Graph) -> None:
        self.graph = graph
        dialect = graph.meta.get('dialect')
        self.dialect = dialect if isinstance(dialect, str) else None
        self.sqlglot = Dialect.get_or_raise(self.dialect)
        # Where a / b never truncates, SUM(x / 100) and SUM(x) / 100 are one formula.
        self.exact = self.dialect is not None and not self.sqlglot.TYPED_DIVISION
        catalog, schema = graph.meta.get('default_catalog'), graph.meta.get('default_schema')
        self.default_catalog = catalog if isinstance(catalog, str) and catalog else None
        self.default_db = schema if isinstance(schema, str) and schema else None
        self.schema = self._schema()
        self.derived = derivation(graph)
        self.ontology = self.derived.ontology
        self.keys: dict[str, str] = dict(self.derived.keys)
        # What a test, a key or an identifier field shows unique, and the identifiers a pack binds
        # as its source's own; never every entity key, which can be a referring column a definition
        # merely lists, shared by many rows.
        self.unique: frozenset[str] = self.derived.unique | frozenset(self.derived.presumed)
        self.explainer = Explainer(graph)
        self.composer = Explainer(graph, qualified=True)
        self.relationships = self.ontology.relationships
        self.by_columns: dict[frozenset[str], Relationship] = {}
        self.by_entities: dict[tuple[str, str], list[Relationship]] = defaultdict(list)
        self.foreign: dict[str, Relationship] = {}
        for rel in self.relationships:
            self.by_entities[(rel.source, rel.target)].append(rel)
            if rel.columns:
                self.by_columns[frozenset(rel.columns)] = rel
                self.foreign.setdefault(rel.columns[0], rel)
        self._upstream: dict[str, set[str]] = {}
        self._origins: dict[str, str] = {}
        self._entities: dict[str, str | None] = {}
        self._virtual = 0
        self._conjuncts: dict[str, list[_Condition]] = {}
        self._held: dict[str, dict[str, _Condition]] = {}
        self._made: dict[str, bool] = {}
        self._aligned: dict[tuple[str, str], bool] = {}
        self._ended: dict[str, frozenset[str]] = {}
        self._untold: dict[str, bool] = {}
        self._forms: dict[str, _Form] = {}
        self._keys: dict[str, list[frozenset[str]]] = {}
        self._filled: dict[str, list[frozenset[str]]] = {}
        self._names: dict[str, list[tuple[int, Node]]] | None = None
        self._named: dict[str, list[Node]] = {}
        self._additive: set[str] | None = None
        self._carriers: dict[str, list[Node]] = {}
        self._slices: dict[str, list[tuple[_Term, Node, list[_Condition]]]] = {}
        self._minor: dict[str, _Minor | None] = {}
        measures = {m.name for m in self.ontology.measures if not m.expression}
        self.terms = self._terms(measures)
        self._term_nodes = {t.node.id: t for t in self.terms}
        self.bound_to: dict[str, list[_Term]] = defaultdict(list)
        for term in self.terms:
            for column in term.bound:
                self.bound_to[column.id].append(term)
        for foreign in list(self.foreign):
            self.foreign.setdefault(self.origin(foreign), self.foreign[foreign])

    # The graph

    def _schema(self) -> dict[str, dict[str, str]]:
        schema: dict[str, dict[str, str]] = {}
        for node in self.graph.relations():
            if node.relation:
                columns = {
                    c.name: c.data_type or 'UNKNOWN'
                    for c in self.graph.columns_of(node.id)
                    if not c.attrs.get('nested') and not c.attrs.get('documented_only')
                }
                if columns:
                    schema[node.relation] = columns
        return schema

    def _terms(self, measures: set[str]) -> list[_Term]:
        terms = []
        for node in sorted(
            (n for n in self.graph.nodes.values() if n.kind == 'term'), key=lambda n: n.name.lower()
        ):
            bound = [
                self.graph.nodes[e.target]
                for e in self.graph.out_edges(node.id, 'defines')
                if e.attrs.get('role') != 'input'
                and e.target in self.graph.nodes
                and self.graph.nodes[e.target].kind == 'column'
            ]
            ids = {c.id for c in bound}
            roots = [c for c in bound if not (self.upstream(c.id) & ids)]
            status = str(node.attrs.get('status', 'draft'))
            terms.append(_Term(node, status, node.name in measures, bound, roots))
        return terms

    def upstream(self, column_id: str) -> set[str]:
        """Every column ``column_id`` is derived from, at any distance."""
        found = self._upstream.get(column_id)
        if found is None:
            node = self.graph.nodes.get(column_id)
            steps = self.graph.trace(node) if node is not None else []
            found = {s.node.id for s in steps if s.node.kind == 'column' and s.node.id != column_id}
            self._upstream[column_id] = found
        return found

    def origin(self, column_id: str) -> str:
        """The column whose values ``column_id`` copies unchanged, through any number of copies."""
        found = self._origins.get(column_id)
        if found is None:
            current, seen = column_id, {column_id}
            while True:
                edges = self.graph.out_edges(current, 'derived_from')
                if len(edges) != 1 or edges[0].attrs.get('transform') != 'direct' or edges[0].target in seen:
                    break
                current = edges[0].target
                seen.add(current)
            found = self._origins[column_id] = current
        return found

    def entity_of(self, relation: Node) -> str | None:
        if relation.id not in self._entities:
            self._entities[relation.id] = self.derived.entity_of(relation)
        return self._entities[relation.id]

    def route(self, column: Node) -> list[Node]:
        """The relation of ``column`` and of every column it is derived from."""
        relations: dict[str, Node] = {}
        for column_id in [column.id, *sorted(self.upstream(column.id))]:
            node = self.graph.nodes.get(column_id)
            parent = self.graph.parent_of(node) if node is not None else None
            if parent is not None:
                relations.setdefault(parent.id, parent)
        return list(relations.values())

    def terms_named(self, output: str) -> list[_Term]:
        """The terms an output column is named for: exactly, or by a declared synonym."""
        return [t for t in self.terms if t.status != 'retired' and name_matches(output, *t.names)]

    def columns_named(self, output: str) -> list[Node]:
        """The computed columns of the graph named as ``output`` is, unless too many share it."""
        found = self._named.get(output)
        if found is None:
            if self._names is None:
                # By name first: only the columns of a name an output has are asked for their
                # edges, which the graph makes as they are asked for.
                self._names = defaultdict(list)
                for position, node in enumerate(self.graph.nodes.values()):
                    if node.kind == 'column' and isinstance(node.attrs.get('expression'), str):
                        self._names[node.name.lower()].append((position, node))
            named = sorted(
                (item for name, items in self._names.items() if name_matches(output, name) for item in items),
                key=lambda item: item[0],
            )
            computed: list[Node] = []
            for _, node in named:
                edges = self.graph.out_edges(node.id, 'derived_from')
                if edges and any(e.attrs.get('transform') != 'direct' for e in edges):
                    computed.append(node)
                    if len(computed) > _MOST_NAMED:
                        break
            # Each name's columns together, the names in the order their first columns come.
            by_name: dict[str, list[Node]] = {}
            for node in computed:
                by_name.setdefault(node.name.lower(), []).append(node)
            found = [n for nodes in by_name.values() for n in nodes]
            self._named[output] = found = found if len(found) <= _MOST_NAMED else []
        return found

    def names_for(self, relation: Node) -> list[str]:
        """What a relation's rows are called: its name, its entity, and the term that names the
        entity with its synonyms."""
        found = [self.graph.label(relation)]
        entity = self.entity_of(relation)
        if entity:
            found.append(entity)
            held = next((e for e in self.ontology.entities if e.name == entity), None)
            term = next((t for t in self.terms if held is not None and t.name == held.term), None)
            if term is not None:
                found += term.names
        return found

    def relation_keys(self, relation: Node) -> list[frozenset[str]]:
        """The sets of column names (lower case) that hold one row each of ``relation``: its unique
        columns, its composite keys, and the columns it groups by when one grouped query builds it."""
        found = self._keys.get(relation.id)
        if found is None:
            found = [
                frozenset({c.name.lower()}) for c in self.graph.columns_of(relation.id) if c.id in self.unique
            ]
            found += [frozenset(k) for k in self.derived.composite.get(relation.id, [])]
            grouped = self._grouped(relation)
            if grouped is not None and grouped[0] not in found:
                found.append(grouped[0])
            self._keys[relation.id] = found
        return found

    def _grouped(self, relation: Node) -> tuple[frozenset[str], frozenset[str]] | None:
        """The output columns ``relation`` groups by, when it is built by one grouped query whose
        grouping columns all pass through: the key they make, and all of them. Grouping by a unique
        key of a source and other columns of that same source is grouping by the key."""
        # Two readings of a relation grouped by the same key group it by that key.
        groups = sorted(
            {
                'GROUP BY: ' + without_readings(str(c).removeprefix('GROUP BY: '), self.sqlglot)
                for c in relation.attrs.get('conditions', []) or []
                if str(c).startswith('GROUP BY: ')
            }
        )
        if len(groups) != 1:
            return None
        tree = self.parse(f'SELECT 1 GROUP BY {groups[0].removeprefix("GROUP BY: ")}')
        group = tree.args.get('group') if isinstance(tree, exp.Select) else None
        if group is None or not group.expressions or _subtotals(group):
            return None
        passed = {self.origin(c.id): c.name.lower() for c in self.graph.columns_of(relation.id)}
        items: list[tuple[Node, str]] = []
        for item in group.expressions:
            node = self.column_at([p.name for p in item.parts]) if isinstance(item, exp.Column) else None
            name = passed.get(self.origin(node.id)) if node is not None else None
            if node is None or name is None:
                return None
            items.append((node, name))
        # A model that groups in a CTE and joins the groups back to detail rows does not hold one
        # row per group: every column of one that does is a grouping column or an aggregate.
        grouping = {name for _, name in items}
        for column in self.graph.columns_of(relation.id):
            expression = column.attrs.get('expression')
            aggregated = isinstance(expression, str) and bool(_AGGREGATE_CALL.search(expression))
            if column.name.lower() not in grouping and not (
                aggregated and not _WINDOW.search(str(expression))
            ):
                return None
        for node, name in items:
            if node.id in self.unique and all(n.parent == node.parent for n, _ in items):
                return frozenset({name}), frozenset(grouping)
        return frozenset(grouping), frozenset(grouping)

    def filled_keys(self, relation: Node) -> list[frozenset[str]]:
        """The keys of ``relation`` that hold one row each and no row holds NULL in: a unique
        column a ``not_null`` test or an enforced primary key also covers, an enforced composite
        primary key, and every column its one grouped query groups by (which keeps one row for
        NULL). A key only an ontology pack presumes is not one."""
        found = self._filled.get(relation.id)
        if found is None:
            not_null = self.derived.ontology.tests.not_null
            primary = enforced_key(self.graph, relation)
            found = [
                frozenset({c.name.lower()})
                for c in self.graph.columns_of(relation.id)
                if c.id in self.derived.unique and (c.id in not_null or c.name.lower() in primary)
            ]
            if len(primary) > 1:
                found.append(frozenset(primary))
            grouped = self._grouped(relation)
            if grouped is not None and grouped[1] not in found:
                found.append(grouped[1])
            self._filled[relation.id] = found
        return found

    def read_schema(self, tree: exp.Expr) -> dict[str, dict[str, str]]:
        """The columns of the relations ``tree`` reads: a schema of every relation in a large project
        costs more to build than the whole check."""
        found = {}
        for table in tree.find_all(exp.Table):
            key = table_key(table, self.default_catalog, self.default_db)
            if key in self.schema:
                found[key] = self.schema[key]
        return found

    def column_at(self, parts: list[str]) -> Node | None:
        """The graph column a fully qualified name (relation key, then column) refers to."""
        for split in range(len(parts) - 1, 0, -1):
            relation = self.graph.node_for_reading('.'.join(parts[:split]).lower())
            if relation is not None:
                return self.graph.column(relation.id, parts[split])
        return None

    def additive(self) -> set[str]:
        """Columns the project sums or counts as they are: amounts, quantities, a measure's inputs."""
        if self._additive is None:
            self._additive = set()
            for node in self.graph.nodes.values():
                expression = node.attrs.get('expression')
                if node.kind == 'column' and isinstance(expression, str) and _TOP_AGGREGATE.match(expression):
                    edges = self.graph.out_edges(node.id, 'derived_from')
                    if len(edges) == 1:
                        self._additive.add(edges[0].target)
        return self._additive

    def measure_column(self, column: Node) -> bool:
        """Whether a column holds an amount that a total would add up: bound to a measure term, an
        aggregate itself, or a column the project sums or counts."""
        if any(t.measure for t in self.bound_to.get(column.id, [])):
            return True
        if column.id in self.keys or column.id in self.unique or id_like(column.name):
            return False
        expression = column.attrs.get('expression')
        if isinstance(expression, str) and _TOP_AGGREGATE.match(expression):
            return True
        return column.id in self.additive() or self.origin(column.id) in self.additive()

    def null_group(self, condition: _Condition, column: Node, groups: set[str]) -> bool:
        """Whether ``condition`` only drops the group of NULLs of the column a grouped ``column``
        is grouped by (``WHERE k IS NOT NULL ... GROUP BY k``), and the query groups its result by
        that column too (``groups``, by origin): every other group keeps the same rows, so the
        query's groups other than NULL's hold the column's values. A query that adds up across
        the groups adds the NULL group's rows too, and needs the filter."""
        tree = condition.tree
        while isinstance(tree, exp.Paren):
            tree = tree.this
        if isinstance(tree, exp.Not) and isinstance(tree.this, exp.Is):
            test = tree.this
        elif isinstance(tree, exp.Is) and isinstance(tree.expression, exp.Not):
            test = exp.Is(this=tree.this, expression=tree.expression.this)
        else:
            return False
        if not (isinstance(test.this, exp.Column) and isinstance(test.expression, exp.Null)):
            return False
        node = condition.columns.get(test.this.sql(dialect=self.sqlglot))
        relation = self.graph.parent_of(column)
        grouped = self.group_origins(relation) if relation is not None else None
        return (
            node is not None
            and grouped is not None
            and self.origin(node.id) in grouped
            and self.origin(node.id) in groups
        )

    def truth(self, tree: exp.Expr) -> exp.Expr:
        """A boolean compared with TRUE or FALSE as the boolean or its NOT, and NOT NOT x as x,
        where the dialect has a boolean type: there ``x = FALSE`` and ``NOT x`` are NULL where ``x``
        is, so they keep the same rows. In MySQL, SQLite or SQL Server a flag is a number, which
        ``WHERE x`` tests for nonzero and ``x = TRUE`` for 1, so they stay apart; so does
        ``x IS TRUE``, since NOT (x IS TRUE) keeps the rows where x is NULL."""
        if self.dialect not in _BOOLEAN_DIALECTS:
            return tree
        negated, inner = False, tree
        while isinstance(inner, (exp.Not, exp.Paren)):
            negated, inner = negated ^ isinstance(inner, exp.Not), inner.this
        if isinstance(inner, (exp.EQ, exp.NEQ)):
            for tested, value in ((inner.this, inner.expression), (inner.expression, inner.this)):
                if isinstance(value, exp.Boolean) and not isinstance(tested, exp.Boolean):
                    if (value.this is True) == isinstance(inner, exp.NEQ):
                        negated = not negated
                    return exp.Not(this=tested) if negated else tested
        if isinstance(inner, (exp.Column, exp.Var)):
            return exp.Not(this=inner) if negated else inner
        return tree

    def declared(self, column: Node) -> bool:
        """Whether the definition of a measure binds ``column`` itself: people, not a model of the
        project, say it is an amount to add up on its rows. A model that sums a value repeated on
        finer rows can be as wrong as a query that does, and a term that only names the column
        (``kind: term``) says nothing of adding it up, so only a measure shows that the value adds
        up at that grain."""
        return any(t.measure and t.status != 'retired' for t in self.bound_to.get(column.id, []))

    def key_origins(self, relation: Node) -> frozenset[str]:
        """The columns whose values the unique columns of ``relation`` copy: two relations with a
        key of the same origin hold one row per row of the same thing."""
        return frozenset(
            self.origin(c.id)
            for c in self.graph.columns_of(relation.id)
            if frozenset({c.name.lower()}) in self.relation_keys(relation)
        )

    def carriers(self, column: Node) -> list[Node]:
        """The columns of the project that hold ``column``'s value on each row of another
        relation, computed row by row from it (never aggregated or windowed on the way), and that
        a definition says is a measure there (:meth:`declared`): order items' copy of a product's
        price, which revenue sums once per item."""
        found = self._carriers.get(column.id)
        if found is None:
            found, seen, stack = [], {column.id}, [column.id]
            while stack:
                for edge in self.graph.in_edges(stack.pop(), 'derived_from'):
                    reader = self.graph.nodes.get(edge.source)
                    if reader is None or reader.id in seen or self._aggregated(reader.id, set()):
                        continue
                    seen.add(reader.id)
                    stack.append(reader.id)
                    if self.declared(reader):
                        found.append(reader)
            self._carriers[column.id] = found
        return found

    def measured_at(self, column: Node, keys: frozenset[str]) -> Node | None:
        """A column the project adds up that repeats ``column``'s value on the rows of a relation
        keyed by a column of origin in ``keys``: summed once per such row, the value counts as the
        project's own measure counts it."""
        for carrier in self.carriers(column):
            relation = self.graph.parent_of(carrier)
            if relation is not None and self.key_origins(relation) & keys:
                return carrier
        return None

    def group_origins(self, relation: Node) -> frozenset[str] | None:
        """The origins of the columns ``relation``'s SQL groups by, when it groups once."""
        groups = {
            c.split(': ', 1)[1]
            for c in map(str, relation.attrs.get('conditions', []) or [])
            if c.startswith('GROUP BY') and ': ' in c
        }
        tree = self.parse(f'SELECT 1 GROUP BY {next(iter(groups))}') if len(groups) == 1 else None
        group = tree.args.get('group') if isinstance(tree, exp.Select) else None
        if group is None:
            return None
        nodes = [self.resolved(c) if isinstance(c, exp.Column) else None for c in group.expressions]
        return frozenset(self.origin(n.id) for n in nodes if n is not None) if all(nodes) else None

    def summed_at(self, form: _Form, groups: frozenset[str], keys: frozenset[str]) -> Node | None:
        """A column a definition binds (:meth:`declared`) that holds the same aggregate of the same
        columns as ``form``, grouped by columns of the same origins ``groups``, on the rows of a
        relation keyed by a column of origin in ``keys``: the supply cost of each item's product,
        which order_items sums over the product's supplies and repeats on each item."""
        if form.formula is None or not form.complete or not form.sources:
            return None
        function = _aggregate_of(form.formula, self.dialect)
        seen: set[str] = set(form.sources)
        stack = sorted(form.sources)
        while stack:
            for edge in self.graph.in_edges(stack.pop(), 'derived_from'):
                reader = self.graph.nodes.get(edge.source)
                if reader is None or reader.id in seen:
                    continue
                seen.add(reader.id)
                stack.append(reader.id)
                relation = self.graph.parent_of(reader)
                if relation is None or not self.declared(reader) or not self.key_origins(relation) & keys:
                    continue
                theirs = self.form(None, reader)
                if (
                    theirs.complete
                    and theirs.sources == form.sources
                    and not theirs.conditions
                    and theirs.formula is not None
                    and _aggregate_of(theirs.formula, self.dialect) == function
                    and self.group_origins(relation) == groups
                ):
                    return reader
        return None

    def slices(self, column: Node) -> list[tuple[_Term, Node, list[_Condition]]]:
        """The measure terms ``column`` computes over some of their rows: a column of a term that
        it computes the same way from the same columns, with conditions of its own as well as every
        condition of the term's (the card payments of an order amount), each with that column and
        the conditions it adds. Such a column is the measure for one slice of its rows, so the
        filters the term requires hold for it too; one whose own conditions are on a column the
        term filters (the amount of returned orders, where the term keeps completed ones) picks
        other rows than the term does, and is another idea."""
        found = self._slices.get(column.id)
        if found is not None:
            return found
        found = []
        mine = self.form(None, column) if isinstance(column.attrs.get('expression'), str) else None
        if mine is not None and mine.formula is not None and mine.complete and mine.conditions:
            own = {c.key: c for c in [*mine.conditions, *self.held_filters(column)]}
            for term in self.terms:
                if not term.measure or term.status == 'retired' or column in term.bound:
                    continue
                required = {self.origin(n.id) for c in self.required(term) for n in c.columns.values()}
                if required & {self.origin(n.id) for c in own.values() for n in c.columns.values()}:
                    continue
                for root in term.roots:
                    theirs = self.form(None, root)
                    held = {c.key for c in [*theirs.conditions, *self.held_filters(root)]}
                    extra = [c for k, c in own.items() if k not in held]
                    if (
                        theirs.complete
                        and theirs.formula == mine.formula
                        and theirs.sources == mine.sources
                        and held <= set(own)
                        and extra
                    ):
                        found.append((term, root, extra))
                        break
        self._slices[column.id] = found
        return found

    def scale(self, column: Node) -> float | None:
        """What ``column`` divides the one column it reads by, when that is all it computes
        (``amount / 100``, cast or rounded or not)."""
        expression = column.attrs.get('expression')
        tree = self.parse(expression) if isinstance(expression, str) else None
        tree = _strip(tree) if tree is not None else None
        if not (isinstance(tree, exp.Div) and isinstance(_strip(tree.this), exp.Column)):
            return None
        return _number(tree.expression)

    def minor(self, column: Node) -> _Minor | None:
        """How ``column`` is read in whole units, when it holds minor units of a currency: every
        column of the project that reads it divided by a constant divides it by the same one, and
        either the column, or a copy of it, is named for minor units (``tax_paid_cents``), or a term
        that is an amount of money (``unit: currency``) is computed from one of those columns."""
        if column.id in self._minor:
            return self._minor[column.id]
        found: _Minor | None = None
        label = self.graph.label
        scaled: list[tuple[Node, float]] = []
        copies: list[Node] = []
        for edge in self.graph.in_edges(column.id, 'derived_from'):
            reader = self.graph.nodes.get(edge.source)
            if reader is None or len(self.graph.out_edges(reader.id, 'derived_from')) != 1:
                continue
            if edge.attrs.get('transform') == 'direct':
                copies.append(reader)
            elif (scale := self.scale(reader)) is not None:
                scaled.append((reader, scale))
        scales = {scale for _, scale in scaled}
        if len(scales) == 1 and next(iter(scales)) in _FRACTIONS:
            readers = {r.id for r, _ in scaled}
            named = [c for c in [column, *copies] if _minor_named(c.name)]
            money = [
                t
                for t in self.terms
                if t.status != 'retired'
                and t.node.attrs.get('unit') == 'currency'
                and any(self.carries(b, r, set()) for b in t.bound for r in readers)
            ]
            if named or money:
                reader, scale = scaled[0][0], int(scaled[0][1])
                evidence = [f'{label(reader)} = {label(column)} / {scale}']
                evidence += [
                    f'{label(c)} copies {label(column)}'
                    if c is not column
                    else f'{label(c)} is named for minor units'
                    for c in named
                ]
                evidence += [f'{t.name!r} ({t.where}) is an amount of money computed from it' for t in money]
                found = _Minor(scale, reader, evidence)
        self._minor[column.id] = found
        return found

    def carries(self, column: Node, target: str, walking: set[str]) -> bool:
        """Whether ``column``'s value carries the units of the column ``target``, at any distance:
        it adds, picks or aggregates ``target``'s values, or scales them by a constant. A column
        that multiplies it by another column (a subtotal times a discount rate) holds the other
        column's units, so a rate is not an amount of money for the amount it scales."""
        if column.id == target:
            return True
        if column.id in walking:
            return False
        walking.add(column.id)
        expression = column.attrs.get('expression')
        tree = self.parse(expression) if isinstance(expression, str) else None
        for edge in self.graph.out_edges(column.id, 'derived_from'):
            read = self.graph.nodes.get(edge.target)
            if read is None or (read.id != target and target not in self.upstream(read.id)):
                continue
            if tree is not None and not _carries(tree, read.name or ''):
                continue
            if tree is None and edge.attrs.get('transform') != 'direct':
                continue
            if self.carries(read, target, walking):
                return True
        return False

    def whole(self, column: Node) -> tuple[str, int] | None:
        """The column in minor units that ``column`` reads in whole units, with its scale: one that
        computes nothing but it, divided by its scale (``order_total / 100``)."""
        form = self.form(None, column) if isinstance(column.attrs.get('expression'), str) else None
        tree = self.parse(form.formula) if form is not None and form.formula is not None else None
        if form is None or tree is None or len(form.sources) != 1:
            return None
        [source] = form.sources
        node = self.graph.nodes.get(source)
        held = self.minor(node) if node is not None else None

        def only(c: exp.Column) -> bool:
            found = self.resolved(c)
            return found is not None and found.id == source

        if held is None or _units(tree, only, held.scale) != 0:
            return None
        return source, held.scale

    def divides(self, column: Node, source: str, scale: int) -> bool:
        """Whether ``column`` is the integer or decimal column ``source`` divided by ``scale`` and
        nothing else, where ``/`` never truncates: only then is ``column >= 20`` the same filter as
        ``source >= 20 * scale``. A cast, a rounding or a division that truncates moves the
        boundary (``CAST(1960 / 100 AS INT)`` is 20)."""
        expression = column.attrs.get('expression')
        tree = self.parse(expression) if self.exact and isinstance(expression, str) else None
        while isinstance(tree, exp.Paren):
            tree = tree.this
        if not isinstance(tree, exp.Div) or _number(tree.expression) != scale:
            return False
        read = tree.this
        while isinstance(read, exp.Paren):
            read = read.this
        node = self.resolved(read) if isinstance(read, exp.Column) else None
        kind = (node.data_type or '').upper() if node is not None else ''
        exact = kind.startswith(('DECIMAL', 'NUMERIC', 'NUMBER', 'INT')) or kind.endswith(('INT', 'INTEGER'))
        return node is not None and node.id == source and exact

    def describe(self, column: Node) -> str:
        """A column's label with what vouches for it: its term's status and its verified edges."""
        label = self.graph.label(column)
        notes = [f'{t.status} {t.name!r}' for t in self.bound_to.get(column.id, [])]
        if any(t.measure for t in self.bound_to.get(column.id, [])):
            edges = self.graph.out_edges(column.id, 'derived_from')
            checked = [e for e in edges if e.attrs.get('verified') == 'confirmed']
            if checked:
                notes.append(f'verified {len(checked)}/{len(edges)} edges')
        return f'{label} ({", ".join(notes)})' if notes else label

    # Conditions

    def parse(self, text: str) -> exp.Expr | None:
        try:
            return sqlglot.parse_one(text, read=self.sqlglot)
        except PARSE_ERRORS:
            return None

    def resolved(self, column: exp.Column) -> Node | None:
        return self.column_at([p.name for p in column.parts])

    def conjuncts(self, condition: str, source: str | None = None) -> list[_Condition]:
        """A filter from the graph (``WHERE: ...`` over fully qualified columns), split into its
        conjuncts; other conditions (grouping, DISTINCT, LIMIT, the equalities of a join) give none.
        Where a filter sits before a window or a LIMIT does not change the rows of the columns it
        filters. One on the nullable side of an outer join restricts only that side's rows; one in
        an inner join's ON restricts every row, as a WHERE does."""
        label, sep, text = condition.partition(': ')
        if not sep:
            return []
        label = label.split(' BEFORE ', 1)[0]
        words = label.split()
        joined = words[-2:] in (['JOIN', 'WHERE'], ['JOIN', 'ON'])
        kind = words[-1]
        side = next((w for w in words if w in ('LEFT', 'RIGHT', 'FULL')), '')
        if side in ('RIGHT', 'FULL') or (not joined and label not in _FILTER_LABELS):
            return []
        tree = self.parse(text)
        if tree is None:
            return []
        found = []
        for part in split_conjuncts(tree, between=True):
            made = self.condition(part, self.resolved, 'WHERE' if joined else label, source)
            if made is None:
                continue
            restricted = frozenset(p for n in made.columns.values() if (p := n.parent))
            if joined and kind == 'ON' and (len(restricted) != 1 or not made.columns):
                continue  # an equality between the joined relations is the join itself
            if joined and side:
                made.limited = restricted
            found.append(made)
        return found

    def condition(
        self,
        tree: exp.Expr,
        resolve: Callable[[exp.Column], Node | None],
        label: str = 'WHERE',
        source: str | None = None,
    ) -> _Condition | None:
        columns: dict[str, Node] = {}
        for column in tree.find_all(exp.Column):
            node = resolve(column)
            if node is None:
                return None
            columns[column.sql(dialect=self.sqlglot)] = node

        def swap(item: exp.Expr) -> exp.Expr:
            if isinstance(item, exp.Column):
                node = columns.get(item.sql(dialect=self.sqlglot))
                if node is not None:
                    return exp.Var(this=self.origin(node.id))
            return item

        types = {self.origin(n.id): (n.data_type or '').upper() for n in columns.values()}
        dated = {c for c, t in types.items() if t == 'DATE'}
        timed = {c for c, t in types.items() if t.startswith(('TIMESTAMP', 'DATETIME', 'TIME'))}
        untimed = {c for c, t in types.items() if t not in ('', 'UNKNOWN') and c not in dated | timed}
        normal = normal_condition(
            self.truth(tree.copy().transform(swap, copy=False)), self.sqlglot, dated, timed, untimed
        )
        opposite = normal_condition(
            self.truth(exp.Not(this=normal.copy())), self.sqlglot, dated, timed, untimed
        )
        return _Condition(
            self.short(tree),
            normal.sql(dialect=self.sqlglot),
            tree,
            columns,
            label,
            source,
            opposite=opposite.sql(dialect=self.sqlglot),
        )

    def short(self, tree: exp.Expr) -> str:
        """``tree`` with short relation names, as the rest of Konstruct shows conditions."""
        return self.explainer.namer.text(tree.sql(dialect=self.sqlglot))

    def required(self, term: _Term) -> list[_Condition]:
        """The filters a term requires, each resolved against the project."""
        return [
            c for text in term.node.attrs.get('filters', []) or [] for c in self._required(term, str(text))
        ]

    def _required(self, term: _Term, text: str) -> list[_Condition]:
        """The conjuncts of one filter ``term`` requires, each resolved against the project."""
        tree = self.parse(text)
        if tree is None:
            return []
        relations = [p for c in term.bound if (p := self.graph.parent_of(c)) is not None]

        def resolve(column: exp.Column) -> Node | None:
            if column.table:
                matches = self.graph.resolve('.'.join(p.name for p in column.parts))
                return next((m for m in matches if m.kind == 'column'), None)
            return next(
                (c for r in relations if (c := self.graph.column(r.id, column.name)) is not None), None
            )

        found = []
        for part in split_conjuncts(tree, between=True):
            made = self.condition(part, resolve, 'WHERE', f'term {term.name!r} ({term.where})')
            if made is not None:
                made.text = part.sql(dialect=self.sqlglot)
                found.append(made)
        return found

    def applied_on_route(self, name: str, column: Node) -> dict[str, str]:
        """Of the filters the term ``name`` requires, those that hold on every source row
        ``column``'s value is computed from (:meth:`held_filters`), each as the term writes it, with
        the relations that apply it: a column that holds an aggregate over filtered rows needs no
        filter on the rows of its own relation."""
        term = next((t for t in self.terms if t.name == name), None)
        if term is None:
            return {}
        held = self.held_filters(column)
        route: dict[str, str] = {}
        for condition in held:
            route.setdefault(condition.key, condition.source or '')
        found = {}
        for text in term.node.attrs.get('filters', []) or []:
            parts = self._required(term, str(text))
            if parts and all(p.key in route for p in parts):
                found[str(text)] = ', '.join(sorted({route[p.key] for p in parts}))
        return found

    def filter_keys(self, node: Node, text: str) -> list[str]:
        """The keys of the conditions the filter ``text`` of the term ``node`` requires: the filter
        holds on a column when :meth:`held_filters` has all of them. Empty when the filter does not
        parse, or ``node`` is not a term this checker binds to a column."""
        term = self._term_nodes.get(node.id)
        return [] if term is None else [p.key for p in self._required(term, text)]

    def held_filters(self, column: Node) -> list[_Condition]:
        """The filters that hold on every source row ``column``'s value is computed from, each with
        the relation that applies it: those of its own relation on the queries it is computed
        through (``row_conditions``: not one in a CTE beside it, above a window or an aggregate
        it reads, on one branch of a union, or on another alias than the one it is read through),
        unless a value upstream was aggregated or windowed (``aggregates_rows``) from rows they
        never saw, and only where each column they filter comes from the same rows as the value's
        inputs or from another table (:meth:`aligned`); and those that hold on every column it is
        derived from. A relation whose columns do
        not say which of its conditions hold on them (a graph an earlier Konstruct built) gives
        none: nothing is taken as applied that is not shown."""
        return list(self._holds(column.id, set()).values())

    def _holds(self, column_id: str, walking: set[str]) -> dict[str, _Condition]:
        found = self._held.get(column_id)
        if found is not None:
            return found
        if column_id in walking:
            return {}  # a cycle: claim nothing
        node = self.graph.nodes.get(column_id)
        if node is None:
            return {}
        held: dict[str, _Condition] = {}
        relation = self.graph.parent_of(node)
        conditions = relation.attrs.get('conditions') if relation is not None else None
        positions = node.attrs.get('row_conditions')
        upstream = sorted({e.target for e in self.graph.out_edges(column_id, 'derived_from')})
        # A value another relation aggregated or windowed was made from rows its filters never saw.
        made_above = any(self._aggregated(u, set()) for u in upstream)
        if relation is not None and conditions and isinstance(positions, list) and not made_above:
            reads = {r.id for r in self.route(node)}
            inputs = [n for u in upstream if (n := self.graph.nodes.get(u)) is not None]
            where = self.graph.label(relation) + (f' ({relation.path})' if relation.path else '')
            for position in positions:
                if isinstance(position, int) and 0 <= position < len(conditions):
                    text = str(conditions[position])
                    parts = self._conjuncts.get(f'{where}\0{text}')
                    if parts is None:
                        parts = self._conjuncts[f'{where}\0{text}'] = self.conjuncts(text, where)
                    for part in parts:
                        if (
                            part.label in _FILTER_LABELS
                            and (not part.limited or part.limited & reads)
                            and all(self.aligned(c, u) for c in part.columns.values() for u in inputs)
                        ):
                            held.setdefault(part.key, part)
        below = [self._holds(u, walking | {column_id}) for u in upstream]
        if below:
            shared = set(below[0]).intersection(*below[1:])
            for key in sorted(shared):
                held.setdefault(key, below[0][key])
        self._held[column_id] = held
        return held

    def aligned(self, one: Node, other: Node) -> bool:
        """Whether the values of two columns of the rows a query reads come from the same rows, or
        from different tables, so that a filter on one holds on the rows of the other: not when
        they may come from two readings of one table, such as a column a model copies from the
        order before each order (``row_reading``)."""
        if one.id == other.id:
            return True
        cached = self._aligned.get((one.id, other.id))
        if cached is not None:
            return cached
        self._aligned[one.id, other.id] = False  # a cycle: claim nothing
        found = False
        if one.parent == other.parent:
            below = [self._derived(one), self._derived(other)]
            reading = one.attrs.get('row_reading')
            if not below[0] and not below[1]:
                found = True  # the columns of one row of a table SQL does not write
            elif reading is not None and reading == other.attrs.get('row_reading'):
                found = all(self.aligned(a, b) for a in below[0] for b in below[1])
        found = found or not (self._ends(one) & self._ends(other))
        self._aligned[one.id, other.id] = found
        return found

    def _derived(self, column: Node) -> list[Node]:
        return [
            n
            for e in self.graph.out_edges(column.id, 'derived_from')
            if (n := self.graph.nodes.get(e.target))
        ]

    def _ends(self, column: Node) -> frozenset[str]:
        """The relations no SQL writes that the column's value is read from, at the end of its lineage."""
        found = self._ended.get(column.id)
        if found is not None:
            return found
        self._ended[column.id] = frozenset()  # a cycle
        upstream = self._derived(column)
        found = (
            frozenset(r for u in upstream for r in self._ends(u))
            if upstream
            else frozenset({column.parent or column.id})
        )
        self._ended[column.id] = found
        return found

    def _aggregated(self, column_id: str, walking: set[str]) -> bool:
        """Whether the value of the column aggregates rows or computes a window over them, in its own
        relation's SQL or in that of a column it is derived from."""
        return self._anywhere(
            column_id, lambda node: node.attrs.get('aggregates_rows') is True, self._made, walking
        )

    def unshown(self, column_id: str) -> bool:
        """Whether the graph does not say which filters hold on the column's rows, or on those of a
        column it is derived from: their relation has conditions and none of its columns
        ``row_conditions``, as in a graph an earlier Konstruct built."""

        def untold(node: Node) -> bool:
            relation = self.graph.parent_of(node)
            if relation is None or not relation.attrs.get('conditions'):
                return False
            return not any(
                isinstance(c.attrs.get('row_conditions'), list) for c in self.graph.columns_of(relation.id)
            )

        return self._anywhere(column_id, untold, self._untold, set())

    def _anywhere(
        self, column_id: str, test: Callable[[Node], bool], memo: dict[str, bool], walking: set[str]
    ) -> bool:
        """Whether ``test`` holds of the column or of a column upstream of it; true on a cycle."""
        found = memo.get(column_id)
        if found is not None:
            return found
        if column_id in walking:
            return True
        node = self.graph.nodes.get(column_id)
        found = node is not None and (
            test(node)
            or any(
                self._anywhere(e.target, test, memo, walking | {column_id})
                for e in self.graph.out_edges(column_id, 'derived_from')
            )
        )
        memo[column_id] = found
        return found

    def held_on(self, columns: list[Node]) -> list[_Condition]:
        """The filters that hold on the rows of every one of ``columns``."""
        found = [{p.key: p for p in self.held_filters(c)} for c in columns]
        if not found:
            return []
        shared = set(found[0]).intersection(*found[1:])
        return [found[0][k] for k in sorted(shared)]

    # Formulas

    def form(self, text: str | None, node: Node | None = None) -> _Form:
        """A formula over fully qualified columns, composed down to source columns; with ``node``,
        that graph column's own formula, worked out once."""
        if node is not None and node.id in self._forms:
            return self._forms[node.id]
        if node is None:
            self._virtual += 1
            node = Node(
                id=f'checksql:{self._virtual}', kind='column', name='query', attrs={'expression': text}
            )
        composed = self.composer.compose(node)
        found = _Form(None, composed.sources, composed.complete)
        if composed.tree is not None:
            formula, condition = _formula(composed.tree, self.exact)
            # Function names in one case: a dialect that keeps them as written (BigQuery,
            # ClickHouse) would otherwise make SUM and sum two formulas.
            found.formula = formula.sql(dialect=self.sqlglot, normalize_functions='upper')
            # The sources of the value, not of the condition taken out of it, which is compared as
            # a filter.
            read = [self.resolved(c) for c in formula.find_all(exp.Column)]
            if read and all(r is not None for r in read):
                found.sources = frozenset(r.id for r in read if r is not None)
            if condition is not None:
                parts = [
                    self.condition(p, self.resolved, 'WHERE')
                    for p in split_conjuncts(condition, between=True)
                ]
                if any(p is None for p in parts):
                    found.complete = False
                found.conditions = [p for p in parts if p is not None]
        if not node.id.startswith('checksql:'):
            self._forms[node.id] = found
        return found

    def check(self, sql: str) -> SqlCheck:
        """Check ``sql`` against the project."""
        return _Run(self, sql).run()


def reachable(
    aliases: list[str],
    equalities: list[tuple[str, str, str, str]],
    unique: dict[str, list[frozenset[str]]],
    fixed: dict[str, set[str]] | None = None,
    null_safe: list[tuple[str, str, str, str]] | None = None,
    grouped: dict[str, list[frozenset[str]]] | None = None,
) -> dict[str, set[str]]:
    """For each source of a join, the sources each of its rows matches at most one row of.

    ``equalities`` are the join's ``(source, column, source, column)`` equalities and ``unique`` the
    sets of columns known to be unique in each source (an empty set: the source has one row).
    ``fixed`` holds the columns of each source an aggregate groups by, which are the same on every
    row of a group, as are the columns equated to them. Within a group, a row of ``a`` matches at
    most one row of ``b`` when the columns of ``b`` it is equated to, with those fixed, cover a unique
    set of ``b``, and through ``b`` at most one row of whatever ``b`` matches at most one of. A source
    that reaches every other keeps its grain: the join repeats none of its rows within a group.

    ``null_safe`` are the ``IS NOT DISTINCT FROM`` equalities, which also match NULL to NULL: they
    cover only the sets of ``grouped``, those a DISTINCT or a GROUP BY makes unique, which keep a
    single row for NULL; a unique test lets many rows hold NULL."""
    matched: dict[tuple[str, str], set[str]] = defaultdict(set)
    for left, left_column, right, right_column in equalities:
        matched[(left, right)].add(right_column)
        matched[(right, left)].add(left_column)
    loose: dict[tuple[str, str], set[str]] = defaultdict(set)
    for left, left_column, right, right_column in null_safe or []:
        loose[(left, right)].add(right_column)
        loose[(right, left)].add(left_column)
    constant: dict[str, set[str]] = defaultdict(set)
    for alias, columns in (fixed or {}).items():
        constant[alias] |= columns
    changed = True
    while changed:
        changed = False
        for left, left_column, right, right_column in equalities:
            for a, ac, b, bc in (
                (left, left_column, right, right_column),
                (right, right_column, left, left_column),
            ):
                if ac in constant[a] and bc not in constant[b]:
                    constant[b].add(bc)
                    changed = True

    def at_most_one(a: str, b: str) -> bool:
        columns = matched.get((a, b), set()) | constant[b]
        if any(u <= columns for u in unique.get(b, [])):
            return True
        columns = columns | loose.get((a, b), set())
        return any(u <= columns for u in (grouped or {}).get(b, []))

    found = {}
    for alias in aliases:
        reached, stack = {alias}, [alias]
        while stack:
            current = stack.pop()
            for other in aliases:
                if other not in reached and at_most_one(current, other):
                    reached.add(other)
                    stack.append(other)
        found[alias] = reached
    return found


def _drops_zeros(condition: _Condition) -> bool:
    """Whether a filter only leaves out zeros (``x <> 0``), which a sum adds nothing for."""
    tree = condition.tree
    return isinstance(tree, exp.NEQ) and any(
        isinstance(side, exp.Literal) and not side.is_string and side.name in ('0', '0.0')
        for side in (tree.this, tree.expression)
    )


def _excludes(condition: _Condition) -> bool:
    """Whether a filter leaves out rows of some kind (``<>``, ``NOT IN``, ``NOT LIKE``), as a
    definition does, rather than choosing the rows of a question (``=``, ``IN``, a date range)."""
    tree = condition.tree
    while isinstance(tree, exp.Paren):
        tree = tree.this
    if isinstance(tree, exp.NEQ):
        return True
    if isinstance(tree, exp.Like) and tree.args.get('negate'):
        return True
    if isinstance(tree, exp.Not):
        inner = tree.this
        return not (isinstance(inner, exp.Is) and isinstance(inner.expression, exp.Null))
    return False


def _minor_named(name: str | None) -> bool:
    return bool(name) and bool(_MINOR_WORDS & set(re.split(r'[^a-z0-9]+', (name or '').lower())))


def _number(node: exp.Expr | None) -> float | None:
    """The value of a numeric literal, cast or not."""
    if isinstance(node, (exp.Paren, exp.Cast)):
        return _number(node.this)
    if isinstance(node, exp.Literal) and not node.is_string:
        try:
            return float(node.name)
        except ValueError:
            return None
    return None


def _compared(condition: _Condition, dialect: Dialect) -> tuple[Node, type[exp.Expr], float] | None:
    """A filter that compares one column with a number, as the column, the comparison with the
    column on its left, and the number."""
    tree = condition.tree
    while isinstance(tree, exp.Paren):
        tree = tree.this
    kind = type(tree)
    if kind not in _COMPARISONS:
        return None
    for column, other, compared in (
        (tree.this, tree.expression, kind),
        (tree.expression, tree.this, _COMPARISONS[kind]),
    ):
        value = _number(other)
        if isinstance(column, exp.Column) and value is not None:
            node = condition.columns.get(column.sql(dialect=dialect))
            return (node, compared, value) if node is not None else None
    return None


def _carries(node: exp.Expr, name: str) -> bool:
    """Whether an expression's value is in the units of the column ``name``: the column, a sum or
    difference with it, an aggregate, cast or pick of it, or it scaled by a constant."""
    if isinstance(node, exp.Column):
        return node.name.lower() == name.lower()
    if isinstance(node, exp.Count):
        return False
    if isinstance(node, exp.Mul):
        for factor, rest in ((node.this, node.expression), (node.expression, node.this)):
            if _number(factor) is not None:
                return _carries(rest, name)
        return False
    if isinstance(node, exp.Div):
        return _carries(node.this, name)
    if isinstance(node, exp.Case):
        branches = [i.args.get('true') for i in node.args.get('ifs') or []] + [node.args.get('default')]
        return any(_carries(b, name) for b in branches if isinstance(b, exp.Expr))
    if isinstance(node, exp.If):
        return any(
            _carries(b, name) for b in (node.args.get('true'), node.args.get('false')) if b is not None
        )
    if isinstance(node, (exp.Add, exp.Sub, exp.Coalesce)):
        return any(
            _carries(b, name) for b in (node.this, node.expression, *node.expressions) if b is not None
        )
    if isinstance(
        node,
        (exp.Paren, exp.Alias, exp.Cast, exp.Round, exp.Abs, exp.Neg, exp.Filter, exp.Window, exp.AggFunc),
    ):
        return isinstance(node.this, exp.Expr) and _carries(node.this, name)
    return False


_BOOLEAN_DIALECTS = frozenset(
    {
        'athena',
        'bigquery',
        'databricks',
        'duckdb',
        'hive',
        'postgres',
        'presto',
        'redshift',
        'snowflake',
        'spark',
        'trino',
    }
)
"""Dialects whose booleans are a type of their own, not numbers."""
_CONSTANT_PARTS = (
    exp.Literal,
    exp.Boolean,
    exp.Null,
    exp.Paren,
    exp.Neg,
    exp.Cast,
    exp.TryCast,
    exp.DataType,
    exp.DataTypeParam,
    exp.Add,
    exp.Sub,
    exp.Mul,
    exp.Div,
    exp.Mod,
    exp.DPipe,
    exp.Concat,
    exp.Interval,
    exp.Var,
)


def _constant(expression: exp.Expr) -> bool:
    """Whether a SELECT item is a literal, or a cast, sum, product or concatenation of literals:
    the same on every row. A function is not, even of literals: ``uuid()``, ``random()``,
    ``now()`` and a user's function can differ from row to row."""
    return all(isinstance(node, _CONSTANT_PARTS) for node in _output(expression).walk())


def _figure(value: float) -> str:
    """A number as people write it: 2000, 19.99."""
    return f'{round(value, 6):f}'.rstrip('0').rstrip('.')


def _units(node: exp.Expr, minor: Callable[[exp.Column], bool], scale: int) -> int | None:
    """The power of the minor unit in a value: 1 for a column in minor units and for what adds,
    averages or picks among its values, 0 once it is divided by ``scale``, by an amount in minor units
    (a ratio) or for a count, and None where the units do not combine (cents added to a count) or
    the check does not follow them."""
    if isinstance(node, exp.Column):
        return 1 if minor(node) else 0
    if isinstance(node, (exp.Literal, exp.Null, exp.Boolean, exp.Count)):
        return 0
    if isinstance(node, exp.Div) and _number(node.expression) is not None:
        # A chain of constant divisors divides by their product: x / 10 / 10 is x / 100.
        divisor, base = 1.0, node
        while isinstance(base, exp.Div) and (value := _number(base.expression)) is not None:
            divisor, base = divisor * value, base.this
        units = _units(base, minor, scale)
        return units - 1 if units is not None and units > 0 and math.isclose(divisor, scale) else units
    if isinstance(node, (exp.Div, exp.Mul)):
        left, right = _units(node.this, minor, scale), _units(node.expression, minor, scale)
        if left is None or right is None:
            return None
        if isinstance(node, exp.Div):
            return left - right
        factor = next((v for side in (node.this, node.expression) if (v := _number(side)) is not None), None)
        return left + right - (1 if factor is not None and factor * scale == 1 and left + right > 0 else 0)
    if isinstance(node, (exp.Add, exp.Sub)):
        left, right = _units(node.this, minor, scale), _units(node.expression, minor, scale)
        return left if left == right else None
    branches: list[Any] | None = None
    if isinstance(node, exp.Coalesce):
        branches = [node.this, *node.expressions]
    elif isinstance(node, exp.Case):
        branches = [i.args.get('true') for i in node.args.get('ifs') or []] + [node.args.get('default')]
    elif isinstance(node, exp.If):
        branches = [node.args.get('true'), node.args.get('false')]
    if branches is not None:
        # A constant branch (COALESCE(x, 0), ELSE 0) is in whatever units the others are.
        found = {_units(b, minor, scale) for b in branches if isinstance(b, exp.Expr) and _number(b) is None}
        return found.pop() if len(found) == 1 else None
    if isinstance(
        node, (exp.Filter, exp.Window, exp.Paren, exp.Alias, exp.Round, exp.Abs, exp.Neg, exp.Cast)
    ):
        return _units(node.this, minor, scale)
    if isinstance(node, exp.WithinGroup) and isinstance(node.this, (exp.PercentileCont, exp.PercentileDisc)):
        order = node.expression
        ordered = order.expressions if isinstance(order, exp.Order) else []
        return _units(ordered[0].this, minor, scale) if len(ordered) == 1 else None
    if isinstance(
        node,
        (
            exp.Sum,
            exp.Avg,
            exp.Max,
            exp.Min,
            exp.AnyValue,
            exp.Median,
            exp.Floor,
            exp.Ceil,
            exp.PercentileCont,
            exp.PercentileDisc,
            exp.Quantile,
            exp.ApproxQuantile,
        ),
    ):
        # A percentile, median or quantile of amounts is one of the amounts.
        return _units(node.this, minor, scale)
    return None if any(minor(c) for c in node.find_all(exp.Column)) else 0


# Formulas: what a measure computes, without what does not change its value


def _zero(node: exp.Expr | None) -> bool:
    return isinstance(node, exp.Literal) and not node.is_string and node.name in ('0', '0.0')


def _one(node: exp.Expr | None) -> bool:
    return isinstance(node, exp.Literal) and not node.is_string and node.name in ('1', '1.0')


def _null(node: exp.Expr | None) -> bool:
    return node is None or isinstance(node, exp.Null)


def _unwrap(node: exp.Expr) -> exp.Expr | None:
    """What a value-preserving wrapper wraps: ``ROUND(x, 2)``, ``CAST(x AS DOUBLE)``, ``COALESCE(x,
    0)``, ``x * 1.0``, brackets and ``OVER (...)`` give ``x``; anything else gives None."""
    inner: Any = None
    if (
        isinstance(node, (exp.Alias, exp.Paren, exp.Round))
        or (isinstance(node, exp.Cast) and node.to.is_type(*exp.DataType.NUMERIC_TYPES))
        or (isinstance(node, exp.Coalesce) and len(node.expressions) == 1 and _zero(node.expressions[0]))
        or (isinstance(node, (exp.Mul, exp.Div)) and _one(node.expression))
        # Over a window, an aggregate computes the same measure for each partition.
        or (isinstance(node, exp.Window) and isinstance(node.this, (exp.AggFunc, exp.Filter)))
    ):
        inner = node.this
    elif isinstance(node, exp.Mul) and _one(node.this):
        inner = node.expression
    return inner if isinstance(inner, exp.Expr) else None


def _strip(node: exp.Expr) -> exp.Expr:
    while (inner := _unwrap(node)) is not None:
        node = inner
    return node


def _collapse(tree: exp.Expr) -> exp.Expr:
    """``SUM(SUM(x))`` as ``SUM(x)``, ``SUM(COUNT(x))`` as ``COUNT(x)``: re-aggregating a sum, count,
    minimum or maximum changes nothing."""

    def flatten(node: exp.Expr) -> exp.Expr:
        if isinstance(node, (exp.Sum, exp.Min, exp.Max)):
            inner = _strip(node.this)
            if type(inner) is type(node) or (isinstance(node, exp.Sum) and isinstance(inner, exp.Count)):
                return inner
        return node

    return tree.transform(flatten, copy=False)


def _formula(tree: exp.Expr, exact: bool | None = None) -> tuple[exp.Expr, exp.Expr | None]:
    """A formula without its value-preserving wrappers and, when it is one aggregate that counts
    only some rows (``SUM(x) FILTER (WHERE c)``, ``SUM(CASE WHEN c THEN x ELSE 0 END)``,
    ``COUNT(CASE WHEN c THEN 1 END)``, ``SUM(IF(c, 1, 0))``), the aggregate over every row and the
    condition ``c``, which is a filter on the rows it reads. To be compared, with ``exact`` given,
    the constant factors of sums and averages are taken out of them (:func:`_linear`); as people
    read it, without."""
    tree = _collapse(_strip(tree.copy()).transform(_strip, copy=False))
    condition: exp.Expr | None = None
    if isinstance(tree, exp.Filter) and isinstance(tree.this, exp.AggFunc):
        where = tree.expression
        condition = where.this if isinstance(where, exp.Where) else where
        tree = tree.this
    if isinstance(tree, exp.AggFunc) and not isinstance(tree.this, exp.Distinct):
        picked = _conditional(tree)
        if picked is not None:
            tree, when = picked
            condition = when if condition is None else exp.and_(condition, when)
    tree = _collapse(tree)
    return (tree if exact is None else _linear(tree, exact)), condition


def _linear(tree: exp.Expr, exact: bool) -> exp.Expr:
    """A formula with each constant factor of a sum or an average taken out of it, which changes
    nothing: ``SUM(x * 2)`` as ``2 * SUM(x)``, and where ``/`` never truncates (``exact``),
    ``SUM(x / 100)`` as ``SUM(x) / 100``, with ``0.01 * x`` written ``x / 100`` and each number
    written one way (``100.0`` as ``100``)."""

    def number(node: exp.Expr) -> exp.Expr:
        if exact and isinstance(node, exp.Literal) and not node.is_string:
            value = _decimal(node)
            if value is not None:
                return exp.Literal.number(format(value.normalize(), 'f'))
        return node

    def divided(node: exp.Expr) -> exp.Expr:
        if exact and isinstance(node, exp.Mul):
            for factor, rest in ((node.this, node.expression), (node.expression, node.this)):
                value = _decimal(factor)
                if value is not None and 0 < value < 1 and (1 / value) == (1 / value).to_integral_value():
                    return exp.Div(this=rest.copy(), expression=exp.Literal.number(format(1 / value, 'f')))
        return node

    def pull(node: exp.Expr) -> exp.Expr:
        if isinstance(node, (exp.Sum, exp.Avg)) and not isinstance(node.this, exp.Distinct):
            inner = _strip(node.this) if isinstance(node.this, exp.Expr) else None
            if exact and isinstance(inner, exp.Div) and _decimal(inner.expression) is not None:
                return exp.Div(this=type(node)(this=inner.this.copy()), expression=inner.expression.copy())
            if isinstance(inner, exp.Mul):
                for factor, rest in ((inner.this, inner.expression), (inner.expression, inner.this)):
                    if _decimal(factor) is not None:
                        return exp.Mul(this=factor.copy(), expression=type(node)(this=rest.copy()))
        return node

    tree = tree.transform(number).transform(divided)
    for _ in range(10):  # each pass takes one factor out of each aggregate
        pulled = tree.transform(pull)
        if pulled == tree:
            break
        tree = pulled
    return tree


def _decimal(node: exp.Expr | None) -> Decimal | None:
    """The exact value of a numeric literal."""
    if isinstance(node, exp.Literal) and not node.is_string:
        try:
            return Decimal(node.name)
        except ArithmeticError:
            return None
    return None


def _aggregate_of(formula: str | None, dialect: str | None) -> str | None:
    """The aggregate a formula applies last, through a constant factor: ``SUM`` for ``SUM(x) / 100``."""
    try:
        tree = sqlglot.parse_one(formula, read=dialect) if formula else None
    except PARSE_ERRORS:
        return None
    while isinstance(tree, (exp.Paren, exp.Div, exp.Mul)):
        if isinstance(tree, exp.Paren) or _decimal(tree.expression) is not None:
            tree = tree.this
        elif isinstance(tree, exp.Mul) and _decimal(tree.this) is not None:
            tree = tree.expression
        else:
            break
    return top_function(tree.sql(dialect=dialect), dialect) if tree is not None else None


def _conditional(agg: exp.AggFunc) -> tuple[exp.Expr, exp.Expr] | None:
    """An aggregate of ``CASE WHEN c THEN x END`` as the aggregate of ``x`` and ``c``."""
    argument = _strip(agg.this) if isinstance(agg.this, exp.Expr) else None
    if isinstance(argument, exp.Case) and not argument.this and len(argument.args.get('ifs') or []) == 1:
        branch = argument.args['ifs'][0]
        when, value, default = branch.this, branch.args.get('true'), argument.args.get('default')
    elif isinstance(argument, exp.If):
        when, value, default = argument.this, argument.args.get('true'), argument.args.get('false')
    else:
        return None
    if not isinstance(value, exp.Expr):
        return None
    if isinstance(agg, exp.Sum) and _one(value) and (_zero(default) or _null(default)):
        return exp.Count(this=exp.Star()), when
    if not (_null(default) or (isinstance(agg, exp.Sum) and _zero(default))):
        return None
    if isinstance(agg, exp.Count) and isinstance(value, exp.Literal):
        return exp.Count(this=exp.Star()), when
    rebuilt = agg.copy()
    rebuilt.set('this', value.copy())
    return rebuilt, when


# The query


def _aggregates(select: exp.Select) -> Iterator[exp.AggFunc]:
    """The aggregate calls of one SELECT, outside window functions and nested queries."""
    for expression in [*select.expressions, select.args.get('having')]:
        if not isinstance(expression, exp.Expr):
            continue
        for agg in expression.find_all(exp.AggFunc):
            if _owner(agg) is select and agg.find_ancestor(exp.Window, exp.Select) is select:
                yield agg


def _literals(query: exp.Expr | None) -> list[exp.Expr] | None:
    """The values of a query that reads nothing, ``SELECT 'a'`` or ``VALUES ('a'), ('b')``."""
    query = _unnest(query)
    if isinstance(query, exp.Select) and not query.args.get('from_') and len(query.expressions) == 1:
        value = _output(query.expressions[0])
        return [value] if isinstance(value, exp.Literal) else None
    if isinstance(query, exp.Values) and all(
        isinstance(r, exp.Tuple) and len(r.expressions) == 1 and isinstance(r.expressions[0], exp.Literal)
        for r in query.expressions
    ):
        return [r.expressions[0] for r in query.expressions]
    return None


def _values_of(scope: Scope, name: str, depth: int = 0) -> list[exp.Expr] | None:
    """The literals the output ``name`` of a query takes when it only passes on a ``VALUES`` list."""
    select = scope.expression
    if depth > 10 or not isinstance(select, exp.Select):
        return None
    value = next((_output(e) for e in select.expressions if e.alias_or_name.lower() == name), None)
    if not isinstance(value, exp.Column):
        return None
    source = scope.sources.get(value.table)
    if isinstance(source, Scope) and not isinstance(source.expression, exp.Values):
        return _values_of(source, value.name.lower(), depth + 1)
    listed = getattr(select.args.get('from_'), 'this', None)
    if not isinstance(listed, exp.Values) or listed.alias_or_name != value.table:
        return None
    columns = [c.lower() for c in listed.alias_column_names]
    if value.name.lower() not in columns:
        return None
    index = columns.index(value.name.lower())
    found = [
        r.expressions[index]
        for r in listed.expressions
        if isinstance(r, exp.Tuple) and len(r.expressions) > index
    ]
    return found if found and all(isinstance(f, exp.Literal) for f in found) else None


def _constant_queries(part: exp.Expr) -> exp.Expr:
    """A condition with each query in it that reads nothing written as its values: ``x NOT IN
    (SELECT 'a')`` as ``x NOT IN ('a')``, ``x <> ALL (SELECT 'a')`` as ``x NOT IN ('a')``,
    ``x <> (SELECT 'a')`` as ``x <> 'a'``. Unchanged, the condition itself."""
    if part.find(exp.Select, exp.Values) is None:
        return part

    def fold(node: exp.Expr) -> exp.Expr:
        if isinstance(node, exp.In) and node.args.get('query') is not None:
            values = _literals(node.args['query'])
            if values:
                return exp.In(this=node.this.copy(), expressions=[v.copy() for v in values])
        if isinstance(node, (exp.EQ, exp.NEQ)):
            right = node.expression
            if isinstance(right, (exp.All, exp.Any)):
                values = _literals(right.this)
                if values:
                    listed = exp.In(this=node.this.copy(), expressions=[v.copy() for v in values])
                    return listed if isinstance(node, exp.EQ) else exp.Not(this=listed)
            elif isinstance(right, exp.Subquery):
                values = _literals(right)
                if values and len(values) == 1:
                    return type(node)(this=node.this.copy(), expression=values[0].copy())
        return node

    folded = part.copy().transform(fold, copy=False)
    return part if folded.find(exp.Select) is not None else folded


def _windowed(select: exp.Select) -> Iterator[tuple[exp.Window, exp.AggFunc]]:
    """The aggregates one SELECT applies over a window, ``SUM(x) OVER (...)``, with the window."""
    for expression in select.expressions:
        for window in expression.find_all(exp.Window):
            inner = window.this.this if isinstance(window.this, exp.Filter) else window.this
            if _owner(window) is select and isinstance(inner, exp.AggFunc):
                yield window, inner


def _partition(window: exp.Window, select: exp.Select) -> list[exp.Expr] | None:
    """The PARTITION BY ``window`` runs over, through the named windows it builds on (``OVER w``
    or ``OVER (w ORDER BY x)`` with ``WINDOW w AS (PARTITION BY k)``), or None when it names one
    ``select`` does not define."""
    partition = list(window.args.get('partition_by') or [])
    named = window.args.get('alias')
    seen: set[str] = set()
    while isinstance(named, exp.Identifier):
        name = named.name.lower()
        base = next(
            (
                w
                for w in select.args.get('windows') or []
                if isinstance(w, exp.Window) and w.name.lower() == name
            ),
            None,
        )
        if base is None or name in seen or partition:
            # A window that names another cannot add its own PARTITION BY.
            return None
        seen.add(name)
        partition = list(base.args.get('partition_by') or [])
        named = base.args.get('alias')
    return partition


def _subtotals(group: exp.Group) -> bool:
    """Whether a GROUP BY adds rows that span its groups: ROLLUP, CUBE, GROUPING SETS, MySQL's
    ``WITH ROLLUP`` and ClickHouse's ``WITH TOTALS``, whose rows hold many values of a column."""
    return any(group.args.get(k) for k in ('rollup', 'cube', 'grouping_sets', 'totals')) or any(
        isinstance(i, (exp.Rollup, exp.Cube, exp.GroupingSets)) for i in group.expressions
    )


def _valued(agg: exp.AggFunc, select: exp.Select) -> list[exp.Column]:
    """The columns of ``select`` whose values ``agg`` adds up: not those of a CASE or IF that only
    choose which rows count (``SUM(CASE WHEN c.region = 'EU' THEN o.amount END)`` adds up only
    ``o.amount``), unless the value is a constant, when those choose what is counted."""
    found: list[exp.Column] = []
    stack: list[exp.Expr] = [agg.this] if isinstance(agg.this, exp.Expr) else []
    while stack:
        node = stack.pop()
        if isinstance(node, exp.Case):
            stack += [
                b.args['true'] for b in node.args.get('ifs') or [] if isinstance(b.args.get('true'), exp.Expr)
            ]
            stack += [d] if isinstance(d := node.args.get('default'), exp.Expr) else []
        elif isinstance(node, exp.If):
            stack += [v for v in (node.args.get('true'), node.args.get('false')) if isinstance(v, exp.Expr)]
        elif isinstance(node, exp.Column):
            found.append(node)
        else:
            stack += list(node.iter_expressions())
    found = [c for c in found if _owner(c) is select]
    return found or _own_columns(agg, select)


def _listing(select: exp.Select) -> list[exp.Expr]:
    """The items of ``select`` that UNNEST or EXPLODE an array in the SELECT list, which repeat
    each row once per element after WHERE, GROUP BY, window functions and QUALIFY have run."""
    return [
        e for e in select.expressions if any(_owner(x) is select for x in e.find_all(exp.Explode, exp.Unnest))
    ]


def _owner(node: exp.Expr) -> exp.Expr | None:
    return node.find_ancestor(exp.Select)


def _own_columns(expression: exp.Expr, select: exp.Select) -> list[exp.Column]:
    return [c for c in expression.find_all(exp.Column) if _owner(c) is select]


def _output(expression: exp.Expr) -> exp.Expr:
    return expression.this if isinstance(expression, exp.Alias) else expression


def _ancestors(node: exp.Expr, kind: type[exp.Expr]) -> Iterator[exp.Expr]:
    current = node.find_ancestor(kind)
    while current is not None:
        yield current
        current = current.find_ancestor(kind)


def _unnest(node: exp.Expr | None) -> exp.Expr | None:
    while isinstance(node, exp.Subquery):
        node = node.this
    return node


def _joined(source: exp.Expr) -> set[str]:
    """The names a join brings in: the alias of what it joins, or for a join in parentheses
    (``LEFT JOIN (a JOIN b ON ...) ON ...``), which has none, the names of each source in it."""
    inner = source.this
    if isinstance(source, exp.Subquery) and not source.alias and isinstance(inner, (exp.Table, exp.Subquery)):
        found = _joined(inner)
    else:
        found = {source.alias_or_name}
    return found.union(*(_joined(j.this) for j in source.args.get('joins') or []))


def _branches(scope: Scope) -> list[Scope]:
    """The SELECTs of a UNION, INTERSECT or EXCEPT, left to right; a SELECT on its own."""
    if isinstance(scope.expression, exp.Select):
        return [scope]
    return [b for child in scope.set_operation_scopes for b in _branches(child)]


def _names(scope: Scope) -> list[str]:
    """The output names of a query: those of its first SELECT."""
    branches = _branches(scope)
    if not branches:
        return []
    select = branches[0].expression
    assert isinstance(select, exp.Select)
    return [e.alias_or_name.lower() for e in select.expressions]


class _Run:
    """One check of one piece of SQL."""

    def __init__(self, checker: Checker, sql: str) -> None:
        self.checker = checker
        self.graph = checker.graph
        self.sql = sql
        self.result = SqlCheck(sql=sql, dialect=checker.dialect)
        self.seen: set[tuple[str, ...]] = set()
        self.rewritten = False
        self.mapped: dict[int, exp.Select] = {}
        self.readers: dict[int, list[Scope]] = defaultdict(list)
        self.infos: dict[int, _ScopeInfo] = {}
        self.laterals: dict[int, Scope] = {}
        self.called: dict[str, list[str]] = {}
        self.placed: dict[tuple[str, str, str], str | None] = {}
        self.canon = Canonical(checker.sqlglot, checker.default_catalog, checker.default_db)
        self.schema = MappingSchema({}, dialect=checker.sqlglot)

    def add(self, finding: Finding, *key: str) -> None:
        identity = (finding.rule, *key) if key else (finding.rule, finding.message)
        if identity not in self.seen:
            self.seen.add(identity)
            self.result.findings.append(finding)

    def run(self) -> SqlCheck:
        checker = self.checker
        try:
            trees = [t for t in sqlglot.parse(self.sql, read=checker.sqlglot) if t is not None]
        except PARSE_ERRORS as error:
            message = str(error).strip().splitlines()[0] if str(error).strip() else type(error).__name__
            self.add(
                Finding('unparsed', 'error', f'the SQL could not be parsed: {message}', 'Correct the syntax.')
            )
            return self.result
        for index, (tree, query) in enumerate(zip(trees, _with_queries(trees), strict=True), 1):
            if query is None:
                continue
            try:
                lineage = next(
                    iter(
                        analyse(
                            tree.sql(dialect=checker.sqlglot),
                            dialect=checker.dialect,
                            schema=checker.read_schema(tree),
                            default_catalog=checker.default_catalog,
                            default_db=checker.default_db,
                        )
                    ),
                    None,
                )
                if lineage is None or lineage.kind == 'unparsed':
                    continue
                self.result.statements += 1
                self.names(lineage, query)
                self.retired(self.read(lineage), query)
                self.used(lineage)
                self.statement(query)
            except _ANALYSIS_ERRORS as error:
                # SQL sqlglot reads but cannot analyse (a PIVOT in a subquery, say) is not checked,
                # and the result says so rather than claim it passed.
                self.add(
                    Finding(
                        'unchecked',
                        'info',
                        f'statement {index} could not be analysed ({type(error).__name__}), so it was not checked',
                        'Check it by hand, or write it without the construct the analysis stops at.',
                    ),
                    str(index),
                )
        return self._finish(trees)

    def _finish(self, trees: list[exp.Expr]) -> SqlCheck:
        order = {s: i for i, s in enumerate(SEVERITIES)}
        self.result.findings.sort(key=lambda f: order[f.severity])
        if self.rewritten:
            self.result.corrected = ';\n\n'.join(
                t.sql(dialect=self.checker.sqlglot, pretty=True) for t in trees
            )
        return self.result

    def statement(self, query: exp.Query) -> None:
        """The checks that read the query scope by scope: joins, then measures."""
        checker = self.checker
        # Normalised as the dialect normalises unquoted names (Snowflake upper-cases them), so that
        # qualify resolves the query's columns against the graph's lower-case ones.
        built = build_schema(checker.read_schema(query), checker.sqlglot)
        self.schema = MappingSchema(built.mapping, dialect=checker.sqlglot, normalize=True)
        try:
            qualified = self._qualified(query)
            root = build_scope(qualified)
        except _QUALIFY_ERRORS:
            # qualify stops at a column its relation does not have; say which.
            try:
                unqualified = build_scope(query.copy())
                for scope in unqualified.traverse() if unqualified is not None else []:
                    self.unknown_qualified(scope)
            except _QUALIFY_ERRORS:
                pass
            return
        if root is None:
            return
        originals = list(query.find_all(exp.Select))
        selects = list(qualified.find_all(exp.Select))
        self.mapped = (
            {id(q): o for q, o in zip(selects, originals, strict=True)}
            if len(selects) == len(originals)
            else {}
        )
        self.readers = defaultdict(list)
        self.infos = {}
        self.laterals = {}
        scopes = list(root.traverse())
        for scope in scopes:
            for source in scope.sources.values():
                if isinstance(source, Scope):
                    self.readers[id(source)].append(scope)
            for branch in scope.set_operation_scopes:
                self.readers[id(branch)].append(scope)
        by_select = {id(s.expression): s for s in scopes}
        for scope in scopes:
            if isinstance(scope.expression, exp.Select):
                self.unknown_qualified(scope)
                self.infos[id(scope)] = self.scope(scope, scope is root)
            elif isinstance(scope.expression, exp.Lateral):
                # A LATERAL subquery is a source like any other, with the SELECT inside it.
                inner = by_select.get(id(_unnest(scope.expression.this)))
                if inner is not None and id(inner) in self.infos:
                    self.infos[id(scope)] = self.infos[id(inner)]
                    self.laterals[id(scope)] = inner
        try:
            sites = list(self.sites(root))
        except _QUALIFY_ERRORS:
            return
        for site in sites:
            self.measure(site)
            self.units(site)

    # Names

    def names(self, lineage: StatementLineage, query: exp.Query) -> None:
        graph = self.graph
        for key in lineage.reads:
            if graph.node_for_relation(key) is None:
                labels = sorted({graph.label(n) for n in graph.relations()} | set(self.checker.schema))
                close = difflib.get_close_matches(key.rsplit('.', 1)[-1], labels, n=3)
                hint = f'; did you mean {", ".join(close)}?' if close else ''
                self.add(
                    Finding(
                        'unknown-relation',
                        'warning',
                        f'{key} is not a relation of the project{hint}',
                        'Read a relation the project has, or build the graph with the warehouse that holds it.',
                    )
                )
        # A PIVOT or UNPIVOT makes new columns out of a relation's, which it does not have.
        pivoted = {
            table_key(t, self.checker.default_catalog, self.checker.default_db)
            for t in query.find_all(exp.Table)
            if t.args.get('pivots') or any(s.args.get('pivots') for s in _ancestors(t, exp.Subquery))
        }
        used = [(s.relation, s.column) for sources in lineage.columns.values() for s in sources]
        for relation_key, column in [*used, *lineage.references]:
            if relation_key not in pivoted:
                self.unknown_column(self.graph.node_for_relation(relation_key), column.split('.', 1)[0])

    def unknown_column(self, relation: Node | None, name: str) -> None:
        graph = self.graph
        if relation is None:
            return
        columns = graph.columns_of(relation.id)
        if not columns or graph.column(relation.id, name) is not None:
            return
        close = difflib.get_close_matches(name, [c.name for c in columns], n=3)
        hint = f'; did you mean {", ".join(close)}?' if close else ''
        self.add(
            Finding(
                'unknown-column',
                'warning',
                f'{graph.label(relation)} has no column {name}{hint}',
                f'Read one of the columns of {graph.label(relation)}.',
            ),
            relation.id,
            name.lower(),
        )

    def unknown_qualified(self, scope: Scope) -> None:
        """A column the query names by its relation's alias (``o.amount_total``), which the
        lineage leaves out when the relation has no such column."""
        for column in scope.columns:
            source = scope.sources.get(column.table)
            if isinstance(source, exp.Table) and not source.args.get('pivots'):
                self.unknown_column(self.relation_of(source), column.name)
            elif isinstance(source, Scope) and column.table:
                # A CTE or subquery has the columns it selects, unless it selects * from something.
                branches = _branches(source)
                if not branches or any(
                    isinstance(b.expression, exp.Select)
                    and (b.expression.is_star or b.expression.args.get('pivots'))
                    for b in branches
                ):
                    continue
                outputs = _names(source)
                if column.name.lower() not in outputs:
                    close = difflib.get_close_matches(column.name.lower(), outputs, n=3)
                    hint = f'; did you mean {", ".join(close)}?' if close else ''
                    self.add(
                        Finding(
                            'unknown-column',
                            'warning',
                            f'{column.table} has no column {column.name}{hint}',
                            f'Read one of the columns {column.table} selects.',
                        ),
                        f'scope:{column.table}',
                        column.name.lower(),
                    )

    def node(self, relation_key: str, column: str) -> Node | None:
        relation = self.graph.node_for_relation(relation_key)
        return self.graph.column(relation.id, column) if relation is not None else None

    def read(self, lineage: StatementLineage) -> dict[str, Node]:
        read: dict[str, Node] = {}
        pairs = [(s.relation, s.column) for sources in lineage.columns.values() for s in sources]
        for relation_key, name in [*pairs, *lineage.references]:
            found = self.node(relation_key, name)
            if found is not None:
                read[found.id] = found
        return read

    def used(self, lineage: StatementLineage) -> None:
        """The terms the query reads through their own columns."""
        names = set(self.result.terms)
        for sources in lineage.columns.values():
            for source in sources:
                found = self.node(source.relation, source.column)
                if found is not None:
                    names |= {t.name for t in self._held_by(found) if t.status != 'retired'}
        self.result.terms = sorted(names)

    def _held_by(self, column: Node) -> list[_Term]:
        """The terms bound to ``column`` or to the column it copies unchanged."""
        checker = self.checker
        terms = list(checker.bound_to.get(column.id, []))
        current = column.id
        while True:
            edges = self.graph.out_edges(current, 'derived_from')
            if len(edges) != 1 or edges[0].attrs.get('transform') != 'direct':
                break
            current = edges[0].target
            terms += [t for t in checker.bound_to.get(current, []) if t not in terms]
        return terms

    # Measures: the aggregates that produce output columns

    def sites(self, root: Scope) -> Iterator[_Site]:
        names = _names(root)
        for branch in _branches(root):
            select = branch.expression
            assert isinstance(select, exp.Select)
            for index, expression in enumerate(select.expressions):
                if index < len(names):
                    yield from self.resolve(branch, expression, names[index], 0)

    def resolve(self, scope: Scope, expression: exp.Expr, name: str, depth: int) -> Iterator[_Site]:
        """The measures behind one output expression, followed through the CTEs and subqueries it
        passes through unchanged, and into a scalar subquery it is."""
        if depth > 20:
            return
        core = _strip(_output(expression))
        select = scope.expression
        if isinstance(core, exp.Column):
            source = scope.sources.get(core.table)
            if isinstance(source, Scope):
                yield from self.through(source, core.name.lower(), name, depth)
            return
        if isinstance(core, exp.Subquery):
            inner = next((s for s in scope.subquery_scopes if s.expression is core.this), None)
            if inner is not None and len(_names(inner)) == 1:
                for branch in _branches(inner):
                    assert isinstance(branch.expression, exp.Select)
                    yield from self.resolve(branch, branch.expression.expressions[0], name, depth + 1)
            return
        if not isinstance(select, exp.Select) or core.find(exp.Select) is not None:
            return  # a value that a query computes on the way is not compared
        own = {id(a) for a in _aggregates(select)} | {id(a) for _, a in _windowed(select)}
        aggregates: list[exp.Expr] = [a for a in core.find_all(exp.AggFunc) if id(a) in own]
        if aggregates:
            yield self.site(scope, core, aggregates, name)

    def through(self, source: Scope, column: str, name: str, depth: int) -> Iterator[_Site]:
        names = _names(source)
        if column not in names:
            return
        index = names.index(column)
        for branch in _branches(source):
            select = branch.expression
            assert isinstance(select, exp.Select)
            if index < len(select.expressions):
                yield from self.resolve(branch, select.expressions[index], name, depth + 1)

    def site(self, scope: Scope, core: exp.Expr, aggregates: list[exp.Expr], name: str) -> _Site:
        checker = self.checker
        select = scope.expression
        assert isinstance(select, exp.Select)
        tree = checker.parse(self.canon.expression(core, scope, self.schema))
        rows = self.rows(scope, [c for a in aggregates for c in _own_columns(a, select)] or None)
        form = _Form(None, frozenset(), False)
        shown = None
        reads: list[Node] = []
        if tree is not None:
            formula, condition = _formula(tree)
            for column in formula.find_all(exp.Column):
                node = checker.resolved(column)
                if node is not None and node not in reads:
                    reads.append(node)
            shown = checker.short(formula)
            # Composed whole, so that the columns of a FILTER or CASE count among its sources, as
            # they do for the graph's columns.
            form = checker.form(tree.sql(dialect=checker.sqlglot))
            conditions = form.conditions
            if form.formula is None and condition is not None:
                parts = [
                    checker.condition(p, checker.resolved, 'WHERE')
                    for p in split_conjuncts(condition, between=True)
                ]
                conditions = [p for p in parts if p is not None]
            for part in conditions:
                part.written = True
                rows.conditions.setdefault(part.key, part)
        return _Site(name, scope, core, aggregates, rows, form, shown, reads)

    def rows(self, scope: Scope, columns: list[exp.Column] | None, depth: int = 0) -> _Rows:
        """The filters on the rows that ``columns`` of ``scope`` come from (every row, for None) and
        the relations those rows are read from."""
        found = _Rows()
        select = scope.expression
        if depth > 20 or not isinstance(select, exp.Select):
            return found
        checker = self.checker
        sources = {alias: source for alias, (_, source) in scope.selected_sources.items()}
        aliases = {c.table for c in columns} & set(sources) if columns else set(sources)
        relations = {relation.id for a in aliases if (relation := self.relation_of(sources[a])) is not None}
        optional = {
            str(j.this.alias_or_name): str(j.side or '').upper() for j in select.args.get('joins') or []
        }
        same = self.same_rows(scope, aliases, sources, optional)
        others: dict[str, list[tuple[exp.Expr, str]]] = defaultdict(list)

        def add(part: exp.Expr, label: str, own: bool, into: _Rows) -> None:
            tree = checker.parse(self.canon.expression(part, scope, self.schema))
            for piece in split_conjuncts(tree, between=True) if tree is not None else []:
                made = checker.condition(piece, checker.resolved, label)
                if made is not None:
                    made.written, made.own = True, own
                    into.conditions.setdefault(made.key, made)

        def written(original: exp.Expr, label: str) -> None:
            part = self.listed_values(scope, _constant_queries(original))
            if part.find(exp.Select) is not None:
                self.semi(scope, original, aliases, found)
                return
            columns = [c for c in original.find_all(exp.Column) if c.find_ancestor(exp.Select) is select]
            used = {c.table for c in columns}
            if not used or len(columns) != len(list(part.find_all(exp.Column))):
                return
            siblings = {
                a
                for a in used - aliases
                if (relation := self.relation_of(sources.get(a))) is not None and relation.id in relations
            }
            if siblings - same:
                # Another alias of the same relation, not tied to the measure's rows by their key,
                # filters other rows.
                for alias in siblings - same:
                    others[alias].append((part, label))
                return
            add(part, label, used <= aliases, found)

        where = select.args.get('where')
        if isinstance(where, exp.Where):
            for part in split_conjuncts(where.this):
                written(part, 'WHERE')
        for join in select.args.get('joins') or []:
            on = join.args.get('on')
            side = str(join.side or '').upper()
            if not isinstance(on, exp.Expr) or side in ('RIGHT', 'FULL'):
                continue
            for part in split_conjuncts(on):
                if isinstance(part, exp.EQ) and all(
                    isinstance(p, exp.Column) for p in (part.this, part.expression)
                ):
                    continue  # the join's own equality
                if side == 'LEFT' and aliases != {join.this.alias_or_name}:
                    continue  # a LEFT JOIN's ON clause only filters the rows it joins
                written(part, 'WHERE')
        for key, label in (('having', 'HAVING'), ('qualify', 'QUALIFY')):
            clause = select.args.get(key)
            if isinstance(clause, exp.Expr):
                for part in split_conjuncts(clause.this):
                    written(part, label)
        aside = _Rows()
        for parts in others.values():
            for part, label in parts:
                add(part, label, False, aside)
        found.nearby |= {
            self.checker.origin(n.id) for c in aside.conditions.values() for n in c.columns.values()
        }
        # A relation joined to the measure's own (not on the optional side of a LEFT JOIN) is where a
        # filter on it goes, too.
        for alias in sorted(set(sources) - aliases - set(optional)):
            relation = self.relation_of(sources[alias])
            if relation is not None:
                found.tables.append((scope, alias, relation))
        for alias in sorted(same):
            joined = sources[alias]
            if isinstance(joined, Scope):
                tied = self.inner_rows(joined, None, depth + 1)
                found.conditions.update(
                    {k: v for k, v in tied.conditions.items() if k not in found.conditions}
                )
                found.nearby |= tied.nearby
        for alias in sorted(aliases):
            source = sources[alias]
            names = [c.name.lower() for c in columns or [] if c.table == alias]
            if isinstance(source, exp.Table):
                relation = self.relation_of(source)
                if relation is None:
                    continue
                found.tables.append((scope, alias, relation))
                # The filters that hold on every row the measure reads here: on each of its
                # columns, or on every column of the relation for a count of its rows.
                nodes = [n for name in names if (n := self.graph.column(relation.id, name)) is not None]
                for held_part in checker.held_on(nodes or self.graph.columns_of(relation.id)):
                    found.conditions.setdefault(held_part.key, held_part)
            elif isinstance(source, Scope):
                found.merge(self.inner_rows(source, names or None, depth + 1))
        return found

    def listed_values(self, scope: Scope, part: exp.Expr) -> exp.Expr:
        """``x IN (SELECT v FROM t)`` as ``x IN (...)`` when ``t`` is a CTE or subquery of a
        ``VALUES`` list, which reads no relation: its values are written out."""
        if part.find(exp.Select) is None:
            return part
        subs = {s.expression.sql(): s for s in scope.subquery_scopes}

        def fold(node: exp.Expr) -> exp.Expr:
            query = _unnest(node.args.get('query')) if isinstance(node, exp.In) else None
            sub = subs.get(query.sql()) if query is not None else None
            names = _names(sub) if sub is not None else []
            values = _values_of(sub, names[0]) if sub is not None and len(names) == 1 else None
            if values:
                return exp.In(this=node.this.copy(), expressions=[v.copy() for v in values])
            return node

        folded = part.copy().transform(fold, copy=False)
        return part if folded.find(exp.Select) is not None else folded

    def keyed(self, scope: Scope, alias: str, column: exp.Column) -> str | None:
        """The column ``alias.column`` copies, when it holds one row each of ``alias``'s rows: a
        key of the relation or of the subquery it is read from."""
        if column.table != alias:
            return None
        source = scope.sources.get(alias)
        unique: list[frozenset[str]] = []
        relation = self.relation_of(source)
        if relation is not None:
            unique = self.checker.relation_keys(relation)
        elif isinstance(source, Scope) and id(source) in self.infos:
            unique = self.infos[id(source)].unique
        node = self.physical(scope, column)
        if node is None or frozenset({column.name.lower()}) not in unique:
            return None
        return self.checker.origin(node.id)

    def same_rows(
        self, scope: Scope, aliases: set[str], sources: dict[str, Any], optional: dict[str, str]
    ) -> set[str]:
        """The other sources of ``scope`` joined (not optionally) to one of ``aliases`` on its key,
        through the same column: each of their rows is one of the measure's, so their filters are
        its filters."""
        select = scope.expression
        assert isinstance(select, exp.Select)
        found = set()
        for eq in self.equalities(select, set(sources)):
            for mine, mine_column, theirs, their_column in (
                (eq.left, eq.left_column, eq.right, eq.right_column),
                (eq.right, eq.right_column, eq.left, eq.left_column),
            ):
                if mine not in aliases or theirs in aliases or optional.get(theirs):
                    continue
                key = self.keyed(scope, mine, exp.column(mine_column, table=mine))
                node = self.physical(scope, exp.column(their_column, table=theirs))
                if key is not None and node is not None and self.checker.origin(node.id) == key:
                    found.add(theirs)
        return found

    def semi(self, scope: Scope, part: exp.Expr, aliases: set[str], found: _Rows) -> None:
        """A condition with a query in it: ``k IN (SELECT k ...)`` or ``EXISTS (SELECT ... WHERE
        k = outer.k)`` on the measure's key applies the subquery's filters to the measure's rows;
        any other that ties the subquery to the measure's rows only has filters nearby."""
        negated = isinstance(part, exp.Not)
        inner = part.this if negated else part
        query = next(
            (q for q in inner.find_all(exp.Select) if q.find_ancestor(exp.Select) is scope.expression), None
        )
        sub = next((s for s in scope.subquery_scopes if s.expression is query), None)
        if sub is None or not isinstance(sub.expression, exp.Select):
            return
        key: str | None = None
        tied = False
        if isinstance(inner, exp.In) and isinstance(inner.this, exp.Column) and inner.this.table in aliases:
            tied = True
            outputs = sub.expression.expressions
            value = _output(outputs[0]) if len(outputs) == 1 else None
            node = self.physical(sub, value) if isinstance(value, exp.Column) else None
            mine = self.keyed(scope, inner.this.table, inner.this)
            if node is not None and mine is not None and self.checker.origin(node.id) == mine:
                key = mine
        elif isinstance(inner, exp.Exists):
            where = sub.expression.args.get('where')
            for eq in split_conjuncts(where.this) if isinstance(where, exp.Where) else []:
                if not isinstance(eq, exp.EQ):
                    continue
                for outer, own in ((eq.this, eq.expression), (eq.expression, eq.this)):
                    if not (isinstance(outer, exp.Column) and isinstance(own, exp.Column)):
                        continue
                    if outer.table in aliases and own.table in sub.sources:
                        tied = True
                        mine = self.keyed(scope, outer.table, outer)
                        node = self.physical(sub, own)
                        if mine is not None and node is not None and self.checker.origin(node.id) == mine:
                            key = mine
        # The measure's own columns the condition compares with the query's rows: what it filters.
        compared = [
            node
            for c in part.find_all(exp.Column)
            if c.find_ancestor(exp.Select) is scope.expression
            and c.table in aliases
            and (node := self.physical(scope, c)) is not None
        ]
        if not tied and not compared:
            return
        rows = self.rows(sub, None)
        written = {k: c for k, c in rows.conditions.items() if c.written}
        if key is not None and not negated:
            for condition in written.values():
                found.conditions.setdefault(condition.key, condition)
        else:
            found.nearby |= {self.checker.origin(n.id) for c in written.values() for n in c.columns.values()}
            found.nearby |= {self.checker.origin(n.id) for n in compared if not (tied and key is not None)}

    def inner_rows(self, source: Scope, names: list[str] | None, depth: int) -> _Rows:
        """The rows the outputs ``names`` of a CTE or subquery come from (every row, for None). Of a
        UNION, only the filters every branch applies, and no relation to add a filter to."""
        outputs = _names(source)
        branches = []
        for branch in _branches(source):
            select = branch.expression
            assert isinstance(select, exp.Select)
            columns: list[exp.Column] = []
            for name in names or []:
                if name in outputs and outputs.index(name) < len(select.expressions):
                    columns += _own_columns(select.expressions[outputs.index(name)], select)
            branches.append(self.rows(branch, columns or None, depth))
        if len(branches) == 1:
            return branches[0]
        shared = set.intersection(*(set(b.conditions) for b in branches)) if branches else set()
        return _Rows({k: branches[0].conditions[k] for k in shared})

    def measure(self, site: _Site) -> None:
        """Check one measure against the terms it reads, the terms it is named for, and the
        columns of the project it is named like."""
        checker = self.checker
        bound: dict[str, _Term] = {}
        sliced: dict[str, str] = {}
        for node in site.reads:
            held = self._held_by(node)
            for term in held:
                if term.status != 'retired':
                    bound.setdefault(term.name, term)
            for term, root, extra in [] if held else checker.slices(node):
                if term.name not in bound:
                    bound[term.name] = term
                    sliced[term.name] = (
                        f'{self.graph.label(node)} computes {term.name!r} as {self.graph.label(root)} does, '
                        f'over the rows where {" and ".join(c.text for c in extra)}'
                    )
        for term in bound.values():
            if term.measure:
                # Read as a slice of the term's column, the term is the formula's, not a binding's.
                severity = _capped(term.severity) if term.name in sliced else term.severity
                for condition in checker.required(term):
                    self.missing(condition, term, site, severity, None, sliced.get(term.name))
        named = [t for t in checker.terms_named(site.output) if t.name not in bound]
        for term in named:
            verdict = self.compare(site, term.roots, term) if term.measure else None
            # A formula compared with the term's says how it differs; one that cannot be compared
            # is checked for reading other terms' columns instead.
            if verdict is None and self.conflicting(site, term):
                continue
            severity = term.severity if verdict == 'same' else _capped(term.severity)
            for condition in checker.required(term):
                self.missing(condition, term, site, severity, None)
        if bound or named:
            return
        read = {n.id for n in site.reads}
        ancestors = read.union(*(checker.upstream(n) for n in read))
        same = [c for c in checker.columns_named(site.output) if c.id not in ancestors]
        if same:
            self.compare(site, same, None)

    def conflicting(self, site: _Site, term: _Term) -> bool:
        """Report a measure named for ``term`` that reads none of its columns, nor a column they
        are computed from or one computed from them, and reads only columns of other terms, or ones
        their columns are computed from: ``revenue`` added up from the order total."""
        checker, label = self.checker, self.graph.label
        if not term.bound or not site.reads:
            return False
        mine = {n.id for n in site.reads}.union(*(checker.upstream(n.id) for n in site.reads))
        own = {c.id for c in term.bound}.union(*(checker.upstream(c.id) for c in term.bound))
        if mine & own:
            return False
        unit = term.node.attrs.get('unit')
        held_by = {
            read.id: [
                (other, held)
                for other in checker.terms
                if other is not term and other.status != 'retired'
                for held in other.bound
                if read.id == held.id or read.id in checker.upstream(held.id)
            ]
            for read in site.reads
        }
        # A column no other term holds may be how this term is computed another way (the order
        # total less its tax, for revenue): only a measure made of other terms' columns alone is
        # computed from them.
        if not all(held_by.values()):
            return False
        for read in site.reads:
            holding = held_by[read.id]
            # Of the terms that hold the column, the one most like this term: an amount of the same
            # unit, then one that does not filter its rows.
            for other, held in sorted(
                holding,
                key=lambda h: (
                    h[0].node.attrs.get('unit') != unit,
                    bool(h[0].node.attrs.get('filters')),
                    h[0].name,
                ),
            )[:1]:
                columns = ', '.join(sorted(label(c) for c in term.bound))
                self.add(
                    Finding(
                        'conflicting-column',
                        _capped(term.severity),
                        f'{site.output} is named for {term.name!r}, but reads {label(read)}, which holds '
                        f'{other.name!r}, not {term.name!r} ({columns})',
                        f'Compute {term.name!r} from {", ".join(checker.describe(c) for c in term.roots)}, or '
                        'name the column for what it computes.',
                        column=_shown(site.output),
                        term=term.name,
                        detail=f'reads {label(read)}',
                        evidence=[
                            f'{term.name!r} ({term.where}) is {columns}',
                            f'{other.name!r} ({other.where}) is {label(held)}',
                        ],
                    ),
                    site.output,
                    term.name,
                )
                return True
        return False

    def compare(self, site: _Site, candidates: list[Node], term: _Term | None) -> str | None:
        """Compare a measure with the columns that hold the idea it is named for: ``same`` when one
        computes it the same way from the same sources, ``differs`` when none does, None when they
        cannot be compared."""
        checker, graph = self.checker, self.graph
        mine = site.form
        if mine.formula is None or not mine.complete:
            return None
        if term is not None and len(term.roots) > 1:
            # A term bound to the columns it is computed from (gross profit: price and cost) is
            # not computed as any one of them; a measure that reads several of them is not compared.
            sources = [
                checker.form(None, r).sources
                if isinstance(r.attrs.get('expression'), str)
                else frozenset({r.id})
                for r in term.roots
            ]
            apart = all(not (a & b) for i, a in enumerate(sources) for b in sources[i + 1 :])
            if apart and sum(1 for s in sources if s & mine.sources) > 1:
                bound = ', '.join(sorted(graph.label(r) for r in term.roots))
                self.add(
                    Finding(
                        'formula-differs',
                        'info',
                        f'{site.output} computes {term.name!r} from several of the columns it is bound to '
                        f'({bound}), and nothing says how they combine, so the formula was not compared',
                        f'Check {site.shown or site.output} against the definition of {term.name!r} by hand.',
                        column=_shown(site.output),
                        term=term.name,
                        detail=site.shown,
                        evidence=[f'{term.name!r} ({term.where}) is bound to {bound}'],
                    ),
                    site.output,
                    term.name,
                )
                return None
        compared: list[tuple[Node, _Form]] = []
        for candidate in sorted(candidates, key=graph.label):
            if not isinstance(candidate.attrs.get('expression'), str):
                continue
            theirs = checker.form(None, candidate)
            if theirs.formula is None or not theirs.complete:
                continue
            if term is None and not (mine.sources & theirs.sources):
                continue  # the same name over other data is another idea, not another formula
            compared.append((candidate, theirs))
        if not compared:
            return None
        idea = term.name if term is not None else site.output
        same = [(c, f) for c, f in compared if f.formula == mine.formula and f.sources == mine.sources]
        if not same:
            self.differs(site, compared, term, idea)
            return 'differs'
        present = site.rows.conditions
        groups = self.result_groups(site)
        verdicts = []
        for candidate, theirs in same:
            held = [*checker.held_filters(candidate), *theirs.conditions]
            # Dropping the NULL group of the result's own groups changes no other group, written
            # in the query or not.
            dropped = {p.key for p in held if checker.null_group(p, candidate, groups)}
            conditions = {p.key: p for p in held if p.key not in dropped}
            missing = [p for k, p in conditions.items() if k not in present]
            extra = [p for k, p in present.items() if p.written and k not in conditions and k not in dropped]
            verdicts.append((len(missing) + len(extra), graph.label(candidate), candidate, missing, extra))
        _, _, candidate, missing, extra = min(verdicts, key=lambda v: (v[0], v[1]))
        severity = term.severity if term is not None else 'warning'
        for condition in missing:
            self.missing(condition, term, site, severity, candidate)
        values = {checker.origin(n.id) for n in site.reads}
        # HAVING chooses groups, which is a slice of the question, not the rows the measure adds up.
        # Any exclusion is the same question, on whichever relation or column it is written; a test
        # of the values themselves is one only on the measure's own rows.
        additive = len(site.aggregates) == 1 and isinstance(site.aggregates[0], exp.Sum)
        excluded = [
            p
            for p in extra
            if p.label == 'WHERE'
            and not (additive and _drops_zeros(p))
            and (_excludes(p) or (p.own and not self._dated(p) and self._on_values(p, values)))
        ]

        def columns(conditions: list[_Condition]) -> set[str]:
            return {checker.origin(n.id) for c in conditions for n in c.columns.values()}

        # A column the definition filters is one it has a view on; any other the query leaves rows
        # out by may be the question's own choice, so that is at most a warning.
        defined = columns([*checker.held_filters(candidate), *(checker.required(term) if term else [])])
        for condition in excluded:
            tested = columns([condition])
            if tested & columns(missing):
                continue  # the missing filter's finding already names it
            self.add(
                Finding(
                    'formula-differs',
                    (severity if tested & defined else _capped(severity)) if term is not None else 'info',
                    f'{site.output} computes {idea!r} as {graph.label(candidate)} does, but with '
                    f'{condition.text}, which leaves out rows it keeps',
                    f'Drop {condition.text}, or name the column for what it computes. '
                    f'Or read {checker.describe(candidate)}.',
                    column=_shown(site.output),
                    term=term.name if term is not None else None,
                    detail=f'with {condition.text}',
                    evidence=[f'{graph.label(candidate)} = {checker.explainer.composed_text(candidate)}'],
                ),
                site.output,
                condition.key,
            )
        if not missing and not extra:
            self.add(
                Finding(
                    'recomputes-measure',
                    'info',
                    f'{site.output} recomputes {idea!r} as {graph.label(candidate)} does',
                    f'Read {checker.describe(candidate)}, so the answer stays in step with its definition.',
                    column=_shown(site.output),
                    term=term.name if term is not None else None,
                    detail=site.shown,
                ),
                site.output,
                candidate.id,
            )
        return 'same'

    def _on_values(self, condition: _Condition, values: set[str]) -> bool:
        """Whether a filter tests the values the measure adds up (``amount > 10``), which changes
        what it measures, rather than ``IS NOT NULL``, which an aggregate ignores anyway."""
        tree = condition.tree
        if isinstance(tree, exp.Not) and isinstance(tree.this, exp.Is):
            return False
        origins = {self.checker.origin(n.id) for n in condition.columns.values()}
        return bool(origins) and origins <= values

    def _dated(self, condition: _Condition) -> bool:
        """Whether a filter is on a date or time: a period of the question, not a definition."""
        return any(
            (n.data_type or '').upper().startswith(('DATE', 'TIME')) for n in condition.columns.values()
        )

    def differs(self, site: _Site, compared: list[tuple[Node, _Form]], term: _Term | None, idea: str) -> None:
        checker, graph = self.checker, self.graph
        mine = site.form
        candidate, theirs = compared[0]
        label = graph.label
        differences = []
        only_mine = sorted(label(graph.nodes[s]) for s in mine.sources - theirs.sources if s in graph.nodes)
        only_theirs = sorted(label(graph.nodes[s]) for s in theirs.sources - mine.sources if s in graph.nodes)
        if only_mine:
            differences.append(f'reads {", ".join(only_mine)}, which {label(candidate)} does not')
        if only_theirs:
            differences.append(f'leaves out {", ".join(only_theirs)}')
        top_mine = _aggregate_of(mine.formula, checker.dialect)
        top_theirs = _aggregate_of(theirs.formula, checker.dialect)
        if top_mine != top_theirs:
            differences.append(f'aggregates with {top_mine or "no aggregate"} against {top_theirs or "none"}')
        held = ', '.join(checker.describe(c) for c, _ in compared)
        self.add(
            Finding(
                'formula-differs',
                _capped(term.severity) if term is not None else 'info',
                f'{site.output} computes {idea!r} as {site.shown}, but {label(candidate)} computes it as '
                f'{checker.explainer.composed_text(candidate)}',
                f'Read {held} instead, or compute it the same way.',
                column=_shown(site.output),
                term=term.name if term is not None else None,
                detail=site.shown,
                evidence=differences,
            ),
            site.output,
            idea,
        )

    def units(self, site: _Site) -> None:
        """A measure whose result is still in the minor units of a currency (cents) that the project
        reads in whole units, unless its name says it is in minor units; and a filter that compares a
        column in minor units with the value a definition requires of the column that reads it in
        whole units, as if it were in whole units."""
        checker, label = self.checker, self.graph.label
        tree = checker.parse(site.form.formula) if site.form.formula is not None else None
        read = (
            [n for c in tree.find_all(exp.Column) if (n := checker.resolved(c)) is not None] if tree else []
        )
        minors = {n.id: m for n in read if (m := checker.minor(n)) is not None}
        scales = {m.scale for m in minors.values()}
        if tree is not None and len(scales) == 1 and not _minor_named(site.output):
            scale = scales.pop()

            def minor(column: exp.Column) -> bool:
                node = checker.resolved(column)
                return node is not None and node.id in minors

            if (_units(tree, minor, scale) or 0) > 0:
                column = next(n for n in read if n.id in minors)
                held = minors[column.id]
                fraction = _FRACTIONS[scale]
                name = _shown(site.output)
                text = site.core.sql(dialect=checker.sqlglot)
                self.add(
                    Finding(
                        'minor-units',
                        'warning',
                        f'{name or text} is in {fraction}: {label(column)} holds {fraction} of the unit '
                        f'{label(held.reader)} reads it in, and the query does not divide it by {scale}',
                        f'Divide {text} by {scale}, or read {label(held.reader)}.',
                        column=name,
                        detail=f'{label(column)} in {fraction}',
                        evidence=held.evidence,
                    ),
                    site.output,
                    str(id(site.scope)),
                )
        for condition in site.rows.conditions.values():
            if condition.written:
                self.threshold(condition)

    def not_null(self, condition: _Condition, present: dict[str, _Condition]) -> bool:
        """Whether ``condition`` is ``x IS NOT NULL`` and the query applies, on the same column, a
        filter that is never true where ``x`` is NULL: a comparison, IN, LIKE or BETWEEN with ``x``
        itself as an operand, negated or not, in a WHERE, HAVING or QUALIFY or in an inner join's
        ON, on the relations the measure's own columns come from. Such a filter keeps no row whose
        ``x`` is NULL. A test of ``x`` inside an OR, a COALESCE or a CASE, or with IS DISTINCT
        FROM, can be true there, so it does not count."""
        checker = self.checker
        tree = condition.tree
        while isinstance(tree, exp.Paren):
            tree = tree.this
        if not (isinstance(tree, exp.Not) and isinstance(tree.this, exp.Is)):
            return False
        test = tree.this
        if not (isinstance(test.this, exp.Column) and isinstance(test.expression, exp.Null)):
            return False
        node = condition.columns.get(test.this.sql(dialect=checker.sqlglot))
        if node is None:
            return False
        wanted = checker.origin(node.id)
        rejecting = (
            exp.EQ,
            exp.NEQ,
            exp.GT,
            exp.GTE,
            exp.LT,
            exp.LTE,
            exp.In,
            exp.Like,
            exp.ILike,
            exp.Between,
        )
        for applied in present.values():
            label = applied.label.upper()
            # Only a filter of the measure's own rows: one on a row tied to them (another order of
            # the customer, the first of a deduplicated subquery) keeps the measure's NULLs.
            if not (applied.written and applied.own) or not (
                label in _FILTER_LABELS or label in ('JOIN ON', 'INNER JOIN ON')
            ):
                continue
            part = applied.tree
            while isinstance(part, (exp.Paren, exp.Not)):
                part = part.this
            if not isinstance(part, rejecting):
                continue
            operands = [part.this] + ([] if isinstance(part, (exp.In, exp.Between)) else [part.expression])
            for operand in operands:
                while isinstance(operand, (exp.Paren, exp.Cast)):
                    operand = operand.this
                found = (
                    applied.columns.get(operand.sql(dialect=checker.sqlglot))
                    if isinstance(operand, exp.Column)
                    else None
                )
                if found is not None and checker.origin(found.id) == wanted:
                    return True
        return False

    def scaled(self, condition: _Condition, present: dict[str, _Condition]) -> bool:
        """Whether the query applies ``condition``, a comparison of a column in whole units with a
        number, to the column in minor units it is read from, on the measure's own rows, scaled:
        ``order_total >= 2000`` on cents for ``order_total >= 20`` on dollars."""
        checker = self.checker
        wanted = _compared(condition, checker.sqlglot)
        whole = checker.whole(wanted[0]) if wanted is not None else None
        if wanted is None or whole is None:
            return False
        if not checker.divides(wanted[0], *whole):
            return False
        for applied in present.values():
            mine = _compared(applied, checker.sqlglot) if applied.written and applied.own else None
            if (
                mine is not None
                and mine[0].id == whole[0]
                and mine[1] is wanted[1]
                and math.isclose(mine[2], wanted[2] * whole[1])
            ):
                return True
        return False

    def threshold(self, condition: _Condition) -> None:
        """Report a filter that compares a column in minor units with the very value a term's
        filter requires of the column that reads it in whole units: ``order_total >= 20`` on cents,
        where large orders are those of 20 dollars or more."""
        checker, label = self.checker, self.graph.label
        mine = _compared(condition, checker.sqlglot)
        held = checker.minor(mine[0]) if mine is not None else None
        if mine is None or held is None:
            return
        column, kind, value = mine
        for term in checker.terms:
            for required in checker.required(term) if term.status != 'retired' else []:
                theirs = _compared(required, checker.sqlglot)
                if theirs is None or theirs[1] is not kind or theirs[2] != value:
                    continue
                target = theirs[0]
                if checker.whole(target) != (column.id, held.scale):
                    continue  # the definition's column is in the same units as the query's
                fraction = _FRACTIONS[held.scale]
                self.add(
                    Finding(
                        'minor-units',
                        'warning',
                        f'the query compares {label(column)}, in {fraction}, with {_figure(value)}, the value '
                        f'{term.name!r} requires of {label(target)} = {label(column)} / {held.scale}',
                        f'Compare {label(column)} with {_figure(value * held.scale)}, or compare '
                        f'{label(target)} with {_figure(value)}.',
                        term=term.name,
                        detail=f'{condition.text} in {fraction}',
                        evidence=[f'required by {required.source}', *held.evidence],
                    ),
                    condition.key,
                    term.name,
                )
                return

    def missing(
        self,
        condition: _Condition,
        term: _Term | None,
        site: _Site,
        severity: str,
        held: Node | None,
        because: str | None = None,
    ) -> None:
        """Report ``condition`` if the rows the measure reads are not filtered by it; ``because``
        says why the term applies, when the query reads none of its columns."""
        present = site.rows.conditions
        contradicted = [p for p in present.values() if p.written and p.key == condition.opposite]
        if (
            condition.key in present or self.scaled(condition, present) or self.not_null(condition, present)
        ) and not contradicted:
            return
        name = term.name if term is not None else site.output
        if ('missing-filter', name, condition.key, site.output, str(id(site.scope))) in self.seen:
            return
        checker = self.checker
        origins = {checker.origin(n.id) for n in condition.columns.values()}
        similar = contradicted or [
            p
            for p in present.values()
            if p.written
            and p.label in _FILTER_LABELS
            and origins & {checker.origin(n.id) for n in p.columns.values()}
        ]
        unshown = next(
            (
                r
                for _, _, r in site.rows.tables
                if any(checker.unshown(c.id) for c in self.graph.columns_of(r.id))
            ),
            None,
        )
        if held is None and unshown is not None:
            # Not a defect the SQL is shown to have: the graph does not say what the relation filters.
            message = (
                f'{name!r} requires {condition.text}, and the graph cannot show whether '
                f'{self.graph.label(unshown)} applies it: it was built by an earlier Konstruct, which did '
                "not record which filters hold on each column's rows"
            )
            evidence = [f'required by {condition.source}'] if condition.source else []
            severity = _capped(severity)
        elif held is None:
            message = f'{name!r} requires {condition.text}, which the query does not apply'
            evidence = [f'required by {condition.source}'] if condition.source else []
        else:
            message = (
                f'{site.output} computes {name!r} as {self.graph.label(held)} does, but without its filter '
                f'{condition.text}'
            )
            evidence = [f'{condition.label} in {condition.source}'] if condition.source else []
        if because:
            evidence.append(because)
        if because and (similar or origins & self.grouped(site)):
            # A slice of a term's column that the query itself filters or groups by the term's
            # filter column: the query chose its rows, so only a note, and nothing to rewrite.
            message += '; the query chooses its rows by that column itself'
            severity = 'info'
            similar = similar or [condition]
        elif similar:
            message += f'; it filters the same column with {similar[0].text}'
            severity = _capped(severity)
        elif origins & site.rows.nearby:
            # The query filters that column on rows tied to the measure's in a way the check cannot
            # follow (an anti-join, say): likely deliberate, so only a note.
            message += '; the query filters that column elsewhere, on rows tied to these'
            severity = 'info'
        rewrite = None
        if condition.label == 'WHERE' and not similar:
            # Two terms that require one filter of one measure share the change, made once.
            placing = (str(id(site.scope)), site.output, condition.key)
            if placing not in self.placed:
                self.placed[placing] = self.place(condition, site)
            rewrite = self.placed[placing]
        fix = f'Add {condition.text} to the query.' if rewrite is None else f'Apply the filter: {rewrite}.'
        if held is None and unshown is not None:
            fix = f'Run konstruct build again, then check the query again. If it still reports this: {fix}'
        if held is not None:
            fix += f' Or read {checker.describe(held)}, which applies it.'
        self.add(
            Finding(
                'missing-filter',
                severity,
                message,
                fix,
                column=_shown(site.output),
                term=term.name if term is not None else None,
                detail=f'without {condition.text}',
                rewrite=rewrite,
                evidence=evidence,
            ),
            name,
            condition.key,
            site.output,
            str(id(site.scope)),
        )

    def retired(self, read: dict[str, Node], query: exp.Query) -> None:
        checker, graph = self.checker, self.graph
        for column in read.values():
            for term in checker.bound_to.get(column.id, []):
                if term.status != 'retired':
                    continue
                related = {r.lower() for r in term.node.attrs.get('related', []) or []}
                replacements = [
                    t
                    for t in checker.terms
                    if t.status != 'retired'
                    and t.bound
                    and (t.name.lower() in related or name_matches(term.name, *t.names))
                ]
                instead = [c for t in replacements for c in t.bound]
                sibling = next((c for c in instead if c.parent == column.parent), None)
                # Read only through SELECT *, the column is passed on rather than chosen.
                starred = not any(c.name.lower() == column.name.lower() for c in query.find_all(exp.Column))
                rewrite = self.rename(query, column, sibling) if sibling is not None and not starred else None
                if instead:
                    fix = f'Read {", ".join(checker.describe(c) for c in instead)} instead.'
                else:
                    fix = f'Ask the owner of {term.name!r} what replaces it.'
                self.add(
                    Finding(
                        'retired-column',
                        'info' if starred else 'warning',
                        f'{graph.label(column)} holds {term.name!r}, which is retired'
                        + (', and SELECT * passes it on' if starred else ''),
                        fix,
                        term=term.name,
                        rewrite=rewrite,
                        evidence=[f'{term.name!r} is retired ({term.where})'],
                    ),
                    column.id,
                    term.name,
                )

    # Joins

    def grouped(self, site: _Site) -> set[str]:
        """The origins of the columns the measure's query groups by."""
        select = site.scope.expression
        group = select.args.get('group') if isinstance(select, exp.Select) else None
        found: set[str] = set()
        for item in group.expressions if group is not None else []:
            if (
                isinstance(item, exp.Literal)
                and item.is_int
                and 0 < int(item.name) <= len(select.expressions)
            ):
                item = _strip(_output(select.expressions[int(item.name) - 1]))
            for column in item.find_all(exp.Column) if isinstance(item, exp.Expr) else []:
                node = self.physical(site.scope, column)
                if node is not None:
                    found.add(self.checker.origin(node.id))
        return found

    def result_groups(self, site: _Site) -> set[str]:
        """The origins of the columns the result is grouped by, one group per value, when the
        measure's own query is the result: grouped by the column itself, not a function of it, and
        without subtotal rows."""
        select = site.scope.expression
        group = select.args.get('group') if isinstance(select, exp.Select) else None
        if not isinstance(group, exp.Group) or _subtotals(group) or self.readers.get(id(site.scope)):
            return set()
        found: set[str] = set()
        for item in group.expressions:
            if (
                isinstance(item, exp.Literal)
                and item.is_int
                and 0 < int(item.name) <= len(select.expressions)
            ):
                item = _output(select.expressions[int(item.name) - 1])
            node = self.physical(site.scope, item) if isinstance(item, exp.Column) else None
            if node is not None:
                found.add(self.checker.origin(node.id))
        return found

    def physical(self, scope: Scope, column: exp.Column) -> Node | None:
        source = scope.sources.get(column.table)
        if isinstance(source, exp.Table):
            relation = self.relation_of(source)
            return self.graph.column(relation.id, column.name) if relation is not None else None
        if isinstance(source, Scope):
            info = self.infos.get(id(source))
            return info.physical.get(column.name.lower()) if info is not None else None
        return None

    def relation_of(self, source: Any) -> Node | None:
        if isinstance(source, exp.Table):
            return self.graph.node_for_relation(
                table_key(source, self.checker.default_catalog, self.checker.default_db)
            )
        return None

    def scope(self, scope: Scope, is_root: bool) -> _ScopeInfo:
        graph, infos = self.graph, self.infos
        select = scope.expression
        assert isinstance(select, exp.Select)
        aliases = list(scope.selected_sources)
        sources = {a: scope.selected_sources[a][1] for a in aliases}
        unique: dict[str, list[frozenset[str]]] = {}
        grouped: dict[str, list[frozenset[str]]] = {}
        names: dict[str, str] = {}
        for alias, source in sources.items():
            relation = self.relation_of(source)
            if relation is not None:
                unique[alias] = self.checker.relation_keys(relation)
                names[alias] = graph.label(relation)
            elif isinstance(source, Scope) and id(source) in infos:
                unique[alias] = infos[id(source)].unique
                grouped[alias] = infos[id(source)].grouped
                names[alias] = f'the subquery {alias}'
            else:
                unique[alias] = []
                names[alias] = alias
        group = select.args.get('group')
        aggregates = list(_aggregates(select))
        fixed: dict[str, set[str]] = defaultdict(set)
        nodes = {a: scope.selected_sources[a][0] for a in aliases}
        unnested = {
            a
            for a, node in nodes.items()
            if isinstance(node, (exp.Unnest, exp.TableFromRows))
            or (isinstance(node, exp.Lateral) and not isinstance(node.this, exp.Subquery))
        }
        for item in group.expressions if isinstance(group, exp.Group) and not _subtotals(group) else []:
            if isinstance(item, exp.Column) and item.table in sources:
                fixed[item.table].add(item.name.lower())
            elif len(tables := {c.table for c in item.find_all(exp.Column)}) == 1 and tables <= unnested:
                # Grouped by a value of the element alone (``f.value::string``), each group holds
                # one element, as grouping by the element does.
                fixed[next(iter(tables))].add(item.sql())
        equalities = self.equalities(select, set(aliases)) + self.correlated(sources)
        exploded = [a for a in aliases if a in unnested]
        # Grouped by the element, each group holds one element, which repeats no row.
        repeating = [a for a in exploded if not fixed.get(a)]
        # An ASOF join matches each row of the left side to at most one row of the side it joins
        # (whose rows can still repeat, once for each left row that matches them). That holds for
        # the grain of the join alone, not as a key of the relation.
        reaching = dict(unique)
        for join in select.args.get('joins') or []:
            if str(join.args.get('method') or '').upper() == 'ASOF':
                alias = join.this.alias_or_name
                matched = frozenset(
                    e.right_column if e.right == alias else e.left_column
                    for e in equalities
                    if e.join is not None and alias in (e.left, e.right)
                )
                reaching[alias] = [*unique.get(alias, []), matched]
        anti = self.anti_joined(select, equalities, unique)
        self.called = {
            a: self.checker.names_for(r) for a, src in sources.items() if (r := self.relation_of(src))
        }
        counted = [a for a in aliases if a not in anti and a not in exploded]
        joins = select.args.get('joins') or []

        def pairing(e: _Join) -> bool:
            # A LEFT or FULL join keeps every row of the sources before it: an equality in its ON
            # between two of them pairs nothing.
            join = joins[e.join] if e.join is not None and e.join < len(joins) else None
            return (
                join is None
                or str(join.side or '').upper() not in ('LEFT', 'FULL')
                or bool(_joined(join.this) & {e.left, e.right})
            )

        kept = [e for e in equalities if e.left in counted and e.right in counted and pairing(e)]
        safe = [
            e
            for e in self.equalities(select, set(aliases), exp.NullSafeEQ)
            if e.left in counted and e.right in counted and pairing(e)
        ]
        loose = [(e.left, e.left_column, e.right, e.right_column) for e in safe]
        pairs = [(e.left, e.left_column, e.right, e.right_column) for e in kept]
        reach = reachable(
            counted,
            pairs,
            reaching,
            fixed,
            loose,
            grouped,
        )
        fans: dict[str, _Fan] = {}
        preserved: list[str] = []
        for alias in counted:
            reached = reach[alias]
            if len(reached) == len(counted) and not repeating:
                preserved.append(alias)
                continue
            others = [a for a in counted if a not in reached]
            fan = self.fan(scope, alias, reached, others, kept, unique, names, safe, grouped)
            if fan is None and repeating:
                fan = _Fan(
                    names[alias],
                    repeating[0],
                    f'{repeating[0]} unnests an array, so each {names[alias]} row repeats once per element',
                    certain=True,
                )
            if fan is not None:
                fans[alias] = fan
        self.relationships(scope, equalities, unique)
        self.presumptions(scope, kept)
        info = _ScopeInfo()
        for expression in select.expressions:
            value = _output(expression)
            if isinstance(value, exp.Column):
                found = self.physical(scope, value)
                if found is not None:
                    info.physical[expression.alias_or_name.lower()] = found
        # Over a grouped SELECT a window runs over the groups, whose rows no join repeats.
        for window, agg in [] if group is not None or aggregates else _windowed(select):
            # Within a window's partition its columns are fixed, as a group's are.
            partition = {**fixed}
            for item in _partition(window, select) or []:
                if isinstance(item, exp.Column) and item.table in sources:
                    partition[item.table] = partition.get(item.table, set()) | {item.name.lower()}
            over = reachable(
                counted,
                pairs,
                reaching,
                partition,
                loose,
                grouped,
            )
            spread = [a for a in repeating if not partition.get(a)]
            windowed = {
                a: f
                for a in counted
                if len(over[a]) < len(counted) or spread
                if (f := fans.get(a)) is not None
            }
            self.aggregate(scope, select, agg, windowed, [a for a in counted if len(over[a]) == len(counted)])
        listed = _listing(select)
        if group is not None or aggregates:
            for agg in aggregates:
                self.aggregate(scope, select, agg, fans, preserved)
            if listed:
                # An UNNEST in the SELECT list runs after the grouping: each group's row repeats
                # once per element, so the groups make no key.
                for expression in select.expressions:
                    info.fanned[expression.alias_or_name.lower()] = _Fan(
                        'group',
                        'unnested element',
                        f'{listed[0].alias_or_name} unnests an array in the SELECT list, so each group '
                        'repeats once per element',
                        certain=True,
                    )
                return info
            info.unique = self.grouped_unique(select)
            info.grouped = list(info.unique)
            return info
        if listed:
            # An UNNEST in the SELECT list repeats each row once per element of the array.
            for alias in counted:
                fans[alias] = _Fan(
                    names[alias],
                    'unnested element',
                    f'{listed[0].alias_or_name} unnests an array in the SELECT list, so each {names[alias]} row '
                    'repeats once per element',
                    certain=True,
                )
        distinct = select.args.get('distinct') is not None
        if distinct and not listed:
            # Rows made distinct on a key of a source no longer repeat that source.
            for alias in list(fans):
                if any(self.outputs(select, alias, keys) is not None for keys in unique.get(alias, [])):
                    del fans[alias]
        for expression in select.expressions:
            name = expression.alias_or_name.lower()
            for column in _own_columns(expression, select):
                fan = fans.get(column.table)
                derived = sources.get(column.table)
                if fan is None and not distinct and isinstance(derived, Scope) and id(derived) in infos:
                    fan = infos[id(derived)].fanned.get(column.name.lower())
                if fan is not None:
                    info.fanned.setdefault(name, fan)
        if distinct:
            # A column the same on every row (a constant) tells no two rows apart.
            info.unique = [frozenset(e.alias_or_name.lower() for e in select.expressions if not _constant(e))]
            info.grouped = list(info.unique)
        limit = select.args.get('limit')
        if (
            isinstance(limit, exp.Limit)
            and isinstance(limit.expression, exp.Literal)
            and limit.expression.name == '1'
        ):
            info.unique = [frozenset()]  # at most one row
        # An UNNEST in the SELECT list runs after DISTINCT ON, QUALIFY and WHERE, so it repeats the
        # rows they keep: only its whole output, made DISTINCT, is unique.
        for alias in [] if listed else preserved:
            for keys in unique.get(alias, []):
                mapped = self.outputs(select, alias, keys)
                if mapped is not None and mapped not in info.unique:
                    info.unique.append(mapped)
        for keys in [] if listed else self.first_rows(select, sources):
            if keys not in info.unique:
                info.unique.append(keys)
            # A partition of NULL is one partition, so its first row is one row too.
            if keys not in info.grouped:
                info.grouped.append(keys)
        if is_root:
            self.grain(info)
        return info

    def first_rows(self, select: exp.Select, sources: dict[str, Any]) -> list[frozenset[str]]:
        """The output columns ``select`` keeps one row per value of, by keeping the first row of
        each partition: ``QUALIFY ROW_NUMBER() OVER (PARTITION BY k ...) = 1``, or ``WHERE rn = 1``
        over a single subquery whose ``rn`` is that ROW_NUMBER(). RANK() and DENSE_RANK() keep
        every row that ties, so they show nothing."""
        found: list[frozenset[str]] = []
        clauses = [select.args.get('qualify')]
        if len(sources) == 1 and not select.args.get('joins'):
            clauses.append(select.args.get('where'))
        for clause in clauses:
            if not isinstance(clause, (exp.Where, exp.Qualify)):
                continue
            for part in split_conjuncts(clause.this):
                kind = type(part)
                if kind not in _COMPARISONS:
                    continue
                for ranked, bound, compared in (
                    (part.this, part.expression, kind),
                    (part.expression, part.this, _COMPARISONS[kind]),
                ):
                    value = _number(bound)
                    first = (compared is exp.EQ and value == 1) or (compared is exp.LTE and value == 1)
                    first = first or (compared is exp.LT and value == 2)
                    if not first:
                        continue
                    keys = self.partition(select, sources, ranked)
                    if keys is not None and keys not in found:
                        found.append(keys)
        return found

    def partition(
        self, select: exp.Select, sources: dict[str, Any], ranked: exp.Expr
    ) -> frozenset[str] | None:
        """The output columns of ``select`` that pass through the PARTITION BY of the ROW_NUMBER()
        that ``ranked`` is, or reads from the select's single subquery."""
        window: exp.Expr | None = ranked
        inner: exp.Select | None = None
        alias = ''
        if isinstance(ranked, exp.Column):
            own = next(
                (e for e in select.expressions if e.alias_or_name.lower() == ranked.name.lower()), None
            )
            source = sources.get(ranked.table)
            if ranked.table and isinstance(source, Scope) and isinstance(source.expression, exp.Select):
                inner, alias = source.expression, ranked.table
                if _listing(inner):
                    return None  # its ROW_NUMBER numbers the rows before they repeat per element
                window = next(
                    (_output(e) for e in inner.expressions if e.alias_or_name.lower() == ranked.name.lower()),
                    None,
                )
            elif own is not None and not ranked.table:
                window = _output(own)
        if not (isinstance(window, exp.Window) and isinstance(window.this, exp.RowNumber)):
            return None
        partition = _partition(window, inner if inner is not None else select)
        if partition is None:
            return None
        names: set[str] = set()
        for column in partition:
            if not isinstance(column, exp.Column):
                return None
            if inner is None:
                mapped = self.outputs(select, column.table, frozenset({column.name.lower()}))
            else:
                # The subquery's output that passes the partition column through, then the
                # select's output that passes that one through.
                passed = next(
                    (
                        e.alias_or_name.lower()
                        for e in inner.expressions
                        if isinstance(value := _output(e), exp.Column)
                        and value.table == column.table
                        and value.name.lower() == column.name.lower()
                    ),
                    None,
                )
                mapped = self.outputs(select, alias, frozenset({passed})) if passed is not None else None
            if mapped is None:
                return None
            names |= mapped
        return frozenset(names)

    def outputs(self, select: exp.Select, alias: str, keys: frozenset[str]) -> frozenset[str] | None:
        """The output names that pass ``alias``'s ``keys`` through, or None if one is not selected."""
        found = set()
        for key in keys:
            name = next(
                (
                    e.alias_or_name.lower()
                    for e in select.expressions
                    if isinstance(value := _output(e), exp.Column)
                    and value.table == alias
                    and value.name.lower() == key
                ),
                None,
            )
            if name is None:
                return None
            found.add(name)
        return frozenset(found)

    def grouped_unique(self, select: exp.Select) -> list[frozenset[str]]:
        group = select.args.get('group')
        if group is None:
            return [frozenset()]  # an aggregate without GROUP BY returns one row
        if _subtotals(group):
            return []  # subtotal rows repeat the values of the columns they roll up
        if group.args.get('all'):
            # GROUP BY ALL groups by every item that is not an aggregate; one in a subquery of the
            # item is computed once per group, so the item is still a grouping item.
            return [
                frozenset(
                    e.alias_or_name.lower()
                    for e in select.expressions
                    if not any(_owner(a) is select for a in _output(e).find_all(exp.AggFunc, exp.Window))
                )
            ]
        names = set()
        for item in group.expressions:
            name = next(
                (e.alias_or_name.lower() for e in select.expressions if _output(e) == item),
                None,
            )
            if name is None:
                return []
            names.add(name)
        return [frozenset(names)]

    def equalities(self, select: exp.Select, aliases: set[str], kind: type[exp.Expr] = exp.EQ) -> list[_Join]:
        """The equalities (or, with ``kind``, the ``IS NOT DISTINCT FROM`` comparisons) between
        columns of two sources in the ON and WHERE clauses."""
        found = []
        clauses: list[tuple[int | None, exp.Expr | None]] = [
            (index, join.args.get('on')) for index, join in enumerate(select.args.get('joins') or [])
        ]
        where = select.args.get('where')
        clauses.append((None, where.this if isinstance(where, exp.Where) else None))
        for index, clause in clauses:
            if not isinstance(clause, exp.Expr):
                continue
            for position, part in enumerate(split_conjuncts(clause)):
                if not isinstance(part, kind):
                    continue
                left, right = part.this, part.expression
                if (
                    isinstance(left, exp.Column)
                    and isinstance(right, exp.Column)
                    and left.table in aliases
                    and right.table in aliases
                    and left.table != right.table
                ):
                    found.append(
                        _Join(
                            left.table,
                            left.name.lower(),
                            right.table,
                            right.name.lower(),
                            part,
                            index,
                            position,
                        )
                    )
        return found

    def anti_joined(
        self, select: exp.Select, equalities: list[_Join], unique: dict[str, list[frozenset[str]]]
    ) -> set[str]:
        """The sources of an anti-join, ``LEFT JOIN p ... WHERE p.k IS NULL`` on a column of the join
        or a key of ``p``: only rows with no match are kept, so they repeat nothing."""
        where = select.args.get('where')
        nulls = {
            (part.this.table, part.this.name.lower())
            for part in (split_conjuncts(where.this) if isinstance(where, exp.Where) else [])
            if isinstance(part, exp.Is)
            and isinstance(part.expression, exp.Null)
            and isinstance(part.this, exp.Column)
        }
        found = set()
        for join in select.args.get('joins') or []:
            alias = join.this.alias_or_name
            if str(join.side or '').upper() != 'LEFT':
                continue
            joined = {
                e.right_column if e.right == alias else e.left_column
                for e in equalities
                if e.join is not None and alias in (e.left, e.right)
            }
            keys = {next(iter(k)) for k in unique.get(alias, []) if len(k) == 1}
            if any(table == alias and (name in joined or name in keys) for table, name in nulls):
                found.add(alias)
        return found

    def correlated(self, sources: dict[str, Any]) -> list[_Join]:
        """The equalities a LATERAL subquery's WHERE clause makes between its rows and those of the
        sources before it, as equalities of the join: ``LATERAL (SELECT * FROM payments p WHERE
        p.order_id = o.order_id) p`` joins on ``order_id``."""
        found = []
        for alias, source in sources.items():
            inner = self.laterals.get(id(source)) if isinstance(source, Scope) else None
            select = inner.expression if inner is not None else None
            if inner is None or not isinstance(select, exp.Select):
                continue
            where = select.args.get('where')
            for position, part in enumerate(
                split_conjuncts(where.this) if isinstance(where, exp.Where) else []
            ):
                if not isinstance(part, exp.EQ):
                    continue
                for own, outer in ((part.this, part.expression), (part.expression, part.this)):
                    if not (isinstance(own, exp.Column) and isinstance(outer, exp.Column)):
                        continue
                    if own.table not in inner.sources or outer.table not in sources or outer.table == alias:
                        continue
                    name = next(
                        (
                            e.alias_or_name.lower()
                            for e in select.expressions
                            if isinstance(value := _output(e), exp.Column)
                            and value.table == own.table
                            and value.name.lower() == own.name.lower()
                        ),
                        None,
                    )
                    if name is not None:
                        found.append(
                            _Join(outer.table, outer.name.lower(), alias, name, part, None, position)
                        )
        return found

    def fan(
        self,
        scope: Scope,
        alias: str,
        reached: set[str],
        others: list[str],
        equalities: list[_Join],
        unique: dict[str, list[frozenset[str]]],
        names: dict[str, str],
        null_safe: list[_Join] | None = None,
        grouped: dict[str, list[frozenset[str]]] | None = None,
    ) -> _Fan | None:
        """Why ``alias``'s rows repeat, when the project gives evidence that they do. A source
        joined on every column of one of its unique keys to the other sources matches at most one
        of its rows for each combination of theirs, so it repeats nothing they do not: such sources
        are set aside one at a time, each against the sources still left, which then fix it. Only
        the equalities of an inner join's ON or of the WHERE clause fix a row: an outer join keeps
        the rows its ON does not match."""
        graph = self.graph
        left = set(reached) | set(others)
        select = scope.expression
        joins = (select.args.get('joins') or []) if isinstance(select, exp.Select) else []

        def inner(eq: _Join) -> bool:
            if eq.join is None:
                return True
            join = joins[eq.join] if eq.join < len(joins) else None
            return join is not None and not join.side and str(join.kind or '').upper() in ('', 'INNER')

        fixing = [eq for eq in equalities if inner(eq)]
        fixing_safe = [eq for eq in null_safe or [] if inner(eq)]

        def joined(joins: list[_Join], source: str) -> set[str]:
            return {
                eq.right_column if eq.right == source else eq.left_column
                for eq in joins
                if source in (eq.left, eq.right) and ({eq.left, eq.right} - {source}) <= left
            }

        def connected() -> bool:
            seen, stack = {alias}, [alias]
            while stack:
                current = stack.pop()
                for eq in [*equalities, *(null_safe or [])]:
                    for a, b in ((eq.left, eq.right), (eq.right, eq.left)):
                        if a == current and b in left and b not in seen:
                            seen.add(b)
                            stack.append(b)
            return seen == left

        changed = True
        while changed:
            changed = False
            for other in [o for o in others if o in left]:
                left.discard(other)
                strict = joined(fixing, other)
                covered = any(k and k <= strict for k in unique.get(other, [])) or any(
                    k and k <= strict | joined(fixing_safe, other) for k in (grouped or {}).get(other, [])
                )
                # Set aside, it must leave no source joined to the others through it alone.
                if covered and connected():
                    changed = True
                else:
                    left.add(other)
        for other in [o for o in others if o in left]:
            pairs = [
                (eq.left_column, eq.right_column) if eq.right == other else (eq.right_column, eq.left_column)
                # A join on IS NOT DISTINCT FROM alone is a join too.
                for eq in [*equalities, *(null_safe or [])]
                if other in (eq.left, eq.right) and ({eq.left, eq.right} - {other}) & reached
            ]
            if not pairs:
                continue
            columns = sorted({theirs for _, theirs in pairs})
            mine = {m for m, _ in pairs}
            for _, theirs in pairs:
                node = self.physical(scope, exp.column(theirs, table=other))
                if node is None:
                    continue
                rel = self.checker.foreign.get(node.id) or self.checker.foreign.get(
                    self.checker.origin(node.id)
                )
                if rel is not None:
                    return _Fan(
                        names[alias],
                        names[other],
                        f'{graph.label(node)} refers to {rel.target} ({rel.via}){_from_pack(rel)}, so many '
                        f'{names[other]} rows can match one {names[alias]} row',
                        f'{names[other]} to one row per {", ".join(columns)}',
                    )
            source = scope.sources.get(other)
            held = self.infos.get(id(source)) if isinstance(source, Scope) else None
            spread = next(
                (
                    f
                    for c in columns
                    if held is not None and (f := held.fanned.get(c)) is not None and f.certain
                ),
                None,
            )
            if spread is not None:
                # The subquery's own SQL repeats the rows its join column comes from.
                return _Fan(
                    names[alias],
                    names[other],
                    f'{spread.evidence}, so many {names[other]} rows can match one {names[alias]} row',
                    f'{names[other]} to one row per {", ".join(columns)}',
                )
            keys = sorted((sorted(k) for k in unique.get(other, []) if k), key=lambda k: (len(k), k))
            if keys:
                key = ', '.join(keys[0])
                return _Fan(
                    names[alias],
                    names[other],
                    f'{names[other]} has one row per {key}{self.presumption(scope, other, keys[0])}, not per '
                    f'{", ".join(columns)}, so many of its rows can match one {names[alias]} row',
                    f'{names[other]} to one row per {", ".join(columns)}',
                )
            matched = next((k for k in unique.get(alias, []) if k and k <= mine), None)
            if matched is not None:
                return _Fan(
                    names[alias],
                    names[other],
                    f'{names[alias]} is joined on its key{self.presumption(scope, alias, sorted(matched))}, and '
                    f'nothing says {names[other]} has one row per {", ".join(columns)}',
                    f'{names[other]} to one row per {", ".join(columns)}',
                    severity='info',
                )
        # Each condition between sources, with whether it pairs their rows: a LEFT or FULL join's ON
        # pairs only the rows of the source it joins, and keeps every row of the others.
        conditions: list[tuple[exp.Expr, bool]] = []
        where = select.args.get('where') if isinstance(select, exp.Select) else None
        for clause, by in [
            *((j.args.get('on'), j) for j in joins),
            (where.this if isinstance(where, exp.Where) else None, None),
        ]:
            outer = by is not None and str(by.side or '').upper() in ('LEFT', 'FULL')
            for part in split_conjuncts(clause) if isinstance(clause, exp.Expr) else []:
                tables = {c.table for c in part.find_all(exp.Column)}
                if len(tables) > 1:
                    conditions.append((part, not outer or bool(_joined(by.this) & tables)))
        for other in [o for o in others if o in left]:
            touching = [
                (p, pairs) for p, pairs in conditions if other in {c.table for c in p.find_all(exp.Column)}
            ]
            paired = [p for p, pairs in touching if pairs]
            if any(isinstance(p, (exp.EQ, exp.NullSafeEQ)) for p in paired):
                continue  # joined on an equality the check cannot read as a key, as before
            if any(isinstance(p, _RANGES) for p in paired) or len(paired) < len(touching):
                # Joined on a range alone, or on a condition that pairs nothing, any number of its
                # rows can match.
                return _Fan(
                    names[alias],
                    names[other],
                    f'{names[other]} is joined on no equality that pairs its rows, so many of them can match '
                    f'one {names[alias]} row',
                )
        return None

    def fixed_by_group(self, scope: Scope, select: exp.Select, alias: str) -> bool:
        """Whether the GROUP BY holds a key of the source ``alias`` that no row holds NULL in among
        its columns, so that each group reads one of its rows, however often the join repeats it:
        an average of the copies of one value is that value. A unique test lets many rows hold
        NULL, which one group would mix, so only the keys of :meth:`Checker.filled_keys` count, or
        those a GROUP BY or DISTINCT makes, which keep one row for NULL; and a key only an ontology
        pack presumes counts for nothing here."""
        group = select.args.get('group')
        if not isinstance(group, exp.Group) or _subtotals(group):
            return False
        names = {
            item.name.lower()
            for item in group.expressions
            if isinstance(item, exp.Column) and item.table == alias
        }
        source = scope.sources.get(alias)
        relation = self.relation_of(source)
        if relation is not None:
            keys = self.checker.filled_keys(relation)
        else:
            info = self.infos.get(id(source)) if isinstance(source, Scope) else None
            keys = info.grouped if info is not None else []
        return any(key and key <= names for key in keys)

    def measured(self, scope: Scope, column: exp.Column, node: Node | None, alias: str) -> bool:
        """Whether the project adds up the value ``column`` reads once per row of the source
        ``alias`` keeps the grain of, as summing it over this join does: a column it repeats on
        those rows (:meth:`Checker.measured_at`), or, for a CTE's aggregate grouped by its key, a
        column that holds the same aggregate grouped by the same columns
        (:meth:`Checker.summed_at`)."""
        keys = self.key_origins(scope, alias)
        if not keys:
            return False
        if node is not None:
            return self.checker.measured_at(node, keys) is not None
        source = scope.sources.get(column.table)
        if not isinstance(source, Scope) or not isinstance(source.expression, exp.Select):
            return False
        select = source.expression
        value = next(
            (
                _strip(_output(e))
                for e in select.expressions
                if e.alias_or_name.lower() == column.name.lower()
            ),
            None,
        )
        group = select.args.get('group')
        if value is None or group is None or not any(True for _ in _aggregates(select)):
            return False
        grouped = [self.physical(source, c) if isinstance(c, exp.Column) else None for c in group.expressions]
        if not grouped or not all(grouped):
            return False
        tree = self.canon.expression(value, source, self.schema)
        groups = frozenset(self.checker.origin(n.id) for n in grouped if n is not None)
        return self.checker.summed_at(self.checker.form(tree), groups, keys) is not None

    def key_origins(self, scope: Scope, alias: str) -> frozenset[str]:
        """The origins of the unique columns of one source of ``scope``."""
        source = scope.sources.get(alias)
        relation = self.relation_of(source)
        if relation is not None:
            return self.checker.key_origins(relation)
        info = self.infos.get(id(source)) if isinstance(source, Scope) else None
        if info is None:
            return frozenset()
        return frozenset(
            self.checker.origin(node.id)
            for keys in info.unique
            if len(keys) == 1 and (node := info.physical.get(next(iter(keys)))) is not None
        )

    def presumed(self, scope: Scope, alias: str, column: str) -> tuple[Node, str] | None:
        """The column ``alias.column`` with the pack that presumes it unique, when only a pack
        does: no test, primary key or identifier field says so."""
        node = self.physical(scope, exp.column(column, table=alias))
        if node is None:
            return None
        pack = self.checker.derived.presumed.get(node.id)
        if pack is None or node.id in self.checker.derived.unique:
            return None
        return node, pack

    def presumption(self, scope: Scope, alias: str, key: list[str]) -> str:
        """What a key that decides a finding rests on, when a pack presumes it rather than a test
        showing it: `` (id presumed unique by the stripe pack)``."""
        found = [(name, p[1]) for name in key if (p := self.presumed(scope, alias, name)) is not None]
        if not found:
            return ''
        packs = sorted({pack for _, pack in found})
        names = ', '.join(name for name, _ in found)
        return f' ({names} presumed unique by the {", ".join(packs)} pack{"s" if len(packs) > 1 else ""})'

    def presumptions(self, scope: Scope, equalities: list[_Join]) -> None:
        """A note for each join the check took as safe on a column only a pack presumes unique, so
        a passing check says what it assumed."""
        for eq in equalities:
            for alias, column, other, other_column in (
                (eq.left, eq.left_column, eq.right, eq.right_column),
                (eq.right, eq.right_column, eq.left, eq.left_column),
            ):
                found = self.presumed(scope, alias, column)
                theirs = self.physical(scope, exp.column(other_column, table=other))
                if found is None or (theirs is not None and theirs.id in self.checker.unique):
                    continue
                node, pack = found
                label = self.graph.label(node)
                self.add(
                    Finding(
                        'presumed-key',
                        'info',
                        f'the join {eq.node.sql(dialect=self.checker.sqlglot)} is taken not to repeat rows '
                        f'because the {pack} ontology pack presumes {label} unique; nothing has tested it',
                        f'Add a unique test or a primary key on {label}, so the check rests on it.',
                        evidence=[f'{label}: the identifier of the source system the {pack} pack describes'],
                    ),
                    node.id,
                )

    def aggregate(
        self, scope: Scope, select: exp.Select, agg: exp.AggFunc, fans: dict[str, _Fan], preserved: list[str]
    ) -> None:
        if isinstance(agg, _NOT_ADDITIVE) or isinstance(agg.this, exp.Distinct):
            return
        if not isinstance(agg, (exp.Sum, exp.Avg, exp.Count)):
            return
        if isinstance(agg.this, exp.Star) or (
            isinstance(agg, exp.Count) and isinstance(agg.this, exp.Literal)
        ):
            self.count_rows(agg, fans, preserved, self.called)
            return
        for column in _valued(agg, select):
            fan = fans.get(column.table)
            source = scope.sources.get(column.table)
            if fan is None and isinstance(source, Scope) and id(source) in self.infos:
                fan = self.infos[id(source)].fanned.get(column.name.lower())
            if fan is None:
                continue
            node = self.physical(scope, column)
            if any(self.measured(scope, column, node, alias) for alias in preserved):
                continue
            if isinstance(agg, exp.Avg) and self.fixed_by_group(scope, select, column.table):
                continue
            what = self.graph.label(node) if node is not None else f'{column.table}.{column.name}'
            function = agg.key.upper()
            text = agg.sql(dialect=self.checker.sqlglot)
            fix = (
                f'Aggregate {fan.pre_aggregate} in a subquery before the join, or aggregate '
                f'{fan.relation} on its own and join the result.'
                if fan.pre_aggregate
                else f'Aggregate {fan.relation} on its own and join the result.'
            )
            self.add(
                Finding(
                    'fan-out',
                    fan.severity,
                    f'{text} {_VERBS.get(function, "aggregates")} {what} once per matching {fan.by} row',
                    fix,
                    column=_output_name(agg),
                    detail=text,
                    evidence=[fan.evidence],
                ),
                text,
            )
            return

    def count_rows(
        self, agg: exp.AggFunc, fans: dict[str, _Fan], preserved: list[str], called: dict[str, list[str]]
    ) -> None:
        """``COUNT(*)`` over a join counts the rows of the source no other repeats. It is wrong when
        it is named for a source whose rows the join repeats (``orders_placed``, ``order_count``:
        by the relation, its entity, or the term that names the entity, a synonym or a word of the
        name), and not for one whose rows it keeps; or when every source's rows repeat, so that it
        counts pairs of rows and no one thing."""
        name = _output_name(agg)
        words = [w for w in re.split(r'[^a-z0-9]+', (name or '').lower()) if w]

        def named_for(alias: str) -> bool:
            return any(
                name_matches(w, c) for w in [*([name] if name else []), *words] for c in called.get(alias, [])
            )

        if any(named_for(a) for a in preserved):
            return
        named = [a for a in fans if named_for(a)]
        chosen = named or ([] if preserved else sorted(fans))
        if not chosen:
            return
        fan = fans[chosen[0]]
        text = agg.sql(dialect=self.checker.sqlglot)
        self.add(
            Finding(
                'fan-out',
                fan.severity,
                f'{text} counts each {fan.relation} row once per matching {fan.by} row',
                f'Count DISTINCT the key of {fan.relation}, or aggregate {fan.relation} on its own and join '
                'the result.',
                column=name,
                detail=text,
                evidence=[fan.evidence],
            ),
            text,
        )

    def grain(self, info: _ScopeInfo) -> None:
        for name, fan in info.fanned.items():
            column = info.physical.get(name)
            if column is None or not self.checker.measure_column(column):
                continue
            self.add(
                Finding(
                    'mixed-grain',
                    fan.severity,
                    f'{self.graph.label(column)} has one value per {fan.relation} row, but the result has '
                    f'one row per {fan.by} row, so the value repeats and a total of this column counts it '
                    'more than once',
                    f'Aggregate {fan.by} first, or select {self.graph.label(column)} in a query at '
                    f'{fan.relation} grain.',
                    column=name,
                    evidence=[fan.evidence],
                ),
                name,
            )

    def relationships(
        self, scope: Scope, equalities: list[_Join], unique: dict[str, list[frozenset[str]]]
    ) -> None:
        checker, graph = self.checker, self.graph
        for eq in equalities:
            if eq.join is None:
                continue
            a = self.physical(scope, exp.column(eq.left_column, table=eq.left))
            b = self.physical(scope, exp.column(eq.right_column, table=eq.right))
            if a is None or b is None or frozenset({a.id, b.id}) in checker.by_columns:
                continue
            ka, kb = checker.keys.get(a.id), checker.keys.get(b.id)
            if ka is not None and ka == kb:
                continue
            verdict: tuple[str, Relationship | None, Node, Node] | None = None
            for near, far, far_key in ((a, b, kb), (b, a, ka)):
                if far_key is None:
                    continue
                parent = graph.parent_of(near)
                entity = checker.entity_of(parent) if parent is not None else None
                rels = checker.by_entities.get((entity, far_key), []) if entity else []
                if rels:
                    wanted = {graph.nodes[r.columns[0]].name.lower() for r in rels if r.columns}
                    if near.name.lower() in wanted:
                        verdict = ('follows', None, near, far)
                        break
                    verdict = ('wrong', rels[0], near, far)
                    break
                if near.name.lower() == far.name.lower() or entity_name(near.name) == far_key:
                    verdict = ('follows', None, near, far)
                    break
            if verdict is not None and verdict[0] == 'follows':
                continue
            if verdict is not None and verdict[1] is not None:
                rel, near, far = verdict[1], verdict[2], verdict[3]
                near_alias = eq.left if near is a else eq.right
                far_alias = eq.right if near is a else eq.left
                assert rel.columns is not None
                wanted_name = graph.nodes[rel.columns[0]].name
                parent = graph.parent_of(near)
                rewrite = None
                if parent is not None and graph.column(parent.id, wanted_name) is not None:
                    rewrite = self.rejoin(scope, eq, near_alias, wanted_name, far_alias, far.name)
                fix = f'Join on {rewrite}.' if rewrite else f"Join on the relationship's columns: {rel.via}."
                self.add(
                    Finding(
                        'join-off-relationship',
                        'warning',
                        f'the join {eq.node.sql(dialect=checker.sqlglot)} matches {graph.label(near)} to '
                        f'the key of {rel.target}, but {rel.source} refers to {rel.target} through {rel.via}',
                        fix,
                        rewrite=f'join on {rewrite}' if rewrite else None,
                        evidence=[f'relationship {rel.source} -> {rel.target}: {", ".join(rel.evidence)}'],
                    ),
                    eq.node.sql(),
                )
                continue
            mismatch = self.mismatched(a, b)
            if mismatch is not None:
                self.add(mismatch, a.id, b.id)
                continue
            left, right = frozenset({eq.left_column}), frozenset({eq.right_column})
            if left in unique.get(eq.left, []) or right in unique.get(eq.right, []):
                continue  # rows of the other side cannot repeat
            self.add(
                Finding(
                    'unknown-join',
                    'info',
                    f'neither {graph.label(a)} nor {graph.label(b)} is known to be unique and no relationship '
                    'links them, so the join may repeat rows on both sides',
                    'Check the join columns, or add a unique or relationships test that says how they relate.',
                ),
                a.id,
                b.id,
            )

    def mismatched(self, a: Node, b: Node) -> Finding | None:
        """A join of one entity's key to a column named for another entity, which no relationship
        links: ``payments.payment_id = customers.customer_id``."""
        checker, graph = self.checker, self.graph
        entities = {e.name for e in checker.ontology.entities}
        for near, far in ((a, b), (b, a)):
            key = checker.keys.get(far.id)
            named = entity_name(near.name) if id_like(near.name) else None
            if key is None or named is None or named == key or named not in entities:
                continue
            return Finding(
                'unknown-join',
                'warning',
                f'the join matches {graph.label(near)}, which identifies a {named}, to {graph.label(far)}, '
                f'the key of {key}, and no relationship links {named} to {key}',
                f'Join {graph.label(far)} to a column that refers to {key}.',
            )
        return None

    # Rewrites

    def original(self, select: exp.Expr) -> exp.Select | None:
        return self.mapped.get(id(select))

    def rejoin(
        self, scope: Scope, eq: _Join, near_alias: str, near_column: str, far_alias: str, far_column: str
    ) -> str | None:
        """Put a join back on its relationship's columns, in the query as written."""
        original = self.original(scope.expression)
        if original is None or eq.join is None:
            return None
        joins = original.args.get('joins') or []
        if eq.join >= len(joins) or not isinstance(joins[eq.join].args.get('on'), exp.Expr):
            return None
        parts = split_conjuncts(joins[eq.join].args['on'])
        if eq.conjunct >= len(parts) or not isinstance(parts[eq.conjunct], exp.EQ):
            return None
        replacement = exp.EQ(
            this=exp.column(near_column, table=near_alias), expression=exp.column(far_column, table=far_alias)
        )
        parts[eq.conjunct].replace(replacement)
        self.rewritten = True
        return replacement.sql(dialect=self.checker.sqlglot)

    def alone(self, scope: Scope, site: _Site, depth: int = 0) -> bool:
        """Whether filtering the rows of ``scope`` changes the measure ``site`` and nothing else:
        every aggregate those rows reach is the measure's, and none reach the result unaggregated."""
        select = scope.expression
        if depth > 20:
            return False
        if isinstance(select, exp.Select) and (
            select.args.get('group') is not None or any(_aggregates(select))
        ):
            mine = {id(a) for a in site.aggregates}
            return all(id(a) in mine for a in _aggregates(select))
        readers = self.readers.get(id(scope), [])
        return bool(readers) and all(self.alone(r, site, depth + 1) for r in readers)

    def ranks(self, scope: Scope, site: _Site, depth: int = 0, names: set[str] | None = None) -> bool:
        """Whether the measure decides which rows the result keeps or how it orders them: through
        HAVING, QUALIFY, ORDER BY with LIMIT or a window over it, in its scope, or a filter or join
        on it in a scope that reads it. A correction would change those rows too, so none is made."""
        select = scope.expression
        if depth > 20:
            return True
        if names is None:
            # The measure's own output, by the name the query gives it and the one it has here.
            names = {site.output}
            if isinstance(select, exp.Select):
                names |= {
                    e.alias_or_name.lower()
                    for e in select.expressions
                    if any(n is site.core for n in e.walk())
                }
        if isinstance(select, exp.Select):
            mine = {a.sql() for a in site.aggregates}
            windows = [w for w in select.find_all(exp.Window) if _owner(w) is select]
            clauses = [select.args.get('having')]
            if depth:
                clauses += [
                    select.args.get('where'),
                    *(j.args.get('on') for j in select.args.get('joins') or []),
                ]
            read = [
                n for c in clauses if isinstance(c, exp.Expr) for n in c.find_all(exp.AggFunc, exp.Column)
            ]
            if (
                select.args.get('qualify') is not None
                or (select.args.get('order') is not None and select.args.get('limit') is not None)
                or any(a.sql() in mine for w in windows for a in w.find_all(exp.AggFunc))
                or any(c.name.lower() in names for w in windows for c in w.find_all(exp.Column))
                or any(
                    (isinstance(n, exp.AggFunc) and n.sql() in mine)
                    or (isinstance(n, exp.Column) and n.name.lower() in names)
                    for n in read
                )
            ):
                return True
        return any(self.ranks(r, site, depth + 1, names) for r in self.readers.get(id(scope), []))

    def visible(self, scope: Scope, wanted: set[str]) -> dict[str, tuple[str, str]] | None:
        """For each column in ``wanted`` (by the column it copies), the one source of ``scope`` and
        the column of it that holds it, or None if one is not there exactly once."""
        checker, graph = self.checker, self.graph
        found: dict[str, list[tuple[str, str]]] = defaultdict(list)
        original = self.original(scope.expression)
        for alias, (_, source) in scope.selected_sources.items():
            shown = self.written_alias(original, alias)
            relation = self.relation_of(source)
            if relation is not None:
                for column in graph.columns_of(relation.id):
                    if checker.origin(column.id) in wanted:
                        found[checker.origin(column.id)].append((shown, column.name))
            elif isinstance(source, Scope) and id(source) in self.infos:
                for name, node in self.infos[id(source)].physical.items():
                    if checker.origin(node.id) in wanted:
                        found[checker.origin(node.id)].append((shown, name))
        if set(found) != wanted or any(len(v) != 1 for v in found.values()):
            return None
        return {k: v[0] for k, v in found.items()}

    def written_alias(self, original: exp.Select | None, alias: str) -> str:
        """``alias`` as the query writes it: qualify names an unaliased table by its name, and in
        Snowflake upper-cases it."""
        tables = [] if original is None else [*original.find_all(exp.Table)]
        return next((t.alias_or_name for t in tables if t.alias_or_name.lower() == alias.lower()), alias)

    def place(self, condition: _Condition, site: _Site) -> str | None:
        """Add a missing filter in the query as written. Into the WHERE clause (or the ON clause of
        the LEFT JOIN) of the scope that reads the relation it filters when the measure's aggregate
        has no GROUP BY, whose groups the filter would remove, and only the measure reads the rows
        it removes; otherwise into the measure's aggregate, when its scope can read the filtered
        columns; otherwise not at all."""
        checker = self.checker
        wanted = {checker.origin(n.id) for n in condition.columns.values()}
        select = site.scope.expression
        assert isinstance(select, exp.Select)
        if self.ranks(site.scope, site):
            return None
        if select.args.get('group') is None:
            targets = [
                (scope, alias)
                for scope, alias, relation in site.rows.tables
                if wanted <= {checker.origin(c.id) for c in self.graph.columns_of(relation.id)}
            ]
            unique = list({(id(s), a): (s, a) for s, a in targets}.values())
            if len(unique) == 1 and self.alone(unique[0][0], site):
                scope, alias = unique[0]
                original = self.original(scope.expression)
                mapping = self.visible(scope, wanted)
                if original is not None and mapping is not None:
                    added = self._condition_in(condition, mapping)
                    return self._where(original, alias, added)
        mapping = self.visible(site.scope, wanted)
        if mapping is None:
            return None
        return self.wrap(site, self._condition_in(condition, mapping))

    def _condition_in(self, condition: _Condition, mapping: dict[str, tuple[str, str]]) -> exp.Expr:
        checker = self.checker

        def swap(item: exp.Expr) -> exp.Expr:
            if isinstance(item, exp.Column):
                node = condition.columns.get(item.sql(dialect=checker.sqlglot))
                if node is not None:
                    alias, name = mapping[checker.origin(node.id)]
                    return exp.column(name, table=alias)
            return item

        return condition.tree.copy().transform(swap, copy=False)

    def _where(self, original: exp.Select, alias: str, added: exp.Expr) -> str | None:
        text = added.sql(dialect=self.checker.sqlglot)
        joins = original.args.get('joins') or []
        if any(str(j.side).upper() in ('RIGHT', 'FULL') for j in joins):
            return None
        left = [
            j
            for j in joins
            if str(j.side).upper() == 'LEFT' and j.this.alias_or_name.lower() == alias.lower()
        ]
        if left:
            if not isinstance(left[0].args.get('on'), exp.Expr):
                return None
            left[0].set('on', exp.and_(left[0].args['on'], added))
            self.rewritten = True
            return f'{text} added to the ON clause of the join to {left[0].this.alias_or_name}'
        original.where(added, append=True, copy=False)
        self.rewritten = True
        return f'{text} added to the WHERE clause'

    def wrap(self, site: _Site, added: exp.Expr) -> str | None:
        """Write the filter into the measure's one aggregate, so the other columns and the groups
        keep their rows: ``FILTER (WHERE ...)``, written as ``IFF`` or ``IF`` in a dialect without it."""
        select = site.scope.expression
        if len(site.aggregates) != 1 or not isinstance(select, exp.Select):
            return None
        original = self.original(select)
        if original is None:
            return None
        mine = list(_aggregates(select))
        theirs = list(_aggregates(original))
        if len(mine) != len(theirs):
            return None
        agg = theirs[[id(a) for a in mine].index(id(site.aggregates[0]))]
        if type(agg) is not type(site.aggregates[0]):
            return None
        holder = agg.parent
        if isinstance(holder, exp.Filter):
            where = holder.expression
            if not isinstance(where, exp.Where):
                return None
            where.set('this', exp.and_(where.this, added))
        else:
            agg.replace(exp.Filter(this=agg.copy(), expression=exp.Where(this=added)))
        self.rewritten = True
        text = added.sql(dialect=self.checker.sqlglot)
        return f'{text} applied to {agg.sql(dialect=self.checker.sqlglot)} alone'

    def _qualified(self, query: exp.Query) -> exp.Expr:
        """A copy of ``query`` with every column qualified by its source, as the build qualifies."""
        checker = self.checker
        kwargs: dict[str, Any] = {
            'dialect': checker.sqlglot,
            'schema': self.schema,
            'validate_qualify_columns': False,
            'identify': False,
        }
        if checker.default_db:
            kwargs['db'] = checker.default_db
            if checker.default_catalog:
                kwargs['catalog'] = checker.default_catalog
        return qualify(query.copy(), **kwargs)

    def rename(self, query: exp.Query, old: Node, new: Node) -> str | None:
        """Swap a column for another of the same relation in the query as written, keeping the
        output's name, and leaving a clause that refers to that name as it is; not when the column
        passes out of a CTE or subquery, or the replacement is read in the same SELECT."""
        relation = self.graph.parent_of(old)
        if relation is None or relation.relation is None:
            return None
        edits: list[exp.Column] = []
        for select in query.find_all(exp.Select):
            tables = [
                *(j.this for j in select.args.get('joins') or []),
                getattr(select.args.get('from_'), 'this', None),
            ]
            aliases = {t.alias_or_name: t for t in tables if isinstance(t, exp.Table)}
            mine = {
                a
                for a, t in aliases.items()
                if table_key(t, self.checker.default_catalog, self.checker.default_db) == relation.relation
            }
            if not mine:
                continue
            nested = select.find_ancestor(exp.CTE, exp.Subquery) is not None

            single = len(aliases) == 1

            def ours(
                column: exp.Column,
                name: str,
                select: exp.Select = select,
                mine: set[str] = mine,
                single: bool = single,
            ) -> bool:
                return (
                    _owner(column) is select
                    and column.name.lower() == name
                    and (column.table in mine or (not column.table and single))
                )

            outputs = [_output(e) for e in select.expressions]
            if any(ours(c, new.name.lower()) for c in select.find_all(exp.Column)):
                return None  # the replacement is read beside it, so the swap would merge the two
            named = {e.alias_or_name.lower() for e in select.expressions}
            for column in select.find_all(exp.Column):
                if not ours(column, old.name.lower()):
                    continue
                clause = column.find_ancestor(exp.Order, exp.Group, exp.Having, exp.Qualify, exp.Select)
                if not column.table and column.name.lower() in named and not isinstance(clause, exp.Select):
                    continue  # the output's name, which the swap keeps
                if nested and any(column is o for o in outputs):
                    return None  # the column passes out under its name to the query around it
                edits.append(column)
        if not edits:
            return None
        for column in edits:
            renamed = column.copy()
            renamed.set('this', exp.to_identifier(new.name))
            parent = column.parent
            if isinstance(parent, exp.Select) and any(column is e for e in parent.expressions):
                column.replace(exp.alias_(renamed, old.name))
            else:
                column.replace(renamed)
        self.rewritten = True
        return f'{old.name} read from {new.name}'


def _output_name(node: exp.Expr) -> str | None:
    """The output column an expression in a SELECT list belongs to."""
    current: exp.Expr | None = node
    while current is not None and not isinstance(current.parent, exp.Select):
        current = current.parent
    if current is None:
        return None
    return _shown(current.alias_or_name.lower())


def _shown(name: str | None) -> str | None:
    """An output column's name, or None for one the query left unnamed (sqlglot's ``_col_0``)."""
    return None if not name or re.fullmatch(r'_col_\d+', name) else name


def _with_queries(trees: list[exp.Expr]) -> Iterator[exp.Query | None]:
    """The query each statement runs: itself, or the SELECT of a CREATE ... AS or INSERT."""
    for tree in trees:
        if isinstance(tree, exp.Query):
            yield tree
        elif isinstance(tree, (exp.Create, exp.Insert)) and isinstance(tree.expression, exp.Query):
            yield tree.expression
        else:
            yield None


def check_sql(graph: Graph, sql: str) -> SqlCheck:
    """Check ``sql`` against the project ``graph`` describes, without running it."""
    return Checker(graph).check(sql)


# Query history


@dataclass
class ShadowMetric:
    """One way that queries in history compute a measure differently from its definition."""

    term: str
    """The measure, or the output name it was computed under when no term holds it."""
    status: str | None
    """The term's status, or None for an idea with no term."""
    rule: str
    """``missing-filter`` or ``formula-differs``."""
    detail: str
    """What they do differently: the filter left out or added, or the formula used."""
    fix: str
    """What to do instead, from the first query found."""
    queries: int = 0
    """Distinct query texts that compute it this way."""
    runs: int = 0
    """Runs of those queries."""
    users: int = 0
    """Distinct people who ran them."""
    people: list[str] = field(default_factory=list)
    """Who ran them, by name; empty when the check pseudonymises."""
    example: str = ''
    """The text of the query run most often; with its literals left out when the check
    pseudonymises, since they can identify a customer or a person."""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Adoption:
    """How many queries read a term through its own columns."""

    term: str
    """The business term."""
    queries: int = 0
    """Distinct query texts that read it."""
    runs: int = 0
    """Runs of those queries."""
    users: int = 0
    """Distinct people who ran them."""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class HistoryCheck:
    """The checks run over query history: shadow metrics, and how widely each term's own columns are read."""

    queries: int = 0
    """Distinct query texts checked."""
    runs: int = 0
    """Runs of those queries."""
    unparsed: int = 0
    """Query texts that could not be parsed."""
    redacted: int = 0
    """Runs whose text the warehouse withheld, which could not be checked."""
    shadow: list[ShadowMetric] = field(default_factory=list)
    """Measures computed differently from their definitions, approved terms first, then by people."""
    adoption: list[Adoption] = field(default_factory=list)
    """Terms read through their own columns."""
    rules: dict[str, int] = field(default_factory=dict)
    """Queries with a finding of each rule."""
    notes: list[str] = field(default_factory=list)
    """What the warehouse withheld, and what would show it."""

    @property
    def approved_shadow(self) -> list[ShadowMetric]:
        """The shadow metrics of approved terms: queries that compute an approved measure
        differently from its definition."""
        return [s for s in self.shadow if s.status == 'approved']

    def summary(self) -> str:
        approved = len(self.approved_shadow)
        return (
            f'{_plural(self.queries, "query")} checked ({_plural(self.runs, "run")}); '
            f'{_plural(len(self.shadow), "shadow metric")}, {approved} of an approved measure.'
        )

    def text(self, style: Style | None = None) -> str:
        """The result as the CLI prints it: plain text, as ``str()`` gives, or drawn for a
        terminal with ``style``."""
        from konstruct.interfaces.render import history_check_lines, show

        return show(history_check_lines, self, style)

    def __str__(self) -> str:
        return self.text()

    def to_dict(self) -> dict[str, Any]:
        return {
            'summary': self.summary(),
            'queries': self.queries,
            'runs': self.runs,
            'unparsed': self.unparsed,
            'redacted': self.redacted,
            'shadow': [s.to_dict() for s in self.shadow],
            'adoption': [a.to_dict() for a in self.adoption],
            'rules': self.rules,
            'notes': self.notes,
        }


def shadow_text(item: ShadowMetric) -> str:
    """How a shadow metric computes its measure: without a filter, with one, or as a formula."""
    if item.rule == 'missing-filter' or item.detail.startswith('with '):
        return item.detail
    return f'as {item.detail}'


def pseudonym(user: str, key: bytes) -> str:
    """A stand-in for a user name, the same for one ``key`` and unrelated across keys, so a report
    can tell people apart without naming them and two reports cannot be matched."""
    return 'user-' + hmac.new(key, user.encode(), hashlib.sha256).hexdigest()[:16]


def _without_literals(text: str, dialect: Dialect) -> str:
    """A query with every literal replaced by ``?``, or nothing if it cannot be read."""
    try:
        tree = sqlglot.parse_one(text, read=dialect)
    except PARSE_ERRORS:
        return ''
    blank = tree.transform(lambda n: exp.Placeholder() if isinstance(n, exp.Literal) else n)
    return blank.sql(dialect=dialect)


def check_history(
    graph: Graph,
    history: QueryHistory,
    *,
    pseudonymise: bool = False,
    key: bytes | None = None,
    limit: int | None = None,
) -> HistoryCheck:
    """Run the checks over query history and gather the queries that compute a measure differently
    from its definition (shadow metrics), with counts of queries, runs and people.

    Args:
        graph: The project's graph.
        history: Distinct query texts with their user and how often they ran
            (:func:`konstruct.inputs.usage.project_history`).
        pseudonymise: Count people without naming them, and leave the literals out of examples.
        key: The key pseudonyms are made with; by default a new random key for each check, so
            pseudonyms cannot be matched across reports.
        limit: The most distinct query texts to check, those run most often first.
    """
    checker = Checker(graph)
    secret = key if key is not None else secrets.token_bytes(32)
    result = HistoryCheck(redacted=history.redacted, notes=list(history.notes))
    grouped: dict[str, list[HistoryQuery]] = defaultdict(list)
    for item in history.queries:
        if item.text and item.text.strip():
            grouped[item.text].append(item)
    texts = sorted(grouped, key=lambda t: (-sum(q.runs for q in grouped[t]), t))
    if limit is not None:
        texts = texts[:limit]
    shadow: dict[tuple[str, str, str], ShadowMetric] = {}
    users: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    best: dict[tuple[str, str, str], int] = {}
    adoption: dict[str, Adoption] = {}
    adopters: dict[str, set[str]] = defaultdict(set)
    rules: dict[str, int] = defaultdict(int)
    for text in texts:
        runs = sum(q.runs for q in grouped[text])
        people = {pseudonym(q.user, secret) if pseudonymise else q.user for q in grouped[text] if q.user}
        result.queries += 1
        result.runs += runs
        try:
            checked = checker.check(text)
        except _QUALIFY_ERRORS:  # SQL that sqlglot reads but cannot analyse counts as unparsed
            result.unparsed += 1
            continue
        if any(f.rule == 'unparsed' for f in checked.findings):
            result.unparsed += 1
            continue
        for rule in {f.rule for f in checked.findings}:
            rules[rule] += 1
        broken: set[str] = set()
        for finding in checked.findings:
            if finding.rule not in SHADOW_RULES or finding.detail is None:
                continue
            idea = finding.term or finding.column or ''
            entry_key = (idea, finding.rule, finding.detail)
            if finding.term:
                broken.add(finding.term)
            entry = shadow.get(entry_key)
            if entry is None:
                held = next((t for t in checker.terms if t.name == finding.term), None)
                entry = shadow[entry_key] = ShadowMetric(
                    idea, held.status if held else None, finding.rule, finding.detail, finding.fix
                )
            entry.queries += 1
            entry.runs += runs
            users[entry_key] |= people
            if runs > best.get(entry_key, -1):
                best[entry_key] = runs
                entry.example = _one_line(_without_literals(text, checker.sqlglot) if pseudonymise else text)
        for name in checked.terms:
            if name in broken:
                continue
            read = adoption.setdefault(name, Adoption(name))
            read.queries += 1
            read.runs += runs
            adopters[name] |= people
    order = {'approved': 0, 'agreed': 1, 'draft': 2}
    for entry_key, entry in shadow.items():
        entry.users = len(users[entry_key])
        entry.people = [] if pseudonymise else sorted(users[entry_key])
    result.shadow = sorted(
        shadow.values(),
        key=lambda s: (order.get(s.status or '', 3), -s.users, -s.queries, s.term, s.detail),
    )
    for name, read in adoption.items():
        read.users = len(adopters[name])
    result.adoption = sorted(adoption.values(), key=lambda a: (-a.queries, a.term))
    result.rules = dict(sorted(rules.items()))
    return result


def _one_line(text: str, width: int = 160) -> str:
    flat = ' '.join(text.split())
    return flat if len(flat) <= width else flat[: width - 3] + '...'
