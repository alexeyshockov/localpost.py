#!/usr/bin/env python
"""Run a Click command as a hosted service.

For a one-shot CLI you can just call ``hello()`` directly. Wrapping it as a
hosted service earns its keep when the command is composed with sibling
services (a background worker, a metrics endpoint, ...) under one host.
"""

import click

from localpost.hosting import run_app
from localpost.hosting.services.click import click_cmd


@click.command()
@click.option("--name", default="world")
def hello(name: str) -> None:
    click.echo(f"Hello, {name}!")


if __name__ == "__main__":
    run_app(click_cmd(hello))
