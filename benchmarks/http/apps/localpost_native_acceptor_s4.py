"""LocalPost h11 backend, acceptor topology with ``selectors=4``.

Wrapper around :mod:`benchmarks.http.apps.localpost_native` that injects
``--selectors 4 --acceptor`` so the runner can pick it up as a separate
stack. One acceptor thread + four worker selectors via the cross-thread
op queue.
"""

from __future__ import annotations

import sys

from benchmarks.http.apps.localpost_native import main

if __name__ == "__main__":
    sys.argv += ["--selectors", "4", "--acceptor"]
    sys.exit(main())
