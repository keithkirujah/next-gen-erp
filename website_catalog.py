"""Read-only website catalogue connectors for NextGen ERP.

Supported sources:
- WooCommerce public Store API (published products only; no credentials needed)
- A public HTTPS JSON feed returning a list or an object with `products`/`items`

The connector only mirrors website catalogue metadata. It never changes local stock,
SKU, purchase cost, or selling prices in the operational inventory tables.
"""
from __future__ import annotations

import json
import sqlite3
import urllib.parse
import urllib.request
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Callable, Iterable

CATALOG_TABLE = "website_catalog_cache"
PAGE_SIZE = 100
MAX_PAGES = 20
MAX_PRODUCTS = PAGE_SIZE * MAX_PAGES
NINETY_NINE_CATALOG_URL = "https://www.99computers.co.ke/catalog.json"
NINETY_NINE_BASE_URL = "https://www.99computers.co.ke/"


def timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def validate_https_url(value: str, *, allow_http: bool = False) -> str:
    """Validate a user-supplied public endpoint and return its normalized URL."""
    value = str(value or "").strip()
    if not value or len(value) > 2000:
        raise ValueError("Enter a website URL or public catalogue feed URL.")
    parsed = urllib.parse.urlsplit(value)
    allowed = {"https"} | ({"http"} if allow_http else set())
    if parsed.scheme.lower() not in allowed or not parsed.hostname:
        scheme_text = "HTTPS" if not allow_http else "HTTP or HTTPS"
        raise ValueError(f"Use a valid {scheme_text} URL.")
    if parsed.username or parsed.password:
        raise ValueError("Do not place usernames or passwords in the website URL.")
    if parsed.port and not (1 <= parsed.port <= 65535):
        raise ValueError("The website URL contains an invalid port.")
    if parsed.fragment:
        raise ValueError("Remove the fragment (#...) from the website URL.")
    return value.rstrip("/") if parsed.path not in ("", "/") else urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def _http_json(url: str, timeout: int = 12):
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "NextGenERP-Catalog/1.0"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        status = getattr(response, "status", 200)
        if status < 200 or status >= 300:
            raise RuntimeError(f"Website returned HTTP {status}.")
        payload = response.read(8 * 1024 * 1024 + 1)
    if len(payload) > 8 * 1024 * 1024:
        raise ValueError("The catalogue response is too large (maximum 8 MB per page).")
    try:
        return json.loads(payload.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("The website response was not valid UTF-8 JSON.") from exc


def _extract_generic_items(payload) -> list:
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = None
        for key in ("products", "items", "data", "results"):
            candidate = payload.get(key)
            if isinstance(candidate, list):
                items = candidate
                break
        if items is None:
            raise ValueError("The JSON feed must be a list or contain a products, items, data, or results list.")
    else:
        raise ValueError("The JSON feed root must be a list or object.")
    if len(items) > MAX_PRODUCTS:
        raise ValueError(f"The feed contains more than the {MAX_PRODUCTS:,}-product safety limit.")
    return items


def _to_text(value, limit=5000) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    return str(value).strip()[:limit]


def _woo_price(item: dict) -> tuple[str, str]:
    prices = item.get("prices") if isinstance(item.get("prices"), dict) else {}
    raw = prices.get("price", item.get("price", item.get("regular_price", "")))
    currency = _to_text(prices.get("currency_code", item.get("currency", "")), 10)
    if raw in (None, ""):
        return "", currency
    try:
        amount = Decimal(str(raw))
        minor_unit = prices.get("currency_minor_unit", 2)
        minor_unit = int(minor_unit)
        if not 0 <= minor_unit <= 6:
            minor_unit = 2
        # WooCommerce Store API exposes `prices.price` in currency minor units.
        # Generic JSON feeds use decimal currency values and must not use this helper.
        amount = amount / (Decimal(10) ** minor_unit)
        if not amount.is_finite() or amount < 0:
            return "", currency
        return format(amount.quantize(Decimal("0.01")), "f"), currency
    except (InvalidOperation, TypeError, ValueError):
        return "", currency


def normalize_woocommerce_product(item) -> dict:
    if not isinstance(item, dict):
        raise ValueError("The website returned a product that is not an object.")
    external_id = _to_text(item.get("id", item.get("sku", item.get("slug", ""))), 200)
    name = _to_text(item.get("name", item.get("title", "")), 300)
    if not external_id or not name:
        raise ValueError("A website product is missing its ID and/or name.")
    price, currency = _woo_price(item)
    categories = item.get("categories", [])
    if isinstance(categories, list):
        category = ", ".join(_to_text(cat.get("name", "") if isinstance(cat, dict) else cat, 100) for cat in categories)
    else:
        category = _to_text(categories, 300)
    images = item.get("images", [])
    image_url = ""
    if isinstance(images, list) and images:
        first = images[0]
        image_url = _to_text(first.get("src", first.get("url", "")) if isinstance(first, dict) else first, 2000)
    if not image_url:
        image_url = _to_text(item.get("image_url", item.get("image", "")), 2000)
    in_stock = item.get("is_in_stock")
    stock_status = _to_text(item.get("stock_status", ""), 80)
    if in_stock is True:
        availability = "In stock"
    elif in_stock is False:
        availability = "Out of stock"
    elif stock_status:
        availability = stock_status.replace("instock", "In stock").replace("outofstock", "Out of stock")
    else:
        availability = "Availability not supplied"
    permalink = _to_text(item.get("permalink", item.get("url", "")), 2000)
    description = _to_text(item.get("short_description", item.get("description", "")), 5000)
    sku = _to_text(item.get("sku", ""), 200)
    return {
        "external_id": external_id, "name": name, "sku": sku,
        "description": description, "price": price, "currency": currency,
        "category": category, "availability": availability,
        "product_url": permalink, "image_url": image_url,
        "brand": _to_text(item.get("brand", ""), 200),
        "sale_price": "", "image_urls": json.dumps([image_url] if image_url else []),
        "website_stock": "",
        "raw_json": json.dumps(item, ensure_ascii=False, separators=(",", ":")),
    }


def _generic_price(value) -> str:
    if value in (None, ""):
        return ""
    try:
        amount = Decimal(str(value).replace(",", "").strip())
        if amount.is_finite() and amount >= 0:
            return format(amount.quantize(Decimal("0.01")), "f")
    except (InvalidOperation, ValueError):
        pass
    return ""


def _normalize_image_list(item: dict, base_url: str = "") -> list[str]:
    raw = item.get("images", [])
    values = []
    if isinstance(raw, list):
        for entry in raw:
            value = entry.get("src", entry.get("url", "")) if isinstance(entry, dict) else entry
            value = _to_text(value, 2000)
            if value:
                values.append(urllib.parse.urljoin(base_url, value) if base_url else value)
    single = _to_text(item.get("image_url", item.get("image", "")), 2000)
    if single:
        values.insert(0, urllib.parse.urljoin(base_url, single) if base_url else single)
    return list(dict.fromkeys(values))[:12]


def normalize_99computers_product(item: dict) -> dict:
    """Normalize the site's own catalog.json shape without writing to the site."""
    if not isinstance(item, dict):
        raise ValueError("99 COMPUTERS catalogue contains a non-object product.")
    external_id = _to_text(item.get("id", ""), 200)
    name = _to_text(item.get("name", ""), 300)
    if not external_id or not name:
        raise ValueError("A 99 COMPUTERS product is missing its ID or name.")
    images = _normalize_image_list(item, NINETY_NINE_BASE_URL)
    regular = _generic_price(item.get("price", ""))
    sale = _generic_price(item.get("sale", ""))
    qty = item.get("qty", item.get("quantity", None))
    try:
        qty_num = Decimal(str(qty)) if qty is not None else None
    except (InvalidOperation, ValueError):
        qty_num = None
    if qty_num is not None:
        availability = "In stock" if qty_num > 0 else "Out of stock"
        website_stock = str(qty)
    else:
        availability = _to_text(item.get("availability", "Availability not supplied"), 80)
        website_stock = ""
    # The site uses `sale` for its promotional selling price; keep it separate from `price`.
    product_url = urllib.parse.urljoin(NINETY_NINE_BASE_URL, "shop/p/" + urllib.parse.quote(external_id, safe=""))
    return {
        "external_id": external_id, "name": name, "sku": _to_text(item.get("sku", ""), 200),
        "description": _to_text(item.get("desc", item.get("description", "")), 5000),
        "price": regular, "currency": "KES", "category": _to_text(item.get("cat", item.get("category", "")), 300),
        "availability": availability, "product_url": product_url,
        "image_url": images[0] if images else "", "brand": _to_text(item.get("brand", ""), 200),
        "sale_price": sale, "image_urls": json.dumps(images, ensure_ascii=False),
        "website_stock": website_stock,
        "raw_json": json.dumps({k: v for k, v in item.items() if k not in {"cost"}}, ensure_ascii=False, separators=(",", ":")),
    }


def normalize_generic_product(item) -> dict:
    if not isinstance(item, dict):
        raise ValueError("Every JSON feed product must be an object.")
    external_id = _to_text(item.get("id", item.get("product_id", item.get("sku", item.get("slug", "")))), 200)
    name = _to_text(item.get("name", item.get("title", item.get("product_name", ""))), 300)
    if not external_id or not name:
        raise ValueError("A JSON feed product is missing its ID/SKU and/or name.")
    raw_price = item.get("price", item.get("regular_price", item.get("selling_price", "")))
    price = ""
    if raw_price not in (None, ""):
        try:
            amount = Decimal(str(raw_price).replace(",", "").strip())
            if amount.is_finite() and amount >= 0:
                price = format(amount.quantize(Decimal("0.01")), "f")
        except (InvalidOperation, ValueError):
            price = ""
    images = item.get("images", [])
    image = item.get("image_url", item.get("image", ""))
    if not image and isinstance(images, list) and images:
        first = images[0]
        image = first.get("src", first.get("url", "")) if isinstance(first, dict) else first
    categories = item.get("categories", item.get("category", ""))
    if isinstance(categories, list):
        categories = ", ".join(_to_text(c.get("name", "") if isinstance(c, dict) else c, 100) for c in categories)
    availability = _to_text(item.get("availability", item.get("stock_status", "Availability not supplied")), 80)
    if item.get("in_stock") is True:
        availability = "In stock"
    elif item.get("in_stock") is False:
        availability = "Out of stock"
    return {
        "external_id": external_id, "name": name, "sku": _to_text(item.get("sku", ""), 200),
        "description": _to_text(item.get("short_description", item.get("description", "")), 5000),
        "price": price, "currency": _to_text(item.get("currency", ""), 10),
        "category": _to_text(categories, 300), "availability": availability,
        "product_url": _to_text(item.get("permalink", item.get("url", "")), 2000),
        "image_url": _to_text(image, 2000),
        "brand": _to_text(item.get("brand", ""), 200),
        "sale_price": _generic_price(item.get("sale_price", item.get("sale", ""))),
        "image_urls": json.dumps(_normalize_image_list(item), ensure_ascii=False),
        "website_stock": _to_text(item.get("qty", item.get("quantity", "")), 80),
        "raw_json": json.dumps(item, ensure_ascii=False, separators=(",", ":")),
    }


def _deduplicate(items: Iterable[dict]) -> list[dict]:
    by_id = {}
    for item in items:
        by_id[item["external_id"]] = item
    if len(by_id) > MAX_PRODUCTS:
        raise ValueError(f"The catalogue exceeds the {MAX_PRODUCTS:,}-product safety limit.")
    return list(by_id.values())


def fetch_catalog(source: str, url: str, *, fetch_json: Callable[[str], object] | None = None) -> list[dict]:
    """Fetch and normalize a public read-only website catalogue."""
    fetch_json = fetch_json or (lambda target: _http_json(target))
    source = str(source or "woocommerce").strip().lower()
    if source == "woocommerce":
        base = validate_https_url(url)
        parsed = urllib.parse.urlsplit(base)
        base_path = parsed.path.rstrip("/")
        endpoint = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, base_path + "/wp-json/wc/store/v1/products", "", ""))
        results = []
        for page in range(1, MAX_PAGES + 1):
            target = endpoint + "?per_page=" + str(PAGE_SIZE) + "&page=" + str(page)
            payload = fetch_json(target)
            if not isinstance(payload, list):
                raise ValueError("WooCommerce did not return a product list. Confirm the site uses WooCommerce and its public Store API is available.")
            results.extend(normalize_woocommerce_product(row) for row in payload)
            if len(results) > MAX_PRODUCTS:
                raise ValueError(f"The catalogue exceeds the {MAX_PRODUCTS:,}-product safety limit.")
            if len(payload) < PAGE_SIZE:
                break
        return _deduplicate(results)
    if source in {"99computers", "99_computers", "site_catalog"}:
        feed_url = validate_https_url(url)
        payload = fetch_json(feed_url)
        if not isinstance(payload, dict) or not isinstance(payload.get("products"), list):
            raise ValueError("99 COMPUTERS catalog.json must contain a products list.")
        return _deduplicate(normalize_99computers_product(row) for row in payload["products"])
    if source in {"json", "json_feed", "generic_json"}:
        feed_url = validate_https_url(url)
        payload = fetch_json(feed_url)
        return _deduplicate(normalize_generic_product(row) for row in _extract_generic_items(payload))
    raise ValueError("Choose WooCommerce public catalogue or Generic JSON feed.")


def initialize_catalog_cache(connection: sqlite3.Connection) -> None:
    connection.execute(f"""CREATE TABLE IF NOT EXISTS {CATALOG_TABLE} (
        source TEXT NOT NULL,
        external_id TEXT NOT NULL,
        name TEXT NOT NULL,
        sku TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL DEFAULT '',
        price TEXT NOT NULL DEFAULT '',
        currency TEXT NOT NULL DEFAULT '',
        category TEXT NOT NULL DEFAULT '',
        availability TEXT NOT NULL DEFAULT '',
        product_url TEXT NOT NULL DEFAULT '',
        image_url TEXT NOT NULL DEFAULT '',
        raw_json TEXT NOT NULL DEFAULT '{{}}',
        updated_at TEXT NOT NULL,
        PRIMARY KEY(source, external_id)
    )""")
    # Forward-compatible cache migration for existing installations.
    existing = {row[1] for row in connection.execute(f"PRAGMA table_info({CATALOG_TABLE})")}
    for column, definition in (("brand", "TEXT NOT NULL DEFAULT ''"), ("sale_price", "TEXT NOT NULL DEFAULT ''"),
                               ("image_urls", "TEXT NOT NULL DEFAULT '[]'"), ("website_stock", "TEXT NOT NULL DEFAULT ''")):
        if column not in existing:
            connection.execute(f"ALTER TABLE {CATALOG_TABLE} ADD COLUMN {column} {definition}")
    connection.commit()


def replace_catalog_cache(connection: sqlite3.Connection, source: str, items: list[dict], updated_at: str | None = None) -> int:
    """Atomically replace one source cache only after a complete successful fetch."""
    initialize_catalog_cache(connection)
    source = str(source or "").strip().lower()
    if source not in {"woocommerce", "json", "json_feed", "generic_json", "99computers", "99_computers", "site_catalog"}:
        raise ValueError("Unsupported catalogue source.")
    canonical_source = "json" if source in {"json_feed", "generic_json"} else ("99computers" if source in {"99_computers", "site_catalog"} else source)
    updated_at = updated_at or timestamp()
    required = {"external_id", "name", "sku", "description", "price", "currency", "category", "availability", "product_url", "image_url", "raw_json"}
    cleaned = []
    for item in items:
        if not isinstance(item, dict) or not required.issubset(item):
            raise ValueError("The fetched catalogue contains an invalid product record; no cached data was changed.")
        cleaned.append((canonical_source, str(item["external_id"]), str(item["name"]), str(item["sku"]),
                        str(item["description"]), str(item["price"]), str(item["currency"]), str(item["category"]),
                        str(item["availability"]), str(item["product_url"]), str(item["image_url"]),
                        str(item.get("brand", "")), str(item.get("sale_price", "")), str(item.get("image_urls", "[]")),
                        str(item.get("website_stock", "")), str(item["raw_json"]), updated_at))
    with connection:
        connection.execute(f"DELETE FROM {CATALOG_TABLE} WHERE source=?", (canonical_source,))
        connection.executemany(f"""INSERT INTO {CATALOG_TABLE}
            (source,external_id,name,sku,description,price,currency,category,availability,product_url,image_url,brand,sale_price,image_urls,website_stock,raw_json,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", cleaned)
    return len(cleaned)


def list_catalog_cache(connection: sqlite3.Connection, query: str = "", limit: int = 2000) -> list:
    initialize_catalog_cache(connection)
    query = str(query or "").strip().casefold()
    limit = max(1, min(int(limit), MAX_PRODUCTS))
    if query:
        needle = "%" + query.replace("%", "\\%").replace("_", "\\_") + "%"
        return connection.execute(f"""SELECT * FROM {CATALOG_TABLE}
            WHERE lower(name) LIKE ? ESCAPE '\\' OR lower(sku) LIKE ? ESCAPE '\\'
               OR lower(category) LIKE ? ESCAPE '\\' OR lower(description) LIKE ? ESCAPE '\\'
            ORDER BY lower(name) LIMIT ?""", (needle, needle, needle, needle, limit)).fetchall()
    return connection.execute(f"SELECT * FROM {CATALOG_TABLE} ORDER BY lower(name) LIMIT ?", (limit,)).fetchall()
