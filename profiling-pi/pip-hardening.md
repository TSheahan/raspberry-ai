# pip Hardening

Mitigates supply-chain attacks delivered via PyPI. Applies to the system Python
environment and any project virtual environments on the Pi.

---

## Target State

A satisfactorily hardened system exhibits **all** of the following:

- The package installer (pip ≥ 26.0 or uv) refuses packages uploaded fewer than
  seven days ago — enforced globally, not per-project.
- All production dependency installs use a hash-verified lock file.
- pip is configured with a single trusted index; extra-index-url is disabled.
- Pre-release packages are never resolved automatically.
- `pip-audit` is present and clean (zero known CVEs across all installed packages).
- pip is barred from installing into the system Python — only virtual environments
  are permitted.
- Wheels are preferred over source distributions to avoid executing build scripts
  on install.

---

## Harden

### 1. Dependency cooldown — pip (≥ 26.0, absolute-date method)

pip v26.0 accepts `--uploaded-prior-to` on the CLI; v26.1 will support a
relative period in `pip.conf`. Until v26.1 ships, use a shell alias or wrapper
that derives the absolute cutoff at runtime.

```bash
# Add to /etc/profile.d/pip-cooldown.sh (system-wide) or ~/.bashrc (user)
pip() {
    command pip "$@" --uploaded-prior-to="$(date -d '-7 days' -Idate)"
}
```

Reload the shell after writing the file:

```bash
source /etc/profile.d/pip-cooldown.sh   # or source ~/.bashrc
```

When pip v26.1 is available, replace the alias with a persistent config entry:

```bash
mkdir -p /etc/pip
cat >> /etc/pip/pip.conf <<'CONF'
[install]
uploaded-prior-to = P7D
CONF
```

### 2. Dependency cooldown — uv (preferred; relative period, globally enforced)

uv is the recommended installer on the Pi because it supports a relative cooldown
natively in its global config file.

```bash
# Install uv if not present
curl -LsSf https://astral.sh/uv/install.sh | sh

# Write global uv config
mkdir -p ~/.config/uv
cat >> ~/.config/uv/uv.toml <<'CONF'
[install]
exclude-newer = "P7D"
CONF
```

For each project lock file, regenerate after adding the config:

```bash
uv lock   # resolves only packages uploaded > 7 days ago
git add uv.lock
```

> **Cooldown bypass (use sparingly):** to pull a critical security patch released
> within the cooldown window, pin the exact version explicitly and pass
> `--exclude-newer P0D` to uv or `--uploaded-prior-to P0D` to pip.

### 3. Single trusted index — no extra indexes

Dependency-confusion attacks rely on pip falling back to PyPI when a private
package name appears on the public registry. Remove that fallback entirely.

```bash
# /etc/pip.conf  (or ~/.config/pip/pip.conf for user scope)
[global]
index-url = https://pypi.org/simple
no-extra-index-url = true
```

```bash
# For uv
cat >> ~/.config/uv/uv.toml <<'CONF'
[pip]
index-url = "https://pypi.org/simple"
extra-index-url = []
CONF
```

### 4. Hash-verified lock files

Hash verification catches packages that have been silently replaced on PyPI after
a lockfile was generated (re-upload attacks).

```bash
# Generate a hash-pinned requirements file with pip-tools
pip install pip-tools
pip-compile --generate-hashes requirements.in -o requirements.lock

# Install from it — pip will abort on any hash mismatch
pip install --require-hashes -r requirements.lock
```

With uv, the lock file includes hashes by default:

```bash
uv sync --locked   # fails if uv.lock is stale or hashes have changed
```

### 5. Virtual-environment enforcement

Prevents pip from modifying the system Python, limiting blast radius if a
malicious package is installed.

```bash
# Add to /etc/environment or /etc/profile.d/pip-venv.sh
export PIP_REQUIRE_VIRTUALENV=true
```

### 6. Prefer binary distributions (avoid executing build scripts)

Source distributions (`.tar.gz`) run `setup.py` or PEP 517 build scripts on
install — a known execution vector. Prefer pre-built wheels.

```bash
# /etc/pip.conf — prefer wheels, fall back to sdist only if no wheel is available
[install]
prefer-binary = true
```

For stricter environments where sdists are unacceptable:

```bash
pip install --only-binary=:all: <package>
```

### 7. Vulnerability scanning

```bash
pip install pip-audit
pip-audit                    # scan the active environment
pip-audit --fix              # attempt automatic upgrades for known CVEs
```

Schedule a weekly audit via cron:

```bash
crontab -e
# Add:
# 0 3 * * 1 /home/pi/.venv/bin/pip-audit --output=json > /var/log/pip-audit.json 2>&1
```

---

## Verify

```bash
# 1. Confirm pip version supports uploaded-prior-to
pip --version
# Expected: pip 26.x.x or later

# 2. Confirm cooldown is active — this should resolve an older version or fail
pip install --dry-run requests 2>&1 | grep -i "uploaded"

# 3. Confirm uv global cooldown
cat ~/.config/uv/uv.toml
# Expected: exclude-newer = "P7D"

# 4. Confirm no extra indexes in pip config
pip config list | grep -E "index|extra"
# Expected: only index-url = https://pypi.org/simple
#           no-extra-index-url = true

# 5. Confirm venv enforcement is set
echo $PIP_REQUIRE_VIRTUALENV
# Expected: true

# 6. Confirm pip-audit is present and clean
pip-audit
# Expected: No known vulnerabilities found

# 7. Confirm lock files include hashes
head -5 requirements.lock   # or inspect uv.lock
# Expected: lines contain --hash=sha256:...
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
