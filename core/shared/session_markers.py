"""
会话 ID 携带方式：任意字符串 → base64 → 零宽字符编码，用特殊零宽标记组包裹。
从对话内容中通过正则匹配起止标记提取会话 ID，与 session_id 的具体格式无关。

编码协议：
  session_id (utf-8)
    → base64 (A-Za-z0-9+/=，最多 65 个不同符号)
    → 每个 base64 字符用 3 位 base-5 零宽字符表示（5³=125 ≥ 65）
    → 有效索引范围 0..64（64 个字符 + padding），故三元组首位最大为 2（3*25=75 > 64）
    → 因此首位为 ZW[3] 或 ZW[4] 的三元组绝不出现在正文中
    → HEAD_MARK/TAIL_MARK 正是利用首位 ≥ 3 的三元组构造，保证不会误中正文
"""

import base64
import re
from typing import Any

# 零宽字符集（5 个字符，基数 5，索引 0-4）
_ZERO_WIDTH = (
    "\u200b",  # 零宽空格            → 0
    "\u200c",  # 零宽非连接符        → 1
    "\u200d",  # 零宽连接符          → 2
    "\ufeff",  # 零宽非断空格        → 3
    "\u180e",  # 蒙古文元音分隔符    → 4
)
_ZW_SET = frozenset(_ZERO_WIDTH)
_ZW_TO_IDX = {c: i for i, c in enumerate(_ZERO_WIDTH)}

# base64 标准字符集（64 个字符），padding 符 "=" 用索引 64 表示
_B64_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_B64_TO_IDX = {c: i for i, c in enumerate(_B64_CHARS)}
_PAD_IDX = 64  # "=" 的编码索引

# 起止标记：首位均为 ZW[3] 或 ZW[4]，保证不出现在 payload 三元组中
_HEAD_MARK = _ZERO_WIDTH[4] * 3 + _ZERO_WIDTH[3] * 3  # 6 个零宽字符
_TAIL_MARK = _ZERO_WIDTH[3] * 3 + _ZERO_WIDTH[4] * 3  # 6 个零宽字符

_ZW_CLASS = r"[\u200b\u200c\u200d\ufeff\u180e]"


def _encode_b64idx(idx: int) -> str:
    """将 base64 字符索引 (0-64) 编码为 3 个零宽字符（3 位 base-5）。"""
    a = idx // 25
    r = idx % 25
    b = r // 5
    c = r % 5
    return _ZERO_WIDTH[a] + _ZERO_WIDTH[b] + _ZERO_WIDTH[c]


def _decode_b64idx(zw3: str) -> int | None:
    """将 3 个零宽字符解码为 base64 字符索引（0-64），非法返回 None。"""
    if len(zw3) != 3:
        return None
    a = _ZW_TO_IDX.get(zw3[0])
    b = _ZW_TO_IDX.get(zw3[1])
    c = _ZW_TO_IDX.get(zw3[2])
    if a is None or b is None or c is None:
        return None
    val = a * 25 + b * 5 + c
    if val > 64:
        return None
    return val


def encode_session_id(session_id: str) -> str:
    """
    将任意字符串会话 ID 编码为不可见的零宽序列：
    HEAD_MARK + zero_width_encoded(base64(utf-8(session_id))) + TAIL_MARK
    """
    b64 = base64.b64encode(session_id.encode()).decode()
    out: list[str] = []
    for ch in b64:
        if ch == "=":
            out.append(_encode_b64idx(_PAD_IDX))
        else:
            idx = _B64_TO_IDX.get(ch)
            if idx is None:
                return ""
            out.append(_encode_b64idx(idx))
    return _HEAD_MARK + "".join(out) + _TAIL_MARK


def decode_session_id(text: str) -> str | None:
    """
    从文本中提取第一个被标记包裹的会话 ID（解码零宽 → base64 → utf-8）。
    若未找到有效标记或解码失败则返回 None。
    """
    m = re.search(
        re.escape(_HEAD_MARK) + r"(" + _ZW_CLASS + r"+?)" + re.escape(_TAIL_MARK),
        text,
    )
    if not m:
        return None
    body = m.group(1)
    if len(body) % 3 != 0:
        return None
    b64_chars: list[str] = []
    for i in range(0, len(body), 3):
        idx = _decode_b64idx(body[i : i + 3])
        if idx is None:
            return None
        b64_chars.append("=" if idx == _PAD_IDX else _B64_CHARS[idx])
    try:
        return base64.b64decode("".join(b64_chars)).decode()
    except Exception:
        return None


def decode_latest_session_id(text: str) -> str | None:
    """
    从文本中提取最后一个被标记包裹的会话 ID。
    用于客户端保留完整历史时，优先命中最近一次返回的 session_id。
    """
    matches = list(
        re.finditer(
            re.escape(_HEAD_MARK) + r"(" + _ZW_CLASS + r"+?)" + re.escape(_TAIL_MARK),
            text,
        )
    )
    if not matches:
        return None
    body = matches[-1].group(1)
    if len(body) % 3 != 0:
        return None
    b64_chars: list[str] = []
    for i in range(0, len(body), 3):
        idx = _decode_b64idx(body[i : i + 3])
        if idx is None:
            return None
        b64_chars.append("=" if idx == _PAD_IDX else _B64_CHARS[idx])
    try:
        return base64.b64decode("".join(b64_chars)).decode()
    except Exception:
        return None


def extract_session_id_marker(text: str) -> str:
    """
    从文本中提取完整的零宽会话 ID 标记段（HEAD_MARK + body + TAIL_MARK），
    用于在 tool_calls 的 text_content 中携带会话 ID 至下一轮对话。
    若未找到则返回空字符串。
    """
    m = re.search(
        re.escape(_HEAD_MARK) + _ZW_CLASS + r"+?" + re.escape(_TAIL_MARK),
        text,
    )
    return m.group(0) if m else ""


def session_id_suffix(session_id: str) -> str:
    """返回响应末尾需附加的不可见标记（含 HEAD/TAIL 包裹的零宽编码会话 ID）。"""
    return encode_session_id(session_id)


def strip_session_id_suffix(text: str) -> str:
    """去掉文本中所有零宽会话 ID 标记段（HEAD_MARK...TAIL_MARK），返回干净正文。"""
    return re.sub(
        re.escape(_HEAD_MARK) + _ZW_CLASS + r"+?" + re.escape(_TAIL_MARK),
        "",
        text,
    )


def _normalize_content(content: str | list[Any]) -> str:
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for p in content:
        if isinstance(p, dict) and p.get("type") == "text" and "text" in p:
            parts.append(str(p["text"]))
        elif isinstance(p, str):
            parts.append(p)
    return " ".join(parts)


def parse_conv_uuid_from_messages(messages: list[dict[str, Any]]) -> str | None:
    """从 messages 中解析最新会话 ID（从最后一条带标记的消息开始逆序查找）。"""
    for m in reversed(messages):
        content = m.get("content")
        if content is None:
            continue
        text = _normalize_content(content)
        decoded = decode_latest_session_id(text)
        if decoded is not None:
            return decoded
    return None
