"""Render dbt model SQL without dbt: Jinja with ref, source, var, config, the project's macros and
its packages' macros, and adapter.dispatch.

Konstruct uses dbt's compiled SQL when it has it. This renderer is for SQL dbt has not compiled: the
other side of a `konstruct diff`, or a project whose artefacts are missing. It runs in a sandbox, never
touches a database, and stands in for anything that would (run_query, statement, load_result) with
an empty result. It is part of the native core (``konstruct._core``, the Rust crate in
``src/jinja/``); this module is where the rest of Konstruct imports it from.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable
from pathlib import Path
from typing import Any

from konstruct import _core


def project_vars(
    project_root: Path, project_name: str | None = None, packages: Collection[str] = ()
) -> dict[str, Any]:
    """The values ``var()`` gives in a dbt project's own models, from ``vars`` in its
    dbt_project.yml: those set for every package, and over them those set under the project's
    name. dbt picks a var by the package of the model it compiles, so a macro, a package's
    included, sees these in the project's models; a block under the name of one of ``packages``
    is for that package's own models, which are not rendered here, and is not a var. A value that
    is itself a template (``{{ env_var(...) }}``) is dbt's to render, with what only dbt has, so it
    is not known here. Only read, as data; nothing in them runs.
    """
    import yaml

    from konstruct.core import yamlio

    try:
        config = yamlio.read(project_root / 'dbt_project.yml') or {}
    except (OSError, yaml.YAMLError):
        return {}
    values = config.get('vars') if isinstance(config, dict) else None
    if not isinstance(values, dict):
        return {}
    name = project_name or (config.get('name') if isinstance(config.get('name'), str) else None)
    scoped = {*packages, *([name] if name else [])}
    found = {str(k): v for k, v in values.items() if not (isinstance(v, dict) and k in scoped)}
    own = values.get(name) if name else None
    if isinstance(own, dict):
        found.update({str(k): v for k, v in own.items()})
    return {k: v for k, v in found.items() if not _templated(v)}


def _templated(value: Any) -> bool:
    """Whether ``value`` holds text that dbt renders as Jinja."""
    if isinstance(value, str):
        return '{{' in value or '{%' in value
    if isinstance(value, dict):
        return any(_templated(v) for v in value.values())
    if isinstance(value, list):
        return any(_templated(v) for v in value)
    return False


def known_packages(project_root: Path, macro_packages: Iterable[str]) -> set[str]:
    """The packages ``project_vars`` takes a block under their name to be for: the installed ones
    and those the manifest's macros come from."""
    packages_dir = project_root / 'dbt_packages'
    installed = [p.name for p in packages_dir.iterdir() if p.is_dir()] if packages_dir.is_dir() else []
    return {*installed, *macro_packages}


class Renderer:
    """Renders models of one dbt project, with its macros and its installed packages' macros."""

    def __init__(
        self,
        project_root: Path,
        manifest: dict[str, Any],
        *,
        adapter_type: str | None = None,
        macro_paths: list[Path] | None = None,
        keep: Callable[[Iterable[Path]], list[Path]] | None = None,
    ) -> None:
        macros = manifest.get('macros') or {}
        known = known_packages(
            project_root, (str(m.get('package_name')) for m in macros.values() if isinstance(m, dict))
        )
        project_name = manifest.get('metadata', {}).get('project_name')
        self._core = _core.Renderer(
            project_root,
            manifest,
            adapter_type=adapter_type,
            macro_paths=macro_paths,
            keep=keep,
            variables=project_vars(project_root, project_name, known),
        )
        self.adapter_type: str = self._core.adapter_type

    def render(self, sql: str) -> str:
        """The SQL of a model with its Jinja rendered. Raises ``ValueError`` for a template that does
        not parse or fails as it runs."""
        return self._core.render(sql)


def render_model(sql: str, renderer: Renderer | None, fallback: Callable[[str], str]) -> str:
    """Render with ``renderer`` when there is one and it succeeds; otherwise with ``fallback``."""
    if renderer is not None:
        try:
            return renderer.render(sql)
        except ValueError:
            pass
    return fallback(sql)


__all__ = ['Renderer', 'known_packages', 'project_vars', 'render_model']
