from __future__ import annotations

from pai.tools import Tool, ToolContext


async def send_telegram(args: dict, ctx: ToolContext) -> str:
    await ctx.send(args["text"])
    return "sent"


TOOLS = [
    Tool(
        name="send_telegram",
        description=(
            "Send a separate Telegram message to the owner right now, in addition to your "
            "normal reply. Use it only for something that should arrive as its own message, "
            "such as a long list or a document-like summary. Your final answer is sent "
            "automatically, so never use this to send the answer itself."
        ),
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string", "description": "Message text (Markdown)."}},
            "required": ["text"],
            "additionalProperties": False,
        },
        fn=send_telegram,
    )
]
