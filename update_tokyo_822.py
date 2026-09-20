import argparse
import base64
import gzip
import csv
import html
import json
import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

FACILITIES_CSV = Path("tokyo_822_facilities.csv")
OUT_JSON = Path("docs/tokyo_822_latest.json")
OUT_CSV = Path("docs/tokyo_822_latest.csv")
BASE_URL = "https://www.kaigokensaku.mhlw.go.jp/13/index.php"
JST = ZoneInfo("Asia/Tokyo")

FACILITIES_GZ_B64 = """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"""

OUTPUT_FIELDS = [
    "no", "facility", "type", "jigyosyo_cd", "service_cd",
    "vacancy", "capacity", "waiting_count", "vacancy_date", "public_value",
    "checked_at_jst", "feature_http_status", "detail_http_status",
    "status", "error", "feature_url", "detail_url",
]


def now_jst() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def html_to_text(source: str) -> str:
    s = re.sub(r"<script[\s\S]*?</script>", " ", source, flags=re.I)
    s = re.sub(r"<style[\s\S]*?</style>", " ", s, flags=re.I)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</(?:tr|td|th|p|div|li|h[1-6]|section)>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def make_url(jigyosyo_cd: str, service_cd: str, action: str) -> str:
    if not jigyosyo_cd or not service_cd:
        return ""
    return (
        f"{BASE_URL}?JigyosyoCd={jigyosyo_cd}&ServiceCd={service_cd}"
        f"&{action}=true"
    )


def load_facilities():
    if FACILITIES_CSV.exists():
        text = FACILITIES_CSV.read_text(encoding="utf-8-sig")
    else:
        text = gzip.decompress(base64.b64decode(FACILITIES_GZ_B64)).decode("utf-8")
    rows = list(csv.DictReader(text.splitlines()))
    for row in rows:
        row["no"] = int(row["no"])
    return rows


def load_existing():
    if not OUT_JSON.exists():
        return {}
    try:
        obj = json.loads(OUT_JSON.read_text(encoding="utf-8"))
        return {int(x["no"]): x for x in obj.get("data", []) if x.get("no")}
    except Exception:
        return {}


def fetch_page(page, url: str, attempts: int = 2):
    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=60000)
            # The official pages are server-rendered. A short pause stabilizes the DOM
            # without the 2-second per-page delay used in the one-facility test.
            page.wait_for_timeout(650)
            source = page.content()
            status = response.status if response else None
            if status == 200 and source:
                return status, source, ""
            last_error = f"HTTP {status}"
        except PlaywrightTimeoutError as e:
            last_error = f"Timeout: {e}"
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
        if attempt < attempts:
            time.sleep(1.5 * attempt)
    return None, "", last_error


def parse_feature(text: str):
    vacancy = None
    capacity = None
    vacancy_date = ""
    public_value = ""

    m = re.search(r"空き数/定員\s*(\d+)\s*/\s*(\d+)\s*人", text)
    if m:
        vacancy = int(m.group(1))
        capacity = int(m.group(2))
        public_value = f"{vacancy}/{capacity}人"
    else:
        m2 = re.search(r"現在の空き数\s*(\d+)\s*人", text)
        if m2:
            vacancy = int(m2.group(1))
            public_value = f"{vacancy}人"

    m_date = re.search(r"(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日時点", text)
    if m_date:
        vacancy_date = (
            f"{m_date.group(1)}-{int(m_date.group(2)):02d}-{int(m_date.group(3)):02d}"
        )

    return vacancy, capacity, vacancy_date, public_value


def parse_detail(text: str):
    waiting = None
    capacity = None

    m_wait = re.search(
        r"待機者数(?:（[^）]*）)?[^0-9]{0,500}?(\d+)\s*人",
        text,
    )
    if m_wait:
        waiting = int(m_wait.group(1))

    m_cap = re.search(r"入所定員[^0-9]{0,100}?(\d+)\s*人", text)
    if m_cap:
        capacity = int(m_cap.group(1))

    return waiting, capacity


def save_outputs(records_by_no, total_facilities: int):
    data = [records_by_no[k] for k in sorted(records_by_no)]

    status_counts = {}
    for row in data:
        status = row.get("status") or "unknown"
        status_counts[status] = status_counts.get(status, 0) + 1

    obj = {
        "version": "tokyo-822-v1",
        "source": "介護サービス情報公表システム（東京都）",
        "total_facilities": total_facilities,
        "batch_size": 50,
        "generated_at_jst": now_jst(),
        "summary": {
            "records": len(data),
            "vacancy_values": sum(isinstance(x.get("vacancy"), int) for x in data),
            "waiting_values": sum(isinstance(x.get("waiting_count"), int) for x in data),
            "status_counts": status_counts,
        },
        "data": data,
    }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    with OUT_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        for row in data:
            writer.writerow({k: row.get(k, "") for k in OUTPUT_FIELDS})


def update_one(page, facility):
    no = facility["no"]
    name = facility["facility"]
    ftype = facility["type"]
    jig = (facility.get("jigyosyo_cd") or "").strip()
    service = (facility.get("service_cd") or "").strip()

    feature_url = make_url(jig, service, "action_kouhyou_detail_feature_index")
    detail_url = make_url(jig, service, "action_kouhyou_detail_024_kihon")

    result = {
        "no": no,
        "facility": name,
        "type": ftype,
        "jigyosyo_cd": jig,
        "service_cd": service,
        "vacancy": None,
        "capacity": None,
        "waiting_count": None,
        "vacancy_date": "",
        "public_value": "",
        "checked_at_jst": now_jst(),
        "feature_http_status": None,
        "detail_http_status": None,
        "status": "",
        "error": "",
        "feature_url": feature_url,
        "detail_url": detail_url,
    }

    if not feature_url:
        result["status"] = "skipped_no_official_url"
        return result

    errors = []

    f_status, f_html, f_err = fetch_page(page, feature_url)
    result["feature_http_status"] = f_status
    if f_html:
        f_text = html_to_text(f_html)
        vacancy, cap_feature, vacancy_date, public_value = parse_feature(f_text)
        result["vacancy"] = vacancy
        result["capacity"] = cap_feature
        result["vacancy_date"] = vacancy_date
        result["public_value"] = public_value
    elif f_err:
        errors.append(f"feature:{f_err}")

    d_status, d_html, d_err = fetch_page(page, detail_url)
    result["detail_http_status"] = d_status
    if d_html:
        d_text = html_to_text(d_html)
        waiting, cap_detail = parse_detail(d_text)
        result["waiting_count"] = waiting
        if result["capacity"] is None:
            result["capacity"] = cap_detail
    elif d_err:
        errors.append(f"detail:{d_err}")

    if f_status == 200 and d_status == 200:
        if result["vacancy"] is None and result["waiting_count"] is None:
            result["status"] = "ok_no_public_value"
        elif result["vacancy"] is None or result["waiting_count"] is None:
            result["status"] = "ok_partial_public_value"
        else:
            result["status"] = "ok"
    elif f_status == 200 or d_status == 200:
        result["status"] = "partial_http"
    else:
        result["status"] = "error"

    result["error"] = " | ".join(errors)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=int, default=1, help="1-based facility number to start")
    parser.add_argument("--count", type=int, default=50, help="number of facilities to process")
    args = parser.parse_args()

    facilities = load_facilities()
    total = len(facilities)
    start_idx = max(0, args.start - 1)
    selected = facilities[start_idx:start_idx + max(0, args.count)]

    existing = load_existing()
    # Ensure all 822 facilities and all output fields exist before refresh.
    # This also upgrades the compact seed file into the full current schema.
    for f in facilities:
        feature_url = make_url(
            (f.get("jigyosyo_cd") or "").strip(),
            (f.get("service_cd") or "").strip(),
            "action_kouhyou_detail_feature_index",
        )
        detail_url = make_url(
            (f.get("jigyosyo_cd") or "").strip(),
            (f.get("service_cd") or "").strip(),
            "action_kouhyou_detail_024_kihon",
        )
        base = existing.get(f["no"], {})
        base.setdefault("no", f["no"])
        base.setdefault("facility", f["facility"])
        base.setdefault("type", f["type"])
        base.setdefault("jigyosyo_cd", f.get("jigyosyo_cd", ""))
        base.setdefault("service_cd", f.get("service_cd", ""))
        base.setdefault("vacancy", None)
        base.setdefault("capacity", None)
        base.setdefault("waiting_count", None)
        base.setdefault("vacancy_date", "")
        base.setdefault("public_value", "")
        base.setdefault("checked_at_jst", "")
        base.setdefault("feature_http_status", None)
        base.setdefault("detail_http_status", None)
        base.setdefault("status", "not_checked")
        base.setdefault("error", "")
        base.setdefault("feature_url", feature_url)
        base.setdefault("detail_url", detail_url)
        existing[f["no"]] = base

    print(f"Tokyo 822 update: start={args.start}, count={len(selected)}, total={total}")

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

        for i, facility in enumerate(selected, start=1):
            result = update_one(page, facility)
            existing[result["no"]] = result
            print(
                f"[{i}/{len(selected)}] No.{result['no']} {result['facility']} "
                f"status={result['status']} vacancy={result['vacancy']} "
                f"waiting={result['waiting_count']}"
            )
            # Checkpoint every 5 facilities; batch commits happen every 50 in Actions.
            if i % 5 == 0:
                save_outputs(existing, total)

        browser.close()

    save_outputs(existing, total)


if __name__ == "__main__":
    main()
