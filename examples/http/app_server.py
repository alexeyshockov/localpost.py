import json
import sys

from localpost import hosting
from localpost.http import HTTPReqCtx, ServerConfig
from localpost.http.app import HttpApp

app = HttpApp()


@app.get("/{name}")
def hello(name: str):
    return f"Hello, {name}!"


@app.post("/{name}/profile")
def update_user_profile(ctx: HTTPReqCtx, name: str):
    profile = json.loads(ctx.body)
    return {"updated_for": name, "profile": profile}


if __name__ == "__main__":
    sys.exit(hosting.run_app(app.service(ServerConfig(host="127.0.0.1", port=8000))))
