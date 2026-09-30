#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Kiraku Osaka 851 official vacancy/waiting updater.

Sources (priority):
  1) MHLW Kaigo Service Information Publication System (official/public; feature + kani overview + kihon detail)
  2) Facility-owned official website linked from MHLW detail page or seed

Classification:
  - 空床取得可能: machine-readable current vacancy count/status is available from an official/public source.
  - 待機人数のみ取得可能: no valid vacancy is available, but numeric waiting count is available.
  - 取得不可: neither is machine-readable from the checked official/public sources.

The script intentionally rejects clearly unusable values such as 0/0 capacity and
"お問い合わせください"-only pages as vacancy data.
"""
from __future__ import annotations

import argparse
import csv
import html
import io
import json
import gzip
import zipfile
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

SEED_CSV = Path("osaka_851_facilities.csv")
OUT_JSON = Path("docs/osaka_851_latest.json")
OUT_CSV = Path("docs/osaka_851_latest.csv")
CLASS_JSON = Path("docs/osaka_851_classification.json")
CLASS_CSV = Path("docs/osaka_851_classification.csv")
CACHE_DIR = Path(".cache")

MHLW_INDEX = "https://www.mhlw.go.jp/stf/kaigo-kouhyou_opendata.html"
KAIGO_BASE = "https://www.kaigokensaku.mhlw.go.jp/27/index.php"
JST = ZoneInfo("Asia/Tokyo")
UA = "Mozilla/5.0 (compatible; KirakuOfficialDataBot/1.0; +https://www.kaigokensaku.mhlw.go.jp/)"

# Current fallback URLs (2026-06-30 dataset, output 2026-07-09).
MHLW_FALLBACK = {
    "510": "https://www.mhlw.go.jp/content/12300000/jigyosho_510_all_20260709180754.csv",
    "520": "https://www.mhlw.go.jp/content/12300000/jigyosho_520_all_20260709180802.csv",
    "540": "https://www.mhlw.go.jp/content/12300000/jigyosho_540_all_20260709180811.csv",
    "550": "https://www.mhlw.go.jp/content/12300000/jigyosho_550_all_20260709180931.csv",
}
DETAIL_ACTION = {
    "510": "action_kouhyou_detail_024_kihon",
    "520": "action_kouhyou_detail_027_kihon",
    "540": "action_kouhyou_detail_026_kihon",
    "550": "action_kouhyou_detail_034_kihon",
}
WAIT_ACTION = {
    # MHLW simplified overview pages expose the numeric 待機者数 reliably.
    # The detailed kihon pages are still fetched separately for homepage links
    # and other official metadata.
    "510": "action_kouhyou_detail_024_kani",
    "520": "action_kouhyou_detail_027_kani",
    "540": "action_kouhyou_detail_026_kani",
    "550": "action_kouhyou_detail_034_kani",
}

OUTPUT_FIELDS = [
    "no", "facility", "type", "address", "city", "jigyosyo_cd", "service_cd",
    "match_method", "match_score", "classification",
    "vacancy", "capacity", "waiting_count", "vacancy_date", "public_value",
    "vacancy_source_type", "vacancy_source_url", "waiting_source_url",
    "official_homepage", "checked_at_jst", "feature_http_status", "detail_http_status",
    "status", "error", "feature_url", "detail_url",
]


def now_jst() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def request_bytes(url: str, timeout: int = 45) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ja,en;q=0.5"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def request_text(url: str, timeout: int = 45) -> Tuple[Optional[int], str, str]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ja,en;q=0.5"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(3_000_000)
            ctype = (r.headers.get("Content-Type") or "").lower()
            charset = "utf-8"
            m = re.search(r"charset=([\w\-]+)", ctype)
            if m:
                charset = m.group(1)
            try:
                text = raw.decode(charset, errors="replace")
            except LookupError:
                text = raw.decode("utf-8", errors="replace")
            return getattr(r, "status", 200), text, ""
    except Exception as e:
        return None, "", f"{type(e).__name__}: {e}"


def fetch_mhlw_page(page, url: str, attempts: int = 2) -> Tuple[Optional[int], str, str]:
    """Fetch MHLW public pages using a real browser, matching the proven Tokyo updater."""
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
        try:
            page.wait_for_timeout(900 * attempt)
        except Exception:
            pass
    return None, "", last_error


def html_to_text(source: str) -> str:
    s = re.sub(r"<script[\s\S]*?</script>", " ", source, flags=re.I)
    s = re.sub(r"<style[\s\S]*?</style>", " ", s, flags=re.I)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</(?:p|div|li|tr|h\d|section|article)>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"[ \t\u3000]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").lower()
    s = s.replace("ヶ", "ケ")
    s = re.sub(r"[\s\u3000・･\-ー―‐–—()（）【】\[\]「」『』,，.．/／\\]", "", s)
    return s


def norm_name(s: str) -> str:
    s = norm(s)
    # Corporation names in the Osaka registry are often prefixed/truncated.
    for p in [
        "社会福祉法人", "医療法人社団", "医療法人財団", "医療法人", "社会医療法人",
        "公益財団法人", "一般財団法人", "一般社団法人", "地方独立行政法人",
    ]:
        s = s.replace(norm(p), "")
    return s


def address_key(s: str) -> str:
    s = norm(s)
    s = s.replace("大阪府", "")
    # normalize Japanese address numerals only enough for fuzzy containment
    return s


def digit_code_from_row(row: Dict[str, str]) -> str:
    for v in row.values():
        x = unicodedata.normalize("NFKC", str(v or "")).strip().upper()
        if re.fullmatch(r"[0-9A-Z]{10}(?:-00)?", x):
            return x[:10]
    return ""


def pick_column(headers: Iterable[str], exact: Iterable[str], contains: Iterable[str]) -> Optional[str]:
    hs = list(headers)
    nmap = {norm(h): h for h in hs}
    for c in exact:
        if norm(c) in nmap:
            return nmap[norm(c)]
    for c in contains:
        nc = norm(c)
        for h in hs:
            if nc and nc in norm(h):
                return h
    return None


def discover_mhlw_urls() -> Dict[str, str]:
    urls = dict(MHLW_FALLBACK)
    status, page, err = request_text(MHLW_INDEX)
    if status == 200 and page:
        for service in ["510", "520", "540", "550"]:
            found = re.findall(
                rf'href=["\']([^"\']*jigyosho_{service}_all_[^"\']+\.csv)["\']', page, flags=re.I
            )
            if found:
                urls[service] = urllib.parse.urljoin(MHLW_INDEX, found[0])
    return urls


def decode_csv(raw: bytes) -> str:
    # MHLW notes that service CSV downloads may be ZIP-compressed even when the link name ends in .csv.
    if raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith((".csv", ".txt"))]
            if not names:
                names = zf.namelist()
            raw = zf.read(names[0])
    elif raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    for enc in ("utf-8-sig", "utf-8", "cp932", "shift_jis"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", errors="replace")


def load_mhlw_service(service: str, url: str) -> List[Dict[str, str]]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / f"mhlw_{service}.csv"
    if cache.exists() and cache.stat().st_size > 1000:
        raw = cache.read_bytes()
    else:
        raw = request_bytes(url, timeout=90)
        cache.write_bytes(raw)
    text = decode_csv(raw)
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    if not reader.fieldnames:
        return rows
    headers = reader.fieldnames
    code_col = pick_column(headers, ["介護保険事業所番号", "事業所番号"], ["事業所番号"])
    name_col = pick_column(headers, ["事業所名称", "事業所名", "施設名称", "施設名"], ["事業所名称", "事業所名"])
    pref_col = pick_column(headers, ["都道府県名"], ["都道府県"])
    city_col = pick_column(headers, ["市区町村名"], ["市区町村"])
    addr_col = pick_column(headers, ["事業所所在地", "所在地", "住所"], ["所在地", "住所"])
    phone_col = pick_column(headers, ["電話番号"], ["電話"])
    hp_col = pick_column(headers, ["ホームページ", "ホームページURL", "URL"], ["ホームページ"])

    for src in reader:
        pref = (src.get(pref_col, "") if pref_col else "") or ""
        # If prefecture column is absent, infer from address.
        addr = (src.get(addr_col, "") if addr_col else "") or ""
        code = ((src.get(code_col, "") if code_col else "") or "").strip().upper()
        code = re.sub(r"-00$", "", code)
        if not re.fullmatch(r"[0-9A-Z]{10}", code):
            code = digit_code_from_row(src)
        # Prefer explicit prefecture/address filtering; otherwise Osaka office numbers are 27-prefixed.
        if pref and "大阪" not in pref:
            continue
        if not pref and "大阪" not in addr and not code.startswith("27"):
            continue
        name = ((src.get(name_col, "") if name_col else "") or "").strip()
        city = ((src.get(city_col, "") if city_col else "") or "").strip()
        phone = ((src.get(phone_col, "") if phone_col else "") or "").strip()
        hp = ((src.get(hp_col, "") if hp_col else "") or "").strip()
        if not name or not code:
            continue
        rows.append({
            "jigyosyo_cd": code,
            "service_cd": service,
            "name": name,
            "city": city,
            "address": addr,
            "phone": phone,
            "homepage": hp,
            "name_norm": norm_name(name),
            "addr_norm": address_key(addr),
            "city_norm": norm(city),
        })
    return rows


def similarity(a: str, b: str) -> float:
    # Lightweight similarity that works well for Japanese facility names without third-party deps.
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return min(len(a), len(b)) / max(len(a), len(b)) * 0.25 + 0.75
    # character bigram Dice coefficient
    def grams(s: str):
        return {s[i:i+2] for i in range(max(1, len(s)-1))} if len(s) > 1 else {s}
    ga, gb = grams(a), grams(b)
    if not ga or not gb:
        return 0.0
    return 2 * len(ga & gb) / (len(ga) + len(gb))


def match_facility(seed: Dict[str, str], candidates: List[Dict[str, str]]) -> Tuple[str, str, float, Optional[Dict[str, str]]]:
    seed_code = (seed.get("jigyosyo_cd_seed") or "").strip().upper().replace("-00", "")
    if seed_code:
        for c in candidates:
            if c["jigyosyo_cd"] == seed_code:
                return c["jigyosyo_cd"], "seed_code", 1.0, c

    nn = norm_name(seed.get("facility", ""))
    aa = address_key(seed.get("address", ""))
    cc = norm(seed.get("city", ""))
    pp = norm(seed.get("phone", ""))
    scored = []
    for c in candidates:
        ns = similarity(nn, c["name_norm"])
        if ns < 0.45:
            continue
        city_bonus = 0.12 if cc and (cc in c["city_norm"] or c["city_norm"] in cc) else 0.0
        addr_bonus = 0.0
        if aa and c["addr_norm"]:
            if aa in c["addr_norm"] or c["addr_norm"] in aa:
                addr_bonus = 0.18
            else:
                addr_bonus = min(0.12, similarity(aa, c["addr_norm"]) * 0.12)
        phone_bonus = 0.08 if pp and c.get("phone") and norm(c["phone"]) == pp else 0.0
        score = min(1.0, ns * 0.72 + city_bonus + addr_bonus + phone_bonus)
        scored.append((score, c))
    scored.sort(key=lambda x: x[0], reverse=True)
    if not scored:
        return "", "unmatched", 0.0, None
    score, best = scored[0]
    # Prefer precision; ambiguous matches remain unmatched.
    second = scored[1][0] if len(scored) > 1 else 0.0
    if score >= 0.78 and (score - second >= 0.06 or score >= 0.92):
        return best["jigyosyo_cd"], "mhlw_name_address", round(score, 3), best
    return "", "unmatched_ambiguous", round(score, 3), best


def make_url(code: str, service: str, action: str) -> str:
    if not code or not service:
        return ""
    return f"{KAIGO_BASE}?JigyosyoCd={urllib.parse.quote(code + '-00')}&ServiceCd={service}&{action}=true"


def parse_feature(text: str) -> Tuple[Optional[int], Optional[int], str, str, bool]:
    vacancy = None
    capacity = None
    date = ""
    public_value = ""
    valid = False
    m = re.search(r"空き数\s*/\s*定員\s*(\d+)\s*/\s*(\d+)\s*人", text)
    if m:
        vacancy, capacity = int(m.group(1)), int(m.group(2))
        public_value = f"{vacancy}/{capacity}人"
        valid = capacity > 0
    else:
        m2 = re.search(r"現在の空き数\s*(\d+)\s*人", text)
        if m2:
            vacancy = int(m2.group(1))
            public_value = f"{vacancy}人"
            valid = True
    dm = re.search(r"(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日時点", text)
    if dm:
        date = f"{dm.group(1)}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}"
    return vacancy, capacity, date, public_value, valid


def parse_detail(text: str) -> Tuple[Optional[int], Optional[int]]:
    wait = None
    cap = None

    # html_to_text() normalizes NFKC, so Japanese full-width parentheses become
    # ASCII parentheses. MHLW rows often look like either:
    #   待機者数(入所希望者で入所していない者の数) 5人
    # or:
    #   待機者数 ... あり (その人数: 5人)
    # First prefer a value on the same rendered table row. This avoids consuming
    # an unrelated capacity/occupancy number from a later row.
    for line in text.splitlines():
        if "待機者数" not in line:
            continue
        tail = line.split("待機者数", 1)[1]
        m = re.search(r"(\d+)\s*人", tail)
        if m:
            wait = int(m.group(1))
            break

    # Some MHLW layouts insert a line break inside the waiting-count row. In
    # those cases only accept a number after the explicit 'その人数' cue.
    if wait is None:
        pos = text.find("待機者数")
        if pos >= 0:
            chunk = text[pos:pos + 700]
            m = re.search(r"その人数[^0-9\n]{0,180}?(\d+)\s*人", chunk)
            if m:
                wait = int(m.group(1))

    # Capacity must come from the actual 入所定員 row, not from explanatory
    # text inside the 待機者数 label (which also contains the words 入所定員).
    for line in text.splitlines():
        if not re.match(r"^\s*入所定員", line):
            continue
        tail = line.split("入所定員", 1)[1]
        m2 = re.search(r"(\d+)\s*人", tail)
        if m2:
            cap = int(m2.group(1))
            break
    return wait, cap


class LinkParser:
    def __init__(self, source: str):
        self.links: List[Tuple[str, str]] = []
        self._parse(source)

    def _parse(self, source: str):
        # Good-enough extractor for public HTML; resolves entities later.
        for m in re.finditer(r"<a\b([^>]*)>([\s\S]*?)</a>", source, flags=re.I):
            attrs, body = m.group(1), m.group(2)
            hm = re.search(r"href\s*=\s*[\"']([^\"']+)[\"']", attrs, flags=re.I)
            if not hm:
                continue
            href = html.unescape(hm.group(1).strip())
            text = html_to_text(body)
            self.links.append((href, text))


def extract_official_homepage(detail_html: str, seed_homepage: str = "", matched_homepage: str = "") -> str:
    for u in [seed_homepage, matched_homepage]:
        if u and re.match(r"^https?://", u):
            return u
    links = LinkParser(detail_html).links
    # Prefer links near/labelled home page and exclude the official system itself.
    for href, text in links:
        u = urllib.parse.urljoin(KAIGO_BASE, href)
        if not re.match(r"^https?://", u):
            continue
        if "kaigokensaku.mhlw.go.jp" in u:
            continue
        if "ホームページ" in text or "website" in text.lower():
            return u
    # Last resort: any external http link.
    for href, text in links:
        u = urllib.parse.urljoin(KAIGO_BASE, href)
        if re.match(r"^https?://", u) and "kaigokensaku.mhlw.go.jp" not in u:
            return u
    return ""


def same_site(a: str, b: str) -> bool:
    try:
        na = urllib.parse.urlparse(a).netloc.lower().removeprefix("www.")
        nb = urllib.parse.urlparse(b).netloc.lower().removeprefix("www.")
        return na == nb
    except Exception:
        return False


def extract_official_vacancy(text: str) -> Tuple[Optional[int], str]:
    t = unicodedata.normalize("NFKC", text)
    # Numeric room/bed count near a vacancy keyword.
    patterns = [
        r"(?:空床|空室|空き(?:状況|情報)?)[^\n0-9]{0,35}(\d+)\s*(?:床|室|人)",
        r"(?:残り|残室|残床)[^\n0-9]{0,15}(\d+)\s*(?:床|室|人)",
        r"(\d+)\s*(?:床|室|人)[^\n]{0,25}(?:空き|空床|空室)",
    ]
    for p in patterns:
        m = re.search(p, t, flags=re.I)
        if m:
            return int(m.group(1)), m.group(0).strip()
    # Machine-readable status is useful even without a count; encode full/no vacancy as 0 and unknown positive as None + status.
    m = re.search(r"(?:空床|空室|空き(?:状況|情報)?)[^\n]{0,40}?(満床|満室|空きなし|空床なし|空室なし|空きあり|空床あり|空室あり|○|△|×)", t, flags=re.I)
    if not m:
        m = re.search(r"(?:入所|入居|特養|老健|介護医療院)[^\n]{0,30}?(満床|満室|空きなし|空床なし|空室なし|空きあり|空床あり|空室あり)", t, flags=re.I)
    if m:
        s = m.group(1)
        if s in {"満床", "満室", "空きなし", "空床なし", "空室なし", "×"}:
            return 0, m.group(0).strip()
        # Positive but number unknown: sentinel -1 means 'available status'.
        return -1, m.group(0).strip()
    return None, ""


def find_vacancy_date(text: str) -> str:
    t = unicodedata.normalize("NFKC", text)
    for p in [
        r"(20\d{2})[/.年-](\d{1,2})[/.月-](\d{1,2})日?",
        r"(\d{1,2})月(\d{1,2})日",
    ]:
        m = re.search(p, t)
        if m:
            if len(m.groups()) == 3:
                return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
            return f"{datetime.now(JST).year:04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return ""


def crawl_official_homepage(homepage: str) -> Tuple[Optional[int], str, str, str]:
    """Return vacancy (count; -1=status-only), public value, date, source URL."""
    if not homepage or not re.match(r"^https?://", homepage):
        return None, "", "", ""
    status, src, err = request_text(homepage, timeout=25)
    if status != 200 or not src:
        return None, "", "", ""
    text = html_to_text(src)
    vac, val = extract_official_vacancy(text)
    if vac is not None:
        # "お問い合わせください" alone is not treated as vacancy data by extract_official_vacancy.
        return vac, val, find_vacancy_date(text), homepage

    links = LinkParser(src).links
    candidates = []
    kw = re.compile(r"空床|空室|空き|vacan|入所.*状況|利用.*状況", re.I)
    for href, label in links:
        u = urllib.parse.urljoin(homepage, href)
        if not same_site(homepage, u):
            continue
        if kw.search(label + " " + u):
            candidates.append(u)
    seen = {homepage}
    for u in candidates[:6]:
        if u in seen:
            continue
        seen.add(u)
        st, s, er = request_text(u, timeout=25)
        if st != 200 or not s:
            continue
        tx = html_to_text(s)
        vac, val = extract_official_vacancy(tx)
        if vac is not None:
            return vac, val, find_vacancy_date(tx), u
    return None, "", "", ""


def load_seed() -> List[Dict[str, str]]:
    with SEED_CSV.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def update_one(page, seed: Dict[str, str], candidates_by_service: Dict[str, List[Dict[str, str]]]) -> Dict[str, object]:
    no = int(seed["no"])
    service = seed["service_cd"]
    code, method, score, matched = match_facility(seed, candidates_by_service.get(service, []))
    feature_url = make_url(code, service, "action_kouhyou_detail_feature_index") if code else ""
    detail_url = make_url(code, service, DETAIL_ACTION.get(service, "action_kouhyou_detail_024_kihon")) if code else ""
    waiting_url = make_url(code, service, WAIT_ACTION.get(service, "action_kouhyou_detail_024_kani")) if code else ""

    result: Dict[str, object] = {
        "no": no, "facility": seed["facility"], "type": seed["type"], "address": seed["address"],
        "city": seed.get("city", ""), "jigyosyo_cd": code, "service_cd": service,
        "match_method": method, "match_score": score, "classification": "取得不可",
        "vacancy": None, "capacity": None, "waiting_count": None, "vacancy_date": "", "public_value": "",
        "vacancy_source_type": "", "vacancy_source_url": "", "waiting_source_url": "",
        "official_homepage": "", "checked_at_jst": now_jst(),
        "feature_http_status": None, "detail_http_status": None, "status": "", "error": "",
        "feature_url": feature_url, "detail_url": detail_url,
    }
    if not code:
        result["status"] = "unmatched_no_official_page"
        result["error"] = "MHLW open data could not be matched with sufficient confidence"
        return result

    errors = []
    f_status, f_html, f_err = fetch_mhlw_page(page, feature_url)
    d_status, d_html, d_err = fetch_mhlw_page(page, detail_url)
    w_status, w_html, w_err = fetch_mhlw_page(page, waiting_url)
    result["feature_http_status"] = f_status
    result["detail_http_status"] = d_status
    if f_err:
        errors.append("feature:" + f_err)
    if d_err:
        errors.append("detail:" + d_err)
    if w_err:
        errors.append("waiting:" + w_err)

    vacancy = None
    capacity_feature = None
    feature_valid = False
    if f_html:
        fv, fc, fd, fp, valid = parse_feature(html_to_text(f_html))
        vacancy, capacity_feature, feature_valid = fv, fc, valid
        result["vacancy_date"] = fd
        result["public_value"] = fp

    waiting = None
    capacity_detail = None
    # Waiting counts are published most consistently on the MHLW simplified
    # overview (kani) page. Fall back to the detailed kihon page if needed.
    if w_html:
        waiting, cap_wait = parse_detail(html_to_text(w_html))
        if cap_wait is not None:
            capacity_detail = cap_wait
        if waiting is not None:
            result["waiting_source_url"] = waiting_url
    if d_html:
        waiting_detail, cap_detail = parse_detail(html_to_text(d_html))
        if capacity_detail is None and cap_detail is not None:
            capacity_detail = cap_detail
        if waiting is None and waiting_detail is not None:
            waiting = waiting_detail
            result["waiting_source_url"] = detail_url
    result["waiting_count"] = waiting
    result["capacity"] = capacity_detail or capacity_feature

    # Reject 0/0 and other impossible feature capacity values when detail shows a real facility capacity.
    if feature_valid and vacancy is not None:
        result["vacancy"] = vacancy
        result["vacancy_source_type"] = "厚労省公式"
        result["vacancy_source_url"] = feature_url
        result["classification"] = "空床取得可能"
    else:
        homepage = extract_official_homepage(
            d_html or "",
            seed.get("official_hp_seed", ""),
            (matched or {}).get("homepage", "") if matched else "",
        )
        result["official_homepage"] = homepage
        if homepage:
            ov, op, od, ou = crawl_official_homepage(homepage)
            if ov is not None:
                # -1 means official site says availability is positive but does not expose a numeric count.
                result["vacancy"] = None if ov == -1 else ov
                result["public_value"] = op
                if od:
                    result["vacancy_date"] = od
                result["vacancy_source_type"] = "施設公式HP"
                result["vacancy_source_url"] = ou
                result["classification"] = "空床取得可能"
            elif waiting is not None:
                result["classification"] = "待機人数のみ取得可能"
            else:
                result["classification"] = "取得不可"
        elif waiting is not None:
            result["classification"] = "待機人数のみ取得可能"

    if f_status == 200 or d_status == 200 or w_status == 200:
        result["status"] = "ok"
    else:
        result["status"] = "http_error"
    result["error"] = " | ".join(errors)
    return result


def load_existing() -> Dict[int, Dict[str, object]]:
    for p in (CLASS_JSON, OUT_JSON):
        if p.exists():
            try:
                obj = json.loads(p.read_text(encoding="utf-8"))
                return {int(x["no"]): x for x in obj.get("data", []) if x.get("no")}
            except Exception:
                pass
    return {}


def save(records: List[Dict[str, object]], total: int, mhlw_urls: Dict[str, str]):
    records = sorted(records, key=lambda x: int(x["no"]))
    counts: Dict[str, int] = {}
    for r in records:
        c = str(r.get("classification") or "")
        counts[c] = counts.get(c, 0) + 1
    obj = {
        "version": "osaka-851-v1",
        "source": "厚生労働省 介護サービス情報公表システム + 施設公式HP",
        "classification_rule": {
            "空床取得可能": "公式・公的サイトに機械取得可能な空床数/空床状態がある",
            "待機人数のみ取得可能": "空床は有効値を取得できないが、公式・公的サイトに数値の待機人数がある",
            "取得不可": "確認した公式・公的サイトから空床・待機人数のいずれも機械取得できない",
        },
        "total_facilities": total,
        "generated_at_jst": now_jst(),
        "mhlw_open_data_urls": mhlw_urls,
        "summary": {
            "records": len(records),
            "classification_counts": counts,
            "matched_official_pages": sum(bool(r.get("jigyosyo_cd")) for r in records),
            "vacancy_values_numeric": sum(isinstance(r.get("vacancy"), int) for r in records),
            "waiting_values": sum(isinstance(r.get("waiting_count"), int) for r in records),
        },
        "data": records,
    }
    for p in [OUT_JSON, CLASS_JSON]:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    for p in [OUT_CSV, CLASS_CSV]:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS, extrasaction="ignore")
            w.writeheader()
            for r in records:
                w.writerow({k: r.get(k, "") for k in OUTPUT_FIELDS})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=int, default=1, help="1-based first facility number in seed order")
    parser.add_argument("--count", type=int, default=851, help="Number of facilities to update")
    parser.add_argument("--limit", type=int, default=0, help="Deprecated testing alias for --count")
    parser.add_argument(
        "--only-unavailable",
        action="store_true",
        help="Recheck only facilities currently classified as 取得不可; preserves existing vacancy results",
    )
    args = parser.parse_args()

    all_facilities = load_seed()
    total = len(all_facilities)
    start_idx = max(0, args.start - 1)
    count = args.limit if args.limit > 0 else args.count
    selected = all_facilities[start_idx:start_idx + max(0, count)]
    print(f"Osaka official research: start={args.start}, count={len(selected)}, total={total}")

    urls = discover_mhlw_urls()
    print("MHLW open data URLs:", json.dumps(urls, ensure_ascii=False))
    candidates_by_service: Dict[str, List[Dict[str, str]]] = {}
    for service in ["510", "520", "540", "550"]:
        print(f"Load MHLW open data {service} ...")
        try:
            candidates_by_service[service] = load_mhlw_service(service, urls[service])
            print(f"  Osaka candidates: {len(candidates_by_service[service])}")
        except Exception as e:
            print(f"  ERROR {service}: {type(e).__name__}: {e}", file=sys.stderr)
            candidates_by_service[service] = []

    existing = load_existing()
    # Ensure unprocessed facilities exist in outputs rather than disappearing during 50-item checkpoints.
    for f in all_facilities:
        n = int(f["no"])
        if n not in existing:
            existing[n] = {
                "no": n, "facility": f["facility"], "type": f["type"], "address": f["address"],
                "city": f.get("city", ""), "service_cd": f.get("service_cd", ""), "jigyosyo_cd": "",
                "match_method": "pending", "match_score": 0, "classification": "未調査",
                "vacancy": None, "capacity": None, "waiting_count": None, "vacancy_date": "", "public_value": "",
                "vacancy_source_type": "", "vacancy_source_url": "", "waiting_source_url": "", "official_homepage": "",
                "checked_at_jst": "", "feature_http_status": None, "detail_http_status": None,
                "status": "pending", "error": "", "feature_url": "", "detail_url": "",
            }

    if args.only_unavailable:
        before = len(selected)
        selected = [
            f for f in selected
            if str(existing.get(int(f["no"]), {}).get("classification") or "") == "取得不可"
        ]
        print(f"Only-unavailable mode: {before} in range -> {len(selected)} facilities to recheck")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
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
        for i, f in enumerate(selected, start=1):
            try:
                r = update_one(page, f, candidates_by_service)
            except Exception as e:
                r = dict(existing.get(int(f["no"]), {}))
                r.update({
                    "no": int(f["no"]), "facility": f["facility"], "type": f["type"], "address": f["address"],
                    "city": f.get("city", ""), "service_cd": f.get("service_cd", ""),
                    "checked_at_jst": now_jst(), "status": "exception", "error": f"{type(e).__name__}: {e}",
                })
            existing[int(r["no"])] = r
            print(f"[{i}/{len(selected)}] No.{r['no']} {r['facility']} class={r.get('classification')} vacancy={r.get('vacancy')} waiting={r.get('waiting_count')}")
            if i % 10 == 0:
                save(list(existing.values()), total, urls)
        browser.close()

    save(list(existing.values()), total, urls)
    print("Saved:", OUT_JSON, OUT_CSV, CLASS_JSON, CLASS_CSV)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
