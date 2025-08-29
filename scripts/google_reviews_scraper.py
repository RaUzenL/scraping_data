"""
Google Maps Reviews Scraper (no proxy)
- Uses Selenium + webdriver-manager (auto driver download)
- Start from EITHER a Google Maps *search query* (collect many places) OR a single *place URL*
- For each place, opens the reviews panel, scrolls to load more, expands long texts
- **Deduplicates** using each card's `data-review-id` (best) or fallback `(text, rating)`
- Optional manual CAPTCHA solving with `--allow-captcha --show`
- Saves CSV with columns: business_name, rating, review

Run examples:
  # Search → top 20 places → up to 150 reviews per place
  python google_reviews_scraper.py \
    --maps-search "restaurants in london" \
    --max-places 20 --max-reviews 150 \
    --out london_restaurants_gmaps_reviews.csv --allow-captcha --show

  # Single place URL → up to 200 reviews
  python google_reviews_scraper.py \
    --place-url "https://www.google.com/maps/place/Some+Business" \
    --max-reviews 200 --out one_place.csv --show

Notes:
- Headless by default; pass --show when you want to watch or solve CAPTCHAs.
- Google changes DOM often; selectors may need updates.
"""
from __future__ import annotations

import re
import time
import argparse
from typing import List, Dict, Any, Optional, Set, Tuple

import pandas as pd
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import NoSuchElementException, TimeoutException

# ───────────────────────── Driver setup ─────────────────────────

def init_driver(show: bool = False) -> webdriver.Chrome:
    opts = Options()
    if not show:
        opts.add_argument("--headless=new")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--window-size=1400,2200")
    opts.add_argument(
        "--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=opts)

# ───────────────────── CAPTCHA handling ────────────────────────
CAPTCHA_HINTS = ("unusual traffic", "verify", "captcha", "recaptcha", "are you a robot")

def looks_like_captcha(driver: webdriver.Chrome) -> bool:
    url = (driver.current_url or "").lower()
    if any(k in url for k in ("sorry", "captcha", "verify")):
        return True
    try:
        low = driver.page_source.lower()
    except Exception:
        return False
    return any(k in low for k in CAPTCHA_HINTS)


def wait_user_to_solve_captcha(driver: webdriver.Chrome, timeout_sec: int = 300) -> bool:
    print("⏳ CAPTCHA detected. Please solve it in the visible browser window. Waiting…")
    t0 = time.time()
    while time.time() - t0 < timeout_sec:
        time.sleep(2)
        if not looks_like_captcha(driver):
            print("✅ CAPTCHA cleared. Resuming.")
            return True
    print("⚠️ Timed out waiting for CAPTCHA.")
    return False

# ───────────────────── Helpers & utilities ─────────────────────

def human_pause(t=0.8):
    time.sleep(t)

NAME_SELECTORS = [
    (By.CSS_SELECTOR, 'h1.DUwDvf'),
    (By.CSS_SELECTOR, '[role="heading"][aria-level="1"]'),
]

def get_business_name(driver: webdriver.Chrome) -> str:
    for how, sel in NAME_SELECTORS:
        try:
            el = driver.find_element(how, sel)
            name = el.text.strip()
            if name:
                return name
        except NoSuchElementException:
            continue
    # fallback: title split
    return (driver.title.split(" - ")[0] or "").strip()


def try_click_xpath(driver: webdriver.Chrome, xpath: str) -> bool:
    try:
        el = driver.find_element(By.XPATH, xpath)
        driver.execute_script("arguments[0].click();", el)
        return True
    except NoSuchElementException:
        return False

# Scroll the left results pane to collect places

def collect_places_from_search(driver: webdriver.Chrome, query: str, max_places: int) -> List[str]:
    search_url = f"https://www.google.com/maps/search/{query.replace(' ', '+')}?hl=en"
    driver.get(search_url)

    try:
        WebDriverWait(driver, 25).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, '[role="feed"],[aria-label*="Results" i]'))
        )
    except TimeoutException:
        pass

    links: List[str] = []
    pane = None
    try:
        pane = driver.find_element(By.CSS_SELECTOR, '[role="feed"]')
    except NoSuchElementException:
        pass

    if pane:
        last = 0
        for _ in range(80):
            cards = pane.find_elements(By.CSS_SELECTOR, 'a[href*="/maps/place/"]')
            for a in cards:
                href = a.get_attribute('href')
                if href and '/maps/place/' in href and href not in links:
                    links.append(href)
                    if len(links) >= max_places:
                        return links
            driver.execute_script("arguments[0].scrollTop = arguments[0].scrollHeight;", pane)
            time.sleep(0.6)
            h = driver.execute_script("return arguments[0].scrollHeight;", pane)
            if h == last:
                break
            last = h
    else:
        anchors = driver.find_elements(By.CSS_SELECTOR, 'a[href*="/maps/place/"]')
        for a in anchors:
            href = a.get_attribute('href')
            if href and '/maps/place/' in href and href not in links:
                links.append(href)
                if len(links) >= max_places:
                    break

    return links

# Open the reviews panel on a place page

def open_reviews_panel(driver: webdriver.Chrome):
    if try_click_xpath(driver, '//button[.//span[contains(translate(.,"REVIEWS","reviews"), "reviews")]]'):
        human_pause(1.2)
    elif try_click_xpath(driver, '//a[contains(@href, "reviews")]'):
        human_pause(1.2)
    # candidates for the scrollable area
    cands = driver.find_elements(By.CSS_SELECTOR, 'div[aria-label*="Reviews" i], div.m6QErb.DxyBCb.kA9KIf.dS8AEf.XiKgde')
    if cands:
        return cands[0]
    try:
        panels = driver.find_elements(By.CSS_SELECTOR, 'div[role="region"]')
        if panels:
            return panels[0]
    except Exception:
        pass
    return None

# Scroll a scrollable reviews panel until enough cards loaded

def scroll_reviews_panel(driver: webdriver.Chrome, panel, target_count: int, max_scrolls: int = 200):
    last_height = 0
    stagnant = 0
    for _ in range(max_scrolls):
        driver.execute_script("arguments[0].scrollTop = arguments[0].scrollHeight;", panel)
        time.sleep(0.8)
        new_height = driver.execute_script("return arguments[0].scrollHeight;", panel)
        stagnant = stagnant + 1 if new_height == last_height else 0
        last_height = new_height
        cards = panel.find_elements(By.CSS_SELECTOR, 'div[data-review-id], div[jscontroller="e6Mltc"], div[aria-label*="review" i]')
        if len(cards) >= target_count or stagnant >= 5:
            break

# Expand longer texts

def expand_more_in_panel(driver: webdriver.Chrome, panel):
    btns = panel.find_elements(By.XPATH, './/button[contains(., "More") or contains(., "Read more") or contains(@aria-label, "More")]')
    for b in btns:
        try:
            driver.execute_script("arguments[0].click();", b)
            time.sleep(0.1)
        except Exception:
            pass

# Parse reviews from panel; return minimal schema, dedup via id/text

def parse_reviews_from_panel(panel, business_name: str, seen_keys: Set[Tuple[str, Optional[float]]], seen_ids: Set[str]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    cards = panel.find_elements(By.CSS_SELECTOR, 'div[data-review-id], div[jscontroller="e6Mltc"], div[aria-label*="review" i]')
    for c in cards:
        # Unique id when available
        rid = c.get_attribute('data-review-id')
        if rid and rid in seen_ids:
            continue

        # Rating
        rating = None
        try:
            stars_el = c.find_element(By.CSS_SELECTOR, '[role="img"][aria-label*="star" i]')
            m = re.search(r"([0-9]+(?:\.[0-9]+)?)", stars_el.get_attribute('aria-label') or "")
            if m:
                rating = float(m.group(1))
        except NoSuchElementException:
            try:
                al = c.find_element(By.CSS_SELECTOR, '[aria-label*="star" i]').get_attribute('aria-label')
                m = re.search(r"([0-9]+(?:\.[0-9]+)?)", al or "")
                if m:
                    rating = float(m.group(1))
            except NoSuchElementException:
                pass

        # Text
        text = None
        for sel in ['span.wiI7pd', 'span.MyEned', 'div[jsname="fk8dgd"] span', 'div[aria-label*="review" i] span']:
            try:
                t = c.find_element(By.CSS_SELECTOR, sel).text.strip()
                if t:
                    text = t
                    print(text)
                    break
            except NoSuchElementException:
                continue
        if not text:
            continue

        # Dedup by id or (text, rating)
        if rid:
            seen_ids.add(rid)
        key = (text, rating)
        if key in seen_keys:
            continue
        seen_keys.add(key)

        rows.append({
            'business_name': business_name,
            'rating': rating,
            'review': text,
        })
    return rows

# ───────────────────── Scrape one place (reviews) ──────────────────────────

def scrape_place_reviews(place_url: str, max_reviews: int, show: bool, allow_captcha: bool) -> List[Dict[str, Any]]:
    driver = init_driver(show)
    out: List[Dict[str, Any]] = []
    try:
        driver.get(place_url + ("&hl=en" if "hl=" not in place_url else ""))

        if looks_like_captcha(driver):
            if allow_captcha and show:
                if not wait_user_to_solve_captcha(driver):
                    return []
            else:
                print("🚧 CAPTCHA on place page. Use --allow-captcha --show to solve manually.")
                return []

        business_name = get_business_name(driver) or ""

        panel = open_reviews_panel(driver)
        if not panel:
            print("⚠️ Could not locate Reviews panel.")
            return []

        seen_ids: Set[str] = set()
        seen_keys: Set[Tuple[str, Optional[float]]] = set()

        expand_more_in_panel(driver, panel)
        scroll_reviews_panel(driver, panel, target_count=max_reviews, max_scrolls=max(50, max_reviews // 5))
        expand_more_in_panel(driver, panel)

        rows = parse_reviews_from_panel(panel, business_name, seen_keys, seen_ids)
        if len(rows) > max_reviews:
            rows = rows[:max_reviews]
        out.extend(rows)
        return out
    finally:
        driver.quit()

# ───────── High-level: search→places→each place reviews → CSV ──────────────

def run_pipeline(maps_search: Optional[str], place_url: Optional[str], max_places: int, max_reviews: int, show: bool, allow_captcha: bool) -> List[Dict[str, Any]]:
    if place_url:
        links = [place_url]
    else:
        drv = init_driver(show)
        try:
            links = collect_places_from_search(drv, maps_search, max_places=max_places)
        finally:
            drv.quit()
    print(f"Found {len(links)} place link(s).")

    all_rows: List[Dict[str, Any]] = []
    for i, link in enumerate(links, 1):
        print(f"→ ({i}/{len(links)}) {link}")
        rows = scrape_place_reviews(link, max_reviews=max_reviews, show=show, allow_captcha=allow_captcha)
        print(f"   collected {len(rows)} unique reviews")
        all_rows.extend(rows)
        time.sleep(0.8)
    return all_rows

# ─────────────────────────── Save CSV ──────────────────────────

def save_to_csv(rows: List[Dict[str, Any]], path: str) -> None:
    df = pd.DataFrame(rows, columns=["business_name", "rating", "review"])  # stable order
    # final dedup guard at CSV step as well
    df = df.drop_duplicates(subset=["business_name", "rating", "review"], keep="first").reset_index(drop=True)
    df.to_csv(path, index=False)
    print(f"✅ Saved {len(df)} unique reviews → {path}")

# ───────────────────────────── CLI ────────────────────────────

def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Google Maps reviews → CSV (business, rating, review)")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--maps-search", help="Google Maps search query, e.g. 'restaurants in london'")
    g.add_argument("--place-url", help="Exact Google Maps place URL")

    p.add_argument("--max-places", type=int, default=8, help="How many places to scrape with --maps-search")
    p.add_argument("--max-reviews", type=int, default=150, help="Max reviews to collect per place")
    p.add_argument("--out", default="gmaps_reviews.csv", help="Output CSV path")
    p.add_argument("--show", action="store_true", help="Show browser (disable headless)")
    p.add_argument("--allow-captcha", action="store_true", help="Allow manual CAPTCHA solving and resume")

    args = p.parse_args(argv)

    rows = run_pipeline(
        maps_search=args.maps_search,
        place_url=args.place_url,
        max_places=args.max_places,
        max_reviews=args.max_reviews,
        show=args.show,
        allow_captcha=args.allow_captcha,
    )

    if not rows:
        print("⚠️ No reviews parsed.")
    save_to_csv(rows, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
