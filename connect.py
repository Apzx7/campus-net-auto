#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校园网自动连接工具（锐捷 SAM+ 5.x / CAS-SSO 门户）。

原理：这台校园网的门户不是老式 ePortal `InterFace.do`，登录链路是：

    http://10.254.241.66/
      -> /self/index
      -> /oauth2/authorization/custom-self
      -> /cas-sso/oauth2.0/authorize
      -> /pc/center?service=...        <-- 登录表单所在页
      提交 POST /cas-sso/login?service=...   (username / password / execution / _eventId=submit)
      -> /login/oauth2/code/custom-self
      -> 门户流程引擎下发认证 -> /portal/entry/pc/finish   <-- 登录成功落地页

登录成功后，接入设备才放行流量。因为门户用服务端「流程引擎」按 sessionId 逐节点下发
认证，而 sessionId 依赖接入设备下发的一次性 queryString（需要 portal key，客户端拿不到），
所以这里不重放 HTTP 报文，而是用真实浏览器（Playwright + 本机 Edge/Chrome）复现
「打开门户 → 填账号密码 → 勾选协议 → 点立即登录」这一套动作；协议变了也不容易坏。

用法：
    python connect.py                 连一次（已在线则立即退出）
    python connect.py --setup         交互式录入账号密码（密码用 Windows DPAPI 加密保存）
    python connect.py --verify        只检测当前是否已联网
    python connect.py --watch         常驻，掉线自动重连
    python connect.py --headful       显示浏览器窗口（排查问题用）
    python connect.py --diagnose      抓取登录页结构，用于门户改版后修选择器
    python connect.py --quiet         静默模式（计划任务用，只写日志）

退出码：0 已联网或登录成功，1 登录失败，2 配置/环境错误，3 门户不可达。
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import ctypes.wintypes as wt
import getpass
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
LOG_DIR = APP_DIR / "logs"
LOG_FILE = LOG_DIR / "connect.log"

DEFAULTS = {
    "portal_url": "http://10.254.241.66/",
    "username": "",
    "password_dpapi": "",
    "password": "",
    "headless": True,
    "browser_channel": "msedge",
    "login_timeout_sec": 75,
    "retries": 2,
    "check_interval_sec": 60,
    "portal_http_timeout_sec": 5,
    "require_ethernet": True,
    "wait_ready_sec": 8,
    "link_grace_sec": 12,
    "service_name": "",
}

# 门户改版后一般只需要改这里
SEL_USERNAME = "#nameInput, input[name='username'], input[placeholder*='账号']"
SEL_PASSWORD = "input[type='password'], input[placeholder*='密码']"
SEL_AGREE = "label.protocol__track, label.ant-checkbox-wrapper"
SEL_SUBMIT = (
    "button.igniter--in-password, button:has-text('立即登录'), "
    "button:has-text('登录'), button:has-text('登 录')"
)
# 登录成功后的路径特征（/pc/center 是登录页本身，不能算成功）
RE_SUCCESS_URL = re.compile(r"10\.254\.241\.66/(portal|self)/")
# 失败提示关键词（命中即判定失败）
ERROR_KEYWORDS = (
    "密码错误", "密码不正确", "用户名或密码", "账号或密码", "用户不存在",
    "账号不存在", "账号已锁定", "认证失败", "验证码", "账号或密码错误",
)
# 登录后可能还需要点一下的按钮
CONNECT_BUTTON_TEXTS = ("连接", "立即连接", "上线", "立即上线", "确认连接")
# 兜底用：离线时访问这个 http 地址，接入设备会把浏览器劫持到真正的认证入口
BAIT_URL = "http://captive.apple.com/hotspot-detect.html"

CONNECTIVITY_PROBES = (
    # 按实测"总耗时"排序（DNS + 连接）：baidu 最快且能校验内容，放第一个。
    # gstatic/miui 的 DNS 解析要 11 秒，只能当兜底。
    ("https://www.baidu.com/", 200, b"baidu"),
    ("http://www.msftconnecttest.com/connecttest.txt", 200, b"Microsoft Connect Test"),
    ("http://www.gstatic.com/generate_204", 204, None),
    ("http://connect.rom.miui.com/generate_204", 204, None),
)
# 开机第一轮用这个：纯 IP、不做 DNS、能在几十毫秒内出结果。
# 注意：它只能证明"内网通"，不能证明"能上外网"——校园网认证前网关也放行它，
# 所以它只在 check_online 里当"快速否定"用，绝不单独作为在线判据。
FAST_PROBE = ("http://223.5.5.5/", (200, 301, 302, 403, 404))  # 阿里 DNS 的 80 端口

log = logging.getLogger("campus")


# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #
def setup_logging(quiet: bool, verbose: bool = False) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.DEBUG)
    log.handlers.clear()

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.addHandler(fh)

    if not quiet:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        sh.setLevel(logging.DEBUG if verbose else logging.INFO)
        log.addHandler(sh)

    if LOG_FILE.exists() and LOG_FILE.stat().st_size > 2 * 1024 * 1024:
        try:
            LOG_FILE.replace(LOG_FILE.with_suffix(".log.1"))
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# 联网检测
# --------------------------------------------------------------------------- #
def _open_no_redirect(url: str, timeout: float):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "Mozilla/5.0"})
    return opener.open(req, timeout=timeout)


def _probe_once(url: str, expect: int, marker, timeout: float):
    """单点探测。返回 (是否在线, 说明)。"""
    try:
        with _open_no_redirect(url, timeout) as resp:
            code = resp.getcode()
            if code != expect:
                return None, f"{url} -> {code}"
            if marker is not None:
                body = resp.read(4096).lower()
                if marker.lower() not in body:
                    return None, f"{url} -> {code}（内容被替换，疑似门户劫持）"
            return True, f"{url} -> {code}"
    except urllib.error.HTTPError as exc:
        location = exc.headers.get("Location") if exc.headers else None
        return None, f"{url} -> {exc.code}" + (f" (跳转 {location[:120]})" if location else "")
    except Exception as exc:  # noqa: BLE001 - 网络层错误一律当作离线
        return None, f"{url} -> {type(exc).__name__}: {exc}"


def check_online(timeout: float = 6.0) -> tuple[bool, str]:
    """返回 (是否已联网, 说明)。

    判据分两层，很关键：
    - 快速通道（纯 IP）只能**快速否定**：连它都不通，肯定没网；通了不代表能上外网。
      校园网在未认证时会放行这个 IP，如果拿它当在线判据，休眠唤醒后会误判"已在线"
      而不去认证，表现就是"显示在线但上不了网"。
    - 真正的在线判据必须用**能识别劫持的域名探测**（204 端点 / 校验响应内容）。
    """
    from concurrent.futures import ThreadPoolExecutor

    # ① 快速否定：纯 IP，连不上就不用再等了（快 0.05s）
    url, codes = FAST_PROBE
    reachable = False
    try:
        with _open_no_redirect(url, min(timeout, 3.0)) as resp:
            reachable = resp.getcode() in codes
    except urllib.error.HTTPError as exc:
        reachable = exc.code in codes
    except Exception:  # noqa: BLE001
        reachable = False
    if not reachable:
        return False, f"{url} 不可达（内网未通）"

    # ② 真正的判据：并发探测域名，**谁先成功就用谁**（不等最慢的那个）
    from concurrent.futures import FIRST_COMPLETED, wait

    results: list[str] = []
    pool = ThreadPoolExecutor(max_workers=len(CONNECTIVITY_PROBES))
    try:
        futs = {
            pool.submit(_probe_once, u, e, m, timeout): u
            for u, e, m in CONNECTIVITY_PROBES
        }
        hard_deadline = time.time() + timeout + 2
        while futs and time.time() < hard_deadline:
            done, _ = wait(list(futs), timeout=0.5, return_when=FIRST_COMPLETED)
            for fut in done:
                futs.pop(fut, None)
                try:
                    ok, msg = fut.result()
                except Exception as exc:  # noqa: BLE001
                    results.append(f"探测异常: {exc}")
                    continue
                if ok:
                    return True, msg
                results.append(msg)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return False, "; ".join(results) or "无可用探测点"


def find_portal_entry(fallback: str, timeout: float = 6.0) -> str:
    """探测接入设备的劫持跳转，拿到带一次性 queryString 的真实门户入口。

    离线时任意 http 请求都会被 NAS 302 到门户；那个地址才是能走完认证流程的入口，
    直接开门户根地址只会在「自助服务」打转。探测不到就退回配置里的地址。
    """
    for url, _expect, _marker in CONNECTIVITY_PROBES:
        if not url.startswith("http://"):  # https 会被证书告警挡住，只看明文 http
            continue
        try:
            with _open_no_redirect(url, timeout) as resp:
                if resp.getcode() in (200, 204):
                    return ""  # 能正常访问，说明没被劫持
        except urllib.error.HTTPError as exc:
            loc = exc.headers.get("Location") if exc.headers else None
            if exc.code in (301, 302, 303, 307, 308) and loc:
                entry = urllib.parse.urljoin(url, loc)
                log.info("探测到门户入口：%s", entry[:200])
                return entry
        except Exception:  # noqa: BLE001
            continue
    return ""


def portal_reachable(portal_url: str, timeout: float = 5.0) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        req = urllib.request.Request(portal_url, method="GET", headers={"User-Agent": "Mozilla/5.0"})
        with opener.open(req, timeout=timeout) as resp:
            return resp.getcode() < 500
    except urllib.error.HTTPError as exc:
        return exc.code < 500
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- #
# 网线闸门 / 开机等待网络就绪
# --------------------------------------------------------------------------- #
_PS_ETHERNET = (
    "$a=@(Get-NetAdapter -Physical -ErrorAction SilentlyContinue | "
    "Where-Object { $_.Status -eq 'Up' -and $_.PhysicalMediaType -like '802.3*' }); "
    "if($a.Count -gt 0){ ($a[0].Name + '|' + $a[0].LinkSpeed) }"
)


def _ethernet_via_netip(portal_url: str) -> tuple[bool, str]:
    """最快路径：只看「到门户的网卡」在不在，纯 socket，几毫秒。"""
    parsed = urllib.parse.urlparse(portal_url)
    host, port = parsed.hostname, parsed.port or 80
    if not host:
        return False, ""
    try:
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.5)
            s.connect((host, port))
            ip = s.getsockname()[0]
        if ip and not ip.startswith("169.254."):
            return True, f"{ip}"
    except Exception:  # noqa: BLE001
        pass
    return False, ""


def physical_ethernet_up(timeout: float = 10.0, portal_url: str = "") -> tuple[bool, str]:
    """是否有「网线插好且已连通」的物理以太网卡。

    优先用纯 socket 判断（毫秒级）；拿不准时才启动 PowerShell 查网卡详情。
    """
    if os.name != "nt":
        return True, "非 Windows，跳过检查"

    fast_ok, fast_ip = _ethernet_via_netip(portal_url or "http://10.254.241.66/")
    if fast_ok:
        return True, f"已获取地址 {fast_ip}"

    # 让 PowerShell 自己把中文转成 UTF-8 输出，避免 GBK/UTF-8 混用产生乱码
    script = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + _PS_ETHERNET
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        info = (proc.stdout or "").strip()
        if info:
            name, _, speed = info.partition("|")
            return True, f"{name}（{speed}）"
        return False, ""
    except Exception as exc:  # noqa: BLE001
        # 查不出来时不拦截，交给后面的门户探测判断
        return True, f"网卡状态未知（{type(exc).__name__}），继续尝试"


def wait_until_ready(cfg: dict, max_wait: int) -> bool:
    """开机后网络栈/DHCP 可能还没就绪，这里等门户可达再动手。"""
    if max_wait <= 0:
        return True
    deadline = time.time() + max_wait
    waited = 0
    while time.time() < deadline:
        if portal_reachable(cfg["portal_url"], cfg.get("portal_http_timeout_sec", 5)):
            if waited:
                log.info("网络已就绪（等了 %d 秒）", waited)
            return True
        time.sleep(3)
        waited += 3
    log.warning("等了 %d 秒门户仍不可达，仍尝试一次", max_wait)
    return False


# --------------------------------------------------------------------------- #
# 纯 HTTP 认证（抓包得到的真实协议，不需要浏览器）
# --------------------------------------------------------------------------- #
# 实测链路（Burp 抓包 2026-10-30）：
#   1. GET  /eportal/index.jsp?wlanuserip=<本机IP>&wlanacname=<AC名>
#            → 302 Location: /portal/portal-main?sessionId=xxx&userIp=...&nasIp=...
#   2. （浏览器完成 CAS 登录，把账号会话绑到该 sessionId）
#   3. POST /eportal/network/serviceLogin  {"sessionId":"xxx","service":"cmcc"}
#            → {"data":{"authResult":"success"}}       ← 这一步才真正放行网络
#   4. POST /eportal/network/userOnline    {"sessionId":"xxx"} → online:true
SERVICE_CODES = {
    "移动": "cmcc", "中国移动": "cmcc", "cmcc": "cmcc",
    "电信": "telecom", "中国电信": "telecom", "telecom": "telecom",
    "联通": "unicom", "中国联通": "unicom", "unicom": "unicom",
}


def service_code(name: str) -> str:
    """把配置里的运营商名字换成门户认的 service 代码。"""
    key = (name or "").strip()
    if not key:
        return "cmcc"
    if key.lower() in SERVICE_CODES:
        return SERVICE_CODES[key.lower()]
    for k, v in SERVICE_CODES.items():
        if k in key or key in k:
            return v
    log.warning("看不懂的运营商「%s」，按移动(cmcc)提交", key)
    return "cmcc"


def _local_ipv4(portal_url: str) -> str:
    """取访问门户时用的本机 IPv4（门户按这个 IP 认终端）。"""
    parsed = urllib.parse.urlparse(portal_url)
    host, port = parsed.hostname, parsed.port or 80
    if not host:
        return ""
    try:
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(2)
            s.connect((host, port))
            return s.getsockname()[0]
    except Exception:  # noqa: BLE001
        return ""


def _http_json(url: str, payload: dict, cookie: str = "", timeout: float = 8.0):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/plain, */*")
    req.add_header("User-Agent", "Mozilla/5.0")
    req.add_header("isPortal", "true")
    if cookie:
        req.add_header("Cookie", cookie)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace") or "{}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _ac_name() -> str:
    """接入设备（AC）名称。换学校若设备厂商不同，改这里即可。"""
    return "H3C-CR16010-F"


def resolve_session(portal_url: str, ip: str = "", timeout: float = 10.0):
    """请求 ePortal 入口，拿到 (sessionId, nasIp, cookie)。

    三处（认证 / 在线确认 / 入口探测）共用同一套解析，避免字符串重复。
    返回的空字符串表示没拿到，调用方自行决定如何处理。
    """
    base = portal_url.rstrip("/")
    ip = ip or _local_ipv4(portal_url)
    if not ip:
        return "", "", ""
    entry = f"{base}/eportal/index.jsp?wlanuserip={ip}&wlanacname={_ac_name()}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        opener.open(urllib.request.Request(entry, headers={"User-Agent": "Mozilla/5.0"}),
                    timeout=timeout)
        return "", "", ""
    except urllib.error.HTTPError as exc:
        loc = exc.headers.get("Location", "") or ""
        raw = exc.headers.get("Set-Cookie", "") or ""
        m = re.search(r"JSESSIONID=([0-9A-Fa-f]+)", raw)
        cookie = f"JSESSIONID={m.group(1)}" if m else ""
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)
        sid = (qs.get("sessionId") or [""])[0]
        nas = (qs.get("nasIp") or [""])[0]
        return sid, nas, cookie
    except Exception:  # noqa: BLE001
        return "", "", ""


def http_login(cfg: dict, entry_url: str = "") -> tuple[bool, str]:
    """不走浏览器，直接调门户接口完成认证。

    返回 (是否成功, 说明)。失败时由调用方决定是否回退到浏览器方案。
    """
    base = cfg["portal_url"].rstrip("/")
    ip = _local_ipv4(cfg["portal_url"])
    if not ip:
        return False, "取不到本机 IP"

    # 1) 入口跳转拿 sessionId（entry_url 存在时直接用它，兼容劫持跳转）
    if entry_url:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        session_id, cookie, nas_ip = "", "", ""
        try:
            opener.open(urllib.request.Request(entry_url,
                        headers={"User-Agent": "Mozilla/5.0"}), timeout=10)
        except urllib.error.HTTPError as exc:
            loc = exc.headers.get("Location", "") or ""
            raw_cookie = exc.headers.get("Set-Cookie", "") or ""
            m = re.search(r"JSESSIONID=([0-9A-Fa-f]+)", raw_cookie)
            if m:
                cookie = f"JSESSIONID={m.group(1)}"
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)
            session_id = (qs.get("sessionId") or [""])[0]
            nas_ip = (qs.get("nasIp") or [""])[0]
        except Exception as exc:  # noqa: BLE001
            return False, f"打开入口失败：{type(exc).__name__}: {exc}"
    else:
        session_id, nas_ip, cookie = resolve_session(cfg["portal_url"], ip)
    log.info("入口跳转：sessionId=%s nasIp=%s", session_id or "?", nas_ip or "?")

    if not session_id:
        return False, "入口没有返回 sessionId（可能已在线，或接入设备未下发）"

    # 2) 用 CAS 表单把账号绑定到该 sessionId（等价于浏览器里登录）
    bound, bmsg = cas_bind_session(cfg, session_id, nas_ip, ip, cookie)
    log.info("CAS 绑定：%s", bmsg)
    if not bound:
        return False, f"账号绑定失败：{bmsg}"

    # 3) 提交认证（这一步才放行网络）
    svc = service_code(cfg.get("service_name") or "")
    try:
        r = _http_json(f"{base}/eportal/network/serviceLogin",
                       {"sessionId": session_id, "service": svc}, cookie)
    except Exception as exc:  # noqa: BLE001
        return False, f"serviceLogin 失败：{type(exc).__name__}: {exc}"

    data = r.get("data") or {}
    if r.get("code") == 200 and data.get("authResult") == "success":
        log.info("认证成功：service=%s authResult=success", svc)
        return True, f"认证成功（service={svc}）"
    return False, f"认证未通过：{data.get('authMessage') or r.get('message') or r}"


def cas_bind_session(cfg: dict, session_id: str, nas_ip: str, user_ip: str,
                     cookie: str = "") -> tuple[bool, str]:
    """用 CAS 表单登录把账号绑定到 flowSessionId（纯 HTTP，等价于浏览器里填表单）。

    抓包确认：登录 URL 带 flowSessionId / nasIp / userIp，
    表单字段 username / password / type=UsernamePassword / _eventId=submit / execution。
    """
    base = cfg["portal_url"].rstrip("/")
    params = {
        "flowSessionId": session_id,
        "customPageId": "",
        "preview": "false",
        "appType": "normal",
        "language": "zh-CN",
        "timer": str(int(time.time() * 1000)),
        "nasIp": nas_ip,
        "userIp": user_ip,
    }
    login_url = f"{base}/cas-sso/login?" + urllib.parse.urlencode(params)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    jar_cookie = cookie

    # 1) 取 execution
    try:
        req = urllib.request.Request(login_url, headers={"User-Agent": "Mozilla/5.0"})
        if jar_cookie:
            req.add_header("Cookie", jar_cookie)
        with opener.open(req, timeout=10) as resp:
            html_text = resp.read().decode("utf-8", "replace")
            sc = resp.headers.get("Set-Cookie", "") or ""
        m = re.search(r"SESSION=([0-9a-f\-]+)", sc)
        if m:
            jar_cookie = f"SESSION={m.group(1)}"
    except urllib.error.HTTPError as exc:
        html_text = exc.read().decode("utf-8", "replace") if exc.fp else ""
        sc = exc.headers.get("Set-Cookie", "") or ""
        m = re.search(r"SESSION=([0-9a-f\-]+)", sc)
        if m:
            jar_cookie = f"SESSION={m.group(1)}"
    except Exception as exc:  # noqa: BLE001
        return False, f"打开登录页失败：{type(exc).__name__}: {exc}"

    # execution 藏在 <div id="login-page-flowkey"> 里（前端 JS 也是这么取的）
    execution = ""
    for pat in (
        r'id=["\']login-page-flowkey["\'][^>]*>([^<]{20,})<',
        r'name=["\']execution["\'][^>]*value=["\']([^"\']+)["\']',
        r'value=["\']([^"\']{40,})["\'][^>]*name=["\']execution["\']',
    ):
        m = re.search(pat, html_text)
        if m:
            execution = m.group(1).strip()
            break
    if not execution:
        return False, "登录页没拿到 execution（页面结构可能变了）"

    # 2) 提交表单
    body = urllib.parse.urlencode({
        "username": (cfg.get("username") or "").strip(),
        "password": resolve_password(cfg),
        "type": "UsernamePassword",
        "_eventId": "submit",
        "geolocation": "",
        "execution": execution,
    }).encode("utf-8")
    req = urllib.request.Request(login_url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("User-Agent", "Mozilla/5.0")
    req.add_header("Referer", login_url)
    if jar_cookie:
        req.add_header("Cookie", jar_cookie)

    try:
        with opener.open(req, timeout=15) as resp:
            code = resp.getcode()
            loc = resp.headers.get("Location", "") or ""
    except urllib.error.HTTPError as exc:
        code = exc.code
        loc = exc.headers.get("Location", "") or ""
    except Exception as exc:  # noqa: BLE001
        return False, f"提交登录失败：{type(exc).__name__}: {exc}"

    log.info("CAS 提交返回 %s，跳转：%s", code, (loc or "(无)")[:120])
    if code in (301, 302, 303) and loc and "login" not in loc.lower():
        return True, "CAS 登录成功"
    if code in (301, 302, 303) and ("code=" in loc or "callbackAuthorize" in loc):
        return True, "CAS 登录成功"
    return False, f"CAS 登录未通过（HTTP {code}）"


def http_user_online(cfg: dict) -> bool:
    """再问一次门户：终端是否已在线。

    注意：`check_online()` 才是最终判据（它验证真的能出网）。这个函数只用于
    "接口说成功但出口还没放行"时做二次确认，失败一律返回 False。
    """
    base = cfg["portal_url"].rstrip("/")
    sid, _nas, cookie = resolve_session(cfg["portal_url"])
    if not sid:
        return False
    try:
        r = _http_json(f"{base}/eportal/network/userOnline", {"sessionId": sid}, cookie)
        return bool((r.get("data") or {}).get("online"))
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- #
# 凭据：Windows DPAPI
# --------------------------------------------------------------------------- #
class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    if os.name != "nt":
        raise RuntimeError("DPAPI 仅支持 Windows")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _BLOB()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = fn(
        ctypes.byref(blob_in),
        "campus-net" if protect else None,
        None, None, None,
        0x1 if protect else 0,  # CRYPTPROTECT_UI_FORBIDDEN
        ctypes.byref(blob_out),
    )
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def protect_password(plain: str) -> str:
    return base64.b64encode(_dpapi(plain.encode("utf-8"), True)).decode("ascii")


def unprotect_password(blob_b64: str) -> str:
    return _dpapi(base64.b64decode(blob_b64), False).decode("utf-8")


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def load_config(path: Path = CONFIG_PATH) -> dict:
    cfg = dict(DEFAULTS)
    if path.exists():
        try:
            cfg.update(json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"配置文件格式错误：{path} ({exc})")
    return cfg


def save_config(cfg: dict, path: Path = CONFIG_PATH) -> None:
    data = {k: v for k, v in cfg.items() if not k.startswith("_")}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def resolve_password(cfg: dict) -> str:
    env = os.environ.get("CAMPUS_PASSWORD")
    if env:
        return env
    if cfg.get("password_dpapi"):
        try:
            return unprotect_password(cfg["password_dpapi"])
        except Exception as exc:  # noqa: BLE001
            raise SystemExit(f"无法解密已保存的密码（换过 Windows 账户？）：{exc}。请重新执行 --setup")
    if cfg.get("password"):
        return cfg["password"]
    raise SystemExit("还没有配置密码，请先执行：python connect.py --setup")


# --------------------------------------------------------------------------- #
# 浏览器登录
# --------------------------------------------------------------------------- #
def _launch(playwright, channel: str, headless: bool):
    errors = []
    for ch in (channel, "msedge", "chrome", None):
        try:
            kwargs = {"headless": headless, "args": ["--no-first-run", "--no-default-browser-check"]}
            if ch:
                kwargs["channel"] = ch
            return playwright.chromium.launch(**kwargs)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{ch or 'bundled'}: {str(exc).splitlines()[0][:120]}")
            continue
    raise RuntimeError("无法启动浏览器 -> " + " | ".join(errors))


def _page_text(page) -> str:
    try:
        return page.inner_text("body", timeout=2000)
    except Exception:  # noqa: BLE001
        return ""


def _has_error(text: str) -> str | None:
    for kw in ERROR_KEYWORDS:
        if kw in text:
            return kw
    return None


def _click_connect_button(page) -> bool:
    for txt in CONNECT_BUTTON_TEXTS:
        try:
            loc = page.locator(f"button:has-text('{txt}'), a:has-text('{txt}')").first
            if loc.count() and loc.is_visible(timeout=800):
                loc.click(timeout=3000)
                log.info("点了一下门户上的「%s」按钮", txt)
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _ensure_agreement(page) -> bool:
    """勾选「请先阅读并同意《免责声明协议》…」。勾选框是异步渲染的，要等它出现。"""
    try:
        page.wait_for_selector("input.ant-checkbox-input", timeout=6000, state="attached")
    except Exception:  # noqa: BLE001
        return False
    try:
        box = page.locator("input.ant-checkbox-input").first
        if box.is_checked():
            return True
        page.locator(SEL_AGREE).first.click(timeout=4000)
        page.wait_for_timeout(300)
        ok = box.is_checked()
        log.debug("勾选协议：%s", "成功" if ok else "未生效")
        return ok
    except Exception as exc:  # noqa: BLE001
        log.debug("勾选协议失败：%s", exc)
        return False


def _dismiss_agreement_modal(page) -> bool:
    """点登录后可能弹出协议确认框，把它点掉。"""
    for txt in ("我已阅读并同意", "同意并继续", "同意", "确定"):
        try:
            btn = page.locator(f".ant-modal button:has-text('{txt}'), .ant-modal-footer button:has-text('{txt}')").first
            if btn.count() and btn.is_visible(timeout=600):
                btn.click(timeout=3000)
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _pick_operator(page, wanted: str = "") -> str:
    """处理「请选择服务」弹窗（截图确认：三个选项 div + 取消/确定）。

    选项在 app-service-select-modal 的 body 里，点击后高亮（active-bg），
    底部「确定」才提交（POST /eportal/network/setService）。返回选中的名字。
    """
    if not _has_service_ui(page):
        return ""
    log.info("检测到「请选择服务」弹窗")

    items = _service_items(page)
    names = [t for t, _ in items]
    log.info("可选服务：%s", " / ".join(names) if names else "（没读到列表）")

    # 选哪个：先精确等于配置名，再模糊包含，最后默认第一项
    target = None
    if wanted:
        for txt, handle in items:
            if txt == wanted:
                target = (txt, handle)
                break
        if target is None:
            for txt, handle in items:
                if wanted in txt or txt in wanted:
                    target = (txt, handle)
                    break
        if target is None:
            log.warning("配置的服务「%s」不在列表 %s 里，改选第一项", wanted, names)
    if target is None and items:
        target = items[0]
    if target is None:
        page.screenshot(path=str(LOG_DIR / "fail-service-empty.png"))
        return ""

    txt, handle = target
    try:
        handle.click(timeout=4000)
        page.wait_for_timeout(500)
        # 确认真的选中了（active-bg 高亮），没选中就再点一次 label 区域中心
        try:
            if "active-bg" not in (handle.get_attribute("class") or ""):
                handle.click(timeout=3000)
        except Exception:  # noqa: BLE001
            pass
        log.info("已选中服务：%s", txt)
    except Exception as exc:  # noqa: BLE001
        log.warning("点击服务失败：%s", exc)
        return ""

    # 点「确定」：真实 DOM 里在 div.footer，主按钮是 ant-btn-primary（排除「取消」）
    for ok_sel in (
        ".ant-modal-body div.footer button.ant-btn-primary",
        "app-service-select-modal div.footer button.ant-btn-primary",
        "div.footer button.ant-btn-primary",
        ".ant-modal-footer button.ant-btn-primary",
        "button.ant-btn-primary:has-text('确定')",
        "button:has-text('确定')",
    ):
        try:
            ok = page.locator(ok_sel).first
            if ok.count() and ok.is_visible(timeout=800):
                ok.click(timeout=4000)
                log.info("已确认服务：%s", txt)
                return txt
        except Exception:  # noqa: BLE001
            continue
    log.warning("没找到「确定」按钮，可能点选即生效")
    return txt


def _has_service_ui(page) -> bool:
    try:
        if "serviceSelection" in page.url:
            return True
        for sel in ("app-service-select-modal", "div.service-box", "div.footer button.ant-btn-primary"):
            if page.locator(sel).count():
                return True
        return False
    except Exception:  # noqa: BLE001
        return False


def _service_items(page):
    """返回 [(文本, locator)]，按页面顺序。

    真实 DOM：.ant-modal-body > .body > #relationInfo > div.service-box（内含 span.service），
    选中项的 class 带 active-bg。
    """
    out = []
    for sel in (
        ".ant-modal-body div.service-box",
        "#relationInfo div.service-box",
        "app-service-select-modal div.service-box",
        "div.service-box",
    ):
        try:
            loc = page.locator(sel)
            n = loc.count()
        except Exception:  # noqa: BLE001
            continue
        for i in range(n):
            try:
                el = loc.nth(i)
                if not el.is_visible(timeout=300):
                    continue
                txt = ""
                try:
                    txt = (el.locator("span.service").first.inner_text(timeout=500) or "").strip()
                except Exception:  # noqa: BLE001
                    txt = (el.inner_text(timeout=500) or "").strip()
                if txt and len(txt) <= 12 and "\n" not in txt and not any(txt == t for t, _ in out):
                    out.append((txt, el))
            except Exception:  # noqa: BLE001
                continue
        if out:
            break
    return out


def _wait_form(page, tries: int = 3, per_try_sec: int = 12) -> bool:
    """等登录表单出现；顺带识别「已在线」页面。"""
    for _ in range(tries):
        try:
            page.wait_for_selector(SEL_PASSWORD, timeout=per_try_sec * 1000, state="visible")
            return True
        except Exception:  # noqa: BLE001
            text = _page_text(page)
            if "已在线" in text or ("在线" in text and "下线" in text):
                return False
            page.wait_for_timeout(1500)
    return False


def login(
    cfg: dict,
    *,
    headful: bool = False,
    keep_open_sec: int = 0,
    online_check=check_online,
    stop_at_portal_page: bool = False,
    entry_url: str = "",
) -> tuple[bool, str]:
    """打开门户并登录。返回 (是否成功, 说明)。

    只在「初次检测为离线」时调用，因此这里把 online_check 通过当作认证成功的依据；
    同时优先识别门户的报错文案，避免把既有网络误判成登录成功。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False, "未安装 playwright，请执行：pip install playwright"

    username = (cfg.get("username") or "").strip()
    if not username:
        return False, "配置里没有账号，请先执行 --setup"
    password = resolve_password(cfg)

    base = cfg["portal_url"].rstrip("/")
    # 门户认证三步链路（实测确认）：
    #   /portal/entry/pc/authenticate    → 登录表单
    #   /portal/entry/pc/serviceSelection→ 选服务（电信/移动/联通）
    #   /portal/entry/pc/finish          → 登录成功
    ENTRY_AUTH = f"{base}/portal/entry/pc/authenticate;flowParams=undefined;from="
    ENTRY_SVC = f"{base}/portal/entry/pc/serviceSelection;flowParams=undefined;from=authenticate;sid=false"

    portal_url = entry_url or ENTRY_AUTH
    timeout_ms = int(cfg["login_timeout_sec"]) * 1000
    deadline = time.time() + timeout_ms / 1000
    shot_dir = LOG_DIR
    shot_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = _launch(p, cfg.get("browser_channel", "msedge"), cfg.get("headless", True) and not headful)
        ctx = browser.new_context(locale="zh-CN", viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        try:
            log.info("打开认证页 %s", portal_url)
            page.goto(portal_url, wait_until="domcontentloaded", timeout=45000)

            # 1) 等登录表单；抓不到就依次换入口
            form_ready = _wait_form(page, tries=2)
            if not form_ready:
                for cand in (ENTRY_AUTH, cfg["portal_url"], BAIT_URL):
                    log.info("没看到登录表单，换入口再试：%s", cand[:90])
                    try:
                        page.goto(cand, wait_until="domcontentloaded", timeout=25000)
                        log.info("落地：%s", page.url[:150])
                        if _wait_form(page, tries=2):
                            form_ready = True
                            break
                    except Exception as exc:  # noqa: BLE001
                        log.debug("换入口失败：%s", exc)
            if not form_ready:
                text = _page_text(page)[:400]
                page.screenshot(path=str(shot_dir / "fail-no-form.png"))
                return False, f"没找到登录表单（门户可能改版或已在线）。页面文字：{text!r}"

            # 2) 填账号密码
            page.locator(SEL_USERNAME).first.fill(username)
            page.locator(SEL_PASSWORD).first.fill(password)
            log.info("已填入账号 %s", username)

            # 3) 勾选协议（必须，否则点登录只会弹协议框）
            _ensure_agreement(page)

            # 4) 提交
            submit = page.locator(SEL_SUBMIT).first
            submit.wait_for(state="visible", timeout=8000)
            for _ in range(80):  # 等 Angular 表单校验放开按钮
                try:
                    if not submit.is_disabled():
                        break
                except Exception:  # noqa: BLE001
                    break
                page.wait_for_timeout(100)
            submit.click(timeout=8000)
            log.info("已提交登录")

            # 万一点击时协议还没勾上，会弹协议框而不是提交，这里补一次
            page.wait_for_timeout(2000)
            if _dismiss_agreement_modal(page) and "cas-sso/login" in page.url:
                log.info("出现协议确认框，已点同意并重新提交")
                _ensure_agreement(page)
                try:
                    submit.click(timeout=8000)
                except Exception as exc:  # noqa: BLE001
                    log.debug("重新提交失败：%s", exc)

            # 先等这一轮 POST 的响应落地，给门户时间把报错渲染出来
            try:
                page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:  # noqa: BLE001
                pass
            page.wait_for_timeout(1200)
            err_kw = _has_error(_page_text(page))
            if err_kw:
                page.screenshot(path=str(shot_dir / "fail-bad-credential.png"))
                return False, f"登录被拒绝（命中提示「{err_kw}」）。请核对账号密码；若出现验证码，请先手动登录一次。"

            # 5) 判定结果：按门户三步链路推进
            #    authenticate(登录) → serviceSelection(选服务) → finish(成功)
            clicked_extra = False
            fetched_service = 0
            operator_tries = 0
            success_url = ""
            err_kw = None
            last_url = None
            while time.time() < deadline:
                page.wait_for_timeout(700)
                online, detail = online_check(timeout=4)
                if online:
                    log.info("联网检测通过：%s", detail)
                    return True, f"登录成功，已联网（{detail}）"
                url = page.url
                if url != last_url:
                    log.info("页面跳转：%s", url[:160])
                    last_url = url

                # 第 2 步：选服务。这个门户每次认证都要重新选，允许重复处理（上限 3 次防死循环）
                if operator_tries < 3 and _has_service_ui(page):
                    operator_tries += 1
                    got = _pick_operator(page, (cfg.get("service_name") or "").strip())
                    if got:
                        log.info("第 %d 次选择服务：%s", operator_tries, got)
                        page.wait_for_timeout(3000)
                        continue

                # 没走到选服务页就主动跳过去（登录成功后会停在自助服务页等别处）
                if fetched_service < 2 and "/serviceSelection" not in url and "/finish" not in url:
                    if not _has_service_ui(page):
                        fetched_service += 1
                        log.info("主动进入选服务页（第 %d 次）：%s", fetched_service, ENTRY_SVC[:110])
                        try:
                            page.goto(ENTRY_SVC, wait_until="domcontentloaded", timeout=25000)
                            page.wait_for_timeout(4000)
                            landed = page.url
                            log.info("选服务页落地：%s", landed[:150])
                            if _has_service_ui(page):
                                log.info("选服务界面已出现")
                            else:
                                page.screenshot(path=str(shot_dir / f"service-miss-{fetched_service}.png"))
                        except Exception as exc:  # noqa: BLE001
                            log.debug("跳转选服务页失败：%s", exc)
                        continue

                if RE_SUCCESS_URL.search(url) and "cas-sso/login" not in url:
                    success_url = url
                    if not clicked_extra:
                        clicked_extra = True
                        _click_connect_button(page)
                    if stop_at_portal_page:
                        log.info("已进入门户页，账号密码校验通过")
                        return True, f"账号密码验证通过：已进入门户页（{url[:120]}）"
                    # 已经到门户页，继续等出口放行
                err_kw = _has_error(_page_text(page))
                if err_kw:
                    page.screenshot(path=str(shot_dir / "fail-bad-credential.png"))
                    return False, f"登录被拒绝（命中提示「{err_kw}」）。请核对账号密码；若出现验证码，请先手动登录一次。"

            page.screenshot(path=str(shot_dir / "fail-timeout.png"))
            if success_url:
                return False, f"已进入门户页但出口仍未放行（{success_url[:120]}），可能需要在门户里手动确认。"
            return False, "等待登录结果超时（页面没有报错，也没有联网成功）。截图见 logs/fail-timeout.png"
        finally:
            if keep_open_sec:
                try:
                    page.wait_for_timeout(keep_open_sec * 1000)
                except Exception:  # noqa: BLE001
                    pass
            ctx.close()
            browser.close()


# --------------------------------------------------------------------------- #
# 诊断
# --------------------------------------------------------------------------- #
def diagnose(cfg: dict, headful: bool = True) -> int:
    from playwright.sync_api import sync_playwright

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    out = {"portal_url": cfg["portal_url"], "when": datetime.now().isoformat(timespec="seconds")}
    with sync_playwright() as p:
        browser = _launch(p, cfg.get("browser_channel", "msedge"), not headful)
        page = browser.new_context(locale="zh-CN", viewport={"width": 1440, "height": 900}).new_page()
        page.goto(cfg["portal_url"], wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(6000)
        out["url"] = page.url
        out["title"] = page.title()
        out["text"] = _page_text(page)[:1500]
        out["inputs"] = page.eval_on_selector_all(
            "input",
            "els => els.map(e => ({type:e.type,name:e.name,id:e.id,ph:e.placeholder,"
            "cls:(e.className||'').toString().slice(0,60),vis:!!(e.offsetWidth||e.offsetHeight)}))",
        )
        out["buttons"] = page.eval_on_selector_all(
            "button, a[class*=btn], div[class*=btn]",
            "els => els.map(e => ({tag:e.tagName,txt:(e.innerText||'').trim().slice(0,24),"
            "cls:(e.className||'').toString().slice(0,70),vis:!!(e.offsetWidth||e.offsetHeight)}))"
            ".filter(x=>x.vis && x.txt)",
        )
        out["cookies"] = [c.get("name") for c in page.context.cookies()]
        page.screenshot(path=str(LOG_DIR / "diagnose.png"))
        browser.close()
    path = LOG_DIR / f"diagnose-{datetime.now():%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("诊断结果已写入 %s", path)
    print(json.dumps({"url": out["url"], "inputs": out["inputs"]}, ensure_ascii=False, indent=1))
    return 0


# --------------------------------------------------------------------------- #
# 连接主流程
# --------------------------------------------------------------------------- #
def _ip_state_file() -> Path:
    return LOG_DIR / ".last_ip"


def _read_last_ip() -> str:
    try:
        return _ip_state_file().read_text(encoding="utf-8").strip()
    except Exception:  # noqa: BLE001
        return ""


def _write_last_ip(ip: str) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        _ip_state_file().write_text(ip, encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _ip_changed(cfg: dict) -> str:
    """本次 IP 与上次记录不同则返回说明，否则返回空。

    只读不写——**必须等认证成功后才由调用方写入**（`_write_last_ip`）。
    否则一旦第一次认证失败，IP 已被记成新的，后续就不会再触发重认证了。
    """
    cur = _local_ipv4(cfg["portal_url"])
    if not cur:
        return ""
    prev = _read_last_ip()
    if prev and prev != cur:
        return f"{prev} → {cur}"
    return ""


def connect_once(cfg: dict, headful: bool = False, boot: bool = False) -> int:
    if cfg.get("require_ethernet", True) or boot:
        # 刚插上网线时网卡还没拿到 DHCP 地址，这里给一小段宽限期，别一次判死
        linked, info = physical_ethernet_up(portal_url=cfg["portal_url"])
        if not linked:
            grace = int(cfg.get("link_grace_sec", 12))
            log.info("暂未检测到可用网络地址，%d 秒内重试…", grace)
            deadline = time.time() + grace
            while time.time() < deadline:
                time.sleep(1)
                linked, info = physical_ethernet_up(portal_url=cfg["portal_url"])
                if linked:
                    break
        if not linked:
            log.info("没有插网线（或网卡未连通），本次不处理：%s", info or "无已连接的物理网卡")
            return 0
        log.info("以太网已连通：%s", info)

    if boot:
        wait_until_ready(cfg, int(cfg.get("wait_ready_sec", 40)))

    online, detail = check_online()
    if not online and boot:
        # 刚恢复链路时 DHCP/路由可能还没通，再给一两秒
        for _ in range(3):
            time.sleep(1)
            online, detail = check_online()
            if online:
                break

    # IP 变了说明 DHCP 重新分配过（休眠唤醒/换网口），旧认证会话多半已失效。
    # 这时即便探测"在线"也不可信，必须重新认证。
    ip_changed = _ip_changed(cfg)
    if ip_changed:
        log.info("检测到本机 IP 变化（%s），重新认证以恢复会话", ip_changed)
        online = False

    if online:
        # 确认在线时也把 IP 记下来，避免把"稳定在线"误判成下次的变化
        _write_last_ip(_local_ipv4(cfg["portal_url"]))
        log.info("已经在线（%s），无需登录", detail)
        return 0
    log.info("当前未联网（%s），准备登录", detail)

    if not portal_reachable(cfg["portal_url"], cfg.get("portal_http_timeout_sec", 5)):
        log.error("连不上门户 %s：确认已接上校园网（网线/WiFi）", cfg["portal_url"])
        return 3

    has_cred = bool((cfg.get("username") or "").strip()) and bool(
        cfg.get("password") or cfg.get("password_dpapi") or os.environ.get("CAMPUS_PASSWORD")
    )
    if not has_cred:
        log.error("还没配置账号密码，请先运行：python connect.py --setup")
        return 2

    attempts = max(1, int(cfg.get("retries", 2)))
    last_msg = ""
    for i in range(1, attempts + 1):
        log.info("第 %d/%d 次尝试", i, attempts)

        # ① 先走纯 HTTP（抓包确认的协议，秒级完成，不弹浏览器）
        try:
            ok, msg = http_login(cfg)
        except Exception as exc:  # noqa: BLE001
            ok, msg = False, f"HTTP 认证异常：{type(exc).__name__}: {exc}"
        log.info("HTTP 认证：%s", msg)
        if ok:
            online, detail = check_online()
            if online or http_user_online(cfg):
                # 认证确认成功，才把当前 IP 记为"已处理过"，
                # 失败时不写，下次触发仍能重新认证
                _write_last_ip(_local_ipv4(cfg["portal_url"]))
                log.info("联网检测通过：%s", detail or "userOnline=true")
                return 0
            log.warning("接口说成功但流量还没放行，等 5 秒再看")
            time.sleep(5)
            online, detail = check_online()
            if online:
                _write_last_ip(_local_ipv4(cfg["portal_url"]))
                log.info("联网检测通过：%s", detail)
                return 0
        last_msg = msg

        # ② 回退：浏览器方案（CAS 登录页填表单）
        log.info("改用浏览器方案")
        entry = find_portal_entry(cfg["portal_url"], cfg.get("portal_http_timeout_sec", 5))
        if not entry:
            log.info("没有探测到劫持跳转，直接使用门户地址 %s", cfg["portal_url"])
        ok, msg = login(cfg, headful=headful, entry_url=entry)
        log.info("%s", msg)
        if ok:
            _write_last_ip(_local_ipv4(cfg["portal_url"]))
            return 0
        last_msg = msg
        if "被拒绝" in msg or "没找到登录表单" in msg:
            break  # 凭据或页面问题，重试没有意义
        time.sleep(3)
    log.error("登录失败：%s", last_msg)
    return 1


def _ask(prompt: str, secret: bool = False) -> str:
    """input 的安全版：没有输入流（EOF）时返回空串而不是抛堆栈。"""
    try:
        return (getpass.getpass(prompt) if secret else input(prompt)).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


def do_setup(cfg: dict, args) -> int:
    if not getattr(args, "interactive", False):
        print("--setup 需要加 --interactive 在终端里交互输入（双击「改账号密码.bat」即可）。")
        print("或者直接编辑 config.json：填 username + service_name，并用 --plain 模式填 password。")
        return 2
    print("=== 校园网账号配置 ===")
    portal = _ask(f"门户地址 [{cfg['portal_url']}]: ")
    if portal:
        cfg["portal_url"] = portal
    username = _ask(f"账号（学号/工号）[{cfg.get('username') or ''}]: ")
    if username:
        cfg["username"] = username
    if not cfg.get("username"):
        print("账号不能为空，已取消。")
        return 2

    pwd = _ask("密码（输入时不显示）: ", secret=True)
    if not pwd:
        print("密码为空，已取消。")
        return 2
    confirm = _ask("再输一次: ", secret=True)
    if pwd != confirm:
        print("两次输入不一致，已取消。")
        return 2

    if args.plain:
        cfg["password"] = pwd
        cfg["password_dpapi"] = ""
        print("已按明文保存（不推荐，仅便于调试）")
    else:
        if os.name == "nt":
            cfg["password_dpapi"] = protect_password(pwd)
            cfg["password"] = ""
            print("已用 Windows DPAPI 加密保存（只有当前 Windows 账户能解密）")
        else:
            cfg["password"] = pwd
            print("非 Windows 平台，已明文保存")

    print()
    print("登录后如果要选运营商，填它的名字（写错也行，脚本会模糊匹配）")
    print("留空 = 自动选列表里的第一个")
    svc = _ask(f"运营商名称（如 校园网/电信）[{cfg.get('service_name') or ''}]: ")
    if svc:
        cfg["service_name"] = svc

    cfg["headless"] = not args.headful
    save_config(cfg)
    print(f"配置已写入 {CONFIG_PATH}")
    print("接下来可以执行：python connect.py --verify")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="校园网自动连接（锐捷 SAM+ / CAS-SSO 门户）")
    parser.add_argument("--setup", action="store_true", help="交互式配置账号密码")
    parser.add_argument("--interactive", action="store_true", help="配合 --setup，确认是在终端里手工运行")
    parser.add_argument("--verify", action="store_true", help="只检测当前是否已联网")
    parser.add_argument("--test-login", action="store_true", help="实际提交一次账号密码，验证凭据是否正确（不要求当前离线）")
    parser.add_argument("--watch", action="store_true", help="常驻运行，掉线自动重连")
    parser.add_argument("--headful", action="store_true", help="显示浏览器窗口")
    parser.add_argument("--boot", action="store_true", help="开机模式：先查网线，再等网络就绪，连上就退出")
    parser.add_argument("--diagnose", action="store_true", help="抓取登录页结构")
    parser.add_argument("--plain", action="store_true", help="配置时明文保存密码")
    parser.add_argument("--quiet", action="store_true", help="静默模式（计划任务用）")
    parser.add_argument("--verbose", action="store_true", help="输出调试日志")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    args = parser.parse_args(argv)

    # 输出被重定向（计划任务、管道）时统一切到 UTF-8，避免中文乱码
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

    setup_logging(args.quiet, args.verbose)
    cfg = load_config(args.config)

    if args.setup:
        return do_setup(cfg, args)

    if args.verify:
        online, detail = check_online()
        print(("已联网： " if online else "未联网： ") + detail)
        return 0 if online else 1

    if args.test_login:
        try:
            ok, msg = login(
                cfg,
                headful=args.headful,
                online_check=lambda timeout=4: (False, "test-login 模式：忽略当前联网状态"),
                stop_at_portal_page=True,
            )
        except SystemExit as exc:
            print(f"配置有问题：{exc}")
            return 2
        print(("账号密码验证通过 —— " if ok else "账号密码验证未通过 —— ") + msg)
        return 0 if ok else 1

    if args.diagnose:
        try:
            return diagnose(cfg, headful=True)
        except Exception as exc:  # noqa: BLE001
            log.error("诊断失败：%s", exc)
            return 2

    if args.watch:
        interval = max(10, int(cfg.get("check_interval_sec", 60)))
        log.info("守护模式启动，每 %d 秒检查一次（Ctrl+C 退出）", interval)
        while True:
            try:
                connect_once(cfg, headful=args.headful)
            except KeyboardInterrupt:
                return 0
            except Exception as exc:  # noqa: BLE001
                log.exception("本轮异常：%s", exc)
            time.sleep(interval)

    try:
        return connect_once(cfg, headful=args.headful, boot=args.boot)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
