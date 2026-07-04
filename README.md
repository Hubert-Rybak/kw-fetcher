# eKW pobieracz — MapaAspen variant

Czyste repo z kodem użytym do pobierania treści polskich ksiąg wieczystych (EKW) w układzie zbliżonym do `eKW-pobieracz`: osobne pliki dla działów I-O, I-Sp, II, III i IV oraz zbiorcze indeksy JSON/CSV.

Repo **nie zawiera** pobranych ksiąg, raportów, logów, cookie, profili przeglądarki ani sekretów. Klucze API są czytane wyłącznie ze zmiennych środowiskowych.

## Zawartość

- `scripts/fetch_ekw_pobieracz_format.py` — główny batch fetcher używany przy pobieraniu KW; WebShare + Camoufox + 2Captcha, rotacja proxy i zapisy w `data/kw/`.
- `scripts/fetch_complete_kw.py` — prostszy fetcher jednej księgi do diagnostyki/pojedynczego pobrania.
- `scripts/fetch_ekw_playwright.py` — łagodny Playwright smoke-test formularza EKW bez masowego pobierania.
- `examples/kw_list.txt` — przykładowa lista wejściowa.

## Instalacja

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
# jeśli Camoufox/Playwright wymaga instalacji przeglądarek w danym środowisku:
python -m playwright install chromium
```

## Konfiguracja sekretów

```bash
cp .env.example .env
# uzupełnij lokalnie: WEBSHARE_API_TOKEN i TWO_CAPTCHA_API_KEY
set -a; . ./.env; set +a
```

Wymagane:

| Zmienna | Opis |
|---|---|
| `WEBSHARE_API_TOKEN` | token WebShare API do pobierania listy proxy |
| `TWO_CAPTCHA_API_KEY` / `TWOCAPTCHA_API_KEY` | klucz 2Captcha do hCaptcha/Incapsula |

Przydatne opcjonalne:

| Zmienna | Domyślnie | Opis |
|---|---:|---|
| `EKW_KW_LIST_FILE` | auto z `data/kw/linked_kw` | plik z numerami KW, po jednym wierszu |
| `EKW_DETAILED_OUTPUT_ROOT` | `data/kw` | katalog wynikowy |
| `EKW_BATCH_LIMIT` | brak | limit liczby KW do testu |
| `WEBSHARE_PROXY_LIMIT` | `20` | liczba pobieranych proxy |
| `EKW_MAX_PROXY_ROUNDS` | liczba proxy | maks. liczba rund proxy |
| `CAMOUFOX_HEADLESS` | `virtual` | tryb Camoufox |

## Użycie batch

```bash
set -a; . ./.env; set +a
EKW_KW_LIST_FILE=examples/kw_list.txt \
EKW_DETAILED_OUTPUT_ROOT=data/kw \
python scripts/fetch_ekw_pobieracz_format.py
```

Wyniki są tworzone pod `data/kw/` i ignorowane przez git. Dla każdej KW powstają m.in.:

```text
WA1P.00107308.6_1o.txt/json/html
WA1P.00107308.6_1s.txt/json/html
WA1P.00107308.6_2.txt/json/html
WA1P.00107308.6_3.txt/json/html
WA1P.00107308.6_4.txt/json/html
WA1P.00107308.6_ALL.txt/json
```

Dodatkowo batch zapisuje `_section_index.json`, `_dzial_1o.json`, `_dzial_1o.csv` oraz `progress.json`.

## Smoke-test pojedynczej KW

```bash
set -a; . ./.env; set +a
EKW_KW=WA1P/00107308/6 EKW_OUTPUT_DIR=runs python scripts/fetch_complete_kw.py
```

## Higiena danych

- `.env`, `data/`, `runs/`, `reports/`, HTML/PDF/screenshoty, cookie i profile przeglądarki są ignorowane.
- Skrypty logują ID zadań i długości tokenów, ale nie zapisują wartości tokenów API, hCaptcha ani haseł proxy.
- Publiczne eksporty w głównym batch fetcherze redagują PESEL-like identyfikatory.
