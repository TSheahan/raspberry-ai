# pip hardening — applied (2026-04-04)

Summary of changes from `raspberry-ai/profiling-pi/pip-hardening.md`. Judgment calls were taken to avoid breaking Raspberry Pi installs (piwheels) and pip/CLI edge cases.

## Applied

| Item | What |
|------|------|
| **uv cooldown** | `~/.config/uv/uv.toml`: `[install] exclude-newer = "P7D"`; `[pip]` uses PyPI + piwheels (same as system pip). |
| **pip config** | `/etc/pip.conf`: explicit `index-url`, kept `extra-index-url` for piwheels; `[install] prefer-binary = true`. |
| **Venv-only pip** | `/etc/profile.d/pip-venv.sh`: `PIP_REQUIRE_VIRTUALENV=true`. |
| **pip-audit** | Installed in `~/pipecat-agent/venv`; ran `pip-audit --fix` (pygments 2.19.2 → 2.20.0 for CVE-2026-4539). Re-scan: no known CVEs for PyPI-auditable packages. |

## Intentionally not applied

| Item | Why |
|------|-----|
| **pip `--uploaded-prior-to` / bash `pip()` wrapper** | System pip is 25.x; wrapper breaks some subcommands; with piwheels, install fails (no upload-time metadata on that index). |
| **`no-extra-index-url = true`** | Would drop piwheels; high risk of failed/slow ARM builds on Pi. |
| **uv `extra-index-url = []`** | Same as above — kept piwheels in `uv.toml`. |
| **Hash lock generation** | No `requirements.in` / `pyproject` under `profiling-pi`; do per project with `pip-compile --generate-hashes` or `uv lock`. |
| **Weekly cron** | Doc example used `/home/pi/...`; not adapted — add locally if wanted. |

## Verify (when useful)

```bash
cat ~/.config/uv/uv.toml
pip config list | grep -E "index|extra|prefer"
echo $PIP_REQUIRE_VIRTUALENV   # after login or: source /etc/profile.d/pip-venv.sh
source ~/pipecat-agent/venv/bin/activate && pip-audit
```

## Follow-up (separate)

- API key in `~/.bashrc` — handling outside this doc.
