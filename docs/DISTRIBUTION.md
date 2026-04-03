# Distribution Strategy

`xian-cli` is already the canonical operator CLI for Xian. The near-term goal
is to make that obvious in packaging and release UX without rewriting the
command surface in another language.

## Current Decision

- Keep `xian-cli` as the public operator entrypoint.
- Keep backend orchestration in `xian-stack` and deterministic node helpers in
  `xian-abci`.
- Treat a native launcher as optional future packaging work, not as the source
  of truth for operator workflows.

## Why Not Rewrite First

- The operator contract already lives here and is covered by CLI tests.
- The current runtime boundary is still Python-based:
  - `xian-cli` imports setup and genesis helpers from `xian-abci`.
  - `xian-cli` calls the machine-readable `xian-stack/scripts/backend.py`
    wrapper.
- Rewriting the frontend alone would improve install ergonomics, but it would
  not remove the Python- and Docker-based runtime below it.

## Release Shape Now

- Publish `xian-tech-cli` to PyPI.
- Keep `xian` as the installed command name.
- Upload wheel and sdist artifacts to GitHub releases.
- Recommend isolated operator installs through `uv tool install xian-tech-cli`
  or `pipx install xian-tech-cli`.
- Provide bootstrap installer scripts for Unix and PowerShell that choose the
  best available install path on the operator machine.
- Preserve `python -m xian_cli` as a valid fallback entrypoint for debugging and
  packaging environments.

The existing release workflow in `.github/workflows/release.yml` already builds
and publishes PyPI artifacts. The missing work is mainly operator-facing
guidance and consistent packaging expectations.

## Native Launcher Criteria

Consider a thin native launcher only if at least one of these becomes true:

- operator adoption is blocked by Python bootstrap friction
- Homebrew/Scoop/direct-download installs become the primary distribution path
- the backend contract is stable enough that a native frontend can stay thin

If a native launcher is added later, it should:

- shell out to the existing `xian` command or consume the same stable JSON
  backend contract
- avoid re-implementing manifest semantics and operator workflows prematurely
- remain a distribution layer, not a second source of product logic
