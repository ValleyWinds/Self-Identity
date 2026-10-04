# 自我信息插件 (Self Identity)

为 MaiBot 提供 Bot 自我信息检索、人设图库浏览、自我人设图片返回，以及自己的 QQ 头像获取能力。

## 功能

- **自我信息检索**：有人问起 Bot 的名字、人设、外貌、穿搭等设定信息时，Bot 会自动检索配置好的自我信息来回答。
- **人设图库**：把人设图片放进插件目录，Bot 就能浏览图库缩略图，并在需要展示自己形象时取出对应原图。
- **自我头像**：Bot 可以获取并展示自己的 QQ 头像。

## 安装

- 推荐方式：在 MaiBot WebUI 的「插件市场」中搜索 `自我信息插件` 或 `Self Identity`，一键安装。
- 手动方式：把本仓库克隆或下载到 MaiBot 的 `plugins` 目录，重启 MaiBot。

需要 MaiBot 1.3.0 或更高版本；插件 ID 为 `valleywinds.self-identity`。

## 配置

安装后在 WebUI 插件配置页（或插件目录的 `config.toml`）中修改：

- **自我信息**：`infos` 列表中每条包含标题、关键词和完整内容。默认内容只是示例，请按自己 Bot 的人设修改。
- **人设图片**：把图片放进插件目录的 `self_image` 文件夹即可，支持 `.jpg`、`.jpeg`、`.png`、`.webp`、`.gif`、`.bmp`，缩略图由插件自动生成。

**头像功能不可用？** 请在主程序配置中把 `bot.qq_account` 设置为 Bot 自己的 QQ 号，留空或为 `0` 时头像功能会提示失败。

## 致谢

本项目基于 [self_identity_plugin](https://github.com/SengokuCola/self_identity_plugin)（上游已停止维护）修改而来，感谢原作者 [SengokuCola](https://github.com/SengokuCola) 的原始工作。

## 许可证

本项目以 GPL-3.0-or-later（GNU 通用公共许可证第 3 版或更新版本）发布，全文见本仓库的 `LICENSE` 文件。
