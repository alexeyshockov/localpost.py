"""LocalPost httptools backend with ``selectors=2``.

Wrapper around :mod:`benchmarks.http.apps.localpost_httptools` that injects
``--selectors 2`` so the runner can pick it up as a separate stack.
"""

from __future__ import annotations

import sys

from benchmarks.http.apps.localpost_httptools import main

if __name__ == "__main__":
    sys.argv += ["--selectors", "2"]
    sys.exit(main())
