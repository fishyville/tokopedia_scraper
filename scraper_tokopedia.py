import os
import time
import datetime
import re
import json
import random
from urllib.parse import quote, urlsplit, urlunsplit

import pandas as pd
import pyodbc
from dotenv import load_dotenv

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options

from tqdm import tqdm

load_dotenv()

# ============================================================
# CONFIGURATION
# ============================================================

DB_SERVER = os.getenv("DB_SERVER")
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASS = os.getenv("DB_PASS")

# Not in the .env keys you gave me, so it's hardcoded here rather
# than invented as another required env var. If your machine has a
# different ODBC driver installed (check via `odbcinst -j` / ODBC
# Data Source Administrator), change this constant.
ODBC_DRIVER = "{ODBC Driver 17 for SQL Server}"

# Safety cap so the scroll loop can't run forever if the site
# stops returning new valid products before target_count is hit.
MAX_BATCHES = 40


def build_driver():
    """Creates a fresh headless Chrome driver for one scrape run.

    Deliberately NOT a module-level global (unlike the original
    script) - this file can now be called repeatedly / concurrently
    from FastAPI requests, so each run needs its own driver and its
    own state instead of sharing process-wide globals.
    """
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    return webdriver.Chrome(options=chrome_options)


def get_db_connection():
    if not all([DB_SERVER, DB_NAME, DB_USER, DB_PASS]):
        raise RuntimeError(
            "Missing one or more required env vars: "
            "DB_SERVER, DB_NAME, DB_USER, DB_PASS"
        )
    conn_str = (
        f"DRIVER={ODBC_DRIVER};"
        f"SERVER={DB_SERVER};"
        f"DATABASE={DB_NAME};"
        f"UID={DB_USER};"
        f"PWD={DB_PASS};"
    )
    return pyodbc.connect(conn_str)


# ============================================================
# SCROLLING
# ============================================================

def scroll_one_batch(driver, batch_number):
    """
    Runs a single scroll batch (gradual step-down scroll so
    Tokopedia's IntersectionObserver-based lazy load fires) and
    reports back whether new content actually loaded.

    Returns True if the page grew after this batch, False if it
    looks like nothing new loaded.
    """

    print(f"Scroll batch {batch_number + 1}...")

    last_height = driver.execute_script("return document.body.scrollHeight")
    viewport_height = driver.execute_script("return window.innerHeight")
    current_scroll = driver.execute_script("return window.pageYOffset")

    target_scroll = current_scroll + viewport_height * 4

    while current_scroll < target_scroll:
        current_scroll += viewport_height
        driver.execute_script(f"window.scrollTo(0, {current_scroll});")
        time.sleep(0.8)

    try:
        WebDriverWait(driver, 10).until(
            lambda d: d.execute_script("return document.body.scrollHeight") > last_height
        )
        return True
    except Exception:
        return False


# ============================================================
# URL HELPERS
# ============================================================

def normalize_product_url(href):
    """
    Strip Tokopedia's search-session tracking query string
    (extParam, keyword, search_id, src, etc.).

    NOTE: kept from the original script but NOT wired into
    get_candidate_links() below - that's how it was in the "perfect
    code" version you confirmed, so I left it as dead code rather
    than silently changing dedup behavior. Say the word if you want
    it actually applied.
    """
    try:
        parts = urlsplit(href)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except Exception:
        return href


# ============================================================
# EXTRACT PRODUCT DATA
# ============================================================

def get_candidate_links(driver):
    """
    Finds product link elements currently rendered on the page
    and returns a de-duplicated {href: element} dict.
    Filters out shop navigation links (/product, /etalase, /review, etc.).
    """
    links = driver.find_elements(By.CSS_SELECTOR, 'a[href*="tokopedia.com/"]')
    unique_products = {}

    excluded_segments = {
        "search", "about", "help", "promo", "mobile-apps",
        "categories", "cart", "login", "register", "product",
        "etalase", "review", "discussion", "info", "feed"
    }

    for link in links:
        href = link.get_attribute("href")
        if not href:
            continue

        try:
            path = href.split("tokopedia.com/", 1)[-1].split("?")[0]
            segments = [s for s in path.split("/") if s]

            # Product URLs must have at least 2 segments: /shop-slug/product-slug
            if len(segments) >= 2 and segments[1].lower() not in excluded_segments:
                text = link.text.strip()
                if text and href not in unique_products:
                    unique_products[href] = link
        except Exception:
            continue

    return unique_products

def get_or_create_store_id(cursor, shop_name: str, shop_stats: dict, username: str | None = None):
    """
    Checks if a store exists.
    
    shop_name:
        The value to store in Stores.ShopName.
        
    username:
        The actual scraped shop name / username.
    """

    if not shop_name:
        return None

    if username is None:
        username = shop_name[:100]
    else:
        username = username[:100]

    # 1. Search for existing store by ShopName or Username
    cursor.execute(
        "SELECT StoreID FROM dbo.Stores WHERE ShopName = ?",
        shop_name
    )
    row = cursor.fetchone()

    if not row:
        cursor.execute(
            "SELECT StoreID FROM dbo.Stores WHERE Username = ?",
            username
        )
        row = cursor.fetchone()

    # 2. Update if exists
    if row:
        store_id = row[0]

        cursor.execute(
            """
            UPDATE dbo.Stores
            SET FollowersCount = ?,
                TotalProducts = ?,
                Rating = ?,
                LastUpdated = GETDATE()
            WHERE StoreID = ?
            """,
            shop_stats.get("FollowersCount", 0),
            shop_stats.get("TotalProducts", 0),
            shop_stats.get("Rating", 0.0),
            store_id
        )

        return store_id

    # 3. Insert new store
    cursor.execute(
        """
        INSERT INTO dbo.Stores
            (Username, ShopName, FollowersCount, TotalProducts, Rating)
        OUTPUT INSERTED.StoreID
        VALUES (?, ?, ?, ?, ?)
        """,
        username,
        shop_name,
        shop_stats.get("FollowersCount", 0),
        shop_stats.get("TotalProducts", 0),
        shop_stats.get("Rating", 0.0)
    )

    return cursor.fetchone()[0]

def extract_shop_name(href):
    """
    Tokopedia product URLs are shaped like:
    https://www.tokopedia.com/{shop-domain}/{product-slug}...
    The first path segment after the domain is the shop's
    URL-slug, which is the closest thing to ShopName available
    from the listing page (it's not the shop's display name,
    but it's the only shop identifier present here).
    """

    try:
        path = href.split("tokopedia.com/", 1)[-1]
        segment = path.split("/")[0].split("?")[0]
        return segment if segment else None
    except Exception:
        return None


def extract_image_url(link_element):
    try:
        img = link_element.find_element(By.TAG_NAME, "img")
        return img.get_attribute("src")
    except Exception:
        return None


def extract_variants_from_pdp(driver, product_url):
    """
    Visits a Tokopedia Product Detail Page (PDP) and extracts all available
    variants, shop stats, and product total ratings.
    """
    driver.get(product_url)
    variants = []
    resolved_url = product_url
    shop_stats = {"Rating": 0.0, "FollowersCount": 0, "TotalProducts": 0}
    total_ratings = None

    try:
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )
        time.sleep(1.5)

        resolved_url = driver.current_url

        path = resolved_url.split("tokopedia.com", 1)[-1]
        if path in ("", "/") or path.startswith("/search"):
            print(f"WARNING: could not resolve PDP for {product_url} "
                  f"(redirected to {resolved_url})")
            return variants, resolved_url, shop_stats, total_ratings

        try:
            body_text = driver.find_element(By.TAG_NAME, "body").text
            
            # Extract Shop Rating and Followers: e.g., "4.9 (22 rb)"
            rating_match = re.search(r"(\d\.\d)\s*\(([\d.,]+\s*(?:rb|jt)?)\)", body_text, re.IGNORECASE)
            if rating_match:
                shop_stats["Rating"] = float(rating_match.group(1))
                shop_stats["FollowersCount"] = parse_sold_to_number(rating_match.group(2)) or 0
            
            # Extract Shop Total Products: e.g., "1569 total barang"
            products_match = re.search(r"([\d.,]+)\s*total barang", body_text, re.IGNORECASE)
            if products_match:
                raw_num = products_match.group(1).replace(".", "").replace(",", "")
                shop_stats["TotalProducts"] = int(raw_num)

            # NEW: Extract Product Total Ratings: e.g., "(2.777 rating)"
            # Extract Shop Total Products: e.g., "1569 total barang"
            products_match = re.search(r"([\d.,]+)\s*total barang", body_text, re.IGNORECASE)
            if products_match:
                raw_num = products_match.group(1).replace(".", "").replace(",", "")
                shop_stats["TotalProducts"] = int(raw_num)

            # NEW: Extract Product Total Ratings
            # Method 1: Explicit CSS Selector (Most Reliable)
            try:
                rating_elem = driver.find_element(By.CSS_SELECTOR, 'span[data-testid="lblPDPDetailProductRatingCounter"]')
                
                # Grabs the number inside the parentheses, ignoring whatever word comes after it
                rating_match = re.search(r"\(([\d.,]+\s*(?:rb|jt)?)[^)]*\)", rating_elem.text.strip(), re.IGNORECASE)
                if rating_match:
                    total_ratings = parse_sold_to_number(rating_match.group(1))
            except Exception:
                pass
            
            # Method 2: Regex Fallback on body_text (Expanded to catch "ulasan", "penilaian", etc.)
            if total_ratings is None:
                product_rating_match = re.search(
                    r"\(([\d.,]+\s*(?:rb|jt)?)\s*(?:rating|ulasan|penilaian|reviews?)\)", 
                    body_text, 
                    re.IGNORECASE
                )
                if product_rating_match:
                    total_ratings = parse_sold_to_number(product_rating_match.group(1))

        except Exception as e:
            print(f"Could not extract shop/product stats for {product_url}: {e}")

        # JSON state parsing for variants
        pdp_data = driver.execute_script("""
            try {
                if (window.__INITIAL_STATE__) {
                    return window.__INITIAL_STATE__;
                }
                const nextData = document.getElementById('__NEXT_DATA__');
                if (nextData) {
                    return JSON.parse(nextData.textContent);
                }
            } catch (e) {
                return null;
            }
            return null;
        """)

        if pdp_data:
            pdp_layout = pdp_data.get('pdpData', {}) or pdp_data.get('props', {}).get('pageProps', {})
            components = pdp_layout.get('components', [])

            for component in components:
                if component.get('name') == 'variant':
                    children = component.get('data', [{}])[0].get('children', [])
                    for child in children:
                        variants.append({
                            "VariantName": child.get('optionName') or child.get('name'),
                            "Price": int(child.get('price', 0))
                        })
                    if variants:
                        return variants, resolved_url, shop_stats, total_ratings

    except Exception as e:
        print(f"Error reading JSON state for {product_url}: {e}")

    # DOM Fallback for variants
    try:
        variant_elements = driver.find_elements(
            By.CSS_SELECTOR,
            'div[data-testid="pdpVariantContainer"] button, div[data-testid="pdpVariantItem"]'
        )

        for elem in variant_elements:
            v_name = elem.text.strip()
            if not v_name:
                continue

            driver.execute_script("arguments[0].click();", elem)
            time.sleep(1.0)

            try:
                price_elem = driver.find_element(By.CSS_SELECTOR, 'div[data-testid="lblPDPDetailProductPrice"]')
                price_text = price_elem.text.strip()
                parsed_price = parse_price_to_number(price_text)
            except Exception as e:
                parsed_price = None
                print(f"Could not fetch price for variant {v_name}: {e}")

            variants.append({
                "VariantName": v_name,
                "Price": parsed_price,
            })

    except Exception as e:
        print(f"Error fallback parsing for {product_url}: {e}")

    return variants, resolved_url, shop_stats, total_ratings


def extract_shop_and_location_from_html(raw_html):
    """
    Extract shop display name and location from the product card HTML.

    Tokopedia product cards contain shop/location values in spans
    with the 'flip' class.

    Returns:
        (shop_display_name, location)
    """

    if not raw_html:
        return None, None

    try:
        matches = re.findall(
            r'<span[^>]*class=["\'][^"\']*\bflip\b[^"\']*["\'][^>]*>'
            r'(.*?)'
            r'</span>',
            raw_html,
            flags=re.IGNORECASE | re.DOTALL
        )

        values = []

        for value in matches:
            value = re.sub(r"<[^>]+>", "", value)
            value = value.replace("&amp;", "&")
            value = value.replace("&nbsp;", " ")
            value = value.strip()

            if value:
                values.append(value)

        cleaned = list(dict.fromkeys(values))

        if len(cleaned) >= 2:
            return cleaned[0], cleaned[1]

        if len(cleaned) == 1:
            return cleaned[0], None

        return None, None

    except Exception as e:
        print(f"Shop/location extraction error: {e}")
        return None, None


def parse_sold_to_number(sold_str):
    if not sold_str:
        return None
    match = re.search(r"([\d.,]+)\s*(rb|jt)?", sold_str.lower())
    if not match:
        return None
    number_part = match.group(1).replace(".", "").replace(",", ".")
    suffix = match.group(2)
    try:
        value = float(number_part)
    except ValueError:
        return None
    if suffix == "rb":
        value *= 1_000
    elif suffix == "jt":
        value *= 1_000_000
    return int(value)


def parse_price_to_number(price_str):
    """'Rp1.250.000' -> 1250000"""
    if not price_str:
        return None
    digits = re.sub(r"[^\d]", "", price_str)
    return int(digits) if digits else None


def parse_rating_to_float(rating_str):
    """'4.9' -> 4.9"""
    try:
        return float(rating_str)
    except (TypeError, ValueError):
        return None


def parse_product(href, link_element, raw_html=None, global_shop_name=None, global_location=None):
    """
    Parses a single product link element into a raw data dict.
    Handles shop badges, struck-through prices, merged ratings, 
    and applies global shop location/name fallbacks if missing on the card.
    """
    try:
        text = link_element.text.strip()
        if not text:
            return None

        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return None

        price = None
        rating = None
        sold = None

        for line in lines:
            # First encountered price is the current/discounted price
            if re.match(r"^Rp[\d.,]+(\s*-\s*Rp[\d.,]+)?$", line):
                if price is None:
                    price = line

            # Match Sold & Embedded Rating (e.g., "5.0 • 13 terjual" or "3 terjual")
            elif "terjual" in line.lower():
                sold = line
                rating_match = re.search(r"([0-5](?:\.\d)?)\s*•", line)
                if rating_match and not rating:
                    rating = rating_match.group(1)

            # Match Standalone Rating (e.g., "4.9")
            elif re.match(r"^[0-5](\.\d)?$", line):
                if rating is None:
                    rating = line

        noise_patterns = [
            r"^hemat\b.*", r"^cashback\b.*", r"^gratis ongkir.*", r"^bebas ongkir.*",
            r"^bisa cod.*", r"^cod.*", r"^cicilan\b.*", r"^diskon\b.*", r"^official store$",
            r"^power merchant.*", r"^pro merchant.*", r"^star seller$", r"^terlaris$",
            r"^baru$", r"^new$", r"^preorder$", r"^spesial diskon$", r"^garansi.*",
            r"^free.*", r"^\d+(\.\d+)?$", r"^iklan$", r"^ad$", r"^sisa\s*\d+$" # Added iklan, ad, and sisa stock badges
        ]

        # Extract Product Name while skipping badges and noise
        name = None
        for line in lines:
            is_price = line.startswith("Rp")
            is_sold = "terjual" in line.lower()
            is_rating = re.match(r"^[0-5](\.\d)?$", line) or ("•" in line)
            
            # UPDATE: Now catches "12%", ">12%", "<57%", "~20%"
            is_discount = re.match(r"^[><~]?\d+\s*%$", line) 
            
            is_noise = any(re.match(pattern, line.lower()) for pattern in noise_patterns)

            if is_price or is_sold or is_rating or is_discount or is_noise:
                continue

            # Skip lines that match short shop badges or standard words
            if len(line) <= 3:
                continue

            if name is None:
                name = line
                break

        if price is None or name is None:
            return None

        shop_display_name, location = extract_shop_and_location_from_html(raw_html)

        return {
            "name": name,
            "price_raw": price,
            "rating_raw": rating,
            "sold_raw": sold or "0 terjual",
            "details_link": href,
            "image_url": extract_image_url(link_element),
            "shop_name": extract_shop_name(href),
            "shop_display_name": shop_display_name or global_shop_name,
            "location_raw": location or global_location
        }

    except Exception as e:
        print("Error extracting product:", e)
        return None


def extract_data(driver, target_count, seen_links, product_data, global_shop_name=None, global_location=None):
    """
    Scrolls and extracts incrementally, batch by batch, stopping
    as soon as `target_count` valid products have been collected.
    """
    print("\nLoading page...")

    stable_rounds = 0
    max_stable_rounds = 3

    for batch in range(MAX_BATCHES):

        grew = scroll_one_batch(driver, batch)
        time.sleep(1)

        candidates = get_candidate_links(driver)
        new_hrefs = [href for href in candidates if href not in seen_links]

        for href in tqdm(new_hrefs, desc=f"Parsing batch {batch + 1}"):

            seen_links.add(href)
            element = candidates[href]

            try:
                raw_html = element.get_attribute("outerHTML")
            except Exception:
                raw_html = None

            # Pass the global variables down to parse_product
            data = parse_product(href, element, raw_html, global_shop_name, global_location)

            if data is not None:
                product_data.append(data)

            if len(product_data) >= target_count:
                break

        print(f"Collected {len(product_data)}/{target_count} valid products so far...")

        if len(product_data) >= target_count:
            print("Target count reached, stopping scroll.")
            break

        if grew:
            stable_rounds = 0
        else:
            stable_rounds += 1
            if stable_rounds >= max_stable_rounds:
                print("No more products loading, stopping scroll.")
                break

    print(f"Extracted {len(product_data)} products.")


# ============================================================
# DATABASE WRITES
# ============================================================

def insert_item(cursor, item: dict) -> int:
    """
    Checks if an item with the same ItemName and StoreID already exists.
    If it exists, updates its current data and returns the existing ItemCode.
    If not, inserts a new item and returns the newly generated IDENTITY ItemCode.
    """
    # 1. Check if the item already exists for this specific store
    cursor.execute(
        """
        SELECT ItemCode FROM dbo.Items 
        WHERE ItemName = ? AND StoreID = ?
        """,
        item["ItemName"], item["StoreID"]
    )
    row = cursor.fetchone()

    if row:
        item_code = row[0]
        # 2a. Update the existing item with fresh scraped data
        cursor.execute(
            """
            UPDATE dbo.Items
            SET ShopName = ?, Location = ?, RatingStar = ?,
                TotalRatings = ?, TotalSold = ?, ImageURL = ?,
                SourceURL = ?, ScrapedAt = ?
            WHERE ItemCode = ?
            """,
            item["ShopName"], item["Location"], item["RatingStar"],
            item["TotalRatings"], item["TotalSold"], item["ImageURL"],
            item["SourceURL"], item["ScrapedAt"], item_code
        )
        return item_code
    else:
        # 2b. Insert a completely new item
        cursor.execute(
            """
            INSERT INTO dbo.Items
                (ItemName, ShopName, Location, RatingStar,
                 TotalRatings, TotalSold, ImageURL, SourceURL, ScrapedAt, StoreID)
            OUTPUT INSERTED.ItemCode
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            item["ItemName"], item["ShopName"], item["Location"],
            item["RatingStar"], item["TotalRatings"], item["TotalSold"],
            item["ImageURL"], item["SourceURL"], item["ScrapedAt"], item["StoreID"]
        )
        return cursor.fetchone()[0]


def insert_variant(cursor, variant: dict):
    """
    Checks if a variant with the same ItemCode and VariantName exists.
    If it exists, updates the Price. If not, inserts the new variant.
    """
    # 1. Check if this specific variant already exists under the parent ItemCode
    cursor.execute(
        """
        SELECT VariantID FROM dbo.ItemVariants
        WHERE ItemCode = ? AND VariantName = ?
        """,
        variant["ItemCode"], variant["VariantName"]
    )
    row = cursor.fetchone()

    if row:
        # 2a. Update the price of the existing variant
        cursor.execute(
            """
            UPDATE dbo.ItemVariants
            SET Price = ?
            WHERE VariantID = ?
            """,
            variant["Price"], row[0]
        )
    else:
        # 2b. Insert the new variant
        cursor.execute(
            """
            INSERT INTO dbo.ItemVariants
                (ItemCode, VariantName, Price)
            VALUES (?, ?, ?)
            """,
            variant["ItemCode"], variant["VariantName"], variant["Price"]
        )


# ============================================================
# ENTRY POINT (called from main.py)
# ============================================================

def run_scrape(search_type: int, keywords: str, target_count: int) -> dict:
    """
    Runs one full scrape based on search_type:
      - search_type = 1: Search by Keyword
      - search_type = 2: Search by Shop Name
    """

    driver = build_driver()
    product_data = []
    seen_links = set()
    debug_raw_records = []

    items_inserted = 0
    variants_inserted = 0
    unresolved_links = 0

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # Determine URL based on search option
        if search_type == 2:
            raw_input = keywords.strip().rstrip("/")
            if "tokopedia.com/" in raw_input:
                path_part = raw_input.split("tokopedia.com/", 1)[-1]
                slug = path_part.split("/")[0].split("?")[0]
            else:
                slug = raw_input.replace("@", "").strip().lower().replace(" ", "-")

            search_url = f"https://www.tokopedia.com/{slug}/product"
            mode_desc = f"Shop Mode ('{slug}')"
        else:
            search_url = f"https://www.tokopedia.com/search?q={quote(keywords)}&ob=5"
            mode_desc = f"Keyword Mode ('{keywords}')"

        driver.get(search_url)
        print(f"\nOpening {mode_desc}: {search_url}")

        wait = WebDriverWait(driver, 30)
        wait.until(lambda d: d.execute_script("return document.readyState") == "complete")
        time.sleep(3)

        global_shop_name = None
        global_location = None

        # If we are scraping a shop page, pull the header info once globally
        # If we are scraping a shop page, pull the header info once globally
        if search_type == 2:
            try:
                shop_page_details = driver.execute_script("""
                    let result = {
                        name: null,
                        location: null
                    };

                    // ========================================================
                    // 1. FIND SHOP NAME
                    // ========================================================
                    const headings = document.querySelectorAll('h1, h2');

                    for (let h of headings) {
                        const text = (h.innerText || h.textContent || '').trim();

                        if (
                            text &&
                            text.length > 1 &&
                            text.length < 100
                        ) {
                            const rect = h.getBoundingClientRect();

                            // Ignore Tokopedia's top navigation
                            if (rect.top > 60) {
                                result.name = text;
                                break;
                            }
                        }
                    }

                    // ========================================================
                    // 2. FIND LOCATION NEAR SHOP NAME
                    // ========================================================
                    if (result.name) {

                        // Find an element containing the shop name
                        const allElements = document.querySelectorAll(
                            'div, span, p, h1, h2, h3'
                        );

                        let shopNameElement = null;

                        for (let el of allElements) {
                            const text = (el.innerText || el.textContent || '').trim();

                            if (
                                text === result.name &&
                                el.children.length === 0
                            ) {
                                shopNameElement = el;
                                break;
                            }
                        }

                        if (shopNameElement) {

                            // ------------------------------------------------
                            // Check nearby elements in the same parent
                            // ------------------------------------------------
                            let parent = shopNameElement.parentElement;

                            for (let level = 0; level < 4 && parent; level++) {

                                const children = parent.querySelectorAll(
                                    'div, span, p'
                                );

                                for (let el of children) {
                                    const text = (el.innerText || el.textContent || '').trim();

                                    if (!text || text === result.name) {
                                        continue;
                                    }

                                    // Tokopedia location patterns
                                    if (
                                        /^(Kota\s+Administrasi\s+.+)$/i.test(text) ||
                                        /^(Kabupaten\s+.+)$/i.test(text) ||
                                        /^(Kab\.\s+.+)$/i.test(text) ||
                                        /^(Kota\s+.+)$/i.test(text) ||
                                        /^(Jakarta\s+.+)$/i.test(text)
                                    ) {
                                        const rect = el.getBoundingClientRect();

                                        if (
                                            rect.top > 60 &&
                                            rect.top >= shopNameElement.getBoundingClientRect().top
                                        ) {
                                            result.location = text;
                                            break;
                                        }
                                    }
                                }

                                if (result.location) {
                                    break;
                                }

                                parent = parent.parentElement;
                            }
                        }
                    }

                    // ========================================================
                    // 3. FALLBACK: SEARCH ENTIRE PAGE
                    // ========================================================
                    if (!result.location) {

                        const locationRegex =
                            /^(Kota\\s+Administrasi\\s+.+|Kabupaten\\s+.+|Kab\\.\\s+.+|Kota\\s+.+|Jakarta\\s+.+)$/i;

                        const elements = document.querySelectorAll(
                            'span, p, div, b, strong'
                        );

                        for (let el of elements) {

                            // Only inspect leaf-ish elements
                            if (el.children.length > 0) {
                                continue;
                            }

                            const text = (
                                el.innerText ||
                                el.textContent ||
                                ''
                            ).trim();

                            if (!locationRegex.test(text)) {
                                continue;
                            }

                            const rect = el.getBoundingClientRect();

                            // Ignore Tokopedia's delivery location at the top
                            if (rect.top > 60) {
                                result.location = text;
                                break;
                            }
                        }
                    }

                    return result;
                """)

                if shop_page_details:
                    global_shop_name = shop_page_details.get("name")
                    global_location = shop_page_details.get("location")

                    print(
                        f"Header Detected -> "
                        f"Name: {global_shop_name} | "
                        f"Location: {global_location}"
                    )

            except Exception as e:
                print("Could not extract global shop details:", e)

        print("\n" + "=" * 60)
        print(f"Scraping top {target_count} products for {mode_desc}")
        print("=" * 60)

        # Pass global elements down
        extract_data(driver, target_count, seen_links, product_data, global_shop_name, global_location)

        raw_df = pd.DataFrame(product_data)

        if not raw_df.empty:
            raw_df = raw_df.drop_duplicates(subset=["details_link"])
            raw_df = raw_df.reset_index(drop=True)
            raw_df = raw_df.head(target_count)

        scraped_at = datetime.datetime.now()

        print("\nVisiting product pages to extract variants...")

        for _, row in tqdm(raw_df.iterrows(), total=len(raw_df), desc="Extracting PDP Variants"):

            details_url = row["details_link"]

            extracted_variants, resolved_source_url, shop_stats, total_ratings = extract_variants_from_pdp(
                driver, details_url
            )

            resolved_path = resolved_source_url.split("tokopedia.com", 1)[-1]
            if resolved_path in ("", "/") or resolved_path.startswith("/search"):
                unresolved_links += 1

            # Scraped shop name is always used as Username
            shop_name = row["shop_display_name"] or row["shop_name"]

            if not extracted_variants:
                extracted_variants = [{
                    "VariantName": "Default",
                    "Price": parse_price_to_number(row["price_raw"]),
                }]

            try:
                if search_type == 2:
                    username = keywords.strip()
                else:
                    username = None

                store_id = get_or_create_store_id(
                    cursor,
                    shop_name,
                    shop_stats,
                    username
                )

                item_record = {
                    "ItemName": row["name"],
                    "ShopName": shop_name,
                    "Location": row["location_raw"],
                    "RatingStar": parse_rating_to_float(row["rating_raw"]),
                    "TotalRatings": total_ratings,
                    "TotalSold": parse_sold_to_number(row["sold_raw"]),
                    "ImageURL": row["image_url"],
                    "SourceURL": resolved_source_url,
                    "ScrapedAt": scraped_at,
                    "StoreID": store_id,
                }

                new_item_code = insert_item(cursor, item_record)
                for variant in extracted_variants:
                    variant["ItemCode"] = new_item_code
                    insert_variant(cursor, variant)
                
                conn.commit()
                items_inserted += 1
                variants_inserted += len(extracted_variants)
            except Exception as e:
                conn.rollback()
                print(f"DB insert failed for {details_url}: {e}")

            time.sleep(random.uniform(1.5, 3.0))

        now = datetime.datetime.today().strftime("%d-%m-%Y_%H%M%S")
        debug_json = f"debug_raw_html_{now}.json"
        with open(debug_json, "w", encoding="utf-8") as f:
            json.dump(debug_raw_records, f, ensure_ascii=False, indent=4)

        print("\n" + "=" * 60)
        print("SCRAPING COMPLETE")
        print("=" * 60)
        print(f"Items inserted:    {items_inserted}")
        print(f"Variants inserted: {variants_inserted}")
        print(f"Unresolved links:  {unresolved_links}")
        print(f"Debug raw HTML:    {debug_json}")

        return {
            "search_type": search_type,
            "keywords": keywords,
            "target_count": target_count,
            "items_scraped": int(len(raw_df)),
            "items_inserted": items_inserted,
            "variants_inserted": variants_inserted,
            "unresolved_links": unresolved_links,
            "debug_file": debug_json,
        }

    finally:
        cursor.close()
        conn.close()
        driver.quit()