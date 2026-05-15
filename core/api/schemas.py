"""OpenAI 兼容的请求/响应模型。"""

import json
from typing import Any

from pydantic import BaseModel, Field

from core.api.conv_parser import strip_session_id_suffix


class OpenAIContentPart(BaseModel):
    type: str
    text: str | None = None
    image_url: dict[str, Any] | str | None = None


class InputAttachment(BaseModel):
    filename: str
    mime_type: str
    data: bytes


class OpenAIMessage(BaseModel):
    role: str = Field(..., description="system | user | assistant | tool")
    content: str | list[OpenAIContentPart] | None = ""
    tool_calls: list[dict[str, Any]] | None = Field(
        default=None, description="assistant 发起的工具调用"
    )
    tool_call_id: str | None = Field(
        default=None, description="tool 消息对应的 call id"
    )

    model_config = {"extra": "allow"}


class OpenAIChatRequest(BaseModel):
    """OpenAI Chat Completions API 兼容请求体。"""

    model: str = Field(default="", description="模型名，可忽略")
    messages: list[OpenAIMessage] = Field(..., description="对话列表")
    stream: bool = Field(default=False, description="是否流式返回")
    tools: list[dict] | None = Field(
        default=None,
        description='工具列表，每项为 {"type":"function","function":{name,description,parameters,strict?}}',
    )
    tool_choice: str | dict | None = Field(
        default=None,
        description='工具选择: "auto"|"required"|"none" 或 {"type":"function","name":"xxx"}',
    )
    parallel_tool_calls: bool | None = Field(
        default=None,
        description="是否允许单次响应中并行多个 tool_call，false 时仅 0 或 1 个",
    )
    web2api_account: str | None = Field(
        default=None,
        description="请求级固定账号；可填写账号名、fingerprint_id 或 fingerprint_id:name。",
    )
    resume_session_id: str | None = Field(default=None, exclude=True)
    attachment_files: list[InputAttachment] = Field(
        default_factory=list,
        exclude=True,
        description="本次实际要发送给站点的附件，由 ChatHandler 根据 full_history 选择来源填充。",
    )
    # 仅供内部调度使用：最后一条 user 消息里的附件 & 所有 user 消息里的附件
    attachment_files_last_user: list[InputAttachment] = Field(
        default_factory=list, exclude=True
    )
    attachment_files_all_users: list[InputAttachment] = Field(
        default_factory=list, exclude=True
    )


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


TAGGED_TOOL_STRICT_SUFFIX = (
    "(严格工具协议模式;如需调用工具, 只能输出 <think> 与一个终结块标签, "
    "终结块只能是 <tool_calls> 或 <final_answer>, 禁止输出标签外文本或替代方案)"
)

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

    parts: list[str] = []

    # 重建会话时会把完整历史重新回放给站点，因此 tools 指令也需要重新注入。
    is_first_turn = not any(m.role in ("assistant", "tool") for m in messages)
    if has_tools and tagged_prompt_prefix and (full_history or is_first_turn):
        parts.append(tagged_prompt_prefix)

    if full_history:
        tail = messages
    else:
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
                        label = (
                            "**Assistant(Call IDs: "
                            + ", ".join(call_ids)
                            + ")**:"
                        )
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
