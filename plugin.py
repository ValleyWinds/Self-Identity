"""自我信息插件。"""

from __future__ import annotations

from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

import asyncio
import base64
import hashlib
import logging
import re

from PIL import Image
from maibot_sdk import Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType


_MAX_DOWNLOAD_IMAGE_BYTES = 15 * 1024 * 1024
_MAX_LOCAL_IMAGE_BYTES = 5 * 1024 * 1024
_IMAGE_FORMAT_NAMES = {"jpg", "jpeg", "png", "webp", "gif", "bmp"}
_SELF_IMAGE_DIR_NAME = "self_image"
_SELF_IMAGE_THUMB_DIR_NAME = "image_thumbup"
_SELF_IMAGE_THUMB_SIZE = (512, 512)
_SELF_IMAGE_PAGE_SIZE = 10
_SUPPORTED_SELF_IMAGE_SUFFIXES = {f".{suffix}" for suffix in _IMAGE_FORMAT_NAMES}
_QQ_AVATAR_URL_TEMPLATE = "https://q1.qlogo.cn/g?b=qq&nk={qq_account}&s=640"
logger = logging.getLogger("plugin.valleywinds.self-identity")


def _tool_param(name: str, param_type: ToolParamType, description: str, required: bool) -> ToolParameterInfo:
    """构造工具参数声明。"""

    return ToolParameterInfo(name=name, param_type=param_type, description=description, required=required)


def _guess_image_format_from_name(file_name: str, default: str = "png") -> str:
    """根据文件名猜测图片格式。"""

    suffix = Path(file_name).suffix.lower().lstrip(".")
    if suffix in _IMAGE_FORMAT_NAMES:
        return "jpeg" if suffix == "jpg" else suffix
    return default


def _guess_image_format_from_bytes(image_bytes: bytes, default: str = "png") -> str:
    """根据图片文件头猜测图片格式。"""

    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if image_bytes.startswith(b"GIF8"):
        return "gif"
    if image_bytes.startswith(b"RIFF") and b"WEBP" in image_bytes[:16]:
        return "webp"
    if image_bytes.startswith(b"BM"):
        return "bmp"
    return default


def _image_bytes_to_base64(image_bytes: bytes) -> str:
    """将图片二进制内容编码为 Base64 字符串。"""

    return base64.b64encode(image_bytes).decode("utf-8")


def _read_image_file(image_path: Path) -> Optional[Tuple[str, str]]:
    """读取本地图片文件并返回格式与 Base64，超过大小上限时返回 None。"""

    if not image_path.exists() or not image_path.is_file():
        return None
    if image_path.stat().st_size > _MAX_LOCAL_IMAGE_BYTES:
        return None
    image_bytes = image_path.read_bytes()
    image_format = _guess_image_format_from_bytes(image_bytes, _guess_image_format_from_name(image_path.name))
    return image_format, _image_bytes_to_base64(image_bytes)


def _build_image_mime_type(image_format: str) -> str:
    """根据内部图片格式生成 MIME 类型。"""

    normalized_format = (image_format or "png").strip().lower()
    if normalized_format == "jpg":
        normalized_format = "jpeg"
    return f"image/{normalized_format}"


def _download_image_url(image_url: str) -> Optional[Tuple[str, str]]:
    """下载图片 URL 并返回格式与 Base64。"""

    request = Request(image_url, headers={"User-Agent": "MaiBot-self-identity-plugin/1.0"})
    with urlopen(request, timeout=10) as response:
        content_type = str(response.headers.get("Content-Type") or "").lower()
        if content_type and not content_type.startswith("image/"):
            return None

        image_bytes = response.read(_MAX_DOWNLOAD_IMAGE_BYTES + 1)
    if not image_bytes or len(image_bytes) > _MAX_DOWNLOAD_IMAGE_BYTES:
        return None

    url_path = unquote(urlparse(image_url).path)
    guessed_format = _guess_image_format_from_name(url_path)
    if content_type.startswith("image/"):
        guessed_format = content_type.split(";", 1)[0].split("/", 1)[1].strip() or guessed_format
    image_format = _guess_image_format_from_bytes(image_bytes, guessed_format)
    return image_format, _image_bytes_to_base64(image_bytes)


def _build_identity_tool_unavailable_result(reason: str) -> Dict[str, Any]:
    """构造工具不可用时的兜底结果。"""

    normalized_reason = str(reason or "").strip() or "工具当前不可用。"
    return {
        "success": False,
        "content": normalized_reason,
    }


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(
        default=True,
        description="是否启用插件",
        json_schema_extra={"label": "启用插件"},
    )
    config_version: str = Field(
        default="1.4.2",
        description="配置版本",
        json_schema_extra={"label": "配置版本", "hidden": True},
    )


class IdentityImageConfig(PluginConfigBase):
    """人设图库配置。"""

    __ui_label__ = "人设图片"
    __ui_icon__ = "image"
    __ui_order__ = 1

    image_dir: str = Field(
        default=_SELF_IMAGE_DIR_NAME,
        description="人设原图目录，支持插件目录相对路径或绝对路径",
        json_schema_extra={
            "label": "人设原图目录",
            "hint": "支持绝对路径；请确保指向可信位置",
        },
    )
    thumbnail_dir: str = Field(
        default=_SELF_IMAGE_THUMB_DIR_NAME,
        description="人设图缩略图目录，支持插件目录相对路径或绝对路径",
        json_schema_extra={
            "label": "人设缩略图目录",
            "hint": "缩略图将写入该目录，支持绝对路径，请确保指向可信位置",
        },
    )


class IdentityInfoItem(PluginConfigBase):
    """单条自我信息。"""

    title: str = Field(default="", description="信息标题")
    keywords: List[str] = Field(default_factory=list, description="关键词列表")
    full_information: str = Field(default="", description="全量信息")


class SearchConfig(PluginConfigBase):
    """搜索配置。"""

    __ui_label__ = "搜索"
    __ui_icon__ = "search"
    __ui_order__ = 2

    default_limit: int = Field(
        default=5,
        ge=1,
        le=20,
        description="默认返回条数",
        json_schema_extra={"label": "检索默认返回条数"},
    )


class InfosSectionConfig(PluginConfigBase):
    """Bot 的自我信息内容，供 Bot 检索回答关于自己的问题。"""

    __ui_label__ = "自我信息"
    __ui_icon__ = "notes"
    __ui_order__ = 3

    # infos 必须放在命名节内：SDK 会把根级字段归入 WebUI 的 general 虚拟节，
    # 而前端按 config[节名][字段名] 取值，根级字段在配置表单里永远取不到值（前端 1.3.x 未修）。
    infos: List[IdentityInfoItem] = Field(
        default_factory=list,
        description="Bot 的自我信息列表",
        json_schema_extra={"label": "自我信息列表"},
    )


class SelfIdentityPluginConfig(PluginConfigBase):
    """插件配置模型。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    identity_image: IdentityImageConfig = Field(default_factory=IdentityImageConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    infos_section: InfosSectionConfig = Field(default_factory=InfosSectionConfig)


class SelfIdentityPlugin(MaiBotPlugin):
    """自我信息插件。"""

    config_model = SelfIdentityPluginConfig

    @property
    def plugin_dir(self) -> Path:
        """返回插件目录。"""

        return Path(__file__).resolve().parent

    async def on_load(self) -> None:
        """插件加载回调。"""

        self._ensure_self_image_library()

    async def on_unload(self) -> None:
        """插件卸载回调。"""

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        """插件配置更新回调。"""

        del scope
        del config_data
        del version

    def _resolve_configured_dir(self, configured_path: str, default_name: str) -> Path:
        """解析插件配置中的目录路径。"""

        normalized_path = configured_path.strip() or default_name
        directory_path = Path(normalized_path)
        if not directory_path.is_absolute():
            directory_path = (self.plugin_dir / directory_path).resolve()
        return directory_path

    @property
    def self_image_dir(self) -> Path:
        """返回人设原图目录。"""

        return self._resolve_configured_dir(self.config.identity_image.image_dir, _SELF_IMAGE_DIR_NAME)

    @property
    def self_image_thumbnail_dir(self) -> Path:
        """返回人设图缩略图目录。"""

        return self._resolve_configured_dir(self.config.identity_image.thumbnail_dir, _SELF_IMAGE_THUMB_DIR_NAME)

    @staticmethod
    def _is_supported_self_image(image_path: Path) -> bool:
        """判断文件是否是支持的人设图片。"""

        return image_path.is_file() and image_path.suffix.lower() in _SUPPORTED_SELF_IMAGE_SUFFIXES

    @staticmethod
    def _build_self_image_id(image_path: Path) -> str:
        """根据文件名和路径生成稳定图片 ID。"""

        identity_source = f"{image_path.name}|{image_path.resolve()}".encode("utf-8", errors="ignore")
        return hashlib.sha1(identity_source).hexdigest()[:12]

    def _build_thumbnail_path(self, image_path: Path) -> Path:
        """构造某张人设图对应的缩略图路径。"""

        image_id = self._build_self_image_id(image_path)
        safe_stem = re.sub(r"[^0-9A-Za-z_.-]+", "_", image_path.stem).strip("._") or "self_image"
        return self.self_image_thumbnail_dir / f"{safe_stem}_{image_id}.png"

    def _ensure_self_image_library(self) -> None:
        """确保人设图库目录存在，并为现有图片生成缩略图。"""

        self.self_image_dir.mkdir(parents=True, exist_ok=True)
        self.self_image_thumbnail_dir.mkdir(parents=True, exist_ok=True)
        generated_count = 0
        for image_path in self._list_self_image_paths(ensure_library=False):
            thumbnail_path = self._build_thumbnail_path(image_path)
            try:
                source_mtime = image_path.stat().st_mtime
                if thumbnail_path.exists() and thumbnail_path.stat().st_mtime >= source_mtime:
                    continue
                self._generate_thumbnail(image_path, thumbnail_path)
                generated_count += 1
            except Exception as exc:
                logger.warning("生成人设图缩略图失败：image=%s error=%s", image_path, exc, exc_info=True)
        logger.info(
            "人设图库检查完成：image_dir=%s thumbnail_dir=%s generated=%s",
            self.self_image_dir,
            self.self_image_thumbnail_dir,
            generated_count,
        )

    @staticmethod
    def _generate_thumbnail(image_path: Path, thumbnail_path: Path) -> None:
        """生成单张人设图的缩略图。"""

        thumbnail_path.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(image_path) as image:
            image.thumbnail(_SELF_IMAGE_THUMB_SIZE, Image.Resampling.LANCZOS)
            if image.mode not in {"RGB", "RGBA"}:
                image = image.convert("RGBA")
            image.save(thumbnail_path, format="PNG", optimize=True)

    def _list_self_image_paths(self, ensure_library: bool = True) -> List[Path]:
        """列出全部人设原图路径。"""

        if ensure_library:
            self._ensure_self_image_library()
        if not self.self_image_dir.exists():
            return []
        return sorted(
            (path for path in self.self_image_dir.iterdir() if self._is_supported_self_image(path)),
            key=lambda path: path.name.lower(),
        )

    def _build_self_image_records(self, ensure_library: bool = True) -> List[Dict[str, Any]]:
        """构造人设图库记录。"""

        records: List[Dict[str, Any]] = []
        for index, image_path in enumerate(self._list_self_image_paths(ensure_library=ensure_library), start=1):
            records.append(
                {
                    "index": index,
                    "id": self._build_self_image_id(image_path),
                    "name": image_path.name,
                    "path": image_path,
                    "thumbnail_path": self._build_thumbnail_path(image_path),
                }
            )
        return records

    def _resolve_self_image_record(self, image_name: str = "", image_index: int = 0) -> Tuple[Optional[Dict[str, Any]], str]:
        """按图片名称或序号解析人设图记录。"""

        records = self._build_self_image_records()
        if not records:
            return None, f"人设图库为空，请先把图片放入 {self.self_image_dir}。"

        normalized_name = image_name.strip()
        if normalized_name:
            for record in records:
                if normalized_name in {str(record["name"]), str(record["id"])}:
                    return record, ""
            return None, f"没有找到名为或 ID 为 {normalized_name} 的人设图。"

        try:
            normalized_index = int(image_index or 0)
        except (TypeError, ValueError):
            normalized_index = 0

        if normalized_index > 0:
            if normalized_index <= len(records):
                return records[normalized_index - 1], ""
            return None, f"人设图序号超出范围：{normalized_index}，当前共有 {len(records)} 张。"

        if len(records) == 1:
            return records[0], ""
        return None, "存在多张人设图，请提供 image_name 或 image_index 来选择一张。"

    def _build_image_content_item(self, image_path: Path, name: str, metadata: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """构造工具图片内容项。"""

        image_result = _read_image_file(image_path)
        if image_result is None:
            return None
        image_format, image_base64 = image_result
        return {
            "type": "image",
            "data": image_base64,
            "mime_type": _build_image_mime_type(image_format),
            "name": name,
            "metadata": metadata,
        }

    async def _resolve_bot_qq_account(self) -> Tuple[str, str]:
        """从主配置读取 Bot 的 QQ 号。"""

        try:
            config_value = await self.ctx.config.get("bot.qq_account", "")
        except Exception as exc:
            return "", f"读取 bot.qq_account 失败：{type(exc).__name__}: {exc}"

        qq_account = str(config_value or "").strip()
        if qq_account in {"", "0"}:
            return "", "当前未配置 bot.qq_account，无法获取自己的 QQ 头像。"
        if not qq_account.isdigit():
            return "", f"bot.qq_account 不是有效 QQ 号：{qq_account}"
        return qq_account, ""

    @staticmethod
    def _score_info_item(item: IdentityInfoItem, title: str, keyword: str, query: str) -> float:
        """计算信息项匹配分数。"""

        normalized_title = title.strip().lower()
        normalized_keyword = keyword.strip().lower()
        normalized_query = query.strip().lower()
        item_title = item.title.strip().lower()
        item_keywords = [entry.strip().lower() for entry in item.keywords if entry.strip()]
        item_information = item.full_information.strip().lower()

        score = 0.0
        if normalized_title:
            if item_title == normalized_title:
                score += 120.0
            elif normalized_title in item_title:
                score += 90.0
            else:
                score += SequenceMatcher(None, normalized_title, item_title).ratio() * 70.0

        if normalized_keyword:
            for item_keyword in item_keywords:
                if item_keyword == normalized_keyword:
                    score += 85.0
                elif normalized_keyword in item_keyword or item_keyword in normalized_keyword:
                    score += 60.0
                else:
                    score += SequenceMatcher(None, normalized_keyword, item_keyword).ratio() * 35.0

        if normalized_query:
            if normalized_query in item_title:
                score += 65.0
            if any(normalized_query in item_keyword for item_keyword in item_keywords):
                score += 55.0
            if normalized_query in item_information:
                score += 35.0
            score += SequenceMatcher(None, normalized_query, item_title).ratio() * 25.0

        return score

    @staticmethod
    def _format_search_results(matches: List[IdentityInfoItem]) -> str:
        """格式化搜索结果文本。"""

        lines: List[str] = [f"共找到 {len(matches)} 条与 Bot 自我信息相关的结果。"]
        for index, item in enumerate(matches, start=1):
            keywords_text = "、".join(item.keywords) if item.keywords else "无"
            lines.extend(
                [
                    "",
                    f"{index}. 标题：{item.title or '未命名'}",
                    f"关键词：{keywords_text}",
                    f"全量信息：{item.full_information or '无'}",
                ]
            )
        return "\n".join(lines)

    @Tool(
        "search_self_information",
        description="当有人提及你的信息，包括基本信息，人设，外貌，特征等等，或者你自己的设定信息有利于你进行下一步回复时调用",
        parameters=[
            _tool_param("query", ToolParamType.STRING, "通用搜索词，可为空", False),
            _tool_param("title", ToolParamType.STRING, "按标题模糊匹配，可为空", False),
            _tool_param("keyword", ToolParamType.STRING, "按关键词搜索，可为空", False),
            _tool_param("limit", ToolParamType.INTEGER, "最多返回几条结果", False),
        ],
    )
    async def handle_search_self_information(
        self,
        query: str = "",
        title: str = "",
        keyword: str = "",
        limit: int = 0,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """搜索自我信息。"""

        del kwargs

        try:
            normalized_limit = int(limit or 0)
        except (TypeError, ValueError):
            normalized_limit = 0
        if normalized_limit <= 0:
            normalized_limit = self.config.search.default_limit
        infos = self.config.infos_section.infos
        if not infos:
            return {
                "success": False,
                "content": "当前还没有配置任何 Bot 自我信息。",
                "matches": [],
            }

        if not query.strip() and not title.strip() and not keyword.strip():
            return {
                "success": False,
                "content": "请至少提供 query、title、keyword 其中一个搜索条件。",
                "matches": [],
            }

        scored_items: List[Tuple[float, IdentityInfoItem]] = []
        for item in infos:
            score = self._score_info_item(item, title=title, keyword=keyword, query=query)
            if score > 0:
                scored_items.append((score, item))

        scored_items.sort(key=lambda entry: entry[0], reverse=True)
        matched_items = [entry[1] for entry in scored_items[:normalized_limit]]
        if not matched_items:
            return {
                "success": False,
                "content": "没有找到匹配的 Bot 自我信息。",
                "matches": [],
            }

        serialized_matches = [
            {
                "title": item.title,
                "keywords": list(item.keywords),
                "full_information": item.full_information,
            }
            for item in matched_items
        ]
        return {
            "success": True,
            "content": self._format_search_results(matched_items),
            "matches": serialized_matches,
        }

    @Tool(
        "view_all_image",
        description=(
            "浏览所有 Bot 人设图片的缩略图版本。每页最多显示 10 张；如果图片超过 10 张，可以通过 page 参数选择页码。"
            "返回结果中的 index、name 或 id 可用于 get_self_image 获取原始大小图片。"
        ),
        parameters=[
            _tool_param("page", ToolParamType.INTEGER, "要浏览的页码，从 1 开始", False),
        ],
    )
    async def handle_view_all_image(
        self,
        page: int = 1,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """分页返回所有人设图缩略图。"""

        del kwargs

        try:
            records = self._build_self_image_records()
            total_count = len(records)
            if total_count == 0:
                return {
                    "success": False,
                    "content": f"人设图库为空，请先把图片放入 {self.self_image_dir}。",
                    "images": [],
                }

            total_pages = max(1, (total_count + _SELF_IMAGE_PAGE_SIZE - 1) // _SELF_IMAGE_PAGE_SIZE)
            try:
                requested_page = int(page or 1)
            except (TypeError, ValueError):
                requested_page = 1
            normalized_page = min(max(1, requested_page), total_pages)
            start_index = (normalized_page - 1) * _SELF_IMAGE_PAGE_SIZE
            page_records = records[start_index : start_index + _SELF_IMAGE_PAGE_SIZE]
            content_items = []
            serialized_images = []
            for record in page_records:
                thumbnail_path = record["thumbnail_path"]
                content_item = self._build_image_content_item(
                    thumbnail_path,
                    f"thumb_{record['index']}_{record['name']}.png",
                    {
                        "source": "valleywinds.self-identity",
                        "usage": "self_identity_thumbnail",
                        "image_index": record["index"],
                        "image_id": record["id"],
                        "image_name": record["name"],
                    },
                )
                if content_item is not None:
                    content_items.append(content_item)
                serialized_images.append(
                    {
                        "index": record["index"],
                        "id": record["id"],
                        "name": record["name"],
                    }
                )

            image_lines = [f"{image['index']}. {image['name']}（id: {image['id']}）" for image in serialized_images]
            content = (
                f"人设图库第 {normalized_page}/{total_pages} 页，共 {total_count} 张。"
                "可使用 get_self_image 的 image_index、image_name 或 id 获取原图。\n"
                + "\n".join(image_lines)
            )
            return {
                "success": True,
                "content": content.strip(),
                "page": normalized_page,
                "page_size": _SELF_IMAGE_PAGE_SIZE,
                "total_pages": total_pages,
                "total_count": total_count,
                "images": serialized_images,
                "content_items": content_items,
            }
        except Exception as exc:
            logger.error("view_all_image 工具异常：error=%s", exc, exc_info=True)
            return _build_identity_tool_unavailable_result(f"浏览人设图库失败：{type(exc).__name__}: {exc}")

    @Tool(
        "get_self_avatar",
        description="当需要查看、展示或引用你自己的 QQ 头像时调用，会根据 bot.qq_account 获取高清 QQ 头像并作为工具图片返回。",
        parameters=[],
    )
    async def handle_get_self_avatar(
        self,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """获取并返回 Bot 自己的 QQ 头像。"""

        del kwargs

        try:
            qq_account, resolve_error = await self._resolve_bot_qq_account()
            if resolve_error:
                logger.warning("get_self_avatar QQ 号解析失败：reason=%s", resolve_error)
                return _build_identity_tool_unavailable_result(resolve_error)

            avatar_url = _QQ_AVATAR_URL_TEMPLATE.format(qq_account=qq_account)

            try:
                image_result = await asyncio.to_thread(_download_image_url, avatar_url)
            except Exception as exc:
                logger.error("get_self_avatar 头像下载失败：qq=%s error=%s", qq_account, exc, exc_info=True)
                return _build_identity_tool_unavailable_result(f"QQ 头像下载失败：{type(exc).__name__}: {exc}")
            if image_result is None:
                return _build_identity_tool_unavailable_result("QQ 头像下载失败：返回内容不是有效图片。")

            image_format, image_base64 = image_result
            image_format = (image_format or "png").strip().lower()
            image_suffix = "jpg" if image_format == "jpeg" else image_format
            mime_type = _build_image_mime_type(image_format)

            return {
                "success": True,
                "content": f"已获取 Bot 自己的 QQ 头像（QQ：{qq_account}）。",
                "qq_account": qq_account,
                "avatar_url": avatar_url,
                "image_format": image_format,
                "image_base64": image_base64,
                "mime_type": mime_type,
                "content_items": [
                    {
                        "type": "image",
                        "data": image_base64,
                        "mime_type": mime_type,
                        "name": f"self_avatar_{qq_account}.{image_suffix}",
                        "metadata": {
                            "source": "valleywinds.self-identity",
                            "usage": "self_avatar",
                            "qq_account": qq_account,
                            "avatar_url": avatar_url,
                        },
                    }
                ],
            }
        except Exception as exc:
            logger.error("get_self_avatar 工具异常：error=%s", exc, exc_info=True)
            return _build_identity_tool_unavailable_result(f"获取自己的头像失败：{type(exc).__name__}: {exc}")

    @Tool(
        "get_self_image",
        description=(
            "获取某张 Bot 人设图片的原始大小版本，并作为工具图片返回。"
            "可以使用 view_all_image 返回的 image_index、image_name 或 id 选择图片。"
        ),
        parameters=[
            _tool_param("image_index", ToolParamType.INTEGER, "图片序号，从 1 开始；可从 view_all_image 返回结果中获取", False),
            _tool_param("image_name", ToolParamType.STRING, "图片文件名或 id；可从 view_all_image 返回结果中获取", False),
        ],
    )
    async def handle_get_self_image(
        self,
        image_index: int = 0,
        image_name: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """返回指定人设图原图，供主模型自行进行图片判断。"""

        del kwargs

        try:
            record, resolve_error = self._resolve_self_image_record(image_name=image_name, image_index=image_index)
            if record is None:
                logger.warning("get_self_image 人设图解析失败：reason=%s", resolve_error)
                return _build_identity_tool_unavailable_result(resolve_error)

            image_path = record["path"]
            if image_path.is_file() and image_path.stat().st_size > _MAX_LOCAL_IMAGE_BYTES:
                return _build_identity_tool_unavailable_result(
                    f"人设原图 {record['name']} 超过 {_MAX_LOCAL_IMAGE_BYTES // (1024 * 1024)}MB 上限，无法整张返回。"
                    "请改用 view_all_image 查看对应缩略图。"
                )

            image_result = _read_image_file(image_path)
            if image_result is None:
                return _build_identity_tool_unavailable_result(f"人设图片读取失败：{image_path}")

            image_format, image_base64 = image_result
            image_format = (image_format or "png").strip().lower()
            mime_type = _build_image_mime_type(image_format)
            return {
                "success": True,
                "content": (
                    f"已返回第 {record['index']} 张 Bot 人设原图：{record['name']}。"
                    "请将这张图片作为自我形象参考。"
                ),
                "image_index": record["index"],
                "image_id": record["id"],
                "image_name": record["name"],
                "image_format": image_format,
                "image_base64": image_base64,
                "mime_type": mime_type,
                "content_items": [
                    {
                        "type": "image",
                        "data": image_base64,
                        "mime_type": mime_type,
                        "name": str(record["name"]),
                        "metadata": {
                            "source": "valleywinds.self-identity",
                            "usage": "self_identity_reference",
                            "image_index": record["index"],
                            "image_id": record["id"],
                            "image_name": record["name"],
                        },
                    }
                ],
            }
        except Exception as exc:
            logger.error("get_self_image 工具异常：error=%s", exc, exc_info=True)
            return _build_identity_tool_unavailable_result(f"人设图片工具暂时不可用：{type(exc).__name__}: {exc}")


def create_plugin() -> SelfIdentityPlugin:
    """创建插件实例。"""

    return SelfIdentityPlugin()
