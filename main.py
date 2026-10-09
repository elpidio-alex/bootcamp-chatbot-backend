import json
import os
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database.db import SessionLocal, get_db
from database.models import Conversation, Message

load_dotenv()

RODIUMAI_URL = "https://api.rodiumai.io/v1/chat/completions"
RODIUMAI_API_KEY = os.environ["RODIUMAI_API_KEY"]
PREVIEW_LENGTH = 60

# Liste des modèles définie côté serveur : le client ne peut jamais en sortir.
ALLOWED_MODELS = [m.strip() for m in os.environ["ALLOWED_MODELS"].split(",") if m.strip()]
DEFAULT_MODEL = ALLOWED_MODELS[0]

# Le prompt vit dans un fichier dédié ; SYSTEM_PROMPT_FILE permet de comparer ancien/nouveau (fiche de test).
PROMPTS_DIR = Path(__file__).parent / "prompts"
SYSTEM_PROMPT = (PROMPTS_DIR / os.getenv("SYSTEM_PROMPT_FILE", "system.md")).read_text(encoding="utf-8")

# Roles the LLM understands. Anything else stored in the DB (e.g. notifications) stays out of its prompt.
LLM_ROLES = {"user", "assistant"}
NOTIFICATION_ROLE = "system-notification"
NOTIFICATION_EVERY = 10  # a notification each time the dialogue reaches a multiple of this many messages
NOTIFICATION_TEXT = "Notification système : Une dizaine de messages écrits."

# Rôle custom : une question de révision est ajoutée toutes les QUIZ_EVERY_N questions de l'étudiant.
QUIZ_ROLE = "quiz"
QUIZ_EVERY_N = int(os.getenv("QUIZ_EVERY_N", "3"))
QUIZ_SEPARATOR = "===QUIZ==="
QUIZ_INSTRUCTION = (PROMPTS_DIR / "quiz_instruction.md").read_text(encoding="utf-8")

app = FastAPI(title="Study Buddy Chatbot")

from fastapi.middleware.cors import CORSMiddleware  # à mettre avec les autres imports, en haut

# Origines autorisées à appeler l'API depuis un navigateur (le front déployé). Vide en local : le proxy Vite suffit.
CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]
if CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )


class ConversationResponse(BaseModel):
    conversation_id: int


class ConversationSummary(BaseModel):
    id: int
    created_at: datetime
    preview: str | None  # first user message, truncated; None while the conversation is empty


class ChatRequest(BaseModel):
    conversation_id: int
    message: str
    model: str | None = None  # le client propose, le serveur décide


class MessageResponse(BaseModel):
    seq: int
    role: str
    content: str
    created_at: datetime


def load_messages(db: Session, conversation_id: int) -> list[Message]:
    # A conversation's messages in order; 404 if the conversation doesn't exist.
    if db.get(Conversation, conversation_id) is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return db.scalars(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.seq)
    ).all()


def build_llm_history(rows: list[Message]) -> list[dict]:
    # The DB history is not necessarily what the LLM sees: transform/filter it here, just before building the request.
    # - system-notification: unknown to the LLM -> dropped.
    # - quiz: transformed into "assistant", so the LLM knows it asked that question and understands the student's answer.
    # - consecutive assistant messages (answer + quiz) are merged: some providers dislike two same roles in a row.
    history: list[dict] = []
    for m in rows:
        if m.role == QUIZ_ROLE:
            role = "assistant"
        elif m.role in LLM_ROLES:
            role = m.role
        else:
            continue
        if history and history[-1]["role"] == "assistant" and role == "assistant":
            history[-1]["content"] += "\n\n" + m.content
        else:
            history.append({"role": role, "content": m.content})
    return history


class QuizSplitter:
    """Splits the streamed reply into the answer and the quiz question, chunk by chunk.

    The separator can arrive cut in two chunks ("===QU" then "IZ==="): the tail of the text that
    could be the beginning of the separator is held back until we know what it is.
    """

    def __init__(self, active: bool):
        self.active = active  # False on turns without quiz: everything is the answer
        self.answer = ""  # what was sent to the client as the answer
        self.quiz = ""  # what came after the separator
        self._buffer = ""  # held-back tail
        self._in_quiz = False

    def feed(self, chunk: str) -> str:
        # Returns the part of the answer that is safe to send to the client now.
        if not self.active:
            self.answer += chunk
            return chunk
        if self._in_quiz:
            self.quiz += chunk
            return ""
        self._buffer += chunk
        index = self._buffer.find(QUIZ_SEPARATOR)
        if index != -1:
            out = self._buffer[:index]
            self.quiz = self._buffer[index + len(QUIZ_SEPARATOR):]
            self._buffer = ""
            self._in_quiz = True
        else:
            hold = 0
            for size in range(min(len(QUIZ_SEPARATOR) - 1, len(self._buffer)), 0, -1):
                if QUIZ_SEPARATOR.startswith(self._buffer[-size:]):
                    hold = size
                    break
            out = self._buffer[: len(self._buffer) - hold]
            self._buffer = self._buffer[len(self._buffer) - hold:]
        self.answer += out
        return out

    def finish(self) -> str:
        # End of stream: release a held-back tail that never became a separator.
        out, self._buffer = self._buffer, ""
        self.answer += out
        return out


def sse(event: dict) -> str:
    # One Server-Sent Event line: the frontend reads "data: {...}" blocks separated by a blank line.
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def save_turn(conversation_id: int, first_seq: int, turn: list[tuple[str, str]]) -> None:
    # A dedicated session: the request's session may already be closed once the stream is running.
    # The unique (conversation_id, seq) constraint rejects a concurrent write of the same turn.
    with SessionLocal() as db:
        db.add_all(
            Message(conversation_id=conversation_id, seq=first_seq + i, role=role, content=content)
            for i, (role, content) in enumerate(turn)
        )
        db.commit()


async def stream_turn(
    conversation_id: int,
    user_text: str,
    next_seq: int,
    payload: dict,
    quiz_due: bool,
    dialogue_count: int,
) -> AsyncIterator[str]:
    splitter = QuizSplitter(active=quiz_due)
    usage = None
    finished = False  # True once the turn is saved, or deliberately dropped because of an error

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
            async with client.stream(
                "POST",
                RODIUMAI_URL,
                headers={"Authorization": f"Bearer {RODIUMAI_API_KEY}"},
                json=payload,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    chunk = json.loads(data)
                    usage = chunk.get("usage") or usage
                    choices = chunk.get("choices") or []
                    text = choices[0].get("delta", {}).get("content") if choices else None
                    if text:
                        safe = splitter.feed(text)
                        if safe:
                            yield sse({"type": "delta", "content": safe})

        tail = splitter.finish()
        if tail:
            yield sse({"type": "delta", "content": tail})

        answer = splitter.answer.strip()
        quiz = splitter.quiz.strip() or None
        if not answer:
            raise ValueError("Empty reply from the LLM.")

        turn = [("user", user_text), ("assistant", answer)]
        if quiz:
            turn.append((QUIZ_ROLE, quiz))
        notification = None
        if dialogue_count % NOTIFICATION_EVERY == 0:
            notification = NOTIFICATION_TEXT
            turn.append((NOTIFICATION_ROLE, notification))

        # The turn is written only now, once the whole reply is in: an error before this point leaves the DB untouched.
        save_turn(conversation_id, next_seq, turn)
        finished = True

        if quiz:
            yield sse({"type": "quiz", "content": quiz})
        if notification:
            yield sse({"type": "notification", "content": notification})
        yield sse({"type": "done", "usage": usage})

    except IntegrityError:
        finished = True
        yield sse({"type": "error", "message": "The conversation was updated concurrently, please retry."})
    except (httpx.HTTPError, ValueError):  # ValueError also covers json.JSONDecodeError
        finished = True  # error: nothing is written, the student can resend the message
        yield sse({"type": "error", "message": "The LLM API call failed."})
    finally:
        if not finished:
            # The client went away mid-stream (Stop button, closed tab): keep what it already received.
            partial = splitter.answer.strip()
            if partial:
                try:
                    save_turn(conversation_id, next_seq, [("user", user_text), ("assistant", partial)])
                except IntegrityError:
                    pass  # concurrent update: nothing to keep


@app.get("/models")
def list_models() -> list[str]:
    return ALLOWED_MODELS


@app.post("/conversations", status_code=201)
def create_conversation(db: Session = Depends(get_db)) -> ConversationResponse:
    conversation = Conversation()
    db.add(conversation)
    db.commit()
    return ConversationResponse(conversation_id=conversation.id)


@app.get("/conversations")
def list_conversations(db: Session = Depends(get_db)) -> list[ConversationSummary]:
    # Newest first, each joined to its first message (seq 1, always the user's) for the preview.
    rows = db.execute(
        select(Conversation, Message.content)
        .outerjoin(Message, and_(Message.conversation_id == Conversation.id, Message.seq == 1))
        .order_by(Conversation.id.desc())
    ).all()
    return [
        ConversationSummary(
            id=conversation.id,
            created_at=conversation.created_at,
            preview=content[:PREVIEW_LENGTH] if content else None,
        )
        for conversation, content in rows
    ]


@app.get("/conversations/{conversation_id}/messages")
def list_messages(conversation_id: int, db: Session = Depends(get_db)) -> list[MessageResponse]:
    return [
        MessageResponse(seq=m.seq, role=m.role, content=m.content, created_at=m.created_at)
        for m in load_messages(db, conversation_id)
    ]


@app.post("/chat")
def chat(req: ChatRequest, db: Session = Depends(get_db)) -> StreamingResponse:
    # Never trust the client: the model must be in the server-side allowlist (checked before any DB/LLM work).
    model = req.model or DEFAULT_MODEL
    if model not in ALLOWED_MODELS:
        raise HTTPException(status_code=400, detail="Model not allowed.")

    # Everything that can fail with a normal HTTP error (404...) happens here, before the stream starts.
    rows = load_messages(db, req.conversation_id)
    history = build_llm_history(rows)
    next_seq = rows[-1].seq + 1 if rows else 1  # follows every stored row so it stays unique

    # Every QUIZ_EVERY_N-th question of the student (this one included) gets a quiz question.
    user_turns = sum(1 for m in rows if m.role == "user") + 1
    quiz_due = user_turns % QUIZ_EVERY_N == 0
    # Dialogue only (user + assistant, quiz excluded), so notifications and quiz don't shift the multiples of 10.
    dialogue_count = sum(1 for m in rows if m.role in LLM_ROLES) + 2

    # The extra quiz instruction only exists in THIS request: it is never stored in the database.
    system_prompt = SYSTEM_PROMPT + ("\n\n" + QUIZ_INSTRUCTION if quiz_due else "")

    # The LLM is stateless: resend the system prompt + the whole conversation each turn.
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            *history,
            {"role": "user", "content": req.message},
        ],
        "max_tokens": 768,
        "stream": True,
        "stream_options": {"include_usage": True},  # ask for the token usage in the last chunk
    }

    return StreamingResponse(
        stream_turn(req.conversation_id, req.message, next_seq, payload, quiz_due, dialogue_count),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    import uvicorn

    port = os.getenv("PORT")  # défini par l'hébergeur (Render...), absent en local
    if port:
        uvicorn.run("main:app", host="0.0.0.0", port=int(port))
    else:
        uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)