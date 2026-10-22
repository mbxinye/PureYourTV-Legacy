# CleanYourTV Legacy：老电视去广告 + 换 Projectivy 桌面

上游 [CleanYourTV](https://github.com/Gathin23/cleanyourtv) 的桌面要求 Android 8.0（API 26），
小米电视 3S 这类 Android 6.0.1（API 23）的机器会被 `pm install` 直接拒绝（`INSTALL_FAILED_OLDER_SDK`）。
这个项目只做一件事：**保留 CleanYourTV 的去广告工具，桌面换成 Projectivy Launcher（最低 API 23）**，
让 2016 年前后的小米/Android TV 也能清干净并换一个没有广告的桌面。

已删除的内容：上游桌面源码、主题、插件、200 多个机型清单、CI 工作流和全部宣传/贡献类文档。
仓库里只剩下方案需要的四样：

| 路径 | 作用 |
|---|---|
| [tools/](tools/) | ADB 工具与停用/恢复脚本（`tv.py`、`disable.ps1`、`restore.ps1`、`check.py`、`privacy.py`、`adb-closed.py`） |
| [projectivy/install.py](projectivy/install.py) | Projectivy 安装器：下载官方 APK、校验哈希、安装、启动、接管 Home，失败自动回滚 |
| [lists/](lists/) | 停用清单（`lists/xiaomi-mi-tv-3s.txt` 是候选清单，未实机验证） |
| [tests/](tests/) | 用假电视跑的自测，改脚本后运行 |

## 一、准备环境（Windows PowerShell）

需要 Python 3（3.8 以上，`python --version` 检查；没装就去 python.org 安装时勾选 Add to PATH）。
在仓库根目录执行：

```powershell
cd <你把仓库放在哪里的路径>\CLeanYourTV-Legacy
python -m venv tools\venv
tools\venv\Scripts\python.exe -m pip install -r tools\requirements.txt
```

后面所有命令都用 `tools\venv\Scripts\python.exe`，不需要激活虚拟环境。
如果 PowerShell 拒绝运行 `.ps1` 脚本，先执行
`Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`。

Linux/macOS 的对应写法：`tools/venv/bin/python`，环境变量用 `export TV_HOST=...`。

## 二、连上电视

在电视的「设置 → 账号与安全 → 调试」里打开 **ADB 调试 / 网络调试**（有的固件叫「开发者选项 → USB 调试」
并需要在同一局域网），然后：

```powershell
$env:TV_HOST = "192.168.1.50"          # 换成你电视的局域网地址
tools\venv\Scripts\python.exe -I tools\tv.py --probe
```

`--probe` 会打印型号、Android 版本和固件。第一次连接时电视可能弹出「允许 USB 调试」，勾选允许。
看到 `192.168.1.50:5555` 拒绝连接，说明 ADB 没开或电视不在同一网段。

## 三、备份当前状态

改动之前先留一份现场，出问题时才有对照：

```powershell
mkdir work\backup -Force
tools\venv\Scripts\python.exe -I tools\tv.py 'pm list packages' | Out-File -Encoding utf8 work\backup\packages.txt
tools\venv\Scripts\python.exe -I tools\tv.py 'pm list packages -d' | Out-File -Encoding utf8 work\backup\disabled.txt
tools\venv\Scripts\python.exe -I tools\tv.py 'getprop' | Out-File -Encoding utf8 work\backup\getprop.txt
```

`work/` 已被 git 忽略。备份里有固件名和序列号，不要提交、不要公开。

## 四、停用广告组件

清单是一行一个包名，`#` 后面写原因。[lists/xiaomi-mi-tv-3s.txt](lists/xiaomi-mi-tv-3s.txt)
是针对小米电视 3S 的候选清单，**默认只放开三个纯广告组件，其余都注释掉了**。
建议先复制一份自己的：

```powershell
mkdir work\my-tv -Force
copy lists\xiaomi-mi-tv-3s.txt work\my-tv\debloat.txt
notepad work\my-tv\debloat.txt        # 取消注释要停的包，或删掉这台电视上没有的那行

tools\disable.ps1 -TvHost 192.168.1.50 work\my-tv\debloat.txt
```

脚本的行为：只用 `pm disable-user --user 0`，**从不卸载**；停用前把电视当时的状态写进
`~/.cleanyourtv/`（Windows 是 `C:\Users\<你>\.cleanyourtv\`），`restore.ps1` 靠它恢复。
它会拒绝停用 Android 核心组件（权限控制器、安装器、SystemUI 等），没有绕过开关。

想知道电视上哪些包涉及语音、投屏、埋点，可以先做一次只读审计：

```powershell
tools\venv\Scripts\python.exe tools\privacy.py
```

审计只列证据：谁申请了麦克风、哪些是固件自己点名的组件、哪些只是包名对上。它**不能证明**某个应用在
监听或上传，`--write` 生成的候选清单同样要自己核对后再用。

撤销整个停用：

```powershell
tools\restore.ps1 -TvHost 192.168.1.50 work\my-tv\debloat.txt -Stock com.mitv.tvhome
```

Android 6 的注意点：`restore` 里那条 `cmd package set-home-activity` 在 API 23 上不存在，会打印一行
错误然后继续，包名照常恢复；Home 归属用下一步的安装器 `--rollback` 或直接按遥控器 Home 键处理。

## 五、安装 Projectivy 并接管 Home

Android 6 没有 `cmd package`，也没有 `pm set-home-activity`，不能从命令行指定默认桌面。
所以接管顺序是：**安装 → 启动 → 确认 Projectivy 真的在前台 → 停用原生桌面 → 按 Home → 再确认**。
任何一步不成立，脚本都会在退出前把原生桌面重新启用。

```powershell
tools\venv\Scripts\python.exe projectivy\install.py --check         # 只读，什么都不改
tools\venv\Scripts\python.exe projectivy\install.py --install-only  # 只安装并启动，不动 Home
# 用遥控器在电视的应用列表里打开 Projectivy，走完它的欢迎/权限页面
tools\venv\Scripts\python.exe projectivy\install.py                # 安装 + 启动 + 接管 Home
```

APK 从 `projectivy/releases.json` 里钉死的官方地址下载，并校验大小和 SHA-256；
本项目**不包含也不重新分发 APK**（Projectivy 的许可证不允许）。GitHub 下载不通时，自己把官方 APK
存到本机再加参数：

```powershell
tools\venv\Scripts\python.exe projectivy\install.py --apk "C:\Users\<你的用户名>\Downloads\ProjectivyLauncher-4.72-c97-xda-release.apk"
```

原生桌面包名默认从电视推断（`dumpsys activity | grep mHomeProcess`）。推断不出来时脚本会停下来，
让你显式指定，不要猜：

```powershell
tools\venv\Scripts\python.exe projectivy\install.py --stock-launcher com.mitv.tvhome
tools\venv\Scripts\python.exe projectivy\install.py --install-only --stock-launcher none   # 完全不动 Home
```

如果提示 Projectivy「没有出现在前台」，通常是它的权限弹窗还挡着：在电视上点完，再跑一次；
此时原生桌面仍是启用状态，没有被停用。

接管完成后**重启一次电视**，再检查一次：

```powershell
tools\venv\Scripts\python.exe projectivy\install.py --check
```

然后用**真实遥控器**试 Home、返回、信号源、音量、关机。脚本注入的按键不能证明厂商按键可用。
确认没问题后再关掉 ADB：

```powershell
tools\venv\Scripts\python.exe tools\adb-closed.py 192.168.1.50
```

退出码 0 表示 5555 已关闭。5555 是明文端口，日常不要一直开着。

## 六、出问题怎么回去

```powershell
# 把原生桌面重新启用（Projectivy 留在电视上，不卸载）
tools\venv\Scripts\python.exe projectivy\install.py --rollback --stock-launcher com.mitv.tvhome

# 没有记录、但 ADB 还能连时的手工恢复
tools\venv\Scripts\python.exe -I tools\tv.py 'pm enable --user 0 com.mitv.tvhome'
tools\venv\Scripts\python.exe -I tools\tv.py 'input keyevent KEYCODE_HOME'
```

接管中途失败时，安装器会把上面这几条命令写到 `~/.cleanyourtv/projectivy-home-<电视地址>.txt`。
它仍然依赖 ADB 可用，所以**接管验证通过之前不要把 ADB 关掉，也不要急着重启**。

系统更新可能把停用的包重新打开：

```powershell
tools\venv\Scripts\python.exe tools\check.py work\my-tv\debloat.txt com.spocky.projengmenu
tools\venv\Scripts\python.exe tools\check.py --fix work\my-tv\debloat.txt com.spocky.projengmenu
```

## 七、常见现象

- **`INSTALL_FAILED_OLDER_SDK`**：说明装的还是要求 API 26 的上游桌面。本项目只装 Projectivy。
- **`INSTALL_FAILED_VERIFICATION_FAILURE`**：国行固件的安装校验拒绝了侧载，是电视的限制，不是 APK 坏了。
  在电视设置里允许「安装未知应用」后再试；仍然拒绝就停手，不要绕过校验器。
- **Home 停在一个占位桌面/黑屏**：原生桌面被停用而 Projectivy 没接管成功。用 `--rollback`；
  新版小米固件（Android 9+）有 HOME 白名单，第三方桌面可能被禁止接管，Android 6 上未确认存在该限制。
- **重启后 ADB 断开**：小米电视常见。从设置里重新打开 ADB 调试再连。
- **截图是全黑或空**：Live TV、HDMI、Netflix 的画面受保护，按 Home 回桌面再截；这不代表连接坏了。
- **电视没有某个包名**：正常。清单里的名字先 `pm list packages | findstr mitv` 核对，没有就删掉那行。

## 八、安全规则（脚本已强制，别绕过）

1. 只停用，永不卸载；不动 `adb enabled` 之类的连接开关。
2. 停用前先备份，保留 `~/.cleanyourtv/` 里的记录和你的清单文件。
3. Android 核心包（`com.android.*` 权限控制器/安装器/SystemUI/shell/settings 等）任何清单都不允许包含。
4. 原生桌面只有在 Projectivy 被确认接管 Home 之后才停用；失败自动回滚。
5. HDMI、频道调谐、输入源菜单、遥控器与蓝牙配对、输入法、设置本体默认保留。
6. 家人在用的应用（视频、语音助手、投屏、固件更新）先问再停；固件更新器能留就留。
7. 不 root、不刷机、不使用来路不明的 APK；`minSdk` 不是本项目要改的东西。

## 许可

本仓库沿用上游 CleanYourTV 的 [MIT 许可](LICENSE)。Projectivy Launcher 是独立项目，
许可证不允许再分发，因此这里只钉住官方下载地址与哈希，由安装器在你机器上下载校验。
