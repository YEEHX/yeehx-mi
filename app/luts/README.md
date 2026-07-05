# luts/ — 官方还原 LUT 放这里（发布包不含，需自行下载）

觅影用还原 LUT 把 log 灰片的缩略图渲染回正常色彩（**只影响预览缩略图，绝不修改原片**）。

**出于版权原因，觅影的发布包不包含任何厂商 LUT 文件**——`.cube` 属各厂商版权资产，请到官方渠道免费下载，放进本文件夹（或在 设置 → LUT 预设 → 「选择 .cube 新增」导入），仅用于个人学习与自有素材的预览用途。

## 推荐配齐的官方 LUT

| 放这里的文件名 | 用途 | 官方下载 |
|---|---|---|
| `dji_dlog_709.cube` | DJI D-Log → Rec.709 | DJI 官网下载中心 → 对应机型页 → "D-Log to Rec.709" LUT |
| `dji_dlogm_709.cube` | DJI D-Log M → Rec.709（Mavic 3 及之后新机型） | 同上 |
| `sony_slog3_709.cube` | Sony S-Log3 → Rec.709 | Sony 专业支持站，常见文件名 `From_SLog3SGamut3.CineToLC-709.cube` |
| `nikon_nlog_709.cube` | RED Log3G10 → Rec.709（映射名为历史遗留） | RED 官网 Downloads → IPP2 LUT 包 |

文件名对应关系在 `app/config.yaml` 的 `luts:` 里，可自行改路径；不想动配置就直接在设置页导入，随导随用。

## 你大概率已经有这些

做后期的话，达芬奇 / FCP 里多半装过官方 LUT。Mac 上常见位置：
`/Library/Application Support/Blackmagic Design/DaVinci Resolve/LUT/`，把对应 `.cube` 拷过来改名即可。

## RED（R3D）素材说明

预览 / 抽帧 R3D 原片依赖 **RED 官方 REDline 工具**（随 REDCINE-X PRO 安装）。请到 RED 官网下载安装并遵守其许可条款；未安装时 R3D 素材无法生成缩略图（不影响登记与搜索）。觅影不附带、不分发 RED 的任何软件组件。

> 声明：觅影与 DJI、Sony、RED 均无关联；上述商标归各自权利人所有。请仅将官方资源用于其许可允许的用途。
