# CUIT 校园网自动连接（成都信息工程大学 · 锐捷 SAM+ / CAS-SSO 门户）

掉线后自动完成认证并验证真的能上网。**纯 HTTP 实现，一次认证约 0.3 秒，不弹浏览器。**

> **适用对象：成都信息工程大学（CUIT）** 的校园网，门户为锐捷 SAM+ 5.x / CAS-SSO 架构
> （门户地址 `10.254.241.66`）。运营商支持**移动 / 电信 / 联通**。
>
> 其他采用同款锐捷 SAM+ 5.x / CAS-SSO 门户的学校大概率也能用，
> 换学校通常只需改 `config.json` 里的 `portal_url` 和 `service_name`，详见「换学校怎么改」。

## 它做什么

```
开机 / 唤醒 / 插网线  →  检测有没有网线
                      →  已在线？→ 1 秒内退出，不打扰
                      →  未在线？→ 自动登录 → 选运营商 → 联网成功即退出
```

跑完就结束，**不常驻、不留后台进程**。

## 真实认证链路（Burp 抓包实测确认）

```
1. GET  /eportal/index.jsp?wlanuserip=<本机IP>&wlanacname=H3C-CR16010-F
        → 302 Location: /portal/portal-main?sessionId=xxx&userIp=...&nasIp=...
        ← 拿到 sessionId 和 JSESSIONID

2. GET  /cas-sso/login?flowSessionId=<sessionId>&nasIp=...&userIp=...
        ← 从 HTML 里 <div id="login-page-flowkey"> 取 execution

3. POST /cas-sso/login?flowSessionId=<sessionId>&...
        username / password / type=UsernamePassword / _eventId=submit / execution
        → 302 Location: /portal/assets/auth-success.html?ticket=ST-xxxx
        ← 账号已绑定到该 sessionId

4. POST /eportal/network/serviceLogin   {"sessionId":"xxx","service":"cmcc"}
        → {"data":{"authResult":"success"}}          ← 这一步才真正放行网络

5. POST /eportal/network/userOnline     {"sessionId":"xxx"}
        → {"data":{"online":true}}
```

关键点：**第 4 步不做，CAS 登录成功也上不了网**（只进了自助服务）。这是最容易踩的坑。

`service` 用运营商代码：**移动 `cmcc`、电信 `telecom`、联通 `unicom`**（脚本会自动转换中文名）。

## 依赖

**只需要 Python 3.9 以上。不用 `pip install` 任何东西。**

装完 Python 就能直接用，全程只用系统自带的 Edge，不需要额外下载浏览器内核。

> 唯一的可选依赖：如果哪天门户改版导致 HTTP 认证失败，脚本会自动尝试浏览器方案，
> 那时才需要 `pip install playwright`。平时用不到。

## 三步用起来

**第 1 步：双击 `改账号密码.bat`**

按提示依次输入：
- 门户地址（直接回车用默认的）
- 你的学号
- 你的密码（输入时不显示，输两遍）
- 你的运营商（`移动` / `电信` / `联通`）

**第 2 步：双击 `立即连接一次.bat`**

看到 `已联网` 就说明配置对了。如果失败，双击 `查看日志.bat` 看原因。

**第 3 步：装成计划任务（可选，但推荐）**

在项目目录里右键「在终端中打开」，然后执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\install-task.ps1
```

装完后：开机、休眠唤醒、插网线时都会自动连一次，连上就退出，**不用管它**。

> 想卸载：`powershell -ExecutionPolicy Bypass -File .\uninstall-task.ps1`

双击就能用的入口：

| 文件 | 作用 |
| --- | --- |
| `改账号密码.bat` | 交互式改账号 / 密码 / 运营商 |
| `立即连接一次.bat` | 手动连一次 |
| `查看日志.bat` | 看最近 40 行日志 |

## 换学校怎么改

只改 `config.json` 三个值（或者用「改账号密码.bat」交互式改）：

```jsonc
{
  "portal_url": "http://10.254.241.66/",  // ← 改成你们学校的门户地址
  "username": "你的学号",
  "service_name": "移动"                   // ← 电信 / 移动 / 联通
  // 密码用「改账号密码.bat」录入，不落明文
}
```

`service_name` 写中文关键字就行，脚本会映射成 `cmcc` / `telecom` / `unicom`。

如果门户地址不同，还有两处可能需要调整（在 `connect.py` 里）：

| 位置 | 说明 |
| --- | --- |
| `http_login()` 的 `wlanacname=H3C-CR16010-F` | 接入设备名，换了设备厂商可能要改 |
| `/eportal/network/serviceLogin` 的接口路径 | 如果对方是更老/更新的版本，用浏览器回退模式也能work |

**不确定的话**：先用 `python connect.py --headful` 跑一次，日志会显示每一步落在哪个页面，
再加 `--diagnose` 抓一次页面结构。实在不行，浏览器回退模式（Playwright）是通用兜底。

## 什么时候会自动跑

计划任务 `CampusNet-AutoConnect` 挂三个触发器，动作统一是 `connect.py --quiet --boot`：

| 触发场景 | 延迟 | 说明 |
| --- | --- | --- |
| 开机 / 登录 Windows | 3 秒 | 冷启动 |
| 休眠、睡眠唤醒 | 3 秒 | 监听 `Power-Troubleshooter` 事件 1 |
| 网络变为「已连接」 | 2 秒 | 监听 `NetworkProfile` 事件 10000，插网线/切换网络都会触发 |

执行顺序：

1. 查有没有插好的物理网线（WiFi、虚拟网卡不算），没有就直接结束；
   刚插网线时网卡可能还没拿到 DHCP 地址，会在 12 秒内重试而不是一次判死；
2. 已在线 → 0.4 秒内退出；未在线 → HTTP 认证 → 联网成功即退出。

任务勾选了 `MultipleInstances=IgnoreNew`，多个触发器同时命中也不会叠出多个进程。

## 常用命令

| 命令 | 作用 |
| --- | --- |
| `python connect.py` | 连一次；已在线则立即退出 |
| `python connect.py --verify` | 只检测是否已联网，退出码 0=在线 / 1=离线 |
| `python connect.py --watch` | 常驻守护，每 60 秒检查，掉线重连 |
| `python connect.py --headful` | 显示浏览器窗口（排查问题用） |
| `python connect.py --diagnose` | 抓取登录页结构到 `logs/diagnose-*.json` |
| `python connect.py --setup --plain` | 明文保存密码（不推荐） |
| `powershell -File .\install-task.ps1 -IntervalMinutes 5` | 额外每 5 分钟自检 |
| `powershell -File .\uninstall-task.ps1` | 删除计划任务 |

退出码：`0` 已联网/登录成功，`1` 登录失败，`2` 配置或环境问题，`3` 门户不可达（大概率没接上校园网）。

## 配置项（`config.json`）

| 字段 | 说明 |
| --- | --- |
| `portal_url` | 门户地址 |
| `username` / `password_dpapi` | `--setup` 自动写入；`password` 用于明文模式 |
| `service_name` | 运营商：`移动` / `电信` / `联通` |
| `require_ethernet` | `true` 时没插网线就直接跳过 |
| `wait_ready_sec` | 开机后等网络就绪的最长秒数，默认 8 |
| `link_grace_sec` | 插网线后等待 DHCP 的宽限秒数，默认 12 |
| `headless` | `true` 后台静默；`false` 弹浏览器窗口 |
| `browser_channel` | `msedge` / `chrome`，失败自动回退 |
| `login_timeout_sec` | 等登录结果的秒数，默认 75 |
| `retries` | 失败重试次数 |

环境变量 `CAMPUS_PASSWORD` 可临时覆盖密码（不落盘）。

## 日志与排错

- 日志：`logs\connect.log`（UTF-8，超 2MB 自动轮转）
- 失败截图：`logs\fail-*.png`
- 页面结构快照：`logs\diagnose-*.json`

| 现象 | 处理 |
| --- | --- |
| `没找到登录表单` | 门户改版，跑 `--diagnose` 后改 `connect.py` 顶部的 `SEL_*` 选择器 |
| `认证未通过` | 账号密码错误，或运营商选错（看日志里的 `service=`） |
| `连不上门户` | 不在校园网内 |
| `无法解密已保存的密码` | 换了 Windows 账户/电脑，重新「改账号密码.bat」 |

## 测试

```powershell
python -m unittest discover -s tests -v                        # Python 单元测试
powershell -File .\tests\detect-campus-ethernet.tests.ps1      # 网卡检测测试
```

## 安全说明

- 密码用 Windows DPAPI 加密存在 `config.json`，**只有这台电脑的这个 Windows 账户能解密**。
- 防的是"配置文件被人翻到"，**不防**能登录 Windows 账户的人。
- `config.json` 已在 `.gitignore` 里，不会误传。
- 请只在自己的设备、自己的账号上使用。

## 适用说明

| 项目 | 说明 |
| --- | --- |
| 学校 | 成都信息工程大学（CUIT，Chengdu University of Information Technology） |
| 门户厂商 | 锐捷网络 Ruijie，SAM+ 5.x 统一身份认证 |
| 认证架构 | ePortal + CAS-SSO（`cas-sso` 平台） |
| 门户地址 | `http://10.254.241.66/` |
| 运营商 | 移动 `cmcc` / 电信 `telecom` / 联通 `unicom` |
| 实测环境 | Windows 10，Python 3.13，Microsoft Edge |

本项目的认证链路是通过 **Burp Suite 抓包**逆向后实现的，不含任何破解行为——
所有请求都是你在浏览器里手动登录时会发出的正常请求，只是自动化了而已。

首次实现时间：2026 年 10 月。如果学校后来升级了门户，`connect.py` 顶部的
`SEL_*` 选择器和 `http_login()` 里的接口路径可能需要同步调整。

## 许可证

[MIT License](LICENSE) —— 可自由使用、修改、分发，保留版权声明即可。

## 致谢

- 认证链路的抓包分析来自实际使用中的反向工程
- 浏览器回退方案基于 [Playwright](https://playwright.dev/)

