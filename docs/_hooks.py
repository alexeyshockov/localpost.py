"""MkDocs hook: rewrite repo-relative links to work on the docs site.

The per-module READMEs and design notes use links like
``../../localpost/http/_base.py`` and ``../../examples/http/`` so they
render correctly when viewed on GitHub. Inside the rendered docs site
those targets don't exist (they're outside ``docs/``), so we rewrite
them here:

- ``localpost/<mod>/README.md`` (cross-module)  -> ``modules/<mod>.md``
- ``localpost/<path>``, ``examples/<path>``, ``benchmarks/<path>``
  (source / example files)                     -> absolute GitHub URLs

The rewrite runs after ``mkdocs-include-markdown-plugin`` has already
inlined per-module READMEs into ``docs/modules/*.md``.
"""

from __future__ import annotations

import re

REPO = "https://github.com/alexeyshockov/localpost.py"
BRANCH = "main"

_MODULES = ("hosting", "scheduler", "http", "di", "openapi")
_MODULE_README_RE = re.compile(r"\]\((?:\.\./)+localpost/(" + "|".join(_MODULES) + r")/README\.md(#[^)]*)?\)")
# Anything else under localpost/, examples/, benchmarks/ -> blob/tree URL on GitHub.
_REPO_PATH_RE = re.compile(r"\]\((?:\.\./)+(localpost|examples|benchmarks)/([^)]+)\)")


def _module_readme_sub(match: re.Match[str]) -> str:
    module = match.group(1)
    fragment = match.group(2) or ""
    return f"](../modules/{module}.md{fragment})"


def _repo_path_sub(match: re.Match[str]) -> str:
    top = match.group(1)
    rest = match.group(2)
    # Files (have a '.', or anchor) -> /blob/. Directories -> /tree/.
    kind = "tree" if rest.endswith("/") else "blob"
    return f"]({REPO}/{kind}/{BRANCH}/{top}/{rest})"


def on_page_markdown(markdown: str, **_: object) -> str:
    markdown = _MODULE_README_RE.sub(_module_readme_sub, markdown)
    markdown = _REPO_PATH_RE.sub(_repo_path_sub, markdown)
    return markdown
