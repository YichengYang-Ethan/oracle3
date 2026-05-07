"""Verify oracle3 package metadata."""

import re
from importlib import metadata


def test_version() -> None:
    """Package exposes a valid SemVer __version__ that matches installed metadata."""
    from oracle3 import __version__

    # Valid SemVer (e.g. 1.1.1 or 1.1.1a0); rejects empty / non-semver strings.
    assert re.fullmatch(r'\d+\.\d+\.\d+([abrc.][\w.]*)?', __version__), (
        f'__version__ {__version__!r} is not a valid SemVer string'
    )

    # Catch drift between pyproject.toml and oracle3/__init__.py — this is the
    # bug `cz bump` leaked once: it bumped pyproject.toml but not __init__.py.
    installed = metadata.version('oracle3')
    assert __version__ == installed, (
        f'__version__ {__version__!r} disagrees with installed metadata {installed!r}'
    )
