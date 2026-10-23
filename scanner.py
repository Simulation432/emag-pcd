
#!/usr/bin/env python3
"""eMAG scanner with sitemap access diagnostics."""

import os
import re
import json
import time
import csv
import gzip
import datetime
import urllib.parse
import urllib.robotparser
import xml.etree.ElementTree as ET
from collections import deque

import requests
from bs4 import BeautifulSoup

HOSTS = (
    "https://www.emag.bg",
    "https://emag.bg"
)

ALLOWED_HOSTS = {"emag.bg", "www.emag.bg"}
UA = "EmagPublicPriceResearch/1.1"

MAX_PRODUCTS = max(0, int(os.getenv("MAX_PRODUCTS", "150")))
MAX_SITEMAPS = max(1, int(os.getenv("MAX_SITEMAPS", "40")))
DELAY = max(2.0, float(os.getenv("REQUEST_DELAY", "5")))

MIN_DIFF = 50.0
MIN_PRICE = 0.0
TIMEOUT = 18

session = requests.Session()
session.headers.update({
    "User-Agent": UA,
    "Accept-Language": "bg-BG,bg;q=0.9"
})

state = {
    "status": "starting",
    "checked": 0,
    "discovered_urls": 0,
    "errors": [],
    "diagnostics": [],
    "updated_utc": None,
    "items": []
}


def record(url, status, message="", final_url=None):
    state["diagnostics"].append({
        "url": url,
        "http_status": status,
        "message": str(message)[:240],
        "final_url": final_url or url
    })


def get(url):
    time.sleep(DELAY)
    response = session.get(
        url,
        timeout=TIMEOUT,
        allow_redirects=True
    )
    if urllib.parse.urlsplit(response.url).hostname not in ALLOWED_HOSTS:
        raise ValueError("Redirect outside emag.bg")
    return response


def robots_for(host):
    url = host + "/robots.txt"
    try:
        response = get(url)
        record(url, response.status_code, "robots.txt", response.url)

        if response.status_code != 200:
            state["errors"].append(
                f"robots.txt {host}: HTTP {response.status_code}"
            )
            return None, []

        rules = urllib.robotparser.RobotFileParser()
        rules.parse(response.text.splitlines())

        if not rules.can_fetch(UA, host + "/"):
            state["errors"].append(
                f"robots.txt does not allow crawling: {host}"
            )
            return None, []

        urls = re.findall(
            r"^\s*Sitemap:\s*(\S+)",
            response.text,
            re.I | re.M
        )

        urls += [
            host + "/sitemap.xml",
            host + "/available_products-index.xml"
        ]

        return rules, list(dict.fromkeys(urls))

    except (requests.RequestException, ValueError) as exc:
        record(url, None, str(exc)[:200])
        state["errors"].append(f"robots failed: {host}")
        return None, []


def amount(text):
    text = re.sub(r"[^\d,.]", "", str(text))
    if not text:
        return None

    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")

    try:
        return float(text)
    except ValueError:
        return None


def eur(value, currency):
    if value is None:
        return None

    if currency.lower().startswith(("лв", "bgn")):
        return round(value / 1.95583, 2)

    return round(value, 2)


def extract(html, url):
    soup = BeautifulSoup(html, "html.parser")
    visible = BeautifulSoup(html, "html.parser")

    for node in visible(["script", "style", "noscript"]):
        node.decompose()

    text = visible.get_text(" ", strip=True)

    match = re.search(
        r"ПЦД\s*:?\s*([\d\s\u00a0.,]+?)\s*(€|EUR|лв\.?|BGN)",
        text,
        re.I
    )

    if not match:
        return None

    pcd = eur(amount(match.group(1)), match.group(2))
    if pcd is None or pcd <= 0:
        return None

    current = None

    for node in soup.find_all("script", type="application/ld+json"):
        try:
            payload = json.loads(node.string or node.get_text())
        except (ValueError, TypeError):
            continue

        stack = [payload]

        while stack:
            obj = stack.pop()

            if isinstance(obj, list):
                stack.extend(obj)

            elif isinstance(obj, dict):
                stack.extend(
                    v for v in obj.values()
                    if isinstance(v, (dict, list))
                )

                if obj.get("priceCurrency") in ("EUR", "BGN") and "price" in obj:
                    val = eur(
                        amount(obj["price"]),
                        obj["priceCurrency"]
                    )

                    if (
                        val is not None
                        and val >= MIN_PRICE
                        and (current is None or val < current)
                    ):
                        current = val

    if current is None:
        match = re.search(
            r"(?:Текуща цена|Специална цена|Цена)"
            r"\s*:?\s*([\d\s\u00a0.,]+?)\s*(€|EUR|лв\.?|BGN)",
            text,
            re.I
        )

        if match:
            current = eur(amount(match.group(1)), match.group(2))

    if current is None or current < MIN_PRICE or current >= pcd:
        return None

    percentage = round(100 * (pcd - current) / pcd, 2)

    if percentage < MIN_DIFF:
        return None

    heading = soup.select_one("h1")

    name = (
        heading.get_text(" ", strip=True)
        if heading
        else (
            soup.title.get_text(" ", strip=True)
            if soup.title else url
        )
    )

    return {
        "name": name[:240],
        "url": url,
        "pcd_eur": pcd,
        "price_eur": current,
        "difference_eur": round(pcd - current, 2),
        "difference_percent": percentage
    }


def save():
    state["updated_utc"] = datetime.datetime.now(
        datetime.timezone.utc
    ).isoformat(timespec="seconds")

    os.makedirs("docs", exist_ok=True)

    with open("docs/results.json", "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)

    with open(
        "docs/results.csv",
        "w",
        encoding="utf-8-sig",
        newline=""
    ) as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "Продукт",
            "Линк",
            "ПЦД €",
            "Цена €",
            "Разлика €",
            "Разлика %"
        ])

        for p in state["items"]:
            writer.writerow([
                p["name"],
                p["url"],
                p["pcd_eur"],
                p["price_eur"],
                p["difference_eur"],
                p["difference_percent"]
            ])

    with open(
        "docs/diagnostics.json",
        "w",
        encoding="utf-8"
    ) as fh:
        json.dump({
            "status": state["status"],
            "diagnostics": state["diagnostics"],
            "errors": state["errors"]
        }, fh, ensure_ascii=False, indent=2)


def main():
    try:
        accessible = []

        for host in HOSTS:
            rules, urls = robots_for(host)

            if rules is not None:
                accessible.append((rules, urls))

        if not accessible:
            state["status"] = "access_blocked"
            return

        queue = deque(
            (url, rules)
            for rules, urls in accessible
            for url in urls
        )

        visited = set()
        products = []
        seen = set()

        while (
            queue
            and len(visited) < MAX_SITEMAPS
            and len(products) < MAX_PRODUCTS * 5
        ):
            url, rules = queue.popleft()

            if url in visited:
                continue

            visited.add(url)

            if urllib.parse.urlsplit(url).hostname not in ALLOWED_HOSTS:
                continue

            if not rules.can_fetch(UA, url):
                record(url, None, "Blocked by robots.txt")
                continue

            try:
                response = get(url)
                record(
                    url,
                    response.status_code,
                    "sitemap",
                    response.url
                )

                if response.status_code in (401, 403, 429, 503, 511):
                    state["errors"].append(
                        f"Sitemap blocked: HTTP {response.status_code}"
                    )
                    continue

                response.raise_for_status()

                if len(response.content) > 15_000_000:
                    continue

                raw = response.content

                if raw[:2] == b"\x1f\x8b":
                    raw = gzip.decompress(raw)

                root = ET.fromstring(raw)

                for node in root.iter():
                    if (
                        node.tag.rsplit("}", 1)[-1] != "loc"
                        or not node.text
                    ):
                        continue

                    link = node.text.strip()

                    if urllib.parse.urlsplit(link).hostname not in ALLOWED_HOSTS:
                        continue

                    if link.lower().endswith((".xml", ".xml.gz")):
                        queue.append((link, rules))

                    elif "/pd/" in link and link not in seen:
                        seen.add(link)
                        products.append((link, rules))

            except (
                requests.RequestException,
                ValueError,
                ET.ParseError,
                OSError
            ) as exc:
                record(url, None, str(exc)[:200])

        state["discovered_urls"] = len(products)

        for url, rules in products[:MAX_PRODUCTS]:
            if not rules.can_fetch(UA, url):
                continue

            try:
                response = get(url)

                if response.status_code in (401, 403, 429, 503, 511):
                    state["errors"].append(
                        f"Product access blocked: HTTP {response.status_code}"
                    )
                    state["status"] = "access_blocked"
                    break

                response.raise_for_status()

                found = extract(response.text, url)

                if found:
                    state["items"].append(found)

                state["checked"] += 1

            except requests.RequestException as exc:
                state["errors"].append(
                    f"Product error: {str(exc)[:120]}"
                )

        if state["status"] == "starting":
            state["status"] = (
                "completed"
                if products
                else "no_product_urls_found"
            )

        state["items"].sort(
            key=lambda p: p["difference_percent"],
            reverse=True
        )

    except Exception as exc:
        state["status"] = "error"
        state["errors"].append(
            type(exc).__name__ + ": " + str(exc)[:200]
        )

    finally:
        save()

        print(json.dumps(
            {
                k: v for k, v in state.items()
                if k != "items"
            },
            ensure_ascii=False
        ))


if __name__ == "__main__":
    main()
