"""单文件 Python 爬虫（设备端产物）。

运行环境由客户端内置解释器决定，本文件必须遵守：
  * CPython 3.8：不能用 3.9+ 的语法与标准库特性（ruff 按 py38 检查本文件）。
  * 只用标准库：客户端里没有 pydantic，也不能装依赖。
  * 单文件：客户端下载后用 SourceFileLoader 加载，不能导入同目录其他模块。
  * 入口类名 Spider，客户端以无参方式构造，再调用 init(ext)。

站点规则固定在本文件内，改动时需与配置文件保持同步。

配置方式（配置 JSON 与本文件放在同一目录，api 可写相对路径）：
  {"key": "qqys", "name": "千千影视", "type": 3, "api": "./spider.py", "ext": "qqys"}
ext 为站点 key 时只服务该站点；缺省时聚合全部站点，站内分类放进筛选项。
"""

import base64
import html
import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    from base.spider import Spider as _Base
except ImportError:  # 本地测试时没有客户端的基类
    _Base = object

UA = (
    "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36"
)
TIMEOUT = 15
ATTEMPTS = 3
FALLBACK_PAGES = 30

# TVBox 用 `$$$` 分线路、`#` 分集、`$` 分名称与值；站点前缀分隔符必须避开它们。
LINE_SEP = "$$$"
EP_SEP = "#"
NAME_SEP = "$"
SITE_SEP = ":"
PLAY_SEP = "|"
FILTER_KEY = "cid"

PARSE_HOSTS = (
    "qq.com",
    "youku.com",
    "iqiyi.com",
    "mgtv.com",
    "bilibili.com",
    "sohu.com",
    "le.com",
    "1905.com",
    "pptv.com",
)
PLAYABLE_SUFFIXES = (".m3u8", ".mp4", ".mpd", ".flv", ".mkv")
# 主流视频站网页线路的线路名：含这些字样，或整个名字就是这些代号（茶杯虎用 YK/TX/QY/MG/BL）。
# 按小写比较。
PARSE_LINE_WORDS = ("优酷", "腾讯", "奇异", "奇艺", "芒果", "哔哩", "b站", "搜狐", "乐视")
PARSE_LINE_CODES = (
    "youku",
    "qq",
    "qiyi",
    "mgtv",
    "bilibili",
    "sohu",
    "letv",
    "yk",
    "tx",
    "qy",
    "mg",
    "bl",
)


class FetchError(Exception):
    """取页失败。"""


# ---------------------------------------------------------------- 取页

_LAX_CONTEXT = None
# 严格校验因证书失败过的 host：之后直接走宽松上下文，省掉每个请求一次注定失败的握手。
_LAX_HOSTS = set()


def _lax_context():
    """不校验证书的上下文。

    Chaquopy 自带的 cacert.pem 与部分影视站的证书链对不上，严格校验会直接失败；
    只在严格校验因证书失败时才降级到这里。
    """
    global _LAX_CONTEXT
    if _LAX_CONTEXT is None:
        _LAX_CONTEXT = ssl.create_default_context()
        _LAX_CONTEXT.check_hostname = False
        _LAX_CONTEXT.verify_mode = ssl.CERT_NONE
    return _LAX_CONTEXT


def _is_cert_error(exc):
    reason = getattr(exc, "reason", exc)
    return isinstance(reason, ssl.SSLCertVerificationError)


def _open(req, timeout):
    if req.host not in _LAX_HOSTS:
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.URLError as exc:
            if isinstance(exc, urllib.error.HTTPError) or not _is_cert_error(exc):
                raise
        _LAX_HOSTS.add(req.host)
    return urllib.request.urlopen(req, timeout=timeout, context=_lax_context())


def fetch_text(url, referer=None, timeout=TIMEOUT):
    """抓取并解码页面；5xx 与网络错误重试，4xx 直接失败。"""
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/json,*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if referer:
        headers["Referer"] = referer
    last = FetchError(f"请求失败 {url}")
    for attempt in range(ATTEMPTS):
        try:
            resp = _open(urllib.request.Request(url, headers=headers), timeout)
            raw = resp.read()
            return _decode(raw, _charset(resp.headers.get("Content-Type", ""), raw))
        except urllib.error.HTTPError as exc:
            last = FetchError(f"HTTP {exc.code}: {url}")
            if exc.code < 500:
                raise last from exc
        except urllib.error.URLError as exc:
            last = FetchError(f"无法连接 {url}: {exc.reason}")
        except Exception as exc:
            last = FetchError(f"请求失败 {url}: {exc}")
        if attempt + 1 < ATTEMPTS:
            time.sleep(0.8 * (attempt + 1))
    raise last


def _charset(ctype, raw):
    m = re.search(r'charset=["\']?([\w-]+)', ctype, re.I)
    if m:
        return m.group(1)
    head = raw[:2048].decode("ascii", "ignore")
    m = re.search(r'charset=["\']?([\w-]+)', head, re.I)
    return m.group(1) if m else "utf-8"


def _decode(raw, encoding):
    for enc in (encoding, "utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    raise FetchError("正文无法解码")


# ---------------------------------------------------------------- 正则工具


def first(pattern, text, group=1):
    """取第一个匹配的分组，去标签并反转义。"""
    m = re.search(pattern, text, re.S)
    if not m:
        return ""
    try:
        value = m.group(group)
    except IndexError:
        return ""
    return clean(value or "")


def clean(value):
    text = html.unescape(re.sub(r"<[^>]+>", "", value or ""))
    return re.sub(r"\s+", " ", text).strip()


def blocks(pattern, text):
    """按起始正则把页面切成卡片块。"""
    starts = [m.start() for m in re.finditer(pattern, text, re.S)]
    stops = starts[1:] + [len(text)]
    return [text[a:b] for a, b in zip(starts, stops)]


# ---------------------------------------------------------------- 播放地址归一


def decode_play_url(raw):
    """把苹果 CMS 播放页的 url 字段还原成地址：直链 / Base64+URL 编码 / URL 编码。"""
    text = (raw or "").strip()
    if not text:
        return ""
    if text.startswith(("http://", "https://")):
        return text
    try:
        padded = text + "=" * (-len(text) % 4)
        candidate = _unquote(base64.b64decode(padded).decode("utf-8"))
        if candidate.startswith(("http://", "https://")):
            return candidate
    except (ValueError, UnicodeDecodeError):
        pass
    unquoted = _unquote(text)
    return unquoted if unquoted.startswith(("http://", "https://")) else ""


def _unquote(text):
    current = text
    for _ in range(4):
        nxt = urllib.parse.unquote(current)
        if nxt == current:
            break
        current = nxt
    return current


def player_result(raw):
    """返回 playerContent 结构：直链 parse=0，主流视频站网页交给客户端解析。"""
    url = decode_play_url(raw)
    if not url:
        return {"parse": 0, "url": "", "msg": "该线路没有可直接播放的地址"}
    parts = urllib.parse.urlsplit(url)
    host = parts.netloc.lower()
    if any(host == h or host.endswith("." + h) for h in PARSE_HOSTS):
        return {"parse": 1, "jx": 1, "url": url}
    if parts.path.lower().endswith(PLAYABLE_SUFFIXES):
        return {"parse": 0, "url": url}
    return {"parse": 1, "jx": 1, "url": url}


# ---------------------------------------------------------------- 站点配置


def _site(key, name, base, kind, categories, profile=None):
    return {
        "key": key,
        "name": name,
        "base": base,
        "kind": kind,
        "categories": categories,
        "profile": profile or {},
    }


CUPTGR_PROFILE = {
    "category": "/show/{tid}-----------.html",
    "category_page": "/show/{tid}--------{page}---.html",
    "search": "/search/-------------.html?wd={keyword}",
    "detail": "/detail/{vid}.html",
    "play": "/play/{vid}-{sid}-{nid}.html",
    "list_item": r'<a href="(?P<link>/detail/\d+\.html)">\s*<div class="content-card">',
    "item_name": r'<div class="title">([^<]+)</div>',
    "item_pic": r'<img src="([^"]+)"',
    "item_remark": r'<div class="tag text-overflow">([^<]*)</div>',
    "detail_title": r'<h1 class="title">([^<]+)</h1>',
    "detail_pic": r'<img class="lazyload"[^>]*data-original="([^"]+)"',
    # 必须紧跟标题：线上详情页本片没有 tag-box，不锚定会取到下方相关推荐卡片的标签。
    "detail_remark": (
        r'<h1 class="title">[^<]*</h1>\s*'
        r'<div class="tag-box">\s*<div class="tag[^"]*">([^<]*)</div>'
    ),
    "detail_content": (
        r'<div class="title">简介:</div>.*?<div class="wrapper_more_text">.*?<div>(.*?)</div>'
    ),
    "line_tab": r'<a href="#playlist(?P<sid>\d+)"[^>]*>(?P<name>[^<]+)</a>',
    "episode_link": r'<div class="listitem[^"]*"><a href="(?P<link>/play/[^"]+)">([^<]+)</a>',
}

PIANKU_PROFILE = {
    "category": "/vodtype/{tid}.html",
    "category_page": "/vodtype/{tid}-{page}.html",
    "search": "/vodsearch/-------------.html?wd={keyword}",
    "detail": "/voddetail/{vid}.html",
    "play": "/vodplay/{vid}-{sid}-{nid}.html",
    "list_item": r'<div class="vod-item">\s*<a href="(?P<link>/voddetail/\d+\.html)"',
    "item_name": r'title="([^"]+)"',
    "item_pic": r'<img src="([^"]+)"',
    "item_remark": r'<span class="remarks">([^<]*)</span>',
    "detail_title": r'<h1 class="detail-title">([^<]+)',
    "detail_pic": r'<img[^>]+src="([^"]+)"[^>]*referrerpolicy',
    "detail_remark": r'<span class="detail-remarks">([^<]*)</span>',
    # 简介可能是 <p>…</p>，也可能嵌套成 <p><p>…</p></p>：取标题到块结束的整段再去标签。
    "detail_content": r"<h3>剧情简介</h3>(.*?)</div>",
    "line_tab": (
        r'<span class="source-tab-item" data-target="playlist-(?P<sid>\d+)">'
        r"(?P<name>[^<]+)</span>"
    ),
    "episode_link": r'<a href="(?P<link>/vodplay/[^"]+)" class="play-btn-item"[^>]*>([^<]+)</a>',
}

QQYS_PROFILE = {
    # /vodtype/{tid}-{page}.html 不认页码，每页都返回第 1 页；筛选页 /vodshow 才能翻页。
    "category": "/vodshow/{tid}-----------.html",
    "category_page": "/vodshow/{tid}--------{page}---.html",
    "search": "/vodsearch/-------------.html?wd={keyword}",
    "detail": "/voddetail{vid}.html",
    "play": "/vodplay/{vid}-{sid}-{nid}.html",
    "list_item": (
        r'<a\s+href="(?P<link>/voddetail\d+\.html)"\s+title="[^"]*"\s+class="module-poster-item'
    ),
    "item_name": r'title="([^"]+)"',
    "item_pic": r'data-original="([^"]+)"',
    "item_remark": r'<div class="module-item-note">([^<]*)</div>',
    "detail_title": r"<h1[^>]*>([^<]+)</h1>",
    "detail_pic": r'<img[^>]+data-original="([^"]+)"',
    # 本片备注在信息区（备注/集数/连载）；module-item-note 只在下方相关推荐卡片里。
    "detail_remark": (
        r'<span class="module-info-item-title">(?:备注|集数|连载)：</span>\s*'
        r'<div class="module-info-item-content">([^<]*)</div>'
    ),
    "detail_content": r'<div class="module-info-introduction-content">\s*<p>(.*?)</p>',
    "line_tab": r'<div class="module-tab-item[^"]*" data-dropdown-value="(?P<name>[^"]+)">',
    "episode_link": (
        r'<a\s+class="module-play-list-link"\s+href="(?P<link>/vodplay/[^"]+)"[^>]*>'
        r"\s*<span>([^<]+)</span>"
    ),
    # 搜索页是另一套卡片（module-card-item），链接在紧跟的 a 上，必须单独配。
    "search_item": (
        r'<div class="module-card-item(?: [^"]*)?">\s*'
        r'<div class="module-card-item-class">[^<]*</div>\s*'
        r'<a href="(?P<link>/voddetail\d+\.html)"'
    ),
    "search_name": r'<div class="module-card-item-title">\s*<a[^>]*><strong>([^<]+)</strong>',
    "search_pic": r'<img[^>]+data-original="([^"]+)"',
    "search_remark": r'<div class="module-item-note">([^<]*)</div>',
}

LVSC_PROFILE = {
    "category": "/frim/{tid}.html",
    "category_page": "/frim/{tid}-{page}.html",
    "search": "/search.php?searchword={keyword}&page={page}",
    "detail": "/movie/{vid}.html",
    "play": "/play/{vid}-{sid}-{nid}.html",
    # 只认带 lazyload 的卡片：顶部轮播卡片每页都一样；搜索页卡片多了 img-* 类名也能命中。
    "list_item": r'<a class="myui-vodlist__thumb[^"]*lazyload" href="(?P<link>/movie/\d+\.html)"',
    "item_name": r'title="([^"]+)"',
    "item_pic": r'data-original="([^"]+)"',
    "item_remark": r'<span class="pic-text text-right">([^<]*)</span>',
    "detail_title": r'<h1 class="title[^"]*">([^<]+)',
    "detail_pic": r'<div class="myui-content__thumb">.*?data-original="([^"]+)"',
    "detail_remark": r'<h1 class="title[^"]*">[^<]*<font[^>]*>([^<]*)</font>',
    "detail_content": r'id="jq">.*?<div class="tab-content myui-panel_bd">(.*?)</div>',
    # 标签是 #playlist1 起，播放页 sid 从 0 起：不取 sid，按出现顺序对应剧集组。
    "line_tab": r'<a href="#playlist\d+" data-toggle="tab">(?P<name>[^<]+)</a>',
    "episode_link": (
        r'<li id="\d+"><a title="[^"]*" href="(?P<link>/play/[^"]+)"[^>]*>([^<]+)</a>'
    ),
}

SITES = (
    _site(
        "taiee",
        "太乙电影",
        "https://ww96.taiee.xyz",
        "api",
        {"20": "电影", "21": "电视剧", "23": "动漫", "22": "综艺"},
    ),
    _site(
        "cuptgr",
        "茶杯虎",
        "https://cuptgr.com",
        "html",
        {"1": "电影", "2": "电视剧", "4": "动漫", "3": "综艺"},
        CUPTGR_PROFILE,
    ),
    _site(
        "pianku",
        "片库",
        "https://4k01.pianku.online",
        "html",
        {"20": "电影", "37": "电视剧", "43": "动漫", "45": "综艺"},
        PIANKU_PROFILE,
    ),
    _site(
        "qqys",
        "千千影视",
        "https://www.qqys01.com",
        "html",
        {"1": "电影", "2": "电视剧", "4": "动漫", "3": "综艺"},
        QQYS_PROFILE,
    ),
    _site(
        "lvsc",
        "绿色影视",
        "https://www.lvsc168.com",
        "html",
        {"1": "电影", "2": "电视剧", "4": "动漫", "3": "综艺"},
        LVSC_PROFILE,
    ),
    # 聚合站：只能搜索，没有分类。
    _site("lxyy", "洛雪TV", "https://tv.lxyy.club", "aggregator", {}),
)

# 搜索页字段缺省回落到列表页字段；映射必须显式写，字符串替换会把 search_item 变成 itemitem。
SEARCH_FALLBACK = {
    "search_item": "list_item",
    "search_name": "item_name",
    "search_pic": "item_pic",
    "search_remark": "item_remark",
}


def find_site(key):
    for site in SITES:
        if site["key"] == key:
            return site
    return None


def sites_for(extend):
    """按 ext 选站点：站点 key 字符串，或含 site 字段的 JSON；其余情况聚合全部站点。"""
    value = extend
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            value = json.loads(value)
        except ValueError:
            value = ""
    if isinstance(value, dict):
        value = value.get("site", "")
    site = find_site(str(value or "").strip())
    return (site,) if site else SITES


def site_url(site, path):
    if path.startswith(("http://", "https://")):
        return path
    return site["base"] + "/" + path.lstrip("/")


def pic_url(site, path):
    """没取到封面时返回空串，别拼成站点首页让客户端当图片去下载。"""
    return site_url(site, path) if path else ""


# ---------------------------------------------------------------- 网页模板适配器


def html_category(site, tid, page):
    p = site["profile"]
    tpl = p["category_page"] if page > 1 else p["category"]
    url = site_url(site, tpl.format(tid=tid, page=page))
    try:
        text = fetch_text(url, referer=site["base"])
    except FetchError:
        return [], page
    return parse_list(site, text, False), page_count(text, page)


def html_search(site, keyword, page):
    tpl = site["profile"]["search"]
    url = site_url(site, tpl.format(keyword=urllib.parse.quote(keyword), page=page))
    try:
        text = fetch_text(url, referer=site["base"])
    except FetchError:
        return [], page
    return parse_list(site, text, True), page_count(text, page)


def parse_list(site, text, is_search):
    p = site["profile"]

    def pattern(field):
        if is_search and p.get(field):
            return p[field]
        return p.get(SEARCH_FALLBACK[field], "")

    item_pat = pattern("search_item")
    name_pat = pattern("search_name")
    pic_pat = pattern("search_pic")
    remark_pat = pattern("search_remark")
    items = []
    for blk in blocks(item_pat, text):
        link = first(item_pat, blk, "link")
        name = first(name_pat, blk)
        if not link or not name:
            continue
        items.append(
            {
                "vod_id": video_id(link),
                "vod_name": name,
                "vod_pic": pic_url(site, first(pic_pat, blk)),
                "vod_remarks": first(remark_pat, blk),
            }
        )
    return items


def page_count(text, page):
    nums = [int(n) for n in re.findall(r"-page-(\d+)\.html", text)]
    nums += [int(n) for n in re.findall(r">\s*(\d{1,3})\s*</a>", text)]
    return max(nums) if nums else page + FALLBACK_PAGES


def html_detail(site, vid):
    p = site["profile"]
    try:
        text = fetch_text(site_url(site, p["detail"].format(vid=vid)), referer=site["base"])
    except FetchError:
        return None
    name = first(p["detail_title"], text)
    lines = parse_lines(site, text, vid)
    if not name or not lines:
        return None
    vod = {
        "vod_id": vid,
        "vod_name": name,
        "vod_pic": pic_url(site, first(p["detail_pic"], text)),
        "vod_remarks": first(p["detail_remark"], text),
    }
    return build_detail(vod, first(p["detail_content"], text), lines)


def parse_lines(site, text, vid):
    p = site["profile"]
    grouped = {}
    for link, ep_name in re.findall(p["episode_link"], text, re.S):
        m = re.search(r"-(\d+)-(\d+)\.html", link)
        sid, nid = (m.group(1), m.group(2)) if m else ("1", "1")
        grouped.setdefault(sid, []).append({"name": clean(ep_name), "value": f"{vid}-{sid}-{nid}"})
    # 标签不带 sid 时第 n 个标签对应第 n 组剧集：千千的标签依次是爱奇艺、FF、BF…，
    # 对应的剧集 sid 却是 1、3、5…，不能拿标签序号当 sid。
    sids = list(grouped)
    names = {}
    for idx, m in enumerate(re.finditer(p["line_tab"], text, re.S)):
        groups = m.groupdict()
        sid = (groups.get("sid") or "").strip() or (sids[idx] if idx < len(sids) else "")
        name = clean(groups.get("name") or "")
        if sid and name:
            names.setdefault(sid, name)
    return [{"name": names.get(sid, f"线路{sid}"), "episodes": eps} for sid, eps in grouped.items()]


def html_play(site, play_value):
    parts = play_value.split("-")
    if len(parts) != 3:
        return {"parse": 0, "url": "", "msg": f"播放值 {play_value} 不是 vid-sid-nid"}
    vid, sid, nid = parts
    p = site["profile"]
    url = site_url(site, p["play"].format(vid=vid, sid=sid, nid=nid))
    try:
        text = fetch_text(url, referer=site_url(site, p["detail"].format(vid=vid)))
    except FetchError:
        return {"parse": 0, "url": "", "msg": f"播放页打不开：{url}"}
    raw = player_url(text)
    if not raw:
        return {"parse": 0, "url": "", "msg": "播放页没有 player_aaaa"}
    result = player_result(raw)
    path = urllib.parse.urlsplit(result["url"]).path
    if result["parse"] and re.search(r"/share/[0-9A-Za-z]+/?$", path):
        direct = share_page_m3u8(result["url"], url)
        if direct:
            return player_result(direct)
    return result


def player_url(text):
    for name in ("player_aaaa", "player_data"):
        payload = extract_json(text, name)
        value = payload.get("url") if payload else None
        if isinstance(value, str) and value:
            return value.replace("\\/", "/")
    # 海洋CMS：var now=base64decode("…") 或 var now="…"。
    m = re.search(r'var\s+now\s*=\s*(?:base64decode|unescape)?\(?\s*"([^"]+)"', text)
    return m.group(1) if m else ""


def share_page_m3u8(url, referer):
    """资源站分享页（如 ryiplay 的 /share/<id>）是网页播放器，m3u8 在页内 var main 里。"""
    try:
        text = fetch_text(url, referer=referer)
    except FetchError:
        return ""
    m = re.search(r'var\s+main\s*=\s*"([^"]+)"', text)
    return urllib.parse.urljoin(url, m.group(1).replace("\\/", "/")) if m else ""


def extract_json(text, name):
    """取出 `name = {...}` 里的 JSON 对象，按括号配对截取。"""
    m = re.search(name + r"\s*=\s*\{", text)
    if not m:
        return None
    start = m.end() - 1
    depth = 0
    for i in range(start, min(len(text), start + 20000)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    payload = json.loads(text[start : i + 1])
                except ValueError:
                    return None
                return payload if isinstance(payload, dict) else None
    return None


# ---------------------------------------------------------------- 采集接口适配器

API_PATH = "/api.php/provide/vod/"


def api_get(site, params):
    query = "&".join(f"{k}={v}" for k, v in params.items())
    try:
        text = fetch_text(site_url(site, API_PATH) + "?" + query, referer=site["base"])
        payload = json.loads(text)
    except (FetchError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def api_raw_list(payload):
    items = payload.get("list")
    return [x for x in items if isinstance(x, dict)] if isinstance(items, list) else []


def api_list(payload):
    out = []
    for item in api_raw_list(payload):
        vid = str(item.get("vod_id", "")).strip()
        name = str(item.get("vod_name", "")).strip()
        if vid and name:
            out.append(
                {
                    "vod_id": vid,
                    "vod_name": name,
                    "vod_pic": str(item.get("vod_pic") or ""),
                    "vod_remarks": str(item.get("vod_remarks") or ""),
                }
            )
    return out


def api_detail(vid, payload):
    items = api_raw_list(payload)
    if not items:
        return None
    item = items[0]
    from_list = str(item.get("vod_play_from") or "").split(LINE_SEP)
    lines = []
    for idx, chunk in enumerate(str(item.get("vod_play_url") or "").split(LINE_SEP)):
        episodes = []
        for entry in chunk.split(EP_SEP):
            entry = entry.strip()
            if not entry:
                continue
            name, sep, value = entry.partition(NAME_SEP)
            if not sep:
                name, value = "", name
            episodes.append(
                {"name": name.strip() or f"第{len(episodes) + 1}集", "value": value.strip()}
            )
        if episodes:
            line = from_list[idx].strip() if idx < len(from_list) else ""
            lines.append({"name": line or f"线路{idx + 1}", "episodes": episodes})
    if not lines:
        return None
    vod = {
        "vod_id": str(item.get("vod_id", vid)),
        "vod_name": str(item.get("vod_name", "")),
        "vod_pic": str(item.get("vod_pic") or ""),
        "vod_remarks": str(item.get("vod_remarks") or ""),
    }
    return build_detail(vod, clean(str(item.get("vod_content") or "")), lines)


def api_pages(payload, page):
    try:
        return max(1, int(payload.get("pagecount") or payload.get("page_count")))
    except (TypeError, ValueError):
        return page


# ---------------------------------------------------------------- 聚合站代理接口（洛雪TV）

PROXY_PATH = "/api/proxy.php"
AGG_ID_SEP = "/"  # 影片 id 编成「资源key/资源内id」


def agg_get(site, query):
    try:
        text = fetch_text(site_url(site, PROXY_PATH) + "?" + query, referer=site["base"] + "/")
        payload = json.loads(text)
    except (FetchError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def agg_search(site, keyword, page):
    """跨资源搜索；要求验证码时返回空（客户端没法答题）。备注带资源名，同名片子才分得清。"""
    payload = agg_get(site, f"action=search&wd={urllib.parse.quote(keyword)}&page={page}")
    rows = payload.get("results")
    if payload.get("need_captcha") or not isinstance(rows, list):
        return [], page
    items = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        source = str(row.get("source") or "").strip()
        vid = str(row.get("id") or "").strip()
        name = str(row.get("name") or "").strip()
        if source and vid and name:
            remark = " ".join(
                x for x in (str(row.get("source_name") or ""), str(row.get("remarks") or "")) if x
            )
            items.append(
                {
                    "vod_id": source + AGG_ID_SEP + vid,
                    "vod_name": name,
                    "vod_pic": str(row.get("pic") or ""),
                    "vod_remarks": remark,
                }
            )
    return items, (page + 1 if items else page)


def agg_detail(site, vid):
    source, _, item_id = vid.partition(AGG_ID_SEP)
    if not source or not item_id:
        return None
    details = agg_get(site, f"action=detail&source={source}&id={item_id}").get("details")
    if not isinstance(details, list) or not details or not isinstance(details[0], dict):
        return None
    item = details[0]
    lines = []
    groups = item.get("episodes") if isinstance(item.get("episodes"), list) else []
    for idx, group in enumerate(groups):
        if not isinstance(group, dict) or not isinstance(group.get("episodes"), list):
            continue
        episodes = [
            {
                "name": str(ep.get("name") or "").strip() or f"第{num}集",
                "value": str(ep.get("url") or "").strip(),
            }
            for num, ep in enumerate(group["episodes"], 1)
            if isinstance(ep, dict) and ep.get("url")
        ]
        if episodes:
            name = str(group.get("group") or "").strip() or f"线路{idx + 1}"
            lines.append({"name": name, "episodes": episodes})
    if not lines:
        return None
    vod = {
        "vod_id": vid,
        "vod_name": str(item.get("name") or ""),
        "vod_pic": str(item.get("pic") or ""),
        "vod_remarks": str(item.get("remarks") or ""),
    }
    return build_detail(vod, clean(str(item.get("content") or "")), lines)


# ---------------------------------------------------------------- 按站点分派


def video_id(link):
    m = re.search(r"(\d+)", link)
    return m.group(1) if m else link


def _strip_seps(text):
    for sep in (LINE_SEP, EP_SEP, NAME_SEP):
        text = text.replace(sep, "")
    return text.strip()


def needs_parse(line):
    """线路是否需要解析：HTML 站点的播放值是 vid-sid-nid 只能看线路名，采集接口直接看地址。"""
    name = line["name"].strip().lower()
    if name in PARSE_LINE_CODES or any(word in name for word in PARSE_LINE_WORDS):
        return True
    return bool(line["episodes"]) and player_result(line["episodes"][0]["value"])["parse"] == 1


def build_detail(vod, content, lines):
    # 客户端从第一条线路播起、失败才换线：直链在前，需解析的在后；sorted 稳定，同类保持原序。
    lines = sorted(lines, key=needs_parse)
    names = LINE_SEP.join(_strip_seps(ln["name"]) or "线路" for ln in lines)
    urls = LINE_SEP.join(
        EP_SEP.join(_strip_seps(ep["name"]) + NAME_SEP + ep["value"] for ep in ln["episodes"])
        for ln in lines
    )
    detail = dict(vod)
    detail.update({"vod_content": content, "vod_play_from": names, "vod_play_url": urls})
    return detail


def site_category(site, tid, page):
    if site["kind"] == "aggregator":
        return [], page
    if site["kind"] == "api":
        payload = api_get(site, {"ac": "detail", "t": tid, "pg": page})
        return api_list(payload), api_pages(payload, page)
    return html_category(site, tid, page)


def site_search(site, keyword, page):
    if site["kind"] == "aggregator":
        return agg_search(site, keyword, page)
    if site["kind"] == "api":
        payload = api_get(site, {"ac": "detail", "wd": urllib.parse.quote(keyword), "pg": page})
        return api_list(payload), api_pages(payload, page)
    return html_search(site, keyword, page)


def site_detail(site, vid):
    if site["kind"] == "aggregator":
        return agg_detail(site, vid)
    if site["kind"] == "api":
        return api_detail(vid, api_get(site, {"ac": "detail", "ids": vid}))
    return html_detail(site, vid)


def site_play(site, play_value):
    if site["kind"] in ("api", "aggregator"):
        return player_result(play_value)
    return html_play(site, play_value)


# ---------------------------------------------------------------- 入口


class Spider(_Base):
    """Python 爬虫入口。"""

    def __init__(self):
        _Base.__init__(self)
        self.sites = SITES

    def init(self, extend=""):
        self.sites = sites_for(extend)

    def getName(self):  # noqa: N802 - 客户端约定驼峰
        return self.sites[0]["name"] if len(self.sites) == 1 else "硬核指南合集"

    def homeContent(self, filter=False):  # noqa: N802
        if len(self.sites) == 1:
            # 单站点：站内分类直接作为首页分类。
            site = self.sites[0]
            classes = [
                {"type_id": site["key"] + "-" + cid, "type_name": name}
                for cid, name in site["categories"].items()
            ]
            return {"class": classes}
        # 聚合：每个站点是一个分类，站内分类放进筛选项，选中后以 extend={"cid": ...} 回传。
        # 只能搜索的站点（聚合站）没有分类，不放进首页。
        browsable = [s for s in self.sites if s["categories"]]
        classes = [{"type_id": s["key"], "type_name": s["name"]} for s in browsable]
        filters = {
            s["key"]: [
                {
                    "key": FILTER_KEY,
                    "name": "分类",
                    "value": [{"n": name, "v": cid} for cid, name in s["categories"].items()],
                }
            ]
            for s in browsable
        }
        return {"class": classes, "filters": filters}

    def homeVideoContent(self):  # noqa: N802
        return {"list": []}

    def categoryContent(self, tid, pg="1", filter=False, extend=None):  # noqa: N802
        site, cid = split_tid(tid, self.sites)
        if site is None:
            return page_payload([], 1, 1)
        chosen = str(extend.get(FILTER_KEY, "")) if isinstance(extend, dict) else ""
        if chosen in site["categories"]:
            cid = chosen
        page = to_page(pg)
        items, pages = site_category(site, cid, page)
        return page_payload(with_site_prefix(items, site), page, pages)

    def searchContent(self, key, quick=False, pg="1"):  # noqa: N802
        page = to_page(pg)
        collected = []
        for site in self.sites:
            try:
                items, _ = site_search(site, key, page)
            except Exception:  # 单站点出错不影响其余站点
                continue
            collected.extend(with_site_prefix(items, site))
        return page_payload(collected, page, 1)

    def detailContent(self, ids):  # noqa: N802
        raw = ids[0] if isinstance(ids, list) and ids else ids
        site_key, vid = split_item_id(str(raw or ""))
        site = find_site(site_key)
        if site is None:
            return {"list": []}
        try:
            detail = site_detail(site, vid)
        except Exception:
            return {"list": []}
        if detail is None:
            return {"list": []}
        detail["vod_id"] = site["key"] + SITE_SEP + detail["vod_id"]
        detail["vod_play_url"] = prefix_play_url(detail["vod_play_url"], site["key"])
        return {"list": [detail]}

    def playerContent(self, flag, id="", vipFlags=None):  # noqa: N802, N803
        site_key, play_value = split_play_id(id)
        site = find_site(site_key)
        if site is None:
            return {"parse": 0, "url": "", "msg": f"播放值缺少站点前缀: {id}"}
        try:
            return site_play(site, play_value)
        except Exception as exc:
            return {"parse": 0, "url": "", "msg": f"播放失败: {exc}"}

    def liveContent(self, url):  # noqa: N802
        return ""

    def isVideoFormat(self, url):  # noqa: N802
        return urllib.parse.urlsplit(url or "").path.lower().endswith(PLAYABLE_SUFFIXES)

    def manualVideoCheck(self):  # noqa: N802
        return False

    def action(self, action):
        return {}

    def destroy(self):
        return None


def split_tid(tid, sites):
    """拆分「站点key-分类id」；只给站点 key 时取该站点第一个分类。"""
    for site in sites:
        if tid == site["key"]:
            return (site, next(iter(site["categories"]))) if site["categories"] else (None, "")
        prefix = site["key"] + "-"
        if tid.startswith(prefix):
            return site, tid[len(prefix) :]
    return None, ""


def with_site_prefix(items, site):
    """给 vod_id 加站点前缀（详情页靠它找回站点），同页内按 vod_id 去重。"""
    out = []
    seen = set()
    for item in items:
        if item["vod_id"] in seen:
            continue
        seen.add(item["vod_id"])
        out.append(dict(item, vod_id=site["key"] + SITE_SEP + item["vod_id"]))
    return out


def split_item_id(value):
    key, _, vid = value.partition(SITE_SEP)
    return (key, vid) if vid else ("", value)


def split_play_id(value):
    key, _, play_value = value.partition(PLAY_SEP)
    return (key, play_value) if play_value else ("", value)


def prefix_play_url(play_url, site_key):
    lines = []
    for line in play_url.split(LINE_SEP):
        eps = []
        for entry in line.split(EP_SEP):
            name, sep, value = entry.partition(NAME_SEP)
            eps.append(name + NAME_SEP + site_key + PLAY_SEP + value if sep else entry)
        lines.append(EP_SEP.join(eps))
    return LINE_SEP.join(lines)


def to_page(pg):
    try:
        return max(1, int(pg))
    except (TypeError, ValueError):
        return 1


def page_payload(items, page, pages):
    return {
        "page": page,
        "pagecount": max(1, pages),
        "limit": len(items),
        "total": len(items),
        "list": items,
    }
