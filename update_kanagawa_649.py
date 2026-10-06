import argparse
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

FACILITIES_CSV = Path("kanagawa_649_facilities.csv")
OUT_JSON = Path("docs/kanagawa_649_latest.json")
OUT_CSV = Path("docs/kanagawa_649_latest.csv")
BASE_URL = "https://www.kaigokensaku.mhlw.go.jp/14/index.php"
JST = ZoneInfo("Asia/Tokyo")

OUTPUT_FIELDS = [
    "no","facility","type","jigyosyo_cd","service_cd",
    "vacancy","capacity","waiting_count","vacancy_date","public_value",
    "checked_at_jst","feature_http_status","detail_http_status","status","error",
    "feature_url","detail_url"
]

def now_jst():
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
    return f"{BASE_URL}?JigyosyoCd={jigyosyo_cd}&ServiceCd={service_cd}&{action}=true"

def detail_action(service_cd: str) -> str:
    return {
        "510": "action_kouhyou_detail_024_kihon",
        "540": "action_kouhyou_detail_026_kihon",
        "520": "action_kouhyou_detail_027_kihon",
        "550": "action_kouhyou_detail_034_kihon",
    }.get(str(service_cd), "action_kouhyou_detail_024_kihon")

def load_facilities():
    with FACILITIES_CSV.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["no"] = int(r["no"])
    return rows

def load_existing():
    if not OUT_JSON.exists():
        return {}
    try:
        j = json.loads(OUT_JSON.read_text(encoding="utf-8"))
        return {int(x["no"]): x for x in j.get("data", []) if x.get("no")}
    except Exception:
        return {}

def fetch_page(page, url: str, attempts: int = 2):
    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=60000)
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
        vacancy_date = f"{m_date.group(1)}-{int(m_date.group(2)):02d}-{int(m_date.group(3)):02d}"
    return vacancy, capacity, vacancy_date, public_value

def parse_detail(text: str):
    waiting = None
    capacity = None
    m_wait = re.search(r"待機者数(?:（[^）]*）)?[^0-9]{0,500}?(\d+)\s*人", text)
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
        "version": "kanagawa-649-v1",
        "source": "介護サービス情報公表システム（神奈川県）",
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
    OUT_JSON.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    with OUT_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS)
        w.writeheader()
        for row in data:
            w.writerow({k: row.get(k, "") for k in OUTPUT_FIELDS})

def update_one(page, facility):
    no = facility["no"]
    name = facility["facility"]
    ftype = facility["type"]
    jig = (facility.get("jigyosyo_cd") or "").strip()
    service = (facility.get("service_cd") or "").strip()

    feature_url = make_url(jig, service, "action_kouhyou_detail_feature_index")
    detail_url = make_url(jig, service, detail_action(service))

    result = {
        "no": no, "facility": name, "type": ftype,
        "jigyosyo_cd": jig, "service_cd": service,
        "vacancy": None, "capacity": None, "waiting_count": None,
        "vacancy_date": "", "public_value": "",
        "checked_at_jst": now_jst(),
        "feature_http_status": None, "detail_http_status": None,
        "status": "", "error": "",
        "feature_url": feature_url, "detail_url": detail_url,
    }

    if not feature_url:
        result["status"] = "skipped_no_official_url"
        return result

    errors = []

    f_status, f_html, f_err = fetch_page(page, feature_url)
    result["feature_http_status"] = f_status
    if f_html:
        vacancy, cap_feature, vacancy_date, public_value = parse_feature(html_to_text(f_html))
        result["vacancy"] = vacancy
        result["capacity"] = cap_feature
        result["vacancy_date"] = vacancy_date
        result["public_value"] = public_value
    elif f_err:
        errors.append(f"feature:{f_err}")

    d_status, d_html, d_err = fetch_page(page, detail_url)
    result["detail_http_status"] = d_status
    if d_html:
        waiting, cap_detail = parse_detail(html_to_text(d_html))
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
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=int, default=1)
    ap.add_argument("--count", type=int, default=50)
    args = ap.parse_args()

    facilities = load_facilities()
    total = len(facilities)
    start_idx = max(args.start - 1, 0)
    subset = facilities[start_idx:start_idx + args.count]
    records = load_existing()

    print(f"Kanagawa 649 update: start={args.start}, count={len(subset)}, total={total}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        for i, fac in enumerate(subset, 1):
            r = update_one(page, fac)
            records[r["no"]] = r
            print(
                f"[{i}/{len(subset)}] No.{r['no']} {r['facility']} "
                f"status={r['status']} vacancy={r['vacancy']} waiting={r['waiting_count']}"
            )
        browser.close()

    save_outputs(records, total)

if __name__ == "__main__":
    main()
