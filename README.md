# 自我信息插件 (Self Identity)

为 MaiBot 提供 Bot 自我信息检索、人设图库浏览、自我人设图片返回，以及自己的 QQ 头像获取能力。

## 插件信息

- 版本：`1.0.0`
- 分类：聊天
- 图标：镜子
- 插件 ID：`valleywinds.self-identity`
- 适配要求：MaiBot（Host）`1.3.0` 及以上，插件 SDK `2.x`
- 开源协议：GPL-3.0-or-later

## 功能

- `search_self_infomation`：按关键词、标题或标签检索 Bot 的自我信息，例如人设、外貌、穿搭、主题元素等。
- `view_all_image`：分页浏览人设图库缩略图，每页最多返回 10 张。
- `get_self_image`：按图片序号、文件名或图片 ID 返回指定人设原图。
- `get_self_avatar`：读取主配置中的 `bot.qq_account`，通过 QQ 公开头像接口获取并返回 Bot 自己的头像。

## 安装

1. 将本仓库克隆或下载到 MaiBot 的 `plugins` 目录中。
2. 确认已安装 MaiBot 插件 SDK 2.x。
3. 在 WebUI 或 `config.toml` 中按需要修改自我信息与人设图库配置。
4. 重启 MaiBot 或重新加载插件。

> 本插件与旧版 `sengokucola.self-identity-plugin` 是两个独立插件（ID 不同）。如果之前安装过旧版，请先卸载旧版再安装本插件，避免功能重复。

## 配置

默认配置位于 `config.toml`。

- `plugin.enabled`：是否启用插件。
- `plugin.config_version`：插件配置版本，当前为 `1.0.0`。
- `identity_image.image_dir`：人设原图目录，支持插件目录相对路径或绝对路径。
- `identity_image.thumbnail_dir`：人设图缩略图目录，支持插件目录相对路径或绝对路径。
- `search.default_limit`：自我信息检索默认返回条数。
- `infos`：Bot 自我信息列表，每项包含标题、关键词和完整说明。

`get_self_avatar` 依赖主程序配置中的 `bot.qq_account`。如果该字段为空或为 `0`，工具会返回失败提示，不会尝试下载头像。

## 人设图库

默认人设原图目录为 `self_image`，缩略图目录为 `image_thumbup`。插件加载时会自动检查目录，并为支持的图片生成缩略图。

支持的图片格式包括：`.jpg`、`.jpeg`、`.png`、`.webp`、`.gif`、`.bmp`。

## 许可证

本项目以 [GPL-3.0-or-later](LICENSE)（GNU 通用公共许可证第 3 版或更新版本）发布。

## 致谢

本项目基于 [SengokuCola/self_identity_plugin](https://github.com/SengokuCola/self_identity_plugin)（上游已停止维护）修改而来，感谢原作者 [SengokuCola](https://github.com/SengokuCola) 的原始工作。
