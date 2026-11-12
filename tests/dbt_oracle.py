"""The manifest reader Konstruct had in Python before it moved to the Rust core, kept as the oracle
that ``tests/test_dbt_read.py`` loads manifests with, beside the core, and compares the two graphs."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from sqlglot import exp
from sqlglot.errors import SqlglotError

from konstruct.core.graph import Graph, Node, column_id, relation_key
from konstruct.core.sql import dialect_for
from konstruct.inputs.dbt import RELATION_TYPES, DbtLoad, DbtProject, ModelSql, render_refs, semantic_metrics
from konstruct.inputs.jinja import Renderer, render_model


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, RecursionError):  # a committed artefact may be nested too deeply
        return None
    return data if isinstance(data, dict) else None


def parse_relation_name(name: str | None, dialect: str | None) -> str | None:
    if not name:
        return None
    try:
        table = exp.to_table(name, dialect=dialect)
    except (SqlglotError, ValueError):
        return relation_key(*name.replace('"', '').replace('`', '').split('.'))
    return relation_key(table.catalog, table.db, table.name)


def _relation_for(node: dict[str, Any], dialect: str | None) -> str | None:
    parsed = parse_relation_name(node.get('relation_name'), dialect)
    if parsed:
        return parsed
    name = node.get('alias') or node.get('identifier') or node.get('name')
    if not name:
        return None
    return relation_key(node.get('database'), node.get('schema'), name)


def _incremental(config: dict[str, Any]) -> dict[str, Any]:
    """An incremental model's key and strategy, which decide what a run keeps of earlier rows."""
    if config.get('materialized') != 'incremental':
        return {}
    found: dict[str, Any] = {k: config[k] for k in ('unique_key', 'incremental_strategy') if config.get(k)}
    key = found.get('unique_key')
    if isinstance(key, list):
        found['unique_key'] = [str(k) for k in key]
    return found


def _description(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _catalog_columns(
    catalog: dict[str, Any] | None, unique_id: str
) -> list[tuple[str, str | None, str | None]]:
    if not catalog:
        return []
    entry = catalog.get('nodes', {}).get(unique_id) or catalog.get('sources', {}).get(unique_id)
    if not entry:
        return []
    columns = sorted(entry.get('columns', {}).values(), key=lambda c: c.get('index') or 0)
    return [(c['name'], c.get('type'), _description(c.get('comment'))) for c in columns if c.get('name')]


def _primary_key(item: dict[str, Any]) -> list[str]:
    """The columns of a model's ``primary_key`` constraint, declared on the model or on its columns."""
    for constraint in item.get('constraints') or []:
        if isinstance(constraint, dict) and constraint.get('type') == 'primary_key':
            return [str(c) for c in constraint.get('columns') or []]
    return [
        str(column.get('name', name))
        for name, column in (item.get('columns') or {}).items()
        if isinstance(column, dict)
        and any(
            isinstance(c, dict) and c.get('type') == 'primary_key' for c in column.get('constraints') or []
        )
    ]


def _add_columns(graph: Graph, parent: Node, item: dict[str, Any], catalog: dict[str, Any] | None) -> bool:
    """Add a node's columns; whether the catalog lists them, rather than only its documentation."""
    primary_key = _primary_key(item)
    if primary_key:
        parent.attrs['primary_key'] = primary_key
        # A dbt constraint reaches the warehouse only under an enforced contract.
        if ((item.get('config') or {}).get('contract') or {}).get('enforced'):
            parent.attrs['primary_key_enforced'] = True
    documented = {
        name.lower(): col for name, col in (item.get('columns') or {}).items() if isinstance(col, dict)
    }
    seen: set[str] = set()
    for name, dtype, comment in _catalog_columns(catalog, item['unique_id']):
        doc = documented.get(name.lower(), {})
        graph.add_column(
            parent,
            name,
            data_type=dtype or doc.get('data_type'),
            description=_description(doc.get('description')) or comment,
            origin='dbt',
        )
        seen.add(name.lower())
    for name, doc in documented.items():
        if name in seen:
            continue
        graph.add_column(
            parent,
            doc.get('name', name),
            data_type=doc.get('data_type'),
            description=_description(doc.get('description')),
            origin='dbt',
            attrs={'documented_only': True} if catalog else {},
        )
    return bool(seen)


def load(
    project: DbtProject,
    graph: Graph,
    *,
    dialect: str | None,
    keep: Callable[[Iterable[Path]], list[Path]] | None = None,
) -> DbtLoad | None:
    """Add a dbt project's nodes, columns, tests and exposures to ``graph``. ``keep`` chooses
    which macro files may be read to render uncompiled models (by default, all)."""
    manifest = _read_json(project.manifest_path)
    if manifest is None:
        graph.warn(f'dbt: {project.name}: no readable manifest at {project.manifest_path}')
        return None
    catalog = _read_json(project.catalog_path)
    metadata = manifest.get('metadata', {})
    dialect = dialect or dialect_for(metadata.get('adapter_type'))
    root = project.root
    # The project's {% docs __overview__ %} block, which dbt's documentation site opens on.
    overview = manifest.get('docs', {}).get(f'doc.{project.name}.__overview__', {}).get('block_contents')
    if isinstance(overview, str) and overview.strip():
        graph.meta.setdefault('overviews', {})[project.name] = overview.strip()

    def rel_path(item: dict[str, Any]) -> str | None:
        path = item.get('original_file_path')
        if not path:
            return None
        return os.path.relpath(root / str(path), str(graph.meta.get('root', root)))

    models: list[ModelSql] = []
    catalogued: set[str] = set()
    schemas: dict[tuple[str | None, str | None], int] = {}
    renderer: Renderer | None = None
    for item in manifest.get('nodes', {}).values():
        kind = RELATION_TYPES.get(item.get('resource_type', ''))
        if kind is None:
            continue
        relation = _relation_for(item, dialect)
        config = item.get('config') or {}
        node = graph.add_node(
            Node(
                id=item['unique_id'],
                kind=kind,
                name=item.get('alias') or item['name'],
                relation=relation,
                description=_description(item.get('description')),
                path=rel_path(item),
                origins=['dbt'],
                attrs={
                    'package': item.get('package_name'),
                    'fqn': item.get('fqn', []),
                    'materialized': config.get('materialized'),
                    'tags': item.get('tags', []),
                    'dbt_name': item['name'],
                    'checksum': (item.get('checksum') or {}).get('checksum'),
                    **_incremental(config),
                },
            )
        )
        if _add_columns(graph, node, item, catalog):
            catalogued.add(node.id)
        upstream = [n for n in item.get('depends_on', {}).get('nodes', []) if not n.startswith('macro.')]
        for parent in upstream:
            graph.add_edge(parent, node.id, 'feeds', origin='dbt')
        key = (item.get('database'), item.get('schema'))
        schemas[key] = schemas.get(key, 0) + 1
        if kind == 'seed' or not relation:
            continue
        compiled = item.get('compiled_code')
        raw = item.get('raw_code')
        if compiled:
            models.append(ModelSql(node.id, relation, compiled, True, upstream))
        elif raw and item.get('language', 'sql') == 'sql':
            if renderer is None:
                macro_paths = [p for p in project.source_paths if 'macro' in p.name]
                renderer = Renderer(project.root, manifest, macro_paths=macro_paths or None, keep=keep)
            sql = render_model(raw, renderer, lambda text: render_refs(text, manifest))
            models.append(ModelSql(node.id, relation, sql, False, upstream))

    for item in manifest.get('sources', {}).values():
        node = graph.add_node(
            Node(
                id=item['unique_id'],
                kind='source',
                name=item.get('identifier') or item['name'],
                relation=_relation_for(item, dialect),
                description=_description(item.get('description'))
                or _description(item.get('source_description')),
                path=rel_path(item),
                origins=['dbt'],
                attrs={
                    'source_name': item.get('source_name'),
                    'package': item.get('package_name'),
                    # What the project says lands the table (`loader:` in sources.yml).
                    **({'declared_loader': item['loader']} if item.get('loader') else {}),
                },
            )
        )
        if _add_columns(graph, node, item, catalog):
            catalogued.add(node.id)

    for item in manifest.get('exposures', {}).values():
        owner = item.get('owner') or {}
        node = graph.add_node(
            Node(
                id=item['unique_id'],
                kind='exposure',
                name=item['name'],
                description=_description(item.get('description')),
                path=rel_path(item),
                origins=['dbt'],
                attrs={
                    k: v
                    for k, v in {
                        'type': item.get('type'),
                        'label': item.get('label'),
                        'maturity': item.get('maturity'),
                        'owner': owner.get('name'),
                        'url': item.get('url'),
                    }.items()
                    if v
                },
            )
        )
        for parent in item.get('depends_on', {}).get('nodes', []):
            graph.add_edge(parent, node.id, 'feeds', origin='dbt')

    for item in manifest.get('nodes', {}).values():
        if item.get('resource_type') == 'test':
            _add_test(graph, item, rel_path(item))

    semantic = semantic_metrics(manifest, rel_path)
    if semantic:
        graph.meta.setdefault('dbt_metrics', []).extend(semantic)

    default_catalog, default_schema = max(schemas, key=lambda k: schemas[k]) if schemas else (None, None)
    return DbtLoad(
        project=project,
        dialect=dialect,
        models=models,
        default_catalog=default_catalog,
        default_schema=default_schema,
        manifest_checksum=metadata.get('invocation_id'),
        has_catalog=catalog is not None,
        catalogued=catalogued,
        counts={
            kind: sum(1 for item in group.values() if item.get('resource_type') == kind)
            for kind, group in (
                ('model', manifest.get('nodes', {})),
                ('seed', manifest.get('nodes', {})),
                ('snapshot', manifest.get('nodes', {})),
                ('test', manifest.get('nodes', {})),
                ('source', manifest.get('sources', {})),
                ('exposure', manifest.get('exposures', {})),
            )
        },
    )


def _threshold(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ''
    return text if text and text.replace(' ', '') not in ('!=0', '>0', '>=1') else None


def _add_test(graph: Graph, item: dict[str, Any], path: str | None) -> None:
    meta = item.get('test_metadata') or {}
    config = item.get('config') or {}
    node = graph.add_node(
        Node(
            id=item['unique_id'],
            kind='test',
            name=item['name'],
            description=_description(item.get('description')),
            path=path,
            origins=['dbt'],
            attrs={
                k: v
                for k, v in {
                    'test': meta.get('name') or 'singular',
                    'severity': config.get('severity'),
                    'error_if': _threshold(config.get('error_if')),
                    'column': item.get('column_name'),
                    'where': config.get('where'),
                    'columns': (meta.get('kwargs') or {}).get('combination_of_columns'),
                }.items()
                if v
            },
        )
    )
    targets = item.get('depends_on', {}).get('nodes', [])
    attached = item.get('attached_node') or (targets[0] if len(targets) == 1 else None)
    column = item.get('column_name')
    if attached and column and graph.column(attached, column):
        graph.add_edge(node.id, column_id(attached, column), 'tests')
    elif attached:
        graph.add_edge(node.id, attached, 'tests')
    kwargs = meta.get('kwargs') or {}
    for target in targets:
        if target == attached:
            continue
        field_name = kwargs.get('field')
        if meta.get('name') == 'relationships' and field_name and graph.column(target, field_name):
            graph.add_edge(node.id, column_id(target, field_name), 'tests', role='relationship target')
        else:
            graph.add_edge(node.id, target, 'tests')


# dbt node selection, as used by `dbt run --select` in orchestration code
