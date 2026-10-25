# AGENTS.md — 给在这个插件仓库工作的 AI

这是一个 MaiBot 插件仓库（Plugin SDK 2.x / Host 1.3.x），插件 `valleywinds.self-identity`。任何 AI 助手在本仓库工作前先读完本文件。

## 权威参考（遇到疑问按此顺序查）

1. **本仓库 `docs/architecture.md`**：模块结构、数据流、安全防御、设计取舍——改代码前先读它，改完同步它。
2. **SDK / MaiBot 主程序源码**：行为疑问的唯一真相（上游仓库或本地源码副本）。
3. **maibot-plugin-dev 技能**（https://github.com/ValleyWinds/maibot-plugin-dev ）：SDK 代理签名速查、组件规范、常见陷阱、发布收录规则——已逐行对照源码核对，**用作加速器而非最终答案**：它是快照，可能落后于你的环境；若其基线低于当前 SDK / Host 版本、或所述与源码冲突，一律以源码为准。

## 开发规矩

- **配置**：`config.toml` 是首次运行按配置模型自动生成的运行时产物，**不进 git**（`.gitignore` 已排除）；实机配置含人设文本，绝不提交。`[plugin].enabled` 是宿主保留键，配置模型里不要声明它（声明了配置页会多一个冗余开关）。
- **版本区间**：`host_application` 上界锁已实测的次版本线（如 `1.3.99`）、`sdk` 上界锁 `2.99.99`，都不要写 `999.999.999`。MaiBot 出新小线后先实测再抬界发新版。
- **插件 `id` 发布后不得变更**；manifest 新增字段前先核对 SDK 的必填/禁写字段清单（manifest v2 是 `extra="forbid"`，写错字段直接拒载）。
- **测试**：`tests/` 脱离宿主可跑（真实 SDK、假 RPC 注入点）：`uv run --project <本地MaiBot根> python -m pytest tests -q`，无实例时装 `maibot-plugin-sdk` + `pillow` + `pytest`。改了 `plugin.py` 必须跑一遍全绿。
- **文档一律简体中文**；README 归用户，`docs/` 归贡献者，别混。

## 发布规矩

- **固定流程**：改代码 → 同步 `CHANGELOG.md` → 更新 `_manifest.json` 的 `version`（严格三段式）→ commit → 打同版本 Tag（`v1.2.3`）→ 基于 Tag 发 GitHub Release，**标题用纯版本号**，预发布勾 Prerelease 不改版本号格式。**push 与发布 Release 前需经仓库所有者确认**。
- **Release Notes 只写用户能感知的更新要点**：一个功能一行；版本号提升与依赖更新不用写；SDK 版本、内部机制等维护者信息一律不进。写完同步 `CHANGELOG.md`（manifest `changelog` 字段指向它）。
- **README 面向用户**，安装指引主路径写「WebUI 插件市场一键安装」；不要写 SDK 安装步骤（SDK 由主程序自动提供）。
- push 前过一遍防泄漏自查：`.env`、密钥、带用户名的本机路径、`data/`、`config.toml`、人设图片不得进仓库。

## 红线（违反即错，无例外）

- **LICENSE 与原作者署名一条不能删**（GPL-3.0-or-later + 上游 MIT 声明见 `LICENSE.upstream`）；更换协议必须遵守原协议规则。
- **不得让用户修改 MaiBot 主程序源码来使用本插件**；确需主程序改动，先向官方提 issue 请求。
- 已收录的 Tag 不改写；修复一律发新版本号。
