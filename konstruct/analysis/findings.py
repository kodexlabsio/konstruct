"""The findings `konstruct build` ends with: the few things in the graph most worth acting on.

A graph answers questions, but a first build has asked none yet. These findings are the answers
most likely to matter, each one line that says what was found and the exact command that shows
more, in this order:

* ``conflict``: up to three pairs of columns that the project itself says hold one business term
  and that compute it differently (:mod:`konstruct.analysis.conflicts`), the most consequential
  first: both columns bound to the term (by definitions.yml or a vocabulary), or one bound to it
  and the other, bound to no term of its own and named like it or one of its synonyms, a likely
  measure conflict with it (``confident``). Each has a plain explanation of the difference
  and how the two differ on the data when the last ``konstruct verify --conflicts`` ran this same
  pair. A pair that only a name links is never one: in public projects those are rarely two
  definitions of one number, and most differ on purpose, by grain or segment;
* ``definition``: an approved business term the code no longer matches: a column its definition
  names that no model builds any more, or a column ``konstruct.lock`` pins that changed; none when
  a dbt project was found but not read, whose models the graph then lacks;
* ``claim``: a claim of the graph the data contradicted when ``konstruct verify`` last ran (an
  influence the lineage does not record, a key that repeats, a relationship with orphans), or,
  where there is data to check it against and verify has not run, that the lineage is unchecked;
* ``reach``: the project's own column (not a package's, such as elementary's) whose change
  reaches the most exposures, and then the most of the project's own columns.

Each kind but ``conflict`` gives at most one finding. A finding that cannot be made (a file an
earlier run wrote that this Konstruct cannot read) is left out with a note, never raised.

They read only the graph and what earlier runs wrote beside it, never the warehouse, so a project
with no definitions and no warehouse still gets the static ones.
"""

from __future__ import annotations

import json
import math
import re
import shlex
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from konstruct.core.graph import RELATION_KINDS, Graph, Node

if TYPE_CHECKING:
    from konstruct.analysis.conflict_data import PairOnData
    from konstruct.analysis.conflicts import Conflict
    from konstruct.interfaces.layout import Style

LIMIT = 6
"""The most findings shown."""

CONFLICTS = 3
"""The most conflicts among them."""

KEY_LENGTH = 200
"""The longest name or key value taken from ``conflict_data.json`` or ``verify.json``."""

LARGEST = 10**18
"""The largest count or total taken from them: no check counts more, and a larger number would run
the line to hundreds of digits."""


@dataclass
class Finding:
    """One thing worth acting on, and the command that shows more."""

    kind: str
    """``conflict``, ``definition``, ``claim`` or ``reach``."""
    text: str
    """What was found."""
    action: str
    """What the command does, as a phrase (``compare them on the data``)."""
    command: str
    """The exact command to run next."""
    subjects: list[str] = field(default_factory=list)
    """The columns, relations or terms it is about, by label."""

    def line(self) -> str:
        """The finding in one line, with its command in backticks."""
        return f'{self.text}; {self.action}: `{self.command}`'

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class TopFindings:
    """The findings a build ends with, most useful first."""

    findings: list[Finding] = field(default_factory=list)
    """Up to three conflicts, then at most one of each other kind."""
    seconds: float = 0.0
    """Time taken."""
    notes: list[str] = field(default_factory=list)
    """What could not be read, such as a ``conflict_data.json`` written by a newer Konstruct."""

    def summary(self) -> str:
        count = len(self.findings)
        if not count:
            return 'Nothing in the graph stands out yet.'
        return f'{count} finding{"s" if count != 1 else ""} worth a look.'

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {'summary': self.summary(), 'findings': [f.to_dict() for f in self.findings]}
        if self.notes:
            data['notes'] = list(self.notes)
        return data

    def text(self, style: Style | None = None) -> str:
        """The result as the CLI prints it: plain text, as ``str()`` gives, or drawn for a
        terminal with ``style``."""
        from konstruct.interfaces.render import findings_lines, show

        return show(findings_lines, self, style)

    def __str__(self) -> str:
        return self.text()


def top_findings(
    graph: Graph, out_dir: Path | None = None, root: Path | None = None, *, limit: int = LIMIT
) -> TopFindings:
    """The findings worth acting on first (see the module docstring). ``out_dir`` is where earlier
    runs wrote ``verify.json`` and ``conflict_data.json``; ``root``, the project, for its seeds and
    ``konstruct.lock``."""
    started = time.perf_counter()
    notes: list[str] = []

    def made(kind: str, make: Callable[[], list[Finding] | Finding | None]) -> list[Finding]:
        # The findings explain a build; one that cannot be made is a note, never a failed build.
        try:
            found = make()
        except Exception as error:
            notes.append(f'the {kind} findings could not be made: {type(error).__name__}: {error}')
            return []
        return found if isinstance(found, list) else [found] if found is not None else []

    found = [
        # Each reads the graph's record of its sources inside made(), as a graph.json written by
        # hand can hold anything there.
        *made(
            'conflict', lambda: _conflicts(graph, out_dir, _has_warehouse(graph), _seeds(graph, root), notes)
        ),
        *made('definition', lambda: _definition(graph, root)),
        *made('claim', lambda: _claim(graph, out_dir, _has_warehouse(graph))),
        *made('reach', lambda: _reach(graph)),
    ]
    result = TopFindings(found[:limit], notes=notes)
    result.seconds = time.perf_counter() - started
    return result


def _read(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, RecursionError):  # RecursionError: nesting deeper than json reads
        return None
    return data if isinstance(data, dict) else None


def _has_warehouse(graph: Graph) -> bool:
    """Whether ``konstruct verify`` has data to rerun the models on: a DuckDB or Postgres
    warehouse the build read that holds relations. Seeds alone are not enough: verify reruns each
    model on its own inputs, which only a built warehouse holds."""
    for source in graph.meta.get('sources', []):
        if source.get('input') not in ('duckdb', 'postgres', 'postgresql') or 'status' in source:
            continue
        held = re.match(r'(\d+) relation', str(source.get('detail', '')))
        if held is None or int(held[1]) > 0:
            return True
    return False


def _seeds(graph: Graph, root: Path | None) -> frozenset[str]:
    """The relations ``verify --conflicts`` can read from the project's seed files."""
    if root is None:
        return frozenset()
    from konstruct.analysis.seedfiles import seed_files

    return frozenset(seed_files(graph, root))


def _runs(graph: Graph, conflict: Conflict, built: bool, seeds: frozenset[str]) -> bool:
    """Whether ``verify --conflicts`` can run the pair, without running anything: each side is
    built by SQL (a dbt model or a SQL file), and each relation its lineage starts from is in the warehouse the
    build read or is a seed."""
    for side in (conflict.a, conflict.b):
        column = graph.nodes.get(side.column)
        owner = graph.parent_of(column) if column is not None else None
        if owner is None or not (owner.path or '').endswith('.sql'):
            return False
        if built:
            continue
        stack, seen = [side.column], {side.column}
        while stack:
            current = stack.pop()
            inputs = graph.out_edges(current, 'derived_from')
            if not inputs:
                parent = graph.parent_of(graph.nodes[current])
                if parent is None or parent.relation not in seeds:
                    return False
            for edge in inputs:
                if edge.target not in seen:
                    seen.add(edge.target)
                    stack.append(edge.target)
    return True


def _count(value: Any) -> bool:
    """Whether ``value``, read from a file an earlier run wrote, is a count a run can give."""
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= LARGEST


def _phrase(count: int, noun: str) -> str:
    return f'{count} {noun}' + ('' if count == 1 else 's')


# conflict


def _conflicts(
    graph: Graph, out_dir: Path | None, built: bool, seeds: frozenset[str], notes: list[str]
) -> list[Finding]:
    """The conflicts the project's own terms declare (:func:`_declared`), the most consequential
    first, each with a plain explanation of the difference and, once ``verify --conflicts`` has
    run it, how the two differ on the data. A pair only a name links is never one of them."""
    from konstruct.analysis.conflicts import find_conflicts

    declared = [(c, said) for c in find_conflicts(graph).conflicts if (said := _declared(graph, c))]
    recorded = _recorded(out_dir, notes) if declared else {}
    found = []
    for index, (conflict, said) in enumerate(declared[:CONFLICTS]):
        subjects = [conflict.a.label, conflict.b.label]
        text = f'{said}: {conflict.explanation()}'
        rest = len(declared) - CONFLICTS
        more = f' ({_phrase(rest, "more such pair")})' if rest > 0 and index == CONFLICTS - 1 else ''
        entry = recorded.get((conflict.a.column, conflict.b.column, conflict.a.composed, conflict.b.composed))
        run = _pair(graph, entry, conflict) if entry is not None else None
        if run is not None and run.skipped is None and run.agrees:
            found.append(
                Finding(
                    'conflict',
                    f'{text}; they agree on the data{more}',
                    'see every difference',
                    'konstruct conflicts',
                    subjects,
                )
            )
        elif run is not None and run.skipped is None:
            shown = run.summary().split(': ', 1)[-1]
            command = run.next_command()
            action = 'see the source rows behind the row that differs most' if command else 'see the rows'
            found.append(
                Finding(
                    'conflict',
                    f'{text}; on the data, {shown}{more}',
                    action,
                    command or 'konstruct verify --conflicts',
                    subjects,
                )
            )
        elif entry is None and _runs(graph, conflict, built, seeds):
            found.append(
                Finding(
                    'conflict',
                    text + more,
                    'compare them on the data',
                    'konstruct verify --conflicts',
                    subjects,
                )
            )
        else:
            # Not runnable on the data, or the last run could not run it: the SQL says the rest.
            found.append(
                Finding(
                    'conflict',
                    text + more,
                    'see every difference and its file',
                    'konstruct conflicts',
                    subjects,
                )
            )
    return found


def _terms(graph: Graph, column_id: str) -> dict[str, Node]:
    """The project's own business terms bound to a column as holding them, by name: from
    definitions.yml or a configured vocabulary, not a column a metric reads, nor an ontology
    pack's binding by table names."""
    found = {}
    for edge in graph.in_edges(column_id, 'defines'):
        term = graph.nodes[edge.source]
        if edge.attrs.get('role') != 'input' and not term.attrs.get('registry'):
            found[term.name] = term
    return found


def _declared(graph: Graph, conflict: Conflict) -> str | None:
    """What the project says of the pair, as the start of a sentence, when it says the two hold
    one business term: both are bound to it, or one is and the other, bound to no term of its
    own and named like it or one of its synonyms, is a likely measure conflict with it
    (``confident``). None when only a name links them: a roll-up, a running total, a subset or a
    count named for a synonym shares the name and differs on purpose."""
    a, b = conflict.a, conflict.b
    mine, theirs = _terms(graph, a.column), _terms(graph, b.column)
    both = sorted(mine.keys() & theirs.keys())
    if both:
        term = mine[both[0]]
        return f"{a.label} and {b.label} are both '{term.name}' in {term.path}, but compute it two ways"
    if conflict.kind != 'measure' or not conflict.confident:
        return None
    if not conflict.reason.startswith(('are named like the business term', 'are bound to the business term')):
        return None
    for bound, other, terms, others in ((a, b, mine, theirs), (b, a, theirs, mine)):
        named = terms.get(conflict.idea)
        # A column bound to a term of its own is that term, which the project says is another number.
        if named is not None and not others and other.composed is not None:
            return (
                f"{bound.label} is '{named.name}' in {named.path}, and {other.label}, named like it, "
                'computes it another way'
            )
    return None


_TEXT = ('idea', 'a', 'b', 'a_column', 'b_column')
_OPTIONAL_TEXT = ('a_formula', 'b_formula', 'key', 'skipped')
_COUNTS = ('rows_a', 'rows_b', 'matched', 'differ', 'only_a', 'only_b')
_TOTALS = ('total_a', 'total_b')
_SCALARS = (str, int, float, bool, type(None))


def _recorded(out_dir: Path | None, notes: list[str]) -> dict[tuple[Any, ...], dict[str, Any]]:
    """The pairs the last ``verify --conflicts`` wrote, by their columns and formulas. A file this
    Konstruct cannot read (malformed, or written by a newer one) is left out, with a note: the
    findings never fail over it."""
    from konstruct.core.formats import FIELD, FIRST

    path = out_dir / 'conflict_data.json' if out_dir is not None else None
    if path is None or not path.is_file():
        return {}
    data = _read(path)
    version = data.get(FIELD, FIRST) if data is not None else None
    if data is None or not isinstance(data.get('pairs', []), list):
        notes.append(f'{path.name} could not be read, so the findings leave out what it recorded')
        return {}
    if not isinstance(version, int) or isinstance(version, bool) or version < 0:
        notes.append(f'{path.name} could not be read, so the findings leave out what it recorded')
        return {}
    if version > FIRST:
        notes.append(
            f'{path.name} was written by a newer Konstruct, so the findings leave out what it recorded'
        )
        return {}
    found: dict[tuple[Any, ...], dict[str, Any]] = {}
    for entry in data.get('pairs', []):
        if isinstance(entry, dict):
            key = tuple(entry.get(k) for k in ('a_column', 'b_column', 'a_formula', 'b_formula'))
            if all(isinstance(k, (str, type(None))) for k in key):
                found.setdefault(key, entry)
    return found


def _pair(graph: Graph, entry: dict[str, Any], conflict: Conflict) -> PairOnData | None:
    """The pair as the last ``verify --conflicts`` ran it, when every field read has its type and
    a value it can have; None otherwise. The file is the project's, so only its numbers are taken
    from it: the columns named are the conflict's own, the key must be a column of both sides'
    relations, and every command made from it quotes what it reads."""
    from konstruct.analysis.conflict_data import ExampleRow, PairOnData

    def count(value: Any) -> bool:
        return value is None or _count(value)

    def total(value: Any) -> bool:
        # A total may be negative (refunds), but never NaN, infinite or of hundreds of digits.
        return value is None or (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and abs(value) <= LARGEST
        )

    key = entry.get('key')
    if not (
        all(isinstance(entry.get(k, ''), str) for k in _TEXT)
        and all(isinstance(entry.get(k), (str, type(None))) for k in _OPTIONAL_TEXT)
        and (key is None or (len(key) <= KEY_LENGTH and _column_of_both(graph, conflict, key)))
        and all(count(entry.get(k)) for k in _COUNTS)
        and all(total(entry.get(k)) for k in _TOTALS)
        and isinstance(entry.get('additive', True), bool)
        and isinstance(entry.get('differences', []), list)
        and all(isinstance(d, str) for d in entry.get('differences', []))
        and isinstance(entry.get('examples', []), list)
    ):
        return None
    fields = set(PairOnData.__dataclass_fields__) - {'examples'}
    pair = PairOnData(**{k: v for k, v in entry.items() if k in fields})
    pair.a, pair.b = conflict.a.label, conflict.b.label
    family = _family(graph, conflict, key) if key is not None else None
    for example in entry.get('examples', []):
        if not isinstance(example, dict) or not all(
            isinstance(example.get(k), _SCALARS) for k in ('key', 'a', 'b')
        ):
            return None
        if not _key_value(example.get('key'), family):
            return None
        if not all(isinstance(example.get(k, True), bool) for k in ('in_a', 'in_b')):
            return None
        pair.examples.append(
            ExampleRow(
                example.get('key'),
                example.get('a'),
                example.get('b'),
                example.get('in_a', True),
                example.get('in_b', True),
            )
        )
    return pair


_INTEGERS = frozenset(
    'TINYINT SMALLINT INTEGER INT BIGINT HUGEINT UTINYINT USMALLINT UINTEGER UBIGINT UHUGEINT INT2 INT4 '
    'INT8 INT64 BYTEINT SERIAL BIGSERIAL'.split()
)
_NUMBERS = frozenset('FLOAT REAL DOUBLE DECIMAL NUMERIC NUMBER FLOAT4 FLOAT8 FLOAT64 BIGNUMERIC'.split())


def _family(graph: Graph, conflict: Conflict, name: str) -> str | None:
    """What values the key column holds, from the first type the graph records for it or a
    column it copies: ``integer``, ``number``, ``boolean`` or ``time``; None for text or a type
    not recorded."""
    for side in (conflict.a, conflict.b):
        column = graph.nodes.get(side.column)
        owner = graph.parent_of(column) if column is not None else None
        key = graph.column(owner.id, name) if owner is not None else None
        stack, seen = ([key], {key.id}) if key is not None else ([], set())
        while stack:
            node = stack.pop()
            if node.data_type:
                base = ' '.join(re.split(r'[(<]', node.data_type.upper(), maxsplit=1)[0].split())
                if base in _INTEGERS:
                    return 'integer'
                if base in _NUMBERS or base == 'DOUBLE PRECISION':
                    return 'number'
                if base in ('BOOLEAN', 'BOOL'):
                    return 'boolean'
                return 'time' if base.startswith(('DATE', 'TIME')) else None
            for edge in graph.out_edges(node.id, 'derived_from'):
                found = graph.nodes.get(edge.target)
                if found is not None and found.id not in seen:
                    seen.add(found.id)
                    stack.append(found)
    return None


def _key_value(value: Any, family: str | None) -> bool:
    """Whether ``value``, a key read from ``conflict_data.json``, can be a value of a key column
    of ``family`` (see :func:`_family`), and is short enough to print: text where the column
    holds numbers is no key of its rows."""
    import datetime as dt

    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return False
    if family == 'integer':
        return isinstance(value, int) and not isinstance(value, bool) and abs(value) <= LARGEST
    if family == 'number':
        return isinstance(value, (int, float)) and not isinstance(value, bool) and abs(value) <= LARGEST
    if family == 'boolean':
        return isinstance(value, bool)
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) > LARGEST:
        return False
    if len(str(value)) > KEY_LENGTH:
        return False
    if family == 'time':
        if not isinstance(value, str):
            return False
        for kind in (dt.datetime, dt.date, dt.time):
            try:
                kind.fromisoformat(value)
            except ValueError:
                continue
            return True
        return False
    return True


def _column_of_both(graph: Graph, conflict: Conflict, name: str) -> bool:
    """Whether ``name`` is a column of the relation of each side of the pair."""
    for side in (conflict.a, conflict.b):
        column = graph.nodes.get(side.column)
        owner = graph.parent_of(column) if column is not None else None
        if owner is None or graph.column(owner.id, name) is None:
            return False
    return True


# definition


def _definition(graph: Graph, root: Path | None) -> Finding | None:
    """An approved term naming a column nothing builds, or a pinned column that changed. When a
    dbt project was found but not read (it is not trusted, or dbt is not run and there is no
    manifest), the graph lacks its models, and every column they build would read as gone: there is
    no such finding then."""
    for source in graph.meta.get('sources', []):
        if isinstance(source, dict) and source.get('input') == 'dbt' and 'status' in source:
            return None
    for term in sorted((n for n in graph.nodes.values() if n.kind == 'term'), key=lambda n: n.name):
        if term.attrs.get('status') != 'approved':
            continue
        for ref in term.attrs.get('columns', []):
            found = [n for n in graph.resolve(str(ref)) if n.kind == 'column' or n.kind in RELATION_KINDS]
            if not found:
                text = f"the approved term '{term.name}' names {ref}, which no model or table builds now"
                command = f'konstruct define {shlex.quote(term.name)}'
                return Finding(
                    'definition', text, 'see its definition and bound columns', command, [term.name]
                )
    if root is not None and (root / 'konstruct.lock').is_file():
        from konstruct.analysis.lock import check

        try:
            locked = check(graph, root, history=False)
        except Exception:  # a lockfile this Konstruct cannot read is reported by `lock --check`
            return None
        if not locked.passed:
            text = locked.summary().rstrip('.')
            return Finding(
                'definition', text, 'see what changed and how to accept it', 'konstruct lock --check'
            )
    return None


# claim


def _listed(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """The objects of a list in a file an earlier run wrote; anything else in it is passed over."""
    items = data.get(key)
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def _claim(graph: Graph, out_dir: Path | None, data: bool) -> Finding | None:
    """verify.json is a file in the project, so nothing it says is printed as it is written: the
    model, the columns and the key or relationship it names must be the graph's, and are named by
    the graph's labels, each count must be one a check can give, and each sentence is made again
    from those. An entry that fails any of this is passed over."""
    verified = _read(out_dir / 'verify.json' if out_dir is not None else None)
    if verified is None:
        claimed = sum(1 for e in graph.edges if e.kind == 'derived_from' and not e.attrs.get('same_relation'))
        if not data or not claimed:
            return None
        text = f'the lineage claims {_phrase(claimed, "column edge")}, none yet checked against the data'
        return Finding('claim', text, 'check each one by rerunning the models', 'konstruct verify')
    for model in _listed(verified, 'models'):
        node_id = model.get('node_id')
        node = graph.nodes.get(node_id) if isinstance(node_id, str) else None
        if node is None:
            continue
        unexplained = [
            (output, source)
            for u in _listed(model, 'unexplained')
            if (output := _named(graph, node, u.get('output'))) is not None
            and (source := _column_at(graph, u.get('input'))) is not None
        ]
        if unexplained:
            label = graph.label(node)
            output, source = unexplained[0]
            more = f' (and {len(unexplained) - 1} more)' if len(unexplained) > 1 else ''
            text = (
                f'verify saw {graph.label(output)} change with {graph.label(source)}, which the '
                f'lineage does not claim{more}'
            )
            command = f'konstruct verify -m {shlex.quote(label)}'
            return Finding('claim', text, 'see the gap in the lineage', command, [label])
    for kind in ('keys', 'links'):
        for check in _listed(verified, kind):
            detail = _failed(graph, kind, check) if check.get('state') == 'failed' else None
            if detail is not None:
                text = f'a claim the ontology makes fails on the data: {detail}'
                return Finding(
                    'claim', text, 'see the checks of every key and relationship', 'konstruct verify'
                )
    return None


def _named(graph: Graph, relation: Node, name: Any) -> Node | None:
    """The column ``name`` of ``relation``, when it is one."""
    if not isinstance(name, str) or len(name) > KEY_LENGTH:
        return None
    return graph.column(relation.id, name)


def _column_at(graph: Graph, reference: Any) -> Node | None:
    """The column a reference names (``shop.raw.orders.status``, ``orders.status``), when it names
    one in the graph."""
    if not isinstance(reference, str) or len(reference) > KEY_LENGTH:
        return None
    return next((n for n in graph.resolve(reference) if n.kind == 'column'), None)


def _failed(graph: Graph, kind: str, check: dict[str, Any]) -> str | None:
    """The sentence of a failed check of a key or a relationship, made again from its columns and
    counts; None when they are not the graph's columns and counts a check can give, or when the
    check found nothing a count says."""
    from konstruct.analysis.integrity import KeyCheck, LinkCheck

    if kind == 'keys':
        column = _column_at(graph, check.get('column'))
        rows, nulls, duplicates = (check.get(k, 0) for k in ('rows', 'nulls', 'duplicates'))
        if (
            column is None
            or not all(_count(v) for v in (rows, nulls, duplicates))
            or not (nulls or duplicates)
        ):
            return None
        return KeyCheck('', graph.label(column), 'failed', rows, nulls, duplicates).detail
    via = check.get('via')
    sides = via.split('=') if isinstance(via, str) else []
    columns = [_column_at(graph, side.strip()) for side in sides]
    values, orphans = check.get('values', 0), check.get('orphans', 0)
    if len(columns) != 2 or None in columns or not (_count(values) and _count(orphans)) or not orphans:
        return None
    joined = ' = '.join(graph.label(c) for c in columns if c is not None)
    return LinkCheck('', '', joined, 'failed', values, orphans).detail


# reach


def _own(graph: Graph) -> Callable[[Node], bool]:
    """Whether a relation is the project's own: not a model, seed or source of a dbt package it
    installs (elementary, dbt_project_evaluator), whose columns its users do not change."""
    projects = {
        str(s['name'])
        for s in graph.meta.get('sources', [])
        if isinstance(s, dict) and s.get('input') == 'dbt' and s.get('name')
    }

    def own(relation: Node) -> bool:
        package = relation.attrs.get('package')
        return not projects or not isinstance(package, str) or package in projects

    return own


def _reach(graph: Graph) -> Finding | None:
    """The project's own column whose change reaches the most exposures, then the most of the
    project's own columns. A column's change reaches everything a change to a column it reads
    reaches, so the most is reached from a column that reads none of the project's own: only those
    are walked, the ones upstream of the most exposures and read the most first, so that of two that
    reach as much the first is named."""
    own = _own(graph)

    def owned(node: Node | None) -> bool:
        owner = graph.parent_of(node) if node is not None else None
        return owner is not None and own(owner)

    exposed: Counter[str] = Counter()
    for exposure in (n for n in graph.nodes.values() if n.kind == 'exposure'):
        exposed.update({s.node.id for s in graph.trace(exposure) if s.node.kind in RELATION_KINDS})
    roots = sorted(
        (
            (exposed[n.parent or ''], len(readers), n.id)
            for n in graph.nodes.values()
            if n.kind == 'column'
            and (readers := graph.in_edges(n.id, 'derived_from'))
            and owned(n)
            and not any(owned(graph.nodes.get(e.target)) for e in graph.out_edges(n.id, 'derived_from'))
        ),
        key=lambda item: (-item[0], -item[1], item[2]),
    )
    best: tuple[list[str], list[Node], Node] | None = None
    for _, _, column_id in roots:
        column = graph.nodes[column_id]
        walk = graph.impact(column)
        exposures = sorted({s.node.name for s in walk if s.node.kind == 'exposure'})
        downstream = [
            s.node for s in walk if s.node.kind == 'column' and s.node.id != column_id and owned(s.node)
        ]
        if best is None or (len(exposures), len(downstream)) > (len(best[0]), len(best[1])):
            best = (exposures, downstream, column)
    if best is None or len(best[1]) < 2:
        return None
    exposures, downstream, column = best
    label = graph.label(column)
    parts = []
    if exposures:
        shown = ', '.join(exposures[:3]) + (f' and {len(exposures) - 3} more' if len(exposures) > 3 else '')
        parts.append(f'{_phrase(len(exposures), "exposure")} ({shown})')
    relations = {c.parent for c in downstream}
    parts.append(f'{_phrase(len(downstream), "column")} in {_phrase(len(relations), "relation")}')
    text = f'a change to {label} reaches {" and ".join(parts)}'
    return Finding('reach', text, 'see everything downstream', f'konstruct impact {label}', [label])
