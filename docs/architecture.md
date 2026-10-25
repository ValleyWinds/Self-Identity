# 架构说明

> 本文档面向贡献者与 AI 协作者；用户向的安装、配置说明见仓库根 README。

## 概述

Self-Identity 是单文件 MaiBot 插件（`plugin.py`，入口 `create_plugin()`），为 Bot 提供三类自我信息能力：

- **自我信息检索**：从配置的 `infos` 列表里按标题 / 关键词 / 通用搜索词打分检索
- **人设图库**：扫描 `self_image/` 目录，自动生成缩略图，分页浏览与按名取原图
- **自我头像**：读宿主配置 `bot.qq_account`，从 QQ 头像服务下载高清头像

能力声明（manifest `capabilities`）只有 `config.get`——除头像工具读取 `bot.qq_account` 外，全部逻辑本地完成，不经 RPC。

## 组件清单

| 工具 | 用途 | 关键行为 |
|------|------|---------|
| `search_self_information` | 检索自我信息 | 打分排序，`limit` 缺省取配置 `search.default_limit` |
| `view_all_image` | 分页浏览图库缩略图 | 每页 10 张，返回 index/name/id 供取原图 |
| `get_self_image` | 取指定人设原图 | 按 index/name/id 解析；超 5MB 拒绝整张返回并引导用缩略图 |
| `get_self_avatar` | 取 Bot 的 QQ 头像 | 未配置 QQ 号时返回明确的失败提示 |

所有工具统一返回 `{"success": bool, "content": str, ...}`；图片类工具把图片放 `content_items`（base64 + MIME + 元数据）。

## 数据流

### 自我信息检索

`config.infos_section.infos`（内存配置）→ `_score_info_item` 按 title 精确/包含/模糊（`SequenceMatcher`）、keyword 精确/包含/模糊、query 命中三层加权打分 → 正分项排序取前 N。纯内存计算，无 IO。

### 人设图库

`self_image/` 目录扫描（按文件名字典序）→ `_build_self_image_id`（文件名+路径的 SHA1 前 12 位）作为稳定 ID → 缩略图按「缩略图 mtime ≥ 原图 mtime」跳过重新生成（512px LANCZOS PNG，写 `image_thumbup/`）。图库目录可配置（`identity_image` 节），支持绝对路径（图库存数据盘场景）；相对路径必须落在插件目录内。

### 自我头像

`ctx.config.get("bot.qq_account")` → 校验非空、非 `0`、纯数字 → `https://q1.qlogo.cn/g?b=qq&nk=<QQ>&s=640` 下载（`asyncio.to_thread` 包同步 urlopen）→ base64 返回。下载在 `asyncio.to_thread` 里跑，不阻塞事件循环。

## 安全防御

- 图片下载上限 15MB、本地读取上限 5MB（`_MAX_DOWNLOAD_IMAGE_BYTES` / `_MAX_LOCAL_IMAGE_BYTES`）
- 解码前像素上限 4000 万（`_MAX_IMAGE_PIXELS`，防压缩炸弹）
- 下载要求 `Content-Type: image/*`
- 配置的相对目录越出插件目录（如 `../../`）时回退默认目录，绝不跟随
- 缩略图文件名对 stem 做白名单替换（`[^0-9A-Za-z_.-]` → `_`）

## 配置模型约定

- 四节结构：`plugin`（仅 `config_version`）/ `identity_image` / `search` / `infos_section`
- **`[plugin].enabled` 是宿主保留键，配置模型不声明它**——宿主拿它做激活门禁与 WebUI 顶部启停开关，直接读写磁盘；模型声明了反而会在配置页多出一个同键冗余开关（1.4.5 移除，见 CHANGELOG）
- `infos` 新装默认带一条示范条目，用户按自己 Bot 的人设改写；存量配置不触发重建（`config_version` 不动）
- `infos` 必须放在命名节 `infos_section` 内：根级字段会被 WebUI 折进 general 虚拟节，前端按 `config[节名][字段名]` 取值，根级列表在配置表单里取不到值

## 测试

`tests/` 脱离宿主可跑（真实 SDK、假 RPC 注入点）：

```
uv run --project <本地MaiBot根> python -m pytest tests -q
```

本地 MaiBot 实例 venv 里 SDK / pillow / pytest 全有；没有实例时装 `maibot-plugin-sdk` + `pillow` + `pytest` 即可。

## 版本与兼容

- `host_application` 上界锁当前实测的 1.x 小版本线（如 `1.3.99`）：MaiBot 进入新小线时插件会被挡，这是有意的刹车——实测兼容后再抬界发新版
- Release Tag 与 manifest `version` 必须一致；插件 `id` 不可变更
