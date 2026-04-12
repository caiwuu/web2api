"""OpenAI 兼容的请求/响应模型。"""

from typing import Any

from pydantic import BaseModel, Field


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
    resume_session_id: str | None = Field(default=None, exclude=True)
    attachment_files: list[InputAttachment] = Field(
        default_factory=list,
        exclude=True,
        description="本次实际要发送给站点的附件，由 ChatHandler 根据 full_history 选择来源填充。",
    )
    attachment_files_last_user: list[InputAttachment] = Field(
        default_factory=list, exclude=True
    )
    attachment_files_all_users: list[InputAttachment] = Field(
        default_factory=list, exclude=True
    )
