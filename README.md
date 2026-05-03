# 自我信息插件 (Self Identity Plugin)

为 MaiBot 提供基于人设图片的自我识别与自我信息检索能力。

## 功能

- 按关键词检索 Bot 的自我信息，例如人设、外貌、穿搭、主题元素等。
- 使用配置中的人设图片与消息图片进行相似度判定，帮助 Bot 判断“这是不是我”。
- 支持从本地路径、`file://`、HTTP/HTTPS 图片链接、Base64 或消息缓存中读取图片。

## 安装

1. 将本仓库克隆或下载到 MaiBot 的插件目录中。
2. 确认已安装 MaiBot 插件 SDK 2.x。
3. 在 WebUI 或 `config.toml` 中按需要修改自我信息与人设图片配置。
4. 重启 MaiBot 或重新加载插件。

## 配置

默认配置位于 `config.toml`。

- `identity_image.image_path`：Bot 人设图片路径，支持插件目录相对路径或绝对路径。
- `identity_image.image_base64`：可选，直接填写图片 Base64 内容；填写后优先使用。
- `identity_image.image_format`：Base64 图片格式，默认为 `png`。
- `identity_image.compare_model`：用于图片相似度判定的模型任务名，建议配置支持图片理解的模型。
- `infos`：Bot 自我信息列表，每项包含标题、关键词和完整说明。

## 许可证

本项目使用 MIT License。
