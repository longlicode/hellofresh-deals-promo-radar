"""Daily Gift Card Publisher for mealkitdeals.com.

Publishes 1 dedicated gift card guide page per day strictly following
the approved US search volume ranking.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT / "data" / "gift_card_guides.json"
SITE_ILANG = ROOT / ".ilang" / "site.ilang"

QUEUE = [
    {
        "provider_name": "HelloFresh",
        "slug": "hellofresh-gift-card",
        "keyword": "hellofresh gift card",
        "volume": 3600,
        "date": "2026-09-21",
    },
    {
        "provider_name": "Home Chef",
        "slug": "home-chef-gift-card",
        "keyword": "home chef gift card",
        "volume": 1000,
        "date": "2026-09-22",
    },
    {
        "provider_name": "Factor",
        "slug": "factor-gift-card",
        "keyword": "factor gift card",
        "volume": 880,
        "date": "2026-09-23",
    },
    {
        "provider_name": "Blue Apron",
        "slug": "blue-apron-gift-card",
        "keyword": "blue apron gift card",
        "volume": 720,
        "date": "2026-09-24",
    },
    {
        "provider_name": "CookUnity",
        "slug": "cookunity-gift-card",
        "keyword": "cookunity gift card",
        "volume": 390,
        "date": "2026-09-25",
    },
    {
        "provider_name": "Hungryroot",
        "slug": "hungryroot-gift-card",
        "keyword": "hungryroot gift card",
        "volume": 320,
        "date": "2026-09-26",
    },
    {
        "provider_name": "EveryPlate",
        "slug": "everyplate-gift-card",
        "keyword": "everyplate gift card",
        "volume": 110,
        "date": "2026-09-27",
    },
    {
        "provider_name": "Marley Spoon",
        "slug": "marley-spoon-gift-card",
        "keyword": "marley spoon gift card",
        "volume": 50,
        "date": "2026-09-28",
    },
]


def main() -> None:
    print(f"Daily Gift Card Publisher loaded with {len(QUEUE)} brand tasks.")
    if not DATA_FILE.exists():
        print("data/gift_card_guides.json not found.", file=sys.stderr)
        sys.exit(1)

    guides = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    today_str = date.today().isoformat()
    print(f"Current date: {today_str}. Active published guides: {len(guides)}")


if __name__ == "__main__":
    main()
