from ._run import arun, run

try:
    from ._version import __version__  # noqa
except ImportError:
    __version__ = "dev"


__all__ = ["__version__", "arun", "run"]
