"""Conflicts: the same business idea computed in more than one way.

``konstruct conflicts`` finds columns that look like the same measure (by name, by synonyms from
definitions.yml, or by being bound to the same business term) and compares how each is computed:

* the formula in terms of source columns (from :mod:`konstruct.analysis.explain`), compared by
  what it computes rather than how it is written (:mod:`konstruct.analysis.identity`), with
  NULLs; formulas that are the same only when no source value is NULL are reported as such;
* the source columns it reads;
* the filters and joins on its route back to source, with the file each lives in;
* the aggregation at the top of the formula.

A column derived from another is a copy, not a competing definition, so such pairs are left out,
and so are pairs that agree on everything but grain (per order against per customer).

A shared name alone links many columns that are not one measure: ``status`` in two tables, a
closing price of two markets, odds of two sports. So each pair is of one kind. A ``measure``
conflict is two amounts whose values add up a source column in common (one made of no column the
other is not), or that a business term binds together, and that differ by a filter, a value
column or a formula. A ``segment`` is one measure over two segments, each side keeping values of
a column the other leaves out (``license_class LIKE '%Black%'`` against ``'%Green%'``). A
``grain`` pair is one measure at two grains, and a ``name`` pair shares only a name.

Real conflicts are rare among the pairs a name links, and most pairs that look alike differ on
purpose, by grain or segment. So a measure conflict is ``confident`` only when it is likely two
definitions of one number (likely, not certain: each is to be checked): the same name, values made of the same
columns, and a predicate only one side has that keeps some of the rows it adds up (a condition in
its formula, or a filter in front of an aggregate, on the column's own route, that is not on one
of its own grouping columns), or a value column one side adds at the same grain or a roll-up of
it. The text lists the confident conflicts by default, most likely first (:attr:`Conflict.rank`),
and ``--all`` lists every pair; ``to_dict`` lists every pair, most consequential downstream
first (exposures, tasks, columns and approved terms), each with its ``kind``, ``confident`` and
``rank``. Which pairs are listed, and what each lists as differing, is as it always was: the
filters, joins and formulas as written. What lineage makes of them (one filter on two copies of a
column, a filter off a column's own route) decides only a pair's kind and its explanation.
"""

from __future__ import annotations

import contextlib
import re
from bisect import bisect_right
from collections import Counter, defaultdict
from collections.abc import Collection, Iterator
from dataclasses import dataclass, field, replace
from functools import lru_cache
from itertools import combinations
from typing import TYPE_CHECKING, Any

import sqlglot
from sqlglot import exp

from konstruct import _core
from konstruct.analysis.explain import Explainer, joined_reading, short_sql
from konstruct.analysis.identity import Formulas, Verdict, Witness
from konstruct.analysis.measures import _MOMENT, amount, uncommented
from konstruct.core.graph import RELATION_KINDS, Graph, Node
from konstruct.core.sql import PARSE_ERRORS, condition_key, normal_condition, split_conjuncts

if TYPE_CHECKING:
    from konstruct.interfaces.layout import Style

# Words that qualify a measure without changing which measure it is.
MODIFIERS = frozenset(
    {
        'total',
        'sum',
        'overall',
        'final',
        'all',
        'the',
        'of',
        'calc',
        'calculated',
        'adjusted',
        'agg',
        'metric',
        'kpi',
    }
)
# Words that name a measure only when nothing else does: sales_amount is sales, but amount is
# an amount.
GENERIC = frozenset({'amount'})
# As :func:`core` folds them, since it turns ``id`` into its concept.
KEY_WORDS = frozenset(
    {w for k in ('id', 'key', 'uuid', 'pk', 'fk', 'sk', 'hash') for w in (k, _core.concept(k) or k)}
)
FILTER_HEADS = ('WHERE', 'HAVING', 'QUALIFY')
SAME_UNLESS_NULL = 'the same unless a source value is NULL or there are no rows'
MEASURE = 'measure'
"""The kind of a pair of amounts that add up a source column in common, or that a term binds
together, and differ in what they count: a filter, a source column or a formula."""
SEGMENT = 'segment'
"""The kind of a pair of one measure over two segments: each side keeps values of one column
that the other does not (``license_class LIKE '%Black%'`` against ``'%Green%'``)."""
GRAIN = 'grain'
"""The kind of a pair of the same measure at different grains (per month against per seller),
or differing only in joins, which a grain or an enrichment brings."""
NAME = 'name'
"""The kind of a pair whose sides share only a name, or are not both amounts."""
KINDS = (MEASURE, SEGMENT, GRAIN, NAME)
LARGE = 50_000
"""The longest condition, in characters, whose columns are each read as the source column they
copy; a longer one is compared by its text alone."""
# A deduplication: QUALIFY ROW_NUMBER() OVER (PARTITION BY ... ORDER BY ...) = 1.
_LATEST = re.compile(
    r'^ROW_NUMBER\(\) OVER \(PARTITION BY (?P<by>.+?) ORDER BY (?P<order>.+?)\) = 1$', re.IGNORECASE
)

# A name Konstruct gives one reading of a relation (``"orders#1/2"``), as a quoted identifier.
_READING = re.compile(r'"([A-Za-z_][\w.$]*)#[^"]*"')
# The longest piece of SQL an explanation quotes, and the most items it lists of one kind.
SHOWN = 160
LISTED = 3
# What a reading name adds to the name of the relation it reads.
_READING_SUFFIX = re.compile(r'#.*$')
# A predicate that keeps rows by one column's values: ``in``, ``not`` (in), ``like`` or ``unlike``.
Kept = tuple[str, frozenset[str]]

# (relation, condition, file), as a side lists its filters and joins.
Condition = tuple[str, str, str | None]
# A side's conditions of one kind, each as (condition, file, key), and the set of their keys.
Keyed = tuple[list[tuple[str, str | None, str]], frozenset[str]]


@dataclass(frozen=True)
class Predicate:
    """One conjunct of what a side counts: of a filter on its route, or of a condition in its
    formula that keeps a value only for some rows (``SUM(CASE WHEN status = 'paid' THEN amount
    END)`` counts only ``status = 'paid'``)."""

    key: str
    """The conjunct in the form that compares equal to any that keeps the same rows, with each
    column read as the source column it copies."""
    shown: tuple[str, int] = field(compare=False)
    """The condition it is a conjunct of, as written with its clause, and its place in it."""
    columns: frozenset[str]
    """The source columns it reads."""
    tables: frozenset[str]
    """The source relations it reads."""
    aggregate: bool
    """Whether it tests an aggregate (``HAVING COUNT(*) >= 5``)."""
    kept: Kept | None
    """The values of its one column it keeps, when it is a test of one column against constants."""
    formula: bool = False
    """Whether it is a condition in the formula rather than a filter on the route."""

    @property
    def sql(self) -> str:
        """The conjunct as written, with its clause, in readable names."""
        text, index = self.shown
        clause, _, body = text.partition(': ')
        parts = _conjuncts_of(body)
        return f'{clause}: {parts[index]}' if index < len(parts) else text


@dataclass(frozen=True)
class _Analysed:
    """A recorded condition, read once: its key, the source relations it reads, those a
    deduplication orders by, and its conjuncts."""

    key: str
    tables: frozenset[str]
    ordered: frozenset[str]
    conjuncts: tuple[Predicate, ...]
    equal: tuple[tuple[str, str], ...] = ()
    """For a join, the pairs of source columns (by name) an equality in it makes equal."""


@dataclass(frozen=True)
class _Profile:
    """A composed formula, read once: whether it aggregates, the source columns its value is made
    of (not those a condition tests), the names it reads only inside ``COUNT(DISTINCT ...)``, and
    the conditions that keep its value only for some rows."""

    aggregate: bool
    values: frozenset[str]
    counted: frozenset[str]
    conditions: tuple[Predicate, ...]
    unioned: frozenset[str] = frozenset()
    """The value's columns read only in a branch of a UNION: rows of another population."""
    chosen: frozenset[str] = frozenset()
    """The value's columns read only as one choice among others (``COALESCE(list, net)``)."""
    windowed: bool = False
    """Whether the value is an aggregate over a window (a running or partitioned total)."""


@dataclass(frozen=True)
class _Assessed:
    """What tells two sides apart in what they count."""

    only: dict[str, tuple[Predicate, ...]]
    """For ``a`` and ``b``, the predicates only that side has that can change what it counts."""
    segments: dict[str, tuple[Predicate, ...]]
    """For ``a`` and ``b``, the predicates that keep values of a column the other side's leave out."""
    segment: bool
    """Whether the two keep values of one column that the other does not."""
    unsure: bool
    """Whether the two restrict one column in ways not known to overlap."""
    values: frozenset[str]
    """The source columns, not keys, that the value of only one side is made of."""
    shared: bool
    """Whether the two values are made of a source column in common, and one of no column the
    other's is not."""


def core(name: str) -> frozenset[str]:
    """The idea a column name stands for: its words, as search by meaning reads them
    (:func:`konstruct._core.terms`), without qualifiers, each folded to its concept.
    ``total_revenue``, ``revenue`` and ``sales_amount`` all give {revenue}."""
    words = [w for w in _core.terms(name) if w not in MODIFIERS]
    named = [w for w in words if w not in GENERIC] or words
    return frozenset(_core.concept(w) or w for w in named)


@dataclass
class Difference:
    kind: str
    """``formula``, ``source``, ``filter``, ``join`` or ``aggregation``."""
    detail: str
    """What differs, such as a condition, a source column or two formulas."""
    side: str | None = None
    """Which column has it (``a`` or ``b``) when only one does; None when both differ."""
    path: str | None = None
    """The file the filter or join is in."""
    witness: Witness | None = None
    """For a formula or aggregation not shown to be the same, values at which the two give
    different results (a proof only when it sets source columns alone; see
    :class:`~konstruct.analysis.identity.Witness`), or the function they apply differently."""
    counted: bool = field(default=False, repr=False)
    """For a source column, whether the side reads it only to count distinct values of it, as a
    count of orders reads the order key: what the count is by, not an amount."""
    bears: bool = field(default=True, repr=False)
    """For a filter, whether it can change what its side counts: it is on the side's route, and
    read through lineage it is no filter the other side has (``fact.status = 'x'`` against
    ``dim.status = 'x'``, or the same condition with a comment)."""

    def to_dict(self) -> dict[str, Any]:
        data = {k: v for k, v in self.__dict__.items() if v is not None and k not in ('counted', 'bears')}
        if self.witness is not None:
            data['witness'] = self.witness.to_dict()
        return data


@dataclass
class Side:
    column: str
    """The column id."""
    label: str
    """Its short name."""
    relation_kind: str
    """The kind of its relation."""
    path: str | None
    """The file that builds it."""
    expression: str | None
    """Its own expression."""
    composed: str | None
    """Its formula in terms of source columns."""
    sources: list[str] = field(default_factory=list)
    """The source columns it reads."""
    filters: list[tuple[str, str, str | None]] = field(default_factory=list)
    """(relation, condition, file) for each filter on the route back to source."""
    joins: list[tuple[str, str, str | None]] = field(default_factory=list)
    """(relation, condition, file) for each join on its route back to source."""
    reach: dict[str, int] = field(default_factory=dict)
    """Counts of what depends on it downstream (exposures, tasks, columns and approved terms)."""
    terms: list[str] = field(default_factory=list)
    """Business terms bound to it or to anything downstream."""
    carried_into: list[str] = field(default_factory=list)
    """Columns of the same idea that are derived from this one, and so inherit its definition."""
    grain: list[str] | None = None
    """What its relation groups by (``order_month``), or None when it groups by nothing."""
    grain_sources: list[str] | None = field(default=None, repr=False)
    """The source columns its grain comes from, which decide whether two grains are one."""
    grain_roots: frozenset[str] = field(default=frozenset(), repr=False)
    """The source columns its grouping columns copy, which a filter that only drops a group reads."""
    route: frozenset[str] = field(default=frozenset(), repr=False)
    """The relations on its route back to source, and those inner joins bring onto it."""
    counted: list[tuple[str, str, str | None]] = field(default_factory=list, repr=False)
    """The :attr:`filters` that can change what it counts: those on its own route, every filter of
    its query when its value reads no source column (``COUNT(*)``), and no deduplication of a
    relation it does not read. :attr:`filters` lists every filter of the relations it reads, as
    it always has, so the pairs listed do not change; this decides the kind of a pair and what it
    says each side counts."""

    @property
    def weight(self) -> int:
        r = self.reach
        return (
            10 * r.get('exposures', 0) + 5 * r.get('terms', 0) + 3 * r.get('tasks', 0) + r.get('columns', 0)
        )

    def to_dict(self) -> dict[str, Any]:
        data = dict(self.__dict__)
        for private in ('grain_sources', 'grain_roots', 'route', 'counted'):
            data.pop(private)
        data['filters'] = [{'relation': r, 'condition': c, 'path': p} for r, c, p in self.filters]
        data['joins'] = [{'relation': r, 'condition': c, 'path': p} for r, c, p in self.joins]
        data['weight'] = self.weight
        return data


@dataclass
class Conflict:
    idea: str
    """What the columns appear to measure, as a term name or the shared words of their names."""
    reason: str
    """Why the two columns were compared."""
    a: Side
    """One side."""
    b: Side
    """The other side."""
    differences: list[Difference] = field(default_factory=list)
    """What differs between them."""
    kind: str = MEASURE
    """``measure`` when the two amounts read a source column in common or a business term binds
    them, and differ by a filter, a source column or a formula; ``grain`` when they are one
    measure at different grains, or differ only in joins; ``name`` when they share only a name or
    are not both amounts."""
    confident: bool = False
    """Whether the pair is likely two definitions of one number: a ``measure`` whose two
    values are made of a source column in common, told apart by a filter or a condition only one
    side has, or by a value column one side adds at the same grain. Only these are listed by
    default; likely, not certain, so each is to be checked. The findings a build ends with name
    a pair the project's own terms bind to one term whatever this is, and a pair of which only one
    column is bound, the other named like the term, only when this holds."""
    rank: int = 0
    """The pair's place, from 1, when every pair is ordered by how likely it is two definitions
    (confident pairs first, then by :attr:`likelihood`, then by downstream weight)."""
    title: str = field(default='', repr=False)
    """What the columns appear to measure, in the words of their names, as the text heads it."""
    assessed: _Assessed | None = field(default=None, repr=False)
    """What tells the two apart in what they count, for a pair of amounts."""
    _explained: str | None = field(default=None, repr=False)
    """:meth:`explanation`, once it is made."""
    _likelihood: int | None = field(default=None, repr=False)
    """:attr:`likelihood`, once it is worked out."""

    @property
    def score(self) -> int:
        return self.a.weight + self.b.weight

    @property
    def grains_differ(self) -> bool:
        """Whether one side groups by something the other does not."""
        return _grain_key(self.a) != _grain_key(self.b)

    @property
    def likelihood(self) -> int:
        """How likely the pair is two definitions, from 5 down to 0.

        A confident pair gives 5. A filter or a source column at one grain both group by gives 4,
        and so does a formula at one grain; a filter or a source column at grains not known to be
        one gives 3, the same with names that differ 2, a formula at different grains 1, and
        anything else, or a pair that shares only a name, 0."""
        if self._likelihood is None:
            self._likelihood = self._likely()
        return self._likelihood

    def _likely(self) -> int:
        if self.confident:
            return 5
        if self.kind == NAME:
            return 0
        kinds = {d.kind for d in self.differences if _definitional(d)}
        same = not self.grains_differ
        if kinds & {'filter', 'source'}:
            named = self.a.column.rsplit('#', 1)[-1] == self.b.column.rsplit('#', 1)[-1]
            return 4 if same and self.a.grain is not None else 3 if named else 2
        if 'formula' in kinds:
            return 4 if same else 1
        return 0

    def explanation(self) -> str:
        """The difference that matters most, in one plain sentence: the rows each side counts that
        the other does not, the value column one side adds, the grains, or where the two formulas
        differ."""
        if self._explained is None:
            self._explained = self._explanation()
        return self._explained

    def _explanation(self) -> str:
        label = {'a': self.a.label, 'b': self.b.label}
        if self.kind == GRAIN and self.grains_differ:
            return (
                f'the same measure at a different grain ({_per(self.a.grain)} against {_per(self.b.grain)})'
            )
        assessed = self.assessed
        if assessed is not None and self.kind == MEASURE and assessed.values and assessed.shared:
            for side, mine in (('a', self.a), ('b', self.b)):
                named = {source.lower(): source for source in mine.sources}
                added = sorted(named.get(v, v) for v in assessed.values if v in named)
                if added:
                    return f'{label[side]} also adds {_few(added)}'
        if assessed is not None and self.kind in (MEASURE, SEGMENT):
            chosen = assessed.segments if self.kind == SEGMENT else assessed.only
            counts = [f'{label[side]} counts only {_rules(chosen[side])}' for side in 'ab' if chosen[side]]
            if counts and self.kind == SEGMENT:
                return 'the same measure over another segment: ' + '; '.join(counts)
            if counts:
                return '; '.join(counts)
        sources = [d for d in self.differences if d.kind == 'source' and _definitional(d)]
        sides = {d.side for d in sources}
        if len(sides) == 1:
            [reader] = sides
            read = [d.detail for d in sources]
            return f'{label[str(reader)]} also reads {_few(read)}'
        if not sources:
            found = next(
                (d for d in self.differences if d.kind == 'filter' and d.side and _definitional(d)), None
            )
            if found is None and not any(d.kind == 'formula' and _definitional(d) for d in self.differences):
                found = next((d for d in self.differences if d.kind == 'join' and d.side), None)
            if found is not None:
                return _keeps(label[str(found.side)], found)
        first, second = _differing(self.a.composed or self.a.expression, self.b.composed or self.b.expression)
        if first == second:
            return 'the two count the same rows the same way; only how they are written differs'
        return f'{self.a.label} computes {first}, where {self.b.label} computes {second}'

    def to_dict(self) -> dict[str, Any]:
        return {
            'idea': self.idea,
            'reason': self.reason,
            'kind': self.kind,
            'confident': self.confident,
            'rank': self.rank,
            'explanation': self.explanation(),
            'likelihood': self.likelihood,
            'score': self.score,
            'a': self.a.to_dict(),
            'b': self.b.to_dict(),
            'differences': [d.to_dict() for d in self.differences],
        }

    def lines(self) -> list[str]:
        out = [f'{self.a.label}  vs  {self.b.label}  ({self.reason}; downstream weight {self.score})']
        for side, name in ((self.a, 'a'), (self.b, 'b')):
            formula = side.composed or side.expression or 'no recorded formula'
            out.append(f'  {name}: {side.label} = {formula}')
            where = side.path or '?'
            out.append(f'     in {where}; feeds {_reach_text(side.reach)}')
            if side.carried_into:
                out.append(f'     carried into {", ".join(side.carried_into)}')
        for difference in self.differences:
            who = (
                f' only in {self.a.label if difference.side == "a" else self.b.label}'
                if difference.side
                else ''
            )
            where = f'  ({difference.path})' if difference.path else ''
            out.append(f'  - {difference.kind}{who}: {difference.detail}{where}')
            if difference.witness is not None:
                out.append(f'      {difference.witness}')
        return out


def _said(label: str) -> frozenset[str]:
    """The words of a column's name as written, without those that only qualify a measure."""
    words = {w.lower() for w in re.split(r'[^0-9A-Za-z]+|(?<=[a-z])(?=[A-Z])', label.rsplit('.', 1)[-1]) if w}
    return frozenset(words - MODIFIERS - GENERIC)


def _title(members: list[Node]) -> str:
    """What a group of columns appears to measure, in the words of the shortest of their names
    as it spells them (``late payment rate``, ``kgco2e``), without the words that only qualify a
    measure."""
    first = min(members, key=lambda m: (len(m.name), m.name))
    words = [w for w in re.split(r'[^0-9A-Za-z]+|(?<=[a-z])(?=[A-Z])', first.name) if w]
    kept = [w.lower() for w in words if w.lower() not in MODIFIERS]
    return ' '.join(kept or [w.lower() for w in words]) or first.name


@lru_cache(maxsize=65536)
def _key_like(column: str) -> bool:
    """:func:`konstruct.semantics.ontology.id_like`, read once for each column name."""
    from konstruct.semantics.ontology import id_like

    return id_like(column)


def _definitional(difference: Difference) -> bool:
    """Whether a difference can make two columns count something else: not a source column that
    is a key (what a count or a grain is by), and not a formula that is the same unless a value
    is NULL."""
    if difference.kind == 'source':
        return not difference.counted and not _key_like(difference.detail)
    if difference.kind == 'formula':
        return not difference.detail.endswith(SAME_UNLESS_NULL)
    if difference.kind == 'filter':
        return difference.bears
    return True


def _plain(formula: str | None, dialect: str | None) -> str | None:
    return None if formula is None else uncommented(formula, dialect)


def _grain_key(side: Side) -> frozenset[str] | None:
    return None if side.grain_sources is None else frozenset(side.grain_sources)


def _per(grain: list[str] | None) -> str:
    return f'per {_and(grain)}' if grain else 'over every row'


@lru_cache(maxsize=4096)
def _conjuncts_of(sql: str) -> tuple[str, ...]:
    """The conjuncts of a condition as written, each in readable names."""
    try:
        tree = sqlglot.parse_one(sql)
    except PARSE_ERRORS:
        return (sql,)
    return tuple(_readable(_written(p)) for p in split_conjuncts(tree))


def _written(node: exp.Expr) -> str:
    """``node`` as SQL, each negated test as people write it (``status NOT IN ('a', 'b')``,
    ``x NOT BETWEEN 1 AND 2``, ``x IS NOT NULL``) rather than as a NOT in front of the test, the
    form sqlglot gives them (``NOT status IN ('a', 'b')``)."""

    def negated(found: exp.Expr) -> exp.Expr:
        if not isinstance(found, exp.Not):
            return found
        inner = found.this.unnest() if isinstance(found.this, exp.Paren) else found.this
        if isinstance(inner, exp.In) and inner.expressions and not inner.args.get('query'):
            listed = ', '.join(_written(e) for e in inner.expressions)
            return exp.Var(this=f'{_written(inner.this)} NOT IN ({listed})')
        if isinstance(inner, exp.Between):
            low, high = _written(inner.args['low']), _written(inner.args['high'])
            return exp.Var(this=f'{_written(inner.this)} NOT BETWEEN {low} AND {high}')
        if isinstance(inner, exp.Is) and isinstance(inner.expression, exp.Null):
            return exp.Var(this=f'{_written(inner.this)} IS NOT NULL')
        return found

    return node.copy().transform(negated).sql(comments=False)


def _readable(sql: str) -> str:
    """``sql`` as an explanation quotes it: each reading Konstruct names (``"orders#1/2"``) as the
    relation it reads, one of several readings of a relation as ``"orders#2"`` and so on in the
    order they appear, so that two readings never read as one column; on one line; and cut at
    :data:`SHOWN` characters, so that a generated condition does not run to kilobytes."""
    names: dict[str, str] = {}
    readings: dict[str, int] = {}

    def name(match: re.Match[str]) -> str:
        if match[0] not in names:
            readings[match[1]] = count = readings.get(match[1], 0) + 1
            names[match[0]] = match[1] if count == 1 else f'"{match[1]}#{count}"'
        return names[match[0]]

    text = ' '.join(_READING.sub(name, sql).split())
    return text if len(text) <= SHOWN else text[: SHOWN - 4].rstrip() + ' ...'


def _differing(first: str | None, second: str | None) -> tuple[str, str]:
    """The parts of two formulas that differ, the smallest that holds every difference, each in
    readable names: ``status <> 'canceled'`` against ``status = 'active'`` rather than the whole
    of two long CASE expressions."""
    if not first or not second:
        return first or 'no recorded formula', second or 'no recorded formula'
    try:
        x, y = sqlglot.parse_one(first), sqlglot.parse_one(second)
    except PARSE_ERRORS:
        return _readable(first), _readable(second)
    while True:
        inner = _one_difference(x, y)
        if inner is None:
            break
        x, y = inner
    return _readable(_written(x)), _readable(_written(y))


def _one_difference(x: exp.Expr, y: exp.Expr) -> tuple[exp.Expr, exp.Expr] | None:
    """The one child of ``x`` that differs from ``y``'s, when the two are alike in all else."""
    if type(x) is not type(y) or set(x.args) != set(y.args):
        return None
    found = None
    for name, value in x.args.items():
        other = y.args.get(name)
        if isinstance(value, list) and isinstance(other, list):
            if len(value) != len(other):
                return None
            pairs = list(zip(value, other, strict=True))
        else:
            pairs = [(value, other)]
        for left, right in pairs:
            if left == right:
                continue
            if not (isinstance(left, exp.Expr) and isinstance(right, exp.Expr)) or found is not None:
                return None
            found = (left, right)
    return found


def _rules(predicates: Collection[Predicate]) -> str:
    """What a side's predicates keep, as a phrase: the rows where each holds."""
    rules = []
    rows = []
    for predicate in predicates:
        clause, condition = predicate.sql.split(': ', 1) if ': ' in predicate.sql else ('', predicate.sql)
        latest = _LATEST.match(condition) if clause.upper().startswith('QUALIFY') else None
        if latest is not None:
            order = latest['order'].strip()
            which = 'earliest' if not re.search(r'\bDESC$', order, re.IGNORECASE) else 'latest'
            order = re.sub(r'\s+(ASC|DESC)$', '', order, flags=re.IGNORECASE)
            rules.append(f'the {which} row per {latest["by"]} (by {order})')
        elif predicate.aggregate:
            rules.append(f'the groups where {condition}')
        else:
            rows.append(condition)
    return _few(([f'the rows where {_few(rows)}'] if rows else []) + rules)


def _nothing(node: exp.Expr | None) -> bool:
    """Whether a CASE result adds nothing: absent, NULL or zero."""
    while isinstance(node, (exp.Cast, exp.Paren)):
        node = node.this
    if node is None or isinstance(node, exp.Null):
        return True
    return isinstance(node, exp.Literal) and not node.is_string and node.name.strip('0.') == ''


def _keeping(tree: exp.Expr) -> list[exp.Expr]:
    """The conditions in a formula that keep its value only for the rows where they hold: of
    ``FILTER (WHERE ...)``, of a ``CASE`` of one ``WHEN`` that adds nothing otherwise, and of
    such an ``IF``."""
    found = []
    for node in tree.walk():
        if isinstance(node, exp.Filter) and isinstance(node.expression, exp.Where):
            found.append(node.expression.this)
        elif isinstance(node, exp.Case) and len(node.args.get('ifs') or []) == 1:
            found += [node.args['ifs'][0].this] if _nothing(node.args.get('default')) else []
        elif isinstance(node, exp.If) and not isinstance(node.parent, exp.Case):
            found += [node.this] if _nothing(node.args.get('false')) else []
    return found


def _in_condition(node: exp.Expr) -> bool:
    """Whether ``node`` is inside a condition of the formula, a test rather than its value."""
    while node.parent is not None:
        parent = node.parent
        if (isinstance(parent, exp.If) and node.arg_key == 'this') or isinstance(parent, exp.Where):
            return True
        node = parent
    return False


def _in_union(node: exp.Expr) -> bool:
    """Whether ``node`` is in a branch of a UNION, as a composed formula writes one
    (``UNION(a.x, b.x)``)."""
    found = node.find_ancestor(exp.Anonymous)
    while found is not None:
        if found.name.upper() == 'UNION':
            return True
        found = found.find_ancestor(exp.Anonymous)
    return False


def _rolls_up(a: Side, b: Side, aggregates: tuple[bool, bool]) -> bool:
    """Whether one side's grain is the other's or a roll-up of it: per order against per order
    and customer, or per order against the rows of an order line that no GROUP BY groups."""
    first, second = _grain_key(a), _grain_key(b)
    if first == second:
        return True
    if (first is None and not aggregates[0]) or (second is None and not aggregates[1]):
        # One side groups nothing and aggregates nothing: it is a value per row, and any grain
        # rolls rows up.
        return True
    if first is None or second is None:
        # A total over every row is a roll-up of any grain.
        return True
    return (
        first <= second or second <= first or a.grain_roots <= b.grain_roots or b.grain_roots <= a.grain_roots
    )


def _kept(node: exp.Expr) -> Kept | None:
    """The values of one column a predicate keeps, when it tests the column against constants
    (``status = 'paid'``, ``NOT kind IN (1, 2)``, ``name LIKE '%Black%'``)."""
    negated = False
    while isinstance(node, (exp.Not, exp.Paren)):
        negated ^= isinstance(node, exp.Not)
        node = node.this
    if isinstance(node, (exp.EQ, exp.NEQ, exp.Like, exp.ILike)):
        column, value = _unwrapped(node.this), node.expression
        if not isinstance(column, exp.Column):
            column, value = _unwrapped(node.expression), node.this
        values = [value]
        mode = {exp.EQ: 'in', exp.NEQ: 'not', exp.Like: 'like', exp.ILike: 'like'}[type(node)]
    elif isinstance(node, exp.In) and not node.args.get('query'):
        column, values, mode = _unwrapped(node.this), list(node.expressions), 'in'
    else:
        return None
    if not isinstance(column, exp.Column) or not all(isinstance(v, exp.Literal) for v in values):
        return None
    if negated:
        mode = {'in': 'not', 'not': 'in', 'like': 'unlike', 'unlike': 'like'}[mode]
    return mode, frozenset(v.name.lower() for v in values)


def _unwrapped(node: exp.Expr) -> exp.Expr:
    """A column inside the casts and the functions of one argument that keep which rows match
    (``LOWER(TRIM(status))``)."""
    while isinstance(node, (exp.Cast, exp.Lower, exp.Upper, exp.Trim, exp.Paren)) and node.this is not None:
        node = node.this
    return node


def _overlap(first: Kept | None, second: Kept | None) -> bool | None:
    """Whether two predicates on one column keep some value in common: False for two segments
    (``= 'A'`` against ``= 'B'``), True for two definitions of one population (``<> 'canceled'``
    against ``= 'active'``), None when it cannot be told."""
    if first is None or second is None:
        return None
    modes = {first[0], second[0]}
    if modes == {'in'}:
        return bool(first[1] & second[1])
    if modes == {'in', 'not'}:
        kept, dropped = (first[1], second[1]) if first[0] == 'in' else (second[1], first[1])
        return bool(kept - dropped)
    if modes == {'not'}:
        return True
    if modes == {'like'}:
        return first[1] == second[1]
    return None


def _few(items: list[str]) -> str:
    """The items as a phrase, at most :data:`LISTED` of them and how many more."""
    return _and(items if len(items) <= LISTED else [*items[:LISTED], f'{len(items) - LISTED} more'])


def _and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else f'{", ".join(items[:-1])} and {items[-1]}'


def _keeps(side: str, difference: Difference) -> str:
    """What a filter or a join on one side does to its rows, as a sentence."""
    from konstruct.analysis.explain import split_condition

    clause, condition = split_condition(difference.detail)
    if re.search(r'\bNOT\b', condition, re.IGNORECASE):
        with contextlib.suppress(*PARSE_ERRORS):
            condition = _written(sqlglot.parse_one(condition))
    clause, condition = clause.upper(), _readable(condition)
    latest = _LATEST.match(condition) if clause.startswith('QUALIFY') else None
    if latest is not None:
        order = latest['order'].strip()
        which = 'earliest' if not re.search(r'\bDESC$', order, re.IGNORECASE) else 'latest'
        order = re.sub(r'\s+(ASC|DESC)$', '', order, flags=re.IGNORECASE)
        return f'{side} keeps only the {which} row per {latest["by"]} (by {order})'
    if difference.kind == 'filter':
        rows = 'groups' if clause.startswith('HAVING') else 'rows'
        return f'{side} keeps only the {rows} where {condition}'
    if clause.startswith(('LEFT', 'RIGHT', 'FULL')):
        how = clause.removesuffix(' ON').removesuffix(' USING')
        return f'{side} reads through a {how} on {condition}, which keeps the rows with no match'
    return f'{side} keeps only the rows that match on {condition}'


def _reach_text(reach: dict[str, int]) -> str:
    parts = [f'{v} {k}' for k, v in reach.items() if v]
    return ', '.join(parts) or 'nothing downstream'


@dataclass
class Conflicts:
    conflicts: list[Conflict] = field(default_factory=list)
    """Every conflict, most consequential downstream first; :attr:`Conflict.rank` orders them by
    how likely each is two definitions."""
    candidates: int = 0
    """Computed columns considered."""
    groups: int = 0
    """Groups of columns that appear to be one idea."""
    every: bool = field(default=False, repr=False)
    """Whether the text and Markdown list the pairs of other grains and those that share only a
    name too; ``to_dict`` always lists every pair, each with its ``kind``."""

    @property
    def measures(self) -> list[Conflict]:
        """The ``measure`` conflicts, confident or not."""
        return [c for c in self.conflicts if c.kind == MEASURE]

    @property
    def confident(self) -> list[Conflict]:
        """The confident conflicts, which the text lists by default, most likely first."""
        return sorted((c for c in self.conflicts if c.confident), key=lambda c: c.rank)

    def shown(self) -> list[Conflict]:
        """The conflicts the text and Markdown list, most likely first."""
        return sorted(self.conflicts, key=lambda c: c.rank) if self.every else self.confident

    def summary(self) -> str:
        """How many pairs there are, every one of them, as ``to_dict`` lists them."""
        if not self.conflicts:
            return f'No conflicting definitions among {self.candidates} computed columns.'
        count = len(self.conflicts)
        return (
            f'{count} conflicting definition{"s" if count != 1 else ""} among '
            f'{self.candidates} computed columns.'
        )

    def overview(self) -> str:
        """The sentence the text and Markdown open with: how many likely conflicts they list (each
        to be checked), and how many pairs of each other kind ``--all`` adds."""
        count = len(self.confident)
        tally = Counter(c.kind for c in self.conflicts if not c.confident)
        found = (
            f'{count} likely conflicting definition{"s" if count != 1 else ""} (check each)'
            if count
            else 'No likely conflicting definitions'
        )
        text = f'{found} among {self.candidates} computed columns'
        phrases = {
            MEASURE: ('may count something else', 'may count something else'),
            SEGMENT: ('is one measure over another segment', 'are one measure over other segments'),
            GRAIN: ('is one measure at another grain', 'are one measure at another grain'),
            NAME: ('shares only a name', 'share only a name'),
        }
        more: list[str] = []
        for kind in KINDS:
            if tally[kind]:
                one, many = phrases[kind]
                lead = (
                    f'{tally[kind]} more pair{"s" if tally[kind] != 1 else ""} '
                    if not more
                    else f'{tally[kind]} '
                )
                more.append(lead + (many if tally[kind] != 1 else one))
        if not more:
            return text + '.'
        if self.every:
            return text + ', and ' + _and(more) + '.'
        return text + '; ' + _and(more) + ' (--all lists them).'

    def to_dict(self) -> dict[str, Any]:
        return {
            'summary': self.summary(),
            'candidates': self.candidates,
            'groups': self.groups,
            'conflicts': [c.to_dict() for c in self.conflicts],
        }

    def text(self, style: Style | None = None) -> str:
        """The result as the CLI prints it: plain text, as ``str()`` gives, or drawn for a
        terminal with ``style``."""
        from konstruct.interfaces.render import conflicts_lines, show

        return show(conflicts_lines, self, style)

    def __str__(self) -> str:
        return self.text()

    def markdown(self) -> str:
        lines = ['# Conflicting definitions', '', self.overview(), '']
        if self.shown():
            lines += [
                'Each entry is a pair of columns that appear to measure the same thing but are computed '
                'differently, the most likely to be two definitions of one number first.',
                '',
            ]
        for index, conflict in enumerate(self.shown(), start=1):
            lines += [
                f'## {index}. {conflict.title or conflict.idea}: `{conflict.a.label}` and `{conflict.b.label}`',
                '',
            ]
            lines += [f'Found because they {conflict.reason}. Downstream weight {conflict.score}.', '']
            if conflict.kind == NAME:
                lines += ['They read no source column in common.', '']
            else:
                lines += [f'{conflict.explanation()}.', '']
            lines += ['| | Column | Formula in terms of sources | File | Feeds |', '|---|---|---|---|---|']
            for side, name in ((conflict.a, 'A'), (conflict.b, 'B')):
                formula = (side.composed or side.expression or 'none recorded').replace('|', '\\|')
                lines.append(
                    f'| {name} | `{side.label}` | `{formula}` | {side.path or ""} | {_reach_text(side.reach)} |'
                )
            lines += ['', '**What differs**', '']
            for difference in conflict.differences:
                who = ''
                if difference.side:
                    who = f' (only in {"A" if difference.side == "a" else "B"})'
                where = f', in `{difference.path}`' if difference.path else ''
                lines.append(f'- {difference.kind}{who}: `{difference.detail}`{where}')
                if difference.witness is not None:
                    lines.append(f'  - {difference.witness}')
            for side, name in ((conflict.a, 'A'), (conflict.b, 'B')):
                if side.carried_into:
                    copies = ', '.join(f'`{c}`' for c in side.carried_into)
                    lines += ['', f"{name}'s definition is carried into {copies}."]
            terms = sorted(set(conflict.a.terms) | set(conflict.b.terms))
            if terms:
                lines += ['', f'Business terms involved: {", ".join(terms)}.']
            lines.append('')
        return '\n'.join(lines).rstrip() + '\n'


def top_function(formula: str | None, dialect: str | None) -> str | None:
    """The aggregate a formula applies last (``SUM`` for ``SUM(x / 100)``), or None."""
    if not formula:
        return None
    try:
        tree = sqlglot.parse_one(formula, read=dialect)
    except PARSE_ERRORS:
        return None
    node = tree
    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, exp.AggFunc):
        return node.key.upper()
    return None


class _Finder:
    """Compares columns two at a time. A column is in many pairs, so everything about one column
    (its side, the keys of its conditions, its top aggregate) is worked out once, and a pair costs
    only the comparison of the two."""

    def __init__(self, graph: Graph) -> None:
        self.graph = graph
        dialect = graph.meta.get('dialect')
        self.dialect = dialect if isinstance(dialect, str) else None
        self.explainer = Explainer(graph)
        self.ancestors: dict[str, set[str]] = {}
        self.formulas: Formulas | None = None
        self.joinless: Formulas | None = None
        self._conditions: dict[str, tuple[list[Condition], list[Condition]]] = {}
        self._sides: dict[str, Side] = {}
        self._ideas: dict[tuple[str, str], Side] = {}
        self._keyed: dict[str, tuple[Keyed, Keyed]] = {}
        self._keys: dict[str, str] = {}
        self._tops: dict[str, str | None] = {}
        self._approved: Counter[str] | None = None
        self._analysed: dict[str, _Analysed] = {}
        self._read: dict[tuple[str, str], _Analysed] = {}
        self._roots: dict[str, str] = {}
        self._grains: dict[str, tuple[list[str], list[str], frozenset[str]] | None] = {}
        self._profiles: dict[str, _Profile | None] = {}
        self._amounts: dict[str, bool] = {}
        self._predicates: dict[str, dict[str, Predicate]] = {}
        self._tables_of: dict[frozenset[str], frozenset[str]] = {}
        self._bound: dict[str, frozenset[str]] = {}
        self._lineage_keys: dict[str, frozenset[str]] = {}

    def upstream(self, column_id: str) -> set[str]:
        if column_id not in self.ancestors:
            seen: set[str] = set()
            stack = [column_id]
            while stack:
                for edge in self.graph.out_edges(stack.pop(), 'derived_from'):
                    if edge.target not in seen:
                        seen.add(edge.target)
                        stack.append(edge.target)
            self.ancestors[column_id] = seen
        return self.ancestors[column_id]

    def candidates(self) -> list[Node]:
        found = []
        for node in self.graph.nodes.values():
            if node.kind != 'column' or not isinstance(node.attrs.get('expression'), str):
                continue
            owner = self.graph.parent_of(node)
            if owner is None or owner.kind not in RELATION_KINDS:
                continue
            words = set(_core.terms(node.name))
            if words & KEY_WORDS or not core(node.name):
                continue
            # A column copied as it is (a dimension, a key, a date) is lineage, not a definition.
            inputs = self.graph.out_edges(node.id, 'derived_from')
            if inputs and all(e.attrs.get('transform') == 'direct' for e in inputs):
                continue
            found.append(node)
        return found

    def term_keys(self) -> dict[frozenset[str], str]:
        keys: dict[frozenset[str], str] = {}
        for node in self.graph.nodes.values():
            if node.kind == 'term':
                for name in [node.name, *node.attrs.get('synonyms', [])]:
                    key = core(name)
                    if key:
                        keys.setdefault(key, node.name)
        return keys

    def where(self, relation: Node) -> str | None:
        """The file that builds a relation: its own, or that of the task that writes it."""
        if relation.path:
            return relation.path
        for edge in self.graph.in_edges(relation.id, 'feeds'):
            source = self.graph.nodes[edge.source]
            if source.kind == 'task' and source.path:
                return source.path
        return None

    def conditions(self, relation: Node) -> tuple[list[Condition], list[Condition]]:
        """A relation's filters and joins as (relation, condition, file). Many columns share the
        relations on their routes back to source, so each relation's conditions are read once."""
        found = self._conditions.get(relation.id)
        if found is None:
            filters: list[Condition] = []
            joins: list[Condition] = []
            for condition in relation.attrs.get('conditions', []):
                raw = uncommented(str(condition), self.dialect)
                if (
                    not raw.split(':', 1)[0].upper().startswith(FILTER_HEADS)
                    and 'JOIN' not in raw.split(':', 1)[0].upper()
                ):
                    # A grouping or a DISTINCT is neither a filter nor a join: it is not shortened.
                    continue
                # As written, as the pairs have always listed it; it is read without its comments.
                text = short_sql(self.graph, str(condition), condition=True)
                head = text.split(':', 1)[0].upper()
                entry = (self.graph.label(relation), text, self.where(relation))
                if head.startswith(FILTER_HEADS):
                    filters.append(entry)
                elif 'JOIN' in head and joined_reading(text.split(': ', 1)[0])[1] is None:
                    # A join the build lists once more to name the reading it joins is one join.
                    joins.append(entry)
                else:
                    continue
                self._read[entry[0], text] = self.analysed(raw, text)
            found = self._conditions[relation.id] = (filters, joins)
        return found

    def analysed(self, raw: str, text: str) -> _Analysed:
        """A condition read once, however many relations record it (``text`` is its short form)."""
        found = self._analysed.get(raw)
        if found is None:
            found = self._analysed[raw] = self._analyse(raw, text)
        return found

    def _analyse(self, raw: str, text: str) -> _Analysed:
        """The condition's key, with each column read as the source column it copies and the
        clause named without where it runs (``WHERE BEFORE WINDOW`` is a ``WHERE``, and a
        ``HAVING`` that tests no aggregate a ``WHERE``, since it keeps what that ``WHERE`` keeps),
        the source relations it reads, those a deduplication orders by, and its conjuncts. The
        condition is parsed once: the key is :func:`~konstruct.core.sql.condition_key`'s, made from
        the conjuncts the predicates are read from."""
        label, sep, body = raw.partition(': ')
        label = label.replace(' BEFORE WINDOW', '')
        if label.startswith(('WHERE', 'HAVING')):
            # Where a filter runs (WHERE BEFORE WINDOW, WINDOW) does not change the rows it keeps.
            label = label.split(' ', 1)[0].rstrip(',')
        try:
            # A condition of hundreds of kilobytes (a generated list) is compared by its text, as
            # any other is, but not read for what each of its columns is a copy of.
            tree = sqlglot.parse_one(body, read=self.dialect) if sep and len(body) <= LARGE else None
        except PARSE_ERRORS:
            tree = None
        if tree is None:
            return _Analysed(self.key(text), frozenset(), frozenset(), ())
        ordered = self._ordered_by(tree) if label.startswith('QUALIFY') else frozenset()
        tree, roots = self._as_sources(tree)
        if label.startswith('HAVING') and tree.find(exp.AggFunc) is None:
            label = 'WHERE' + label[len('HAVING') :]
        filtering = label.startswith(FILTER_HEADS)
        conjuncts = []
        normal = []
        equal = []
        for index, part in enumerate(split_conjuncts(tree)):
            if isinstance(part, exp.EQ) and id(part.this) in roots and id(part.expression) in roots:
                equal.append((roots[id(part.this)][0], roots[id(part.expression)][0]))
            predicate = self.predicate(label, part, roots, (text, index)) if filtering else None
            forms = self.normal(part)
            normal += forms
            if predicate is not None:
                conjuncts.append(replace(predicate, key=f'{predicate.key}: {" AND ".join(sorted(forms))}'))
        key = f'{label}: {" AND ".join(sorted(normal))}'
        tables = frozenset(t for _, t in roots.values() if t is not None)
        return _Analysed(key, tables, ordered, tuple(conjuncts), tuple(equal))

    def normal(self, part: exp.Expr) -> list[str]:
        """The conjunct in :func:`~konstruct.core.sql.normal_condition`'s form, as SQL: a BETWEEN
        as its two bounds. It changes ``part``."""
        return [
            # The conjunct is read no more, so it is written without a copy.
            normal_condition(p, self.dialect).sql(dialect=self.dialect, copy=False)
            for p in split_conjuncts(part, between=True)
        ]

    def _as_sources(self, tree: exp.Expr) -> tuple[exp.Expr, dict[int, tuple[str, str | None]]]:
        """``tree`` with each column read as the source column it copies, and for each column so
        read (by ``id``) its source's name (``shop.orders.status``, as a composed formula names it)
        and its relation's id."""
        roots: dict[int, tuple[str, str | None]] = {}
        for ref, column in self.columns_in(tree):
            root = self.root(column.id)
            source = self.graph.nodes[root]
            owner = self.graph.parent_of(source)
            if owner is None:
                continue
            label = self.graph.label(owner)
            replacement = exp.column(source.name, table=exp.to_identifier(label))
            roots[id(replacement)] = (f'{label}.{source.name}'.lower(), owner.id)
            if ref is tree:
                tree = replacement
            ref.replace(replacement)
        return tree, roots

    def predicate(
        self,
        label: str,
        part: exp.Expr,
        roots: dict[int, tuple[str, str | None]],
        shown: tuple[str, int],
        *,
        formula: bool = False,
    ) -> Predicate:
        """A conjunct of a condition whose columns :meth:`_as_sources` has read (``roots``), with
        only its clause as its key, to which the caller adds the conjunct's normal form. ``shown``
        is the condition as written and the conjunct's place in it."""
        found = [roots[id(c)] for c in part.find_all(exp.Column) if id(c) in roots]
        columns = frozenset(name for name, _ in found)
        tables = frozenset(table for _, table in found if table is not None)
        aggregate = part.find(exp.AggFunc) is not None
        head = 'WHERE' if label.startswith('HAVING') and not aggregate else label
        kept = _kept(part) if len(columns) == 1 else None
        return Predicate(head, shown, columns, tables, aggregate, kept, formula)

    def root(self, column_id: str) -> str:
        """The column a column is a plain copy of, followed back to its source: two filters on
        copies of one column (``fact_sales.status`` and ``dim_orders.status``) filter alike."""
        found = self._roots.get(column_id)
        if found is None:
            found, seen = column_id, {column_id}
            while True:
                inputs = self.graph.out_edges(found, 'derived_from')
                if (
                    len(inputs) != 1
                    or inputs[0].attrs.get('transform') != 'direct'
                    or inputs[0].target in seen
                ):
                    break
                found = inputs[0].target
                seen.add(found)
            self._roots[column_id] = found
        return found

    def columns_in(self, tree: exp.Expr) -> list[tuple[exp.Column, Node]]:
        """The graph's column behind each column reference in ``tree``."""
        found = []
        for ref in list(tree.find_all(exp.Column)):
            parts = [p.name for p in ref.parts[:-1]]
            if parts and '#' in parts[-1]:
                # A reading of a relation (orders#1, the first time a query reads orders) reads
                # the relation's own columns.
                parts[-1] = parts[-1].split('#', 1)[0]
                ref.set('table', exp.to_identifier(parts[-1]))
            table = '.'.join(parts)
            relation = self.graph.node_for_relation(table) if table else None
            column = self.graph.column(relation.id, ref.name) if relation is not None else None
            if column is not None:
                found.append((ref, column))
        return found

    def _ordered_by(self, tree: exp.Expr) -> frozenset[str]:
        """For a deduplication (``QUALIFY ROW_NUMBER() OVER (... ORDER BY x) = 1``), the source
        relations its order reads; empty for any other condition."""
        window = tree.find(exp.Window)
        if window is None or not isinstance(window.this, exp.RowNumber) or window.args.get('order') is None:
            return frozenset()
        tables = set()
        for _, column in self.columns_in(window.args['order']):
            owner = self.parent_of_id(self.root(column.id))
            if owner is not None:
                tables.add(owner.id)
        return frozenset(tables)

    def grain(self, column: Node) -> tuple[list[str], list[str], frozenset[str]] | None:
        """What the column's relation groups by, as the names of the grouping columns, the
        source columns they come from, and the source columns they copy; None when it groups by
        nothing."""
        owner = self.graph.parent_of(column)
        if owner is None:
            return None
        if owner.id not in self._grains:
            names: list[str] = []
            sources: set[str] = set()
            roots: set[str] = set()
            for condition in owner.attrs.get('conditions', []):
                head, _, text = str(condition).partition(': ')
                if not head.startswith('GROUP BY'):
                    continue
                try:
                    tree = sqlglot.parse_one(f'SELECT 1 GROUP BY {text}', read=self.dialect)
                except PARSE_ERRORS:
                    continue
                for _, grouped in self.columns_in(tree):
                    names.append(grouped.name)
                    root = self.root(grouped.id)
                    parent = self.parent_of_id(root)
                    if parent is not None:
                        roots.add(f'{self.graph.label(parent)}.{self.graph.nodes[root].name}'.lower())
                    sources.add(root)
                    # The source columns it is computed from (DATE_TRUNC of an order date), found
                    # by the walk that is already made, not by composing its formula.
                    sources.update(
                        a for a in self.upstream(grouped.id) if not self.graph.out_edges(a, 'derived_from')
                    )
            found = (sorted(set(names)), sorted(sources), self.joined_to(owner, roots)) if names else None
            self._grains[owner.id] = found
        return self._grains[owner.id]

    def joined_to(self, owner: Node, roots: set[str]) -> frozenset[str]:
        """``roots`` with each column an equality in one of the relation's joins makes equal to one
        of them: grouping by ``orders.id`` groups by ``items.order_id`` when they are joined on it."""
        self.conditions(owner)
        pairs = [
            pair
            for condition in owner.attrs.get('conditions', [])
            if (known := self._analysed.get(uncommented(str(condition), self.dialect))) is not None
            for pair in known.equal
        ]
        found = set(roots)
        grown = True
        while grown:
            grown = False
            for first, second in pairs:
                if (first in found) != (second in found):
                    found |= {first, second}
                    grown = True
        return frozenset(found)

    def side(self, column: Node) -> Side:
        """The column as one side of a pair, made once and shared by every pair it is in."""
        found = self._sides.get(column.id)
        if found is None:
            found = self._sides[column.id] = self._side(column)
        return found

    def side_in(self, idea: str, column: Node, carried: list[str]) -> Side:
        """The column's side in the conflicts of one idea, with the columns of that idea derived
        from it: a column can be a side in several ideas, with other copies in each."""
        found = self._ideas.get((idea, column.id))
        if found is None:
            found = self._ideas[idea, column.id] = replace(self.side(column), carried_into=carried)
        return found

    def approved(self) -> Counter[str]:
        """How many approved terms have each name."""
        if self._approved is None:
            self._approved = Counter(
                n.name
                for n in self.graph.nodes.values()
                if n.kind == 'term' and n.attrs.get('status') == 'approved'
            )
        return self._approved

    def _side(self, column: Node) -> Side:
        graph = self.graph
        owner = graph.parent_of(column)
        assert owner is not None
        explanation = self.explainer.explain(column)
        filters: list[Condition] = []
        joins: list[Condition] = []
        relations = {owner.id: owner}
        for ancestor in self.upstream(column.id):
            parent = graph.parent_of(graph.nodes[ancestor])
            if parent is not None:
                relations[parent.id] = parent
        for relation in relations.values():
            relation_filters, relation_joins = self.conditions(relation)
            filters.extend(relation_filters)
            joins.extend(relation_joins)
        walk = graph.impact(column)
        reach = {
            'exposures': sum(1 for s in walk if s.node.kind == 'exposure'),
            'tasks': sum(1 for s in walk if s.node.kind == 'task'),
            'columns': sum(1 for s in walk if s.node.kind == 'column'),
        }
        terms = sorted(
            {
                graph.nodes[e.source].name
                for target in [column.id, *(s.node.id for s in walk)]
                for e in graph.in_edges(target, 'defines')
            }
        )
        approved = self.approved()
        reach['terms'] = sum(approved[name] for name in terms)
        read = {o.id for a in self.upstream(column.id) if (o := self.parent_of_id(a)) is not None}
        route = self.route({owner.id, *read}, joins)
        grain = self.grain(column)
        return Side(
            column=column.id,
            label=graph.label(column),
            relation_kind=owner.kind,
            path=self.where(owner),
            expression=explanation.expression,
            composed=explanation.composed,
            sources=explanation.sources,
            filters=sorted(set(filters)),
            joins=sorted(set(joins)),
            reach=reach,
            terms=terms,
            grain=grain[0] if grain else None,
            grain_sources=grain[1] if grain else None,
            grain_roots=grain[2] if grain else frozenset(),
            route=frozenset(route),
            counted=sorted(
                # A value of no source column (COUNT(*), a constant) has its query's rows: there
                # is no route to place a filter on, so each of its query's filters is on it.
                {
                    f
                    for f in filters
                    if not read or (self.bears_on(f, read, joins) and self.on_route(f, route))
                }
            ),
        )

    def route(self, relations: set[str], joins: list[Condition]) -> set[str]:
        """The relations a column reads, and those a join on its route joins to them: a filter on
        any of these can change its rows, and one on another relation the same query reads (in a
        CTE the column's rows never meet) cannot."""
        joined = [
            self._read[relation, text].tables for relation, text, _ in joins if (relation, text) in self._read
        ]
        grown = True
        while grown:
            grown = False
            for tables in joined:
                if tables & relations and not tables <= relations:
                    relations |= tables
                    grown = True
        return relations

    def on_route(self, condition: Condition, route: set[str]) -> bool:
        """Whether a filter reads a relation on the column's route, or nothing it can place."""
        analysed = self._read.get((condition[0], condition[1]))
        return analysed is None or not analysed.tables or bool(analysed.tables & route)

    def parent_of_id(self, column_id: str) -> Node | None:
        node = self.graph.nodes.get(column_id)
        return self.graph.parent_of(node) if node is not None else None

    def bears_on(self, condition: Condition, read: set[str], joins: list[Condition]) -> bool:
        """Whether a filter can change the rows a measure reading the relations ``read`` counts.
        A deduplication (``QUALIFY ROW_NUMBER() ... = 1``) does not bear on it when it orders by
        a relation the measure does not read (it picks one of the rows a join repeats), or when
        it dedupes such a relation reached only through outer joins."""
        relation, text, _ = condition
        if not text.upper().startswith('QUALIFY') or not _LATEST.match(text.split(': ', 1)[-1]):
            return True
        analysed = self._read.get((relation, text))
        ordered = analysed.ordered if analysed is not None else frozenset()
        if ordered and not ordered & read:
            # It picks one of the rows a join to another relation repeats, by that relation's
            # columns: it undoes the join's fan-out, which a side without the join never had.
            return False
        tables = analysed.tables if analysed is not None else frozenset()
        if not tables or tables & read:
            return True
        for joined in joins:
            head = joined[1].split(':', 1)[0].upper()
            known = self._read.get((joined[0], joined[1]))
            if known is not None and known.tables & tables and not head.startswith(('LEFT', 'RIGHT', 'FULL')):
                return True
        return False

    def bears(self, mine: Side, theirs: Side, condition: str) -> bool:
        """Whether a filter only ``mine`` lists, as written, can change what it counts: it is one
        of :attr:`Side.counted`, and read through lineage it is no filter ``theirs`` counts by."""
        other = self.lineage_keys(theirs)
        return any(
            (known := self._read.get((relation, text))) is None or known.key not in other
            for relation, text, _ in mine.counted
            if text == condition
        )

    def lineage_keys(self, side: Side) -> frozenset[str]:
        """The keys of the side's :attr:`Side.counted` filters, each column read as the source
        column it copies."""
        found = self._lineage_keys.get(side.column)
        if found is None:
            found = self._lineage_keys[side.column] = frozenset(
                known.key if (known := self._read.get((relation, text))) is not None else self.key(text)
                for relation, text, _ in side.counted
            )
        return found

    def key(self, condition: str) -> str:
        """The condition in the form that compares equal to any condition that filters alike."""
        found = self._keys.get(condition)
        if found is None:
            found = self._keys[condition] = condition_key(condition, self.dialect)
        return found

    def keyed(self, side: Side) -> tuple[Keyed, Keyed]:
        """The side's filters and its joins, each condition with its key."""
        found = self._keyed.get(side.column)
        if found is None:
            kinds = []
            for items in (side.filters, side.joins):
                # A condition's own key, as the pairs have always been told apart by: what lineage
                # makes of it (one filter on two copies of a column) decides the kind, not the list.
                listed = [(condition, path, self.key(condition)) for _, condition, path in items]
                kinds.append((listed, frozenset(k for _, _, k in listed)))
            found = self._keyed[side.column] = (kinds[0], kinds[1])
        return found

    def top(self, formula: str | None) -> str | None:
        """:func:`top_function` of the formula, read once for every pair it is in."""
        if formula is None:
            return None
        if formula not in self._tops:
            self._tops[formula] = top_function(formula, self.dialect)
        return self._tops[formula]

    def profile(self, formula: str | None) -> _Profile | None:
        """:class:`_Profile` of a composed formula, read once for every side that has it."""
        if formula is None:
            return None
        if formula not in self._profiles:
            self._profiles[formula] = self._profile(uncommented(formula, self.dialect))
        return self._profiles[formula]

    def _profile(self, formula: str) -> _Profile | None:
        try:
            tree = sqlglot.parse_one(formula, read=self.dialect)
        except PARSE_ERRORS:
            return None
        names: dict[str, bool] = {}
        for ref in tree.find_all(exp.Column):
            count = ref.find_ancestor(exp.Count)
            distinct = isinstance(count, exp.Count) and isinstance(count.this, exp.Distinct)
            names[ref.name.lower()] = names.get(ref.name.lower(), True) and distinct
        # Each condition that keeps the value only for some rows, as written, before its columns
        # are read as their sources.
        written = [f'WHERE: {c.sql(dialect=self.dialect, comments=False)}' for c in _keeping(tree)]
        roots: dict[int, tuple[str, str | None]] = {}
        for ref in list(tree.find_all(exp.Column)):
            # A composed formula names source columns, each read through the readings that led to
            # it (``"orders#(stg_orders#1)".amount``): each is named here as a filter's column is.
            relation = '.'.join(_READING_SUFFIX.sub('', p.name) for p in ref.parts[:-1])
            if not relation:
                continue
            named = exp.column(ref.name, table=exp.to_identifier(relation))
            roots[id(named)] = (f'{relation}.{ref.name}'.lower(), None)
            if ref is tree:
                tree = named
            ref.replace(named)
        refs = [r for r in tree.find_all(exp.Column) if id(r) in roots and not _in_condition(r)]
        values = {roots[id(r)][0] for r in refs}
        whole = {roots[id(r)][0] for r in refs if not _in_union(r)}
        added = {roots[id(r)][0] for r in refs if r.find_ancestor(exp.Coalesce) is None}
        aggregate = tree.find(exp.AggFunc) is not None
        windowed = any(isinstance(w.this, exp.AggFunc) for w in tree.find_all(exp.Window))
        conditions = []
        for condition, text in zip(_keeping(tree), written, strict=False):
            for index, part in enumerate(split_conjuncts(condition)):
                predicate = self.predicate('WHERE', part, roots, (text, index), formula=True)
                key = f'{predicate.key}: {" AND ".join(sorted(self.normal(part)))}'
                conditions.append(replace(predicate, key=key))
        return _Profile(
            aggregate=aggregate,
            values=frozenset(values),
            counted=frozenset(n for n, only in names.items() if only),
            conditions=tuple(conditions),
            unioned=frozenset(values - whole),
            chosen=frozenset(values - added),
            windowed=windowed,
        )

    def counted(self, side: Side, source: str) -> bool:
        """Whether the side's formula reads ``source`` only inside ``COUNT(DISTINCT ...)``."""
        profile = self.profile(side.composed)
        return profile is not None and source.rsplit('.', 1)[-1].lower() in profile.counted

    def measure(self, side: Side) -> bool:
        """Whether the column holds an amount (:func:`konstruct.analysis.measures.amount`), rather
        than a date, a label or a flag."""
        found = self._amounts.get(side.column)
        if found is None:
            node = self.graph.nodes[side.column]
            found = self._amounts[side.column] = amount(
                side.composed or side.expression, self.dialect, name=node.name, data_type=node.data_type
            )
        return found

    def bound(self, column_id: str) -> frozenset[str]:
        """The business terms bound to the column itself."""
        found = self._bound.get(column_id)
        if found is None:
            found = self._bound[column_id] = frozenset(
                self.graph.nodes[e.source].name
                for e in self.graph.in_edges(column_id, 'defines')
                if e.attrs.get('role') != 'input'
            )
        return found

    def predicates(self, side: Side) -> dict[str, Predicate]:
        """What the side counts only some rows of, by key: the conjuncts of the filters on its
        route and of the conditions in its formula. A conjunct of a filter that reads only the
        side's own grouping columns is left out: it drops whole groups and changes no other's."""
        found = self._predicates.get(side.column)
        if found is None:
            found = {}
            for relation, text, _ in side.counted:
                analysed = self._read.get((relation, text))
                for predicate in analysed.conjuncts if analysed is not None else ():
                    grouping = predicate.columns and predicate.columns <= side.grain_roots
                    if not grouping or predicate.aggregate:
                        found.setdefault(predicate.key, predicate)
            profile = self.profile(side.composed)
            for predicate in profile.conditions if profile is not None else ():
                found.setdefault(predicate.key, predicate)
            self._predicates[side.column] = found
        return found

    def values(self, side: Side) -> frozenset[str]:
        profile = self.profile(side.composed)
        return profile.values if profile is not None else frozenset()

    def assess(self, a: Side, b: Side) -> _Assessed:
        """What tells the two sides apart in what they count (see :class:`_Assessed`)."""
        mine, theirs = self.predicates(a), self.predicates(b)
        only = {
            'a': [p for k, p in mine.items() if k not in theirs],
            'b': [p for k, p in theirs.items() if k not in mine],
        }
        segments: dict[str, set[Predicate]] = {'a': set(), 'b': set()}
        unsure = False
        for first in only['a']:
            for second in only['b']:
                if not first.columns & second.columns:
                    continue
                overlap = _overlap(first.kept, second.kept) if first.columns == second.columns else None
                if overlap is False:
                    segments['a'].add(first)
                    segments['b'].add(second)
                elif overlap is None:
                    unsure = True
        left, right = self.values(a), self.values(b)
        differ = frozenset(c for c in left ^ right if not _key_like(c))
        return _Assessed(
            only={k: tuple(p for p in v if p not in segments[k]) for k, v in only.items()},
            segments={k: tuple(sorted(v, key=lambda p: p.key)) for k, v in segments.items()},
            segment=bool(segments['a']),
            unsure=unsure,
            values=differ,
            shared=bool(left & right) and (left <= right or right <= left),
        )

    def linked(self, a: Side, b: Side) -> bool:
        """Whether the two values are made of a source column in common, and one of no relation
        the other's is not: two values of different tables (travel and vehicles) that share only a
        column of a lookup table (an emission factor) are not one measure."""
        left, right = self.values(a), self.values(b)
        if not left & right:
            return False
        first, second = self.tables_of(left), self.tables_of(right)
        return first <= second or second <= first

    def tables_of(self, columns: frozenset[str]) -> frozenset[str]:
        found = self._tables_of.get(columns)
        if found is None:
            found = self._tables_of[columns] = frozenset(c.rsplit('.', 1)[0] for c in columns)
        return found

    def kind(self, a: Side, b: Side, differences: list[Difference], assessed: _Assessed) -> str:
        """For two amounts that :meth:`linked` links: ``measure`` when something but a grain or a
        segment tells them apart; ``segment`` when each keeps values of a column the other leaves
        out; ``grain`` when only the grain, a join or the aggregation a grain brings does."""
        strong = {d.kind for d in differences if _definitional(d)}
        counts = bool(assessed.only['a'] or assessed.only['b'] or assessed.values)
        if _grain_key(a) != _grain_key(b):
            # At another grain, a formula or a key it counts by is what the grain brings; only a
            # filter or another amount read says the two count something else.
            return MEASURE if counts else SEGMENT if assessed.segment else GRAIN
        if counts or ('formula' in strong and not assessed.segment):
            return MEASURE
        if assessed.segment:
            return SEGMENT
        kinds = {d.kind for d in differences}
        inner = [
            d
            for d in differences
            if d.kind == 'join' and not d.detail.upper().startswith(('LEFT', 'RIGHT', 'FULL'))
        ]
        # At one grain, an inner join on one side keeps fewer rows; an outer join keeps them all.
        return MEASURE if inner or 'aggregation' in kinds else GRAIN

    def confident(self, a: Side, b: Side, assessed: _Assessed, *, term: bool = False) -> bool:
        """Whether a ``measure`` pair is likely two definitions of one number: the two values
        are made of a source column in common and no segment or unknown restriction of one column
        tells them apart, and either one side's value adds a column at the same grain, or the two
        values are made of the same columns and a predicate only one side has keeps some of the
        rows it counts (a condition in its formula, or a filter on its route in front of an
        aggregate). A filter in front of no aggregate only drops rows; it changes no value."""
        if not assessed.shared or assessed.segment or assessed.unsure:
            return False
        if not term and _said(a.label) != _said(b.label):
            # Names that mean the same only through the words search folds together (expenses
            # and total_cost) are weaker evidence of one number than one name, or a business
            # term's synonyms.
            return False
        profiles = self.profile(a.composed), self.profile(b.composed)
        if assessed.values:
            known = [p for p in profiles if p is not None]
            other = frozenset().union(*(p.unioned | p.chosen for p in known))
            amounts = {v for v in assessed.values if not _MOMENT.search(v.rsplit('.', 1)[-1])}
            if not amounts or amounts & other or len({p.windowed for p in known}) > 1:
                # A column read only in another branch of a UNION adds rows of another population,
                # one read as an alternative (COALESCE) replaces the value, a moment changes what
                # a duration is counted from, and a running total is another measure: none adds
                # a term to the same amount.
                return False
            both = (
                bool(profiles[0] and profiles[0].aggregate),
                bool(profiles[1] and profiles[1].aggregate),
            )
            return _rolls_up(a, b, both)
        for side, mine in (('a', a), ('b', b)):
            profile = self.profile(mine.composed)
            aggregates = profile is not None and profile.aggregate
            for predicate in assessed.only[side]:
                # A test of an aggregate keeps or drops whole groups (HAVING COUNT(*) >= 5) and
                # changes no group's value.
                placed = bool(predicate.tables & mine.route) or predicate.formula
                if (
                    placed
                    and predicate.columns
                    and not predicate.aggregate
                    and (predicate.formula or aggregates)
                ):
                    return True
        return False

    def compare(self, a: Side, b: Side) -> list[Difference]:
        differences: list[Difference] = []
        sources_a, sources_b = set(a.sources), set(b.sources)
        for source in sorted(sources_a - sources_b):
            differences.append(Difference('source', source, 'a'))
        for source in sorted(sources_b - sources_a):
            differences.append(Difference('source', source, 'b'))
        for kind, (listed_a, keys_a), (listed_b, keys_b) in zip(
            ('filter', 'join'), self.keyed(a), self.keyed(b), strict=True
        ):
            # Conditions that filter alike, however they are written, are not a difference.
            for condition, path, key in listed_a:
                if key not in keys_b:
                    differences.append(Difference(kind, condition, 'a', path))
            for condition, path, key in listed_b:
                if key not in keys_a:
                    differences.append(Difference(kind, condition, 'b', path))
        for difference in differences:
            if difference.kind == 'filter':
                mine, theirs = (a, b) if difference.side == 'a' else (b, a)
                difference.bears = self.bears(mine, theirs, difference.detail)
        if a.composed is not None and a.composed == b.composed:
            # The same text is the same formula. What else tells the two apart, such as a
            # not_null test on one or an outer join on the route of one, is in the filters and
            # joins above, not in the formula.
            return differences
        verdict = self.verdict(a, b)
        # A verdict of the same with no guarantee (a divisor zero at a sampled point) is not
        # taken as the same.
        trusted = verdict is not None and verdict.error_bound < 1
        if trusted and verdict is not None and verdict.equal:
            return differences
        if trusted and any(d.kind == 'join' for d in differences) and self.same_inside_joins(a, b):
            # They differ only through an outer join on one route, which is reported above.
            return differences
        witness = verdict.witness if verdict is not None else None
        if trusted and verdict is not None and verdict.equal_without_nulls:
            if not any(d.kind == 'source' for d in differences):
                detail = f'{a.composed} against {b.composed}, {SAME_UNLESS_NULL}'
                differences.append(Difference('formula', detail, witness=witness))
            return differences
        top_a, top_b = self.top(a.composed), self.top(b.composed)
        if top_a != top_b and (top_a or top_b):
            detail = f'{top_a or "none"} against {top_b or "none"}'
            differences.append(Difference('aggregation', detail, witness=witness))
            witness = None
        differs = verdict is not None or a.composed != b.composed
        if differs and not any(d.kind in ('source', 'aggregation') for d in differences):
            differences.append(Difference('formula', f'{a.composed} against {b.composed}', witness=witness))
        return differences

    def conflict(
        self, idea: str, reason: str, a: Side, b: Side, differences: list[Difference], title: str
    ) -> Conflict:
        """The pair as a conflict, of its kind, and whether it is confident."""
        bound = bool(self.bound(a.column) & self.bound(b.column))
        if not ((bound or self.linked(a, b)) and self.measure(a) and self.measure(b)):
            # Most pairs a name links are not one measure: nothing more is read of them.
            return Conflict(idea, reason, a, b, differences, NAME, title=title)
        for difference in differences:
            if difference.kind == 'source':
                # Read only for the pairs of amounts, whose kind it can change.
                difference.counted = self.counted(a if difference.side == 'a' else b, difference.detail)
        assessed = self.assess(a, b)
        kind = MEASURE if bound else self.kind(a, b, differences, assessed)
        term = reason.startswith(('are named like the business term', 'are bound to the business term'))
        confident = kind == MEASURE and self.confident(a, b, assessed, term=term)
        return Conflict(idea, reason, a, b, differences, kind, confident, title=title, assessed=assessed)

    def same_inside_joins(self, a: Side, b: Side) -> bool:
        """Whether the two formulas are the same when a column read through an outer join is
        taken as it is in its own relation: then what tells them apart is the join."""
        if self.formulas is None or not self.formulas.joins:
            return False
        if self.joinless is None:
            self.joinless = Formulas(self.graph, conditions=False, outer=False)
        first = self.joinless.column(self.graph.nodes[a.column])
        verdict = self.joinless.compare(first, self.joinless.column(self.graph.nodes[b.column]))
        return verdict.equal and verdict.error_bound < 1

    def verdict(self, a: Side, b: Side) -> Verdict | None:
        """Whether two columns' formulas compute the same values, however they are written.
        Their conditions are left out: the filters and joins on each route are compared on
        their own, and reported with the files they are in."""
        if a.composed is None or b.composed is None:
            return None
        if self.formulas is None:
            self.formulas = Formulas(self.graph, conditions=False)
        first = self.formulas.column(self.graph.nodes[a.column])
        return self.formulas.compare(first, self.formulas.column(self.graph.nodes[b.column]))


def _likely(conflict: Conflict) -> tuple[int, bool, int, int, str, str, str]:
    """The order of :attr:`Conflict.rank`: the kinds in order, confident pairs first, then the most
    likely, then the most consequential."""
    return (
        KINDS.index(conflict.kind),
        not conflict.confident,
        -conflict.likelihood,
        -conflict.score,
        conflict.idea,
        conflict.a.label,
        conflict.b.label,
    )


def _pairs(nodes: list[Node], touching: Collection[str] | None) -> Iterator[tuple[Node, Node]]:
    """Each pair of ``nodes`` in the order :func:`itertools.combinations` gives them; with
    ``touching``, only the pairs with a column in it, without visiting the others."""
    if touching is None:
        yield from combinations(nodes, 2)
        return
    marked = [i for i, node in enumerate(nodes) if node.id in touching]
    for i, first in enumerate(nodes):
        later = range(i + 1, len(nodes)) if first.id in touching else marked[bisect_right(marked, i) :]
        for j in later:
            yield first, nodes[j]


def find_conflicts(
    graph: Graph, limit: int | None = None, *, touching: Collection[str] | None = None
) -> Conflicts:
    """Pairs of columns that appear to be one business idea computed differently, ranked.

    With ``touching``, a set of column ids, only the pairs with a column in it are compared: the
    conflicts are those of every pair that involve one of those columns, in the same order, at a
    cost that grows with how many columns it names rather than with the number of pairs."""
    finder = _Finder(graph)
    candidates = finder.candidates()
    term_keys = finder.term_keys()
    groups: dict[str, list[Node]] = defaultdict(list)
    reasons: dict[str, str] = {}
    for column in candidates:
        words = core(column.name)
        term = term_keys.get(words)
        if term:
            groups[f'term:{term}'].append(column)
            reasons[f'term:{term}'] = f'are named like the business term {term!r}'
        else:
            groups['name:' + ' '.join(sorted(words))].append(column)
        for edge in graph.in_edges(column.id, 'defines'):
            # A column a metric reads is its input, not a column that holds the measure.
            if edge.attrs.get('role') == 'input':
                continue
            name = graph.nodes[edge.source].name
            if column not in groups[f'term:{name}']:
                groups[f'term:{name}'].append(column)
            reasons[f'term:{name}'] = f'are bound to the business term {name!r}'
    result = Conflicts(candidates=len(candidates))
    seen_pairs: set[tuple[str, str]] = set()
    for key, members in groups.items():
        relations = {m.parent for m in members}
        if len(members) < 2 or len(relations) < 2:
            continue
        result.groups += 1
        idea = key.split(':', 1)[1]
        title = idea if key.startswith('term:') else _title(members)
        reason = reasons.get(key, 'have names that mean the same thing')
        # Compare only the columns that define the idea themselves; one derived from another
        # member inherits its definition and is listed as carried into it.
        ids = {m.id for m in members}
        roots = [m for m in members if not (finder.upstream(m.id) & ids)]
        # Each member's own ancestors among the roots, not every root against every member.
        carried: dict[str, list[str]] = {r.id: [] for r in roots}
        for member in members:
            for root in finder.upstream(member.id) & carried.keys():
                carried[root].append(graph.label(member))
        for labels in carried.values():
            labels.sort()
        for first, second in _pairs(sorted(roots, key=lambda n: n.id), touching):
            pair = (first.id, second.id)
            if pair in seen_pairs or first.parent == second.parent:
                continue
            seen_pairs.add(pair)
            # A column derived from the other is a copy, not a competing definition.
            if first.id in finder.upstream(second.id) or second.id in finder.upstream(first.id):
                continue
            a, b = (
                finder.side_in(key, first, carried[first.id]),
                finder.side_in(key, second, carried[second.id]),
            )
            differences = finder.compare(a, b)
            if not differences:
                continue
            result.conflicts.append(finder.conflict(idea, reason, a, b, differences, title))
    result.conflicts.sort(key=lambda c: (-c.score, c.idea, c.a.label, c.b.label))
    for rank, conflict in enumerate(sorted(result.conflicts, key=_likely), start=1):
        conflict.rank = rank
    if limit is not None:
        result.conflicts = result.conflicts[:limit]
    return result


def measure_conflicts(graph: Graph, term: str, columns: list[Node]) -> list[Conflict]:
    """Pairs of the columns bound to one business term that compute it differently.

    The same comparison as :func:`find_conflicts`, restricted to one term's columns: a column
    derived from another of them carries its definition and is not compared. The ontology uses it
    for the rule that a measure has one formula."""
    finder = _Finder(graph)
    ids = {c.id for c in columns}
    roots = sorted((c for c in columns if not (finder.upstream(c.id) & ids)), key=lambda n: n.id)
    found: list[Conflict] = []
    for first, second in combinations(roots, 2):
        if first.parent == second.parent:
            continue
        a, b = finder.side(first), finder.side(second)
        differences = finder.compare(a, b)
        if differences:
            found.append(
                finder.conflict(term, f'are bound to the business term {term!r}', a, b, differences, term)
            )
    return found
