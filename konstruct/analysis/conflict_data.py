"""`konstruct verify --conflicts`: whether two definitions of one idea disagree on the data.

:mod:`konstruct.analysis.conflicts` finds columns that appear to measure the same thing but are
computed differently, and says how their formulas, filters and joins differ. That is a difference
in the SQL; this module says what it does to the numbers. For each conflicting pair it runs both
columns' SQL on the same rows and compares them:

1. Each side's model is composed down to source tables, every model it reads inlined, the way
   :mod:`konstruct.analysis.why` composes one (:class:`~konstruct.analysis.why._Composer`). Every
   model's SQL must be a single query that only reads
   (:func:`~konstruct.analysis.verify.read_only_query`) before any of it runs.
2. Each source table is copied once, up to a number of rows, into verify's sandbox (the DuckDB
   warehouse attached read-only, a Postgres transaction that is rolled back, or a sample of a remote
   warehouse in memory), and both sides read the same copies, so a difference is the definitions'
   alone. A dbt seed the warehouse does not hold is read from its CSV file instead, so a project
   with seeds and no built warehouse can still be checked.
3. The two outputs are matched by a column that identifies the rows of both: a key of either
   relation in the derived ontology, or a column named like an identifier, present in both outputs
   and unique and never null in both. By that key the run counts the rows whose values differ and
   the rows only one side has, gives both totals, and lists the rows that differ most. With no such
   column, only the row counts and the totals are compared.

Nothing is written to the warehouse.
"""

from __future__ import annotations

import math
import re
import shlex
import shutil
import tempfile
import time
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from konstruct.analysis.conflicts import Conflict, Conflicts, Difference, Side, find_conflicts
from konstruct.analysis.effect import _count, _number, _numeric, _same, _value
from konstruct.analysis.query import QueryError
from konstruct.analysis.seedfiles import SeedFile, load_seeds, seed_database, seed_files
from konstruct.analysis.verify import (
    DuckDBEngine,
    Engine,
    _literal,
    _lock,
    _sandbox,
    candidates,
    engine_for,
    quote,
)
from konstruct.core.graph import Graph, Node

if TYPE_CHECKING:
    from konstruct.analysis.sensitivity import Settings
    from konstruct.interfaces.layout import Style

Output = tuple[list[str], list[tuple[Any, ...]]]

EXAMPLES = 5
"""The most rows listed for each pair."""


@dataclass
class ExampleRow:
    """One row on which the two sides differ."""

    key: Any
    """The value of the key column."""
    a: Any
    """The first side's value, or None when it has no row of the key."""
    b: Any
    """The second side's value, or None when it has no row of the key."""
    in_a: bool = True
    """Whether the first side has a row of the key."""
    in_b: bool = True
    """Whether the second side has a row of the key."""

    def to_dict(self) -> dict[str, Any]:
        from konstruct.analysis.why import _jsonable

        return {
            'key': _jsonable(self.key),
            'a': _jsonable(self.a),
            'b': _jsonable(self.b),
            'in_a': self.in_a,
            'in_b': self.in_b,
        }


@dataclass
class PairOnData:
    """One conflicting pair, run on the same rows."""

    idea: str
    """What the two columns appear to measure."""
    a: str
    """The first column, by label."""
    b: str
    """The second column, by label."""
    a_column: str = ''
    """The first column's id."""
    b_column: str = ''
    """The second column's id."""
    a_formula: str | None = None
    """The first column's formula in terms of source columns, as ``konstruct conflicts`` compared it."""
    b_formula: str | None = None
    """The second column's."""
    differences: list[str] = field(default_factory=list)
    """What ``konstruct conflicts`` found differs in their SQL, one line each."""
    key: str | None = None
    """The column the rows were matched by; None when no column identifies the rows of both."""
    rows_a: int | None = None
    """Rows the first side's model returns."""
    rows_b: int | None = None
    """Rows the second side's model returns."""
    matched: int | None = None
    """Keys both sides have."""
    differ: int | None = None
    """Of those, the keys whose two values differ."""
    only_a: int | None = None
    """Keys only the first side has."""
    only_b: int | None = None
    """Keys only the second side has."""
    total_a: float | None = None
    """The sum of the first side's values, when they are numbers."""
    total_b: float | None = None
    """The sum of the second side's values."""
    examples: list[ExampleRow] = field(default_factory=list)
    """Up to five rows that differ, the largest difference first."""
    skipped: str | None = None
    """Why the pair was not run, when it was not."""
    additive: bool = True
    """Whether both sides add up over rows (a sum, a count or an amount per row), so that their
    totals mean something. An average, a rate, a price or a maximum does not: its totals are not
    compared."""

    @property
    def agrees(self) -> bool | None:
        """Whether nothing compared differs: every key and value when there is a key, else the
        totals of an additive measure. None when the pair was not run, or when it has neither a key
        nor totals to compare."""
        if self.skipped is not None:
            return None
        if self.key is not None:
            same = not self.additive or _same(self.total_a, self.total_b)
            return not (self.differ or self.only_a or self.only_b) and same
        if not self.additive:
            return None
        return _same(self.total_a, self.total_b)

    def to_dict(self) -> dict[str, Any]:
        data = {k: v for k, v in self.__dict__.items() if k != 'examples'}
        data['agrees'] = self.agrees
        data['examples'] = [e.to_dict() for e in self.examples]
        return data

    def totals(self) -> str | None:
        """The two totals, and how far apart they are, as text."""
        if self.total_a is None or self.total_b is None or not self.additive:
            return None
        text = f'totals {_number(self.total_a)} and {_number(self.total_b)}'
        if not _same(self.total_a, self.total_b):
            text += f', {_number(abs(self.total_b - self.total_a))} apart'
            if self.total_a:
                text += f' ({abs(self.total_b - self.total_a) / abs(self.total_a) * 100:.1f}% of the first)'
        return text

    def summary(self) -> str:
        """What the run found, in one sentence."""
        if self.skipped is not None:
            return f'not run: {self.skipped}'
        totals = self.totals()
        if self.key is None and not self.additive:
            rows = f'{_count(self.rows_a or 0, "row")} against {self.rows_b or 0}'
            return f'not comparable without a shared key: no column identifies the rows of both ({rows}), and the values do not add up over rows'
        if self.key is None:
            rows = f'{_count(self.rows_a or 0, "row")} against {self.rows_b or 0}'
            found = f'no column identifies the rows of both, so only the totals are compared: {rows}'
            return f'{found}; {totals or "the values are not numbers"}'
        if self.agrees:
            text = f'{self.a} and {self.b} agree on every one of the {self.matched} {self.key} values'
            if self.total_a is None or not self.additive:
                return text
            return text + f', with the total {_number(self.total_a)}'
        parts = []
        if self.differ:
            parts.append(
                f'they give different values for {self.differ} of the {self.matched} {self.key} values both have'
            )
        for count, side in ((self.only_a, self.a), (self.only_b, self.b)):
            if count:
                parts.append(f'{_count(count, "row")} of {self.key} only in {side}')
        if totals:
            parts.append(totals)
        return f'{self.a} and {self.b} differ: ' + '; '.join(parts)

    def next_command(self) -> str | None:
        """The command that lists the source rows behind the row that differs most."""
        if self.key is None or not self.examples:
            return None
        top = self.examples[0]
        # The side without the row, whose rows left out say which filter or join removed it.
        column = self.b if top.in_a and not top.in_b else self.a
        where = shlex.quote(f'{self.key}={top.key}')
        return f'konstruct verify --why {shlex.quote(str(column))} --where {where}'


@dataclass
class ConflictsOnData:
    """``konstruct verify --conflicts``: each conflicting pair, run on the same rows."""

    warehouse: str
    """Where the pairs ran: the warehouse, the seeds read from their CSV files, or both."""
    max_rows: int
    """The most rows copied from any source."""
    pairs: list[PairOnData] = field(default_factory=list)
    """One result per conflicting pair, most consequential first."""
    conflicts: int = 0
    """Conflicting pairs that could be run (the measure conflicts, or every pair with ``--all``);
    more than ``pairs`` when the run was limited."""
    sources: list[str] = field(default_factory=list)
    """The source tables copied, by relation key: each once, for every pair that reads it."""
    sampled: bool = False
    """Whether a copy reached the row limit, so the results may be for a sample."""
    seconds: float = 0.0
    """Time taken."""

    def counts(self) -> dict[str, int]:
        run = [p for p in self.pairs if p.skipped is None]
        agree = sum(1 for p in run if p.agrees)
        return {'disagree': len(run) - agree, 'agree': agree, 'skipped': len(self.pairs) - len(run)}

    def summary(self) -> str:
        if not self.pairs:
            return 'No conflicting definitions to run on the data.'
        counts = self.counts()
        total = len(self.pairs)
        where = f' on {self.warehouse}' if self.warehouse else ''
        limited = f' (of {self.conflicts})' if self.conflicts > total else ''
        return (
            f'{total} conflicting pair{"s" if total != 1 else ""}{limited} run{where}: '
            f'{counts["disagree"]} disagree{"s" if counts["disagree"] == 1 else ""} on the data, '
            f'{counts["agree"]} agree{"s" if counts["agree"] == 1 else ""}, {counts["skipped"]} not run.'
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            'summary': self.summary(),
            'warehouse': self.warehouse,
            'max_rows': self.max_rows,
            'conflicts': self.conflicts,
            'sources': self.sources,
            'sampled': self.sampled,
            'seconds': round(self.seconds, 3),
            'counts': self.counts(),
            'pairs': [p.to_dict() for p in self.pairs],
        }

    def text(self, style: Style | None = None) -> str:
        """The result as the CLI prints it: plain text, as ``str()`` gives, or drawn for a
        terminal with ``style``."""
        from konstruct.interfaces.render import conflict_data_lines, show

        return show(conflict_data_lines, self, style)

    def __str__(self) -> str:
        return self.text()


# Seeds


SEED_MEMORY = '2GB'
"""The most memory DuckDB may use while it reads seeds and runs pairs on them."""


class SeedEngine(DuckDBEngine):
    """An in-memory DuckDB with the warehouse file, when there is one, attached read-only, and each
    dbt seed it does not hold read from its CSV file through :mod:`konstruct.analysis.seedfiles`,
    which never hands DuckDB a path from the project. The seeds are read before the sandbox is
    locked, so the models' SQL can read no file itself."""

    def __init__(
        self,
        warehouse: Path | None,
        root: Path,
        seeds: Mapping[str, SeedFile],
        *,
        memory_limit: str | None = None,
    ) -> None:
        import duckdb

        self.con = duckdb.connect()
        self.database = ''
        if memory_limit is not None:
            self.con.execute(f'SET memory_limit = {_literal(memory_limit)}')
        # External access is off before the warehouse is attached, with only the warehouse and the
        # private database the seeds are read into allowed.
        private = Path(tempfile.mkdtemp(prefix='konstruct-seeds-')).resolve()
        _sandbox(self.con, [*([warehouse] if warehouse is not None else []), seed_database(private)])
        if warehouse is not None:
            self.con.execute(f'ATTACH {_literal(str(warehouse))} (READ_ONLY)')
            row = self.con.execute(
                'SELECT database_name FROM duckdb_databases() WHERE path = ?', [str(warehouse.resolve())]
            ).fetchone()
            self.database = (row[0] if row else warehouse.stem).lower()
        wanted = {key: seed for key, seed in seeds.items() if not self._held(key)}
        try:
            self.seeds, self.skipped = load_seeds(
                self.con, root, wanted, memory_limit=memory_limit, directory=private
            )
        finally:
            shutil.rmtree(private, ignore_errors=True)
        names = sorted(key.rsplit('.', 1)[-1] for key in self.seeds)
        one = len(names) == 1
        read = f'the seed{"" if one else "s"} {", ".join(names)}'
        files = 'its CSV file' if one else 'their CSV files'
        if warehouse is None:
            self.label = f'{read}, read from {files}' if names else 'no seed that could be read'
        elif names:
            self.label = f'{warehouse.name}, with {read} read from {files}'
        else:
            self.label = str(warehouse)
        _lock(self.con)

    def _held(self, key: str) -> bool:
        """Whether the warehouse has the relation ``key``."""
        parts = key.lower().split('.')
        if not self.database or len(parts) != 3 or parts[0] != self.database:
            return False
        row = self.con.execute(
            'SELECT count(*) FROM information_schema.tables WHERE lower(table_catalog) = ? '
            'AND lower(table_schema) = ? AND lower(table_name) = ?',
            parts,
        ).fetchone()
        return bool(row and row[0])

    def table(self, key: str) -> str:
        if key in self.seeds:
            return self.seeds[key]
        if not self.database:
            raise LookupError(f'input {key} is not a seed, and no warehouse was found')
        return super().table(key)


# Comparing


def compare(
    a: Output, b: Output, key: str | None, a_name: str, b_name: str, *, limit: int = EXAMPLES
) -> PairOnData:
    """The two outputs compared: by ``key`` when it is given (a column of both, unique in each),
    else by their row counts and totals. Values are the same by the rule ``verify --effect`` uses:
    1 and 1.0 are one number, and NULL is the same as NULL."""
    names_a, rows_a = a
    names_b, rows_b = b
    value_a, value_b = names_a.index(a_name), names_b.index(b_name)
    found = PairOnData('', '', '', key=key, rows_a=len(rows_a), rows_b=len(rows_b))
    found.total_a = _numeric([r[value_a] for r in rows_a])
    found.total_b = _numeric([r[value_b] for r in rows_b])
    if key is None:
        return found
    key_a, key_b = names_a.index(key), names_b.index(key)
    by_a = {_value(r[key_a]): r for r in rows_a}
    by_b = {_value(r[key_b]): r for r in rows_b}
    shared = by_a.keys() & by_b.keys()
    found.matched = len(shared)
    found.only_a = len(by_a.keys() - shared)
    found.only_b = len(by_b.keys() - shared)
    differing: list[ExampleRow] = []
    for k in by_a.keys() | by_b.keys():
        row_a, row_b = by_a.get(k), by_b.get(k)
        if row_a is not None and row_b is not None and _same(row_a[value_a], row_b[value_b]):
            continue
        shown = row_a[key_a] if row_a is not None else by_b[k][key_b]
        differing.append(
            ExampleRow(
                key=shown,
                a=row_a[value_a] if row_a is not None else None,
                b=row_b[value_b] if row_b is not None else None,
                in_a=row_a is not None,
                in_b=row_b is not None,
            )
        )
    found.differ = sum(1 for e in differing if e.in_a and e.in_b)
    differing.sort(key=lambda e: (-_gap(e), repr(e.key)))
    found.examples = differing[:limit]
    return found


_NOT_ADDED = re.compile(
    r'(?:^|_)(?:avg|average|mean|median|rate|ratio|pct|percent|percentage|share|price|prices|balance|'
    r'balances|score|rating|per|unit|min|max|minimum|maximum)(?:_|$)',
    re.IGNORECASE,
)


def _additive(side: Side, graph: Graph) -> bool:
    """Whether a side's values add up over rows: a sum or a count, or an amount per row that is
    not an average, a ratio, a rate or a price. A division by a constant converts a unit and adds
    up; a division by anything else is a ratio."""
    import sqlglot
    from sqlglot import exp

    from konstruct.analysis.measures import _unwrap
    from konstruct.core.sql import PARSE_ERRORS

    dialect = graph.meta.get('dialect')
    try:
        tree = sqlglot.parse_one(
            side.composed or side.expression or '', read=dialect if isinstance(dialect, str) else None
        )
    except PARSE_ERRORS:
        return False
    node = _unwrap(tree)
    if isinstance(node, (exp.Sum, exp.Count, exp.CountIf)):
        return True
    if isinstance(node, exp.AggFunc):
        return False
    if isinstance(node, exp.Div) and not isinstance(_unwrap(node.expression), exp.Literal):
        return False
    return not _NOT_ADDED.search(graph.nodes[side.column].name)


def _gap(example: ExampleRow) -> float:
    """How far apart the two values are, a missing row or a NULL counting as zero; 0 when either
    is not a number, so those rows come after the numbers."""
    values = []
    for value, present in ((example.a, example.in_a), (example.b, example.in_b)):
        if not present or value is None:
            values.append(0.0)
        elif isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
            values.append(float(value))
        else:
            return 0.0
    gap = abs(values[0] - values[1])
    return gap if math.isfinite(gap) else 0.0


def _key_columns(graph: Graph) -> dict[str, list[str]]:
    """Each relation's key columns in the derived ontology, by the relation's label."""
    from konstruct.semantics.ontology import derive

    keys: dict[str, list[str]] = {}
    for entity in derive(graph).entities:
        for key in entity.keys:
            relation, _, column = key.rpartition('.')
            keys.setdefault(relation, []).append(column.lower())
    return keys


def _unique(rows: list[tuple[Any, ...]], index: int) -> bool:
    values = [r[index] for r in rows]
    return None not in values and len({_value(v) for v in values}) == len(values)


# Running


def conflicts_on_data(
    graph: Graph,
    root: Path,
    warehouse: str | Path | Engine | None,
    *,
    conflicts: Conflicts | None = None,
    touching: Collection[str] | None = None,
    limit: int | None = 50,
    every: bool = False,
    max_rows: int = 1_000_000,
    seeds: Mapping[str, SeedFile] | None = None,
    settings: Settings | None = None,
) -> ConflictsOnData:
    """Run each conflicting pair on the same rows, read-only (see the module docstring).

    ``conflicts`` are the pairs to run, by default :func:`~konstruct.analysis.conflicts.find_conflicts`
    of the graph (with ``touching``, only the pairs with a column in it): its measure conflicts, or
    with ``every`` every pair, the first ``limit`` of them. ``warehouse`` is a DuckDB file, a
    Postgres URL or an engine; with a DuckDB file or none, the ``seeds`` (by default
    :func:`~konstruct.analysis.seedfiles.seed_files`) the warehouse does not hold are read from
    their files.

    Raises:
        QueryError: There are pairs to run, but no warehouse and no seed to run them on.
    """
    started = time.perf_counter()
    found = conflicts if conflicts is not None else find_conflicts(graph, touching=touching)
    pool = list(found.conflicts) if every else found.measures
    chosen = pool[:limit] if limit is not None else pool
    result = ConflictsOnData('', max_rows, conflicts=len(pool))
    if not chosen:
        return result
    seeds = seed_files(graph, root) if seeds is None else seeds
    path: Path | None = None
    engine: Engine | None = None
    if isinstance(warehouse, Engine) or (warehouse is not None and remote(str(warehouse))):
        engine = engine_for(warehouse)
    elif warehouse is not None:
        path = Path(warehouse)
    elif not seeds:
        raise QueryError(
            'running the conflicts on the data needs the data: the models built in a DuckDB file or a '
            'Postgres database, or dbt seeds (pass --warehouse PATH or a postgresql:// URL)'
        )
    try:
        runner = _Runner(graph, root, engine, max_rows, settings)
        runner.compose(chosen, result)
        if engine is None:
            used = {k: v for k, v in seeds.items() if k in runner.composer.sources}
            engine = SeedEngine(path, root, used, memory_limit=SEED_MEMORY)
            runner.engine = engine
        result.warehouse = engine.label
        runner.run(result)
    finally:
        if engine is not None:
            engine.close()
    result.seconds = time.perf_counter() - started
    return result


def remote(text: str) -> bool:
    """Whether ``text`` names a Postgres database rather than a DuckDB file."""
    return text.startswith(('postgres://', 'postgresql://')) or 'host=' in text or 'dbname=' in text


class _Runner:
    """Composes both sides of every pair with one composer, so each source is copied once and every
    side reads the same copy."""

    def __init__(
        self, graph: Graph, root: Path, engine: Engine | None, max_rows: int, settings: Settings | None
    ) -> None:
        from konstruct.analysis.sensitivity import sensitive_columns
        from konstruct.analysis.why import _Composer

        self.graph = graph
        self.engine = engine
        self.max_rows = max_rows
        self.found = {c.node.relation: c for c in candidates(graph, root) if c.node.relation}
        read, write = (engine.read_dialect, engine.dialect) if engine is not None else ('duckdb', 'duckdb')
        self.composer = _Composer(graph, self.found, read, write)
        self.sensitive = sensitive_columns(graph, settings)
        self.trees: list[tuple[PairOnData, Node, Node, Any, Any]] = []
        self._keys: dict[str, list[str]] | None = None

    def compose(self, chosen: list[Conflict], result: ConflictsOnData) -> None:
        from sqlglot.errors import SqlglotError

        for conflict in chosen:
            pair = PairOnData(
                conflict.idea,
                conflict.a.label,
                conflict.b.label,
                a_column=conflict.a.column,
                b_column=conflict.b.column,
                a_formula=conflict.a.composed,
                b_formula=conflict.b.composed,
                differences=[_difference(conflict, d) for d in conflict.differences],
                additive=all(_additive(side, self.graph) for side in (conflict.a, conflict.b)),
            )
            result.pairs.append(pair)
            columns = [self.graph.nodes[conflict.a.column], self.graph.nodes[conflict.b.column]]
            try:
                trees = [self._tree(column) for column in columns]
            except (QueryError, SqlglotError, ValueError) as error:
                pair.skipped = _reason(error)
                continue
            self.trees.append((pair, columns[0], columns[1], trees[0], trees[1]))

    def _tree(self, column: Node) -> Any:
        owner = self.graph.parent_of(column)
        candidate = self.found.get(owner.relation) if owner is not None and owner.relation else None
        if candidate is None or owner is None:
            name = self.graph.label(owner) if owner is not None else column.id
            raise QueryError(f'{name} is not built by SQL that verify can rerun (a dbt model or a SQL file)')
        return self.composer.query(candidate)

    def run(self, result: ConflictsOnData) -> None:
        from sqlglot import exp

        engine = self.engine
        assert engine is not None
        engine.start()
        scratch: dict[str, str] = {}
        failed: dict[str, str] = {}
        for key, index in sorted(self.composer.sources.items(), key=lambda item: item[1]):
            name = f'__kp_src{index}'
            try:
                scratch[name], _ = engine.copy(key, index, self.max_rows)
                _, counted = engine.run(f'SELECT count(*) FROM {scratch[name]}')
            except Exception as error:  # a source missing from the warehouse, or unreadable
                failed[name] = f'cannot read {key} from {engine.label}: {_reason(error)}'
                continue
            result.sources.append(key)
            result.sampled = result.sampled or int(counted[0][0]) >= self.max_rows
        for pair, first, second, tree_a, tree_b in self.trees:
            reads = {t.name for tree in (tree_a, tree_b) for t in tree.find_all(exp.Table)}
            missing = sorted(failed[name] for name in reads if name in failed)
            if missing:
                pair.skipped = missing[0]
                continue
            try:
                self._pair(pair, first, second, [self._point(t, scratch) for t in (tree_a, tree_b)])
            except Exception as error:  # a model that will not run is reported, not fatal
                pair.skipped = _reason(error)

    def _point(self, tree: Any, scratch: Mapping[str, str]) -> str:
        """The composed query read from the copies: each source placeholder replaced by its copy."""
        from sqlglot import exp

        tree = tree.copy()
        for table in list(tree.find_all(exp.Table)):
            if table.name in scratch and not table.db:
                replacement = exp.to_table(scratch[table.name])
                replacement.set('alias', table.args.get('alias'))
                table.replace(replacement)
        assert self.engine is not None
        return str(tree.sql(dialect=self.engine.dialect))

    def _pair(self, pair: PairOnData, first: Node, second: Node, queries: list[str]) -> None:
        engine = self.engine
        assert engine is not None
        names: list[list[str]] = []
        for query in queries:
            names.append(self._run(f'SELECT * FROM ({query}) AS kp_side LIMIT 0')[0])
        wanted = [first.name.lower(), second.name.lower()]
        for side, (columns, name) in enumerate(zip(names, wanted, strict=True)):
            if name not in columns:
                label = pair.a if side == 0 else pair.b
                raise LookupError(f'{label} is not among the columns its model returns')
        keys = self._candidates(first, second, names)
        outputs = []
        for query, name in zip(queries, wanted, strict=True):
            listed = [*keys, name] if name not in keys else keys
            picked = ', '.join(quote(c) for c in listed)
            outputs.append(self._run(f'SELECT {picked} FROM ({query}) AS kp_side'))
        key = next(
            (
                k
                for k in keys
                if k not in wanted
                and _unique(outputs[0][1], outputs[0][0].index(k))
                and _unique(outputs[1][1], outputs[1][0].index(k))
            ),
            None,
        )
        counted = compare(outputs[0], outputs[1], key, wanted[0], wanted[1])
        for name in (
            'key',
            'rows_a',
            'rows_b',
            'matched',
            'differ',
            'only_a',
            'only_b',
            'total_a',
            'total_b',
        ):
            setattr(pair, name, getattr(counted, name))
        pair.examples = counted.examples
        self._mask(pair, first, second)

    def _run(self, sql: str) -> Output:
        engine = self.engine
        assert engine is not None
        # Built from queries the composer has already checked to be one query that only reads; the
        # DuckDB engine checks each statement again before it reaches the connection.
        return engine.run(sql)

    def _candidates(self, first: Node, second: Node, names: list[list[str]]) -> list[str]:
        """The columns that may identify the rows of both: the relations' keys in the derived
        ontology first, then the columns named like identifiers, each in both outputs."""
        from konstruct.semantics.ontology import id_like

        if self._keys is None:
            self._keys = _key_columns(self.graph)
        listed: list[str] = []
        for column in (first, second):
            owner = self.graph.parent_of(column)
            if owner is not None:
                listed += self._keys.get(self.graph.label(owner), [])
        listed += [n for n in names[0] if id_like(n)]
        both = set(names[0]) & set(names[1])
        return list(dict.fromkeys(n for n in listed if n in both))

    def _mask(self, pair: PairOnData, first: Node, second: Node) -> None:
        """Hide the values of a sensitive measure or key in the rows listed."""
        from konstruct.analysis.sensitivity import mask

        for node, side in ((first, 'a'), (second, 'b')):
            kinds = self.sensitive.get(node.id)
            if kinds:
                for example in pair.examples:
                    if getattr(example, f'in_{side}'):
                        setattr(example, side, mask(kinds))
        for node in (first, second):
            owner = self.graph.parent_of(node)
            if owner is None or pair.key is None:
                continue
            for column in self.graph.columns_of(owner.id):
                kinds = self.sensitive.get(column.id)
                if column.name.lower() == pair.key and kinds:
                    for example in pair.examples:
                        example.key = mask(kinds)


def _difference(conflict: Conflict, difference: Difference) -> str:
    who = ''
    if difference.side:
        who = f' only in {conflict.a.label if difference.side == "a" else conflict.b.label}'
    from konstruct.analysis.explain import split_condition

    clause, rest = split_condition(difference.detail)
    return f'{difference.kind}{who}: ' + (f'{clause} {rest}' if clause else difference.detail)


def _reason(error: BaseException) -> str:
    text = str(error).strip()
    return text.splitlines()[0][:200] if text else type(error).__name__
