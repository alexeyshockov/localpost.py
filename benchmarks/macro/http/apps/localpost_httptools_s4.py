"""LocalPost httptools backend with ``selectors=4``.

Wrapper around :mod:`benchmarks.macro.http.apps.localpost_httptools` that injects
``--selectors 4`` so the runner can pick it up as a separate stack.
"""

from __future__ import annotations

import sys

from benchmarks.macro.http.apps.localpost_httptools import main

if __name__ == "__main__":
    sys.argv += ["--selectors", "4"]
    sys.exit(main())
