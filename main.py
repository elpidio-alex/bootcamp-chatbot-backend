import os
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database.db import get_db
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

# QUIZ : rôle custom. Une question de révision est ajoutée toutes les QUIZ_EVERY_N questions de l'étudiant.
QUIZ_ROLE = "quiz"
QUIZ_EVERY_N = int(os.getenv("QUIZ_EVERY_N", "3"))
QUIZ_SEPARATOR = "===QUIZ==="
QUIZ_INSTRUCTION = (PROMPTS_DIR / "quiz_instruction.md").read_text(encoding="utf-8")

app = FastAPI(title="Study Buddy Chatbot")


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


class ChatResponse(BaseModel):
    reply: str
    notification: str | None = None  # set when this turn also stored a system-notification
    quiz: str | None = None  # QUIZ : set when this turn also stored a quiz question


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


def split_quiz(reply: str) -> tuple[str, str | None]:
    # QUIZ : separate the normal answer from the quiz question the LLM appended after the separator.
    # No separator (the LLM forgot) -> no quiz, the whole reply is the answer.
    answer, separator, quiz = reply.partition(QUIZ_SEPARATOR)
    quiz = quiz.strip()
    if not separator or not quiz:
        return reply.strip(), None
    return answer.strip(), quiz


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
def chat(req: ChatRequest, db: Session = Depends(get_db)) -> ChatResponse:
    # Never trust the client: the model must be in the server-side allowlist (checked before any DB/LLM work).
    model = req.model or DEFAULT_MODEL
    if model not in ALLOWED_MODELS:
        raise HTTPException(status_code=400, detail="Model not allowed.")

    # Load this conversation from the database, in message order.
    rows = load_messages(db, req.conversation_id)
    history = build_llm_history(rows)
    # seq follows every stored row (notifications and quiz included) so it stays unique.
    next_seq = rows[-1].seq + 1 if rows else 1
    user_message = {"role": "user", "content": req.message}

    # QUIZ : count the student's questions (this one included). Every QUIZ_EVERY_N-th one gets a quiz question.
    user_turns = sum(1 for m in rows if m.role == "user") + 1
    quiz_due = user_turns % QUIZ_EVERY_N == 0

    # The extra quiz instruction only exists in THIS request: it is never stored in the database.
    system_prompt = SYSTEM_PROMPT + ("\n\n" + QUIZ_INSTRUCTION if quiz_due else "")

    # The LLM is stateless: resend the system prompt + the whole conversation each turn.
    messages = [{"role": "system", "content": system_prompt}, *history, user_message]

    try:
        response = httpx.post(
            RODIUMAI_URL,
            headers={"Authorization": f"Bearer {RODIUMAI_API_KEY}"},
            json={
                "model": model,
                "messages": messages,
                "max_tokens": 768,  # QUIZ : room for the answer + the quiz question
                "stream": False,
            },
            timeout=30,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="The LLM API call failed.") from exc

    reply = response.json()["choices"][0]["message"]["content"]

    # QUIZ : on a quiz turn, split the LLM output into the answer and the quiz question.
    quiz = None
    if quiz_due:
        reply, quiz = split_quiz(reply)

    # Only record the turn once the call succeeded, so a failure doesn't leave a dangling user message.
    turn = [("user", req.message), ("assistant", reply)]
    if quiz:
        turn.append((QUIZ_ROLE, quiz))

    # Count only the dialogue (user + assistant, quiz excluded) so notifications and quiz
    # don't shift the total off the multiples of 10.
    dialogue_count = sum(1 for m in rows if m.role in LLM_ROLES) + 2
    notification = None
    if dialogue_count % NOTIFICATION_EVERY == 0:
        notification = NOTIFICATION_TEXT
        turn.append((NOTIFICATION_ROLE, notification))

    db.add_all(
        Message(conversation_id=req.conversation_id, seq=next_seq + i, role=role, content=content)
        for i, (role, content) in enumerate(turn)
    )

    try:
        db.commit()
    except IntegrityError:
        # Another request already wrote these seq numbers in this conversation while we waited for the LLM.
        db.rollback()
        raise HTTPException(
            status_code=409, detail="The conversation was updated concurrently, please retry."
        )
    return ChatResponse(reply=reply, notification=notification, quiz=quiz)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)