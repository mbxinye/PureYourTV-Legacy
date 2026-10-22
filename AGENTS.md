# AGENTS.md

在这个仓库里工作的助手请先读这里。给人类的说明在 [README.md](README.md)。

## 这个项目是什么

CleanYourTV 的**去广告工具**加上 **Projectivy 桌面**，用来支持 Android 6.0/7.x（API 23–25）的老电视，
目标机型是小米电视 3S（Android 6.0.1，MIUI TV）。上游要求 API 26 的桌面源码、主题、插件、机型清单和
CI 都已经删掉了，不要再把它们带回来。

| 路径 | 内容 |
|---|---|
| `tools/` | `tv.py`（ADB-over-TCP 客户端）、`b64push.py`、`disable.sh`/`disable.ps1`、`restore.sh`/`restore.ps1`、`recovery.py`、`check.py`、`privacy.py`、`adb-closed.py` |
| `projectivy/` | `install.py` 换桌面安装器，`releases.json` 钉住官方 APK 的 URL/大小/SHA-256 |
| `lists/` | 停用清单。`xiaomi-mi-tv-3s.txt` 是**未实机验证的候选清单**，不要当成已验证配置发布 |
| `tests/` | `python -m unittest discover -s tests`，全部用假电视，不需要真机 |

## 在 Windows 上运行

用户的机器是 Windows + PowerShell，命令一律按这个写法给：

```powershell
python -m venv tools\venv
tools\venv\Scripts\python.exe -m pip install -r tools\requirements.txt
$env:TV_HOST = "192.168.1.50"
tools\venv\Scripts\python.exe -I tools\tv.py --probe
tools\venv\Scripts\python.exe projectivy\install.py --check
```

不要给 `tools/venv/bin/...` 这种 POSIX 路径；bash 脚本只在 Git Bash 里有效，默认交给 `.ps1`。

## 真机操作的规矩（不可协商）

1. 只 `pm disable-user --user 0`，永不 `pm uninstall`；不 root、不刷机。
2. 改动前备份包列表、停用列表、`getprop`；保留 `~/.cleanyourtv/` 里的恢复记录。
3. Android 核心包不允许进清单（权限控制器、安装器、ext services/shared、module metadata、SystemUI、
   shell、settings、`android`），脚本会拒绝，没有绕过开关。
4. Home 保护：Projectivy 安装→启动→确认在前台，才允许停用原生桌面；任何一步不成立就回滚。
   Android 6 没有 `cmd package`，这一点由 `projectivy/install.py` 处理，不要手写新流程。
5. HDMI/调谐/输入源/遥控器与蓝牙/输入法/设置本体/Play 服务默认保留；家人在用的应用先问。
6. 一次停一小批，每批之间截图确认。Live TV、HDMI、Netflix 的截图是黑的，那不是故障。
7. 电视拒绝时（侧载校验失败、每个包都拒绝停用）停手报告，不要重试刷循环，不要降低 `minSdk`，
   不使用别处编译的 APK。
8. 文档和截图里不出现真实 IP、序列号、账号、Wi-Fi 名；示例统一用 `192.168.1.50`。

## 改动之前

- 改 `tools/` 或 `projectivy/` 的脚本：跑 `python -m unittest discover -s tests -v`。
  套件里少数与 PowerShell/路径有关的失败在干净的 HEAD 上同样存在，改动不要**新增**失败。
- 文档只有 `README.md`（中文，面向使用者）和这个文件（面向助手）。不要再开新文档目录。
- 提交标题用 `area: what changed`，例如 `tools: keep Projectivy as the expected Home in check.py`。
