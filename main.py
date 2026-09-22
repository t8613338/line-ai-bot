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


@app.get("/")
def home():
    return {"status": "LINE AI Bot is running"}


# =========================
# PostgreSQL 對話記憶
# =========================

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
                    (
                        user_id,
                        response_id
                    )
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


# Render 啟動時建立資料表
init_database()


# =========================
# OpenAI
# =========================

def ask_ai(
    user_id: str,
    user_text: str
) -> str:

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

    # 從 PostgreSQL 取得上一輪對話
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
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(
            req,
            timeout=60
        ) as response:

            result = json.loads(
                response.read().decode("utf-8")
            )

        # 將最新 Response ID 寫入 PostgreSQL
        response_id = result.get("id")

        if response_id:
            save_previous_response_id(
                user_id,
                response_id
            )

        # 取得 AI 回覆
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

        return (
            "AI 暫時無法回答，"
            "請稍後再試。"
        )

    except Exception as e:
        print(
            f"OpenAI error: "
            f"{type(e).__name__}: {e}"
        )

        return (
            "AI 暫時發生錯誤，"
            "請稍後再試。"
        )


# =========================
# LINE 回覆
# =========================

def reply_line(
    reply_token: str,
    text: str
):
    if not LINE_CHANNEL_ACCESS_TOKEN:
        raise RuntimeError(
            "LINE_CHANNEL_ACCESS_TOKEN "
            "is not configured"
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
                f"Bearer "
                f"{LINE_CHANNEL_ACCESS_TOKEN}",
            "Content-Type":
                "application/json",
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


# =========================
# LINE Webhook
# =========================

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
        if (
            event.get("type") == "message"
            and event.get(
                "message",
                {}
            ).get("type") == "text"
            and event.get("replyToken")
        ):

            user_text = (
                event["message"]["text"]
            )

            source = event.get(
                "source",
                {}
            )

            # 每位 LINE 使用者分開記憶
            user_id = source.get("userId")

            # 群組中仍以個別 userId 優先
            if not user_id:
                user_id = (
                    source.get("groupId")
                    or source.get("roomId")
                    or "unknown"
                )

            # 清除自己的對話記憶
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

    return {"status": "ok"}
