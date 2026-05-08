# Contributing to localpost

Thanks for your interest in contributing! This document covers everything you need to file a good
issue, send a pull request, or cut a release.

## Reporting issues

- **Bugs** — open a [GitHub issue](https://github.com/alexeyshockov/localpost.py/issues/new) with a
  minimal reproducer, the Python and `localpost` version, and the AnyIO backend (`asyncio` or
  `trio`).
- **Feature requests / design discussion** — open an issue first so we can agree on scope before
  you write code.
- **Questions** — open a [GitHub
  Discussion](https://github.com/alexeyshockov/localpost.py/discussions) or an issue tagged
  `question`.

## Development setup

Requirements: Python 3.12+ and [`uv`](https://docs.astral.sh/uv/) (which manages Python itself,
the virtualenv, and dependencies). [`just`](https://github.com/casey/just) is used to wrap the
common workflows.

```bash
git clone https://github.com/alexeyshockov/localpost.py.git
cd localpost.py
just deps         # uv sync --all-groups --all-extras
just unit-tests   # confirm the toolchain is healthy
```

The full list of recipes is in `justfile` (run `just --list`). The most common ones:

| Command | What it does |
| --- | --- |
| `just deps` | Sync the venv with every group + extra. |
| `just deps-upgrade` | `uv lock --upgrade` and re-sync. |
| `just format` | `ruff check --fix` + `ruff format` on `localpost/`. |
| `just types` | `ty check localpost`. |
| `just type-coverage` | `basedpyright --verifytypes` on the public API. |
| `just unit-tests` | `pytest -m "not integration"` with coverage. |
| `just integration-tests` | `pytest -m "integration" -n auto` (testcontainers). |
| `just tests` | Both suites. |
| `just check FILE` | Ruff + ty on a single file — handy in tight edit loops. |
| `just docs-serve` | Live-reloading docs site (Zensical). |

After editing any file under `localpost/`, run `just check <file>` to catch lint and type
regressions early.

## Project structure

```
localpost/
├── hosting/      # Service lifecycle + orchestration (start/stop/signals)
├── scheduler/    # In-process composable task scheduler
├── http/         # Lightweight sync HTTP/1.1 server (h11 / httptools)
├── di/           # .NET-style scoped IoC container
├── openapi/      # Type-driven HTTP framework with OpenAPI 3.2 generation
└── threadtools/  # Sync primitives (TaskGroup, Channel, ...)
```

Files prefixed with `_` are internal; the public API is re-exported from each module's
`__init__.py`. Don't import from a `_`-module outside the module it lives in.

Each module has a README (`localpost/<module>/README.md`) — read those first when working in a
specific area. Cross-cutting design notes live in `docs/`.

## Code style and conventions

- **AnyIO everywhere** — structured concurrency; no raw `asyncio`. Tested on both asyncio and Trio
  backends (`anyio_mode = "auto"` in `pyproject.toml`).
- **Public API is fully typed** and verified with `ty check` and `basedpyright --verifytypes`.
- **Internal code** uses types where they aid readability — not strictly required.
- **Errors as values** — `Result[T]` (Ok / failure) flows through scheduler tasks and arg
  resolvers.
- **Sync + async duality** — the scheduler accepts both; sync callables are offloaded to threads
  via `anyio.to_thread`.
- **Ruff** with `line-length = 120` and `pyupgrade.keep-runtime-typing = true`. Wrap comments and
  docstrings to 120 chars too.
- **Docstrings**: Google convention.

Run `just format` before committing.

## Commits

We use [Conventional Commits](https://www.conventionalcommits.org/) with a module scope. Looking
at recent history is the fastest way to internalise the pattern:

```
feat(scheduler): add cron trigger
fix(threadtools): dedup body and task exceptions in TaskGroup.__exit__
refactor(http): hoist async ctx Protocol + ASGI bridge to localpost.http
docs(scheduler): clarify Task.subscribe buffer semantics
test(http): add wsgi roundtrip cases
chore(deps): bump anyio to 4.12
```

Common types: `feat`, `fix`, `refactor`, `docs`, `test`, `chore`, `perf`. Use `!` after the
scope (e.g. `feat(http)!: ...`) for breaking changes — and add a CHANGELOG entry under
**Changed (BREAKING)**.

Other rules:

- **Never skip hooks on commit** (`--no-verify`). If a hook fails, fix the underlying issue.
- **Prefer a new commit over `--amend`** unless you're correcting the very last commit you just
  made and haven't pushed.

## Pull requests

Before opening:

- [ ] `just format` is clean
- [ ] `just types` passes
- [ ] `just type-coverage` passes (public API stays fully typed)
- [ ] `just unit-tests` passes
- [ ] You added or updated tests for the change
- [ ] You added a CHANGELOG entry under `## [Unreleased]` (Added / Changed / Fixed / Removed,
      with breaking changes called out)
- [ ] Module README and any relevant `docs/` notes are updated

The `main` branch is the only stable branch and will never be force-pushed. Any other branch,
including release branches, may be rebased or force-pushed at any time.

## Tests

- **Unit tests** (`just unit-tests`) — fast, hermetic, default suite. Marker: `not integration`.
- **Integration tests** (`just integration-tests`) — `@pytest.mark.integration`, run in parallel
  via `pytest-xdist` (`-n auto`), use testcontainers.

Run a single test with:

```bash
pytest tests/path/to/test.py::test_function -v
```

Async tests pick up `anyio_mode = "auto"` from `pyproject.toml` — no fixtures needed.

## Documentation

- Module READMEs (`localpost/<module>/README.md`) are the canonical end-user docs and are surfaced
  on the docs site verbatim.
- Cross-cutting design notes live under `docs/design/`.
- Architectural decisions live under `docs/adr/`.
- The site is built with [Zensical](https://zensical.com/); preview locally with `just docs-serve`.

## Release process (maintainers)

The release pipeline is automated via two `just` recipes plus a GitHub Actions workflow.

### One-time setup

- `gh` CLI authenticated against this repo (`gh auth status`).
- Push access to `main`.
- The PyPI project is configured for [trusted
  publishing](https://docs.pypi.org/trusted-publishers/) from
  `.github/workflows/pypi-publish.yaml` (no API tokens stored anywhere).

### Cutting a release

1. **Land the release notes.** On a feature branch, replace `## [Unreleased]` in `CHANGELOG.md`
   with a `## [X.Y.Z] - YYYY-MM-DD` heading and finalise the entries. Open a PR, get it merged
   to `main`.

2. **Run the release recipe** from a clean `main`:

   ```bash
   just release 0.6.0
   ```

   This:
   - bumps `pyproject.toml` (`uv version 0.6.0`),
   - commits `release: 0.6.0`,
   - pushes `main`,
   - creates a **draft** GitHub release `v0.6.0` pre-filled with the `## [0.6.0]` CHANGELOG
     section as its body.

   Guards: aborts if the working tree is dirty, you're not on `main`, or the matching CHANGELOG
   section is missing.

3. **Review and publish the release** on GitHub. Edit notes if needed and click **Publish
   release**. This:
   - creates the `v0.6.0` git tag at the release commit,
   - fires the `release: published` event, which triggers
     [`pypi-publish.yaml`](.github/workflows/pypi-publish.yaml),
   - the workflow runs `uv build` then `uv publish` with PyPI trusted publishing.

4. **Open the next dev cycle:**

   ```bash
   just release-post 0.7.0
   ```

   This bumps `pyproject.toml` to `0.7.0.dev0` and commits/pushes `chore: bump version to
   0.7.0.dev0`. Use the next planned target — `0.6.1` for a patch line, `0.7.0` for the next
   minor, etc.

### Versioning convention

[PEP 440](https://peps.python.org/pep-0440/) with [SemVer](https://semver.org/) on top:

- Released versions: `X.Y.Z`.
- In-development on `main`: `X.Y.Z.dev0` where `X.Y.Z` is the **target** of the next release.
  PEP 440 sorts `0.6.0.dev0 < 0.6.0 < 0.7.0.dev0`, so the suffix marks "heading toward this
  version, not there yet."
- Breaking changes bump the minor pre-1.0 (per SemVer's pre-release allowance) and the major
  post-1.0.
- Patch releases (`X.Y.Z+1`) are reserved for bug fixes that don't change the public API.

### Hotfix releases

Same flow with the patch component bumped — e.g. `just release 0.6.1` from `main` once the fix is
merged, then `just release-post 0.6.2` (or `0.7.0` if the next planned release is a minor).

### Yanking a release

If a release is broken, yank it on PyPI (don't delete — yanking preserves the version number while
discouraging installs). PyPI does not currently expose yank from the command line; do it through
the project's [release management
page](https://pypi.org/manage/project/localpost/releases/) and include a short reason
("broken: see #NNN"). Then cut a follow-up release with the fix as soon as possible.
