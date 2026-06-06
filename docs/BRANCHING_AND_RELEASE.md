# Branching and Release Policy

## Branch Numbering

Feature branches use a two-digit order prefix when they are active development
branches:

```text
main
02-next-feature
03.Economy
```

Rules:

- `main` is the current default main line. The former `01.POINTSCHAIN` branch
  has been merged back into `main` and removed.
- `03.Economy` is reserved for the next economy-model development line.
- Number prefixes represent branch creation / project sequence order, not
  priority.
- Use the next unused number when starting a new feature branch.
- Keep names lowercase and use hyphens after the numeric prefix.
- If a branch already exists without a prefix, rename it before pushing new
  work from that branch.

Current active and historical sequence:

```text
main                       active default main line
02-WebTerminal-docker      abandoned, preserved for history
02-WebTerminal-qemu        abandoned, preserved for history
03.Economy                 active economy-model development line
hackme_web_lite            lightweight target branch for low-end devices
```

## Release ID Rule

The server release ID lives in:

```text
services/platform/release_info.py
```

Every push to a shared branch must increment the last numeric segment by 1
before release. Branch-specific release trains may prefix the date build with
the two-digit branch number, for example `04_` or `05_`.

Example:

```text
04_2026.04.29-016 -> 04_2026.04.29-017
```

Also update visible documentation references:

- `README.md`
- `docs/README.zh-TW.md`
- `docs/For_developer.md`
- `docs/UPDATE_SUMMARY.md`

`docs/UPDATE_SUMMARY.md` is the frontend-visible release note source used by
the root GitHub update center. Keep it concise and operator-facing, because it
is shown after previewing or applying updates from the settings page.
