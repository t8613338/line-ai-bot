import os
import base64
import hashlib
import hmac
import json
import urllib.request
import urllib.error

import psycopg
from fastapi import FastAPI, Request, HTTPException

app = FastAPI()

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")


# =========================================================
# 基本狀態
# =========================================================

@app.get("/")
def home():
    return {
        "status": "LINE AI Bot is running",
        "vision": True,
        "memory": True
    }


# =========================================================
# PostgreSQL 對話記憶
# =========================================================

def init_database():
    if not DATABASE_URL:
        print("DATABASE_URL is not configured")
        return

    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS conversations (
                        user_id TEXT PRIMARY KEY,
                        previous_response_id TEXT,
                        updated_at TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
                            cur.execute("""
                CREATE TABLE IF NOT EXISTS api_usage (
                    id BIGSERIAL PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    input_tokens INTEGER DEFAULT 0,
                    output_tokens INTEGER DEFAULT 0,
                    total_tokens INTEGER DEFAULT 0,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            conn.commit()

        print("Database initialized successfully")

    except Exception as e:
        print(
            f"Database init error: "
            f"{type(e).__name__}: {e}"
        )


def get_previous_response_id(user_id: str):
    if not DATABASE_URL:
        return None

    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT previous_response_id
                    FROM conversations
                    WHERE user_id = %s
                    """,
                    (user_id,)
                )

                row = cur.fetchone()

                if row:
                    return row[0]

    except Exception as e:
        print(
            f"Database read error: "
            f"{type(e).__name__}: {e}"
        )

    return None


def save_previous_response_id(
    user_id: str,
    response_id: str
):
    if not DATABASE_URL:
        return

    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO conversations (
                        user_id,
                        previous_response_id,
                        updated_at
                    )
                    VALUES (%s, %s, NOW())

                    ON CONFLICT (user_id)
                    DO UPDATE SET
                        previous_response_id =
                            EXCLUDED.previous_response_id,
                        updated_at = NOW()
                    """,
                    (user_id, response_id)
                )

            conn.commit()

    except Exception as e:
        print(
            f"Database save error: "
            f"{type(e).__name__}: {e}"
        )


def clear_conversation(user_id: str):
    if not DATABASE_URL:
        return

    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM conversations
                    WHERE user_id = %s
                    """,
                    (user_id,)
                )

            conn.commit()

    except Exception as e:
        print(
            f"Database clear error: "
            f"{type(e).__name__}: {e}"
        )


init_database()


# =========================================================
# LINE：下載使用者傳來的圖片
# =========================================================

def download_line_image(message_id: str):
    if not LINE_CHANNEL_ACCESS_TOKEN:
        raise RuntimeError(
            "LINE_CHANNEL_ACCESS_TOKEN is not configured"
        )

    url = (
        "https://api-data.line.me"
        f"/v2/bot/message/{message_id}/content"
    )

    req = urllib.request.Request(
        url,
        headers={
            "Authorization":
                f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"
        },
        method="GET"
    )

    try:
        with urllib.request.urlopen(
            req,
            timeout=30
        ) as response:

            image_bytes = response.read()

            content_type = (
                response.headers.get(
                    "Content-Type",
                    "image/jpeg"
                )
            )

            return image_bytes, content_type

    except urllib.error.HTTPError as e:
        error_body = e.read().decode(
            "utf-8",
            errors="replace"
        )

        print(
            f"LINE image download error "
            f"{e.code}: {error_body}"
        )

        return None, None

    except Exception as e:
        print(
            f"LINE image download error: "
            f"{type(e).__name__}: {e}"
        )

        return None, None


# =========================================================
# OpenAI 共用請求
# =========================================================

def call_openai(
    user_id: str,
    input_content
) -> str:

    if not OPENAI_API_KEY:
        return "AI 尚未設定完成。"

    request_data = {
        "model": "gpt-5.6-luna",
        "instructions": (
            "你是一位實用、準確的 AI 助理。"
            "請使用繁體中文回答，"
            "除非使用者要求其他語言。"
            "回答適合在 LINE 上閱讀，"
            "避免不必要的冗長內容。"
            "請根據目前對話上下文自然延續回答。"
            "如果收到圖片，請仔細分析圖片內容。"
            "可以辨識圖片中的物品、文字、場景、"
            "文件、商品、食物、介面與其他可見資訊。"
            "看不清楚的內容不要猜測，"
            "應明確說明無法確認。"
        ),
        "input": input_content
    }

    previous_response_id = (
        get_previous_response_id(user_id)
    )

    if previous_response_id:
        request_data["previous_response_id"] = (
            previous_response_id
        )

    data = json.dumps(
        request_data
    ).encode("utf-8")

    req = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=data,
        headers={
            "Authorization":
                f"Bearer {OPENAI_API_KEY}",
            "Content-Type":
                "application/json"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(
            req,
            timeout=90
        ) as response:

            result = json.loads(
                response.read().decode("utf-8")
            )

        response_id = result.get("id")

        if response_id:
            save_previous_response_id(
                user_id,
                response_id
            )
# 記錄 OpenAI API Token 使用量
usage = result.get("usage", {})

input_tokens = usage.get("input_tokens", 0)
output_tokens = usage.get("output_tokens", 0)
total_tokens = usage.get(
    "total_tokens",
    input_tokens + output_tokens
)

if DATABASE_URL:
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO api_usage (
                        user_id,
                        input_tokens,
                        output_tokens,
                        total_tokens
                    )
                    VALUES (%s, %s, %s, %s)
                    """,
                    (
                        user_id,
                        input_tokens,
                        output_tokens,
                        total_tokens
                    )
                )
            conn.commit()
    except Exception as e:
        print(
            f"API usage save error: "
            f"{type(e).__name__}: {e}"
        )
        for item in result.get("output", []):
            if item.get("type") == "message":
                for content in item.get(
                    "content",
                    []
                ):
                    if (
                        content.get("type")
                        == "output_text"
                    ):
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


# =========================================================
# 純文字 AI
# =========================================================

def ask_ai(
    user_id: str,
    user_text: str
) -> str:

    return call_openai(
        user_id,
        user_text
    )


# =========================================================
# 圖片 AI
# =========================================================

def ask_ai_with_image(
    user_id: str,
    image_bytes: bytes,
    content_type: str
) -> str:

    if not image_bytes:
        return "無法取得圖片內容，請重新傳送圖片。"

    if not content_type:
        content_type = "image/jpeg"

    # 防止 Content-Type 帶額外參數
    content_type = (
        content_type.split(";")[0].strip()
    )

    image_base64 = base64.b64encode(
        image_bytes
    ).decode("utf-8")

    data_url = (
        f"data:{content_type};base64,"
        f"{image_base64}"
    )

    input_content = [
        {
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": (
                        "請分析這張圖片。"
                        "先說明圖片的主要內容；"
                        "如果圖片中有清楚可辨識的文字，"
                        "請一併讀取並整理。"
                        "如果使用者之後繼續詢問這張圖片，"
                        "請保留圖片上下文。"
                    )
                },
                {
                    "type": "input_image",
                    "image_url": data_url,
                    "detail": "auto"
                }
            ]
        }
    ]

    return call_openai(
        user_id,
        input_content
    )


# =========================================================
# LINE 回覆
# =========================================================

def reply_line(
    reply_token: str,
    text: str
):
    if not LINE_CHANNEL_ACCESS_TOKEN:
        raise RuntimeError(
            "LINE_CHANNEL_ACCESS_TOKEN "
            "is not configured"
        )

    # LINE 單則文字保守控制長度
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
                f"Bearer "
                f"{LINE_CHANNEL_ACCESS_TOKEN}",
            "Content-Type":
                "application/json"
        },
        method="POST"
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


# =========================================================
# LINE Webhook
# =========================================================

@app.post("/webhook")
async def webhook(request: Request):

    body = await request.body()

    signature = request.headers.get(
        "x-line-signature"
    )

    if not LINE_CHANNEL_SECRET:
        raise HTTPException(
            status_code=500,
            detail=(
                "LINE_CHANNEL_SECRET "
                "is not configured"
            )
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

    expected_signature = (
        base64.b64encode(
            digest
        ).decode("utf-8")
    )

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

    for event in payload.get(
        "events",
        []
    ):

        if event.get("type") != "message":
            continue

        if not event.get("replyToken"):
            continue

        message = event.get(
            "message",
            {}
        )

        message_type = message.get("type")

        source = event.get(
            "source",
            {}
        )

        # 優先使用真正的 LINE userId
        user_id = source.get("userId")

        if not user_id:
            user_id = (
                source.get("groupId")
                or source.get("roomId")
                or "unknown"
            )


        # ---------------------------------
        # 文字訊息
        # ---------------------------------

        if message_type == "text":

            user_text = message.get(
                "text",
                ""
            )

            if (
                user_text.strip().lower()
                == "/clear"
            ):
                clear_conversation(
                    user_id
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

            continue


        # ---------------------------------
        # 圖片訊息
        # ---------------------------------

        if message_type == "image":

            message_id = message.get("id")

            if not message_id:
                reply_line(
                    event["replyToken"],
                    "無法取得圖片 ID，請重新傳送圖片。"
                )

                continue

            content_provider = (
                message.get(
                    "contentProvider",
                    {}
                )
            )

            provider_type = (
                content_provider.get("type")
            )

            # LINE 使用者一般上傳的圖片
            if (
                not provider_type
                or provider_type == "line"
            ):

                image_bytes, content_type = (
                    download_line_image(
                        message_id
                    )
                )

                if not image_bytes:
                    reply_line(
                        event["replyToken"],
                        (
                            "圖片下載失敗，"
                            "請重新傳送一次。"
                        )
                    )

                    continue

                ai_answer = (
                    ask_ai_with_image(
                        user_id,
                        image_bytes,
                        content_type
                    )
                )

                reply_line(
                    event["replyToken"],
                    ai_answer
                )

                continue


            # 外部來源圖片
            if provider_type == "external":

                external_url = (
                    content_provider.get(
                        "originalContentUrl"
                    )
                )

                if external_url:

                    input_content = [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type":
                                        "input_text",
                                    "text":
                                        "請分析這張圖片。"
                                },
                                {
                                    "type":
                                        "input_image",
                                    "image_url":
                                        external_url,
                                    "detail":
                                        "auto"
                                }
                            ]
                        }
                    ]

                    ai_answer = call_openai(
                        user_id,
                        input_content
                    )

                    reply_line(
                        event["replyToken"],
                        ai_answer
                    )

                    continue


            reply_line(
                event["replyToken"],
                "目前無法讀取這張圖片。"
            )

            continue


        # ---------------------------------
        # 尚未支援的訊息
        # ---------------------------------

        reply_line(
            event["replyToken"],
            (
                "目前支援文字與圖片。"
                "語音、影片與檔案功能之後可以再加入。"
            )
        )

    return {"status": "ok"}
