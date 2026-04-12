"""
Mock Claude API：与 claude.py 调用格式兼容，不消耗 token。
设置 CLAUDE_START_URL 和 CLAUDE_API_BASE 指向 http://ip:port/mock 即可调试。
"""

import asyncio
import json
import uuid as uuid_mod
from collections.abc import AsyncIterator

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, StreamingResponse

router = APIRouter(prefix="/mock", tags=["mock"])

MOCK_ORG_UUID = "00000000-0000-0000-0000-000000000001"

# 自定义回复：请求来时在终端用多行输入要回复的内容
INPUT_PROMPT = "Mock 回复内容（支持多行，空行结束）:"


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def mock_start_page() -> str:
    """CLAUDE_START_URL 指向 /mock 时，浏览器加载此页。"""
    return """
<!DOCTYPE html>
<html><head><title>Mock Claude</title></head>
<body><p>Mock Claude - 调试用</p></body>
</html>
"""


@router.get("/account")
def mock_account() -> dict:
    """_get_org_uuid 调用的 GET /account，返回 memberships 含 org uuid。"""
    return {
        "memberships": [
            {"organization": {"uuid": MOCK_ORG_UUID}},
        ],
    }


@router.post("/organizations/{org_uuid}/chat_conversations")
def mock_create_conversation(org_uuid: str) -> dict:
    """_post_create_conversation 调用的创建会话接口。"""
    return {
        "uuid": str(uuid_mod.uuid4()),
    }


def _read_reply_from_stdin() -> str:
    """在终端通过多次 input 读取多行回复内容（空行结束，阻塞，应在线程中调用）。"""
    print(INPUT_PROMPT, flush=True)
    print("直接粘贴多行文本，最后再按一次回车输入空行结束。", flush=True)
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        # 空行表示输入结束
        if line == "":
            break
        lines.append(line)
    return "\n".join(lines).rstrip()


@router.post("/organizations/{org_uuid}/chat_conversations/{conv_uuid}/completion")
async def mock_completion(
    org_uuid: str,
    conv_uuid: str,  # noqa: ARG001
) -> StreamingResponse:
    """stream_completion 调用的 completion 接口，返回 SSE 流。请求来时在终端 input 输入回复内容。"""

    # 在线程中执行 input，避免阻塞事件循环
    reply_text = await asyncio.to_thread(_read_reply_from_stdin)

    async def sse_stream() -> AsyncIterator[str]:
        msg_uuid = str(uuid_mod.uuid4())
        # message_start
        yield f"data: {json.dumps({'type': 'message_start', 'message': {'id': msg_uuid, 'uuid': msg_uuid, 'model': 'claude-sonnet-4-5-20250929', 'type': 'message', 'role': 'assistant'}})}\n\n"
        # content_block_start
        yield f"data: {json.dumps({'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}})}\n\n"
        # content_block_delta 分块流式输出
        chunk_size = 2
        for i in range(0, len(reply_text), chunk_size):
            chunk = reply_text[i : i + chunk_size]
            yield f"data: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': chunk}})}\n\n"
            await asyncio.sleep(0.05)
        # content_block_stop
        yield f"data: {json.dumps({'type': 'content_block_stop', 'index': 0})}\n\n"
        # message_stop
        yield f"data: {json.dumps({'type': 'message_stop'})}\n\n"

    return StreamingResponse(
        sse_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
