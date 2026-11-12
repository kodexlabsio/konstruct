"""Test the claims of a hand-written ODCS data contract against the graph.

A contract says what a table holds: its columns and their types, which are keys, which never
hold a null, which refer to another table, which are measures. :func:`check_contract` reads the
contract (one YAML document or several) and tests each of those claims against what the graph
knows, in Rust (``src/odcs/``). A finding is ``contradicted`` when the graph holds the opposite (a
missing column, another type, a key that ``konstruct verify`` found repeated, a relationship the
graph has pointing elsewhere, a measure that is not an aggregate) and ``unsupported`` when nothing
in the graph says so (a key with no uniqueness test, a relationship nothing shows).

Nothing here reads the warehouse, and Konstruct runs no quality rule: each is text for the tools
that run it, and is listed in :attr:`ContractCheck.not_run`. ``konstruct verify`` checks keys and
relationships against the data, but it does not run the rules of a contract.

The file is read once, by :mod:`konstruct.core.yamlio`, which refuses aliases that stand for more
than a million values; the parsed documents go to the engine as JSON.
"""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import yaml

from konstruct import _core
from konstruct.core import yamlio
from konstruct.core.graph import Graph
from konstruct.outputs.odcs import facts
from konstruct.semantics.ontology import Ontology, derive

if TYPE_CHECKING:
    from konstruct.interfaces.layout import Style

SEVERITIES = ('contradicted', 'unsupported')


@dataclass
class Finding:
    """One claim of a contract that the graph does not bear out."""

    severity: str
    """``contradicted`` (the graph holds the opposite, or the claim is malformed) or ``unsupported``
    (nothing in the graph says so)."""
    kind: str
    """``missing-table``, ``missing-column``, ``wrong-type``, ``key``, ``unique``, ``required``,
    ``table-qualifier`` (a table name more qualified than the graph's), ``relationship``, ``measure`` or
    ``malformed`` (a claim in the wrong shape)."""
    contract: str
    """The contract's name."""
    where: str
    """What the claim is about: ``orders.amount``, or ``orders.customer_id -> customers.id``."""
    message: str
    """What the graph says."""

    def to_dict(self) -> dict[str, Any]:
        return {
            'severity': self.severity,
            'kind': self.kind,
            'contract': self.contract,
            'where': self.where,
            'message': self.message,
        }


@dataclass
class ContractCheck:
    """What testing a contract's claims against the graph found."""

    contracts: int
    """The contracts in the file."""
    claims: int
    """The claims tested."""
    findings: list[Finding] = field(default_factory=list)
    """The claims the graph contradicts or does not support."""
    not_run: list[str] = field(default_factory=list)
    """Quality rules in the contract, listed because Konstruct does not run them."""
    notes: list[str] = field(default_factory=list)
    """References that could not be resolved, and so were not checked."""

    @property
    def contradicted(self) -> list[Finding]:
        """The findings where the graph holds the opposite."""
        return [f for f in self.findings if f.severity == 'contradicted']

    @property
    def ok(self) -> bool:
        """Whether no claim is contradicted. An unsupported claim is not a contradiction."""
        return not self.contradicted

    def text(self, style: Style | None = None) -> str:
        """The result as the CLI prints it: plain text, as ``str()`` gives, or drawn for a
        terminal with ``style``."""
        from konstruct.interfaces.render import contract_check_lines, show

        return show(contract_check_lines, self, style)

    def __str__(self) -> str:
        return self.text()

    def to_dict(self) -> dict[str, Any]:
        return {
            'ok': self.ok,
            'contracts': self.contracts,
            'claims': self.claims,
            'findings': [f.to_dict() for f in self.findings],
            'not_run': self.not_run,
            'notes': self.notes,
        }


def _plain(value: Any) -> Any:
    """``value`` as JSON holds it: keys as text, and a date or anything else YAML constructs as its text."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float('inf') else str(value)
    return str(value)


def _nfc(value: Any) -> Any:
    """``value`` with every string in Unicode normal form C, so that a name typed as é and one
    typed as e and an accent are one name."""
    if isinstance(value, str):
        return value if value.isascii() else unicodedata.normalize('NFC', value)
    if isinstance(value, dict):
        return {_nfc(k): _nfc(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_nfc(v) for v in value]
    return value


def _types(value: Any) -> set[str]:
    """Every ``physicalType`` the contracts claim, at any depth."""
    if isinstance(value, dict):
        found = {value['physicalType']} if isinstance(value.get('physicalType'), str) else set()
        return found.union(*(_types(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(_types(v) for v in value))
    return set()


def _measures(documents: list[Any]) -> set[str]:
    """The names, in lower case, of the properties the contracts claim to be measures."""
    found: set[str] = set()
    for document in documents:
        schema = document.get('schema') if isinstance(document, dict) else None
        for item in schema if isinstance(schema, list) else []:
            properties = item.get('properties') if isinstance(item, dict) else None
            for entry in properties if isinstance(properties, list) else []:
                if isinstance(entry, dict) and entry.get('semanticType') == 'measure':
                    found |= {str(entry[k]).lower() for k in ('name', 'physicalName') if k in entry}
    return found


def check_contract(graph: Graph, text: str, ontology: Ontology | None = None) -> ContractCheck:
    """Test the claims of the ODCS contracts in ``text`` against ``graph``.

    Args:
        graph: The graph to test against.
        text: One contract, or several as YAML documents.
        ontology: The graph's ontology, when already derived.

    Raises:
        ValueError: ``text`` is not YAML (or its aliases stand for too much), or not a data contract.
    """
    try:
        return _check(graph, text, ontology)
    except RecursionError as error:
        # Nesting deep enough to defeat a walk of the value, whatever got it past the reader.
        raise ValueError('invalid contract: nested too deeply to read') from error


def _check(graph: Graph, text: str, ontology: Ontology | None) -> ContractCheck:
    try:
        documents = [_nfc(_plain(d)) for d in yamlio.load_all(text)]
    except yaml.YAMLError as error:
        raise ValueError(f'invalid YAML: {error}') from error
    except RecursionError as error:
        raise ValueError('invalid YAML: nested too deeply to read') from error
    shown = _nfc(
        facts(graph, ontology or derive(graph), measures=_measures(documents), types=_types(documents))
    )
    found = _core.odcs_check(json.dumps(shown), json.dumps(documents))
    data = json.loads(found)
    return ContractCheck(
        contracts=data['contracts'],
        claims=data['claims'],
        findings=[
            Finding(f['severity'], f['kind'], f['contract'], f['where'], f['message'])
            for f in data['findings']
        ],
        not_run=data['not_run'],
        notes=data['notes'],
    )
