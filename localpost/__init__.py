import logging
from importlib.metadata import PackageNotFoundError, version

from ._debug import debug
from ._utils import Result

try:
    __version__ = version("localpost")
except PackageNotFoundError:
    __version__ = "dev"


__all__ = ["Result", "__version__", "debug"]


# Set up logging according to the best practices:
# https://docs.python.org/3/howto/logging.html#configuring-logging-for-a-library
logging.getLogger("localpost").addHandler(logging.NullHandler())
