"""Execution provider registry with lazy optional-extra loading.

Mirrors the pattern used for metadata and blob store adapters in ``vfs.vfs``:
providers that require optional extras are loaded lazily so that a missing
dependency produces a clear, actionable error rather than an opaque
``ModuleNotFoundError``.

Registry shape
--------------
``_EXECUTION_PROVIDERS`` maps provider name → ``(extra_name, driver_module,
adapter_module, class_name)``.  ``resolve_execution_provider`` looks up the
name, imports the driver guard module, then imports the adapter class.

Adding a new provider requires only inserting a row in
``_EXECUTION_PROVIDERS`` — no changes to this function.
"""

from __future__ import annotations

import importlib
import importlib.util
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vfs.config import VFSConfig
    from vfs.protocols.execution import ExecutionProvider

#: Provider name → (extra_name, driver_module, adapter_module, class_name).
#: ``driver_module`` is the importable guard for the optional dependency.
_EXECUTION_PROVIDERS: dict[str, tuple[str, str, str, str]] = {
    "monty": (
        "monty",
        "pydantic_monty",
        "vfs.execution.monty_provider",
        "MontyExecutionProvider",
    ),
    "just-bash": (
        "just-bash",
        "just_bash",
        "vfs.execution.just_bash_provider",
        "JustBashExecutionProvider",
    ),
}


def resolve_execution_provider(name: str, config: VFSConfig) -> ExecutionProvider:  # noqa: ARG001
    """Return an :class:`~vfs.protocols.execution.ExecutionProvider` for ``name``.

    Parameters
    ----------
    name:
        Provider name string (e.g. ``"monty"``).
    config:
        VFS configuration (currently unused; reserved for future provider
        configuration forwarding).

    Raises
    ------
    ValueError
        When ``name`` is not a registered provider.
    ImportError
        When the provider's optional extra is not installed, with an actionable
        "install ai-vfs[extra]" message (no raw traceback exposed).
    """
    spec = _EXECUTION_PROVIDERS.get(name)
    if spec is None:
        known = ", ".join(sorted(_EXECUTION_PROVIDERS))
        raise ValueError(f"Unknown execution provider {name!r}. Known providers: {known or '(none registered)'}.")

    extra, driver, adapter_module, class_name = spec
    if importlib.util.find_spec(driver) is None:
        raise ImportError(
            f"Execution provider {name!r} requires the optional {extra!r} extra "
            f"(missing dependency {driver!r}). "
            f"Install it with: pip install 'ai-vfs[{extra}]'"
        )
    if importlib.util.find_spec(adapter_module) is None:
        raise ImportError(
            f"Execution provider {name!r} is not available in this build of ai-vfs "
            f"(adapter {adapter_module!r} is not present)."
        )
    module = importlib.import_module(adapter_module)
    cls = getattr(module, class_name)
    return cls()
