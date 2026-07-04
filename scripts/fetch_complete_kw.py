#!/usr/bin/env python3
"""Fetch all current sections for a Polish EKW land-register entry.

This is a secrets-free version of the working EKW fetcher:
- WebShare API token is read only from WEBSHARE_API_TOKEN.
- 2Captcha API key is read only from TWO_CAPTCHA_API_KEY / TWOCAPTCHA_API_KEY.
- No tokens, proxy passwords, cookies, or captcha tokens are written to logs.

Default KW: WA1P/00107308/6. Override with EKW_KW=WA1P/00107308/6 or
EKW_COURT/EKW_NUM/EKW_CHECK.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import random
import re
import time
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from typing import Any, cast

# Playwright/Camoufox browser path must be set before importing Camoufox.
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", os.environ.get("EKW_PLAYWRIGHT_BROWSERS_PATH", "/opt/data/.cache/ms-playwright"))

from camoufox.async_api import AsyncCamoufox

SEARCH_URL = "https://przegladarka-ekw.ms.gov.pl/eukw_prz/KsiegiWieczyste/wyszukiwanieKW"
HCAPTCHA_SITE_KEY = "dd6e16a7-972e-47d2-93d0-96642fb6d8de"
DEFAULT_COUNTRIES = "PL,CZ,SK,AT,DE,NL,DK,FI,HR,HU,IE,IT,CH"
INDICATORS = [
    "Dział I", "Dział II", "Dział III", "Dział IV", "Własność",
    "Oznaczenie nieruchomości", "Przeglądanie treści księgi", "Podrubryka", "Rubryka",
]
SECTION_ORDER = ("DIO", "DIS", "DII", "DIII", "DIV")


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def env_str(name: str, default: str) -> str:
    return cast(str, env(name, default))


def get_kw_parts() -> tuple[str, str, str]:
    kw = env("EKW_KW")
    if kw:
        m = re.fullmatch(r"([A-Z0-9]{4})/(\d{8})/(\d)", kw.strip(), re.I)
        if not m:
            raise SystemExit("EKW_KW must look like WA1P/00107308/6")
        return m.group(1).upper(), m.group(2), m.group(3)
    return env_str("EKW_COURT", "WA1P").upper(), env_str("EKW_NUM", "00107308"), env_str("EKW_CHECK", "6")


KW_COURT, KW_NUM, KW_CHECK = get_kw_parts()
KW = f"{KW_COURT}/{KW_NUM}/{KW_CHECK}"
RUN_ID = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
OUT = pathlib.Path(env_str("EKW_OUTPUT_DIR", "./data/ekw_runs"))
OUT.mkdir(parents=True, exist_ok=True)
LOG = OUT / f"{RUN_ID}_ekw_fetch.log"


def log(message: str) -> None:
    print(message, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(message + "\n")


def require_env(*names: str) -> str:
    for name in names:
        value = env(name)
        if value:
            return value
    joined = " / ".join(names)
    raise SystemExit(f"Missing required environment variable: {joined}")


def http_json(url: str, *, data: dict[str, str] | None = None, headers: dict[str, str] | None = None, timeout: int = 45) -> dict[str, Any]:
    encoded = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=encoded, headers=headers or {})
    if data is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def get_webshare_proxies(limit: int) -> list[dict[str, Any]]:
    token = require_env("WEBSHARE_API_TOKEN")
    plan_id = env("WEBSHARE_PLAN_ID")
    countries = env_str("WEBSHARE_COUNTRIES", DEFAULT_COUNTRIES)
    qs: dict[str, str] = {
        "mode": env_str("WEBSHARE_MODE", "backbone"),
        "country_code__in": countries,
        "page": "1",
        "page_size": str(limit),
        "valid": "true",
    }
    if plan_id:
        qs["plan_id"] = plan_id
    url = "https://proxy.webshare.io/api/v2/proxy/list/?" + urllib.parse.urlencode(qs)
    data = http_json(
        url,
        headers={
            "Authorization": f"Token {token}",
            "Accept": "application/json",
            "X-Webshare-Source": "kw-fetcher",
        },
        timeout=30,
    )
    return data.get("results", [])


def solve_hcaptcha(page_url: str, proxy: dict[str, Any], user_agent: str, timeout: int = 240) -> str:
    """Ask 2Captcha to solve hCaptcha via the same WebShare proxy.

    Deliberately logs only task IDs and token lengths, never the API key, proxy
    password, or captcha token.
    """
    api_key = require_env("TWO_CAPTCHA_API_KEY", "TWOCAPTCHA_API_KEY")
    proxy_auth = f"{proxy['username']}:{proxy['password']}@p.webshare.io:{proxy['port']}"
    task = {
        "key": api_key,
        "method": "hcaptcha",
        "sitekey": HCAPTCHA_SITE_KEY,
        "pageurl": page_url,
        "json": "1",
        "invisible": env("TWO_CAPTCHA_INVISIBLE", "0"),
        "proxytype": "HTTP",
        "proxy": proxy_auth,
        "userAgent": user_agent,
    }
    response = http_json("https://2captcha.com/in.php", data=task)
    if response.get("status") != 1:
        raise RuntimeError(f"2Captcha task creation failed: {response.get('request')}")
    task_id = response["request"]
    log(f"  2Captcha task created: {task_id}")

    start = time.time()
    while time.time() - start < timeout:
        time.sleep(10)
        poll_url = "https://2captcha.com/res.php?" + urllib.parse.urlencode({
            "key": api_key,
            "action": "get",
            "id": task_id,
            "json": "1",
        })
        result = http_json(poll_url)
        if result.get("status") == 1:
            token = result["request"]
            log(f"  2Captcha solved in {int(time.time() - start)}s; token_len={len(token)}")
            return token
        log(f"  2Captcha wait {int(time.time() - start)}s: {result.get('request')}")
    raise TimeoutError("2Captcha timeout")


async def wait_for_search_form(page, timeout_s: int = 40) -> bool:
    for _ in range(timeout_s):
        html = await page.content()
        if "kodWydzialuInput" in html:
            return True
        if "Error 15" in html or "Access Denied" in html or "Incident ID" in html:
            return False
        await asyncio.sleep(1)
    return False


async def all_frame_text(page) -> str:
    texts: list[str] = []
    for frame in page.frames:
        try:
            texts.append(await frame.inner_text("body", timeout=2500))
        except Exception:
            pass
    return "\n".join(texts)


async def find_incapsula_frame(page):
    for frame in page.frames:
        try:
            html = await frame.content()
        except Exception:
            continue
        if "SWCGHOEL" in html or "onCaptchaFinished" in html or "Additional security check" in html:
            return frame, html
    return None, ""


def has_data(text: str) -> bool:
    low = text.lower()
    return any(indicator.lower() in low for indicator in INDICATORS)


async def human_pause(a: float = 0.4, b: float = 1.3) -> None:
    await asyncio.sleep(random.uniform(a, b))


async def save_search_result(page, text: str, proxy_label: str) -> None:
    base = OUT / f"{RUN_ID}_kw_{KW_COURT}_{KW_NUM}_{KW_CHECK}_search_result"
    html = await page.content()
    base.with_suffix(".txt").write_text(text, encoding="utf-8")
    base.with_suffix(".html").write_text(html, encoding="utf-8", errors="ignore")
    base.with_suffix(".json").write_text(json.dumps({
        "kw": KW,
        "fetched_at": datetime.now(UTC).isoformat(),
        "proxy": proxy_label,
        "url": page.url,
        "text": text,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        await page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
    except Exception as exc:
        log(f"  screenshot skipped: {type(exc).__name__}: {str(exc)[:120]}")


async def open_current_content(page) -> None:
    try:
        await page.wait_for_selector("#przyciskWydrukZupelny", timeout=15000)
        log("  Opening complete KW content via #przyciskWydrukZupelny")
        await page.click("#przyciskWydrukZupelny", timeout=15000)
        await page.wait_for_load_state("domcontentloaded", timeout=30000)
        await human_pause(3, 5)
        return
    except Exception as exc:
        log(f"  Button click failed; falling back to POST: {type(exc).__name__}: {str(exc)[:160]}")

    await page.evaluate("""async () => {
        const form = document.querySelector('form#command');
        if (!form) throw new Error('form#command not found');
        const fd = new FormData(form);
        fd.append('przyciskWydrukZupelny', '');
        const response = await fetch(form.action, {
            method: 'POST',
            body: new URLSearchParams(fd),
            credentials: 'include'
        });
        const html = await response.text();
        document.open(); document.write(html); document.close();
    }""")
    await human_pause(3, 5)


async def collect_current_sections(page, proxy_label: str) -> dict[str, Any]:
    sections = await page.evaluate("""async () => {
        const out = [];
        const forms = Array.from(document.querySelectorAll('form[action*="pokazWydruk"]'));
        for (const form of forms) {
            const dzial = form.querySelector('[name="dzialKsiegi"]')?.value || '';
            const label = form.querySelector('input[type="submit"]')?.value || dzial;
            if (!dzial) continue;
            const fd = new FormData(form);
            const response = await fetch(form.action, {
                method: 'POST',
                body: new URLSearchParams(fd),
                credentials: 'include'
            });
            const html = await response.text();
            const doc = new DOMParser().parseFromString(html, 'text/html');
            out.push({dzial, label, status: response.status, url: response.url, html, text: doc.body ? doc.body.innerText : ''});
        }
        return out;
    }""")

    if not sections:
        # The current page may already be one section; save it as DIO fallback.
        sections = [{
            "dzial": "DIO",
            "label": "Dział I-O",
            "status": 200,
            "url": page.url,
            "html": await page.content(),
            "text": await all_frame_text(page),
        }]

    combined: list[str] = []
    clean_sections: list[dict[str, Any]] = []
    for section in sections:
        dzial = section.get("dzial") or "UNKNOWN"
        label = section.get("label") or dzial
        text = section.get("text") or ""
        html = section.get("html") or ""
        sec_base = OUT / f"{RUN_ID}_kw_{KW_COURT}_{KW_NUM}_{KW_CHECK}_{dzial}"
        sec_base.with_suffix(".txt").write_text(text, encoding="utf-8")
        sec_base.with_suffix(".html").write_text(html, encoding="utf-8", errors="ignore")
        combined.append(f"\n\n===== {label} ({dzial}) status={section.get('status')} =====\n{text}")
        clean_sections.append({k: v for k, v in section.items() if k != "html"})

    all_text = "".join(combined).strip()
    base = OUT / f"{RUN_ID}_kw_{KW_COURT}_{KW_NUM}_{KW_CHECK}_ALL_CURRENT"
    base.with_suffix(".txt").write_text(all_text, encoding="utf-8")
    payload = {
        "kw": KW,
        "fetched_at": datetime.now(UTC).isoformat(),
        "proxy": proxy_label,
        "sections": clean_sections,
        "text": all_text,
    }
    base.with_suffix(".json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"  SUCCESS saved all current sections: {base}.json/.txt count={len(sections)} text_len={len(all_text)}")
    return payload


async def try_proxy(index: int, proxy: dict[str, Any]) -> dict[str, Any] | None:
    country = proxy.get("country_code", "??")
    port = proxy.get("port")
    proxy_label = f"{country}:{port}"
    log("\n" + "=" * 72 + f"\nAttempt {index}: {proxy_label}\n" + "=" * 72)

    async with AsyncCamoufox(
        headless=env("CAMOUFOX_HEADLESS", "virtual"),
        humanize=True,
        locale=env("EKW_LOCALE", "pl-PL"),
        geoip=True,
        block_images=False,
        i_know_what_im_doing=True,
        proxy={
            "server": f"http://p.webshare.io:{port}",
            "username": proxy["username"],
            "password": proxy["password"],
        },
    ) as browser:
        page = await browser.new_page()
        await page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=int(env_str("EKW_NAV_TIMEOUT_MS", "50000")))
        if not await wait_for_search_form(page):
            log("  blocked before form")
            return None

        user_agent = await page.evaluate("navigator.userAgent")
        log(f"  form OK; ua={user_agent[:80]}")
        await page.fill("#kodWydzialuInput", KW_COURT)
        await page.fill("[name='numerKw']", KW_NUM)
        await page.fill("[name='cyfraKontrolna']", KW_CHECK)
        await human_pause(0.6, 1.2)
        await page.click("[name='wyszukaj']")
        await asyncio.sleep(8)

        inc_frame, inc_html = await find_incapsula_frame(page)
        if inc_frame:
            match = re.search(r'xhr\.open\("POST",\s*"([^"]*SWCGHOEL[^"]*)"', inc_html)
            if not match:
                log("  hCaptcha frame found but SWCGHOEL endpoint missing")
                return None
            post_url = match.group(1)
            log(f"  Incapsula hCaptcha frame OK; endpoint={post_url[:120]}")
            token = solve_hcaptcha(inc_frame.url, proxy, user_agent)
            result = await inc_frame.evaluate("""async ({token, postUrl}) => {
                for (const ta of document.querySelectorAll('textarea')) {
                    ta.value = token;
                    ta.dispatchEvent(new Event('change', {bubbles: true}));
                }
                const response = await fetch(postUrl, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                    body: 'g-recaptcha-response=' + encodeURIComponent(token),
                    credentials: 'include'
                });
                const text = await response.text().catch(() => '');
                window.parent.location.reload(true);
                return {
                    status: response.status,
                    textHead: text.slice(0, 120),
                    textareaLens: Array.from(document.querySelectorAll('textarea')).map(t => [t.name, (t.value || '').length])
                };
            }""", {"token": token, "postUrl": post_url})
            log(f"  SWCGHOEL result: {json.dumps(result, ensure_ascii=False)}")
            await asyncio.sleep(20)
        else:
            log("  no Incapsula hCaptcha frame after submit; checking result directly")

        # If the verification cookie was set and the form comes back, resubmit once.
        for phase in ("post-verify", "resubmit"):
            text = await all_frame_text(page)
            html = await page.content()
            data_ready = has_data(text)
            form_ready = "kodWydzialuInput" in html
            log(f"  {phase}: text_len={len(text)} html_len={len(html)} form={form_ready} data={data_ready} url={page.url}")
            if data_ready:
                await save_search_result(page, text, proxy_label)
                await open_current_content(page)
                return await collect_current_sections(page, proxy_label)
            if phase == "post-verify" and form_ready:
                await page.fill("#kodWydzialuInput", KW_COURT)
                await page.fill("[name='numerKw']", KW_NUM)
                await page.fill("[name='cyfraKontrolna']", KW_CHECK)
                await page.click("[name='wyszukaj']")
                await asyncio.sleep(20)
                continue
            break

        fail_base = OUT / f"{RUN_ID}_{index}_{country}_{port}_fail"
        fail_base.with_suffix(".html").write_text(await page.content(), encoding="utf-8", errors="ignore")
        fail_base.with_suffix(".txt").write_text(await all_frame_text(page), encoding="utf-8")
        try:
            await page.screenshot(path=str(fail_base.with_suffix(".png")), full_page=True)
        except Exception:
            pass
        return None


async def main() -> int:
    limit = int(env_str("WEBSHARE_PROXY_LIMIT", "8"))
    max_attempts = int(env_str("EKW_MAX_ATTEMPTS", "5"))
    proxies = get_webshare_proxies(limit)
    log(f"Run {RUN_ID}; KW={KW}; proxies={len(proxies)}; max_attempts={max_attempts}")
    if not proxies:
        raise SystemExit("No WebShare proxies returned")

    for index, proxy in enumerate(proxies[:max_attempts], 1):
        try:
            result = await try_proxy(index, proxy)
            if result:
                return 0
        except Exception as exc:
            import traceback
            log(f"  EXCEPTION {type(exc).__name__}: {str(exc)[:400]}")
            log(traceback.format_exc()[-1200:])
        await asyncio.sleep(5)

    log("DONE success=False")
    return 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
