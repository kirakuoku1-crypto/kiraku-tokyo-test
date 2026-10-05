#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Kiraku Saitama 635 official vacancy/waiting updater.

Primary source: Saitama Prefecture official vacancy / waiting PDFs.
The PDF parsing logic mirrors the proven v22 browser parser used by the Netlify app.

Outputs:
  docs/saitama_635_latest.json
  docs/saitama_635_latest.csv
  docs/saitama_635_classification.json
  docs/saitama_635_classification.csv
"""
from __future__ import annotations

import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

FACILITIES_JSON = Path("saitama_635_facilities.json")
OUT_JSON = Path("docs/saitama_635_latest.json")
OUT_CSV = Path("docs/saitama_635_latest.csv")
CLASS_JSON = Path("docs/saitama_635_classification.json")
CLASS_CSV = Path("docs/saitama_635_classification.csv")
JST = ZoneInfo("Asia/Tokyo")
PDFJS = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js"
GAS_URL = "https://script.google.com/macros/s/AKfycbyYLpHyKleoGPen_08yoKTq3Vg7OxjyovZMtG5AM6_9SpTEV5vDQ0291JkFsYf9-ojH/exec"

FIELDS = [
    "id", "facility", "type", "region", "address", "tel", "jigyosho_no",
    "classification", "vacancy", "waiting_count", "vacancy_date",
    "vacancy_source_type", "vacancy_source_url", "waiting_source_url",
    "source_row_no", "provided", "confident", "wait_provided", "wait_confident",
    "checked_at_jst", "status", "error",
]


def now_jst() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


PARSER_JS = r'''
window.KIRAKU_GAS_URL = __GAS_URL__;
window.KIRAKU_FACILITIES = [];

function jsonpGet(url, cb, timeoutMs){
  timeoutMs=timeoutMs||30000;
  var done=false, timer=null, s=document.createElement('script');
  var name='__kjp_'+Date.now()+'_'+Math.floor(Math.random()*1e8);
  function finish(ok,data){
    if(done)return; done=true; if(timer)clearTimeout(timer);
    try{delete window[name];}catch(e){window[name]=undefined;}
    try{s.remove();}catch(e){}
    cb(ok,data);
  }
  window[name]=function(data){finish(true,data);};
  var sep=url.indexOf('?')>=0?'&':'?';
  s.src=url+sep+'callback='+encodeURIComponent(name);
  s.onerror=function(){finish(false,null);};
  timer=setTimeout(function(){finish(false,null);},timeoutMs);
  document.head.appendChild(s);
}
function prefDigits(s){ return String(s||'').replace(/[０-９]/g,function(c){return String(c.charCodeAt(0)-0xFF10);}); }
function prefNorm(s){
  var v=String(s||''); try{v=v.normalize('NFKC');}catch(e){}
  return v.replace(/[\s　・･\.．,，、。'"「」『』（）()\[\]【】<>〈〉《》\-‐‑–—―ｰー]/g,'').toLowerCase();
}
function prefPhone(s){ return prefDigits(String(s||'')).replace(/[^0-9]/g,''); }
function prefNameCore(s){
  var v=prefNorm(s);
  ['地域密着型特別養護老人ホーム','特別養護老人ホーム','介護老人保健施設','介護医療院','老人保健施設','老健'].forEach(function(p){
    var k=prefNorm(p); if(v.indexOf(k)===0) v=v.slice(k.length);
  });
  return v;
}
function prefSourceKey(url){
  var m=String(url||'').match(/(?:^|\/)(0[1-9]|10)[^\/]*-(tokuyo|roken)-[^\/]+\.pdf/i);
  return m ? (m[1]+'-'+m[2].toLowerCase()) : '';
}
function prefFacSourceKey(x){ return prefSourceKey(x&&x.ex&&x.ex.kushoUrl); }
function prefCandidatesForUrl(url){
  var k=prefSourceKey(url), arr=[];
  (window.KIRAKU_FACILITIES||[]).forEach(function(x){ if(prefFacSourceKey(x)===k) arr.push(x); });
  return arr;
}
function prefLatestSourceUrls(){
  return new Promise(function(resolve,reject){
    jsonpGet(window.KIRAKU_GAS_URL+'?action=officialSources',function(ok,d){
      if(ok&&d&&d.ok&&Array.isArray(d.urls)&&d.urls.length>=10) resolve(d.urls);
      else reject(new Error('officialSources failed'));
    },30000);
  });
}
function prefFetchArrayBuffer(url){
  return new Promise(function(resolve,reject){
    jsonpGet(window.KIRAKU_GAS_URL+'?action=officialPdf&url='+encodeURIComponent(url),function(ok,d){
      if(!ok||!d||!d.ok||!d.base64){reject(new Error('officialPdf failed'));return;}
      try{
        var bin=atob(d.base64),u8=new Uint8Array(bin.length);
        for(var i=0;i<bin.length;i++)u8[i]=bin.charCodeAt(i);
        resolve(u8.buffer);
      }catch(e){reject(e);}
    },45000);
  });
}
function prefExtractUpdateDate(allText){
  var t=prefDigits(String(allText||'')).replace(/\s+/g,'');
  var m=t.match(/情報更新日[:：]?(?:令和)?(\d{1,2})年(\d{1,2})月(\d{1,2})日/);
  if(!m) return '';
  var y=2018+Number(m[1]),mo=('0'+m[2]).slice(-2),d=('0'+m[3]).slice(-2);
  return y+'-'+mo+'-'+d;
}
function prefItemText(it){return String(it&&it.str||'').trim();}
function prefRowBlocks(items,w){
  var nums=[];
  items.forEach(function(it){
    var st=prefDigits(prefItemText(it)); var x=it.transform?it.transform[4]:0,y=it.transform?it.transform[5]:0;
    if(/^\d{1,3}$/.test(st)&&x<w*0.105) nums.push({n:Number(st),y:y});
  });
  nums.sort(function(a,b){return b.y-a.y;});
  var unique=[];
  nums.forEach(function(a){if(!unique.length||Math.abs(unique[unique.length-1].y-a.y)>3)unique.push(a);});
  return unique.map(function(a,i){
    var top=i===0?a.y+45:(unique[i-1].y+a.y)/2;
    var bot=i===unique.length-1?a.y-45:(a.y+unique[i+1].y)/2;
    return {n:a.n,y:a.y,top:top,bot:bot,items:items.filter(function(it){var yy=it.transform?it.transform[5]:0;return yy<=top&&yy>bot;})};
  });
}
function prefFindHeaderX(items,re,fallback){
  var xs=[];items.forEach(function(it){var st=prefItemText(it);if(re.test(st))xs.push(it.transform?it.transform[4]:0);});
  if(!xs.length)return fallback;xs.sort(function(a,b){return Math.abs(a-fallback)-Math.abs(b-fallback);});return xs[0];
}
function prefFindVacancyHeaderX(items,w){
  var exact=[];items.forEach(function(it){var st=prefDigits(prefItemText(it)).replace(/[\s　]/g,'');if(st==='空床数')exact.push(it.transform?it.transform[4]:0);});
  if(exact.length){exact.sort(function(a,b){return Math.abs(a-w*0.72)-Math.abs(b-w*0.72);});return exact[0];}
  return prefFindHeaderX(items,/空床数|空床/,w*0.72);
}
function prefFindWaitHeaderX(items,w,vacX){
  var xs=[];items.forEach(function(it){var st=prefDigits(prefItemText(it)).replace(/[\s　]/g,'');var x=it.transform?it.transform[4]:0;if(x<w*0.45||x>=vacX)return;if(/待ち数|待機/.test(st))xs.push(x);});
  if(xs.length){xs.sort(function(a,b){return a-b;});return xs[0];}
  return w*0.62;
}
function prefColText(block,x,wid){
  var a=block.items.filter(function(it){var xx=it.transform?it.transform[4]:0;return xx>=x-wid&&xx<=x+wid;});
  a.sort(function(p,q){var py=p.transform[5],qy=q.transform[5];if(Math.abs(py-qy)>2)return qy-py;return p.transform[4]-q.transform[4];});
  return a.map(prefItemText).filter(Boolean).join(' ');
}
function prefNormalizeCellText(s){return prefDigits(String(s||'')).replace(/[\t\r\n　]+/g,' ').replace(/ +/g,' ').trim();}
function prefParseWaitCell(s){
  var t=prefNormalizeCellText(s);
  if(!t)return {provided:false,value:null,text:'',confident:true};
  if(/施設へ確認|確認をお願い|要確認/.test(t))return {provided:false,value:null,text:t,confident:true};
  if(/^(?:無し|なし|無|0名?|０名?)$/.test(t))return {provided:true,value:0,text:t,confident:true};
  var nums=t.match(/\d+/g)||[];
  if(nums.length!==1)return {provided:false,value:null,text:t,confident:false,ambiguous:nums.length>1};
  var v=Number(nums[0]);if(!isFinite(v)||v<0||v>9999)return {provided:false,value:null,text:t,confident:false};
  return {provided:true,value:v,text:t,confident:true};
}
function prefParseVacancyCell(s){
  var t=prefNormalizeCellText(s);
  if(!t)return {provided:false,value:null,isOpen:false,text:'',confident:true};
  if(/施設へ確認|確認をお願い|要確認/.test(t))return {provided:false,value:null,isOpen:false,text:t,confident:true};
  if(/^(?:無し|なし|無|満床|0|0床|0名)$/.test(t))return {provided:true,value:0,isOpen:false,text:t,confident:true};
  var roomRe=/(?:個室|多床室|男性|女性)[^0-9]{0,6}(\d+)/g,rm,vals=[];
  while((rm=roomRe.exec(t))!==null)vals.push(Number(rm[1]));
  if(vals.length){var total=vals.reduce(function(a,b){return a+b;},0);return {provided:true,value:total,isOpen:total>0,text:t,confident:true};}
  var nums=t.match(/\d+/g)||[];
  if(nums.length===1){var v=Number(nums[0]);if(isFinite(v)&&v>=0&&v<=999)return {provided:true,value:v,isOpen:v>0,text:t,confident:true};}
  if(nums.length>1)return {provided:false,value:null,isOpen:false,text:t,confident:false,ambiguous:true};
  if(/有り|あり|有|○|〇|若干名/.test(t))return {provided:true,value:null,isOpen:true,text:t,confident:true};
  return {provided:false,value:null,isOpen:false,text:t,confident:true};
}
function prefMatchFacility(block,candidates){
  var joined=prefNorm(block.items.map(prefItemText).join(' '));
  var best=null,bs=0;
  candidates.forEach(function(x){
    var sc=0,name=prefNorm(x.name),core=prefNameCore(x.name),ph=prefPhone(x.tel);
    if(name&&joined.indexOf(name)!==-1)sc+=8;else if(core&&core.length>=3&&joined.indexOf(core)!==-1)sc+=5;
    var pv=x.prevNames||[];for(var i=0;i<pv.length;i++){var kk=prefNorm(pv[i]);if(kk&&joined.indexOf(kk)!==-1){sc=Math.max(sc,7);break;}}
    if(ph&&joined.indexOf(ph)!==-1)sc+=6;
    var addr=prefNorm(x.address);if(addr&&addr.length>7&&joined.indexOf(addr.slice(-Math.min(12,addr.length)))!==-1)sc+=2;
    if(sc>bs){bs=sc;best=x;}
  });
  return bs>=5?best:null;
}
function prefParsePage(page,url){
  return page.getTextContent().then(function(tc){
    var items=tc.items||[],vp=page.getViewport({scale:1}),w=vp.width;
    var vacX=prefFindVacancyHeaderX(items,w),waitX=prefFindWaitHeaderX(items,w,vacX),colWid=Math.max(10,w*0.025);
    var blocks=prefRowBlocks(items,w),candidates=prefCandidatesForUrl(url),rows=[];
    blocks.forEach(function(b){
      var fac=prefMatchFacility(b,candidates);if(!fac)return;
      var vac=prefParseVacancyCell(prefColText(b,vacX,colWid));
      var wai=prefParseWaitCell(prefColText(b,waitX,colWid));
      rows.push({id:fac.id,provided:vac.provided,confident:vac.confident!==false,ambiguous:!!vac.ambiguous,vacancy:vac.value,isOpen:vac.isOpen,vacancyText:vac.text,waitProvided:wai.provided,waitConfident:wai.confident!==false,waitCount:wai.provided?wai.value:null,waitText:wai.text,sourceUrl:url,rowNo:b.n});
    });
    return {rows:rows,text:items.map(prefItemText).join(' ')};
  });
}
function prefParsePdfBuffer(buf,url){
  return window.pdfjsLib.getDocument({data:new Uint8Array(buf)}).promise.then(function(pdf){
    var jobs=[];for(var p=1;p<=pdf.numPages;p++)jobs.push(pdf.getPage(p).then(function(pg){return prefParsePage(pg,url);}));
    return Promise.all(jobs).then(function(parts){
      var all='',rows=[];parts.forEach(function(q){all+=' '+q.text;rows=rows.concat(q.rows);});
      var upd=prefExtractUpdateDate(all);rows.forEach(function(r){r.updated=upd;});return rows;
    });
  });
}
async function runSaitamaOfficial(){
  var urls=await prefLatestSourceUrls();
  urls=(urls||[]).filter(function(u){return /\/(0[1-9]|10)[^\/]*-(tokuyo|roken)-[^\/]+\.pdf/i.test(u);});
  var seen={};urls=urls.filter(function(u){if(seen[u])return false;seen[u]=1;return true;});
  var byId={},errors=[],ix=0;
  async function worker(){
    while(true){
      var my=ix++; if(my>=urls.length)return;
      var url=urls[my];
      try{
        var buf=await prefFetchArrayBuffer(url);var rows=await prefParsePdfBuffer(buf,url);
        rows.forEach(function(r){var old=byId[r.id];if(!old||(!old.provided&&r.provided))byId[r.id]=r;});
      }catch(e){errors.push({url:url,error:String(e&&e.message||e)});}
    }
  }
  var workers=[];for(var i=0;i<Math.min(3,urls.length);i++)workers.push(worker());
  await Promise.all(workers);
  return {urls:urls,items:byId,errors:errors};
}
'''.replace("__GAS_URL__", json.dumps(GAS_URL, ensure_ascii=False))


def classify(item: dict | None) -> str:
    if item:
        if item.get("provided") and item.get("confident", True) and not item.get("ambiguous"):
            return "空床取得可能"
        if item.get("waitProvided") and item.get("waitConfident", True):
            return "待機人数のみ取得可能"
    return "取得不可"


def main() -> None:
    if not FACILITIES_JSON.exists():
        raise SystemExit(f"missing {FACILITIES_JSON}")
    facilities = json.loads(FACILITIES_JSON.read_text(encoding="utf-8"))
    if not isinstance(facilities, list) or len(facilities) < 600:
        raise SystemExit("facility seed is invalid")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page()
        page.set_content("<!doctype html><html><head></head><body></body></html>", wait_until="domcontentloaded")
        page.add_script_tag(url=PDFJS)
        page.wait_for_function("typeof window.pdfjsLib !== 'undefined'", timeout=60000)
        page.add_script_tag(content=PARSER_JS)
        page.evaluate("f => { window.KIRAKU_FACILITIES = f; }", facilities)
        result = page.evaluate("runSaitamaOfficial()")
        browser.close()

    checked = now_jst()
    items = result.get("items", {}) or {}
    records = []
    by_id_for_app = {}
    for fac in facilities:
        p = items.get(fac.get("id"))
        c = classify(p)
        ex = fac.get("ex") or {}
        rec = {
            "id": fac.get("id", ""),
            "facility": fac.get("name", ""),
            "type": fac.get("type", ""),
            "region": fac.get("region", ""),
            "address": fac.get("address", ""),
            "tel": fac.get("tel", ""),
            "jigyosho_no": ex.get("jigyoshoNo", ""),
            "classification": c,
            "vacancy": p.get("vacancy") if p else None,
            "waiting_count": p.get("waitCount") if p and p.get("waitProvided") else None,
            "vacancy_date": p.get("updated", "") if p else "",
            "vacancy_source_type": "埼玉県公式" if p else "",
            "vacancy_source_url": p.get("sourceUrl", "") if p else ex.get("kushoUrl", ""),
            "waiting_source_url": p.get("sourceUrl", "") if p and p.get("waitProvided") else "",
            "source_row_no": p.get("rowNo") if p else None,
            "provided": bool(p and p.get("provided")),
            "confident": bool(p.get("confident", True)) if p else False,
            "wait_provided": bool(p and p.get("waitProvided")),
            "wait_confident": bool(p.get("waitConfident", True)) if p else False,
            "checked_at_jst": checked,
            "status": "ok" if p else "not_listed_or_unmatched",
            "error": "",
        }
        records.append(rec)
        if p:
            q = dict(p)
            q["classification"] = c
            q["checkedAt"] = checked
            by_id_for_app[fac.get("id")] = q

    counts = Counter(r["classification"] for r in records)
    stats = {
        "total": len(records),
        "matched": len(items),
        "open": sum(1 for r in records if isinstance(r.get("vacancy"), int) and r["vacancy"] > 0),
        "files": len(result.get("urls", []) or []) - len(result.get("errors", []) or []),
        "failed": len(result.get("errors", []) or []),
        "classification": dict(counts),
    }
    payload = {
        "generated_at_jst": checked,
        "source": "埼玉県公式 特養・老健 空床・入所待ち情報提供システム",
        "source_urls": result.get("urls", []) or [],
        "stats": stats,
        "errors": result.get("errors", []) or [],
        "items": by_id_for_app,
        "records": records,
    }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with OUT_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader(); w.writerows(records)

    grouped = {"空床取得可能": [], "待機人数のみ取得可能": [], "取得不可": []}
    for r in records:
        grouped[r["classification"]].append({k: r[k] for k in ["id","facility","type","region","vacancy","waiting_count","vacancy_date","vacancy_source_url"]})
    CLASS_JSON.write_text(json.dumps({"generated_at_jst": checked, "counts": dict(counts), "groups": grouped}, ensure_ascii=False, indent=2), encoding="utf-8")
    with CLASS_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        fields = ["classification","id","facility","type","region","vacancy","waiting_count","vacancy_date","vacancy_source_url"]
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader()
        for cls in ["空床取得可能","待機人数のみ取得可能","取得不可"]:
            for x in grouped[cls]: w.writerow({"classification": cls, **x})

    print(json.dumps(stats, ensure_ascii=False))


if __name__ == "__main__":
    main()
