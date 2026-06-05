"""自我信息插件。"""

from __future__ import annotations

from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

import base64
import hashlib
import json
import logging
import re

from PIL import Image
from maibot_sdk import Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType


_MAX_DOWNLOAD_IMAGE_BYTES = 15 * 1024 * 1024
_SELF_IMAGE_DIR_NAME = "self_image"
_SELF_IMAGE_THUMB_DIR_NAME = "image_thumbup"
_SELF_IMAGE_THUMB_SIZE = (512, 512)
_SELF_IMAGE_PAGE_SIZE = 10
_SUPPORTED_SELF_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
_QQ_AVATAR_URL_TEMPLATE = "https://q1.qlogo.cn/g?b=qq&nk={qq_account}&s=640"
logger = logging.getLogger("plugin.self_identity_plugin")


def _tool_param(name: str, param_type: ToolParamType, description: str, required: bool) -> ToolParameterInfo:
    """构造工具参数声明。"""

    return ToolParameterInfo(name=name, param_type=param_type, description=description, required=required)


def _extract_nested_mapping(payload: Any) -> Dict[str, Any]:
    """从 capability 返回值中剥离常见包装层，取出业务字典。"""

    current = payload
    visited: set[int] = set()
    while isinstance(current, dict):
        current_id = id(current)
        if current_id in visited:
            break
        visited.add(current_id)

        for wrapper_key in ("result", "data"):
            nested_value = current.get(wrapper_key)
            if isinstance(nested_value, dict):
                current = nested_value
                break
        else:
            return current
    return {}


def _guess_image_format_from_name(file_name: str, default: str = "png") -> str:
    """根据文件名猜测图片格式。"""

    suffix = Path(file_name).suffix.lower().lstrip(".")
    if suffix in {"jpg", "jpeg", "png", "webp", "gif", "bmp"}:
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


def _decode_base64_image(raw_base64: str) -> Optional[Tuple[str, str]]:
    """解析普通 Base64 或 data URL 图片内容。"""

    normalized_base64 = raw_base64.strip()
    if not normalized_base64:
        return None

    image_format = "png"
    data_url_match = re.match(
        r"^data:image/(?P<format>[a-zA-Z0-9.+-]+);base64,(?P<data>.+)$",
        normalized_base64,
        re.DOTALL,
    )
    if data_url_match is not None:
        image_format = data_url_match.group("format").lower()
        normalized_base64 = data_url_match.group("data").strip()

    try:
        image_bytes = base64.b64decode(normalized_base64, validate=True)
    except Exception:
        return None
    if not image_bytes:
        return None

    return _guess_image_format_from_bytes(image_bytes, image_format), _image_bytes_to_base64(image_bytes)


def _read_image_file(image_path: Path) -> Optional[Tuple[str, str]]:
    """读取本地图片文件并返回格式与 Base64。"""

    if not image_path.exists() or not image_path.is_file():
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


def _resolve_file_url(file_url: str) -> Optional[Path]:
    """将 file:// URL 转换为本地路径。"""

    parsed_url = urlparse(file_url)
    if parsed_url.scheme.lower() != "file":
        return None

    if parsed_url.netloc and parsed_url.path:
        return Path(f"//{parsed_url.netloc}{unquote(parsed_url.path)}")
    return Path(unquote(parsed_url.path))


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


def _safe_preview(value: Any, max_length: int = 160) -> str:
    """构造适合日志输出的短预览，避免泄漏图片二进制内容。"""

    if isinstance(value, dict):
        return f"<dict keys={sorted(str(key) for key in value.keys())}>"
    if isinstance(value, list):
        return f"<list len={len(value)}>"

    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("data:image/"):
        header = text.split(",", 1)[0]
        return f"{header},<base64 len={max(0, len(text) - len(header) - 1)}>"
    if len(text) > max_length:
        return f"{text[:max_length]}...<len={len(text)}>"
    return text


def _classify_image_reference(reference: str) -> str:
    """判断图片引用类型，用于调试信息。"""

    normalized_reference = reference.strip()
    if not normalized_reference:
        return "empty"
    if normalized_reference.startswith("data:image/"):
        return "data_url"
    if _is_windows_drive_path(normalized_reference):
        return "path_or_text"

    parsed_url = urlparse(normalized_reference)
    normalized_scheme = parsed_url.scheme.lower()
    if normalized_scheme:
        return normalized_scheme
    if re.fullmatch(r"[A-Za-z0-9+/=\s]+", normalized_reference) and len(normalized_reference) > 64:
        return "base64"
    return "path_or_text"


def _is_windows_drive_path(reference: str) -> bool:
    """判断字符串是否像 Windows 盘符路径。"""

    return bool(re.match(r"^[A-Za-z]:[\\/]", reference.strip()))


def _read_image_reference_with_reason(reference: str) -> Tuple[Optional[Tuple[str, str]], str]:
    """从图片引用读取图片，并返回失败原因。"""

    normalized_reference = reference.strip()
    if not normalized_reference:
        return None, "empty_reference"

    decoded_base64 = _decode_base64_image(normalized_reference)
    if decoded_base64 is not None:
        return decoded_base64, "base64_ok"

    parsed_url = urlparse(normalized_reference)
    normalized_scheme = parsed_url.scheme.lower()
    if not _is_windows_drive_path(normalized_reference):
        if normalized_scheme == "file":
            file_path = _resolve_file_url(normalized_reference)
            if file_path is None:
                return None, "file_url_unresolved"
            image_result = _read_image_file(file_path)
            return image_result, "file_url_ok" if image_result is not None else f"file_not_found:{file_path}"
        if normalized_scheme in {"http", "https"}:
            try:
                image_result = _download_image_url(normalized_reference)
            except Exception as exc:
                return None, f"url_download_error:{type(exc).__name__}: {exc}"
            return image_result, "url_ok" if image_result is not None else "url_not_image_or_too_large"
        if normalized_scheme:
            return None, f"unsupported_scheme:{normalized_scheme}"

    try:
        image_path = Path(normalized_reference)
    except OSError as exc:
        return None, f"path_parse_error:{type(exc).__name__}: {exc}"
    image_result = _read_image_file(image_path)
    return image_result, "path_ok" if image_result is not None else f"path_not_found:{image_path}"


def _read_image_reference(reference: str) -> Optional[Tuple[str, str]]:
    """从本地路径、file URL、http(s) URL、data URL 或 Base64 引用中读取图片。"""

    image_result, _ = _read_image_reference_with_reason(reference)
    return image_result


def _extract_image_hash(component: Dict[str, Any]) -> str:
    """从图片消息段中提取图片 hash。"""

    for key in ("hash", "binary_hash", "image_hash", "file_hash"):
        value = str(component.get(key) or "").strip()
        if value:
            return value
    return ""


def _read_cached_image_by_hash_with_reason(image_hash: str) -> Tuple[Optional[Tuple[str, str]], str]:
    """通过图片 hash 从本地图片缓存数据库读取图片，并返回失败原因。"""

    if not image_hash:
        return None, "empty_hash"

    try:
        from sqlmodel import select

        from src.common.database.database import get_db_session
        from src.common.database.database_model import Images, ImageType

        with get_db_session() as db:
            statement = select(Images).filter_by(image_hash=image_hash, image_type=ImageType.IMAGE).limit(1)
            image_record = db.exec(statement).first()
            if image_record is None or getattr(image_record, "no_file_flag", False):
                return None, "cache_record_not_found_or_no_file"
            raw_full_path = str(image_record.full_path or "").strip()

        if not raw_full_path:
            return None, "cache_record_empty_path"

        image_path = Path(raw_full_path).expanduser().resolve()
        image_result = _read_image_file(image_path)
        return image_result, "cache_file_ok" if image_result is not None else f"cache_file_not_found:{image_path}"
    except Exception as exc:
        return None, f"cache_lookup_error:{type(exc).__name__}: {exc}"


def _read_cached_image_by_hash(image_hash: str) -> Optional[Tuple[str, str]]:
    """通过图片 hash 从本地图片缓存数据库读取图片。"""

    image_result, _ = _read_cached_image_by_hash_with_reason(image_hash)
    return image_result


def _iter_image_reference_values(value: Any) -> List[str]:
    """从图片消息段 data 或附加字段中收集可能的图片引用。"""

    if isinstance(value, dict):
        references: List[str] = []
        for key in ("binary_data_base64", "base64", "data_url", "url", "file", "file_path", "path", "data"):
            references.extend(_iter_image_reference_values(value.get(key)))
        return references

    if isinstance(value, list):
        references = []
        for item in value:
            references.extend(_iter_image_reference_values(item))
        return references

    normalized_value = str(value or "").strip()
    if not normalized_value:
        return []
    if normalized_value.startswith("[") and normalized_value.endswith("]") and not normalized_value.startswith("[CQ:"):
        return []

    references = [normalized_value]
    cq_url_match = re.search(r"(?:url|file|path)=([^,\]]+)", normalized_value)
    if cq_url_match is not None:
        references.append(cq_url_match.group(1).strip())
    return references


def _extract_json_object(text: str) -> Dict[str, Any]:
    """从模型输出中提取 JSON 对象。"""

    normalized_text = str(text or "").strip()
    if not normalized_text:
        return {}

    candidates = [normalized_text]
    if normalized_text.startswith("```"):
        fenced_text = normalized_text.strip("`").strip()
        if fenced_text.lower().startswith("json"):
            fenced_text = fenced_text[4:].strip()
        candidates.append(fenced_text)
    match = re.search(r"\{.*\}", normalized_text, re.DOTALL)
    if match is not None:
        candidates.append(match.group(0))

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def _extract_llm_text_pair(llm_result: Dict[str, Any]) -> Tuple[str, str]:
    """从 LLM 能力返回中提取正文与推理文本。"""

    response_candidates = [
        llm_result.get("response"),
        llm_result.get("content"),
        llm_result.get("text"),
        llm_result.get("output"),
    ]
    completion = llm_result.get("completion")
    if isinstance(completion, dict):
        response_candidates.extend(
            [
                completion.get("response"),
                completion.get("content"),
                completion.get("text"),
                completion.get("output"),
            ]
        )

    response_text = ""
    for candidate in response_candidates:
        normalized_candidate = str(candidate or "").strip()
        if normalized_candidate:
            response_text = normalized_candidate
            break

    reasoning_text = str(llm_result.get("reasoning") or "").strip()
    return response_text, reasoning_text


def _pick_first_present(mapping: Dict[str, Any], keys: List[str], default: Any = None) -> Any:
    """按候选键读取第一个存在且非空的值。"""

    for key in keys:
        value = mapping.get(key)
        if value not in (None, "", []):
            return value
    return default


def _coerce_bool(value: Any) -> bool:
    """将模型返回的布尔类值规范化为 bool。"""

    if isinstance(value, bool):
        return value
    normalized_value = str(value or "").strip().lower()
    return normalized_value in {"true", "yes", "y", "1", "similar", "相似", "是", "一致"}


def _normalize_choice(value: Any, allowed_values: List[str], alias_map: Dict[str, str], default: str) -> str:
    """规范化模型返回的枚举值。"""

    normalized_value = str(value or "").strip().lower()
    if not normalized_value:
        return default
    if normalized_value in allowed_values:
        return normalized_value
    return alias_map.get(normalized_value, default)


def _to_plain_list(value: Any) -> List[str]:
    """将任意列表值规范化为字符串列表。"""

    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _build_identity_tool_unavailable_result(reason: str, debug_info: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """构造图片比对工具不可用时的兜底结果。"""

    normalized_reason = str(reason or "").strip() or "图片比对工具当前不可用。"
    result = {
        "success": False,
        "content": normalized_reason,
    }
    if debug_info:
        result["debug_info"] = debug_info
    return result


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="1.3.0", description="配置版本")


class IdentityImageConfig(PluginConfigBase):
    """人设图库配置。"""

    __ui_label__ = "人设图片"
    __ui_icon__ = "image"
    __ui_order__ = 1

    image_dir: str = Field(
        default=_SELF_IMAGE_DIR_NAME,
        description="人设原图目录，支持插件目录相对路径或绝对路径",
    )
    thumbnail_dir: str = Field(
        default=_SELF_IMAGE_THUMB_DIR_NAME,
        description="人设图缩略图目录，支持插件目录相对路径或绝对路径",
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

    default_limit: int = Field(default=5, ge=1, le=20, description="默认返回条数")
    recent_message_scan_limit: int = Field(default=80, ge=10, le=500, description="按消息 ID 查图时扫描的最近消息数")


class SelfIdentityPluginConfig(PluginConfigBase):
    """插件配置模型。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    identity_image: IdentityImageConfig = Field(default_factory=IdentityImageConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    infos: List[IdentityInfoItem] = Field(default_factory=list, description="Bot 的自我信息列表")


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
                logger.info("生成人设图缩略图失败：image=%s error=%s", image_path, exc, exc_info=True)
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
    def _extract_image_from_message(message: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """从消息中提取第一张图片。"""

        image_format, image_base64, error, _ = SelfIdentityPlugin._extract_image_from_message_with_debug(message)
        return image_format, image_base64, error

    @staticmethod
    def _extract_image_from_message_with_debug(
        message: Dict[str, Any],
    ) -> Tuple[Optional[str], Optional[str], Optional[str], Dict[str, Any]]:
        """从消息中提取第一张图片，并保留失败排查信息。"""

        debug_info: Dict[str, Any] = {
            "message_id": str(message.get("message_id") or "").strip(),
            "message_keys": sorted(str(key) for key in message.keys()),
            "raw_message_type": type(message.get("raw_message")).__name__,
            "image_components": [],
        }

        raw_message = message.get("raw_message")
        if not isinstance(raw_message, list):
            logger.info(
                "identiy_myself_in_pic 无法读取图片：raw_message 不是列表，message_id=%s raw_type=%s keys=%s",
                debug_info["message_id"],
                debug_info["raw_message_type"],
                debug_info["message_keys"],
            )
            return None, None, "目标消息结构不合法，无法读取图片。", debug_info

        debug_info["raw_message_len"] = len(raw_message)
        logger.info(
            "identiy_myself_in_pic 开始解析目标消息图片：message_id=%s raw_message_len=%s",
            debug_info["message_id"],
            len(raw_message),
        )

        for index, component in enumerate(raw_message):
            if not isinstance(component, dict):
                continue
            if str(component.get("type") or "").strip().lower() != "image":
                continue

            component_debug: Dict[str, Any] = {
                "index": index,
                "keys": sorted(str(key) for key in component.keys()),
                "data_type": type(component.get("data")).__name__,
                "data_preview": _safe_preview(component.get("data")),
                "has_binary_data_base64": bool(str(component.get("binary_data_base64") or "").strip()),
                "references": [],
            }
            debug_info["image_components"].append(component_debug)

            binary_data_base64 = str(component.get("binary_data_base64") or "").strip()
            if binary_data_base64:
                component_debug["binary_data_base64_len"] = len(binary_data_base64)
                image_result = _decode_base64_image(binary_data_base64)
                if image_result is not None:
                    image_format, image_base64 = image_result
                    logger.info(
                        "identiy_myself_in_pic 从 binary_data_base64 解析图片成功：message_id=%s component_index=%s format=%s",
                        debug_info["message_id"],
                        index,
                        image_format,
                    )
                    return image_format, image_base64, None, debug_info
                component_debug["binary_data_base64_decode"] = "failed"
                logger.info(
                    "identiy_myself_in_pic binary_data_base64 解码失败：message_id=%s component_index=%s len=%s",
                    debug_info["message_id"],
                    index,
                    len(binary_data_base64),
                )

            image_hash = _extract_image_hash(component)
            component_debug["hash"] = f"{image_hash[:12]}...<len={len(image_hash)}>" if image_hash else ""
            image_result, cache_reason = _read_cached_image_by_hash_with_reason(image_hash)
            component_debug["cache_lookup"] = cache_reason
            if image_result is not None:
                image_format, image_base64 = image_result
                logger.info(
                    "identiy_myself_in_pic 从图片缓存解析成功：message_id=%s component_index=%s hash=%s format=%s",
                    debug_info["message_id"],
                    index,
                    component_debug["hash"],
                    image_format,
                )
                return image_format, image_base64, None, debug_info
            logger.info(
                "identiy_myself_in_pic 图片缓存未命中：message_id=%s component_index=%s hash=%s reason=%s",
                debug_info["message_id"],
                index,
                component_debug["hash"],
                cache_reason,
            )

            references = []
            references.extend(_iter_image_reference_values(component.get("data")))
            references.extend(_iter_image_reference_values(component.get("url")))
            references.extend(_iter_image_reference_values(component.get("file")))
            references.extend(_iter_image_reference_values(component.get("file_path")))
            references.extend(_iter_image_reference_values(component.get("path")))
            references.extend(_iter_image_reference_values(component.get("data_url")))
            for reference in references:
                image_result, reference_reason = _read_image_reference_with_reason(reference)
                component_debug["references"].append(
                    {
                        "kind": _classify_image_reference(reference),
                        "preview": _safe_preview(reference),
                        "result": reference_reason,
                    }
                )
                if image_result is not None:
                    image_format, image_base64 = image_result
                    logger.info(
                        "identiy_myself_in_pic 从图片引用解析成功：message_id=%s component_index=%s kind=%s format=%s",
                        debug_info["message_id"],
                        index,
                        _classify_image_reference(reference),
                        image_format,
                    )
                    return image_format, image_base64, None, debug_info

            logger.info(
                "identiy_myself_in_pic 图片组件解析失败：message_id=%s component_index=%s keys=%s reference_count=%s",
                debug_info["message_id"],
                index,
                component_debug["keys"],
                len(references),
            )

            return None, None, "目标消息里有图片标记，但拿不到可供比对的图片二进制内容。", debug_info

        logger.info(
            "identiy_myself_in_pic 目标消息中没有图片组件：message_id=%s raw_message_len=%s",
            debug_info["message_id"],
            len(raw_message),
        )
        return None, None, "目标消息中没有图片。", debug_info

    async def _find_message_by_id(
        self,
        stream_id: str,
        msg_id: str,
    ) -> Optional[Dict[str, Any]]:
        """通过 Host 提供的单条消息查询能力按消息 ID 查找目标消息。"""

        normalized_stream_id = stream_id.strip()
        normalized_msg_id = msg_id.strip()
        if not normalized_stream_id or not normalized_msg_id:
            return None

        lookup_result = await self.ctx.call_capability(
            "message.get_by_id",
            message_id=normalized_msg_id,
            chat_id=normalized_stream_id,
        )
        if not isinstance(lookup_result, dict):
            raise RuntimeError("message.get_by_id 返回格式异常。")
        capability_result = _extract_nested_mapping(lookup_result)
        if lookup_result.get("success") is False or capability_result.get("success") is False:
            raise RuntimeError(
                str(capability_result.get("error") or lookup_result.get("error") or "message.get_by_id 查询失败。")
            )

        direct_message = capability_result.get("message")
        if direct_message is None:
            return None
        if not isinstance(direct_message, dict):
            raise RuntimeError("message.get_by_id 返回的 message 字段格式异常。")
        return direct_message

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
        "search_self_infomation",
        description="当有人提及你的信息，包括基本信息，人设，外貌，特征等等，或者你自己的设定信息有利于你进行下一步回复时调用",
        parameters=[
            _tool_param("query", ToolParamType.STRING, "通用搜索词，可为空", False),
            _tool_param("title", ToolParamType.STRING, "按标题模糊匹配，可为空", False),
            _tool_param("keyword", ToolParamType.STRING, "按关键词搜索，可为空", False),
            _tool_param("limit", ToolParamType.INTEGER, "最多返回几条结果", False),
        ],
    )
    async def handle_search_self_infomation(
        self,
        query: str = "",
        title: str = "",
        keyword: str = "",
        limit: int = 0,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """搜索自我信息。"""

        del kwargs

        normalized_limit = limit if limit > 0 else self.config.search.default_limit
        infos = self.config.infos
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
                        "source": "self_identity_plugin",
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
            logger.info("view_all_image 工具异常：error=%s", exc, exc_info=True)
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

        tool_debug_info: Dict[str, Any] = {
            "kwargs_keys": sorted(str(key) for key in kwargs.keys()),
        }

        try:
            qq_account, resolve_error = await self._resolve_bot_qq_account()
            if resolve_error:
                logger.info("get_self_avatar QQ 号解析失败：reason=%s", resolve_error)
                return _build_identity_tool_unavailable_result(resolve_error, tool_debug_info)

            avatar_url = _QQ_AVATAR_URL_TEMPLATE.format(qq_account=qq_account)
            tool_debug_info["qq_account"] = qq_account
            tool_debug_info["avatar_url"] = avatar_url

            try:
                image_result = _download_image_url(avatar_url)
            except Exception as exc:
                logger.info("get_self_avatar 头像下载失败：qq=%s error=%s", qq_account, exc, exc_info=True)
                return _build_identity_tool_unavailable_result(
                    f"QQ 头像下载失败：{type(exc).__name__}: {exc}",
                    tool_debug_info,
                )
            if image_result is None:
                return _build_identity_tool_unavailable_result("QQ 头像下载失败：返回内容不是有效图片。", tool_debug_info)

            image_format, image_base64 = image_result
            image_format = (image_format or "png").strip().lower()
            image_suffix = "jpg" if image_format == "jpeg" else image_format
            mime_type = _build_image_mime_type(image_format)
            tool_debug_info["image_format"] = image_format
            tool_debug_info["image_base64_len"] = len(image_base64 or "")

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
                            "source": "self_identity_plugin",
                            "usage": "self_avatar",
                            "qq_account": qq_account,
                            "avatar_url": avatar_url,
                        },
                    }
                ],
                "debug_info": tool_debug_info,
            }
        except Exception as exc:
            tool_debug_info["exception"] = f"{type(exc).__name__}: {exc}"
            logger.info("get_self_avatar 工具异常：error=%s", tool_debug_info["exception"], exc_info=True)
            return _build_identity_tool_unavailable_result(
                f"获取自己的头像失败：{type(exc).__name__}: {exc}",
                tool_debug_info,
            )

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

        tool_debug_info: Dict[str, Any] = {
            "image_index": image_index,
            "image_name": image_name.strip(),
            "kwargs_keys": sorted(str(key) for key in kwargs.keys()),
        }
        logger.info(
            "get_self_image 工具调用开始：image_index=%s image_name=%s kwargs_keys=%s",
            image_index,
            image_name,
            tool_debug_info["kwargs_keys"],
        )

        try:
            record, resolve_error = self._resolve_self_image_record(image_name=image_name, image_index=image_index)
            if record is None:
                logger.info("get_self_image 人设图解析失败：reason=%s", resolve_error)
                return _build_identity_tool_unavailable_result(resolve_error, tool_debug_info)

            image_path = record["path"]
            image_result = _read_image_file(image_path)
            if image_result is None:
                return _build_identity_tool_unavailable_result(f"人设图片读取失败：{image_path}", tool_debug_info)

            image_format, image_base64 = image_result
            image_format = (image_format or "png").strip().lower()
            mime_type = _build_image_mime_type(image_format)
            tool_debug_info["image_format"] = image_format
            tool_debug_info["image_base64_len"] = len(image_base64 or "")
            tool_debug_info["resolved_image"] = {
                "index": record["index"],
                "id": record["id"],
                "name": record["name"],
            }
            logger.info(
                "get_self_image 人设图解析成功：index=%s name=%s image_format=%s base64_len=%s",
                record["index"],
                record["name"],
                image_format,
                tool_debug_info["image_base64_len"],
            )
            return {
                "success": True,
                "content": (
                    f"已返回第 {record['index']} 张 Bot 人设原图：{record['name']}。"
                    "请将这张图片作为自我形象参考，与当前对话中的目标图片自行进行视觉判断。"
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
                            "source": "self_identity_plugin",
                            "usage": "self_identity_reference",
                            "image_index": record["index"],
                            "image_id": record["id"],
                            "image_name": record["name"],
                        },
                    }
                ],
                "debug_info": tool_debug_info,
            }
        except Exception as exc:
            tool_debug_info["exception"] = f"{type(exc).__name__}: {exc}"
            logger.info(
                "get_self_image 工具异常：error=%s",
                tool_debug_info["exception"],
                exc_info=True,
            )
            return _build_identity_tool_unavailable_result(
                f"人设图片工具暂时不可用：{type(exc).__name__}: {exc}",
                tool_debug_info,
            )


def create_plugin() -> SelfIdentityPlugin:
    """创建插件实例。"""

    return SelfIdentityPlugin()
