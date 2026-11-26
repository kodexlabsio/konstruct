"""Whether a computed column holds an amount: a number that adding up, averaging or comparing as
a quantity makes sense of, rather than a date, a label, a flag or a key.

:mod:`konstruct.analysis.conflicts` reports two columns as one measure computed two ways only when
both are amounts. The test reads the formula's syntax tree, not its text: comments, operators
inside string literals and Konstruct's own reading names (``"orders#1/2"``) say nothing, and a
wrapper (``COALESCE``, ``ROUND``, ``CAST``, ``NULLIF``, a window) is looked through to the value
it wraps.
"""

from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

from konstruct.core.sql import PARSE_ERRORS

__all__ = ['amount', 'is_amount_type', 'not_amount_type', 'uncommented']

# Types whose values are not amounts.
NOT_AMOUNTS = (
    'DATE',
    'TIME',
    'CHAR',
    'TEXT',
    'STRING',
    'BOOL',
    'UUID',
    'JSON',
    'BLOB',
    'BYTES',
    'BINARY',
    'VARIANT',
    'OBJECT',
    'ARRAY',
    'MAP',
    'STRUCT',
    'INTERVAL',
)
AMOUNTS = ('INT', 'DECIMAL', 'NUMERIC', 'NUMBER', 'DOUBLE', 'FLOAT', 'REAL', 'MONEY')
# A name that says the column holds a moment; a rate per unit of time (orders_per_day) does not.
_MOMENT = re.compile(
    r'(?<!per_)(?<!per)(^|_)(date|day|time|timestamp|ts|at|datetime|month|year|week|quarter)$', re.IGNORECASE
)

# Aggregates whose result is an amount whatever they read.
_COUNTING: tuple[type[exp.Expr], ...] = (
    exp.Count,
    exp.Sum,
    exp.Avg,
    exp.Stddev,
    exp.StddevPop,
    exp.StddevSamp,
    exp.Variance,
)
_COUNTING += (exp.VariancePop, exp.Quantile, exp.Median, exp.ApproxDistinct, exp.CountIf, exp.Corr)
# Aggregates whose result is a flag, a text or a list.
_NOT_AMOUNT_AGGREGATES: tuple[type[exp.Expr], ...] = (
    exp.LogicalOr,
    exp.LogicalAnd,
    exp.GroupConcat,
    exp.ArrayAgg,
)
# Functions that return their argument's kind of value.
_WRAPPERS: tuple[type[exp.Expr], ...] = (
    exp.Coalesce,
    exp.Round,
    exp.Abs,
    exp.Nullif,
    exp.Floor,
    exp.Ceil,
    exp.Greatest,
)
_WRAPPERS += (exp.Least, exp.Paren, exp.Neg, exp.Lag, exp.Lead, exp.FirstValue, exp.LastValue)
_WRAPPERS += (exp.Anonymous,)
# Functions that return text.
_TEXT: tuple[type[exp.Expr], ...] = (exp.Trim, exp.Upper, exp.Lower, exp.Substring, exp.Concat, exp.DPipe)
_TEXT += (exp.JSONExtract, exp.JSONExtractScalar, exp.JSONBExtract, exp.JSONBExtractScalar)
# Functions that return a duration in units, such as days between two dates.
_DURATIONS: tuple[type[exp.Expr], ...] = (exp.DateDiff, exp.DatetimeDiff, exp.TimestampDiff, exp.TimeDiff)
# Functions that return a part of a moment, a label or a moment.
_NOT_AMOUNTS: tuple[type[exp.Expr], ...] = (
    exp.Extract,
    exp.TimeToStr,
    exp.Concat,
    exp.DPipe,
    exp.Upper,
    exp.Lower,
    exp.Trim,
)
_NOT_AMOUNTS += (exp.Substring, exp.DateTrunc, exp.TimestampTrunc, exp.DateAdd, exp.DateSub)
_NOT_AMOUNTS += (exp.JSONExtract, exp.JSONExtractScalar, exp.JSONBExtract, exp.JSONBExtractScalar)
_NOT_AMOUNTS += (exp.RowNumber, exp.Rank, exp.DenseRank, exp.Predicate, exp.Not, exp.Connector)
_NOT_AMOUNTS += (exp.CurrentDate, exp.CurrentTimestamp, exp.StrToTime, exp.StrToDate, exp.Interval)


def uncommented(sql: str, dialect: str | None) -> str:
    """``sql`` without its comments, written as it was otherwise; ``sql`` itself when it has none
    or does not parse. A condition keeps its label (``WHERE: ...``)."""
    if '/*' not in sql and '--' not in sql:
        return sql
    label, sep, text = sql.partition(': ')
    if not sep or not label.isupper():
        label, sep, text = '', '', sql
    try:
        tree = sqlglot.parse_one(text, read=dialect)
    except PARSE_ERRORS:
        return sql
    return f'{label}{sep}{tree.sql(dialect=dialect, comments=False)}'


def not_amount_type(data_type: str | None) -> bool:
    """Whether a declared type holds something other than amounts."""
    upper = (data_type or '').upper()
    return bool(upper) and any(word in upper for word in NOT_AMOUNTS)


def is_amount_type(data_type: str | None) -> bool:
    """Whether a declared type is a number."""
    upper = (data_type or '').upper()
    return not not_amount_type(upper) and any(word in upper for word in AMOUNTS)


def amount(formula: str | None, dialect: str | None, *, name: str = '', data_type: str | None = None) -> bool:
    """Whether a column named ``name``, of ``data_type`` when declared, computed by ``formula``
    holds an amount.

    A declared type other than a number settles it. Otherwise the formula decides, looked through
    its wrappers: an aggregate that counts, adds or averages is an amount, and so is ``MIN`` or
    ``MAX`` of an amount; arithmetic and a difference between two moments are amounts; a flag, a
    text, a part of a date, a rank and a constant are not. A formula that does not parse is an
    amount when its type says so."""
    if not_amount_type(data_type):
        return False
    tree = _parse(formula, dialect)
    if tree is None:
        return is_amount_type(data_type)
    verdict = False if isinstance(_unwrap(tree), (exp.Literal, exp.Null, exp.Boolean)) else _amount(tree)
    if verdict is None:
        return is_amount_type(data_type)
    if verdict and _MOMENT.search(name) and not _counts(tree):
        # A MIN or MAX, or arithmetic, named for a moment (first_order_date) is that moment.
        return False
    return verdict


def _parse(formula: str | None, dialect: str | None) -> exp.Expr | None:
    if not formula:
        return None
    try:
        return sqlglot.parse_one(formula, read=dialect)
    except PARSE_ERRORS:
        return None


def _counts(tree: exp.Expr) -> bool:
    """Whether the value is a count, a sum or an average, which a moment never is."""
    node = _unwrap(tree)
    return isinstance(node, (_COUNTING, _DURATIONS))


def _unwrap(node: exp.Expr) -> exp.Expr:
    while True:
        if (
            isinstance(node, (exp.Window, exp.Filter))
            or (isinstance(node, exp.Cast) and _amount_type(node.to))
            or (isinstance(node, _WRAPPERS) and not isinstance(node, exp.Anonymous) and node.this is not None)
        ):
            node = node.this
        else:
            return node


def _amount_type(to: exp.DataType | None) -> bool:
    return to is not None and to.is_type(*exp.DataType.NUMERIC_TYPES)


def _amount(node: exp.Expr) -> bool | None:
    """True for an amount, False for something else, None when the syntax does not say."""
    if isinstance(node, (exp.Window, exp.Filter)):
        return _amount(node.this)
    if isinstance(node, exp.Cast):
        if _amount_type(node.to):
            inner = _amount(node.this)
            # Text read as a number (CAST(NULLIF(TRIM(price), '') AS DECIMAL)) is a number; a flag
            # or a moment cast to one is not.
            return True if inner is None or _text(node.this) else inner
        return False
    if isinstance(node, _COUNTING):
        return True
    if isinstance(node, (exp.Min, exp.Max)):
        argument = _unwrap(node.this)
        if isinstance(argument, exp.Column):
            # The least or greatest of a column is an amount unless the column is a moment.
            return not _MOMENT.search(argument.name)
        return _amount(node.this)
    if isinstance(node, _NOT_AMOUNT_AGGREGATES + _NOT_AMOUNTS):
        return False
    if isinstance(node, _DURATIONS):
        return True
    if isinstance(node, (exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod, exp.IntDiv)):
        # One moment minus another is a duration, a number; anything else with a moment (a date
        # minus an interval is a date) is not.
        operands = [_amount(node.this), _amount(node.expression)]
        if isinstance(node, exp.Sub) and operands == [False, False]:
            return True if _moment(node.this) and _moment(node.expression) else None
        return False not in operands
    if isinstance(node, (exp.Case, exp.If)):
        results = [_amount(r) for r in _results(node)]
        if any(r is False for r in results):
            return False
        literals = [r for r in _results(node) if isinstance(_unwrap(r), (exp.Literal, exp.Null))]
        if len(literals) == len(_results(node)):
            # Only constants, such as 1 and 0: a flag or a code, not an amount.
            return False
        return True if any(results) else None
    if isinstance(node, _WRAPPERS) and not isinstance(node, exp.Anonymous):
        values = [_amount(v) for v in [node.this, *node.expressions] if isinstance(v, exp.Expr)]
        if any(v is False for v in values):
            return False
        return True if any(values) else None
    if isinstance(node, exp.Boolean):
        return False
    # A constant inside a formula (the 0 of COALESCE(x, 0), the 100 of x / 100) says nothing.
    return None


def _text(node: exp.Expr) -> bool:
    """Whether ``node`` is text: a function of text, looked through ``NULLIF`` and ``COALESCE``."""
    while isinstance(node, (exp.Nullif, exp.Coalesce, exp.Paren)) and node.this is not None:
        node = node.this
    return isinstance(node, _TEXT)


def _moment(node: exp.Expr) -> bool:
    """Whether ``node`` is a moment: a column named for one, a date or a time function, or a cast
    to a date or a time."""
    node = _unwrap(node)
    if isinstance(node, exp.Column):
        return bool(_MOMENT.search(node.name))
    if isinstance(node, exp.Cast):
        return node.to is not None and node.to.is_type(*exp.DataType.TEMPORAL_TYPES)
    return isinstance(
        node,
        (
            exp.CurrentDate,
            exp.CurrentTimestamp,
            exp.StrToTime,
            exp.StrToDate,
            exp.DateTrunc,
            exp.TimestampTrunc,
            exp.DateAdd,
            exp.DateSub,
        ),
    )


def _results(node: exp.Expr) -> list[exp.Expr]:
    if isinstance(node, exp.If):
        return [r for r in (node.args.get('true'), node.args.get('false')) if isinstance(r, exp.Expr)]
    found = [i.args['true'] for i in node.args.get('ifs') or [] if isinstance(i.args.get('true'), exp.Expr)]
    default = node.args.get('default')
    return found + ([default] if isinstance(default, exp.Expr) else [])
