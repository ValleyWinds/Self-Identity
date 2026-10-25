"""Self-Identity 插件测试：真实 SDK、纯函数直测，不依赖 MaiBot 宿主。

ctx 相关路径只测不需要 RPC 的分支（搜索工具不碰 ctx；
头像工具在 ctx 未注入时必须优雅降级为 success=False）。
"""

import asyncio

from maibot_sdk.context import PluginContext


# ---------- 纯函数：图片格式识别与编码 ----------


def test_image_format_from_bytes(plugin_module):
    guess = plugin_module._guess_image_format_from_bytes
    assert guess(b"\xff\xd8\xff\xe0...") == "jpeg"
    assert guess(b"\x89PNG\r\n\x1a\n...") == "png"
    assert guess(b"GIF89a...") == "gif"
    assert guess(b"RIFF\x00\x00\x00\x00WEBP...") == "webp"
    assert guess(b"BM\x36\x00...") == "bmp"
    assert guess(b"not an image") == "png"


def test_image_format_from_name(plugin_module):
    guess = plugin_module._guess_image_format_from_name
    assert guess("photo.JPG") == "jpeg"
    assert guess("photo.jpg") == "jpeg"
    assert guess("a.png") == "png"
    assert guess("noext") == "png"


def test_build_image_mime_type(plugin_module):
    build = plugin_module._build_image_mime_type
    assert build("jpg") == "image/jpeg"
    assert build("JPEG") == "image/jpeg"
    assert build("png") == "image/png"
    assert build("") == "image/png"


def test_base64_roundtrip(plugin_module):
    encoded = plugin_module._image_bytes_to_base64(b"\xff\xd8\xff test")
    import base64

    assert base64.b64decode(encoded) == b"\xff\xd8\xff test"


# ---------- 配置模型 ----------


def test_default_config(plugin_module):
    config = plugin_module.SelfIdentityPluginConfig()
    assert config.plugin.config_version == "1.4.4"
    assert config.search.default_limit == 5
    assert config.identity_image.image_dir == "self_image"
    infos = config.infos_section.infos
    assert len(infos) == 1
    assert infos[0].title == "她不做的那些事"
    assert "打游戏" in infos[0].keywords
    assert "不打游戏" in infos[0].full_information


def test_config_injection(plugin):
    plugin.set_plugin_config(
        {
            "plugin": {"config_version": "9.9.9"},
            "search": {"default_limit": 3},
            "infos_section": {
                "infos": [
                    {
                        "title": "名字",
                        "keywords": ["叫什么"],
                        "full_information": "我叫小满。",
                    }
                ]
            },
        }
    )
    assert plugin.config.plugin.config_version == "9.9.9"
    assert plugin.config.search.default_limit == 3
    assert plugin.config.infos_section.infos[0].title == "名字"


# ---------- 搜索工具（只读 self.config，不碰 ctx） ----------


def _inject_one_info(plugin, title="名字", keywords=None, info="我叫小满。"):
    plugin.set_plugin_config(
        {
            "plugin": {"config_version": "1.4.4"},
            "infos_section": {
                "infos": [{"title": title, "keywords": keywords or [], "full_information": info}]
            },
        }
    )


def test_search_empty_infos(plugin):
    plugin.set_plugin_config(
        {"plugin": {"config_version": "1.4.4"}, "infos_section": {"infos": []}}
    )
    result = asyncio.run(plugin.handle_search_self_information(query="名字"))
    assert result["success"] is False
    assert "还没有配置" in result["content"]


def test_search_requires_condition(plugin):
    _inject_one_info(plugin)
    result = asyncio.run(plugin.handle_search_self_information())
    assert result["success"] is False
    assert "至少提供" in result["content"]


def test_search_title_match(plugin):
    _inject_one_info(plugin)
    result = asyncio.run(plugin.handle_search_self_information(title="名字"))
    assert result["success"] is True
    assert result["matches"][0]["full_information"] == "我叫小满。"


def test_search_no_match(plugin):
    _inject_one_info(plugin)
    result = asyncio.run(plugin.handle_search_self_information(query="完全无关的词组"))
    assert result["success"] is False


def test_search_limit(plugin):
    plugin.set_plugin_config(
        {
            "plugin": {"config_version": "1.4.4"},
            "infos_section": {
                "infos": [
                    {"title": f"条目{index}", "keywords": ["共同的"], "full_information": "内容"}
                    for index in range(1, 6)
                ]
            },
        }
    )
    result = asyncio.run(plugin.handle_search_self_information(keyword="共同的", limit=2))
    assert result["success"] is True
    assert len(result["matches"]) == 2


# ---------- 目录解析 ----------


def test_resolve_configured_dir(plugin, plugin_module):
    from pathlib import Path

    plugin_dir = Path(plugin_module.__file__).resolve().parent
    assert plugin._resolve_configured_dir("", "self_image") == plugin_dir / "self_image"
    assert plugin._resolve_configured_dir("pics", "self_image") == plugin_dir / "pics"
    absolute = Path("D:/somewhere/absolute").as_posix()
    assert plugin._resolve_configured_dir(absolute, "self_image") == Path(absolute)
    escaped = plugin._resolve_configured_dir("../../etc", "self_image")
    assert escaped == (plugin_dir / "self_image")


# ---------- 工具的 ctx 依赖路径 ----------


def test_get_self_avatar_without_ctx_degrades(plugin):
    result = asyncio.run(plugin.handle_get_self_avatar())
    assert result["success"] is False


def test_get_self_avatar_config_missing(plugin):
    async def fake_rpc(method, plugin_id, payload):
        assert payload["capability"] == "config.get"
        return {"success": True, "result": ""}

    plugin._set_context(PluginContext("valleywinds.self-identity", rpc_call=fake_rpc))
    result = asyncio.run(plugin.handle_get_self_avatar())
    assert result["success"] is False
    assert "qq_account" in result["content"]


def test_view_all_image_empty_library(plugin):
    plugin.set_plugin_config(
        {"plugin": {"config_version": "1.4.4"}, "infos_section": {"infos": []}}
    )
    result = asyncio.run(plugin.handle_view_all_image())
    assert result["success"] is False
    assert "图库为空" in result["content"]
