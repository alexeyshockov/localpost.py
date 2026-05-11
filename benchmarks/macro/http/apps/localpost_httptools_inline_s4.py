"""LocalPost httptools backend, inline (no pool), ``selectors=4``."""

from __future__ import annotations

import sys

from benchmarks.macro.http.apps.localpost_httptools_inline import main

if __name__ == "__main__":
    sys.argv += ["--selectors", "4"]
    sys.exit(main())
