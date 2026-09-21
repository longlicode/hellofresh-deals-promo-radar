"""Daily Gift Card Publisher — one guide page per calendar day, queue order.

Before publish: fetch official source URLs (stdlib fetch, Playwright fallback on 403).
Unverified fields → answer text starts with Unconfirmed; reviewed_at = UTC date of run.
"""

from __future__ import annotations

import json
import re
import sys
import traceback
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT / "data" / "gift_card_guides.json"
SITE_ILANG = ROOT / ".ilang" / "site.ilang"

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TOOLS))

from scraper import fetch  # noqa: E402

from gift_card_brand_specs import BRAND_SPECS  # noqa: E402

try:
    from browser_render import fetch_rendered, playwright_available
except ImportError:

    def playwright_available() -> bool:
        return False

    def fetch_rendered(url: str) -> tuple[int, str, str]:
        return 0, url, ""


QUEUE_LINE = re.compile(
    r"^\s*\d+\s*\|\s*(\d{4}-\d{2}-\d{2})\s*\|\s*[^|]+\|\s*([^|]+)\|\s*(/guides/([^/]+)/)\s*\|\s*status:\s*(\w+)",
    re.I,
)


def parse_queue_from_ilang() -> list[dict[str, str]]:
    text = SITE_ILANG.read_text(encoding="utf-8")
    rows: list[dict[str, str]] = []
    for line in text.splitlines():
        m = QUEUE_LINE.match(line)
        if not m:
            continue
        sched, _provider, path, slug, status = m.groups()
        rows.append(
            {
                "date": sched.strip(),
                "provider_name": _provider.strip(),
                "path": path.strip(),
                "slug": slug.strip(),
                "status": status.strip().lower(),
            }
        )
    return rows


def published_slugs(guides: dict[str, Any]) -> set[str]:
    return {(b.get("slug") or "").strip() for b in guides.values() if b.get("slug")}


def fetch_official(urls: list[str]) -> tuple[int, str, str, str]:
    """Return status, final_url, body, url_used."""
    last_status = 0
    last_url = urls[0] if urls else ""
    for url in urls:
        status, final_url, body = fetch(url)
        if status == 403 and playwright_available():
            status, final_url, body = fetch_rendered(url)
        last_status, last_url = status, url
        if status == 200 and body and not str(body).startswith("__ERROR__"):
            return status, final_url, body, url
    return last_status, last_url, "", last_url


def _has_keywords(body: str, keywords: list[str]) -> bool:
    low = body.lower()
    return any(k.lower() in low for k in keywords)


def _label(provider: str, page: str, reviewed: str) -> str:
    return f"{provider} — {page} (reviewed {reviewed})"


def build_guide_block(spec: dict[str, Any], reviewed: str) -> dict[str, Any]:
    provider = spec["provider_name"]
    sources_cfg: dict[str, list[str]] = spec["sources"]
    fetched: dict[str, tuple[int, str, str, str]] = {}
    for key, urls in sources_cfg.items():
        fetched[key] = fetch_official(urls)

    def src_label(key: str, page_name: str) -> tuple[str, str]:
        st, _fin, _body, used = fetched[key]
        lbl = _label(provider, page_name, reviewed)
        return used, lbl

    buy_url, buy_lbl = src_label("buy", "Gift purchase page")
    redeem_url, redeem_lbl = src_label("redeem", "Redemption / gift page")
    terms_url, terms_lbl = src_label("terms", "Terms")

    buy_body = fetched["buy"][2]
    redeem_body = fetched["redeem"][2]
    terms_body = fetched["terms"][2]

    gift_kw = ["gift card", "gift-card", "egift", "e-gift", "gift cards", "gifts"]

    q1 = (
        f"Yes. {provider} exposes an official gift-card-related page (see source link)."
        if buy_body and _has_keywords(buy_body, gift_kw)
        else f"Unconfirmed. Official gift-card purchase content could not be verified from fetched pages on {reviewed}."
    )
    q2 = (
        f"Redemption details are on {provider}'s official gift/redemption page linked below (verify steps there on {reviewed})."
        if redeem_body and _has_keywords(redeem_body, gift_kw + ["redeem", "code"])
        else f"Unconfirmed. Redemption steps were not verified from official HTML on {reviewed}."
    )
    pay_kw = ["payment method", "credit card", "card on file", "billing", "subscription"]
    q3 = (
        f"Yes. Official terms or FAQ mention subscription billing or a payment method requirement (see source)."
        if terms_body and _has_keywords(terms_body, pay_kw)
        else f"Unconfirmed. Payment-method requirements for gift redemption were not verified on {reviewed}."
    )
    combo_kw = ["cannot be combined", "not be combined", "may not be combined", "not combinable", "cannot combine"]
    q4 = (
        "No. Official terms state gift cards/vouchers cannot be combined with promotional offers (see source)."
        if terms_body and _has_keywords(terms_body, combo_kw)
        else f"Unconfirmed. Promo stacking rules for gift cards were not verified on {reviewed}."
    )
    exp_kw = ["no expiration", "do not expire", "does not expire", "no inactivity", "dormancy"]
    q5 = (
        "No expiration or dormancy fees are described in the fetched official terms excerpt (see source)."
        if terms_body and _has_keywords(terms_body, exp_kw)
        else f"Unconfirmed. Expiration or dormancy rules were not verified on {reviewed}."
    )
    q6 = (
        f"Unconfirmed. Retail (Target/Walmart) availability is not tracked on {provider}'s official site at review time."
    )

    quick_answers = [
        {
            "question": f"Can you buy official {provider} gift cards?",
            "answer": q1,
            "source_url": buy_url,
            "source_label": buy_lbl,
        },
        {
            "question": f"Where and how do you redeem a {provider} gift card?",
            "answer": q2,
            "source_url": redeem_url,
            "source_label": redeem_lbl,
        },
        {
            "question": "Do you need a credit card or payment method on file to redeem?",
            "answer": q3,
            "source_url": terms_url,
            "source_label": terms_lbl,
        },
        {
            "question": "Can gift cards be combined with promo codes or new-customer offers?",
            "answer": q4,
            "source_url": terms_url,
            "source_label": terms_lbl,
        },
        {
            "question": "Do gift cards expire or charge inactivity fees?",
            "answer": q5,
            "source_url": terms_url,
            "source_label": terms_lbl,
        },
        {
            "question": "Are physical gift cards sold in retail stores?",
            "answer": q6,
            "source_url": terms_url,
            "source_label": terms_lbl,
        },
    ]

    steps: list[dict[str, str]] = []
    if redeem_body and _has_keywords(redeem_body, gift_kw):
        steps = [
            {
                "step": f"1. Open {provider}'s official gift/redemption page (source link).",
                "source_url": redeem_url,
                "source_label": redeem_lbl,
            },
            {
                "step": "2. Sign in or create an account if the official page requires it.",
                "source_url": redeem_url,
                "source_label": redeem_lbl,
            },
            {
                "step": "3. Enter the gift code shown on your card or email, then follow on-page instructions.",
                "source_url": redeem_url,
                "source_label": redeem_lbl,
            },
            {
                "step": "4. Confirm plan selection and any backup payment method required by the official flow.",
                "source_url": terms_url,
                "source_label": terms_lbl,
            },
        ]
    else:
        steps = [
            {
                "step": f"Unconfirmed. Follow {provider}'s official gift or help pages when available; steps were not verified on {reviewed}.",
                "source_url": redeem_url,
                "source_label": redeem_lbl,
            }
        ]

    terms_summary: list[dict[str, str]] = []
    if terms_body and _has_keywords(terms_body, ["gift", "voucher", "card"]):
        terms_summary.append(
            {
                "topic": "Gift cards / vouchers",
                "detail": "See official terms for gift-card rules fetched on the review date.",
                "source_url": terms_url,
                "source_label": terms_lbl,
            }
        )
    else:
        terms_summary.append(
            {
                "topic": "Gift cards / vouchers",
                "detail": f"Unconfirmed. Gift-card terms were not extracted from official pages on {reviewed}.",
                "source_url": terms_url,
                "source_label": terms_lbl,
            }
        )

    return {
        "slug": spec["slug"],
        "provider_name": provider,
        "keyword": spec["keyword"],
        "monthly_search_volume": spec["monthly_search_volume"],
        "title_primary": spec["title_primary"],
        "meta_description": spec["meta_description"],
        "reviewed_at": reviewed,
        "quick_answers": quick_answers,
        "steps_to_redeem": steps,
        "terms_summary": terms_summary,
    }


def spec_for_slug(slug: str) -> dict[str, Any] | None:
    for spec in BRAND_SPECS.values():
        if spec.get("slug") == slug:
            return spec
    return None


def json_key_for_slug(slug: str) -> str:
    spec = spec_for_slug(slug)
    if spec:
        return spec["json_key"]
    return slug.replace("-", "_")


def mark_ilang_published(slug: str) -> None:
    text = SITE_ILANG.read_text(encoding="utf-8")
    pattern = re.compile(
        rf"(\|\s*/guides/{re.escape(slug)}/\s*\|\s*status:\s*)queued",
        re.I,
    )
    new_text, n = pattern.subn(r"\1published", text, count=1)
    if n:
        SITE_ILANG.write_text(new_text, encoding="utf-8")


def pick_task(queue: list[dict[str, str]], guides: dict[str, Any], today_str: str) -> dict[str, str] | None:
    done = published_slugs(guides)
    for row in queue:
        slug = row["slug"]
        if slug in done:
            continue
        if row["date"] > today_str:
            print(f"Next in queue: {slug} scheduled on {row['date']} (today is {today_str}). Nothing to publish.")
            return None
        return row
    print("Queue empty or all guides already published.")
    return None


def main() -> None:
    today_str = date.today().isoformat()
    reviewed = today_str

    if not SITE_ILANG.is_file():
        print(f"Missing {SITE_ILANG}", file=sys.stderr)
        sys.exit(1)

    queue = parse_queue_from_ilang()
    if not queue:
        print("No DAILY_TASK queue rows parsed from site.ilang", file=sys.stderr)
        sys.exit(1)

    guides: dict[str, Any] = {}
    if DATA_FILE.is_file():
        guides = json.loads(DATA_FILE.read_text(encoding="utf-8"))

    task = pick_task(queue, guides, today_str)
    if not task:
        return

    slug = task["slug"]
    if slug in published_slugs(guides):
        print(f"Already published: {slug}")
        return

    spec = spec_for_slug(slug)
    if not spec:
        print(f"No BRAND_SPECS entry for slug {slug}", file=sys.stderr)
        sys.exit(1)

    print(f"Publishing one guide: {slug} (scheduled {task['date']}, reviewed {reviewed})")
    block = build_guide_block(spec, reviewed)
    key = json_key_for_slug(slug)
    guides[key] = block

    DATA_FILE.write_text(json.dumps(guides, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    mark_ilang_published(slug)
    print(f"Wrote {DATA_FILE} key={key}")
    print(f"Live path after deploy: /guides/{slug}/")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
