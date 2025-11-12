"""
Automation script to validate campaign product pricing on xxl.no.

This script uses Playwright to:
1. Navigate to the campaigns page and open the most recent campaign immediately to the right
   of the "All campaigns" banner.
2. Iterate through the top N products (default 10) on that campaign and add each to the cart.
3. Verify that the product price matches across the campaign listing, product detail page,
   and the cart.

Requirements:
    pip install playwright
    playwright install

Example:
    python scripts/verify_campaign_prices.py --products 10 --headless
"""

import argparse
import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import List, Optional

from playwright.async_api import Browser, BrowserContext, Locator, Page, async_playwright


BASE_URL = "https://www.xxl.no"
CAMPAIGNS_URL = f"{BASE_URL}/kampanjer"


@dataclass
class PriceSnapshot:
    context: str
    raw_text: str
    numerical: Optional[float] = None


@dataclass
class ProductVerification:
    index: int
    name: str
    listing_price: PriceSnapshot
    pdp_price: Optional[PriceSnapshot] = None
    cart_price: Optional[PriceSnapshot] = None
    notes: List[str] = field(default_factory=list)
    success: bool = False


def parse_price(raw: str) -> Optional[float]:
    """
    Convert any string containing a Norwegian currency representation into a float.

    Handles values formatted like "1 299,-", "kr 399,90" or "1.299,00 kr".
    """
    if not raw:
        return None

    cleaned = raw.strip()
    if not cleaned:
        return None

    # Remove currency words and whitespace
    cleaned = re.sub(r"(nok|kr|\s|,-)", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.replace("\u00a0", "")  # non-breaking space

    # Replace thousands separators and normalize decimal comma to dot
    cleaned = cleaned.replace(".", "").replace(",", ".")

    try:
        return float(cleaned)
    except ValueError:
        return None


async def accept_cookies_if_present(page: Page, notes: List[str]) -> None:
    selectors = [
        "button:has-text('Godta alle')",
        "button:has-text('Aksepter alle')",
        "button:has-text('Accept all')",
        "[data-testid='cookie-accept-button']",
    ]
    for selector in selectors:
        button = page.locator(selector)
        if await button.count():
            try:
                await button.first.click(timeout=2000)
                notes.append("Accepted cookie consent dialog.")
                return
            except Exception:
                continue


async def dismiss_dialogs(page: Page, notes: List[str]) -> None:
    """Close generic popups that may block interactions."""
    popup_selectors = [
        "button:has-text('Nei takk')",
        "button:has-text('Lukk')",
        "button[aria-label='Close']",
        ".close-button",
        "[data-testid='close-button']",
    ]
    for selector in popup_selectors:
        elements = page.locator(selector)
        if await elements.count():
            try:
                await elements.first.click(timeout=2000)
                notes.append(f"Dismissed popup via selector {selector}.")
            except Exception:
                continue


async def get_campaign_tiles(page: Page) -> Locator:
    """
    Return locator for campaign tiles on the campaigns overview page.

    We try multiple selectors to remain resilient to markup changes.
    """
    selectors = [
        "[data-testid='campaign-card']",
        "[data-test='campaign-card']",
        "a.campaign-card",
        "section a:has(h3)",
    ]
    for selector in selectors:
        locator = page.locator(selector)
        if await locator.count():
            return locator
    raise RuntimeError("Unable to locate campaign tiles on the campaigns page.")


async def open_latest_campaign(page: Page, notes: List[str]) -> None:
    tiles = await get_campaign_tiles(page)
    if await tiles.count() < 2:
        raise RuntimeError(
            "Expected at least two campaign tiles (All campaigns + latest campaign)."
        )

    # The first tile is assumed to be "All campaigns"; select the immediate next one.
    latest_campaign = tiles.nth(1)
    try:
        await latest_campaign.click(timeout=5000)
    except Exception:
        notes.append("Latest campaign tile not directly clickable, triggering via script.")
        href = await latest_campaign.get_attribute("href")
        if not href:
            raise
        await page.goto(href, wait_until="domcontentloaded")


async def get_product_cards(page: Page) -> Locator:
    selectors = [
        "[data-testid='product-card']",
        "[data-test='product-card']",
        "article.product-card",
        "li:has([data-testid='product-title'])",
        "div[data-component='ProductCard']",
    ]
    for selector in selectors:
        locator = page.locator(selector)
        if await locator.count():
            return locator
    raise RuntimeError("Unable to locate product cards on the campaign page.")


async def extract_product_name(card: Locator) -> str:
    name_selectors = [
        "[data-testid='product-title']",
        "a[data-testid='product-card-name']",
        "h3",
        "a",
    ]
    for selector in name_selectors:
        element = card.locator(selector)
        if await element.count():
            text = (await element.first.inner_text()).strip()
            if text:
                return text
    return "Unknown product"


async def extract_product_price(card: Locator) -> PriceSnapshot:
    price_selectors = [
        "[data-testid='product-price']",
        ".price",
        ".product-price",
        "[class*='price']",
    ]

    for selector in price_selectors:
        element = card.locator(selector)
        if await element.count():
            raw_price = (await element.first.inner_text()).strip()
            return PriceSnapshot("listing", raw_price, parse_price(raw_price))

    raw = (await card.inner_text()).strip()
    return PriceSnapshot("listing", raw, parse_price(raw))


async def resolve_product_href(card: Locator) -> Optional[str]:
    link = card.locator("a")
    if await link.count():
        href = await link.first.get_attribute("href")
        if href and href.startswith("http"):
            return href
        if href:
            return BASE_URL + href
    return None


async def ensure_variant_selection(page: Page, notes: List[str]) -> None:
    """
    Attempt to select the first available variant (size/color) if required before adding to cart.
    """
    variant_containers = [
        "[data-testid='size-selector'] button:not([disabled])",
        "[role='radiogroup'] button:not([disabled])",
        "button:has-text('Velg størrelse')",
        "[data-testid='product-variant'] button:not([disabled])",
    ]

    for selector in variant_containers:
        options = page.locator(selector)
        count = await options.count()
        if count:
            try:
                await options.first.click(timeout=2000)
                notes.append(f"Selected first available variant via {selector}.")
                return
            except Exception:
                continue


async def add_to_cart(page: Page, notes: List[str]) -> None:
    add_selectors = [
        "button:has-text('Legg i handlekurv')",
        "button:has-text('Add to cart')",
        "button[data-testid='add-to-cart']",
        "[aria-label='Legg i handlekurv']",
    ]

    for selector in add_selectors:
        button = page.locator(selector)
        if await button.count():
            try:
                await button.first.click(timeout=4000)
                notes.append("Clicked add to cart button.")
                return
            except Exception:
                continue

    raise RuntimeError("Unable to locate add to cart button on product page.")


async def get_pdp_price(page: Page) -> PriceSnapshot:
    price_selectors = [
        "[data-testid='product-price']",
        "span[itemprop='price']",
        "[data-test='product-price']",
        "[class*='price']",
    ]

    for selector in price_selectors:
        element = page.locator(selector)
        if await element.count():
            raw = (await element.first.inner_text()).strip()
            return PriceSnapshot("pdp", raw, parse_price(raw))

    raw = (await page.inner_text("body")).strip()
    return PriceSnapshot("pdp", raw, parse_price(raw))


async def go_to_cart(context: BrowserContext) -> Page:
    cart_page = await context.new_page()
    await cart_page.goto(f"{BASE_URL}/cart", wait_until="domcontentloaded")
    return cart_page


async def get_cart_price(cart_page: Page, product_name: str) -> Optional[PriceSnapshot]:
    # Try to match the cart item that contains the product name.
    item_locators = [
        "[data-testid='cart-item']",
        "article.cart-item",
        "[class*='cart-item']",
        "li:has([data-testid='cart-item-title'])",
    ]

    for selector in item_locators:
        items = cart_page.locator(selector)
        count = await items.count()
        for idx in range(count):
            item = items.nth(idx)
            text = await item.inner_text()
            if product_name.lower() in text.lower():
                price_locators = [
                    "[data-testid='item-price']",
                    "[class*='price']",
                    "span:has-text('kr')",
                ]
                for price_selector in price_locators:
                    price_el = item.locator(price_selector)
                    if await price_el.count():
                        raw = (await price_el.first.inner_text()).strip()
                        return PriceSnapshot("cart", raw, parse_price(raw))
                raw = text.strip()
                return PriceSnapshot("cart", raw, parse_price(raw))

    return None


async def verify_product(
    context: BrowserContext, campaign_page: Page, card: Locator, index: int
) -> ProductVerification:
    notes: List[str] = []
    name = await extract_product_name(card)
    listing_price = await extract_product_price(card)

    href = await resolve_product_href(card)
    if not href:
        notes.append("Could not resolve product link; skipping.")
        return ProductVerification(index=index, name=name, listing_price=listing_price, notes=notes)

    pdp = await context.new_page()
    await pdp.goto(href, wait_until="domcontentloaded")
    await accept_cookies_if_present(pdp, notes)
    await dismiss_dialogs(pdp, notes)

    pdp_price = await get_pdp_price(pdp)

    await ensure_variant_selection(pdp, notes)
    await add_to_cart(pdp, notes)
    await asyncio.sleep(2)  # Allow cart updates

    cart_page = await go_to_cart(context)
    cart_price = await get_cart_price(cart_page, name)

    for page_obj in [pdp, cart_page]:
        await page_obj.close()

    verification = ProductVerification(
        index=index,
        name=name,
        listing_price=listing_price,
        pdp_price=pdp_price,
        cart_price=cart_price,
        notes=notes,
    )

    if (
        listing_price.numerical is not None
        and pdp_price.numerical is not None
        and cart_price
        and cart_price.numerical is not None
    ):
        if (
            listing_price.numerical == pdp_price.numerical
            and listing_price.numerical == cart_price.numerical
        ):
            verification.success = True
        else:
            verification.notes.append(
                f"Price mismatch detected: listing={listing_price.numerical}, "
                f"pdp={pdp_price.numerical}, cart={cart_price.numerical}"
            )
    else:
        verification.notes.append("Unable to parse all prices for comparison.")

    return verification


async def run(products: int, headless: bool) -> List[ProductVerification]:
    async with async_playwright() as p:
        browser: Browser = await p.chromium.launch(headless=headless)
        context: BrowserContext = await browser.new_context()
        page: Page = await context.new_page()

        notes: List[str] = []
        await page.goto(CAMPAIGNS_URL, wait_until="domcontentloaded")
        await accept_cookies_if_present(page, notes)
        await dismiss_dialogs(page, notes)

        await open_latest_campaign(page, notes)
        await dismiss_dialogs(page, notes)

        cards_locator = await get_product_cards(page)
        count = await cards_locator.count()
        if count == 0:
            raise RuntimeError("No products found in campaign.")

        verifications: List[ProductVerification] = []
        for idx in range(min(products, count)):
            card = cards_locator.nth(idx)
            verification = await verify_product(context, page, card, idx + 1)
            # Inherit page-level notes for each product
            verification.notes = notes + verification.notes
            verifications.append(verification)

        await browser.close()
        return verifications


def serialize_results(verifications: List[ProductVerification]) -> str:
    payload = []
    for record in verifications:
        payload.append(
            {
                "index": record.index,
                "product": record.name,
                "success": record.success,
                "listing_price": record.listing_price.__dict__,
                "pdp_price": record.pdp_price.__dict__ if record.pdp_price else None,
                "cart_price": record.cart_price.__dict__ if record.cart_price else None,
                "notes": record.notes,
            }
        )
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def async_main(args: argparse.Namespace) -> None:
    verifications = await run(args.products, args.headless)
    print(serialize_results(verifications))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify price consistency for campaign products on xxl.no"
    )
    parser.add_argument(
        "--products",
        type=int,
        default=10,
        help="Number of top campaign products to verify (default: 10).",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser in headless mode.",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
