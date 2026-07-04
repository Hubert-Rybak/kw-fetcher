#!/usr/bin/env python3
"""Fetch EKW/KW records and save them in a section-oriented layout.

Output format uses compact suffixes for the five logical register sections:
  WA1P.00107308.6_1o.html / .txt / .json  -> Dział I-O
  WA1P.00107308.6_1s.html / .txt / .json  -> Dział I-Sp
  WA1P.00107308.6_2.html  / .txt / .json  -> Dział II
  WA1P.00107308.6_3.html  / .txt / .json  -> Dział III
  WA1P.00107308.6_4.html  / .txt / .json  -> Dział IV

Additionally writes per-KW ALL.json/TXT and run-level _dzial_1o.json/_dzial_1o.csv
with parcel rows.

Secrets are read only from environment:
  WEBSHARE_API_TOKEN
  TWO_CAPTCHA_API_KEY or TWOCAPTCHA_API_KEY
No proxy passwords, API keys, cookies or captcha tokens are written to output.
"""
from __future__ import annotations

import asyncio
import csv
import json
import os
import pathlib
import random
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

os.environ.setdefault(
    "PLAYWRIGHT_BROWSERS_PATH",
    os.environ.get("EKW_PLAYWRIGHT_BROWSERS_PATH", "/opt/data/.cache/ms-playwright"),
)

from bs4 import BeautifulSoup
from camoufox.async_api import AsyncCamoufox

SEARCH_URL = "https://przegladarka-ekw.ms.gov.pl/eukw_prz/KsiegiWieczyste/wyszukiwanieKW"
HCAPTCHA_SITE_KEY = "dd6e16a7-972e-47d2-93d0-96642fb6d8de"
DEFAULT_COUNTRIES = "PL,CZ,SK,AT,DE,NL,DK,FI,HR,HU,IE,IT,CH"
SHARED_ROAD_KW = os.environ.get("EKW_SHARED_ROAD_KW", "WA1P/00107308/6")
INDICATORS = [
    "Dział I", "Dział II", "Dział III", "Dział IV", "Własność",
    "Oznaczenie nieruchomości", "Przeglądanie treści księgi", "Podrubryka", "Rubryka",
    "Numer księgi wieczystej", "Dział I-O", "Właściciel", "DZIAŁ I-O", "DZIAŁ II",
]
PESEL_RE = re.compile(r"(?<!\d)\d{11}(?!\d)")


def redact_public_identifiers(value: Any) -> Any:
    """Redact PESEL-like identifiers before writing public exports."""
    if isinstance(value, str):
        return PESEL_RE.sub("PESEL_REDACTED", value)
    if isinstance(value, list):
        return [redact_public_identifiers(v) for v in value]
    if isinstance(value, dict):
        return {k: redact_public_identifiers(v) for k, v in value.items()}
    return value


class RecoverableSessionBlocked(RuntimeError):
    """The current browser/proxy session is poisoned; retry KW in a fresh session."""


SECTIONS: list[tuple[str, str, str]] = [
    ("DIO", "Dział I-O", "1o"),
    ("DIS", "Dział I-Sp", "1s"),
    ("DII", "Dział II", "2"),
    ("DIII", "Dział III", "3"),
    ("DIV", "Dział IV", "4"),
]
DIO_TAGS = [
    "Numer działki",
    "Identyfikator działki",
    "Obręb ewidencyjny (numer, nazwa)",
    "Sposób korzystania",
    "Przyłączenie (numer księgi wieczystej, z której odłączono działkę, obszar)",
    "Numer księgi dawnej",
    "Oznaczenie zbioru dokumentów",
    "Przyłączenie (numer księgi wieczystej, z której odłączono działkę)",
    "Przyłączenie (obszar)",
    "Obszar całej nieruchomości",
]


def env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def require_env(*names: str) -> str:
    for n in names:
        v = env(n)
        if v:
            return v
    raise SystemExit("Missing required environment variable: " + " / ".join(names))


def http_json(url: str, *, data: dict[str, str] | None = None, headers: dict[str, str] | None = None, timeout: int = 45) -> dict[str, Any]:
    encoded = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=encoded, headers=headers or {})
    if data is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def safe_slash_kw(kw: str) -> str:
    return kw.replace("/", "_")


def dot_kw(kw: str) -> str:
    return kw.replace("/", ".")


def split_kw(kw: str) -> tuple[str, str, str]:
    m = re.fullmatch(r"([A-Z0-9]{4})/(\d{8})/(\d)", kw.strip().upper())
    if not m:
        raise ValueError(f"Invalid KW: {kw!r}")
    return m.group(1), m.group(2), m.group(3)


def is_kw_not_found_text(text: str) -> bool:
    """True for the official EKW result page saying the KW does not exist."""
    compact = re.sub(r"\s+", " ", (text or "").lower())
    return "księga o numerze" in compact and "nie została odnaleziona" in compact


def read_kw_list() -> list[str]:
    list_file = env("EKW_KW_LIST_FILE")
    kws: list[str] = []
    seen: set[str] = set()
    if list_file:
        text = pathlib.Path(str(list_file)).read_text(encoding="utf-8", errors="ignore")
    else:
        text = "\n".join(p.name.replace("_", "/", 2) for p in pathlib.Path("data/kw/linked_kw").glob("WA1P_*_*"))
    for kw in re.findall(r"[A-Z0-9]{4}/\d{8}/\d", text.upper()):
        if kw not in seen:
            seen.add(kw)
            kws.append(kw)
    if env("EKW_INCLUDE_SHARED", "1") != "0" and SHARED_ROAD_KW not in seen:
        kws.insert(0, SHARED_ROAD_KW)
    return kws


@dataclass
class RunPaths:
    out: pathlib.Path
    log: pathlib.Path
    progress: pathlib.Path


def make_paths() -> RunPaths:
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_root = pathlib.Path(env("EKW_DETAILED_OUTPUT_ROOT", "data/kw") or "data/kw")
    out = out_root
    out.mkdir(parents=True, exist_ok=True)
    return RunPaths(out=out, log=out / f"{run_id}_batch.log", progress=out / "progress.json")

PATHS = make_paths()


def log(msg: str) -> None:
    print(msg, flush=True)
    with PATHS.log.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def kw_output_dir(kw: str) -> pathlib.Path:
    group = "shared_road" if kw == SHARED_ROAD_KW else "linked_kw"
    p = PATHS.out / group / dot_kw(kw)
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_proxies(n: int) -> list[dict[str, Any]]:
    token = require_env("WEBSHARE_API_TOKEN")
    qs = {
        "mode": env("WEBSHARE_MODE", "backbone") or "backbone",
        "country_code__in": env("WEBSHARE_COUNTRIES", DEFAULT_COUNTRIES) or DEFAULT_COUNTRIES,
        "page": "1",
        "page_size": str(n),
        "valid": "true",
    }
    if env("WEBSHARE_PLAN_ID"):
        qs["plan_id"] = env("WEBSHARE_PLAN_ID") or ""
    url = "https://proxy.webshare.io/api/v2/proxy/list/?" + urllib.parse.urlencode(qs)
    data = http_json(url, headers={"Authorization": f"Token {token}", "Accept": "application/json", "X-Webshare-Source": "kw-fetcher"}, timeout=30)
    return data.get("results", [])


def solve_hcaptcha(page_url: str, proxy: dict[str, Any], user_agent: str, timeout: int = 240) -> str:
    api_key = require_env("TWO_CAPTCHA_API_KEY", "TWOCAPTCHA_API_KEY")
    proxy_auth = f"{proxy['username']}:{proxy['password']}@p.webshare.io:{proxy['port']}"
    payload = {
        "key": api_key,
        "method": "hcaptcha",
        "sitekey": HCAPTCHA_SITE_KEY,
        "pageurl": page_url,
        "json": "1",
        "invisible": env("TWO_CAPTCHA_INVISIBLE", "0") or "0",
        "proxytype": "HTTP",
        "proxy": proxy_auth,
        "userAgent": user_agent,
    }
    response = http_json("https://2captcha.com/in.php", data=payload)
    if response.get("status") != 1:
        raise RuntimeError(f"2Captcha task creation failed: {response.get('request')}")
    task_id = response["request"]
    log(f"      2Captcha task={task_id}")
    start = time.time()
    while time.time() - start < timeout:
        time.sleep(10)
        poll_url = "https://2captcha.com/res.php?" + urllib.parse.urlencode({"key": api_key, "action": "get", "id": task_id, "json": "1"})
        result = http_json(poll_url)
        if result.get("status") == 1:
            token = result["request"]
            log(f"      2Captcha solved in {int(time.time() - start)}s; token_len={len(token)}")
            return token
        log(f"      2Captcha wait {int(time.time() - start)}s: {result.get('request')}")
    raise TimeoutError("2Captcha timeout")


async def frame_texts(page) -> str:
    out: list[str] = []
    for fr in page.frames:
        try:
            out.append(await fr.inner_text("body", timeout=2500))
        except Exception:
            pass
    return "\n".join(out)


async def wait_form(page, timeout: int = 45) -> bool:
    for _ in range(timeout):
        html = await page.content()
        if "kodWydzialuInput" in html:
            return True
        if "Error 15" in html or "Access Denied" in html or "Incident ID" in html:
            return False
        await asyncio.sleep(1)
    return False


async def maybe_solve_incap(page, proxy: dict[str, Any], ua: str) -> bool:
    inc = None
    inc_html = ""
    for fr in page.frames:
        try:
            html = await fr.content()
        except Exception:
            continue
        if "SWCGHOEL" in html or "onCaptchaFinished" in html or "Additional security check" in html:
            inc = fr
            inc_html = html
            break
    if not inc:
        return False
    m = re.search(r'xhr\.open\("POST",\s*"([^"]*SWCGHOEL[^"]*)"', inc_html)
    if not m:
        log("      captcha frame found but SWCGHOEL endpoint missing")
        return False
    post_url = m.group(1)
    token = solve_hcaptcha(inc.url, proxy, ua)
    result = await inc.evaluate(
        """async ({tok, postUrl}) => {
            for (const ta of document.querySelectorAll('textarea')) {
                ta.value = tok;
                ta.dispatchEvent(new Event('change', {bubbles:true}));
            }
            const response = await fetch(postUrl, {
                method:'POST',
                headers:{'Content-Type':'application/x-www-form-urlencoded'},
                body:'g-recaptcha-response=' + encodeURIComponent(tok),
                credentials:'include'
            });
            const txt = await response.text().catch(()=>'');
            window.parent.location.reload(true);
            return {status: response.status, textHead: txt.slice(0, 80)};
        }""",
        {"tok": token, "postUrl": post_url},
    )
    log(f"      SWCGHOEL result={json.dumps(result, ensure_ascii=False)}")
    await asyncio.sleep(15)
    return True


def clean_html(html: str) -> str:
    # Remove WAF/browser helper scripts from committed details; leave EKW tables intact.
    soup = BeautifulSoup(html or "", "html.parser")
    for script in soup.find_all("script"):
        txt = script.get_text(" ", strip=True)
        src = script.get("src") or ""
        if "f5_cspm" in txt or "SWCGHOEL" in txt or "_Incapsula_Resource" in src or "Norway-arme-filter" in txt:
            script.decompose()
    return str(soup)


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for script in soup.find_all("script"):
        script.decompose()
    text = soup.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n" if text.strip() else ""


def td_groups(html: str) -> list[list[str]]:
    soup = BeautifulSoup(html or "", "html.parser")
    groups: list[list[str]] = []
    current: list[str] = []
    for td in soup.find_all("td"):
        t = td.get_text(" ", strip=True)
        if t:
            current.append(t)
        elif current:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def extract_dio_rows(html: str, kw: str) -> dict[str, dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    base = dot_kw(kw)
    idx = 1
    for group in td_groups(html):
        if "Numer działki" not in group and "Obszar całej nieruchomości" not in group:
            continue
        row: dict[str, str] = {"id": f"{base}-{idx}"}
        pending: str | None = None
        for cell in group:
            c = cell.strip()
            if pending:
                if pending == "Przyłączenie (numer księgi wieczystej, z której odłączono działkę, obszar)":
                    parts = [p.strip() for p in c.split(",", 1)]
                    row["Numer księgi dawnej"] = parts[0]
                    if len(parts) > 1:
                        row["Przyłączenie (obszar)"] = parts[1]
                elif pending == "Przyłączenie (numer księgi wieczystej, z której odłączono działkę)":
                    row["Numer księgi dawnej"] = c
                else:
                    row[pending] = c
                pending = None
            if c in DIO_TAGS:
                pending = c
        if "Obszar całej nieruchomości" in group and "Numer działki" not in row:
            row.setdefault("Numer działki", "---")
        if len(row) > 1:
            rows[row["id"]] = row
            idx += 1
    return rows


def section_json(kw: str, dzial: str, label: str, suffix: str, url: str, html: str, proxy_label: str) -> dict[str, Any]:
    cleaned = clean_html(html)
    text = html_to_text(cleaned)
    payload: dict[str, Any] = {
        "kw": kw,
        "id": f"{dot_kw(kw)}_{suffix}",
        "dzial": dzial,
        "label": label,
        "suffix": suffix,
        "url": url,
        "fetched_at": datetime.now(UTC).isoformat(),
        "proxy": proxy_label,
        "text": text,
        "td_groups": td_groups(cleaned),
    }
    if dzial == "DIO":
        payload["dzial_1o"] = extract_dio_rows(cleaned, kw)
    return redact_public_identifiers(payload)


async def open_result_content(page) -> bool:
    for selector, label in [
        ("#przyciskWydrukZwykly:not([disabled])", "current-id"),
        ("button[name='przyciskWydrukZwykly']:not([disabled])", "current-name"),
        ("#przyciskWydrukZupelny:not([disabled])", "complete-id"),
        ("button[name='przyciskWydrukZupelny']:not([disabled])", "complete-name"),
    ]:
        try:
            loc = page.locator(selector).first
            if await loc.count() == 0:
                continue
            log(f"      opening via {label}")
            await loc.click(timeout=15000)
            await page.wait_for_load_state("domcontentloaded", timeout=30000)
            await asyncio.sleep(3)
            return True
        except Exception as e:
            log(f"      open button {label} failed: {type(e).__name__}: {str(e)[:120]}")
    return False


async def click_section_and_capture(page, label: str, dzial: str) -> tuple[str, str] | None:
    selectors = [
        f"input[type='submit'][value='{label}']",
        f"input[value='{label}']",
        f"button:has-text('{label}')",
    ]
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            if await loc.count() == 0:
                continue
            await loc.click(timeout=12000)
            await page.wait_for_load_state("domcontentloaded", timeout=30000)
            await asyncio.sleep(1.5)
            return await page.content(), page.url
        except Exception:
            continue

    # Fallback: same-page form POST, only if click selectors fail.
    try:
        data = await page.evaluate(
            """async ({dzial, label}) => {
                const forms = Array.from(document.querySelectorAll('form[action*="pokazWydruk"]'));
                let form = forms.find(f => (f.querySelector('[name="dzialKsiegi"]')?.value || '') === dzial)
                    || forms.find(f => (f.querySelector('input[type="submit"]')?.value || '') === label);
                if (!form) return null;
                const fd = new FormData(form);
                const r = await fetch(form.action, {method:'POST', body:new URLSearchParams(fd), credentials:'include'});
                const h = await r.text();
                return {html:h, url:r.url};
            }""",
            {"dzial": dzial, "label": label},
        )
        if data and data.get("html"):
            return data["html"], data.get("url") or page.url
    except Exception:
        pass
    return None


async def save_kw_sections(page, kw: str, proxy_label: str) -> bool:
    out_dir = kw_output_dir(kw)
    base = dot_kw(kw)
    search_html = clean_html(await page.content())
    (out_dir / f"{base}_search_result.html").write_text(search_html, encoding="utf-8")
    (out_dir / f"{base}_search_result.txt").write_text(
        redact_public_identifiers(html_to_text(search_html)),
        encoding="utf-8",
    )

    if not await open_result_content(page):
        log("      could not open print/current/complete content")
        return False

    sections: list[dict[str, Any]] = []
    for dzial, label, suffix in SECTIONS:
        captured = await click_section_and_capture(page, label, dzial)
        if not captured:
            log(f"      section missing: {label} ({dzial})")
            continue
        html, url = captured
        payload = section_json(kw, dzial, label, suffix, url, html, proxy_label)
        file_base = out_dir / f"{base}_{suffix}"
        pathlib.Path(str(file_base) + ".html").write_text(clean_html(html), encoding="utf-8")
        pathlib.Path(str(file_base) + ".txt").write_text(payload["text"], encoding="utf-8")
        pathlib.Path(str(file_base) + ".json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        sections.append({k: v for k, v in payload.items() if k not in {"td_groups", "text"}} | {"text_len": len(payload["text"]), "td_group_count": len(payload["td_groups"])})
        log(f"      saved {suffix}: text_len={len(payload['text'])} groups={len(payload['td_groups'])}")

    combined_text = "\n\n".join((out_dir / f"{base}_{suffix}.txt").read_text(encoding="utf-8", errors="ignore") for _, _, suffix in SECTIONS if (out_dir / f"{base}_{suffix}.txt").exists())
    all_payload = {
        "kw": kw,
        "fetched_at": datetime.now(UTC).isoformat(),
        "proxy": proxy_label,
        "format": "section-oriented-kw-export",
        "sections": sections,
        "text": combined_text,
    }
    (out_dir / f"{base}_ALL.json").write_text(json.dumps(all_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / f"{base}_ALL.txt").write_text(combined_text, encoding="utf-8")
    ok = len(sections) >= 5 and "DZIAŁ II" in combined_text and len(combined_text) > 1500
    log(f"      KW saved sections={len(sections)} all_text_len={len(combined_text)} ok={ok}")
    return ok


async def save_not_found_page(page, kw: str, proxy_label: str) -> None:
    out_dir = kw_output_dir(kw)
    base = dot_kw(kw)
    html = clean_html(await page.content())
    text = redact_public_identifiers(html_to_text(html))
    (out_dir / f"{base}_NOT_FOUND.html").write_text(html, encoding="utf-8")
    (out_dir / f"{base}_NOT_FOUND.txt").write_text(text, encoding="utf-8")
    (out_dir / f"{base}_NOT_FOUND.json").write_text(
        json.dumps({
            "kw": kw,
            "status": "not_found",
            "fetched_at": datetime.now(UTC).isoformat(),
            "proxy": proxy_label,
            "text": text,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log("      KW not found by EKW")


async def submit_search_form(page) -> None:
    """Submit the EKW search form with human-like fallback paths.

    On some proxy/browser combinations Playwright sees the submit button as
    visible/enabled but waits forever for geometric stability. Try the normal
    click first, then force-click, then Enter from the last field. All paths use
    the real form controls rather than direct HTTP requests so session state and
    anti-bot instrumentation stay browser-native.
    """
    button = page.locator("[name='wyszukaj']")
    try:
        await button.click(timeout=10000)
        return
    except Exception as exc:  # noqa: BLE001
        log(f"      submit normal click failed; fallback force click: {type(exc).__name__}")
    try:
        await button.click(timeout=10000, force=True)
        return
    except Exception as exc:  # noqa: BLE001
        log(f"      submit force click failed; fallback Enter: {type(exc).__name__}")
    await page.focus("[name='cyfraKontrolna']")
    await page.keyboard.press("Enter")


async def fetch_kw(page, kw: str, proxy: dict[str, Any]) -> str:
    court, number, check = split_kw(kw)
    proxy_label = f"{proxy.get('country_code')}:{proxy.get('port')}"
    log(f"    KW {kw}: search")
    await page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=int(env("EKW_NAV_TIMEOUT_MS", "50000") or "50000"))
    if not await wait_form(page):
        log("      no search form / blocked; rotating browser/proxy and retrying this KW")
        raise RecoverableSessionBlocked("no search form before submit")
    ua = await page.evaluate("navigator.userAgent")
    await page.fill("#kodWydzialuInput", court)
    await page.fill("[name='numerKw']", number)
    await page.fill("[name='cyfraKontrolna']", check)
    await asyncio.sleep(random.uniform(0.5, 1.5))
    await submit_search_form(page)
    await asyncio.sleep(6)

    for phase in range(3):
        text = await frame_texts(page)
        html = await page.content()
        has_data = any(x.lower() in text.lower() for x in INDICATORS)
        has_result_button = "przyciskWydrukZwykly" in html or "przyciskWydrukZupelny" in html
        has_form = "kodWydzialuInput" in html
        if is_kw_not_found_text(text):
            await save_not_found_page(page, kw, proxy_label)
            return "not_found"
        if has_result_button or has_data:
            return "ok" if await save_kw_sections(page, kw, proxy_label) else "fail"
        solved = await maybe_solve_incap(page, proxy, ua)
        if solved:
            await asyncio.sleep(8)
            html = await page.content()
            text = await frame_texts(page)
            if is_kw_not_found_text(text):
                await save_not_found_page(page, kw, proxy_label)
                return "not_found"
            if "przyciskWydrukZwykly" in html or "przyciskWydrukZupelny" in html or any(x.lower() in text.lower() for x in INDICATORS):
                return "ok" if await save_kw_sections(page, kw, proxy_label) else "fail"
            if "kodWydzialuInput" in html:
                log("      form returned after captcha; resubmit")
                await page.fill("#kodWydzialuInput", court)
                await page.fill("[name='numerKw']", number)
                await page.fill("[name='cyfraKontrolna']", check)
                await submit_search_form(page)
                await asyncio.sleep(12)
                continue
        if has_form and phase == 0:
            await asyncio.sleep(3)
            continue
        log(f"      no result phase={phase} form={has_form} data={has_data} text_len={len(text)}")
        break

    out_dir = kw_output_dir(kw)
    base = dot_kw(kw)
    fail_html = clean_html(await page.content())
    fail_text = html_to_text(fail_html)
    if is_kw_not_found_text(fail_text):
        await save_not_found_page(page, kw, proxy_label)
        return "not_found"
    fail_has_form = "kodWydzialuInput" in fail_html
    fail_has_data = any(x.lower() in fail_text.lower() for x in INDICATORS)
    if not fail_has_form and not fail_has_data and len(fail_text) < 2500:
        log(f"      poisoned/blocked result body; rotating browser/proxy text_len={len(fail_text)}")
        raise RecoverableSessionBlocked(f"small no-form no-data body text_len={len(fail_text)}")
    (out_dir / f"{base}_FAIL.html").write_text(fail_html, encoding="utf-8")
    (out_dir / f"{base}_FAIL.txt").write_text(fail_text, encoding="utf-8")
    return "fail"


def existing_done(kw: str) -> bool:
    if env("EKW_FORCE_REFETCH", "1") == "1":
        return False
    p = kw_output_dir(kw) / f"{dot_kw(kw)}_ALL.json"
    if not p.exists():
        return False
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return len(data.get("sections") or []) >= 5 and len(data.get("text") or "") > 1500
    except Exception:
        return False


async def run_with_proxy(proxy: dict[str, Any], todo: list[str], status: dict[str, str]) -> None:
    label = f"{proxy.get('country_code')}:{proxy.get('port')}"
    log(f"\n=== proxy {label}; remaining={len(todo)} ===")
    async with AsyncCamoufox(
        headless=env("CAMOUFOX_HEADLESS", "virtual"),
        humanize=True,
        locale=env("EKW_LOCALE", "pl-PL"),
        geoip=True,
        block_images=False,
        i_know_what_im_doing=True,
        proxy={"server": f"http://p.webshare.io:{proxy['port']}", "username": proxy["username"], "password": proxy["password"]},
    ) as browser:
        page = await browser.new_page()
        for kw in list(todo):
            if status.get(kw) in {"ok", "not_found"}:
                continue
            if existing_done(kw):
                status[kw] = "ok"
                PATHS.progress.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
                continue
            try:
                result = await fetch_kw(page, kw, proxy)
            except RecoverableSessionBlocked as e:
                log(f"    KW {kw}: BLOCKED_SESSION {str(e)[:200]}")
                status[kw] = "pending"
                PATHS.progress.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
                raise RuntimeError(f"blocked/poisoned session during {kw}; rotating proxy/browser") from e
            except Exception as e:
                import traceback
                msg = f"{type(e).__name__}: {str(e)[:300]}"
                log(f"    KW {kw}: EXCEPTION {msg}")
                log(traceback.format_exc()[-1200:])
                # If the Playwright/Camoufox driver died, continuing the same proxy/browser
                # causes a fast cascade of fake failures for every remaining KW. Abort the
                # proxy round and let main() rotate to a fresh proxy/browser session.
                if any(marker in msg.lower() for marker in [
                    "connection closed", "pipe closed", "browser has been closed",
                    "target page", "target context", "target browser", "epipe",
                    "timeout", "proxy_bad_gateway", "ns_error_proxy", "proxy connection",
                    "net::err_proxy", "tunnel connection failed", "navigation timeout",
                ]):
                    status[kw] = "pending"
                    PATHS.progress.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
                    raise RuntimeError(f"browser/proxy/session failed during {kw}; rotating proxy/browser") from e
                result = "fail"
            status[kw] = result if result in {"ok", "not_found", "fail"} else "fail"
            PATHS.progress.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
            await asyncio.sleep(random.uniform(3, 8))


def write_run_indexes() -> None:
    dio_rows: dict[str, dict[str, str]] = {}
    section_index: list[dict[str, Any]] = []
    for p in PATHS.out.glob("*/*/*.json"):
        rel = p.relative_to(PATHS.out)
        if "private" in rel.parts or ".pesel." in p.name:
            continue
        if p.name.endswith("_ALL.json") or p.name.endswith("_search_result.json"):
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        section_index.append({
            "kw": data.get("kw"),
            "dzial": data.get("dzial"),
            "label": data.get("label"),
            "suffix": data.get("suffix"),
            "path": str(p.relative_to(PATHS.out)),
            "text_len": len(data.get("text") or ""),
            "td_group_count": len(data.get("td_groups") or []),
        })
        for key, row in (data.get("dzial_1o") or {}).items():
            dio_rows[key] = row | {"kw": data.get("kw") or ""}
    (PATHS.out / "_section_index.json").write_text(json.dumps(section_index, ensure_ascii=False, indent=2), encoding="utf-8")
    (PATHS.out / "_dzial_1o.json").write_text(json.dumps(dio_rows, ensure_ascii=False, indent=1), encoding="utf-8")
    fieldnames = sorted({k for row in dio_rows.values() for k in row.keys()})
    if fieldnames:
        with (PATHS.out / "_dzial_1o.csv").open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames, delimiter=";")
            w.writeheader()
            for row in dio_rows.values():
                w.writerow(row)


async def main() -> int:
    all_kws = read_kw_list()
    if env("EKW_BATCH_LIMIT"):
        all_kws = all_kws[: int(env("EKW_BATCH_LIMIT") or "0")]
    status: dict[str, str] = {}
    if PATHS.progress.exists():
        try:
            existing_status = json.loads(PATHS.progress.read_text(encoding="utf-8"))
            if isinstance(existing_status, dict):
                status.update({str(k): str(v) for k, v in existing_status.items()})
        except Exception:
            status = {}
    for kw in all_kws:
        status.setdefault(kw, "pending")
    PATHS.progress.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    (PATHS.out / "kw_list.txt").write_text("\n".join(all_kws) + "\n", encoding="utf-8")
    log(f"Run out={PATHS.out}; KW count={len(all_kws)}; include_shared={SHARED_ROAD_KW in all_kws}")
    proxies = get_proxies(int(env("WEBSHARE_PROXY_LIMIT", "20") or "20"))
    if not proxies:
        raise SystemExit("no WebShare proxies returned")
    max_proxies = int(env("EKW_MAX_PROXY_ROUNDS", str(len(proxies))) or str(len(proxies)))
    for proxy in proxies[:max_proxies]:
        remaining = [kw for kw in all_kws if status.get(kw) not in {"ok", "not_found"}]
        if not remaining:
            break
        try:
            await run_with_proxy(proxy, remaining, status)
        except Exception as e:
            log(f"proxy failed {proxy.get('country_code')}:{proxy.get('port')} {type(e).__name__}: {str(e)[:300]}")
        write_run_indexes()
        await asyncio.sleep(10)
    write_run_indexes()
    batch_status = {kw: status.get(kw, "pending") for kw in all_kws}
    ok = sum(1 for v in batch_status.values() if v == "ok")
    not_found = sum(1 for v in batch_status.values() if v == "not_found")
    fail = sum(1 for v in batch_status.values() if v == "fail")
    pending = sum(1 for v in batch_status.values() if v == "pending")
    log(f"DONE ok={ok} not_found={not_found} fail={fail} pending={pending} out={PATHS.out}")
    return 0 if ok + not_found == len(all_kws) else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
