"""自我信息插件。"""

from __future__ import annotations

from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

import base64
import json
import logging
import re

from maibot_sdk import Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType


_MAX_DOWNLOAD_IMAGE_BYTES = 15 * 1024 * 1024
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
    config_version: str = Field(default="1.0.0", description="配置版本")


class IdentityImageConfig(PluginConfigBase):
    """人设图片配置。"""

    __ui_label__ = "人设图片"
    __ui_icon__ = "image"
    __ui_order__ = 1

    image_path: str = Field(
        default="",
        description="Bot 人设图片路径，支持插件目录相对路径或绝对路径",
    )
    image_base64: str = Field(
        default="",
        description="可选，直接填写图片 Base64 内容；填写后优先使用",
    )
    image_format: str = Field(
        default="png",
        description="当使用 Base64 方式配置图片时对应的图片格式",
    )
    compare_model: str = Field(
        default="utils",
        description="用于图片相似度判定的模型任务名，建议填写支持图片理解的模型",
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

    async def on_unload(self) -> None:
        """插件卸载回调。"""

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        """插件配置更新回调。"""

        del scope
        del config_data
        del version

    def _resolve_identity_image(self) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """解析人设图片。"""

        configured_base64 = self.config.identity_image.image_base64.strip()
        configured_format = self.config.identity_image.image_format.strip().lower() or "png"
        if configured_base64:
            return configured_format, configured_base64, None

        configured_path = self.config.identity_image.image_path.strip()
        if not configured_path:
            return None, None, "尚未配置 Bot 的人设图片。"

        image_path = Path(configured_path)
        if not image_path.is_absolute():
            image_path = (self.plugin_dir / image_path).resolve()
        if not image_path.exists() or not image_path.is_file():
            return None, None, f"人设图片不存在：{image_path}"

        image_result = _read_image_file(image_path)
        if image_result is None:
            return None, None, f"人设图片读取失败：{image_path}"
        image_format, image_base64 = image_result
        return image_format, image_base64, None

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

    async def _resolve_compare_model(self) -> str:
        """选择图片比对模型。"""

        preferred_model = self.config.identity_image.compare_model.strip()
        available_models = await self.ctx.llm.get_available_models()
        if preferred_model and preferred_model in available_models:
            return preferred_model
        if "utils" in available_models:
            return "utils"
        if preferred_model:
            return preferred_model
        if available_models:
            return available_models[0]
        return ""

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
        "identiy_myself_in_pic",
        description="当有人发送人物或角色图片时，使用此工具来检查该角色是否与自身一致",
        parameters=[
            _tool_param("msg_id", ToolParamType.STRING, "要比对的消息 ID", True),
        ],
    )
    async def handle_identiy_myself_in_pic(
        self,
        msg_id: str = "",
        stream_id: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """判定目标图片与 Bot 人设图的差异度。"""

        normalized_msg_id = msg_id.strip()
        normalized_stream_id = stream_id.strip()
        tool_debug_info: Dict[str, Any] = {
            "msg_id": normalized_msg_id,
            "stream_id_present": bool(normalized_stream_id),
            "target_message_found": False,
        }
        logger.info(
            "identiy_myself_in_pic 工具调用开始：msg_id=%s stream_id_present=%s kwargs_keys=%s",
            normalized_msg_id,
            bool(normalized_stream_id),
            sorted(str(key) for key in kwargs.keys()),
        )
        if not normalized_msg_id:
            logger.info("identiy_myself_in_pic 工具调用失败：缺少 msg_id")
            return _build_identity_tool_unavailable_result("缺少 msg_id，无法进行图片比对。", tool_debug_info)
        if not normalized_stream_id:
            logger.info("identiy_myself_in_pic 工具调用失败：缺少 stream_id，msg_id=%s", normalized_msg_id)
            return _build_identity_tool_unavailable_result(
                "缺少当前会话 stream_id，无法按消息 ID 查找图片。",
                tool_debug_info,
            )

        try:
            identity_format, identity_base64, identity_error = self._resolve_identity_image()
            if identity_error is not None:
                logger.info("identiy_myself_in_pic 人设图解析失败：msg_id=%s reason=%s", normalized_msg_id, identity_error)
                return _build_identity_tool_unavailable_result(identity_error, tool_debug_info)
            tool_debug_info["identity_image_format"] = identity_format
            logger.info(
                "identiy_myself_in_pic 人设图解析成功：msg_id=%s identity_format=%s",
                normalized_msg_id,
                identity_format,
            )

            target_message = await self._find_message_by_id(
                normalized_stream_id,
                normalized_msg_id,
            )
            if target_message is None:
                logger.info(
                    "identiy_myself_in_pic 未找到目标消息：msg_id=%s",
                    normalized_msg_id,
                )
                return _build_identity_tool_unavailable_result(
                    f"未找到消息 ID 为 {normalized_msg_id} 的消息。",
                    tool_debug_info,
                )
            tool_debug_info["target_message_found"] = True
            tool_debug_info["target_message_keys"] = sorted(str(key) for key in target_message.keys())
            logger.info(
                "identiy_myself_in_pic 找到目标消息：msg_id=%s message_keys=%s",
                normalized_msg_id,
                tool_debug_info["target_message_keys"],
            )

            target_format, target_base64, target_error, image_debug_info = self._extract_image_from_message_with_debug(
                target_message
            )
            tool_debug_info["image_extract"] = image_debug_info
            if target_error is not None:
                logger.info(
                    "identiy_myself_in_pic 目标图片解析失败：msg_id=%s reason=%s debug=%s",
                    normalized_msg_id,
                    target_error,
                    image_debug_info,
                )
                return _build_identity_tool_unavailable_result(target_error, tool_debug_info)
            tool_debug_info["target_image_format"] = target_format
            logger.info(
                "identiy_myself_in_pic 目标图片解析成功：msg_id=%s target_format=%s",
                normalized_msg_id,
                target_format,
            )

            model_name = await self._resolve_compare_model()
            if not model_name:
                logger.info("identiy_myself_in_pic 无可用模型：msg_id=%s", normalized_msg_id)
                return _build_identity_tool_unavailable_result(
                    "当前没有可用的 LLM 模型，无法执行图片比对。",
                    tool_debug_info,
                )
            tool_debug_info["model"] = model_name
            logger.info("identiy_myself_in_pic 开始调用模型比对：msg_id=%s model=%s", normalized_msg_id, model_name)

            prompt_messages: List[Dict[str, Any]] = [
                {
                    "role": "system",
                    "content": (
                        "你是一个图片差异分析助手。"
                        "请比较两张图片是否在表达同一个角色、同一个人设或同一视觉身份。"
                        "请同时关注相似点和差异点，重点参考发型、瞳色、服装、配饰、主题元素、色彩与整体气质。"
                        "不要因为姿势、背景、裁剪或轻微画风变化就直接判定完全不同。"
                        "只输出 JSON，不要输出额外解释。"
                        'JSON 格式必须为 {"similar": true/false, "difference_level": "low|medium|high", "confidence": "high|medium|low", "reason": "...", "matched_points": ["..."], "difference_points": ["..."]}。'
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "第一张图是 Bot 的人设图。"},
                        {
                            "type": "image",
                            "image_format": identity_format,
                            "image_base64": identity_base64,
                        },
                        {"type": "text", "text": "第二张图是待判定消息中的图片。"},
                        {
                            "type": "image",
                            "image_format": target_format,
                            "image_base64": target_base64,
                        },
                        {
                            "type": "text",
                            "text": "请判断这两张图的人设差异度，并给出是否相似、差异等级、相似点、差异点和总体原因。",
                        },
                    ],
                },
            ]

            llm_result = await self.ctx.llm.generate(
                prompt=prompt_messages,
                model=model_name,
                temperature=0.1,
                max_tokens=600,
            )
            if not llm_result.get("success"):
                tool_debug_info["llm_error"] = str(llm_result.get("error") or "").strip()
                logger.info(
                    "identiy_myself_in_pic 模型调用失败：msg_id=%s model=%s error=%s",
                    normalized_msg_id,
                    model_name,
                    tool_debug_info["llm_error"] or "模型调用失败",
                )
                return _build_identity_tool_unavailable_result(
                    f"图片差异度判定失败：{llm_result.get('error') or '模型调用失败'}",
                    tool_debug_info,
                )

            response_text, reasoning_text = _extract_llm_text_pair(llm_result)
            parsed_result = _extract_json_object(response_text)
            parsed_from = "response"
            if not parsed_result and reasoning_text:
                parsed_result = _extract_json_object(reasoning_text)
                parsed_from = "reasoning"

            tool_debug_info["llm_response"] = {
                "result_keys": sorted(str(key) for key in llm_result.keys()),
                "response_len": len(response_text),
                "reasoning_len": len(reasoning_text),
                "response_preview": _safe_preview(response_text),
                "reasoning_preview": _safe_preview(reasoning_text),
                "parsed_from": parsed_from if parsed_result else "",
                "parsed_keys": sorted(str(key) for key in parsed_result.keys()),
            }
            logger.info(
                "identiy_myself_in_pic 模型返回解析：msg_id=%s model=%s response_len=%s reasoning_len=%s parsed_keys=%s parsed_from=%s",
                normalized_msg_id,
                model_name,
                len(response_text),
                len(reasoning_text),
                tool_debug_info["llm_response"]["parsed_keys"],
                tool_debug_info["llm_response"]["parsed_from"],
            )
            if not parsed_result:
                return _build_identity_tool_unavailable_result(
                    "图片差异度判定失败：模型调用成功，但没有返回可解析的 JSON 判定结果。",
                    tool_debug_info,
                )

            similar = _coerce_bool(_pick_first_present(parsed_result, ["similar", "is_similar", "same", "是否相似"]))
            difference_level = _normalize_choice(
                _pick_first_present(parsed_result, ["difference_level", "diff_level", "difference", "差异度"]),
                ["low", "medium", "high"],
                {
                    "低": "low",
                    "较低": "low",
                    "小": "low",
                    "中": "medium",
                    "中等": "medium",
                    "一般": "medium",
                    "高": "high",
                    "较高": "high",
                    "大": "high",
                },
                "unknown",
            )
            confidence = _normalize_choice(
                _pick_first_present(parsed_result, ["confidence", "置信度"]),
                ["low", "medium", "high"],
                {
                    "低": "low",
                    "较低": "low",
                    "中": "medium",
                    "中等": "medium",
                    "高": "high",
                    "较高": "high",
                },
                "unknown",
            )
            reason = str(_pick_first_present(parsed_result, ["reason", "summary", "explanation", "总体说明", "原因"], "")).strip()
            if not reason:
                return _build_identity_tool_unavailable_result(
                    "图片差异度判定失败：模型返回 JSON 中缺少 reason/原因。",
                    tool_debug_info,
                )
            matched_points = _to_plain_list(_pick_first_present(parsed_result, ["matched_points", "similar_points", "相似点"], []))
            difference_points = _to_plain_list(
                _pick_first_present(parsed_result, ["difference_points", "different_points", "差异点"], [])
            )

            content_lines = [
                f"判定结果：{'相似' if similar else '不相似'}",
                f"差异度：{difference_level}",
                f"置信度：{confidence}",
                f"总体说明：{reason}",
            ]
            if matched_points:
                content_lines.append(f"相似点：{'、'.join(matched_points)}")
            if difference_points:
                content_lines.append(f"差异点：{'、'.join(difference_points)}")

            return {
                "success": True,
                "content": "\n".join(content_lines),
                "similar": similar,
                "difference_level": difference_level,
                "confidence": confidence,
                "reason": reason,
                "matched_points": matched_points,
                "difference_points": difference_points,
                "target_message_id": normalized_msg_id,
                "model": llm_result.get("model", model_name),
                "raw_response": response_text,
            }
        except Exception as exc:
            tool_debug_info["exception"] = f"{type(exc).__name__}: {exc}"
            logger.info(
                "identiy_myself_in_pic 工具异常：msg_id=%s error=%s",
                normalized_msg_id,
                tool_debug_info["exception"],
                exc_info=True,
            )
            return _build_identity_tool_unavailable_result(
                f"图片比对工具暂时不可用：{type(exc).__name__}: {exc}",
                tool_debug_info,
            )


def create_plugin() -> SelfIdentityPlugin:
    """创建插件实例。"""

    return SelfIdentityPlugin()
