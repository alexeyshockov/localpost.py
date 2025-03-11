import logging

from testcontainers.core import utils

# See https://github.com/testcontainers/testcontainers-python/pull/758
utils.setup_logger = logging.getLogger
