"""Whether a computed column holds an amount, read from its formula's syntax tree."""

from __future__ import annotations

import pytest

from konstruct.analysis.measures import amount, uncommented


@pytest.mark.parametrize(
    'formula',
    [
        'SUM(o.amount)',
        'COALESCE(SUM(o.amount), 0)',
        'ROUND(AVG(o.amount), 2)',
        'CAST(SUM(o.amount) AS DECIMAL(12, 2))',
        "DATE_DIFF('day', o.ordered_at, o.shipped_at)",
        'o.price - o.cost',
        'o.amount / 100',
        '"orders#1/3".amount + 1',
        'COUNT(DISTINCT o.id)',
        'SUM(CASE WHEN o.status = 1 THEN 1 ELSE 0 END)',
        "SUM(o.amount) FILTER(WHERE o.status <> 'x')",
        'SUM(o.amount) OVER (PARTITION BY o.customer_id)',
        'MAX(o.amount)',
        'SUM(o.amount) /* net of refunds */',
        "SUM(CASE WHEN o.note = 'a/b' THEN o.amount END)",
    ],
)
def test_amounts(formula: str) -> None:
    assert amount(formula, 'duckdb', name='value') is True


@pytest.mark.parametrize(
    ('formula', 'name'),
    [
        ('MAX(o.order_date)', 'last_order_date'),
        ('MIN(o.created_at)', 'first_seen'),
        ('BOOL_OR(o.flag)', 'has_flag'),
        ("STRING_AGG(o.tag, ',')", 'tags'),
        ("o.payload ->> 'x'", 'x'),
        ("CASE WHEN o.s = 'x' THEN 1 ELSE 0 END", 'is_x'),
        ("CASE WHEN o.s = 'x' THEN 'a' ELSE 'b' END", 'label'),
        ('EXTRACT(year FROM o.d)', 'order_year'),
        ("STRFTIME(o.d, '%Y-%m')", 'cohort'),
        ('ROW_NUMBER() OVER (ORDER BY o.d)', 'n'),
        ('MAX(CASE WHEN o.viewed THEN 1 ELSE 0 END)', 'viewed'),
        ('o.d + INTERVAL 1 DAY', 'due'),
        ('1', 'one'),
    ],
)
def test_not_amounts(formula: str, name: str) -> None:
    assert amount(formula, 'duckdb', name=name) is False


def test_a_declared_type_settles_it() -> None:
    assert amount('MAX(o.x)', 'duckdb', name='x', data_type='DATE') is False
    assert amount('MAX(o.x)', 'duckdb', name='x', data_type='VARIANT') is False
    assert amount('not sql (((', 'duckdb', name='x', data_type='BIGINT') is True
    assert amount('not sql (((', 'duckdb', name='x') is False


def test_comments_are_dropped() -> None:
    assert uncommented('SUM(o.amount) /* net */', 'duckdb') == 'SUM(o.amount)'
    assert uncommented("WHERE: o.s = 'Usage' /* usage only */", 'duckdb') == "WHERE: o.s = 'Usage'"
    assert uncommented('SUM(o.amount)', 'duckdb') == 'SUM(o.amount)'


def test_a_rate_per_unit_of_time_is_not_a_moment() -> None:
    assert amount('count(*) / 7', None, name='orders_per_day')
    assert amount('count(*) / 7', None, name='orders_perday')


def test_a_date_minus_an_interval_is_a_date_and_two_moments_a_duration() -> None:
    assert not amount("order_date - interval '1 day'", None, name='x')
    assert amount('cast(shipped as date) - cast(ordered as date)', None, name='x')
    assert amount("cast(nullif(trim(price), '') as decimal(10, 2))", None, name='x')
