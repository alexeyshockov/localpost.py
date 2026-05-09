# localpost.http

A small synchronous HTTP/1.1 server built on
[h11](https://h11.readthedocs.io/), plus a URI-template router, WSGI / ASGI
bridges, and a small framework (`HttpApp`) on top. Three layers, each
usable on its own; pair with `localpost.hosting` for lifecycle, or run
standalone.

In-process only — for multi-core fanout, run multiple processes under an
external supervisor. Sync server only; async handlers are reached via
`localpost.http.asgi.to_asgi(handler)` plugged into uvicorn / hypercorn /
granian.

```bash
pip install localpost[http-server]            # h11 backend (default, pure Python)
pip install localpost[http-server,http-fast]  # also adds the httptools backend
```

```python
from localpost.hosting import run_app
from localpost.http import ServerConfig
from localpost.http.app import HttpApp


app = HttpApp()


@app.get("/{name}")
def hello(name: str):
    return f"Hello, {name}!"


run_app(app.service(ServerConfig(host="127.0.0.1", port=8000)))
```

**Full reference:** <https://alexeyshockov.github.io/localpost.py/modules/http/>

Examples: [`examples/http/`](../../examples/http/).
