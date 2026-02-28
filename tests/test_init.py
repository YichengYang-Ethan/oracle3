"""Verify oracle3 package metadata."""


def test_version() -> None:
    """Package exposes the expected version string."""
    from oracle3 import __version__

    assert __version__ == '1.0.0'
