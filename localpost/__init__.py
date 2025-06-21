from ._debug import debug
from ._run import arun, run

try:
    from .__meta__ import version as __version__  # noqa
except ImportError:
    __version__ = "dev"


__all__ = ["__version__", "arun", "run", "debug"]
