"""Flask app served by Gunicorn (1 worker, gthread, 32 threads).

Single-worker on purpose: we measure server overhead, not the multiplicative
effect of more processes.
"""

from __future__ import annotations

import sys

from gunicorn.app.base import BaseApplication

from benchmarks.macro.http.apps._cli import parse_port
from benchmarks.macro.http.apps._flask_app import build_app


class _GunicornApp(BaseApplication):
    def __init__(self, app, options: dict):
        self._app = app
        self._options = options
        super().__init__()

    def load_config(self) -> None:
        for k, v in self._options.items():
            self.cfg.set(k, v)

    def load(self):
        return self._app


def main() -> int:
    port = parse_port()
    options = {
        "bind": f"127.0.0.1:{port}",
        "workers": 1,
        "threads": 32,
        "worker_class": "gthread",
        "accesslog": None,
        "errorlog": "-",
        "loglevel": "warning",
    }
    _GunicornApp(build_app(), options).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
