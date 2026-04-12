"""Prompt 构建：从 OpenAI messages 提取对话并拼装为发给模型的文本。"""

from __future__ import annotations

import json
from typing import Any

from core.shared.session_markers import strip_session_id_suffix
from core.shared.models import OpenAIContentPart, OpenAIMessage


def _norm_content(c: str | list[OpenAIContentPart] | None) -> str:
    """将 content 转为单段字符串。仅支持官方格式：字符串或 type=text 的 content part（取 text 字段）。"""
    if c is None:
        return ""
    if isinstance(c, str):
        return strip_session_id_suffix(c)
    if not isinstance(c, list):
        return ""
    return strip_session_id_suffix(
        " ".join(
            p.text or ""
            for p in c
            if isinstance(p, OpenAIContentPart) and p.type == "text" and p.text
        )
    )


# 追加在用户消息末尾：并行多工具时要求 `<tool_calls>` 终结块
TAGGED_TOOL_STRICT_SUFFIX = (
    "(严格工具协议模式;如需调用工具, 只能输出 <think> 与一个终结块标签, "
    "终结块只能是 <tool_calls> 或 <final_answer>, 禁止输出标签外文本或替代方案)"
)

# 单工具调用：`parallel_tool_calls=False` 时用 `<tool_call>` 终结块
TAGGED_TOOL_STRICT_SUFFIX_SINGLE = (
    "(严格工具协议模式;如需调用工具, 只能输出 <think> 与一个终结块标签, "
    "终结块只能是 <tool_call> 或 <final_answer>, 禁止输出标签外文本或替代方案)"
)


def extract_user_content(
    messages: list[OpenAIMessage],
    *,
    has_tools: bool = False,
    tagged_prompt_prefix: str = "",
    allow_parallel_tool_calls: bool = True,
    full_history: bool = False,
) -> str:
    """
    从 messages 中提取对话，拼成发给模型的 prompt。
    网页/会话侧已有完整历史，只取尾部：最后一条为 user 时，从后向前找到最后一个 assistant（不包含），
    取该 assistant 之后到末尾；最后一条为 tool 时，从后向前找到最后一个 user（不包含），取该 user 之后到末尾。
    支持 user、assistant、tool 角色；assistant 的 tool_calls 与 tool 结果会拼回。
    Tagged tool 模式：完整工具协议 Prompt 仅第一次对话传入（按完整 messages 判断 is_first_turn）；
    后续只传尾部内容。
    """
    if not messages:
        return ""

    parts: list[str] = []  # 按顺序拼接的多段文本，最后 join

    # 首轮：messages 里还没有 assistant/tool，需要把完整 tagged 协议前缀插入一次
    is_first_turn = not any(m.role in ("assistant", "tool") for m in messages)
    if has_tools and tagged_prompt_prefix and (full_history or is_first_turn):
        parts.append(tagged_prompt_prefix)

    if full_history:
        tail = messages
    else:
        # 站点侧已有历史：只截取「当前轮」需发给模型的尾部片段
        last = messages[-1]
        if last.role == "user":
            i = len(messages) - 1
            while i >= 0 and messages[i].role != "assistant":
                i -= 1
            tail = messages[i + 1 :]
        elif last.role == "tool":
            i = len(messages) - 1
            while i >= 0 and messages[i].role != "user":
                i -= 1
            tail = messages[i + 1 :]
        else:
            tail = messages[-2:]

    for m in tail:
        if m.role == "system":
            txt = _norm_content(m.content)
            if txt:
                parts.append(f"System：{txt}")
        elif m.role == "user":
            txt = _norm_content(m.content)
            if txt:
                if has_tools:
                    strict_suffix = (
                        TAGGED_TOOL_STRICT_SUFFIX
                        if allow_parallel_tool_calls
                        else TAGGED_TOOL_STRICT_SUFFIX_SINGLE
                    )
                    parts.append(f"**User**: {txt} {strict_suffix}")
                else:
                    parts.append(f"User：{txt}")
        elif m.role == "assistant":
            tool_calls_list = list(m.tool_calls or [])
            if tool_calls_list:
                content_text = _norm_content(m.content)
                if content_text:
                    parts.append(f"**Assistant**:\n\n{content_text}")
                replay_payloads: list[dict[str, Any]] = []
                call_ids: list[str] = []
                for tc in tool_calls_list:
                    fn = tc.get("function") or {}
                    call_id = tc.get("id", "")
                    if call_id:
                        call_ids.append(str(call_id))
                    name = fn.get("name", "")
                    args = fn.get("arguments", "{}")
                    if isinstance(args, str):
                        try:
                            args_obj = json.loads(args)
                        except json.JSONDecodeError:
                            args_obj = {"raw": args}
                    elif isinstance(args, dict):
                        args_obj = args
                    else:
                        args_obj = {"raw": str(args)}
                    replay_payloads.append(
                        {
                            "name": name,
                            "arguments": args_obj,
                        }
                    )
                if len(replay_payloads) == 1 and not allow_parallel_tool_calls:
                    label = (
                        f"**Assistant(Call ID: {call_ids[0]})**:"
                        if call_ids
                        else "**Assistant**:"
                    )
                    parts.append(
                        label
                        + "\n\n<tool_call>"
                        + json.dumps(replay_payloads[0], ensure_ascii=False)
                        + "</tool_call>"
                    )
                else:
                    if not call_ids:
                        label = "**Assistant**:"
                    elif len(call_ids) == 1:
                        label = f"**Assistant(Call ID: {call_ids[0]})**:"
                    else:
                        label = "**Assistant(Call IDs: " + ", ".join(call_ids) + ")**:"
                    parts.append(
                        label
                        + "\n\n<tool_calls>"
                        + json.dumps(replay_payloads, ensure_ascii=False)
                        + "</tool_calls>"
                    )
            else:
                txt = _norm_content(m.content)
                if txt:
                    if has_tools:
                        parts.append(f"**Assistant**:\n\n{txt}")
                    else:
                        parts.append(f"Assistant：{txt}")
        elif m.role == "tool":
            txt = _norm_content(m.content)
            call_id = m.tool_call_id or ""
            terminal_desc = (
                "<tool_calls>[...]</tool_calls>"
                if allow_parallel_tool_calls
                else "<tool_call>{...}</tool_call>"
            )
            single_tool_rule = (
                "If only one tool is needed, still use a JSON array with one item.\n"
                if allow_parallel_tool_calls
                else ""
            )
            json_target = "<tool_calls>" if allow_parallel_tool_calls else "<tool_call>"
            parts.append(
                f"Tool result for call_id={call_id}:\n"
                "<tool_result>\n"
                f"{txt}\n"
                "</tool_result>\n\n"
                "Now output exactly one response using only the tagged protocol:\n"
                "- optional <think>...</think>\n"
                f"- then exactly one {terminal_desc} or "
                "<final_answer>...</final_answer>\n"
                "Do not output Observation.\n"
                "Do not output <tool_result>.\n"
                "Do not output tool results.\n"
                "Do not output a second terminal block.\n"
                f"Stop immediately after </{'tool_calls' if allow_parallel_tool_calls else 'tool_call'}> "
                "or </final_answer>.\n"
                f"Inside {json_target}, the content must be valid JSON.\n"
                f"{single_tool_rule}"
                "If a string value contains quotes, backslashes, or newlines, "
                "escape them exactly as JSON requires."
            )
    return "\n".join(parts)
