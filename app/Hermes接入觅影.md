# Hermes 接入觅影 —— 在微信里远程查素材、收图、收片、批量导出

链路：**微信 → Hermes（跑在这台 Mac）→ 觅影 MCP → 觅影本体**
觅影不用提前开着——MCP 层每次干活前探活，没开就自动拉起（日志照旧进 `app/out/app.log`）。

---

## 一、装 Hermes 并接上微信（一次性）

按官方 [快速开始](https://www.majiabin.com/hermes/getting-started/quickstart)（[GitHub](https://github.com/NousResearch/hermes-agent)）装好 Hermes，然后：

```bash
pip install aiohttp cryptography qrcode   # 微信通道依赖
hermes gateway setup                      # 选 Weixin → 手机微信扫码 → 确认登录
```

扫码成功后凭证自动存到 `~/.hermes/weixin/accounts/`，不用手动配 token。

## 二、把觅影挂进 Hermes（一次性）

编辑 `~/.hermes/config.yaml`，加：

```yaml
mcp_servers:
  miying:
    command: "/Users/你的用户名/Dev/YEEHX-Mi/app/mcp_run.sh"
    args: []
    allowed_tools:
      - miying_search
      - miying_preview
      - miying_get_original
      - miying_export
      - miying_status
      - miying_reveal
```

重启 `hermes gateway`，在 CLI 里敲 `/tools` 能看到 `mcp-miying-*` 就通了。
（首次连接时 `mcp_run.sh` 会自动把 fastmcp 装进 `app/.venv`，慢十几秒，仅此一次。）

### 图形界面版（设置 → MCP → 新服务器）

名称填 `miying`，服务器 JSON 粘贴：

```json
{
  "type": "stdio",
  "command": "/bin/bash",
  "args": ["/Users/你的用户名/Dev/YEEHX-Mi/app/mcp_run.sh"]
}
```

保存后点「重新加载 MCP」。如果报格式错误，把 `"type": "stdio",` 这行删掉再试（不同版本字段要求不一样）。
用 `/bin/bash` 包一层是为了避开路径里的空格和脚本执行权限两个坑。

## 三、微信侧锁门（强烈建议，先做再用）

微信那头等于能操作素材库和导出文件，必须白名单只认你自己。`~/.hermes/.env`：

```bash
WEIXIN_DM_POLICY=allowlist
WEIXIN_ALLOWED_USERS=你的user_id     # 先随便发条消息，网关日志里能看到你的 user_id
# 群聊默认 disabled，保持别动
```

## 四、怎么用（微信里直接说人话）

| 你说 | 发生什么 |
| --- | --- |
| 找一段雨中鹦鹉洲长江大桥的航拍 | 回文件名列表（含位置、标签） |
| 发我第 1、3 条的预览图 | 收到代表帧图片 |
| 把第 1 条原片发我 | ≤950MB 发原片；超了自动转 1080p 代理片再发，并附原片路径 |
| 把所有夜景航拍拷到 /Users/你的用户名/Desktop/周五用 | 原片复制到该文件夹（只复制，不动原素材） |
| 觅影现在什么状态 | 版本、打标/导出进度、待确认候选词 |

搜索优先走觅影的 AI 指令解析（本地模型，懂你的标签库）；模型没起来会自动退回关键词检索并注明。

## 五、自动化

Hermes 自带定时任务，例如在 Hermes 里说：
「每天早上 9 点检查觅影打标进度，有失败任务就微信告诉我」——它会定时调 `miying_status`。
导出类同理：「每周五晚把本周新进的航拍素材拷到 /Volumes/交付盘/本周」。

## 六、工具清单（MCP 暴露给 Hermes 的全部能力）

| 工具 | 干什么 | 写入风险 |
| --- | --- | --- |
| miying_search | 自然语言搜素材 | 无（只读） |
| miying_preview | 出代表帧 JPG 用于发图 | 无（写到 out/mcp/preview） |
| miying_get_original | 给原片路径；超限自动转代理片 | 无（代理写到 out/mcp/proxy，原片不动） |
| miying_export | 把命中素材复制到指定文件夹 | 仅向目标文件夹写入副本 |
| miying_status | 服务/队列状态 | 无 |
| miying_reveal | 在访达中弹出选中原文件（人在电脑前用） | 无（只读） |

对原始素材**零写入**，全链路最重的操作就是「复制」。

## 七、可调参数（配在 mcp_servers.miying.env 下）

```yaml
    env:
      YEEHX_MI_PORT: "8788"            # 觅影端口，跟启动觅影.command 保持一致
      MIYING_WECHAT_LIMIT_MB: "950"    # 超过这个体积就转代理片（微信上限 1GB，留余量）
```

## 八、注意

- **Mac 别睡死**：系统设置 → 电池/能源 → 关闭「自动进入睡眠」（合盖另说），或常驻 `caffeinate -s`。Mac 睡了整条链路都断。
- 觅影手动开着也不冲突：MCP 探活同一端口，活着就直接用。
- 代理片/预览图缓存在 `app/out/mcp/`，占地方了随时整个删掉。
- 离线卷（硬盘没挂）：搜索照样命中记录，但取原片/导出会明确报「卷离线」。
- 转码耗时：4K 长片转 1080p 要几分钟，微信里 Hermes 会一直「正在输入」，等它。

## 九、排错

| 现象 | 看哪 |
| --- | --- |
| /tools 里没有 miying | `hermes doctor`；确认 config.yaml 路径正确、`mcp_run.sh` 有执行权限（`chmod +x`） |
| 提示「没找到 app/.venv」 | 先双击跑一次 `启动觅影.command` 装环境 |
| 「觅影启动失败」 | `app/out/app.log` 最后几十行 |
| 微信不回消息 | `hermes gateway` 是否在跑；白名单是否把自己挡了 |
