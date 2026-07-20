# Secrets Scanning

This project uses two layers of plaintext secret detection before code is merged:

- `python3 scripts/prepush/runner.py --quick --ci` (custom scanner plus gitleaks)
- `python3 scripts/security/gate/scan_plaintext_secrets.py --fail-on high`

The custom scanner checks project-specific plaintext patterns such as credential
assignments, bearer authorization headers, private-key markers, and database
connection URLs. It also treats `runtime/logs/` specially: behavior logs are allowed,
but logs must not contain passwords, tokens, API keys, private keys, session IDs,
Authorization headers, cookies, or JWT material.

## Local Setup

`gitleaks` is a required external developer dependency. It is not listed in
`requirements.txt` because it is a standalone CLI, not a Python package.

Install pre-commit and gitleaks, then enable the hooks:

```bash
python3 -m pip install --user pre-commit
GITLEAKS_VERSION=8.30.1
curl -sSfL "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" -o /tmp/gitleaks.tar.gz
tar -xzf /tmp/gitleaks.tar.gz -C /tmp gitleaks
install -m 0755 /tmp/gitleaks ~/.local/bin/gitleaks
export PATH="$HOME/.local/bin:$PATH"
pre-commit install
gitleaks version
```

On macOS, Homebrew is also acceptable:

```bash
brew install gitleaks
```

The local hook fails closed when `gitleaks` is missing so commits cannot
silently skip the generic scanner.

If a new shell cannot find a user-local `gitleaks` install, add this line to
`~/.bashrc` or the active shell startup file:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

The pre-push gitleaks check materializes its candidate files below `/tmp` and
removes that tree when the check ends. Quick and pre-commit runs scan staged,
unstaged, untracked, and canonical deployment documentation. Full runs scan all
tracked product files. The dedicated CI secrets workflow uses full strict mode.
Unchanged bulk historical evidence below `docs/AGENTS/reports/` is omitted from
repeated full scans; a staged, unstaged, or untracked change in that path is
still included. Generated root paths such as `output/` are forbidden outright,
and `.gitleaks.toml` handles remaining runtime exclusions.

Run the checks manually:

```bash
pre-commit run
python3 -m scripts.prepush.checks.secrets_check \
  --mode full \
  --strict \
  --report-json /tmp/hackme_web_gitleaks_report.json
python3 scripts/security/gate/scan_plaintext_secrets.py --fail-on high
```

## Reports

The custom scanner writes masked reports to:

- `/tmp/hackme_web_test_artifacts/reports/security/secrets_scan_report.json`
- `/tmp/hackme_web_test_artifacts/reports/security/secrets_scan_report.md`

CI also uploads its generated `gitleaks_report.json` as an artifact. Reports
must not include complete secret values. Evidence is masked, for example:

- `token field -> <masked>`
- `password field -> <masked>`
- `Authorization: Bearer <redacted>`
- `postgres://<redacted>`

If a real secret has already been committed, rotate the secret immediately.
Deleting only the latest git version is not sufficient because earlier commits,
forks, caches, or CI logs may still contain it.

## Allowlist Policy

Temporary allowlist entries live in `scripts/security/gate/secrets_allowlist.yml`. Each entry
must include:

- `file`
- `line` or `pattern`
- `reason`
- `owner`
- `expiry` in `YYYY-MM-DD`

Expired or incomplete allowlist entries fail the scan. Never allowlist real
private keys, real tokens, real passwords, production database URLs, session
secrets, or JWT signing secrets. Test fixtures should use clearly fake values
and a short-lived allowlist reason when needed.

## Fix Guidance

Passwords must not be stored in plaintext. Store only password hashes generated
with Argon2id or bcrypt.

Tokens, API keys, JWT secrets, and database URLs must live in environment
variables or a secret manager. Repository files should only keep examples such
as `.env.example` placeholders.

Logs must redact sensitive fields before writing them. Examples of acceptable
logged forms are `token field -> <masked>` and `password field -> <masked>`.
