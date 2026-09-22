import os
import base64
import hashlib
import hmac
import json
import urllib.request
import urllib.error

from fastapi import FastAPI, Request, HTTPException

app = FastAPI()

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# 每位 LINE 使用者各自保存上一個 OpenAI Response ID
# 注意：Render 重啟或休眠後，這些暫存記憶會消失
user_conversations = {}


@app.get("/")
def home():
    return {"status": "LINE AI Bot is running"}


def ask_ai(user_id: str, user_text: str) -> str:
    if not OPENAI_API_KEY:
        return "AI 尚未設定完成。"

    request_data = {
        "model": "gpt-5.6-luna",
        "instructions": (
            "你是一位實用、準確的 AI 助理。"
            "請使用繁體中文回答，除非使用者要求其他語言。"
            "回答適合在 LINE 上閱讀，避免不必要的冗長內容。"
            "請根據目前對話上下文自然延續回答。"
        ),
        "input": user_text
    }

    # 如果這位 LINE 使用者之前聊過，
    # 就把上一個 Response 接到這一次
    previous_response_id = user_conversations.get(user_id)

    if previous_response_id:
        request_data["previous_response_id"] = previous_response_id

    data = json.dumps(request_data).encode("utf-8")

    req = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=data,
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            result = json.loads(
                response.read().decode("utf-8")
            )

        # 記住這次 Response ID，供下一句延續
        response_id = result.get("id")

        if response_id:
            user_conversations[user_id] = response_id

        # 找出 AI 回覆文字
        for item in result.get("output", []):
            if item.get("type") == "message":
                for content in item.get("content", []):
                    if content.get("type") == "output_text":
                        return content.get(
                            "text",
                            "AI 沒有產生回答。"
                        )

        return "AI 沒有產生回答。"

    except urllib.error.HTTPError as e:
        error_body = e.read().decode(
            "utf-8",
            errors="replace"
        )

        print(
            f"OpenAI API error {e.code}: "
            f"{error_body}"
        )

        return "AI 暫時無法回答，請稍後再試。"

    except Exception as e:
        print(
            f"OpenAI error: "
            f"{type(e).__name__}: {e}"
        )

        return "AI 暫時發生錯誤，請稍後再試。"


def reply_line(reply_token: str, text: str):
    if not LINE_CHANNEL_ACCESS_TOKEN:
        raise RuntimeError(
            "LINE_CHANNEL_ACCESS_TOKEN is not configured"
        )

    text = text[:4900]

    data = json.dumps({
        "replyToken": reply_token,
        "messages": [
            {
                "type": "text",
                "text": text
            }
        ]
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.line.me/v2/bot/message/reply",
        data=data,
        headers={
            "Authorization":
                f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(
            req,
            timeout=15
        ) as response:
            response.read()

    except urllib.error.HTTPError as e:
        error_body = e.read().decode(
            "utf-8",
            errors="replace"
        )

        print(
            f"LINE API error {e.code}: "
            f"{error_body}"
        )

    except Exception as e:
        print(
            f"LINE error: "
            f"{type(e).__name__}: {e}"
        )


@app.post("/webhook")
async def webhook(request: Request):
    body = await request.body()
    signature = request.headers.get(
        "x-line-signature"
    )

    if not LINE_CHANNEL_SECRET:
        raise HTTPException(
            status_code=500,
            detail="LINE_CHANNEL_SECRET is not configured"
        )

    if not signature:
        raise HTTPException(
            status_code=400,
            detail="Missing LINE signature"
        )

    digest = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"),
        body,
        hashlib.sha256
    ).digest()

    expected_signature = base64.b64encode(
        digest
    ).decode("utf-8")

    if not hmac.compare_digest(
        signature,
        expected_signature
    ):
        raise HTTPException(
            status_code=400,
            detail="Invalid signature"
        )

    payload = json.loads(
        body.decode("utf-8")
    )

    for event in payload.get("events", []):
        if (
            event.get("type") == "message"
            and event.get("message", {}).get("type") == "text"
            and event.get("replyToken")
        ):
            user_text = event["message"]["text"]

            # 取得 LINE 使用者 ID
            user_id = (
                event.get("source", {}).get("userId")
                or event.get("source", {}).get("groupId")
                or event.get("source", {}).get("roomId")
                or "unknown"
            )

            # /clear 清除目前對話
            if user_text.strip().lower() == "/clear":
                user_conversations.pop(
                    user_id,
                    None
                )

                reply_line(
                    event["replyToken"],
                    "對話記憶已清除。"
                )

                continue

            ai_answer = ask_ai(
                user_id,
                user_text
            )

            reply_line(
                event["replyToken"],
                ai_answer
            )

    return {"status": "ok"}
