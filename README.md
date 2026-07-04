# KW Fetcher

Generic, reusable code for fetching Polish land and mortgage register (Elektroniczne Księgi Wieczyste / EKW) records by KW number.

The main batch fetcher writes one output file set per logical register section: I-O, I-Sp, II, III and IV, plus combined JSON/TXT and run-level indexes. The repository contains only source code and examples — no fetched register data, logs, cookies, browser profiles or API secrets.

## Contents

- `scripts/fetch_ekw_sections.py` — main batch fetcher for KW records; uses WebShare proxy rotation, Camoufox and 2Captcha, and writes outputs under `data/kw/` by default.
- `scripts/fetch_complete_kw.py` — simpler single-KW diagnostic fetcher.
- `scripts/fetch_ekw_playwright.py` — gentle Playwright smoke-test for the EKW search form.
- `examples/kw_list.txt` — example KW input list.

## Installation

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
# If your environment needs browser binaries:
python -m playwright install chromium
```

## Secret configuration

```bash
cp .env.example .env
# Fill WEBSHARE_API_TOKEN and TWO_CAPTCHA_API_KEY locally.
set -a; . ./.env; set +a
```

Required variables:

| Variable | Description |
|---|---|
| `WEBSHARE_API_TOKEN` | WebShare API token used to fetch proxy inventory |
| `TWO_CAPTCHA_API_KEY` / `TWOCAPTCHA_API_KEY` | 2Captcha key used for hCaptcha/Incapsula challenges |

Useful optional variables:

| Variable | Default | Description |
|---|---:|---|
| `EKW_KW_LIST_FILE` | auto-discovery from `data/kw/linked_kw` | Input file with one KW per line |
| `EKW_DETAILED_OUTPUT_ROOT` | `data/kw` | Output root |
| `EKW_BATCH_LIMIT` | unset | Limit number of KWs for a test run |
| `WEBSHARE_PROXY_LIMIT` | `20` | Number of proxies requested from WebShare |
| `EKW_MAX_PROXY_ROUNDS` | number of proxies | Maximum proxy/browser rounds |
| `CAMOUFOX_HEADLESS` | `virtual` | Camoufox headless mode |

## Batch usage

```bash
set -a; . ./.env; set +a
EKW_KW_LIST_FILE=examples/kw_list.txt \
EKW_DETAILED_OUTPUT_ROOT=data/kw \
python scripts/fetch_ekw_sections.py
```

Outputs are written under `data/kw/`, which is ignored by git. For each KW the batch fetcher creates files similar to:

```text
WA1P.00107308.6_1o.txt/json/html
WA1P.00107308.6_1s.txt/json/html
WA1P.00107308.6_2.txt/json/html
WA1P.00107308.6_3.txt/json/html
WA1P.00107308.6_4.txt/json/html
WA1P.00107308.6_ALL.txt/json
```

The batch run also writes `_section_index.json`, `_dzial_1o.json`, `_dzial_1o.csv` and `progress.json`.

## Single-KW smoke test

```bash
set -a; . ./.env; set +a
EKW_KW=WA1P/00107308/6 EKW_OUTPUT_DIR=runs python scripts/fetch_complete_kw.py
```

## Data hygiene

- `.env`, `data/`, `runs/`, `reports/`, HTML/PDF/screenshot outputs, cookies and browser profiles are ignored.
- Scripts log task IDs and token lengths only; they do not write API keys, captcha tokens or proxy passwords to output.
- Public exports from the main batch fetcher redact PESEL-like identifiers.
