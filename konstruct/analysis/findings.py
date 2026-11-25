"""The findings `konstruct build` ends with: the few things in the graph most worth acting on.

A graph answers questions, but a first build has asked none yet. These findings are the answers
most likely to matter, each one line that says what was found and the exact command that shows
more, at most one of each kind, in this order:

* ``conflict``: the most consequential pair of columns that the project itself says hold one
  business term and that compute it differently (:mod:`konstruct.analysis.conflicts`): both bound
  to the term (by definitions.yml or a vocabulary), or one bound to it and the other, bound to no
  term of its own and named like it, compared with it; with how they differ on the data when the
  last ``konstruct verify --conflicts`` ran this same pair. A pair that only a name links is never
  one: in public projects those are rarely two definitions of one number, and most differ on
  purpose, by grain or segment;
* ``definition``: an approved business term the code no longer matches: a column its definition
  names that no model builds any more, or a column ``konstruct.lock`` pins that changed; none when
  a dbt project was found but not read, whose models the graph then lacks;
* ``claim``: a claim of the graph the data contradicted when ``konstruct verify`` last ran (an
  influence the lineage does not record, a key that repeats, a relationship with orphans), or,
  where there is data to check it against and verify has not run, that the lineage is unchecked;
* ``reach``: the project's own column (not a package's, such as elementary's) whose change
  reaches the most exposures, and then the most of the project's own columns.

They read only the graph and what earlier runs wrote beside it, never the warehouse, so a project
with no definitions and no warehouse still gets the static ones.
"""

from __future__ import annotations

import json
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

LIMIT = 4
"""The most findings shown."""

KEY_LENGTH = 200
"""The longest name taken from ``verify.json``."""

LARGEST = 10**18
"""The largest count taken from it: no check counts more, and a larger number would run the line
to hundreds of digits."""


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
    """At most one of each kind."""
    seconds: float = 0.0
    """Time taken."""

    def summary(self) -> str:
        count = len(self.findings)
        if not count:
            return 'Nothing in the graph stands out yet.'
        return f'{count} finding{"s" if count != 1 else ""} worth a look.'

    def to_dict(self) -> dict[str, Any]:
        return {'summary': self.summary(), 'findings': [f.to_dict() for f in self.findings]}

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
    built = _has_warehouse(graph)
    found = [
        _conflict(graph, out_dir, built or _has_seeds(graph, root)),
        _definition(graph, root),
        _claim(graph, out_dir, built),
        _reach(graph),
    ]
    result = TopFindings([f for f in found if f is not None][:limit])
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


def _has_seeds(graph: Graph, root: Path | None) -> bool:
    """Whether ``verify --conflicts`` can run on the project's seeds."""
    if root is None:
        return False
    from konstruct.analysis.seedfiles import seed_files

    return bool(seed_files(graph, root))


def _count(value: Any) -> bool:
    """Whether ``value``, read from a file an earlier run wrote, is a count a run can give."""
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= LARGEST


def _phrase(count: int, noun: str) -> str:
    return f'{count} {noun}' + ('' if count == 1 else 's')


# conflict


def _conflict(graph: Graph, out_dir: Path | None, data: bool) -> Finding | None:
    """The most consequential conflict the project's own terms declare (:func:`_declared`). A pair
    only a name links is never one."""
    from konstruct.analysis.conflicts import find_conflicts

    declared = [(c, said) for c in find_conflicts(graph).conflicts if (said := _declared(graph, c))]
    if not declared:
        return None
    top, said = declared[0]
    subjects = [top.a.label, top.b.label]
    more = f' ({_phrase(len(declared) - 1, "more such pair")})' if len(declared) > 1 else ''
    run = _recorded(top, out_dir)
    if run is not None:
        if run.agrees:
            text = f'{said}, but agree on the data{more}'
            return Finding(
                'conflict', text, 'see how they differ in the SQL', 'konstruct conflicts', subjects
            )
        found = run.summary().split(': ', 1)[-1]
        text = f'{said}, and differ on the data: {found}{more}'
        command = run.next_command()
        if command is not None:
            return Finding(
                'conflict', text, 'see the source rows behind the row that differs most', command, subjects
            )
        return Finding('conflict', text, 'see the rows', 'konstruct verify --conflicts', subjects)
    text = f'{said} ({_differs(top)}){more}'
    if data:
        return Finding('conflict', text, 'compare them on the data', 'konstruct verify --conflicts', subjects)
    return Finding('conflict', text, 'see what differs and where', 'konstruct conflicts', subjects)


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
    own and named like it, has a formula compared with the bound one's. None when only a name
    links them."""
    a, b = conflict.a, conflict.b
    mine, theirs = _terms(graph, a.column), _terms(graph, b.column)
    both = sorted(mine.keys() & theirs.keys())
    if both:
        term = mine[both[0]]
        return f"{a.label} and {b.label} are both '{term.name}' in {term.path}, but compute it two ways"
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


def _differs(conflict: Conflict) -> str:
    """The first difference between the two, as a phrase."""
    difference = conflict.differences[0]
    who = ''
    if difference.side:
        who = f' only in {conflict.a.label if difference.side == "a" else conflict.b.label}'
    rest = len(conflict.differences) - 1
    return f'a {difference.kind}{who}' + (
        f', and {rest} more difference{"s" if rest != 1 else ""}' if rest else ''
    )


def _recorded(conflict: Conflict, out_dir: Path | None) -> PairOnData | None:
    """The pair as the last ``verify --conflicts`` ran it, if it ran these same two formulas."""
    from konstruct.analysis.conflict_data import ExampleRow, PairOnData

    data = _read(out_dir / 'conflict_data.json' if out_dir is not None else None)
    for entry in (data or {}).get('pairs', []):
        if not isinstance(entry, dict) or entry.get('skipped'):
            continue
        same = (entry.get('a_column'), entry.get('b_column')) == (conflict.a.column, conflict.b.column)
        formulas = (entry.get('a_formula'), entry.get('b_formula')) == (
            conflict.a.composed,
            conflict.b.composed,
        )
        if not (same and formulas):
            continue
        fields = set(PairOnData.__dataclass_fields__) - {'examples'}
        pair = PairOnData(**{k: v for k, v in entry.items() if k in fields})
        # The file is the project's, so only its numbers are taken from it: the columns named are
        # the conflict's own, and every command made from it quotes what it reads.
        pair.a, pair.b = conflict.a.label, conflict.b.label
        rows = set(ExampleRow.__dataclass_fields__)
        pair.examples = [
            ExampleRow(**{k: v for k, v in e.items() if k in rows}) for e in entry.get('examples', [])
        ]
        return pair
    return None


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
