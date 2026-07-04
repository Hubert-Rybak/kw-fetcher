#!/usr/bin/env python3
"""Próba pobrania KW przez pełną sesję Playwright/Chromium.

Zasady:
- wolne, pojedyncze żądanie testowe; bez równoległości i bez obchodzenia zabezpieczeń,
- pełna przeglądarka z JS i cookies,
- zapisujemy diagnostykę lokalnie w runs/, nie commitujemy treści księgi/PDF.

Użycie:
    PLAYWRIGHT_BROWSERS_PATH=/opt/data/.cache/ms-playwright \
    python3 scripts/fetch_ekw_playwright.py WA1P/00107308/6
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

ENTRY_URL = "https://ekw.ms.gov.pl/"
SEARCH_URL = "https://przegladarka-ekw.ms.gov.pl/eukw_prz/KsiegiWieczyste/wyszukiwanieKW"


def parse_kw(value: str) -> dict[str, str]:
    m = re.fullmatch(r"\s*([A-Z0-9]{4})/(\d{1,8})/(\d)\s*", value.upper())
    if not m:
        raise SystemExit("Niepoprawny format KW. Oczekiwano np. WA1P/00107308/6")
    code, number, check = m.groups()
    return {"code": code, "number": number.zfill(8), "check_digit": check, "normalized": f"{code}/{number.zfill(8)}/{check}"}


async def slow_fill(page, selector: str, value: str) -> None:
    await page.locator(selector).click(timeout=15_000)
    await page.keyboard.type(value, delay=120)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("kw")
    ap.add_argument("--out", default="runs")
    ap.add_argument("--headful", action="store_true", help="Uruchom bez headless, jeśli środowisko ma DISPLAY/xvfb")
    ap.add_argument("--via-menu", action="store_true", help="Zamiast bezpośredniego URL kliknij link z oficjalnej strony głównej")
    ns = ap.parse_args()
    kw = parse_kw(ns.kw)

    root = Path(__file__).resolve().parents[1]
    out = root / ns.out
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    result: dict[str, object] = {
        "timestamp_utc": stamp,
        "kw": kw,
        "entry_url": ENTRY_URL,
        "search_url": SEARCH_URL,
        "steps": [],
        "warnings": [],
        "status": "started",
    }

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=not ns.headful,
            args=["--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            locale="pl-PL",
            timezone_id="Europe/Warsaw",
            viewport={"width": 1366, "height": 900},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
            ),
            accept_downloads=True,
        )
        page = await context.new_page()
        console_messages: list[str] = []
        page.on("console", lambda msg: console_messages.append(f"{msg.type}: {msg.text}"))

        try:
            await page.goto(ENTRY_URL, wait_until="domcontentloaded", timeout=60_000)
            result["steps"].append({"step": "entry_loaded", "url": page.url, "title": await page.title()})
            await page.wait_for_timeout(2500)

            # Jeśli pojawi się baner cookies, akceptujemy zwykłym kliknięciem użytkownika.
            for text in ["Akceptuję", "Akceptuje", "Zgadzam", "OK"]:
                try:
                    btn = page.get_by_text(text, exact=False).first
                    if await btn.count():
                        await btn.click(timeout=2000)
                        result["steps"].append({"step": "cookie_banner_clicked", "text": text})
                        await page.wait_for_timeout(1200)
                        break
                except Exception:
                    pass

            if ns.via_menu:
                # Najłagodniejsza ścieżka: zachowujemy się jak zwykły użytkownik strony głównej
                # i klikamy oficjalny link zamiast przechodzić bezpośrednio na subdomenę.
                await page.get_by_text("Przeglądanie księgi wieczystej", exact=False).first.click(timeout=15_000)
                await page.wait_for_load_state("domcontentloaded", timeout=60_000)
            else:
                await page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=60_000)
            result["steps"].append({"step": "search_loaded", "url": page.url, "title": await page.title(), "via_menu": ns.via_menu})
            await page.wait_for_timeout(5000)

            await page.screenshot(path=str(out / f"{stamp}_playwright_search.png"), full_page=True)
            html = await page.content()
            (out / f"{stamp}_playwright_search.html").write_text(html, encoding="utf-8", errors="ignore")

            incapsula = "_Incapsula_Resource" in html or "Incapsula" in html or "Request unsuccessful" in html
            result["incapsula_or_waf_seen_after_load"] = incapsula

            selectors_present = {
                "kodWydzialuInput": await page.locator("#kodWydzialuInput").count(),
                "numerKw": await page.locator("[name='numerKw']").count(),
                "cyfraKontrolna": await page.locator("[name='cyfraKontrolna']").count(),
                "wyszukaj": await page.locator("[name='wyszukaj']").count(),
            }
            result["selectors_present"] = selectors_present

            if not all(v > 0 for v in selectors_present.values()):
                result["status"] = "blocked_or_form_not_detected"
                result["conclusion"] = "Playwright/Chromium nie zobaczył formularza EKW; prawdopodobnie zatrzymał nas WAF/wyzwanie JS albo zmieniły się selektory."
                return 0

            await slow_fill(page, "#kodWydzialuInput", kw["code"])
            await page.wait_for_timeout(700)
            await slow_fill(page, "[name='numerKw']", kw["number"])
            await page.wait_for_timeout(700)
            await slow_fill(page, "[name='cyfraKontrolna']", kw["check_digit"])
            await page.wait_for_timeout(1200)
            result["steps"].append({"step": "kw_form_filled"})

            await page.locator("[name='wyszukaj']").click(timeout=15_000)
            await page.wait_for_load_state("domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(5000)
            result["steps"].append({"step": "search_submitted", "url": page.url, "title": await page.title()})

            await page.screenshot(path=str(out / f"{stamp}_playwright_result.png"), full_page=True)
            result_html = await page.content()
            (out / f"{stamp}_playwright_result.html").write_text(result_html, encoding="utf-8", errors="ignore")

            body_text = await page.locator("body").inner_text(timeout=10_000)
            (out / f"{stamp}_playwright_result.txt").write_text(body_text, encoding="utf-8", errors="ignore")
            result["result_text_excerpt"] = body_text[:2000]
            result["detected_keywords"] = {
                "numer_ksiegi": "Numer księgi wieczystej" in body_text,
                "wydzial": "wydział" in body_text.lower() or "Wydział" in body_text,
                "brak": "nie została odnaleziona" in body_text.lower() or "brak" in body_text.lower(),
                "captcha": "captcha" in body_text.lower(),
            }

            # Nie klikamy masowo dalej; jeśli są przyciski wydruków, odnotowujemy je.
            for name in ["przyciskWydrukZwykly", "przyciskWydrukZupelny", "przyciskDalej"]:
                result[f"button_{name}_count"] = await page.locator(f"[name='{name}']").count()

            result["status"] = "submitted"
            result["conclusion"] = "Formularz został wypełniony i wysłany; wynik zapisano lokalnie w runs/."
            return 0
        except PlaywrightTimeoutError as e:
            result["status"] = "timeout"
            result["error"] = str(e)
            return 1
        except Exception as e:
            result["status"] = "error"
            result["error"] = repr(e)
            return 1
        finally:
            result["console_messages_tail"] = console_messages[-20:]
            (out / f"{stamp}_playwright_report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(result, ensure_ascii=False, indent=2))
            await context.close()
            await browser.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
