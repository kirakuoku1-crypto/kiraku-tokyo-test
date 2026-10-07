import argparse
import csv
import html
import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

FACILITIES_CSV = Path("kanagawa_649_facilities.csv")
OUT_JSON = Path("docs/kanagawa_649_latest.json")
OUT_CSV = Path("docs/kanagawa_649_latest.csv")

# 神奈川県の介護情報ポータル。
# 厚労省 kaigokensaku の神奈川県ページは GitHub Actions から 403 になるため、
# 介護情報サービスかながわを取得元にする。
BASE_URL = "https://kaigo.rakuraku.or.jp/search-office/detail.html"
JST = ZoneInfo("Asia/Tokyo")

OUTPUT_FIELDS = [
    "no", "facility", "type", "jigyosyo_cd", "service_cd",
    "vacancy", "capacity", "waiting_count", "vacancy_date", "public_value",
    "checked_at_jst", "feature_http_status", "detail_http_status", "status", "error",
    "feature_url", "detail_url",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/153.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.8,en;q=0.6",
    "Cache-Control": "no-cache",
}


def now_jst():
    return datetime.now(JST).isoformat(timespec="seconds")


def html_to_text(source: str) -> str:
    s = re.sub(r"<script[\s\S]*?</script>", " ", source, flags=re.I)
    s = re.sub(r"<style[\s\S]*?</style>", " ", s, flags=re.I)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(
        r"</(?:tr|td|th|p|div|li|h[1-6]|section|dt|dd|label)>",
        "\n",
        s,
        flags=re.I,
    )
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = s.replace("\u3000", " ")
    return re.sub(r"\s+", " ", s).strip()


def extract_office_numbers(raw: str):
    # "1472700101 / 1472700945-00" のような複数番号にも対応。
    vals = re.findall(r"[0-9A-Z]{10}", str(raw or "").upper())
    out = []
    for v in vals:
        if v not in out:
            out.append(v)
    return out


def service_cd_for(office_no: str, configured: str, facility_type: str) -> str:
    # 老健・介護医療院は固定。
    if facility_type == "老健":
        return "520"
    if facility_type == "介護医療院":
        return "550"

    # 特養は 149... が地域密着型、その他は通常の特養として扱う。
    if facility_type == "特養":
        if str(office_no).startswith("149"):
            return "540"
        return "510"

    return str(configured or "510")


def make_url(office_no: str, service_cd: str) -> str:
    return (
        f"{BASE_URL}?JGNO=ST{office_no}"
        f"&SVCD={service_cd}&THNO=00000"
    )


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
        return {
            int(x["no"]): x
            for x in j.get("data", [])
            if x.get("no") is not None
        }
    except Exception:
        return {}


def decode_body(body: bytes, content_type: str) -> str:
    m = re.search(r"charset=([A-Za-z0-9_\-]+)", content_type or "", re.I)
    encs = [m.group(1)] if m else []
    encs += ["utf-8", "cp932", "shift_jis", "euc_jp"]
    seen = set()
    for enc in encs:
        if not enc or enc.lower() in seen:
            continue
        seen.add(enc.lower())
        try:
            return body.decode(enc)
        except Exception:
            pass
    return body.decode("utf-8", errors="replace")


def fetch_page(url: str, attempts: int = 3):
    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=45) as resp:
                status = getattr(resp, "status", resp.getcode())
                body = resp.read()
                source = decode_body(body, resp.headers.get("Content-Type", ""))
                if status == 200 and source:
                    return status, source, ""
                last_error = f"HTTP {status}"
        except urllib.error.HTTPError as e:
            last_error = f"HTTP {e.code}"
        except urllib.error.URLError as e:
            last_error = f"URL error: {e.reason}"
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"

        if attempt < attempts:
            time.sleep(1.2 * attempt)

    return None, "", last_error


def parse_date_jp(value: str):
    m = re.search(r"(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日", value or "")
    if not m:
        return ""
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def parse_vacancy_field(text: str):
    """
    介護情報サービスかながわの「空き情報」を取得。
    例:
      空きあり(2025年12月03日現在)
      空きわずか(2018年07月16日現在)
      空きなし(...)
      -
    qualitative な「空きあり/わずか」は検索ボタンで拾えるよう vacancy=1 とする。
    実床数ではないので public_value には元表現をそのまま保存する。
    """
    m = re.search(
        r"空き情報\s*(.*?)(?=介護保険事業所番号|指定年月日|最終更新日)",
        text,
        re.I,
    )
    raw = (m.group(1).strip() if m else "")
    raw = re.sub(r"\s+", " ", raw).strip()

    if raw in {"", "-", "－", "―", "ー"}:
        return None, "", ""

    date = parse_date_jp(raw)

    # 実数記載がある場合を最優先。
    m_num = re.search(r"(?:空き|空室|空床)[^0-9]{0,10}(\d+)\s*(?:床|室|人)", raw)
    if m_num:
        n = int(m_num.group(1))
        return n, raw, date

    if re.search(r"空き\s*(?:あり|有り|有)", raw):
        return 1, raw, date
    if re.search(r"空き\s*(?:わずか|僅か)", raw):
        return 1, raw, date
    if re.search(r"(?:空き\s*(?:なし|無し|無)|満床)", raw):
        return 0, raw, date

    # 不明表現は raw だけ残し、数値としては使わない。
    return None, raw, date


def parse_waiting(text: str):
    m = re.search(r"待機者数[^0-9]{0,80}?(\d+)\s*人", text)
    return int(m.group(1)) if m else None


def parse_capacity(text: str, facility_type: str):
    patterns = []
    if facility_type == "特養":
        patterns += [
            r"介護老人福祉施設入所定員[^0-9]{0,80}?(\d+)\s*人",
            r"地域密着型介護老人福祉施設入所者生活介護.*?定員[^0-9]{0,80}?(\d+)\s*人",
        ]
    elif facility_type == "老健":
        patterns += [
            r"介護老人保健施設入所定員[^0-9]{0,80}?(\d+)\s*人",
            r"入所定員[^0-9]{0,80}?(\d+)\s*人",
        ]
    elif facility_type == "介護医療院":
        patterns += [
            r"介護医療院入所定員[^0-9]{0,80}?(\d+)\s*人",
            r"入所定員[^0-9]{0,80}?(\d+)\s*人",
            r"療養床数[^0-9]{0,80}?(\d+)\s*床",
        ]

    for pat in patterns:
        m = re.search(pat, text)
        if m:
            return int(m.group(1))
    return None


def parse_last_update(text: str):
    m = re.search(
        r"最終更新日[^0-9]{0,80}?(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日",
        text,
    )
    if not m:
        return ""
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def merge_pages(parts):
    """
    1施設に複数の事業所番号がある場合は同一施設として集約。
    vacancy: 1件でも「空きあり/わずか」なら open。
    waiting/capacity: 別ユニット等の値を合算。
    """
    successful = [p for p in parts if p.get("ok")]
    if not successful:
        return None

    vacancy_values = [p["vacancy"] for p in successful if p["vacancy"] is not None]
    vacancy = None
    if any(v > 0 for v in vacancy_values):
        vacancy = 1 if all(v <= 1 for v in vacancy_values) else sum(v for v in vacancy_values if v > 0)
    elif vacancy_values and all(v == 0 for v in vacancy_values):
        vacancy = 0

    raw_values = [p["public_value"] for p in successful if p["public_value"]]
    if raw_values:
        uniq = []
        for v in raw_values:
            if v not in uniq:
                uniq.append(v)
        public_value = " / ".join(uniq)
    else:
        public_value = ""

    waits = [p["waiting_count"] for p in successful if p["waiting_count"] is not None]
    waiting = sum(waits) if waits else None

    caps = [p["capacity"] for p in successful if p["capacity"] is not None]
    capacity = sum(caps) if caps else None

    # 空き情報の明示日があれば最も新しい日。
    vacancy_dates = [p["vacancy_date"] for p in successful if p["vacancy_date"]]
    vacancy_date = max(vacancy_dates) if vacancy_dates else ""

    # 空き情報の日付がない場合は、公式ページの最終更新日を参考日として使用。
    if not vacancy_date:
        last_updates = [p["last_update"] for p in successful if p["last_update"]]
        vacancy_date = max(last_updates) if last_updates else ""

    source = next(
        (p for p in successful if p["vacancy"] is not None or p["public_value"]),
        successful[0],
    )

    return {
        "vacancy": vacancy,
        "capacity": capacity,
        "waiting_count": waiting,
        "vacancy_date": vacancy_date,
        "public_value": public_value,
        "feature_url": source["url"],
        "detail_url": source["url"],
    }


def fetch_one_office(office_no: str, configured_service: str, facility_type: str):
    service_cd = service_cd_for(office_no, configured_service, facility_type)
    url = make_url(office_no, service_cd)
    status, source, err = fetch_page(url)
    if not source:
        return {
            "ok": False,
            "status": status,
            "error": err,
            "url": url,
            "service_cd": service_cd,
        }

    text = html_to_text(source)

    # 想定外ページを成功扱いしない。
    if "介護保険事業所番号" not in text or office_no not in text:
        return {
            "ok": False,
            "status": status,
            "error": "official detail page not recognized",
            "url": url,
            "service_cd": service_cd,
        }

    vacancy, public_value, vacancy_date = parse_vacancy_field(text)
    waiting = parse_waiting(text)
    capacity = parse_capacity(text, facility_type)
    last_update = parse_last_update(text)

    return {
        "ok": True,
        "status": status,
        "error": "",
        "url": url,
        "service_cd": service_cd,
        "vacancy": vacancy,
        "capacity": capacity,
        "waiting_count": waiting,
        "vacancy_date": vacancy_date,
        "public_value": public_value,
        "last_update": last_update,
    }


def update_one(facility):
    no = facility["no"]
    name = facility["facility"]
    ftype = facility["type"]
    jig_raw = (facility.get("jigyosyo_cd") or "").strip()
    configured_service = (facility.get("service_cd") or "").strip()

    result = {
        "no": no,
        "facility": name,
        "type": ftype,
        "jigyosyo_cd": jig_raw,
        "service_cd": configured_service,
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
        "feature_url": "",
        "detail_url": "",
    }

    office_numbers = extract_office_numbers(jig_raw)
    if not office_numbers:
        result["status"] = "skipped_no_official_url"
        result["error"] = "事業所番号なし"
        return result

    parts = [
        fetch_one_office(office_no, configured_service, ftype)
        for office_no in office_numbers
    ]

    merged = merge_pages(parts)
    if not merged:
        result["status"] = "error"
        result["error"] = " | ".join(
            f"{office_numbers[i]}:{p.get('error') or 'fetch failed'}"
            for i, p in enumerate(parts)
        )
        if parts:
            result["feature_url"] = parts[0].get("url", "")
            result["detail_url"] = parts[0].get("url", "")
        return result

    result.update(merged)

    ok_parts = [p for p in parts if p.get("ok")]
    result["feature_http_status"] = 200
    result["detail_http_status"] = 200

    failed_parts = [
        f"{office_numbers[i]}:{p.get('error') or 'fetch failed'}"
        for i, p in enumerate(parts)
        if not p.get("ok")
    ]
    if failed_parts:
        result["error"] = " | ".join(failed_parts)

    if result["vacancy"] is None and result["waiting_count"] is None:
        result["status"] = "ok_no_public_value"
    elif result["vacancy"] is None or result["waiting_count"] is None:
        result["status"] = "ok_partial_public_value"
    else:
        result["status"] = "ok"

    return result


def save_outputs(records_by_no, total_facilities: int):
    data = [records_by_no[k] for k in sorted(records_by_no)]

    status_counts = {}
    for row in data:
        status = row.get("status") or "unknown"
        status_counts[status] = status_counts.get(status, 0) + 1

    obj = {
        "version": "kanagawa-649-v2-rakuraku",
        "source": "介護情報サービスかながわ",
        "source_url": "https://kaigo.rakuraku.or.jp/",
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


def preflight(facilities):
    for fac in facilities:
        ids = extract_office_numbers(fac.get("jigyosyo_cd"))
        if not ids:
            continue
        office_no = ids[0]
        svc = service_cd_for(
            office_no,
            fac.get("service_cd", ""),
            fac.get("type", ""),
        )
        url = make_url(office_no, svc)
        status, source, err = fetch_page(url, attempts=2)
        if status == 200 and source:
            text = html_to_text(source)
            if "介護保険事業所番号" in text and office_no in text:
                print(f"Preflight OK: {office_no} {url}")
                return
        raise RuntimeError(
            f"公式取得元に接続できません。データは更新しません。"
            f" office={office_no} status={status} error={err}"
        )
    raise RuntimeError("事業所番号が見つからないため実行できません")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=int, default=1)
    ap.add_argument("--count", type=int, default=50)
    args = ap.parse_args()

    facilities = load_facilities()
    total = len(facilities)
    start_idx = max(args.start - 1, 0)
    subset = facilities[start_idx:start_idx + args.count]

    # 取得元が全面ブロックされた場合、誤った空データを保存せず Actions を赤にする。
    preflight(facilities)

    records = load_existing()

    print(
        f"Kanagawa 649 update v2 (介護情報サービスかながわ): "
        f"start={args.start}, count={len(subset)}, total={total}"
    )

    batch_results = []
    for i, fac in enumerate(subset, 1):
        r = update_one(fac)
        batch_results.append(r)
        records[r["no"]] = r
        print(
            f"[{i}/{len(subset)}] No.{r['no']} {r['facility']} "
            f"status={r['status']} vacancy={r['vacancy']} "
            f"waiting={r['waiting_count']}"
        )
        time.sleep(0.15)

    hard_errors = sum(r.get("status") == "error" for r in batch_results)

    # 全件エラーなら誤更新を保存しない。
    if batch_results and hard_errors == len(batch_results):
        raise RuntimeError(
            f"このバッチは {hard_errors}/{len(batch_results)} 件すべて取得失敗。"
            "出力を更新せず停止します。"
        )

    save_outputs(records, total)

    print(
        json.dumps(
            {
                "batch": len(batch_results),
                "hard_errors": hard_errors,
                "vacancy_values": sum(
                    isinstance(r.get("vacancy"), int) for r in batch_results
                ),
                "waiting_values": sum(
                    isinstance(r.get("waiting_count"), int) for r in batch_results
                ),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
