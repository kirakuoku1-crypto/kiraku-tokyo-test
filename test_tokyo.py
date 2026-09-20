import html
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

def html_to_text(source: str) -> str:
    # hiddenタブ内の情報も含めてHTML全体をテキスト化する
    s = re.sub(r"<script[\s\S]*?</script>", " ", source, flags=re.I)
    s = re.sub(r"<style[\s\S]*?</style>", " ", s, flags=re.I)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</(?:tr|td|th|p|div|li|h[1-6]|section)>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()

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
        page.wait_for_timeout(2000)
        feature_html = page.content()
        feature_text = html_to_text(feature_html)
        Path("feature_page.html").write_text(feature_html, encoding="utf-8")

        result["feature_http_status"] = response.status if response else None
        result["feature_title"] = page.title()

        m_vac = re.search(r"空き数/定員\s*(\d+)\s*/\s*(\d+)\s*人", feature_text)
        if m_vac:
            result["vacancy"] = int(m_vac.group(1))
            result["capacity_from_vacancy"] = int(m_vac.group(2))
        else:
            m_vac2 = re.search(r"現在の空き数\s*(\d+)\s*人", feature_text)
            if m_vac2:
                result["vacancy"] = int(m_vac2.group(1))

        m_date = re.search(
            r"(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日時点",
            feature_text
        )
        if m_date:
            result["vacancy_date"] = (
                f"{m_date.group(1)}-{int(m_date.group(2)):02d}-{int(m_date.group(3)):02d}"
            )

        # --- 待機者数・定員 ---
        response2 = page.goto(DETAIL_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2000)
        detail_html = page.content()
        detail_text = html_to_text(detail_html)
        Path("detail_page.html").write_text(detail_html, encoding="utf-8")

        result["detail_http_status"] = response2.status if response2 else None
        result["detail_title"] = page.title()

        # hiddenの「サービス内容」領域もHTML全体から取得
        m_wait = re.search(
            r"待機者数(?:（[^）]*）)?[^0-9]{0,500}?(\d+)\s*人",
            detail_text
        )
        if m_wait:
            result["waiting_count"] = int(m_wait.group(1))

        m_cap = re.search(r"入所定員[^0-9]{0,100}?(\d+)\s*人", detail_text)
        if m_cap:
            result["capacity"] = int(m_cap.group(1))

        result["success"] = (
            result.get("feature_http_status") == 200
            and result.get("detail_http_status") == 200
            and isinstance(result.get("vacancy"), int)
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
