# pip Hardening

Mitigates supply-chain attacks delivered via PyPI. Scoped to the Raspberry Pi 4
voice agent appliance (1 GB RAM, ARM64, piwheels dependency).

---

## Target State

A satisfactorily hardened system exhibits **all** of the following:

- uv refuses packages uploaded fewer than seven days ago (global cooldown)
- pip is barred from installing into the system Python — only virtual environments
  are permitted
- Wheels are preferred over source distributions to avoid executing build scripts
- `pip-audit` is present and clean (zero known CVEs across all installed packages)

---

## Applied on This Pi

### 1. Dependency cooldown — uv (globally enforced)

uv supports a relative cooldown natively in its global config.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh

mkdir -p ~/.config/uv
cat >> ~/.config/uv/uv.toml <<'CONF'
[install]
exclude-newer = "P7D"
CONF
```

> **Cooldown bypass (use sparingly):** to pull a critical security patch released
> within the cooldown window, pin the exact version explicitly and pass
> `--exclude-newer P0D` to uv.

### 2. Virtual-environment enforcement

Prevents pip from modifying the system Python, limiting blast radius if a
malicious package is installed.

```bash
# /etc/profile.d/pip-venv.sh
export PIP_REQUIRE_VIRTUALENV=true
```

### 3. Prefer binary distributions (avoid executing build scripts)

Source distributions run `setup.py` or PEP 517 build scripts on install — a
known execution vector. Prefer pre-built wheels.

```bash
# /etc/pip.conf
[install]
prefer-binary = true
```

For stricter environments where sdists are unacceptable:

```bash
pip install --only-binary=:all: <package>
```

### 4. Vulnerability scanning

```bash
pip install pip-audit
pip-audit                    # scan the active environment
pip-audit --fix              # attempt automatic upgrades for known CVEs
```

---

## Good Practice, Not Applied Here

These are sound general hardening measures that don't work or cause breakage on
this specific hardware.

### Dependency cooldown — pip (`--uploaded-prior-to`)

pip 26.0+ accepts `--uploaded-prior-to` on the CLI. The venv has pip 26.0.1,
so the flag is technically available. However:

- piwheels does not expose upload-time metadata, so the flag causes install
  failures for any package resolved from the piwheels index.
- A shell wrapper (`pip-cooldown.sh`) that injects the flag breaks some pip
  subcommands (e.g. `pip list`, `pip config`).

**When it becomes applicable:** when piwheels adds upload-time metadata, or
when PyPI ARM64 wheel coverage makes piwheels unnecessary. The uv cooldown
covers the gap in the meantime.

### Single trusted index (`no-extra-index-url`)

Dependency-confusion attacks rely on pip checking multiple indexes. The general
recommendation is to set `no-extra-index-url = true` and use only PyPI.

On this Pi, piwheels (`https://www.piwheels.org/simple`) is kept as an
`extra-index-url` because:

- piwheels provides pre-built ARM64 wheels for packages that PyPI may only
  offer as source distributions on aarch64.
- Building from source on 1 GB RAM risks OOM kills, multi-hour compiles, and
  missing system-level build dependencies.
- piwheels is maintained by the Raspberry Pi Foundation — a lower supply-chain
  risk than an arbitrary third-party index.

**When it becomes applicable:** if PyPI's ARM64 wheel coverage catches up to
piwheels, or on Pi hardware with more RAM where source builds are tolerable.

### Hash-verified lock files

Hash verification catches packages silently replaced on PyPI after a lockfile
was generated.

```bash
pip-compile --generate-hashes requirements.in -o requirements.lock
pip install --require-hashes -r requirements.lock
```

Not applied at the provisioning level because there is no `requirements.in` or
`pyproject.toml` under `profiling-pi`. Appropriate to adopt per-project once the
voice agent has a stable dependency set — generate the lock from the live venv
with `pip freeze` + `pip-compile --generate-hashes`.

---

## Deployed Configuration

### `/etc/pip.conf`

```ini
[global]
index-url = https://pypi.org/simple
extra-index-url = https://www.piwheels.org/simple

[install]
prefer-binary = true
```

### `/etc/profile.d/pip-venv.sh`

```bash
export PIP_REQUIRE_VIRTUALENV=true
```

### `~/.config/uv/uv.toml` (provisioning user)

```toml
[install]
exclude-newer = "P7D"
```

---

## Verify

```bash
# 1. uv cooldown active
cat ~/.config/uv/uv.toml
# Expected: exclude-newer = "P7D"

# 2. venv enforcement
echo $PIP_REQUIRE_VIRTUALENV
# Expected: true

# 3. pip config
pip config list | grep -E "index|prefer"
# Expected: index-url = https://pypi.org/simple
#           extra-index-url = https://www.piwheels.org/simple
#           install.prefer-binary='true'

# 4. pip-audit clean (run inside the voice venv)
source /home/voice/venv/bin/activate
pip-audit
# Expected: No known vulnerabilities found
# Expected skips: flatbuffers (piwheels version label), torch/torchaudio (+cpu local version)
```

---

## Notes

- **Cooldown is not a substitute for pinning.** An unpinned `pip install requests`
  with a 7-day cooldown still allows any version uploaded more than 7 days ago,
  including previously compromised ones that survived quarantine. Use both.
- **Security patches need a manual bypass.** When a CVE fix ships, it may be
  younger than the cooldown window. Coordinate with `pip-audit --fix` and a
  deliberate override.
- The litellm/telnyx incident (April 2026) demonstrated a ~2.5-hour exposure
  window from malicious upload to PyPI quarantine, and over 119 000 installs of
  the compromised version. A 7-day cooldown would have given zero exposure on
  those machines.
