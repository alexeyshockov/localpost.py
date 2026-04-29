"""LocalPost httptools backend, inline (no pool), ``selectors=2``."""

from __future__ import annotations

import sys

from benchmarks.http.apps.localpost_httptools_inline import main

if __name__ == "__main__":
    sys.argv += ["--selectors", "2"]
    sys.exit(main())
