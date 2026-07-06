# 玩椰 YEEHX · 觅影

**自然语言素材库 · 浏览 / 查找 / 调用系统**

本地运行的个人影视素材库：扫描硬盘 → 让素材被 AI 理解 → 用自然语言搜索 → 像 Finder 一样浏览 → 一键调用进剪辑或喂给 AI。

> **铁律**：原素材**只读**（绝不重命名/移动/删除/改写）；一切写入只落数据目录 `app/out/`；数据库可重建。
>
> **平台**：**macOS + Windows**。平台差异全部收口在 `app/core/osplat.py`（卷身份：mac diskutil UUID / Win 卷GUID；系统对话框：osascript / PowerShell；定位：Finder / 资源管理器）。R3D 的 REDline 解码依赖 RED 官方软件，装了就有。

---

## 怎么跑

**最简单**：mac 双击 `app/启动觅影.command`，Windows 双击 `app/启动觅影.bat`。第一次会自动建环境、装依赖（要等几分钟，只第一次慢）。服务转**后台运行**：浏览器自动打开后终端窗口自己关掉；依赖没变化时二次启动秒开；已在运行时双击直接开浏览器。

**停止 / 重启**：mac 双击 `app/停止觅影.command`，Windows 双击 `app/停止觅影.bat`（都按 PID/命令行验明正身精确停，连 MCP 自动拉起的实例一并识别，不误杀其他项目）；重启 = 先停止再启动。Windows 的启动逻辑在 `app/winlaunch.py`（bat 只负责找 Python 和建 venv）。

要求 **Python ≥ 3.10**（启动脚本会检查并提示；老 mac 自带 3.9 不行，Windows 没装 Python 会自动打开下载页引导）。

> 双击若被拦「无法验证开发者」：右键该文件 →「打开」→ 再「打开」，只需这一次。

**手动**（排错用）：

```bash
cd "仓库根目录"            # 含 app/ 的那一层
python3 -m venv app/.venv && source app/.venv/bin/activate
pip install -r app/requirements.txt        # 复现/发布环境用 requirements.lock.txt
python -m uvicorn app.main:app --port 8788
```

Windows 手动（排错用，PowerShell）：

```powershell
cd 仓库根目录
py -3 -m venv app\.venv; app\.venv\Scripts\Activate.ps1
pip install -r app\requirements.txt
python -m uvicorn app.main:app --port 8788
```

**AI 模型**：默认连本地 `ollama` 的 `qwen3.6:35b`（`http://localhost:11434/v1`）。
没装/想换：进「设置 → AI 模型」切到 API（OpenAI 兼容）或改本地模型名；「测试连接」只探活不保存，「保存」才落盘。
没有模型也能扫盘、浏览、套 LUT、搜索——只是不会自动打标（首次连不上模型时页面顶部会给引导条）。

**日志**：`app/out/app.log`（启动脚本自动落盘，关终端窗口也能回看；超 5MB 自动截断）。远程帮人排错先看页面左上角版本号 + 这份日志。

---

## 怎么用（五入口）

- **素材库**：像 Finder 一样浏览。打开文件夹会轻量登记当前层素材（指纹没变不重复登记）。选中后按需点「生图」「打标」「LUT」「标签」「下载」「导出」。**硬盘后来又加了素材？选中盘或文件夹点「同步」**——双向对账：新增入库+生图+只补未打标的，已消失文件清库记录；已打标的不重打，可放心反复点。
- **待复核**：AI 提出的新词先进这里，带置信度和理由。确认才加入标签库；同义词可直接并入别名。
- **标签库**：所有词都是同一种标签，类目只控制名称、颜色、排序。支持拖拽换类目、拖到另一个标签上合并、AI 整理建议。
- **精选库**：打过星的素材。
- **设置**：模型（本地/API 双卡片，点卡片切换，Key 遮挡显示，「获取列表」「测试连接」都不落盘）、类目、隐藏文件夹、LUT 预设、**重复素材检测**（补算指纹 → 查看重复 → 可忽略有意的备份组）、**手机/局域网访问**（开关+口令，见下）、**素材库体检**（离线卷/缺缩略图分原因/悬空引用/指纹失败/候选词/模型连通一屏看清，附 清理悬空引用·重试指纹失败·补在线缩略图 三个修复动作）、**标签库备份**（导出 JSON 或含参考图 zip；导入支持合并/替换；合并·瘦身·删词前自动备份到 `out/backups` 滚动 10 份）、页面底部**危险区**（清空缩略图 / 清空标签库 / 重置数据库——统一输入 `YEEHX` 确认，先预览影响范围，任务运行时拒绝，词表类操作强制先备份，全部写审计日志）。

**工具栏**全部按钮有悬停说明；「清除」可把选中素材/文件夹的打标数据（含手动标签）洗掉重来，可选连缩略图一起清，锁定🔒素材跳过；「锁定」弹框里也能**解除锁定**，配合左侧「已锁定」分面能筛出库里全部锁定素材。「导出」「下载」支持直接选文件夹（自动展开其下全部素材）。

**键盘与鼠标**（素材网格 / 大图）：

| 操作 | 效果 |
|---|---|
| 点缩略图 | 全屏大图（lightbox）；←→ 翻页，Esc 关闭 |
| 点卡片标题 | 详情抽屉（备注/标签/星级/「在 Finder 中显示」） |
| `1-5` / `0` | 给选中素材或当前大图打星 / 取消 |
| 方向键 | 网格里移动光标；空格选中；回车看大图 |
| Shift + 勾选 | 范围多选 |
| 下载（多选） | 批量抽代表帧打 zip（≤40 个） |

**搜索**：顶部搜索框 + 左侧分面（含「已锁定」筛选）；结果支持排序切换（默认/最新/最早/星级/大小/名称）和无限滚动。

**AI 找素材 / 指令式导出**（金边输入框）：一句话描述，可带范围和动作——如"**在 02_视频 里**找武当山雪景航拍空镜，**导出到桌面武当山备选**"。AI 解析成 范围+标签组合+关键词（+导出提议），先显示搜索结果和"AI 理解"，导出必须经确认弹框（显示命中数、方式、目标路径，可改可选）才执行。结果页可继续追问收窄。要调用模型，慢几秒属正常，Esc 取消。

> **AI 动作白名单**：只有 搜索（只读）和 导出（=复制到目标文件夹）两个动作。AI 层不存在任何能移动/删除/重命名/改写原始素材的通道——导出底层只有 copy/重渲染到目标目录，这是觅影的铁律。

**导出**：复制原素材 / 图片帧（视频取与缩略图同一帧）/ 清单。清单格式：JSON（给 AI）、CSV、TXT 路径、Premiere（txt 路径清单——Premiere 不认 fcpxml）、FCPXML（Final Cut Pro）。导出目标限 `/Volumes` 下的盘和用户目录（路径白名单）。

**手机（同 WiFi）**：设置页打开「手机/局域网访问」→ 用页面给出的带口令地址在手机浏览器打开（首次打开记住 30 天，可随时换口令）。手机版聚焦 搜索 / AI 找 / 浏览 / 大图 / 打星 / 改描述，左下「筛选」按钮呼出标签面板；建库和批量操作请在电脑上做。改动开关后需先「停止」再「启动」生效；设置页会实测监听状态，没生效会黄字提醒（含 macOS 防火墙放行提示）。

---

## 安全边界

- 原素材**只读**；删素材=删库记录、忽略=库内状态、导出=复制，都不碰源文件；人工锁定项（🔒）永不被自动逻辑覆盖；**数据库可重建，原素材不受影响**。
- 盘的身份认 **卷 UUID/指纹 + 盘内相对路径**：外接盘改名/换口/挂载点变都不怕；**盘没挂载绝不判为删除**（只显示离线）。
- **改名/移动不丢标签**：素材有内容指纹（content_id）。文件改名/移动后，重新快扫或「同步」它的新位置，旧记录（标签/星级/缩略图）会自动接管到新路径。快扫还会顺手清掉本夹已消失文件的记录——但有人工痕迹（标签/星级/锁定/备注）的记录会保留，等搬家识别接管；确认真删除了就用「同步」清。「同步」内部先扫描后清理（串行），搬家识别永远先于清理执行。
- **数据安全三件事**：① API key 明文存在 `app/out/model_settings.json`（已 gitignore，但请知悉，别把 out/ 发给别人）；② 服务默认只绑 `127.0.0.1`，「手机/局域网访问」是唯一对外口子——必须显式开启，开启后局域网请求必须带访问口令（首次 `?token=` 落 cookie），Host 必须是私网地址（挡 DNS rebinding），浏览/参考图/导出另有路径白名单（仅 `/Volumes` 和用户目录），陌生网络环境建议关闭；③ 危险区操作（清缩略图/清标签库/重置库）统一 `YEEHX` 短语确认 + 任务运行时拒绝 + 词表类强制先备份 + 审计行进日志。

---

## 模块速览（`app/`）

```
config.py          配置（模型/LUT/阈值），单例 cfg
db.py              SQLite：全表 + FTS5（中文按字切分）+ 旧库就地迁移
core/              ids · files(分类/指纹) · volumes(卷身份) · folders(继承)
                   assets(素材记录/搬家rehome) · inheritance(批量重算生效索引)
                   candidates(待复核) · tag_merges(合并建议) · search(排序/分块)
media/             frames(抽帧/RAW/HEIC兜底) · luts · color · metadata + thumbnails
ai/                vision(支持 mock) · prompt · source_rules
tasks/             五队列任务管理器（并发/持久化/关服恢复/暂停不卡队列）
scan/              scanner(快扫/补指纹/清理) · tagging(素材级 AI 打标)
export/            导出（图片/原素材/清单 JSON·CSV·TXT·FCPXML·Premiere）
main.py            FastAPI 全部路由 + 静态服务 + 访问守卫/白名单/体检/危险区
core/tag_io.py     标签库 导出/导入/自动备份/一键清空
mcp_server.py      MCP 接入（搜/预览/原片/导出/状态/Finder 弹出，自动拉起服务）
mcp_run.sh         Hermes 等外部 Agent 的 MCP 启动壳（依赖自愈，输出走 stderr）
hermes/miying/     Hermes 技能 SKILL.md（装到 ~/.hermes/skills/media/miying/）
web/index.html     界面（CSS 变量主题 + 五入口 + lightbox + 任务条 + 手机适配）
tests/             63 个测试（YEEHX_MOCK=1，不连模型不碰真实数据）
```

## MCP / 微信（Hermes）

觅影自带 MCP server（stdio）：`miying_search / preview / get_original / export / status / reveal` 六个工具，全是薄壳调本地 HTTP API——觅影没开会自动拉起。超过微信上限（默认 950MB，`MIYING_WECHAT_LIMIT_MB` 可调）的视频自动转 1080p 代理片，缓存 20GB 上限按 LRU 淘汰。
Hermes 接入：MCP 的 command 指向 `app/mcp_run.sh`；配套技能 `app/hermes/miying/SKILL.md` 复制到 `~/.hermes/skills/media/miying/`（让模型遇到"觅影/找素材"时走 MCP 工具而不是自己写脚本）。细节见 `app/Hermes接入觅影.md`。

## 接口分组（UI 用 / Agent 用）

**UI 用**：`/api/fs` `/api/search` `/api/facets` `/api/tags` `/api/apply` `/api/candidates*` `/api/tasks*` `/api/settings*` `/api/luts*` `/api/export` `/api/asset/{id}`（详情/打分/备注/下载/reveal）`/api/duplicates*` `/api/assets/download_zip` 等。

**Agent 用（UI 不调用，给外部 AI/脚本，删改前注意）**：`/api/health`、`GET /api/ping`、`POST /api/asset/{id}/tags`、`/api/asset/{id}/lock`、`/api/asset/{id}/fine_tag`、`/api/folder/{id}` 系列、`/api/path/hide`、`/api/scan/cleanup`、`/api/tag/{id}/alias`、`/api/tag/{id}/ref_file`、`/api/tasks/cancel_all`、`DELETE /api/lut/{name}`、`/api/db/reset`。

典型 Agent 流：`POST /api/search`（自然语言+分面）→ 拿结构化清单 → `POST /api/export`（JSON/路径清单）→ 喂给剪辑或下游 AI。

## 测试

```bash
YEEHX_MOCK=1 python3 -m pytest app/tests -q
```

63 个测试：`test_smoke` 主流程冒烟（扫描→生图→打标→搜索→继承→导出→重复检测→改名保标签→AI搜索→清除打标→锁定筛选）+ `test_danger` 危险区 + `test_phase0/1/23` 一期优化回归（备份导入还原、搜索水合新旧一致、延迟指纹搬家合并、白名单 403、局域网判定、FTS 降级、打标退避重试、重置竞态与泵存活）。mock 模式不连模型、数据目录隔离。改完代码跑一遍，全绿再用。
