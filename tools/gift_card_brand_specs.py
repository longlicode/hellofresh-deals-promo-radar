"""Official URL specs and verification rules for daily gift-card guides."""

from __future__ import annotations

from typing import Any

# Each brand: candidate official URLs (tried in order) + shared question templates.
BrandSpec = dict[str, Any]

BRAND_SPECS: dict[str, BrandSpec] = {
    "home_chef": {
        "json_key": "home_chef",
        "provider_name": "Home Chef",
        "slug": "home-chef-gift-card",
        "keyword": "home chef gift card",
        "monthly_search_volume": 1000,
        "title_primary": "Home Chef Gift Card: Buy, Redeem & Official Terms",
        "meta_description": (
            "Official-source facts on Home Chef gift cards: purchase page, redemption, "
            "and terms verified on review date (Unconfirmed where the source did not verify)."
        ),
        "sources": {
            "buy": [
                "https://www.homechef.com/gift-cards",
            ],
            "redeem": [
                "https://www.homechef.com/gift-cards",
            ],
            "terms": [
                "https://www.homechef.com/terms",
            ],
        },
    },
    "factor": {
        "json_key": "factor",
        "provider_name": "Factor",
        "slug": "factor-gift-card",
        "keyword": "factor gift card",
        "monthly_search_volume": 880,
        "title_primary": "Factor Gift Card: Official Sources & Redemption Facts",
        "meta_description": (
            "Factor gift card facts from official pages only; fields marked Unconfirmed "
            "when sources could not be verified on the review date."
        ),
        "sources": {
            "buy": [
                "https://www.factor75.com/",
                "https://www.factor75.com/faq",
            ],
            "redeem": [
                "https://www.factor75.com/faq",
            ],
            "terms": [
                "https://www.factor75.com/pages/terms-and-conditions",
                "https://www.factor75.com/terms",
            ],
        },
    },
    "blue_apron": {
        "json_key": "blue_apron",
        "provider_name": "Blue Apron",
        "slug": "blue-apron-gift-card",
        "keyword": "blue apron gift card",
        "monthly_search_volume": 720,
        "title_primary": "Blue Apron Gift Card: Buy, Redeem & Official Terms",
        "meta_description": (
            "Blue Apron gift card guide using official Blue Apron URLs; Unconfirmed where "
            "pages could not be fetched or verified on the review date."
        ),
        "sources": {
            "buy": [
                "https://www.blueapron.com/gifts",
            ],
            "redeem": [
                "https://www.blueapron.com/gifts",
            ],
            "terms": [
                "https://www.blueapron.com/terms",
            ],
        },
    },
    "cookunity": {
        "json_key": "cookunity",
        "provider_name": "CookUnity",
        "slug": "cookunity-gift-card",
        "keyword": "cookunity gift card",
        "monthly_search_volume": 390,
        "title_primary": "CookUnity Gift Card: Official Buy & Redeem Facts",
        "meta_description": (
            "CookUnity gift cards: official gift-card page and terms, reviewed on date shown; "
            "Unconfirmed when not verifiable from fetched official HTML."
        ),
        "sources": {
            "buy": [
                "https://www.cookunity.com/gift-cards",
            ],
            "redeem": [
                "https://www.cookunity.com/gift-cards",
            ],
            "terms": [
                "https://www.cookunity.com/terms",
            ],
        },
    },
    "hungryroot": {
        "json_key": "hungryroot",
        "provider_name": "Hungryroot",
        "slug": "hungryroot-gift-card",
        "keyword": "hungryroot gift card",
        "monthly_search_volume": 320,
        "title_primary": "Hungryroot Gift Card: Official Sources & Terms",
        "meta_description": (
            "Hungryroot gift card facts from official pages with review date; "
            "Unconfirmed items when official sources did not verify at fetch time."
        ),
        "sources": {
            "buy": [
                "https://www.hungryroot.com/gift",
            ],
            "redeem": [
                "https://www.hungryroot.com/gift",
            ],
            "terms": [
                "https://www.hungryroot.com/terms",
            ],
        },
    },
    "everyplate": {
        "json_key": "everyplate",
        "provider_name": "EveryPlate",
        "slug": "everyplate-gift-card",
        "keyword": "everyplate gift card",
        "monthly_search_volume": 110,
        "title_primary": "EveryPlate Gift Card: Official FAQ & Terms",
        "meta_description": (
            "EveryPlate gift card facts from official HelloFresh-group pages; "
            "Unconfirmed when sources are unavailable or do not mention gift cards."
        ),
        "sources": {
            "buy": [
                "https://www.everyplate.com/gifts",
                "https://www.hellofresh.com/gifts",
            ],
            "redeem": [
                "https://www.hellofresh.com/gift/redeem",
            ],
            "terms": [
                "https://www.everyplate.com/about/termsandconditions",
                "https://www.hellofresh.com/about/termsandconditions",
            ],
        },
    },
    "marley_spoon": {
        "json_key": "marley_spoon",
        "provider_name": "Marley Spoon",
        "slug": "marley-spoon-gift-card",
        "keyword": "marley spoon gift card",
        "monthly_search_volume": 50,
        "title_primary": "Marley Spoon Gift Card: Official Terms & Availability",
        "meta_description": (
            "Marley Spoon gift card facts from official sources; Unconfirmed when gift cards "
            "are not verifiable on fetched official pages at review time."
        ),
        "sources": {
            "buy": [
                "https://marleyspoon.com/",
                "https://marleyspoon.com/gift",
            ],
            "redeem": [
                "https://marleyspoon.com/",
            ],
            "terms": [
                "https://marleyspoon.com/terms",
                "https://marleyspoon.com/pages/terms-and-conditions",
            ],
        },
    },
}


def slug_to_json_key(slug: str) -> str:
    return slug.replace("-", "_").replace("__", "_")
