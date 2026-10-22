# 园记自由版 / Yuanji Free

This repository is Yuanji Free, an independently developed third-party open-source
assistant built for the Youshikoudai evaluation system. Keep it independent of any
private activation service, payment backend, signing key or release server.

## Run

- Python 3.10+; the maintained development environment uses Python 3.12.
- Run `python launch.py --setup-only` to create `.venv` and install dependencies.
- Run `python launch.py` to open the desktop app. `--serve` starts a local-only
  service at `http://127.0.0.1:8765` for browser checks.
- Configure the user's own model API through Settings. Do not supply or invent a
  shared key. Do not turn on mock mode for real users.
- Website integration uses the user's own authorized login. Do not automate
  publication, revocation or external messages without the user's instruction.

## Changes and checks

- Preserve session generation, account ownership and class isolation checks.
- Default to manual child selection; optional external services require explicit
  local configuration.
- Tests must use synthetic data and a temporary `KIDOBS_DATA_DIR`. Never open a
  teacher's existing database or photos just to test a change.
- Never commit local settings, cookies, database files, photos, logs, deployment
  credentials, private keys or third-party teaching material without rights.
- The source tree contains no commercial update channel. Do not add one.
- Keep README startup instructions and `.env.example` aligned with code.
