"""LocalPost httptools backend, inline (no pool), acceptor topology with ``selectors=4``.

Wrapper around :mod:`benchmarks.macro.http.apps.localpost_httptools_inline` that
injects ``--selectors 4 --acceptor`` so the runner can pick it up as a
separate stack. Characterises the acceptor topology in isolation —
handlers run on the worker selector thread without a pool hop.
"""

from __future__ import annotations

import sys

from benchmarks.macro.http.apps.localpost_httptools_inline import main

if __name__ == "__main__":
    sys.argv += ["--selectors", "4", "--acceptor"]
    sys.exit(main())
