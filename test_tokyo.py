import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

FEATURE_URL = (
    "https://www.kaigokensaku.mhlw.go.jp/13/index.php?"
    "JigyosyoCd=1371600303-00&ServiceCd=510&"
    "action_kouhyou_detail_feature_index=true"
)
DETAIL_URL = (
    "https://www.kaigokensaku.mhlw.go.jp/13/index.php?"
    "JigyosyoCd=1371600303-00&ServiceCd=510&"
    "action_kouhyou_detail_024_kihon=true"
)

result = {
    "facility": "ゆたか苑",
    "office_no": "1371600303",
    "tested_at_utc": datetime.now(timezone.utc).isoformat(),
    "feature_url": FEATURE_URL,
    "detail_url": DETAIL_URL,
    "success": False,
}

def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()

try:
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        context = browser.new_context(
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/140.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 1200},
        )
        page = context.new_page()

        # --- 空き情報 ---
        response = page.goto(FEATURE_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)
        feature_text = normalize(page.locator("body").inner_text(timeout=15000))
        result["feature_http_status"] = response.status if response else None
        result["feature_title"] = page.title()
        result["feature_text_head"] = feature_text[:1000]
        Path("feature_page.html").write_text(page.content(), encoding="utf-8")

        m_vac = re.search(r"空き数/定員\s*(\d+)\s*/\s*(\d+)\s*人", feature_text)
        if not m_vac:
            m_vac = re.search(r"現在の空き数\s*(\d+)\s*人", feature_text)
            if m_vac:
                result["vacancy"] = int(m_vac.group(1))
        else:
            result["vacancy"] = int(m_vac.group(1))
            result["capacity_from_vacancy"] = int(m_vac.group(2))

        m_date = re.search(r"（\s*(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日時点\s*）", feature_text)
        if m_date:
            result["vacancy_date"] = f"{m_date.group(1)}-{int(m_date.group(2)):02d}-{int(m_date.group(3)):02d}"

        # --- 待機者数 ---
        response2 = page.goto(DETAIL_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)
        detail_text = normalize(page.locator("body").inner_text(timeout=15000))
        result["detail_http_status"] = response2.status if response2 else None
        result["detail_title"] = page.title()
        result["detail_text_head"] = detail_text[:1000]
        Path("detail_page.html").write_text(page.content(), encoding="utf-8")

        # 「待機者数」から近い場所の数値を優先
        m_wait = re.search(
            r"待機者数[^0-9]{0,250}?(\d+)\s*人",
            detail_text
        )
        if m_wait:
            result["waiting_count"] = int(m_wait.group(1))

        m_cap = re.search(r"入所定員\s*(\d+)\s*人", detail_text)
        if m_cap:
            result["capacity"] = int(m_cap.group(1))

        result["success"] = (
            isinstance(result.get("vacancy"), int)
            and isinstance(result.get("waiting_count"), int)
        )

        browser.close()

except Exception as e:
    result["error"] = f"{type(e).__name__}: {e}"

Path("result.json").write_text(
    json.dumps(result, ensure_ascii=False, indent=2),
    encoding="utf-8",
)

print(json.dumps(result, ensure_ascii=False, indent=2))

if not result["success"]:
    sys.exit(1)
