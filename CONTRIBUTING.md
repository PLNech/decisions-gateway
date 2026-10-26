# Contributing

Issues and pull requests are welcome.

- Keep the runtime on the Python standard library. Dev tools (pytest, ruff) are fine.
- Run the checks before pushing:

  ```bash
  poetry install
  poetry run pytest -q
  poetry run ruff check . && poetry run ruff format --check .
  ```

- Changes to labels, logging, the store or error messages must keep `tests/test_privacy.py` green without
  weakening it. New label values come from config allow-lists or closed enums, never from request text.
- Changes to the systemd templates must pass `systemd-analyze verify` after `deploy/render-units.sh`, and
  changes to the launchd templates `plutil -lint` after `deploy/render-launchd.sh`, as CI does. Keep the two
  deploy flavours in step: a setting added to one gets its counterpart, or a comment saying there is none.
- New config keys go in `decisions_gateway/config.py`, `deploy/config.example.toml` and the README table.
- One logical change per commit, with a short imperative message. Add a line under `Unreleased` in
  `CHANGELOG.md` for anything a user would notice.

By contributing you agree that your contribution is licensed under the MIT license of this project.
