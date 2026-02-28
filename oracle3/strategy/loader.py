"""Shared strategy class loader — usable by CLI and the control server."""

from __future__ import annotations

import importlib
import importlib.util
import os
import uuid
from pathlib import Path

from oracle3.strategy.strategy import Strategy


def load_strategy_class(strategy_ref: str) -> type[Strategy]:
    """Load a Strategy subclass from ``'module.path:ClassName'`` or ``'/file.py:ClassName'``.

    Raises :exc:`ValueError` on any loading error so callers can wrap it into
    whatever exception type is appropriate (e.g. ``click.ClickException`` in CLI
    code, or a plain dict error in socket handlers).
    """
    if ':' not in strategy_ref:
        raise ValueError(
            "Invalid strategy reference. "
            "Use 'module.path:ClassName' or '/path/to/file.py:ClassName'."
        )

    module_or_file, class_name = strategy_ref.split(':', 1)

    if module_or_file.endswith('.py') or os.path.sep in module_or_file:
        file_path = Path(module_or_file).expanduser().resolve()
        if not file_path.exists():
            raise ValueError(f'Strategy file not found: {file_path}')
        module_name = f'_swm_user_strategy_{uuid.uuid4().hex}'
        spec = importlib.util.spec_from_file_location(module_name, file_path)
        if spec is None or spec.loader is None:
            raise ValueError(f'Could not load strategy file: {file_path}')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    else:
        try:
            module = importlib.import_module(module_or_file)
        except ModuleNotFoundError as exc:
            raise ValueError(f'Module not found: {module_or_file!r}') from exc

    strategy_cls = getattr(module, class_name, None)
    if strategy_cls is None:
        raise ValueError(f'Class {class_name!r} not found in {module_or_file!r}')
    if not isinstance(strategy_cls, type) or not issubclass(strategy_cls, Strategy):
        raise ValueError(
            f'Class {class_name!r} must inherit from oracle3.strategy.strategy.Strategy'
        )
    return strategy_cls
