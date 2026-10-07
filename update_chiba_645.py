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

FACILITIES_CSV = Path("chiba_645_facilities.csv")
OUT_JSON = Path("docs/chiba_645_latest.json")
OUT_CSV = Path("docs/chiba_645_latest.csv")
BASE_URL = "https://www.kaigokensaku.mhlw.go.jp/12/index.php"
JST = ZoneInfo("Asia/Tokyo")

OUTPUT_FIELDS = [
    "no", "id", "facility", "type", "jigyosyo_cd", "service_cd",
    "vacancy", "capacity", "waiting_count", "vacancy_date", "public_value",
    "checked_at_jst", "feature_http_status", "detail_http_status", "status", "error",
    "feature_url", "detail_url",
]


def now_jst():
    return datetime.now(JST).isoformat(timespec="seconds")


def html_to_text(source: str) -> str:
    s = re.sub(r"<script[\s\S]*?</script>", " ", source, flags=re.I)
    s = re.sub(r"<style[\s\S]*?</style>", " ", s, flags=re.I)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</(?:tr|td|th|p|div|li|h[1-6]|section|dt|dd|label)>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s).replace("\u3000", " ")
    return re.sub(r"\s+", " ", s).strip()


def extract_office_numbers(raw: str):
    vals = re.findall(r"[0-9A-Z]{10}", str(raw or "").upper())
    out = []
    for v in vals:
        if v not in out:
            out.append(v)
    return out


def service_cd_for(office_no: str, facility_type: str, configured: str = "") -> str:
    if facility_type == "老健":
        return "520"
    if facility_type == "介護医療院":
        return "550"
    if facility_type == "特養":
        return "540" if str(office_no).startswith("129") else "510"
    return str(configured or "")


def make_url(office_no: str, service_cd: str, action: str) -> str:
    if not office_no or not service_cd:
        return ""
    return f"{BASE_URL}?JigyosyoCd={office_no}-00&ServiceCd={service_cd}&{action}=true"


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
        obj = json.loads(OUT_JSON.read_text(encoding="utf-8"))
        return {int(x["no"]): x for x in obj.get("data", []) if x.get("no") is not None}
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
        v, c = int(m.group(1)), int(m.group(2))
        public_value = f"{v}/{c}人"
        # 0/0 は空きなしではなく、施設側が空き情報を実質未入力と判断する。
        if c > 0:
            vacancy, capacity = v, c
    else:
        m2 = re.search(r"現在の空き数\s*(\d+)\s*人", text)
        if m2:
            vacancy = int(m2.group(1))
            public_value = f"{vacancy}人"

    # 「（2026年06月01日時点）」等。特色ページの空床値の鮮度判定に使う。
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
    m_cap = re.search(r"入所定員[^0-9]{0,150}?(\d+)\s*人", text)
    if m_cap:
        capacity = int(m_cap.group(1))
    return waiting, capacity


def fetch_detail_with_fallback(page, office_no, service_cd):
    actions = [
        "action_kouhyou_detail_024_kani",
        "action_kouhyou_detail_010_kani",
        "action_kouhyou_detail_024_kihon",
    ]
    last = (None, "", "", "")
    for action in actions:
        url = make_url(office_no, service_cd, action)
        st, src, err = fetch_page(page, url)
        last = (st, src, err, url)
        if src:
            txt = html_to_text(src)
            if office_no in txt or "待機者数" in txt or "入所定員" in txt:
                return st, src, err, url
    return last


def fetch_one_office(page, office_no, facility_type, configured_service):
    svc = service_cd_for(office_no, facility_type, configured_service)
    feature_url = make_url(office_no, svc, "action_kouhyou_detail_feature_index")
    fs, fhtml, ferr = fetch_page(page, feature_url)
    vacancy = capacity = waiting = None
    vacancy_date = public_value = ""
    if fhtml:
        vacancy, capacity, vacancy_date, public_value = parse_feature(html_to_text(fhtml))

    ds, dhtml, derr, detail_url = fetch_detail_with_fallback(page, office_no, svc)
    if dhtml:
        waiting, cap2 = parse_detail(html_to_text(dhtml))
        if capacity is None:
            capacity = cap2

    ok = bool(fhtml or dhtml)
    return {
        "ok": ok,
        "service_cd": svc,
        "vacancy": vacancy,
        "capacity": capacity,
        "waiting_count": waiting,
        "vacancy_date": vacancy_date,
        "public_value": public_value,
        "feature_http_status": fs,
        "detail_http_status": ds,
        "feature_url": feature_url,
        "detail_url": detail_url,
        "error": " | ".join(x for x in [ferr and f"feature:{ferr}", derr and f"detail:{derr}"] if x),
    }


def merge_parts(parts):
    ok = [p for p in parts if p.get("ok")]
    if not ok:
        return None

    vv = [p["vacancy"] for p in ok if isinstance(p.get("vacancy"), int)]
    if any(v > 0 for v in vv):
        vacancy = sum(v for v in vv if v > 0)
    elif vv and all(v == 0 for v in vv):
        vacancy = 0
    else:
        vacancy = None

    waits = [p["waiting_count"] for p in ok if isinstance(p.get("waiting_count"), int)]
    waiting = sum(waits) if waits else None
    caps = [p["capacity"] for p in ok if isinstance(p.get("capacity"), int) and p.get("capacity", 0) > 0]
    capacity = sum(caps) if caps else None
    dates = [p["vacancy_date"] for p in ok if p.get("vacancy_date")]
    vacancy_date = max(dates) if dates else ""
    raws = []
    for p in ok:
        if p.get("public_value") and p["public_value"] not in raws:
            raws.append(p["public_value"])
    source = next((p for p in ok if isinstance(p.get("vacancy"), int)), ok[0])
    return {
        "vacancy": vacancy,
        "capacity": capacity,
        "waiting_count": waiting,
        "vacancy_date": vacancy_date,
        "public_value": " / ".join(raws),
        "feature_http_status": source.get("feature_http_status"),
        "detail_http_status": source.get("detail_http_status"),
        "feature_url": source.get("feature_url", ""),
        "detail_url": source.get("detail_url", ""),
        "error": " | ".join(p.get("error", "") for p in parts if p.get("error")),
    }


def update_one(page, fac):
    ids = extract_office_numbers(fac.get("jigyosyo_cd"))
    result = {
        "no": fac["no"], "id": fac.get("id", ""), "facility": fac.get("facility", ""),
        "type": fac.get("type", ""), "jigyosyo_cd": fac.get("jigyosyo_cd", ""),
        "service_cd": fac.get("service_cd", ""), "vacancy": None, "capacity": None,
        "waiting_count": None, "vacancy_date": "", "public_value": "",
        "checked_at_jst": now_jst(), "feature_http_status": None,
        "detail_http_status": None, "status": "", "error": "", "feature_url": "", "detail_url": "",
    }
    if not ids:
        result["status"] = "skipped_no_official_url"
        result["error"] = "事業所番号なし"
        return result

    parts = [fetch_one_office(page, oid, fac.get("type", ""), fac.get("service_cd", "")) for oid in ids]
    merged = merge_parts(parts)
    if not merged:
        result["status"] = "error"
        result["error"] = " | ".join(p.get("error") or "fetch failed" for p in parts)
        if parts:
            result["feature_url"] = parts[0].get("feature_url", "")
            result["detail_url"] = parts[0].get("detail_url", "")
        return result

    result.update(merged)
    if result["vacancy"] is None and result["waiting_count"] is None:
        result["status"] = "ok_no_public_value"
    elif result["vacancy"] is None or result["waiting_count"] is None:
        result["status"] = "ok_partial_public_value"
    else:
        result["status"] = "ok"
    return result


def save_outputs(records, total):
    data = [records[k] for k in sorted(records)]
    status_counts = {}
    for r in data:
        status_counts[r.get("status") or "unknown"] = status_counts.get(r.get("status") or "unknown", 0) + 1
    obj = {
        "version": "chiba-645-v1-mhlw",
        "source": "厚生労働省 介護サービス情報公表システム（千葉県）",
        "source_url": "https://www.kaigokensaku.mhlw.go.jp/12/index.php",
        "total_facilities": total,
        "batch_size": 50,
        "generated_at_jst": now_jst(),
        "summary": {
            "records": len(data),
            "vacancy_values": sum(isinstance(x.get("vacancy"), int) for x in data),
            "waiting_values": sum(isinstance(x.get("waiting_count"), int) for x in data),
            "fresh_vacancy_candidates": None,
            "status_counts": status_counts,
        },
        "rules": {
            "zero_zero": "0/0は空きなしにせず要確認",
            "stale_days": 21,
            "stale_handling": "21日以上前の空床値は現在の空きありに含めず参考表示",
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=int, default=1)
    ap.add_argument("--count", type=int, default=50)
    args = ap.parse_args()
    facilities = load_facilities()
    total = len(facilities)
    start = max(args.start - 1, 0)
    subset = facilities[start:start + max(args.count, 0)]
    existing = load_existing()

    # 未処理施設も全645件の骨格を保持する。
    for f in facilities:
        base = existing.get(f["no"], {})
        for k, v in {
            "no": f["no"], "id": f.get("id", ""), "facility": f.get("facility", ""),
            "type": f.get("type", ""), "jigyosyo_cd": f.get("jigyosyo_cd", ""),
            "service_cd": f.get("service_cd", ""), "vacancy": None, "capacity": None,
            "waiting_count": None, "vacancy_date": "", "public_value": "", "checked_at_jst": "",
            "feature_http_status": None, "detail_http_status": None, "status": "not_checked",
            "error": "", "feature_url": f.get("official_url", ""), "detail_url": "",
        }.items():
            base.setdefault(k, v)
        existing[f["no"]] = base

    print(f"Chiba 645 update: start={args.start}, count={len(subset)}, total={total}")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled", "--no-sandbox"])
        context = browser.new_context(
            locale="ja-JP", timezone_id="Asia/Tokyo",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"),
            viewport={"width": 1440, "height": 1200},
        )
        page = context.new_page()
        for i, fac in enumerate(subset, 1):
            r = update_one(page, fac)
            existing[r["no"]] = r
            print(f"[{i}/{len(subset)}] No.{r['no']} {r['facility']} status={r['status']} vacancy={r['vacancy']} waiting={r['waiting_count']} date={r['vacancy_date']}")
            if i % 5 == 0:
                save_outputs(existing, total)
        browser.close()
    save_outputs(existing, total)


if __name__ == "__main__":
    main()
