# VBHelper

> **V**ibe + **H**elper —— 氛围编程帮助者

常驻 Windows 桌面的悬浮球。点开是项目目录树，再按需调用依赖体检、项目索引、
AI 工具总线这些开发动作。单文件 EXE，不装运行库，不内置浏览器内核。

`Windows 10 19045+` · `Python 3.10+` · `Apache-2.0` · `单文件 EXE`

简体中文 | [English](README.en.md)

![VBHelper](vbhelper/assets/vbhelper_256.png)

---

## 目录

- [为什么做这个](#为什么做这个)
- [功能](#功能)
- [交互](#交互)
- [架构](#架构)
- [环境](#环境)
- [安装与运行](#安装与运行)
- [构建](#构建)
- [测试](#测试)
- [目录结构](#目录结构)
- [配置与数据](#配置与数据)
- [设计约束](#设计约束)
- [已知限制](#已知限制)
- [常见问题](#常见问题)
- [免责声明](#免责声明)
- [许可](#许可)

---

## 为什么做这个

AI 编程工具的文件浏览器普遍偏弱：看不到目录结构、切项目要重开、想改一个文件得先找路径。
VBHelper 的做法是把「项目上下文」做成一个常驻的悬浮球：

- 桌面常驻，不占任务栏；
- 点开即当前项目的完整目录树，后台扫描、按需展开；
- 目录、文件、空白的右键菜单直接给出针对当前路径的动作；
- 常用开发能力（依赖体检、项目索引、API 站点、AI 工具总线）收在同一个菜单里，不另开一堆窗口。

它不替代 IDE，也不接管编辑：只处理「我现在要看/改/查这个路径」这一类动作。

## 功能

### 文件与导航

| 能力 | 说明 |
|---|---|
| 目录树 | 悬浮球展开为文件树；后台线程扫描，展开时不阻塞；目录 mtime 变化自动失效缓存 |
| 标记与注释 | 给目录/文件打颜色标记、写备注，存项目内 `.vbhelper.json`；项目不可写时回退全局配置 |
| 快捷修改 | 文本 / Hex / PE / JAR / Class / Markdown 六类查看器；大文件走后台加载与 mmap，超过 2MB 的文本分页只读 |
| 快速搜索 | 文件名索引 + 文件内容全文搜索，结果直接回车打开（面板内 `Ctrl+P`） |
| Git 状态 | 目录树按 porcelain 状态给文件着色，可暂存 / 取消暂存 / 提交 |

### 开发工具

| 能力 | 说明 |
|---|---|
| 依赖医生 | 解析 `requirements*.txt`、`pyproject.toml`、`Pipfile`、`setup.cfg`、`package.json` + lock、`pom.xml`、`build.gradle[.kts]`、`libs.versions.toml`、`go.mod`；查 PyPI / npm / Maven Central / Go proxy 最新版本，经 OSV 查漏洞，输出升级命令与 Markdown / JSON 报告 |
| API 工作台 | 管理 API 站点：Base URL、密钥、连通测试、余额查询；密钥经 Windows DPAPI 加密，界面只显示打码值；可一键写入 Claude Code 配置 |
| 项目 RAG | 静默多线程索引项目，SQLite FTS5（unicode61）+ trigram 双索引；中文短词走 trigram 兜底；检索结果带 `相对路径:起止行`，供 AI 精确读取 |
| 提示词工作台 | 128 套工程类提示词模板（架构、重构、审查、逆向、性能、框架），支持分类、搜索、占位符填写、导入导出 |

### AI 协作

| 能力 | 说明 |
|---|---|
| MCP 工具总线 | 向 AI 客户端开放 18 个本机工具：截屏、鼠标键盘、联网搜索、网页正文提取、剪贴板、RAG 检索、项目读取、系统信息、通知 |
| 传输与鉴权 | 只绑 `127.0.0.1`，随机端口 + Bearer 令牌（DPAPI 存储），常量时间比较；支持 HTTP 直连与 stdio 桥两种接入 |
| 安全模型 | 危险工具默认关闭；鼠标键盘按「每次确认 / 会话授权 / 永不确认」三档策略；`Ctrl+Alt+Shift+Esc` 全局急停；所有调用写审计表并按规则脱敏 |
| 自动恢复 | stdio 桥发现主程序未运行时，以无窗口方式拉起 GUI 并等待服务就绪，最多 15 秒 |

### 桌面与系统

| 能力 | 说明 |
|---|---|
| 音乐播放器 | 本地曲库、在线搜索（音源脚本由用户提供）、LRC 歌词、迷你播放条；FLAC / M4A 经 ffmpeg 转码后播放 |
| 网络工具 | DNS 延迟优选、一键切换、DoH / DoT 测速与证书信息、ECH 检测、hosts 编辑（需管理员） |
| HUD | 常驻桌面的 CPU / 内存 / 磁盘 / 网络 / GPU / 温度仪表盘；三态背景，可拖动、可锁定 |
| 主题 | 12 套预设 + 自定义主色，切换即时生效，无需重启 |

## 交互

| 动作 | 结果 |
|---|---|
| 拖动悬浮球 | 自由移动；松手后靠近屏幕边缘自动吸附 |
| 单击悬浮球 | 展开 / 收起目录树面板 |
| 右键目录 | 展开、下钻、建立 RAG 索引、打开终端、快捷启动 |
| 右键文件 | 快捷修改、用提示词工作台打开、复制路径 |
| 右键空白 | 刷新、在资源管理器中打开、快捷启动 |
| 面板 `Ctrl+P` | 快速搜索 |
| 面板 `Esc` | 收起面板 |

悬浮球与面板都可锁定位置，锁定后不响应拖动。

## 架构

```text
run.py
  └─ app.py  VBHelper(FloatingMixin, MetaMixin, ScanMixin, TreeMixin)
       ├─ AppContext          配置 / 路径 / 主题 / 任务 / HTTP / 数据库 / 窗口 / 功能注册表
       ├─ ui/                 DPI 缩放、主题令牌、PIL 渲染、控件、虚拟列表、异步、窗口壳
       ├─ store/              SQLite（WAL）、HTTP 条件缓存、DPAPI 密钥库、审计
       ├─ services/           Git、RAG、网页提取与搜索、截屏、输入、剪贴板
       └─ features/           deps / apiwork / mcp / rag，按开关加载
```

**线程模型**：Tk 只在主线程；网络、磁盘、子进程一律走 `TaskRunner`，结果经队列回主线程。
每个后台任务有 `key` 和 `group`，关窗或退出时按组取消。

**功能协议**：每个功能实现 `Feature`（`mount` / `open` / `shutdown`），由 `FeatureRegistry`
按 `config.features` 决定是否加载。关闭的功能不 import 模块、不建窗口、不监听端口。

**依赖边界**：运行期只有 Pillow、psutil、certifi 三个第三方包。HTTP 用标准库，
JSON-RPC 自研，Markdown 自研，OCR / 模型 / 浏览器一律外接，不塞进 EXE。

## 环境

- Windows 10 19045+ / Windows 11
- Python 3.10+（源码运行）
- 运行期依赖：Pillow ≥ 10、psutil ≥ 5.9、certifi
- 可选外部组件：
  - `git.exe` —— Git 状态、暂存、提交
  - `ffmpeg` —— FLAC / M4A 转码播放
  - `node.exe` —— 在线音源脚本宿主

## 安装与运行

```bash
python -m pip install -r requirements.txt
python run.py
```

首次启动会在 `%APPDATA%\VBHelper\config.json` 生成默认配置。托盘右键退出。

## 构建

```bash
python -m pip install "pyinstaller>=6.21"
python -m PyInstaller --clean VBHelper.spec
```

产物 `dist\VBHelper.exe`，单文件，约 19 MB，冷启动 3–6 秒。

构建配置在 `VBHelper.spec`，不要用命令行参数重新生成它：

- `hiddenimports` 列出按需加载的功能包，漏项会让功能在 EXE 里静默消失；
- `icon` 与资源清单也写在 spec 里，`--specpath` 会把整个文件覆盖成默认模板。

发布快照（不含开发历史）由 `tools/release.py` 生成：

```bash
python tools/release.py --tag v0.1.0
```

## 测试

不依赖 pytest，全部是可直接运行的脚本，放在 `tools/checks/`：

```bash
python tools/checks/_store_check.py       # 存储、DPAPI、审计（111 项）
python tools/checks/_rag_check.py         # 索引、分块、检索（31 项）
python tools/checks/_ctx_check.py         # AppContext 与功能注册表
python tools/checks/_ui_widgets_check.py  # 主题令牌与控件
python tools/checks/_music_check.py       # 播放链路与布局审计（50 项）
python tools/checks/_mcp_check.py         # MCP 端到端（含 stdio 桥）
python tools/checks/_qa_exe_smoke.py dist\VBHelper.exe   # EXE 冒烟
```

提交前至少跑 `python -m compileall vbhelper` 与对应模块的检查脚本。

## 目录结构

```text
vbhelper/
  app.py            应用主类：启动顺序、托盘、悬浮球
  context.py        AppContext：配置、路径、任务、HTTP、窗口、功能注册表
  core.py           常量、Win32 基础封装、配置读写
  floating.py       悬浮球与主面板交互
  tree.py           目录树渲染与展开
  scan.py           扫描线程池与缓存
  meta.py           标记与注释持久化
  editor.py         快捷修改窗口
  music*.py         播放器、在线接口、音源运行时、AES
  promptlab.py     提示词工作台
  nettools.py       网络工具
  hud.py            常驻仪表盘
  mcp_stdio.py      MCP stdio 桥
  ui/               主题、缩放、渲染、控件、虚拟列表、异步、窗口壳
  store/            SQLite、缓存、密钥库、审计
  services/         Git、RAG、网页、截屏、输入、剪贴板
  features/         按开关加载的功能模块
  viewers/          Hex / PE / JAR / Class / Markdown 查看器
tools/
  checks/           自检与冒烟脚本
  release.py        发布快照
  make_icon.py      图标生成
docs/
  dev/              开发约定（注释规范）
```

## 配置与数据

| 路径 | 内容 |
|---|---|
| `%APPDATA%\VBHelper\config.json` | 主题、功能开关、音源、窗口几何、最近目录 |
| `%APPDATA%\VBHelper\debug.log` | 诊断日志（设置里打开后才写） |
| `%LOCALAPPDATA%\VBHelper\db\vbhelper.db` | HTTP 缓存、审计、RAG 索引 |
| `%LOCALAPPDATA%\VBHelper\mcp\endpoint.json` | MCP 端点与令牌，仅服务运行期间存在 |
| 项目内 `.vbhelper.json` | 该项目的标记与注释 |

密钥不写明文：`store/secrets.py` 用 `CryptProtectData` 加密后存 base64，
绑定当前 Windows 用户，换机器无法解密。

## 设计约束

- 单文件 EXE，不内置浏览器内核、大模型、驱动
- 运行期第三方依赖只有三个
- 界面全部 Tk 绘制，视觉靠 PIL 超采样与分层窗口，不引入 GUI 框架
- 网络 / 磁盘 / 子进程不进主线程
- 新增功能必须可开关，关闭后不加载模块、不注册资源

## 已知限制

- 仅 Windows。主题与控件依赖 Win32 分层窗口与 DWM
- 在线音乐播放需要用户自行提供音源脚本，仓库不附带、不托管任何音源或音频内容
- FLAC / M4A 播放依赖系统安装 `ffmpeg`
- 修改 DNS、编辑 hosts 需要管理员权限，入口处会提示提权
- Git 功能依赖系统 `git.exe`，缺失时相关入口显示不可用
- 全局快捷键尚未实现，`Ctrl+P` 仅在面板获得焦点时生效

## 常见问题

**悬浮球不见了？**
托盘图标右键「显示主面板」，或在任务管理器结束进程后重启。

**打包后菜单缺项？**
`VBHelper.spec` 的 `hiddenimports` 漏了对应功能包。PyInstaller 只做静态分析，
`__import__` 拼出来的模块名它看不到。

**MCP 客户端连不上？**
先在面板启动服务，确认 `%LOCALAPPDATA%\VBHelper\mcp\endpoint.json` 存在；
stdio 桥会在主程序未运行时自动拉起，最多等 15 秒。

**播放 FLAC 没声音？**
安装 `ffmpeg` 并加入 `PATH`，播放器会先转成 320k mp3 缓存再播。

## 免责声明

本项目用于技术学习与开发效率场景。音乐模块不提供、不分发、不托管任何音频数据；
在线播放所需的音源脚本由使用者自行提供，其合法性与准确性由使用者负责。
项目与酷我、酷狗、QQ 音乐、网易云音乐、咪咕等平台无任何关联，相关名称与商标归各自所有者。
请在遵守当地法律法规的前提下使用。

## 许可

Apache License 2.0，见 [LICENSE](LICENSE)。第三方组件与参考实现的归属见 [NOTICE](NOTICE)。
