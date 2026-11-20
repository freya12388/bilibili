# -*- coding: utf-8 -*-
"""
B站UP主动态监控 → PushPlus 微信推送
- 由 GitHub Actions 每分钟触发
- 只推送图文动态(DRAW)和文字动态(WORD)，其余类型(视频投稿/直播等)只推进游标不推送
- state/last.txt 记录游标(最新已处理 dynamic id)，由 workflow 自动 commit 回仓库
"""
import hashlib
import html
import json
import os
import re
import time
import urllib.parse
import urllib.request

UP_MID   = os.environ.get("UP_MID", "3706962053172003")   # 我走路带风风风
TOKEN    = os.environ.get("PUSHPLUS_TOKEN", "")           # PushPlus token（需实名）
SCKEY    = os.environ.get("SERVERCHAN_SENDKEY", "")       # Server酱 SendKey（推荐，微信扫码即得）
SESSDATA = os.environ.get("BILI_SESSDATA", "")            # 可选：B站小号cookie，提高稳定性
PUSH_URL = "http://www.pushplus.plus/send"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
PUSH_TYPES = {"DYNAMIC_TYPE_DRAW", "DYNAMIC_TYPE_WORD", "DYNAMIC_TYPE_OPUS"}  # 图文/文字/新版opus
FEATURES = ("itemOpusStyle,opusBigCover,onlyEndVote,decorationCard,"
            "onlyOriginal,originList,ugcDelete,onlyForFans")   # 返回新版opus结构，正文在 major.opus.summary.text
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state", "last.txt")


def http_json(url, cookie=None, data=None):
    headers = {"User-Agent": UA,
               "Accept": "application/json, text/plain, */*",
               "Accept-Language": "zh-CN,zh;q=0.9",
               "Accept-Encoding": "gzip, deflate",
               "Cache-Control": "no-cache",
               "Origin": "https://space.bilibili.com",
               "Referer": "https://space.bilibili.com/%s/dynamic" % UP_MID,
               "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
               "sec-ch-ua-mobile": "?0",
               "sec-ch-ua-platform": '"Windows"',
               "Sec-Fetch-Dest": "empty",
               "Sec-Fetch-Mode": "cors",
               "Sec-Fetch-Site": "same-site"}
    if cookie:
        headers["Cookie"] = cookie
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            if raw[:2] == b"\x1f\x8b":            # gzip 响应自动解压
                import gzip
                raw = gzip.decompress(raw)
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError("HTTP %s for %s" % (e.code, url[:110])) from e


def enrich_cookie(cookie):
    """先访问B站首页（跟随重定向），用 CookieJar 收割 WAF 种下的 Cookie
    （b_nut/b_lsid 等），模拟真实浏览器"先逛首页再调接口"，解 feed/space 的 412"""
    import http.cookiejar
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    req = urllib.request.Request("https://www.bilibili.com/", headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Cookie": cookie})
    try:
        with opener.open(req, timeout=15) as r:
            r.read(256)
    except Exception as e:
        print("enrich page err: %r" % e)
    have = {c.split("=")[0].strip() for c in cookie.split(";") if "=" in c}
    add = ["%s=%s" % (c.name, c.value) for c in jar if c.name not in have]
    if add:
        print("enriched: +%s" % ",".join(c.name for c in jar if c.name not in have))
        cookie += "; " + "; ".join(add)
    else:
        print("enrich: no new cookies")
    return cookie


BUVID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state", "buvid.txt")


def get_cookie():
    """指纹 cookie 持久化复用（模拟真实浏览器：同一 buvid 反复使用）"""
    cookie = ""
    try:
        with open(BUVID_FILE, encoding="utf-8") as f:
            cookie = f.read().strip()
    except Exception:
        pass
    if not cookie:
        spi = http_json("https://api.bilibili.com/x/frontend/finger/spi")
        cookie = "buvid3=%s; buvid4=%s" % (spi["data"]["b_3"], spi["data"]["b_4"])
        os.makedirs(os.path.dirname(BUVID_FILE), exist_ok=True)
        with open(BUVID_FILE, "w", encoding="utf-8") as f:
            f.write(cookie)
        try:   # 新指纹先预热注册一次
            http_json("https://api.bilibili.com/x/web-interface/nav", cookie=cookie)
        except Exception:
            pass
    return cookie


# WBI 签名（B站网页接口标准算法，-352 风控的正解）
MIXIN_TAB = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27,
             43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37,
             48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25,
             54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52]


def get_wbi_key(cookie):
    """取WBI签名key；nav 被风控时返回 None（降级为不签名请求）"""
    try:
        nav = http_json("https://api.bilibili.com/x/web-interface/nav", cookie=cookie)
        wbi = (nav.get("data") or {}).get("wbi_img") or {}
        raw = ""
        for u in (wbi.get("img_url") or "", wbi.get("sub_url") or ""):
            raw += re.sub(r"\.png|\.jpg", "", os.path.basename(u))
        key = "".join(raw[i] for i in MIXIN_TAB if i < len(raw))[:32]
        return key if key else None
    except Exception as e:
        print("wbi key unavailable: %r" % e)
        return None


def wbi_sign(params, key):
    params = dict(params)
    params["wts"] = int(time.time())
    params = {k: str(v).replace("!'()*", "") for k, v in params.items()}
    qs = urllib.parse.urlencode(sorted(params.items()))
    params["w_rid"] = hashlib.md5((qs + key).encode()).hexdigest()
    return params


def fetch_dynamics(remint=False):
    """返回 items（按 id 正序）；code!=0 或网络异常时抛错"""
    if remint and os.path.exists(BUVID_FILE):     # 重铸指纹（旧buvid可能被WAF拉黑）
        os.remove(BUVID_FILE)
        print("buvid reminted")
    cookie = get_cookie()
    cookie = enrich_cookie(cookie)
    if SESSDATA:
        cookie += "; SESSDATA=%s" % SESSDATA
    params = {"host_mid": UP_MID, "timezone_offset": -480, "features": FEATURES}
    key = get_wbi_key(cookie)
    if key:
        params = wbi_sign(params, key)
    else:
        print("fetch without wbi sign")
    url = ("https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space?%s"
           % urllib.parse.urlencode(params))
    d = http_json(url, cookie=cookie)
    if d.get("code") != 0:
        raise RuntimeError("bili api code=%s msg=%s" % (d.get("code"), d.get("message")))
    items, uname = [], UP_MID
    for it in (d.get("data") or {}).get("items") or []:
        mod = it.get("modules", {})
        author = mod.get("module_author") or {}
        uname = author.get("name") or uname
        dyn = mod.get("module_dynamic") or {}
        major = dyn.get("major") or {}
        opus = major.get("opus") or {}
        # 正文：新版opus结构优先，老结构 desc.text 兜底
        text = ((opus.get("summary") or {}).get("text")
                or (dyn.get("desc") or {}).get("text") or "").strip()
        pics = []
        for p in (opus.get("pics") or []):                    # 新版图片
            u = p.get("url") or p.get("src")
            if u:
                pics.append(u)
        for p in ((major.get("draw") or {}).get("items") or []):   # 老版图片
            if p.get("src"):
                pics.append(p["src"])
        link = ""
        ju = opus.get("jump_url") or ""
        if ju.startswith("//"):
            link = "https:" + ju
        if it.get("id_str"):
            items.append({"id": str(it["id_str"]), "type": it.get("type", ""),
                          "uname": uname, "text": text, "pics": pics, "url": link})
    items.sort(key=lambda x: int(x["id"]))
    return items


def load_last():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return int(f.read().strip() or 0)
    except Exception:
        return None      # 首次运行


def save_last(v):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        f.write(str(v))


def push(item):
    link = item.get("url") or ("https://t.bilibili.com/%s" % item["id"])
    title = "【B站】%s 发了新动态" % item["uname"]
    if SCKEY:      # Server酱：markdown 格式，免费每日5条
        text = (item["text"] or "(无文字内容)")[:20000]
        imgs = "".join(("![图](%s)\n\n" % u) for u in item["pics"][:9])
        desp = "%s\n\n%s[原文链接](%s)" % (text, imgs, link)
        body = urllib.parse.urlencode({"title": title, "desp": desp}).encode("utf-8")
        req = urllib.request.Request("https://sctapi.ftqq.com/%s.send" % SCKEY, data=body)
        with urllib.request.urlopen(req, timeout=20) as r:
            res = json.loads(r.read().decode("utf-8"))
        ok = res.get("code") == 0
        print("push(serverchan) %s ok=%s" % (item["id"], ok))
        return ok
    # PushPlus：html 格式（需实名 verify.pushplus.plus）
    text = html.escape(item["text"] or "(无文字内容)")
    if len(text) > 20000:
        text = text[:20000] + "..."
    text = text.replace("\n", "<br>")                        # 保留段落换行
    imgs = "".join('<br><img src="%s" style="max-width:100%%"/>' % u for u in item["pics"][:9])
    content = '<p>%s</p>%s<p><a href="%s">原文链接</a></p>' % (text, imgs, link)
    body = json.dumps({"token": TOKEN, "title": title,
                       "content": content, "template": "html"}).encode("utf-8")
    res = http_json(PUSH_URL, data=body)
    ok = res.get("code") == 200
    print("push(pushplus) %s ok=%s msg=%s" % (item["id"], ok, res.get("message")))
    return ok


def main():
    if not TOKEN and not SCKEY:
        print("missing PUSHPLUS_TOKEN / SERVERCHAN_SENDKEY")
        return
    last = load_last()

    for attempt in range(4):                    # 拉取重试（后两次换新指纹）
        try:
            items = fetch_dynamics(remint=(attempt >= 2))
            break
        except Exception as e:
            print("fetch retry %d: %r" % (attempt + 1, e))
            time.sleep(3 * (attempt + 1))
    else:
        print("fetch failed; state untouched, retry next run")
        return

    if last is None:                            # 首次运行：只记基线，不推送
        if items:
            save_last(int(items[-1]["id"]))
            print("first run, baseline saved: %s" % items[-1]["id"])
        else:                                   # 空列表(风控抖动)不落基线，下轮重试
            print("first run got empty feed; skip baseline")
        return

    cursor = last
    for item in [i for i in items if int(i["id"]) > last]:
        if item["type"] in PUSH_TYPES:
            if push(item):
                cursor = int(item["id"])
            else:
                break                           # 推送失败：游标停在成功处，下轮重推
        else:
            cursor = int(item["id"])            # 非推送类型(如视频投稿)只推进游标
    if cursor != last:
        save_last(cursor)
    print("done. cursor=%s new_items=%d" % (cursor, len([i for i in items if int(i['id']) > last])))


if __name__ == "__main__":
    main()
