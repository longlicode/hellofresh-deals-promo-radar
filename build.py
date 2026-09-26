# ::ILANG
# [TYPE:tool][FILE:build.py]
# ::OBJECTIVE{render_static_site}
#   target: 读 site.ilang + data/offers.json 渲染 site/ 含 JSON-LD sitemap robots
# ::BOUNDARY{never:编 price 或缺字段时伪造结构化数据}
"""Render static coupon site from offers.json. Stdlib only.

URL scheme: extensionless paths via directory index files
  /compare           -> site/compare/index.html
  /providers/{slug}  -> site/providers/{slug}/index.html
  /deals/{id}        -> site/deals/{id}/index.html
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from string import Template
from typing import Any

from ilang_config import ROOT, load_site_config

sys.path.insert(0, str(ROOT / "tools"))
from card_presentation import card_conditions_line, pick_card_lead_offer  # noqa: E402
from listing_title import finalize_listing_title, listing_title_passes  # noqa: E402
from offer_quality import offer_passes_quality  # noqa: E402

from scraper import (
    VALID_UNTIL_NOT_STATED,
    _clean_title,
    _intro_is_brand_copy,
    _is_unit_price_not_promo,
    _looks_like_real_promo,
    _prefer_audience_headline,
    _validate_code,
    clean_conditions,
)

DATA_PATH = ROOT / "data" / "offers.json"
CUTOFF_DATA_PATH = ROOT / "data" / "cursor_openai_cutoff.json"
CONTENT_LAYERS_PATH = ROOT / "data" / "content_layers.json"
AUDIENCE_GUIDES_PATH = ROOT / "data" / "audience_guides.json"
CANCEL_GUIDES_PATH = ROOT / "data" / "cancel_guides.json"
CONTACT_GUIDES_PATH = ROOT / "data" / "contact_guides.json"
BUYER_COMPARISONS_PATH = ROOT / "data" / "buyer_comparisons.json"
GIFT_CARD_GUIDES_PATH = ROOT / "data" / "gift_card_guides.json"
FEATURED_DEALS_PATH = ROOT / "data" / "featured_deals.json"
SITE_DIR = ROOT / "site"
TPL_DIR = ROOT / "templates"

FOOTER_LINKS_HTML = (
    '<p class="footer-links">'
    '<a href="/about/">About</a> '
    '<a href="/contact/">Contact</a> '
    '<a href="/privacy/">Privacy</a> '
    '<a href="/compare/">Compare</a>'
    "</p>"
)
NAV_LINKS_HTML = (
    '<a href="/">Home</a>\n'
    '        <a href="/compare/">Compare</a>\n'
    '        <a href="/about/">About</a>\n'
    '        <a href="/contact/">Contact</a>'
)


def nav_links_html(include_contact: bool = True) -> str:
    links = [
        '<a href="/">Home</a>',
        '<a href="/compare/">Compare</a>',
        '<a href="/guides/plant-forward-meal-deals/">Guides</a>',
        '<a href="/about/">About</a>',
    ]
    if include_contact:
        links.append('<a href="/contact/">Contact</a>')
    return "\n        ".join(links)


def footer_links_html(include_contact: bool = True) -> str:
    parts = ['<a href="/about/">About</a>']
    if include_contact:
        parts.append('<a href="/contact/">Contact</a>')
    parts.extend(['<a href="/privacy/">Privacy</a>', '<a href="/terms/">Terms</a>', '<a href="/compare/">Compare</a>'])
    return '<p class="footer-links">' + " ".join(parts) + "</p>"


def slugify(text: str) -> str:
    s = text.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:80] or "offer"


def sitemap_lastmod(value: str | None, fallback: str) -> str:
    """Normalize a build/scrape timestamp for sitemap lastmod (W3C datetime)."""
    raw = str(value or fallback or "").strip()
    if not raw:
        return fallback[:10]
    if "T" in raw:
        normalized = raw.replace("Z", "+00:00")
        if "+" not in normalized:
            normalized = normalized[:19] + "+00:00"
        return normalized[:25]
    return raw[:10]


def latest_offer_timestamp(rows: list[dict[str, Any]], fallback: str) -> str:
    stamps = [str(o.get("fetched_at") or "").strip() for o in rows if o.get("fetched_at")]
    return max(stamps, default=fallback)


def offer_id(offer: dict[str, Any]) -> str:
    base = f"{offer.get('provider','')}-{offer.get('title','')}-{offer.get('offer_url','')}"
    h = hashlib.sha1(base.encode("utf-8")).hexdigest()[:10]
    return f"{slugify(offer.get('provider', 'x'))}-{slugify(offer.get('title', 'offer'))[:40]}-{h}"


def load_offers() -> dict[str, Any]:
    if not DATA_PATH.exists():
        return {
            "brand": "mealkitdeals",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "offers": [],
            "providers": [],
        }
    return json.loads(DATA_PATH.read_text(encoding="utf-8"))


def sanitize_offer(offer: dict[str, Any]) -> None:
    """Display-time cleanup: readable titles, no fake codes. Mutates offer in place."""
    raw_title = (offer.get("title") or "").strip()
    snippet = offer.get("snippet") or ""
    conditions = offer.get("conditions") or ""
    headline = _prefer_audience_headline(raw_title, snippet, conditions)
    title = _clean_title(headline)
    # Title cleanup must not erase a valid official extract — keep raw if clean stripped too much.
    if len(title) < 10 and len(headline) >= 10:
        title = headline
    offer["title"] = title
    if offer.get("snippet"):
        sn = _clean_title(offer.get("snippet") or "")
        if sn.lower() == title.lower() or title.lower() in sn.lower():
            offer["snippet"] = ""
        else:
            offer["snippet"] = sn
    code = _validate_code(offer.get("code"), title, offer.get("snippet") or "")
    if code:
        offer["code"] = code
        offer["code_required"] = "yes"
    else:
        offer.pop("code", None)
        if (offer.get("code_required") or "").lower() == "yes":
            offer.pop("code_required", None)
    note = (offer.get("valid_until_note") or "").strip()
    if note == "官方页未标":
        offer["valid_until_note"] = VALID_UNTIL_NOT_STATED
    listing = finalize_listing_title(offer, title)
    if listing:
        offer["title"] = listing
    else:
        offer["title"] = ""


def is_showable(offer: dict[str, Any]) -> bool:
    # Never surface fluff / unreachable placeholders as deals
    if offer.get("status") in {"expired", "listing", "unreachable", "no_offer"}:
        return False
    vu = offer.get("valid_until")
    if vu:
        try:
            if date.fromisoformat(vu[:10]) < date.today():
                return False
        except ValueError:
            pass
    title = (offer.get("title") or "").lower()
    if "check current promotions" in title or "temporarily unreachable" in title:
        return False
    title = (offer.get("title") or "").strip()
    if _is_unit_price_not_promo(title, offer.get("price")):
        return False
    if not offer_passes_quality(offer):
        return False
    if offer.get("visible_verified") is False:
        return False
    if not listing_title_passes(offer):
        return False
    if _looks_like_real_promo(title, offer.get("price"), offer.get("code")):
        return True
    # Benefit is a fallback headline only when title cleanup left nothing usable.
    benefit = (offer.get("benefit") or "").strip()
    if not title and benefit and _looks_like_real_promo(benefit, offer.get("price"), offer.get("code")):
        return True
    return False


def month_label() -> str:
    return datetime.now(timezone.utc).strftime("%B %Y")


def read_tpl(name: str) -> Template:
    return Template((TPL_DIR / name).read_text(encoding="utf-8"))


def tpl_escape(value: Any) -> str:
    """Values are inserted literally by string.Template — do not double '$'."""
    return str(value)


def render_tpl(name: str, mapping: dict[str, Any]) -> str:
    safe = {k: tpl_escape(v) for k, v in mapping.items()}
    return read_tpl(name).safe_substitute(safe)


def ga4_head_html(ga4_id: str) -> str:
    """Google Analytics 4 gtag snippet for <head> (empty when ID missing)."""
    ga4_id = (ga4_id or "").strip()
    if not ga4_id:
        return ""
    return render_tpl("_ga4.html", {"ga4_id": ga4_id})


def offer_valid_display(offer: dict[str, Any]) -> str:
    if offer.get("valid_until"):
        return str(offer["valid_until"])
    note = (offer.get("valid_until_note") or "").strip()
    if note == "官方页未标":
        note = VALID_UNTIL_NOT_STATED
    return note or VALID_UNTIL_NOT_STATED


def offer_code_required_html(offer: dict[str, Any]) -> str:
    code = offer.get("code")
    req = (offer.get("code_required") or "").strip().lower()
    if code:
        return f"yes — <strong>{html.escape(str(code))}</strong>"
    if req == "yes":
        return "yes"
    if req == "no":
        return "no"
    return ""


def load_content_layers() -> dict[str, Any]:
    if not CONTENT_LAYERS_PATH.exists():
        return {}
    return json.loads(CONTENT_LAYERS_PATH.read_text(encoding="utf-8"))


def provider_content_layer_html(name: str, layers: dict[str, Any]) -> str:
    block = layers.get(name)
    if not block or not block.get("sections"):
        return ""
    reviewed = html.escape(str(block.get("reviewed_at") or "").strip())
    parts: list[str] = [
        '<section class="panel content-layer">',
        f"<h2>Before you buy: {html.escape(name)}</h2>",
    ]
    if reviewed:
        parts.append(
            f'<p class="meta">Editorial layer · last reviewed {reviewed} · '
            "each bullet links to the source we used.</p>"
        )
    for section in block.get("sections") or []:
        title = html.escape(str(section.get("title") or "").strip())
        if not title:
            continue
        parts.append(f"<h3>{title}</h3>")
        parts.append('<ul class="content-layer-list">')
        for item in section.get("items") or []:
            text = html.escape(str(item.get("text") or "").strip())
            src = html.escape(str(item.get("source_url") or "").strip())
            label = html.escape(str(item.get("source_label") or "Source").strip())
            if not text or not src:
                continue
            parts.append(
                "<li>"
                f"{text} "
                f'<a href="{src}" rel="nofollow noopener">{label}</a>'
                "</li>"
            )
        parts.append("</ul>")
    parts.append("</section>")
    return "\n    ".join(parts)


def load_audience_guides() -> dict[str, Any]:
    if not AUDIENCE_GUIDES_PATH.exists():
        return {}
    return json.loads(AUDIENCE_GUIDES_PATH.read_text(encoding="utf-8"))


def load_cancel_guides() -> dict[str, Any]:
    if not CANCEL_GUIDES_PATH.exists():
        return {}
    return json.loads(CANCEL_GUIDES_PATH.read_text(encoding="utf-8"))


def load_contact_guides() -> dict[str, Any]:
    if not CONTACT_GUIDES_PATH.exists():
        return {}
    return json.loads(CONTACT_GUIDES_PATH.read_text(encoding="utf-8"))


def load_buyer_comparisons() -> dict[str, Any]:
    if not BUYER_COMPARISONS_PATH.exists():
        return {}
    return json.loads(BUYER_COMPARISONS_PATH.read_text(encoding="utf-8"))


def buyer_comparisons_by_provider(comparisons: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for block in comparisons.values():
        for brand in block.get("brands") or []:
            name = str(brand).strip()
            if name:
                out[name].append(block)
    return out


def buyer_comparisons_index_list_html(comparisons: dict[str, Any]) -> str:
    rows: list[str] = []
    for block in sorted(comparisons.values(), key=lambda b: str(b.get("title_primary") or "")):
        slug = (block.get("slug") or "").strip().strip("/")
        if not slug:
            continue
        href = page_path("guides", slug)
        title = html.escape(str(block.get("title_primary") or slug))
        rows.append(f'<li><a href="{html.escape(href)}">{title}</a></li>')
    return "\n      ".join(rows) if rows else ""


def provider_buyer_comparison_links_html(name: str, comparisons: dict[str, Any]) -> str:
    blocks = buyer_comparisons_by_provider(comparisons).get(name) or []
    if not blocks:
        return ""
    links: list[str] = []
    for block in sorted(blocks, key=lambda b: str(b.get("title_primary") or "")):
        slug = (block.get("slug") or "").strip().strip("/")
        if not slug:
            continue
        href = page_path("guides", slug)
        label = html.escape(str(block.get("title_primary") or slug))
        links.append(f'<a href="{html.escape(href)}">{label}</a>')
    if not links:
        return ""
    joined = " · ".join(links)
    return f'<p class="meta provider-buyer-compare-link">Buyer comparison: {joined}</p>'


def _buyer_compare_cell_html(cell: dict[str, Any]) -> str:
    display = html.escape(str(cell.get("display") or "").strip())
    captured = html.escape(str(cell.get("captured_at") or "").strip())
    internal = str(cell.get("internal_path") or "").strip()
    src = html.escape(str(cell.get("source_url") or "").strip())
    label = html.escape(str(cell.get("source_label") or "Official source").strip())
    parts = [f"<p>{display}</p>"]
    if internal:
        ih = html.escape(internal)
        parts.append(
            f'<p class="meta"><a href="{ih}">Open guide on this site</a>'
            + (f" · Captured {captured}" if captured else "")
            + "</p>"
        )
    elif src:
        meta = f'Captured {captured} · <a href="{src}" rel="nofollow noopener">{label}</a>'
        parts.append(f'<p class="meta">{meta}</p>')
    elif captured:
        parts.append(f'<p class="meta">Captured {captured}</p>')
    return "\n".join(parts)



def _https_only(url: str) -> str:
    u = (url or "").strip()
    return u if u.startswith("https://") else ""


def build_featured_deals_html(deals_doc: dict[str, Any]) -> str:
    """Render homepage featured deal cards from data/featured_deals.json (editorial, not scraped)."""
    cards: list[str] = []
    for deal in deals_doc.get("deals") or []:
        if not deal.get("enabled", True):
            continue
        title = html.escape(str(deal.get("title") or "").strip())
        cta_url = _https_only(str(deal.get("cta_url") or ""))
        image_url = _https_only(str(deal.get("image_url") or ""))
        cta_text = html.escape(str(deal.get("cta_text") or "View deal").strip())
        if not title or not cta_url:
            continue

        currency = (deal.get("currency") or "USD").strip().upper()
        sym = "$" if currency == "USD" else f"{currency} "
        rrp = str(deal.get("rrp") or "").strip()
        deal_price = str(deal.get("deal_price") or "").strip()
        price_html = ""
        if deal_price:
            price_html = f'<p class="featured-deal-price"><span class="deal-price">{sym}{html.escape(deal_price)}</span>'
            if rrp:
                price_html += f' <span class="rrp">RRP {sym}{html.escape(rrp)}</span>'
            price_html += "</p>"

        badge_raw = str(deal.get("badge") or "").strip()
        badges_html = ""
        if badge_raw:
            pills = [html.escape(p.strip()) for p in badge_raw.split("|") if p.strip()]
            badges_html = "".join(f'<span class="pill featured-badge">{p}</span>' for p in pills)

        bullets = deal.get("bullets") or []
        bullet_items = "".join(
            f"<li>{html.escape(str(b).strip())}</li>" for b in bullets if str(b).strip()
        )
        bullets_html = f"<ul class=\"featured-deal-bullets\">{bullet_items}</ul>" if bullet_items else ""

        img_html = ""
        if image_url:
            img_html = (
                f'<div class="featured-deal-media">'
                f'<img src="{html.escape(image_url)}" alt="{title}" width="500" height="500" loading="lazy" decoding="async" />'
                f"</div>"
            )

        cards.append(
            f"""
            <article class="featured-deal card">
              <div class="featured-deal-layout">
                {img_html}
                <div class="featured-deal-body">
                  <div class="featured-deal-badges">{badges_html}</div>
                  <h2 class="featured-deal-title">{title}</h2>
                  {price_html}
                  {bullets_html}
                  <a class="btn featured-deal-cta" href="{html.escape(cta_url)}" target="_blank" rel="noopener noreferrer sponsored">{cta_text}</a>
                </div>
              </div>
            </article>
            """
        )

    if not cards:
        return ""
    return (
        '<section class="featured-section" aria-labelledby="featured-deals-heading">'
        '<p class="eyebrow" id="featured-deals-heading">Partner Spotlight · Smart Home &amp; Living Specials</p>'
        '<p class="meta featured-deal-intro">Curated limited-time offers handpicked from our verified brand partners.</p>'
        + "\n".join(cards)
        + '<p class="meta featured-deal-note">Editorial pick · pricing and badges as supplied for this listing. '
        "We may earn a commission if you buy through the link.</p>"
        "</section>"
    )


def build_gift_card_guide_pages(
    brand: str,
    domain: str,
    base_vars: dict[str, Any],
    guides: dict[str, Any],
    generated: str,
) -> list[tuple[str, str]]:
    written: list[tuple[str, str]] = []
    for _key, block in guides.items():
        slug = (block.get("slug") or "").strip().strip("/")
        if not slug:
            continue
        primary = str(block.get("title_primary") or "Gift Card Guide").strip()
        reviewed = html.escape(str(block.get("reviewed_at") or generated).strip())
        lede = html.escape(str(block.get("meta_description") or "").strip())
        rel_path = page_path("guides", slug)

        qa_rows: list[str] = []
        for qa in block.get("quick_answers") or []:
            q = html.escape(str(qa.get("question") or "").strip())
            a = html.escape(str(qa.get("answer") or "").strip())
            src = html.escape(str(qa.get("source_url") or "").strip())
            lbl = html.escape(str(qa.get("source_label") or "Official source").strip())
            qa_rows.append(
                f"<li><strong>{q}</strong><br />{a} "
                f'<a href="{src}" rel="nofollow noopener">{lbl}</a></li>'
            )

        step_rows: list[str] = []
        for s in block.get("steps_to_redeem") or []:
            st = html.escape(str(s.get("step") or "").strip())
            src = html.escape(str(s.get("source_url") or "").strip())
            lbl = html.escape(str(s.get("source_label") or "Official source").strip())
            step_rows.append(
                f'<li>{st} <a href="{src}" rel="nofollow noopener">{lbl}</a></li>'
            )

        terms_rows: list[str] = []
        for t in block.get("terms_summary") or []:
            top = html.escape(str(t.get("topic") or "").strip())
            det = html.escape(str(t.get("detail") or "").strip())
            src = html.escape(str(t.get("source_url") or "").strip())
            lbl = html.escape(str(t.get("source_label") or "Official source").strip())
            terms_rows.append(
                f"<li><strong>{top}:</strong> {det} "
                f'<a href="{src}" rel="nofollow noopener">{lbl}</a></li>'
            )

        page_html = render_tpl(
            "gift_card_guide.html",
            {
                **base_vars,
                "title": f"{primary} — {brand}",
                "description": lede[:300],
                "canonical": abs_url(domain, rel_path),
                "og_title": primary,
                "heading": html.escape(primary),
                "lede": lede,
                "crumb_title": html.escape(primary),
                "quick_answers": "\n        ".join(qa_rows),
                "redemption_steps": "\n        ".join(step_rows),
                "terms_list": "\n        ".join(terms_rows),
                "reviewed_at": reviewed,
            },
        )
        write_page(rel_path, page_html)
        written.append((rel_path, block.get("reviewed_at") or generated))
    return written


def build_buyer_comparison_pages(
    brand: str,
    domain: str,
    base_vars: dict[str, Any],
    comparisons: dict[str, Any],
    generated: str,
) -> list[tuple[str, str]]:
    written: list[tuple[str, str]] = []
    for _key, block in comparisons.items():
        slug = (block.get("slug") or "").strip().strip("/")
        if not slug:
            continue
        brands = [str(b).strip() for b in (block.get("brands") or []) if str(b).strip()]
        primary = str(block.get("title_primary") or slug).strip()
        reviewed = html.escape(str(block.get("reviewed_at") or generated).strip())
        lede = html.escape(
            str(block.get("meta_description") or "").strip()
            or "Official-brand facts only; each figure links to the page we captured it from."
        )
        rel_path = page_path("guides", slug)
        header_cells = "".join(f"<th>{html.escape(b)}</th>" for b in brands)
        comp_rows: list[str] = []
        for dim in block.get("comparison_dimensions") or []:
            label = html.escape(str(dim.get("label") or "").strip())
            cells = dim.get("cells") or {}
            tds = "".join(
                f"<td>{_buyer_compare_cell_html(cells.get(b) or {})}</td>" for b in brands
            )
            comp_rows.append(f"<tr><th scope=\"row\">{label}</th>{tds}</tr>")
        terms_sorted = sorted(
            block.get("terms_rows") or [],
            key=lambda r: (str(r.get("captured_at") or ""), str(r.get("brand") or "")),
        )
        term_rows: list[str] = []
        for row in terms_sorted:
            cap = html.escape(str(row.get("captured_at") or ""))
            bname = html.escape(str(row.get("brand") or ""))
            topic = html.escape(str(row.get("topic") or ""))
            summary = html.escape(str(row.get("summary") or ""))
            src = html.escape(str(row.get("source_url") or ""))
            slabel = html.escape(str(row.get("source_label") or "Official source"))
            src_cell = (
                f'<a href="{src}" rel="nofollow noopener">{slabel}</a>' if src else "—"
            )
            term_rows.append(
                f"<tr><td>{cap}</td><td>{bname}</td><td>{topic}</td>"
                f"<td>{summary}</td><td>{src_cell}</td></tr>"
            )
        persona_parts: list[str] = []
        for persona in block.get("personas") or []:
            heading = html.escape(str(persona.get("heading") or "").strip())
            body = html.escape(str(persona.get("body") or "").strip())
            if heading and body:
                persona_parts.append(f"<h3>{heading}</h3><p>{body}</p>")
        page_html = render_tpl(
            "buyer_comparison.html",
            {
                **base_vars,
                "title": f"{primary} — {brand}",
                "description": lede[:300],
                "canonical": abs_url(domain, rel_path),
                "og_title": primary,
                "heading": html.escape(primary),
                "lede": lede,
                "crumb_title": html.escape(primary),
                "brand_header_cells": header_cells,
                "comparison_rows": "\n            ".join(comp_rows) or "<tr><td colspan=\"3\">—</td></tr>",
                "terms_rows": "\n            ".join(term_rows) or "<tr><td colspan=\"5\">—</td></tr>",
                "persona_blocks": "\n      ".join(persona_parts),
                "reviewed_at": reviewed,
            },
        )
        write_page(rel_path, page_html)
        written.append((rel_path, block.get("reviewed_at") or generated))
    return written


def cancel_guides_by_provider(guides: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for block in guides.values():
        name = (block.get("provider_name") or "").strip()
        if name:
            out[name] = block
    return out


def provider_cancel_guide_link_html(name: str, guides: dict[str, Any]) -> str:
    block = cancel_guides_by_provider(guides).get(name)
    if not block:
        return ""
    slug = (block.get("slug") or "").strip().strip("/")
    if not slug:
        return ""
    href = page_path("guides", slug)
    label = (block.get("provider_link_label") or f"See how to cancel {name}").strip()
    return (
        f'<p class="meta provider-cancel-link">'
        f'<a href="{html.escape(href)}">{html.escape(label)}</a></p>'
    )


def cancel_guides_index_list_html(guides: dict[str, Any]) -> str:
    rows: list[str] = []
    for block in sorted(guides.values(), key=lambda b: str(b.get("title_primary") or "")):
        slug = (block.get("slug") or "").strip().strip("/")
        if not slug:
            continue
        href = page_path("guides", slug)
        title = html.escape(str(block.get("title_primary") or slug))
        rows.append(f'<li><a href="{html.escape(href)}">{title}</a></li>')
    return "\n      ".join(rows) if rows else "<li>—</li>"


def contact_guides_by_provider(guides: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for block in guides.values():
        name = (block.get("provider_name") or "").strip()
        if name:
            out[name] = block
    return out


def provider_contact_guide_link_html(name: str, guides: dict[str, Any]) -> str:
    block = contact_guides_by_provider(guides).get(name)
    if not block:
        return ""
    slug = (block.get("slug") or "").strip().strip("/")
    if not slug:
        return ""
    href = page_path("guides", slug)
    label = (block.get("provider_link_label") or f"Customer service — {name}").strip()
    return (
        f'<p class="meta provider-contact-link">'
        f'<a href="{html.escape(href)}">{html.escape(label)}</a></p>'
    )


def contact_guides_index_list_html(guides: dict[str, Any]) -> str:
    rows: list[str] = []
    for block in sorted(guides.values(), key=lambda b: str(b.get("title_primary") or "")):
        slug = (block.get("slug") or "").strip().strip("/")
        if not slug:
            continue
        href = page_path("guides", slug)
        title = html.escape(str(block.get("title_primary") or slug))
        rows.append(f'<li><a href="{html.escape(href)}">{title}</a></li>')
    return "\n      ".join(rows) if rows else "<li>—</li>"


def contact_cluster_links_html(current_slug: str, guides: dict[str, Any]) -> str:
    rows: list[str] = []
    for block in sorted(guides.values(), key=lambda b: str(b.get("title_primary") or "")):
        slug = (block.get("slug") or "").strip().strip("/")
        if not slug or slug == current_slug:
            continue
        href = page_path("guides", slug)
        title = html.escape(str(block.get("title_primary") or slug))
        rows.append(f"<li><a href=\"{html.escape(href)}\">{title}</a></li>")
    if not rows:
        return ""
    return (
        "<h2>Other customer service guides on this site</h2>"
        "<ul class=\"content-layer-list\">"
        + "\n      ".join(rows)
        + "</ul>"
    )


def contact_cancel_cross_link_html(block: dict[str, Any], cancel_guides: dict[str, Any]) -> str:
    cancel_slug = (block.get("cancel_guide_slug") or "").strip().strip("/")
    if not cancel_slug:
        name = (block.get("provider_name") or "").strip()
        cancel_block = cancel_guides_by_provider(cancel_guides).get(name)
        if cancel_block:
            cancel_slug = (cancel_block.get("slug") or "").strip().strip("/")
    if not cancel_slug:
        return ""
    href = page_path("guides", cancel_slug)
    prov = html.escape(str(block.get("provider_name") or "this brand"))
    return (
        f"<h2>Pause or cancel your subscription</h2>"
        f'<p class="meta">For official cancellation steps on {prov}, see our '
        f'<a href="{html.escape(href)}">subscription cancellation guide</a> '
        f"(sourced from the brand's help or terms pages).</p>"
    )


def cancel_contact_cross_link_html(block: dict[str, Any], contact_guides: dict[str, Any]) -> str:
    name = (block.get("provider_name") or "").strip()
    contact_block = contact_guides_by_provider(contact_guides).get(name)
    if not contact_block:
        return ""
    slug = (contact_block.get("slug") or "").strip().strip("/")
    if not slug:
        return ""
    href = page_path("guides", slug)
    prov = html.escape(name)
    return (
        f"<h2>Need {prov} customer service?</h2>"
        f'<p class="meta">Phone, chat, and help-center paths we quoted from official pages: '
        f'<a href="{html.escape(href)}">{prov} customer service guide</a>.</p>'
    )


def _contact_fastest_path_html(block: dict[str, Any]) -> str:
    fp = block.get("fastest_path") or {}
    text = html.escape(str(fp.get("text") or "").strip())
    src = html.escape(str(fp.get("source_url") or "").strip())
    label = html.escape(str(fp.get("source_label") or "Official source").strip())
    if not text:
        return "Official fastest-contact wording not extracted — use the brand contact page linked below."
    if not src:
        return text
    return f"{text} <a href=\"{src}\" rel=\"nofollow noopener\">{label}</a>"


def _contact_channel_blocks_html(block: dict[str, Any]) -> str:
    parts: list[str] = []
    for ch in block.get("channels") or []:
        heading = html.escape(str(ch.get("heading") or "").strip())
        if heading:
            parts.append(f"<h2>{heading}</h2>")
        inner = _cancel_sourced_list_html(ch.get("items") or [])
        parts.append(f'<ul class="content-layer-list">{inner}</ul>')
    return "\n      ".join(parts)


def _contact_hours_block_html(block: dict[str, Any]) -> str:
    items = block.get("hours") or []
    if not items:
        return ""
    inner = _cancel_sourced_list_html(items)
    return (
        "<h2>Hours (from the brand)</h2>"
        f'<ul class="content-layer-list">{inner}</ul>'
    )


def build_contact_guide_pages(
    brand: str,
    domain: str,
    base_vars: dict[str, Any],
    guides: dict[str, Any],
    cancel_guides: dict[str, Any],
    generated: str,
) -> list[tuple[str, str]]:
    written: list[tuple[str, str]] = []
    for _key, block in guides.items():
        slug = (block.get("slug") or "").strip().strip("/")
        if not slug:
            continue
        primary = str(block.get("title_primary") or "Customer service").strip()
        reviewed = html.escape(str(block.get("reviewed_at") or generated).strip())
        lede = html.escape(str(block.get("meta_description") or "").strip())
        rel_path = page_path("guides", slug)
        synonyms = block.get("synonym_headings") or []
        syn_html = ""
        if synonyms:
            syn_html = "<h2>Related searches on this page</h2><ul class=\"content-layer-list\">"
            syn_html += "".join(
                f"<li>{html.escape(str(s).strip())}</li>" for s in synonyms if str(s).strip()
            )
            syn_html += "</ul>"
        page_html = render_tpl(
            "contact_guide.html",
            {
                **base_vars,
                "title": f"{primary} — {brand}",
                "description": lede[:300],
                "canonical": abs_url(domain, rel_path),
                "og_title": primary,
                "heading": html.escape(primary),
                "lede": lede,
                "crumb_title": html.escape(primary),
                "synonym_blocks": syn_html,
                "fastest_path": _contact_fastest_path_html(block),
                "channel_blocks": _contact_channel_blocks_html(block),
                "hours_block": _contact_hours_block_html(block),
                "cancel_cross_link": contact_cancel_cross_link_html(block, cancel_guides),
                "cluster_links": contact_cluster_links_html(slug, guides),
                "reviewed_at": reviewed,
            },
        )
        write_page(rel_path, page_html)
        written.append((rel_path, block.get("reviewed_at") or generated))
    return written


def cancel_cluster_links_html(current_slug: str, guides: dict[str, Any]) -> str:
    rows: list[str] = []
    for block in sorted(guides.values(), key=lambda b: str(b.get("title_primary") or "")):
        slug = (block.get("slug") or "").strip().strip("/")
        if not slug or slug == current_slug:
            continue
        href = page_path("guides", slug)
        title = html.escape(str(block.get("title_primary") or slug))
        rows.append(f"<li><a href=\"{html.escape(href)}\">{title}</a></li>")
    if not rows:
        return ""
    return (
        "<h2>Other subscription cancellation guides on this site</h2>"
        "<ul class=\"content-layer-list\">"
        + "\n      ".join(rows)
        + "</ul>"
    )


def _cancel_append_sections_html(block: dict[str, Any]) -> str:
    parts: list[str] = []
    for sec in block.get("append_sections") or []:
        heading = html.escape(str(sec.get("heading") or "").strip())
        if heading:
            parts.append(f"<h2>{heading}</h2>")
        phrases = sec.get("related_phrases") or []
        if phrases:
            parts.append('<ul class="content-layer-list">')
            parts.extend(
                f"<li>{html.escape(str(p).strip())}</li>" for p in phrases if str(p).strip()
            )
            parts.append("</ul>")
        steps = _cancel_sourced_list_html(sec.get("steps") or [], ordered=True)
        if steps:
            parts.append(f'<ol class="content-layer-list cancel-steps">{steps}</ol>')
        timing = _cancel_sourced_list_html(sec.get("timing_conditions") or [])
        if timing:
            parts.append(f'<ul class="content-layer-list">{timing}</ul>')
    return "\n      ".join(parts)


def _cancel_sourced_list_html(items: list[dict[str, Any]], *, ordered: bool = False) -> str:
    tag = "ol" if ordered else "ul"
    rows: list[str] = []
    for item in items or []:
        text = html.escape(str(item.get("text") or "").strip())
        src = html.escape(str(item.get("source_url") or "").strip())
        label = html.escape(str(item.get("source_label") or "Official source").strip())
        if not text or not src:
            continue
        rows.append(
            f"<li>{text} "
            f'<a href="{src}" rel="nofollow noopener">{label}</a></li>'
        )
    inner = "\n      ".join(rows) if rows else "<li>Official steps not extracted — use the brand help or terms page linked below.</li>"
    if ordered:
        return inner
    return inner


def build_cancel_guide_pages(
    brand: str,
    domain: str,
    base_vars: dict[str, Any],
    guides: dict[str, Any],
    contact_guides: dict[str, Any],
    generated: str,
) -> list[tuple[str, str]]:
    written: list[tuple[str, str]] = []
    for _key, block in guides.items():
        slug = (block.get("slug") or "").strip().strip("/")
        if not slug:
            continue
        primary = str(block.get("title_primary") or "Cancel subscription").strip()
        reviewed = html.escape(str(block.get("reviewed_at") or generated).strip())
        lede = html.escape(str(block.get("lede") or "").strip())
        rel_path = page_path("guides", slug)
        synonyms = block.get("synonym_headings") or []
        syn_html = ""
        if synonyms:
            syn_html = "<h2>Related searches on this page</h2><ul class=\"content-layer-list\">"
            syn_html += "".join(
                f"<li>{html.escape(str(s).strip())}</li>" for s in synonyms if str(s).strip()
            )
            syn_html += "</ul>"
        app = block.get("app_notes")
        app_html = ""
        if isinstance(app, dict) and (app.get("text") or "").strip():
            app_html = (
                "<h2>How to cancel HelloFresh on the app</h2>"
                f"<p>{html.escape(str(app.get('text') or '').strip())} "
                f'<a href="{html.escape(str(app.get("source_url") or ""))}" rel="nofollow noopener">'
                f'{html.escape(str(app.get("source_label") or "Source"))}</a></p>'
            )
        before_rows: list[str] = []
        for link in block.get("before_cancel_links") or []:
            label = html.escape(str(link.get("label") or "").strip())
            path = str(link.get("path") or "").strip()
            if not label or not path:
                continue
            before_rows.append(
                f'<li><a href="{html.escape(path)}">View {label} promo listings on {html.escape(brand)}</a></li>'
            )
        page_html = render_tpl(
            "cancel_guide.html",
            {
                **base_vars,
                "title": f"{primary} — {brand}",
                "description": html.escape(str(block.get("meta_description") or lede)[:300]),
                "canonical": abs_url(domain, rel_path),
                "og_title": primary,
                "heading": html.escape(primary),
                "lede": lede,
                "crumb_title": html.escape(primary),
                "synonym_blocks": syn_html,
                "steps": _cancel_sourced_list_html(block.get("steps") or [], ordered=True),
                "timing": _cancel_sourced_list_html(block.get("timing_conditions") or []),
                "app_block": app_html,
                "append_block": _cancel_append_sections_html(block),
                "cluster_links": cancel_cluster_links_html(slug, guides),
                "contact_cross_link": cancel_contact_cross_link_html(block, contact_guides),
                "before_links": "\n      ".join(before_rows) if before_rows else "<li>—</li>",
                "reviewed_at": reviewed,
            },
        )
        write_page(rel_path, page_html)
        written.append((rel_path, block.get("reviewed_at") or generated))
    return written


def _audience_guide_list_html(items: list[dict[str, Any]], *, name_key: str = "") -> str:
    rows: list[str] = []
    for item in items or []:
        text = html.escape(str(item.get("text") or "").strip())
        src = html.escape(str(item.get("source_url") or "").strip())
        label = html.escape(str(item.get("source_label") or "Source").strip())
        if not text or not src:
            continue
        prefix = ""
        if name_key:
            prov = html.escape(str(item.get(name_key) or "").strip())
            if prov:
                prefix = f"<strong>{prov}.</strong> "
        rows.append(
            f"<li>{prefix}{text} "
            f'<a href="{src}" rel="nofollow noopener">{label}</a></li>'
        )
    return "\n      ".join(rows) if rows else "<li>—</li>"


def build_audience_guide_pages(
    brand: str,
    domain: str,
    base_vars: dict[str, Any],
    guides: dict[str, Any],
    generated: str,
) -> list[tuple[str, str]]:
    written: list[tuple[str, str]] = []
    for _key, block in guides.items():
        slug = (block.get("slug") or "").strip().strip("/")
        if not slug:
            continue
        title = str(block.get("title") or "Audience guide").strip()
        reviewed = html.escape(str(block.get("reviewed_at") or generated).strip())
        why = html.escape(str(block.get("why_segment") or "").strip())
        rel_path = page_path("guides", slug)
        page_html = render_tpl(
            "audience.html",
            {
                **base_vars,
                "title": f"{title} — {brand}",
                "description": (why or title)[:300],
                "canonical": abs_url(domain, rel_path),
                "og_title": title,
                "heading": html.escape(title),
                "lede": why,
                "crumb_title": html.escape(title),
                "how_to_pick": _audience_guide_list_html(block.get("how_to_pick") or []),
                "good_fit": _audience_guide_list_html(block.get("good_fit") or [], name_key="provider"),
                "consider_skipping": _audience_guide_list_html(
                    block.get("consider_skipping") or [], name_key="provider"
                ),
                "table_judgment": _audience_guide_list_html(block.get("table_judgment") or []),
                "reviewed_at": reviewed,
            },
        )
        write_page(rel_path, page_html)
        written.append((rel_path, block.get("reviewed_at") or generated))
    return written


def provider_about_html(name: str, profiles: dict[str, Any]) -> str:
    prof = profiles.get(name) or {}
    intro = (prof.get("intro") or "").strip()
    src = (prof.get("intro_source_url") or "").strip()
    if not intro or not _intro_is_brand_copy(intro, src):
        return ""
    src_html = ""
    if src:
        src_html = (
            f'<p class="meta">Source: <a href="{html.escape(src)}" rel="nofollow noopener">'
            f"{html.escape(src)}</a></p>"
        )
    return (
        f'<section class="panel about-brand">'
        f"<h2>About {html.escape(name)}</h2>"
        f"<p>{html.escape(intro)}</p>"
        f"{src_html}"
        f"</section>"
    )


def provider_sources_html(rows: list[dict[str, Any]]) -> str:
    items: list[str] = []
    seen: set[str] = set()
    for o in rows:
        src = (o.get("source_url") or o.get("offer_url") or "").strip()
        if not src or src in seen:
            continue
        seen.add(src)
        items.append(
            f'<li><a href="{html.escape(src)}" rel="nofollow noopener">{html.escape(src)}</a></li>'
        )
    if not items:
        return ""
    return (
        '<section class="panel">'
        "<h2>Official source pages</h2>"
        "<ul>"
        + "".join(items)
        + "</ul></section>"
    )


def _sentence_case_fragment(fragment: str) -> str:
    fragment = fragment.strip()
    if not fragment:
        return ""
    if fragment[0].islower():
        return fragment[0].upper() + fragment[1:]
    return fragment


def _split_condition_fragments(conditions: str) -> list[str]:
    parts: list[str] = []
    for piece in re.split(r"[,;]", conditions):
        p = piece.strip()
        if p:
            parts.append(p)
    return parts


# Homepage-only: whole-line fragments that read worse than showing nothing.
_CARD_CONDITION_UNREADABLE = frozenset(
    {
        "upcoming order",
        "for life",
        "first 4 weeks",
    }
)


def _drop_unreadable_card_condition(line: str) -> str:
    if not line:
        return ""
    if line.strip().lower() in _CARD_CONDITION_UNREADABLE:
        return ""
    return line.strip()


def _format_card_conditions(kept_fragments: list[str], comma_joined: str) -> str:
    """Turn deduped fragments into a short readable line; fall back if not clearer."""
    if not kept_fragments:
        return ""
    if len(kept_fragments) == 1:
        single = kept_fragments[0]
        cased = _sentence_case_fragment(single)
        return comma_joined if cased == single and single != comma_joined else cased
    formatted = "; ".join(_sentence_case_fragment(f) for f in kept_fragments)
    if formatted.lower().replace("; ", ", ") == comma_joined.lower():
        return formatted
    return formatted


def offer_card_conditions_line(offer: dict[str, Any]) -> str:
    """Homepage card line 2: deduped, lightly formatted conditions (detail pages keep full text)."""
    title = (offer.get("title") or "").strip()
    benefit = (offer.get("benefit") or "").strip()
    raw_cleaned = clean_conditions((offer.get("conditions") or "").strip())
    tl = title.lower()
    bl = benefit.lower()

    kept: list[str] = []
    if raw_cleaned:
        for part in _split_condition_fragments(raw_cleaned):
            pl = part.lower()
            if pl in tl or (bl and pl in bl):
                continue
            if bl and len(pl) >= 6 and pl in bl:
                continue
            if tl and len(pl) >= 10 and pl in tl:
                continue
            kept.append(part)

    comma_joined = ", ".join(kept)
    conditions = _format_card_conditions(kept, comma_joined)

    code = (offer.get("code") or "").strip()
    if code and code.upper() not in title.upper():
        code_bit = f"Code {code}"
        if code_bit.lower() not in conditions.lower():
            conditions = f"{conditions}; {code_bit}" if conditions else code_bit

    return _drop_unreadable_card_condition(conditions.strip(" ;"))


def normalize_offer_title(title: str) -> str:
    """Display-time cleanup for UI crumbs; never invents new offer claims."""
    from html import unescape

    title = _clean_title(unescape(title or ""))
    title = re.sub(r"\s*\|\s*[A-Za-z][A-Za-z0-9 &'-]{1,40}$", "", title)
    return title.strip(" -–|:;,.")


def abs_url(domain: str, path: str) -> str:
    domain = domain.rstrip("/")
    if not domain.startswith("http"):
        domain = "https://" + domain
    if not path.startswith("/"):
        path = "/" + path
    # Directory indexes on Cloudflare Pages live at trailing-slash URLs;
    # non-slash requests 308 to slash — keep page canonicals on the slash form.
    # File assets (sitemap.xml, robots.txt, *.css, …) must NOT get a trailing slash.
    is_file = bool(re.search(r"\.[A-Za-z0-9]{1,8}$", path.rstrip("/")))
    if is_file:
        path = path.rstrip("/")
    elif path != "/" and not path.endswith("/"):
        path = path + "/"
    return domain + path


def page_path(*parts: str) -> str:
    """Extensionless public path with trailing slash for CF Pages directory indexes.

    Example: ('providers','hellofresh') -> /providers/hellofresh/
    """
    clean = [p.strip("/") for p in parts if p and p.strip("/")]
    if not clean:
        return "/"
    return "/" + "/".join(clean) + "/"


def write_page(rel_path: str, content: str) -> None:
    """Write HTML as directory index so /path/ serves without .html."""
    rel = rel_path.strip("/")
    if not rel or rel == "index":
        out = SITE_DIR / "index.html"
    else:
        out = SITE_DIR / rel / "index.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(content, encoding="utf-8")


def json_ld_offer(offer: dict[str, Any], page_url: str) -> dict[str, Any]:
    node: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "Offer",
        "name": offer.get("title"),
        "url": offer.get("offer_url") or page_url,
        "availability": "https://schema.org/InStock"
        if offer.get("status") in {"active", "listing"}
        else "https://schema.org/SoldOut",
    }
    if offer.get("price"):
        node["price"] = str(offer["price"])
        node["priceCurrency"] = offer.get("currency") or "USD"
    if offer.get("valid_until"):
        node["priceValidUntil"] = offer["valid_until"][:10]
    seller = {"@type": "Organization", "name": offer.get("provider")}
    if offer.get("domain"):
        seller["url"] = f"https://{offer['domain']}"
    node["seller"] = seller
    return node


def clean_output_dirs() -> None:
    """Remove prior page trees so leftover .html files cannot leak old URLs."""
    for name in (
        "providers",
        "deals",
        "compare",
        "about",
        "contact",
        "privacy",
        "cursor-openai-cutoff",
    ):
        target = SITE_DIR / name
        if target.exists():
            shutil.rmtree(target)
    for stale in ("compare.html", "404.html"):
        p = SITE_DIR / stale
        if p.exists():
            p.unlink()


def build_static_pages(
    brand: str,
    domain: str,
    affiliate_note: str,
    base_vars: dict[str, Any],
    cfg: dict[str, Any],
) -> list[tuple[str, str]]:
    """Emit About / Contact / Privacy trust pages. Never invent contact email."""
    pub = cfg.get("publisher") or {}
    contact_email = (pub.get("contact_email") or "").strip()
    about_operator = (pub.get("about_operator") or "").strip()
    written: list[tuple[str, str]] = []

    about_who = (
        html.escape(about_operator)
        if about_operator
        else "the independent publisher of this site"
    )
    about_body = f"""
        <p><strong>{html.escape(brand)}</strong> is a meal-kit promo radar: we list publicly visible promotions scraped from official brand pages.</p>
        <p>It is operated by {about_who}. The site is a static Cloudflare Pages site built from a public GitHub repository. Listings update automatically from public sources.</p>
        <p>We only show titles, prices, promo codes, and expiry dates when those fields are extracted from official pages. We do not invent offers, prices, codes, or valid-through dates.</p>
        <p>Outbound brand links may be affiliate links. Third-party advertising may appear on the site in the future; see the Privacy page for how that works.</p>
    """
    about_path = page_path("about")
    about_html = render_tpl(
        "static.html",
        {
            **base_vars,
            "title": f"About — {brand}",
            "description": f"Who runs {brand} and what this meal-kit promo radar does.",
            "canonical": abs_url(domain, about_path),
            "og_title": f"About {brand}",
            "eyebrow": "About",
            "heading": f"About {brand}",
            "body": about_body,
        },
    )
    write_page(about_path, about_html)
    written.append((about_path, about_html))

    if contact_email and "@" in contact_email:
        contact_body = f"""
        <p><strong>{html.escape(brand)}</strong> is a meal-kit promo radar: it lists publicly visible promotions scraped from official brand pages. It does not invent offers, prices, codes, or expiry dates.</p>
        <p>To reach the publisher about listing corrections, outdated promos, privacy questions, or partnership inquiries, email:</p>
        <p><a href="mailto:{html.escape(contact_email)}">{html.escape(contact_email)}</a></p>
        <p>Please include the page URL when you report an incorrect or outdated listing. We read messages sent to this address and reply when we can; we do not promise a fixed response time.</p>
        """
        contact_path = page_path("contact")
        contact_html = render_tpl(
            "static.html",
            {
                **base_vars,
                "title": f"Contact — {brand}",
                "description": f"Contact the publisher of {brand}.",
                "canonical": abs_url(domain, contact_path),
                "og_title": f"Contact {brand}",
                "eyebrow": "Contact",
                "heading": "Contact",
                "body": contact_body,
            },
        )
        write_page(contact_path, contact_html)
        written.append((contact_path, contact_html))

    contact_line = (
        '<p><strong>Contact.</strong> For privacy questions, use the email on the <a href="/contact/">Contact</a> page.</p>'
        if contact_email and "@" in contact_email
        else '<p><strong>Contact.</strong> A public contact email will be listed on this site once the publisher publishes one.</p>'
    )
    privacy_body = f"""
        <p>This Privacy Policy applies to <strong>{html.escape(brand)}</strong> at <strong>{html.escape(domain)}</strong>, a static meal-kit promo radar hosted on Cloudflare Pages.</p>
        <p><strong>What we collect.</strong> The public site itself does not run a member login and does not ask you to create an account. Standard web server / CDN logs (such as IP address, user agent, and requested URL) may be processed by Cloudflare while serving the site. We do not sell personal information.</p>
        <p><strong>Affiliate links.</strong> Some outbound links to meal-kit brands may be affiliate links. If you click them and later subscribe or purchase, we may earn a commission at no extra cost to you. Affiliate networks and brand sites have their own privacy policies.</p>
        <p><strong>Third-party advertising.</strong> The site is prepared to display third-party ads (for example display or affiliate network creatives). Ad partners may use cookies or similar technologies to measure impressions or personalize ads. When ad codes are added, those partners' policies also apply. We will not invent tracking that is not actually installed.</p>
        <p><strong>Scraped listings.</strong> Promo titles, codes, and prices shown on this site come from publicly available brand pages. We do not invent missing fields.</p>
        {contact_line}
        <p>Last updated: {html.escape(date.today().isoformat())}.</p>
    """
    privacy_path = page_path("privacy")
    privacy_html = render_tpl(
        "static.html",
        {
            **base_vars,
            "title": f"Privacy Policy — {brand}",
            "description": f"Privacy Policy for {brand}, including affiliate links and third-party ads.",
            "canonical": abs_url(domain, privacy_path),
            "og_title": f"Privacy — {brand}",
            "eyebrow": "Legal",
            "heading": "Privacy Policy",
            "body": privacy_body,
        },
    )
    write_page(privacy_path, privacy_html)
    written.append((privacy_path, privacy_html))

    terms_body = f"""
        <p>Welcome to <strong>{html.escape(brand)}</strong> (<strong>{html.escape(domain)}</strong>). By accessing or using this website, you agree to be bound by these Terms of Service.</p>
        <p><strong>Affiliate Disclosure & Earnings Disclaimer.</strong> {html.escape(brand)} is an independent shopping guide and promotional directory. Some outbound links on this website (including links to meal kit providers, Amazon, and partner brand products) are affiliate links. If you click through and make a purchase or subscribe, we may receive a commission at no additional cost to you. Product pricing, promotions, and availability are set by the respective merchants and are subject to change without notice.</p>
        <p><strong>Accuracy of Information.</strong> Promotional listings and deal details are collected from public brand pages and official partner feeds. While we strive to ensure all information is timely and accurate, we do not warrant that product descriptions, pricing, or terms are error-free. Always confirm pricing, discount eligibility, and terms on the merchant's official site before completing an order.</p>
        <p><strong>Intellectual Property.</strong> Brand names, logos, trademarks, and registered trademarks displayed on this site are the property of their respective owners. Their display does not imply endorsement or affiliation beyond our participation in standard affiliate marketing programs.</p>
        <p><strong>Limitation of Liability.</strong> {html.escape(brand)} shall not be liable for any direct, indirect, incidental, or consequential damages resulting from your use of this website or your transactions with third-party merchants.</p>
        {contact_line}
        <p>Last updated: {html.escape(date.today().isoformat())}.</p>
    """
    terms_path = page_path("terms")
    terms_html = render_tpl(
        "static.html",
        {
            **base_vars,
            "title": f"Terms of Service & Affiliate Disclosure — {brand}",
            "description": f"Terms of Service and Affiliate Disclosure for {brand}.",
            "canonical": abs_url(domain, terms_path),
            "og_title": f"Terms & Affiliate Disclosure — {brand}",
            "eyebrow": "Legal",
            "heading": "Terms of Service & Affiliate Disclosure",
            "body": terms_body,
        },
    )
    write_page(terms_path, terms_html)
    written.append((terms_path, terms_html))
    return written


def load_cursor_cutoff() -> dict[str, Any]:
    if not CUTOFF_DATA_PATH.exists():
        return {}
    return json.loads(CUTOFF_DATA_PATH.read_text(encoding="utf-8"))


def buttondown_subscribe_html(
    buttondown_username: str,
    *,
    embed_tag: str,
    form_id: str,
    heading: str = "",
    blurb: str = "",
    missing_config_note: str = "",
) -> str:
    username = (buttondown_username or "").strip()
    tag = (embed_tag or username or "site").strip()
    if not username:
        note = missing_config_note or (
            "Publisher has not configured <code>buttondown_username</code> in .ilang yet."
        )
        return f'<p class="meta">{note}</p>'
    action = f"https://buttondown.email/api/emails/embed-subscribe/{html.escape(username)}"
    head = f"<h2>{html.escape(heading)}</h2>" if heading else ""
    intro = f'<p class="lede subscribe-lede">{html.escape(blurb)}</p>' if blurb else ""
    return (
        f'<div class="cutoff-subscribe">{head}{intro}'
        f'<form class="cutoff-subscribe-form" action="{action}" method="post" rel="noopener">'
        f'<label for="{html.escape(form_id)}">Email</label>'
        f'<input id="{html.escape(form_id)}" type="email" name="email" required autocomplete="email" />'
        f'<input type="hidden" name="tag" value="{html.escape(tag)}" />'
        '<button type="submit">Subscribe — confirm via email</button>'
        "</form>"
        '<p class="meta">Buttondown sends a confirmation link; you are not subscribed until you click it. '
        "Every email includes an unsubscribe link. We do not display subscriber counts.</p>"
        "</div>"
    )


def cutoff_subscribe_html(buttondown_username: str) -> str:
    return buttondown_subscribe_html(
        buttondown_username,
        embed_tag="cursor-openai-cutoff",
        form_id="bd-email-cutoff",
        heading="Email updates",
    )


def site_newsletter_html(cfg: dict[str, Any]) -> str:
    nl = cfg.get("newsletter") or {}
    username = (os.environ.get("BUTTONDOWN_USERNAME") or nl.get("buttondown_username") or "").strip()
    return buttondown_subscribe_html(
        username,
        embed_tag=(nl.get("embed_tag") or username or "mealkitdeals-promo").strip(),
        form_id="bd-email-home",
        heading="Weekly promo & term changes",
        blurb=(nl.get("blurb") or "").strip()
        or "One email when headline discounts or conditions change on brands we track — same pipeline as this site.",
        missing_config_note=(
            "Newsletter embed is not configured yet (.ilang NEWSLETTER → buttondown_username)."
        ),
    )


def cutoff_timeline_rows_html(timeline: list[dict[str, Any]]) -> str:
    rows: list[str] = []
    for row in timeline:
        event_date = html.escape(str(row.get("event_date") or "—"))
        quote = html.escape(str(row.get("quote_en") or "").strip() or "—")
        src = html.escape(str(row.get("source_url") or "#"))
        reviewed = html.escape(str(row.get("reviewed_at") or "—"))
        fetch_note = (row.get("fetch_note") or "").strip()
        note_html = (
            f' <span class="cutoff-timeline-fetch">{html.escape(fetch_note)}</span>'
            if fetch_note
            else ""
        )
        rows.append(
            "<li>"
            f'<span class="cutoff-timeline-date">{event_date}</span>'
            f" {quote} "
            f'<a href="{src}" rel="nofollow noopener">{src}</a> '
            f"复核 {reviewed}{note_html}"
            "</li>"
        )
    return "\n        ".join(rows) if rows else "<li>暂无公开源条目。</li>"


def build_cursor_cutoff_page(
    brand: str,
    domain: str,
    affiliate_note: str,
    base_vars: dict[str, Any],
    cfg: dict[str, Any],
    generated: str,
) -> tuple[str, str] | None:
    cutoff_cfg = cfg.get("cutoff_tracker") or {}
    slug = (cutoff_cfg.get("page_slug") or "cursor-openai-cutoff").strip().strip("/")
    data = load_cursor_cutoff()
    if not data:
        return None

    status = (data.get("status_label") or "提案中").strip()
    if status not in {"提案中", "已确认", "已提前或已延长"}:
        status = "提案中"

    countdown_iso = (
        (cutoff_cfg.get("countdown_utc") or "").strip()
        or (data.get("countdown_target_utc") or "2026-11-12T23:59:59+00:00").strip()
    )
    countdown_label = (data.get("countdown_label_zh") or "距 OpenAI 提出的过渡日（未确认）").strip()
    status_note = html.escape(
        (data.get("status_note_en") or "").strip()
        or "Official termination date not yet confirmed on public OpenAI pages."
    )
    next_watch = html.escape(
        (data.get("next_watch_zh") or "").strip()
        or "下一项：等待各模型厂商在公开页面上说明 Cursor 集成是否变化。"
    )
    timeline = list(data.get("timeline") or [])

    buttondown = (os.environ.get("BUTTONDOWN_USERNAME") or "").strip() or (
        cutoff_cfg.get("buttondown_username") or ""
    ).strip()

    rel_path = page_path(slug)
    page_html = render_tpl(
        "cursor_cutoff.html",
        {
            **base_vars,
            "title": f"Cursor × OpenAI 模型供应 · 状态与倒计时 — {brand}",
            "description": (
                "公开来源追踪：OpenAI 向 Cursor 供应模型的提案中截止日、状态行、倒计时与邮件订阅。"
            ),
            "canonical": abs_url(domain, rel_path),
            "og_title": "Cursor × OpenAI cutoff tracker (public sources)",
            "status_label": html.escape(status),
            "status_note": status_note,
            "countdown_label": html.escape(countdown_label),
            "countdown_iso": html.escape(countdown_iso),
            "subscribe_block": cutoff_subscribe_html(buttondown),
            "timeline_rows": cutoff_timeline_rows_html(timeline),
            "next_watch": next_watch,
        },
    )
    write_page(rel_path, page_html)
    return rel_path, page_html


def render() -> None:
    cfg = load_site_config()
    data = load_offers()
    content_layers = load_content_layers()
    audience_guides = load_audience_guides()
    cancel_guides = load_cancel_guides()
    contact_guides = load_contact_guides()
    buyer_comparisons = load_buyer_comparisons()
    site = cfg["site"]
    brand = site.get("brand") or data.get("brand") or "mealkitdeals"
    domain = site.get("domain") or data.get("domain") or "localhost"
    niche = site.get("niche") or data.get("niche") or "meal kit deals"
    affiliate_note = cfg["affiliate_note"]
    affiliates = cfg["affiliates"]
    shelved = set(cfg.get("shelved") or [])
    pub = cfg.get("publisher") or {}
    analytics = cfg.get("analytics") or {}
    ga4_id = (analytics.get("ga4_id") or "").strip()
    ga4_head = ga4_head_html(ga4_id)
    has_contact = bool((pub.get("contact_email") or "").strip() and "@" in (pub.get("contact_email") or ""))

    provider_profiles: dict[str, Any] = dict(data.get("provider_profiles") or {})
    raw_offers = [dict(o) for o in data.get("offers", [])]
    for o in raw_offers:
        if o.get("provider") in shelved:
            continue
        sanitize_offer(o)
    offers = [
        o
        for o in raw_offers
        if o.get("provider") not in shelved and is_showable(o)
    ]
    # Drop duplicate cards for the same brand + benefit + code after cleanup
    seen_card: set[tuple[str, str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for o in offers:
        key = (
            o.get("provider") or "",
            (o.get("benefit") or o.get("title") or "").strip().lower(),
            (o.get("code") or "").upper(),
        )
        if key in seen_card:
            continue
        seen_card.add(key)
        deduped.append(o)
    offers = deduped
    for o in offers:
        o["_id"] = offer_id(o)
        o["_affiliate"] = affiliates.get(o.get("provider", ""), o.get("offer_url", "#"))

    by_provider: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for o in offers:
        by_provider[o.get("provider", "Unknown")].append(o)

    # Prefer config provider order (skip shelved brands — data kept, pages not emitted)
    provider_order = [p["name"] for p in cfg["providers"] if p["name"] not in shelved]
    for name in by_provider:
        if name not in provider_order and name not in shelved:
            provider_order.append(name)

    SITE_DIR.mkdir(parents=True, exist_ok=True)
    (SITE_DIR / "assets").mkdir(exist_ok=True)
    clean_output_dirs()

    month = month_label()
    # Homepage "Updated" stamp = build time so deploys are externally verifiable
    built_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    generated = built_at
    base_vars = {
        "brand": html.escape(brand),
        "niche": html.escape(niche),
        "month": html.escape(month),
        "generated": html.escape(str(generated)),
        "ga4_head": ga4_head,
        "affiliate_note": html.escape(affiliate_note),
        "canonical_home": abs_url(domain, "/"),
        "year": str(datetime.now(timezone.utc).year),
        "nav_links": nav_links_html(has_contact),
        "footer_links": footer_links_html(has_contact),
    }

    # CSS
    (SITE_DIR / "assets" / "style.css").write_text(
        (TPL_DIR / "style.css").read_text(encoding="utf-8"), encoding="utf-8"
    )
    # Cloudflare Pages cache headers (keep homepage from sticking on edge)
    headers_src = TPL_DIR / "_headers"
    if headers_src.exists():
        (SITE_DIR / "_headers").write_text(headers_src.read_text(encoding="utf-8"), encoding="utf-8")

    # Index cards — only brands with at least one showable offer (no empty promo shells).
    cards = []
    listed_providers: list[str] = []
    for name in provider_order:
        rows = by_provider.get(name, [])
        if not rows:
            continue
        listed_providers.append(name)
        top = pick_card_lead_offer(name, rows)
        top_title = top.get("title", name)
        cond_line = card_conditions_line(top, offer_card_conditions_line)
        cond_html = (
            f'<p class="card-conditions">{html.escape(cond_line)}</p>'
            if cond_line
            else ""
        )
        provider_href = page_path("providers", slugify(name))
        cards.append(
            f"""
            <article class="card">
              <p class="eyebrow">{html.escape(name)}</p>
              <h2><a href="{provider_href}">{html.escape(top_title)}</a></h2>
              {cond_html}
              <a class="btn" href="{provider_href}">View {html.escape(name)}</a>
            </article>
            """
        )

    item_list = {
        "@context": "https://schema.org",
        "@type": "ItemList",
        "itemListElement": [
            {
                "@type": "ListItem",
                "position": i + 1,
                "url": abs_url(domain, page_path("providers", slugify(name))),
                "name": name,
            }
            for i, name in enumerate(listed_providers)
        ],
    }

    featured_doc: dict[str, Any] = {}
    if FEATURED_DEALS_PATH.exists():
        try:
            featured_doc = json.loads(FEATURED_DEALS_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            featured_doc = {}

    index_html = render_tpl(
        "index.html",
        {
            **base_vars,
            "title": f"{brand} — {niche} promo radar ({month})",
            "description": f"Live public promo listings for meal kits: {', '.join(provider_order[:6])}. Updated {month}.",
            "canonical": abs_url(domain, "/"),
            "og_title": f"{brand} meal kit deals — {month}",
            "featured_deals": build_featured_deals_html(featured_doc),
            "cards": "\n".join(cards) or "<p>No offers extracted yet. Pipeline will retry.</p>",
            "json_ld": json.dumps(item_list, ensure_ascii=False),
            "offer_count": str(len(offers)),
            "provider_count": str(len(listed_providers)),
            "subscribe_block": site_newsletter_html(cfg),
        },
    )
    write_page("/", index_html)

    # Compare page — only rows with real offers
    compare_rows = []
    compare_list = []
    pos = 0
    for name in provider_order:
        rows = by_provider.get(name, [])
        if not rows:
            continue
        pos += 1
        top = pick_card_lead_offer(name, rows)
        provider_href = page_path("providers", slugify(name))
        price_cell = f"${html.escape(str(top['price']))}" if top.get("price") else "—"
        code_cell = html.escape(str(top["code"])) if top.get("code") else "—"
        compare_rows.append(
            f"<tr><td><a href=\"{provider_href}\">{html.escape(name)}</a></td>"
            f"<td>{html.escape(top.get('title',''))}</td>"
            f"<td>{price_cell}</td>"
            f"<td>{code_cell}</td>"
            f"<td>{html.escape(top.get('status',''))}</td>"
            f"<td><a href=\"{html.escape(top.get('_affiliate') or top.get('offer_url',''))}\">Go</a></td></tr>"
        )
        compare_list.append(
            {
                "@type": "ListItem",
                "position": pos,
                "url": abs_url(domain, provider_href),
                "name": name,
            }
        )
    compare_path = page_path("compare")
    compare_ld = {"@context": "https://schema.org", "@type": "ItemList", "itemListElement": compare_list}
    compare_html = render_tpl(
        "compare.html",
        {
            **base_vars,
            "title": f"Compare meal kit promos — {brand} ({month})",
            "description": f"Side-by-side public promo listings across {len(by_provider)} meal kit brands.",
            "canonical": abs_url(domain, compare_path),
            "og_title": f"Compare meal kit deals — {month}",
            "rows": "\n".join(compare_rows) or "<tr><td colspan=\"6\">No public promo offers extracted yet.</td></tr>",
            "json_ld": json.dumps(compare_ld, ensure_ascii=False),
            "cancel_guides_list": cancel_guides_index_list_html(cancel_guides),
            "contact_guides_list": contact_guides_index_list_html(contact_guides),
            "buyer_comparisons_list": buyer_comparisons_index_list_html(buyer_comparisons),
        },
    )
    write_page(compare_path, compare_html)

    # Provider + deal pages — only for brands with showable offers
    sitemap_urls: list[tuple[str, str]] = [("/", generated)]

    for name in provider_order:
        rows = by_provider.get(name, [])
        if not rows:
            continue
        pslug = slugify(name)
        provider_path = page_path("providers", pslug)
        deal_links = []
        offer_nodes = []
        prices = []
        for o in rows:
            did = o["_id"]
            deal_path = page_path("deals", did)
            code_pill = f" code:{html.escape(str(o['code']))}" if o.get("code") else ""
            detail_bits = []
            if o.get("benefit"):
                detail_bits.append(f"What you get: {html.escape(str(o['benefit']))}")
            cond_display = clean_conditions(str(o.get("conditions") or ""))
            if cond_display:
                detail_bits.append(f"Conditions: {html.escape(cond_display)}")
            cr = offer_code_required_html(o)
            if cr:
                detail_bits.append(f"Code required: {cr}")
            detail_bits.append(f"Valid until: {html.escape(offer_valid_display(o))}")
            src = o.get("source_url") or ""
            if src:
                detail_bits.append(
                    f'Source: <a href="{html.escape(src)}" rel="nofollow noopener">{html.escape(src)}</a>'
                )
            detail_html = (
                f'<p class="deal-detail">{" · ".join(detail_bits)}</p>' if detail_bits else ""
            )
            deal_links.append(
                f"<li><a href=\"{deal_path}\">{html.escape(o.get('title',''))}</a>"
                f" <span class=\"pill\">{html.escape(o.get('status',''))}{code_pill}</span>"
                f"{detail_html}</li>"
            )
            page_url = abs_url(domain, deal_path)
            offer_nodes.append(json_ld_offer(o, page_url))
            if o.get("price"):
                try:
                    prices.append(float(str(o["price"]).replace(",", "")))
                except ValueError:
                    pass

            breadcrumbs = {
                "@context": "https://schema.org",
                "@type": "BreadcrumbList",
                "itemListElement": [
                    {"@type": "ListItem", "position": 1, "name": "Home", "item": abs_url(domain, "/")},
                    {
                        "@type": "ListItem",
                        "position": 2,
                        "name": name,
                        "item": abs_url(domain, provider_path),
                    },
                    {"@type": "ListItem", "position": 3, "name": o.get("title"), "item": page_url},
                ],
            }
            ld_graph = [json_ld_offer(o, page_url), breadcrumbs]
            price_html = ""
            if o.get("price"):
                price_html = f"<p class=\"price\">{html.escape(o.get('currency','USD'))} {html.escape(str(o['price']))}</p>"
            benefit = (o.get("benefit") or "").strip() or (o.get("title") or "")
            conditions = clean_conditions((o.get("conditions") or "").strip())
            deal_desc = (o.get("benefit") or o.get("snippet") or o.get("title") or "")[:160]
            src_l = (o.get("source_url") or "").lower()
            if name == "Marley Spoon" and ("rtc-50p" in src_l or "rtc50" in src_l):
                deal_desc = (
                    f"Marley Spoon promo code & discount: {deal_desc}. "
                    "Official rtc50p offer page (public link)."
                )[:300]
            deal_html = render_tpl(
                "deal.html",
                {
                    **base_vars,
                    "title": f"{o.get('title')} — {name} | {brand}",
                    "description": deal_desc,
                    "canonical": page_url,
                    "og_title": html.escape(str(o.get("title"))),
                    "provider": html.escape(name),
                    "provider_link": provider_path,
                    "deal_title": html.escape(str(o.get("title"))),
                    "benefit": html.escape(str(benefit)),
                    "conditions": html.escape(conditions),
                    "code_required_html": offer_code_required_html(o),
                    "valid_display": html.escape(offer_valid_display(o)),
                    "price_html": price_html,
                    "cta_url": html.escape(o.get("_affiliate") or o.get("offer_url") or "#"),
                    "source_url": html.escape(o.get("source_url") or ""),
                    "fetched_at": html.escape(str(o.get("fetched_at") or "")),
                    "json_ld": json.dumps(ld_graph, ensure_ascii=False),
                    "status": html.escape(str(o.get("status") or "")),
                },
            )
            write_page(deal_path, deal_html)
            sitemap_urls.append((deal_path, o.get("fetched_at") or generated))

        product_ld: dict[str, Any] = {
            "@context": "https://schema.org",
            "@type": "Service",
            "name": f"{name} meal kit promotions",
            "provider": {"@type": "Organization", "name": name},
            "url": abs_url(domain, provider_path),
        }
        if prices:
            product_ld["offers"] = {
                "@type": "AggregateOffer",
                "lowPrice": str(min(prices)),
                "highPrice": str(max(prices)),
                "priceCurrency": "USD",
                "offerCount": str(len(prices)),
            }
        elif offer_nodes:
            cleaned = []
            for n in offer_nodes[:20]:
                cleaned.append(n)
            product_ld["offers"] = cleaned

        faq_ld = {
            "@context": "https://schema.org",
            "@type": "FAQPage",
            "mainEntity": [
                {
                    "@type": "Question",
                    "name": f"Where do {name} promo details come from?",
                    "acceptedAnswer": {
                        "@type": "Answer",
                        "text": f"From the public official {name} pages listed in .ilang/site.ilang. Prices/codes are only shown when extracted; we never invent them.",
                    },
                },
                {
                    "@type": "Question",
                    "name": f"How often is {name} updated?",
                    "acceptedAnswer": {
                        "@type": "Answer",
                        "text": "The public GitHub Actions pipeline refreshes about every 6 hours.",
                    },
                },
            ],
        }

        deal_list_html = (
            "\n".join(deal_links)
            if deal_links
            else (
                "<p><strong>No public promo offer extracted yet.</strong> "
                "We do not invent titles, prices, or codes. "
                f"Check the <a href=\"{html.escape(affiliates.get(name, '#'))}\">official {html.escape(name)} site</a> "
                "for live promotions.</p>"
            )
        )

        prof = provider_profiles.get(name) or {}
        intro = (prof.get("intro") or "").strip()
        desc_bits = [
            f"{name} promo codes, coupons, and deals for {month}.",
            intro[:140] if intro else "Public listings scraped from official brand pages.",
        ]
        if name == "Marley Spoon" and rows:
            src0 = (rows[0].get("source_url") or "").lower()
            if "rtc-50p" in src0 or "rtc50" in src0:
                desc_bits.append(
                    "Live listing for the official rtc50p discount offer page (up to 50% off)."
                )
        listing_count = len(rows)
        provider_html = render_tpl(
            "provider.html",
            {
                **base_vars,
                "title": f"{name} promo codes & deals — {brand} ({month})",
                "description": " ".join(desc_bits)[:300],
                "canonical": abs_url(domain, provider_path),
                "og_title": f"{name} promo codes & deals — {month}",
                "h1": html.escape(f"{name} promo codes & deals"),
                "lede": html.escape(
                    f"Public {name} promo codes, coupons, and meal kit deals from official pages "
                    f"for {month}. Every line below is extracted from brand sites — nothing invented."
                ),
                "provider": html.escape(name),
                "about_section": provider_about_html(name, provider_profiles),
                "content_layer": provider_content_layer_html(name, content_layers),
                "listings_heading": html.escape(f"{name} promo codes & coupons ({listing_count})"),
                "listings_note": html.escape(
                    f"{listing_count} public offer(s) extracted from official {name} pages."
                ),
                "deal_list": deal_list_html,
                "source_section": provider_sources_html(rows),
                "json_ld": json.dumps([product_ld, faq_ld], ensure_ascii=False),
                "official": html.escape(affiliates.get(name, rows[0].get("source_url", "#") if rows else "#")),
                "cancel_guide_link": provider_cancel_guide_link_html(name, cancel_guides),
                "contact_guide_link": provider_contact_guide_link_html(name, contact_guides),
                "buyer_comparison_links": provider_buyer_comparison_links_html(
                    name, buyer_comparisons
                ),
            },
        )
        write_page(provider_path, provider_html)
        sitemap_urls.append((provider_path, latest_offer_timestamp(rows, generated)))

    sitemap_urls.append((compare_path, generated))

    # Legal / trust pages for affiliate review
    static_pages = build_static_pages(brand, domain, affiliate_note, base_vars, cfg)
    for path, _html in static_pages:
        sitemap_urls.append((path, generated))

    for guide_path, guide_lm in build_audience_guide_pages(
        brand, domain, base_vars, audience_guides, generated
    ):
        sitemap_urls.append((guide_path, str(guide_lm)))

    for cancel_path, cancel_lm in build_cancel_guide_pages(
        brand, domain, base_vars, cancel_guides, contact_guides, generated
    ):
        sitemap_urls.append((cancel_path, str(cancel_lm)))

    for contact_path, contact_lm in build_contact_guide_pages(
        brand, domain, base_vars, contact_guides, cancel_guides, generated
    ):
        sitemap_urls.append((contact_path, str(contact_lm)))

    for buyer_path, buyer_lm in build_buyer_comparison_pages(
        brand, domain, base_vars, buyer_comparisons, generated
    ):
        sitemap_urls.append((buyer_path, str(buyer_lm)))

    gift_card_guides: dict[str, Any] = {}
    if GIFT_CARD_GUIDES_PATH.exists():
        try:
            gift_card_guides = json.loads(GIFT_CARD_GUIDES_PATH.read_text(encoding="utf-8"))
        except Exception:
            gift_card_guides = {}

    for gc_path, gc_lm in build_gift_card_guide_pages(
        brand, domain, base_vars, gift_card_guides, generated
    ):
        sitemap_urls.append((gc_path, str(gc_lm)))

    cutoff_meta = load_cursor_cutoff()
    cutoff_page = build_cursor_cutoff_page(
        brand, domain, affiliate_note, base_vars, cfg, generated
    )
    if cutoff_page:
        sitemap_urls.append((cutoff_page[0], cutoff_meta.get("generated_at") or generated))

    # 404 page (Cloudflare Pages serves this for missing paths)
    (SITE_DIR / "404.html").write_text(
        render_tpl("404.html", {"ga4_head": ga4_head}),
        encoding="utf-8",
    )

    # sitemap + robots
    sm = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for path, lastmod in sitemap_urls:
        lm = sitemap_lastmod(str(lastmod), generated)
        sm.append("  <url>")
        sm.append(f"    <loc>{html.escape(abs_url(domain, path))}</loc>")
        sm.append(f"    <lastmod>{html.escape(lm)}</lastmod>")
        sm.append("  </url>")
    sm.append("</urlset>")
    (SITE_DIR / "sitemap.xml").write_text("\n".join(sm) + "\n", encoding="utf-8")
    (SITE_DIR / "robots.txt").write_text(
        f"User-agent: *\nAllow: /\nSitemap: {abs_url(domain, '/sitemap.xml')}\n",
        encoding="utf-8",
    )

    print(f"Built {len(offers)} offers across {len(by_provider)} providers -> {SITE_DIR}")


if __name__ == "__main__":
    from audit_offer_quality import collect_quality_issues  # noqa: E402

    issues = collect_quality_issues()
    if issues:
        print(f"quality audit failed ({len(issues)} issue(s)):", file=sys.stderr)
        for line in issues:
            print(f"- {line}", file=sys.stderr)
        sys.exit(1)
    render()
