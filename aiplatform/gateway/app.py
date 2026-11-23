"""Internal assistant gateway (FastAPI): mock SSO, entitlements, conversation state, rate
limits, audit and SSE streaming. Spec section 2.

    uvicorn aiplatform.gateway.app:app --port 8200      # or: make api

Identity: a signed JWT -> AuthContext; nothing in the request body or the prompt can
change who the user is or which branches they see.
"""

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

import httpx
import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from aiplatform import export
from aiplatform.audit.writer import AuditRecord, write_audit
from aiplatform.gateway import handbook
from aiplatform.gateway.auth import AuthContext, UnknownUser, gateway_db_url, load_staff_user
from aiplatform.gateway.conversations import ConversationStore, MessageRecord
from aiplatform.gateway.security import (
    InvalidToken,
    RateLimiter,
    gateway_settings,
    issue_token,
    verify_token,
)
from aiplatform.llm.config import get_llm_settings
from assistants.data.agent.agent import DataAgent

log = logging.getLogger("bcai.gateway")


# ---- request / response models ------------------------------------------------------
class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=30)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: uuid.UUID | None = None
    message: str = Field(min_length=1, max_length=20_000)  # 2,000-char cap enforced + audited
    assistant: Literal["data"] = "data"  # router: explicit choice in the UI (DESIGN.md)


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message_id: uuid.UUID
    rating: Literal[1, -1]
    comment: str | None = Field(default=None, max_length=1000)


def sse(event: dict[str, Any]) -> str:
    """One SSE frame. The chart event's kind goes back into `type` inside the payload."""
    kind = event["type"]
    data = {k: v for k, v in event.items() if k != "type"}
    if kind == "chart":
        data["type"] = data.pop("chart_type")
    return f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def create_app(
    agent_factory: Callable[[], DataAgent] | None = None,
    audit: Callable[[AuditRecord], None] = write_audit,
    feedback_writer: Callable[[dict[str, Any]], None] | None = None,
    run_decoder_loop: bool = True,
) -> FastAPI:
    gs = gateway_settings()
    store = ConversationStore()
    limiter = RateLimiter(gs.rate_per_min)
    state: dict[str, Any] = {}

    def get_agent() -> DataAgent:
        if "agent" not in state:
            if agent_factory is not None:
                state["agent"] = agent_factory()
            else:
                from aiplatform.llm import get_embedder, get_provider

                state["agent"] = DataAgent(get_provider(), get_embedder())
        agent: DataAgent = state["agent"]
        return agent

    async def decoder_loop() -> None:
        from assistants.data.semantic.atum_decoder import run_decoder
        from assistants.data.settings import get_settings

        while True:
            await asyncio.sleep(gs.decoder_interval_s)
            try:

                def once() -> None:
                    with psycopg.connect(get_settings().decoder_url) as conn:
                        stats = run_decoder(conn)
                    if stats.candidates:
                        log.info("decoder: %s", stats)

                await asyncio.to_thread(once)
            except Exception:
                log.exception("Atumverse decoder run failed")

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(decoder_loop()) if run_decoder_loop else None
        yield
        if task:
            task.cancel()

    app = FastAPI(
        title="Bank Conversational AI - internal gateway", version="0.4", lifespan=lifespan
    )

    def current_user(authorization: Annotated[str | None, Header()] = None) -> AuthContext:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "missing bearer token")
        try:
            return verify_token(authorization[7:], gs.jwt_secret)
        except InvalidToken as e:
            raise HTTPException(401, "invalid or expired token") from e

    User = Annotated[AuthContext, Depends(current_user)]

    def audit_blocked(user: AuthContext, question: str, reason: str) -> None:
        try:
            audit(
                AuditRecord(
                    username=user.username,
                    user_role=user.role,
                    question=question[:4000],
                    route="refused",
                    status="blocked",
                    error=reason,
                )
            )
        except Exception:
            log.exception("AUDIT WRITE FAILED (gateway rejection)")

    # ---- endpoints -----------------------------------------------------------------
    @app.get("/auth/users")
    def demo_users() -> list[dict[str, Any]]:
        """Mock SSO picker: the four demo staff users."""
        out = []
        for name in ("bm_salmiya", "rh_north", "ho_analyst", "it_ops"):
            try:
                u = load_staff_user(name)
            except UnknownUser:
                continue
            out.append(
                {
                    "username": u.username,
                    "display_name": u.display_name,
                    "role": u.role,
                    "branches": list(u.branches),
                }
            )
        return out

    @app.post("/auth/login")
    def login(body: LoginRequest) -> dict[str, Any]:
        try:
            user = load_staff_user(body.username)
        except UnknownUser as e:
            raise HTTPException(401, "unknown user") from e
        return {
            "access_token": issue_token(user, gs.jwt_secret, gs.token_ttl_s),
            "token_type": "bearer",
            "expires_in": gs.token_ttl_s,
        }

    @app.get("/me")
    def me(user: User) -> dict[str, Any]:
        s = get_llm_settings()
        return {
            "username": user.username,
            "display_name": user.display_name,
            "role": user.role,
            "branches": list(user.branches),
            "llm": {"provider": s.provider, "model": get_agent().provider.model},
        }

    @app.post("/chat")
    async def chat(body: ChatRequest, user: User) -> Any:
        question = body.message.strip()
        if len(question) > gs.max_question_chars:
            await asyncio.to_thread(audit_blocked, user, question, "message too long")
            return JSONResponse(
                {
                    "code": "too_long",
                    "message": f"Questions are limited to {gs.max_question_chars} characters.",
                },
                status_code=413,
            )
        if not limiter.allow(user.username):
            await asyncio.to_thread(audit_blocked, user, question, "rate limited")
            return JSONResponse(
                {
                    "code": "rate_limited",
                    "message": f"Limit is {gs.rate_per_min} questions per minute.",
                },
                status_code=429,
            )
        conv = store.get_or_create(body.conversation_id, user.username)
        agent = get_agent()

        async def stream() -> AsyncIterator[str]:
            yield sse({"type": "conversation", "conversation_id": str(conv.id)})
            record: MessageRecord | None = None
            events: list[dict[str, Any]] = []
            async for ev in agent.run(question, user, conv.ctx):
                events.append(ev)
                if ev["type"] == "done":
                    record = MessageRecord(ev["message_id"], question, time.time(), events)
                    conv.messages.append(record)
                yield sse(ev)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/conversations/{conversation_id}")
    def conversation(conversation_id: uuid.UUID, user: User) -> dict[str, Any]:
        conv = store.get(conversation_id, user.username)
        if conv is None:
            raise HTTPException(404, "conversation not found")
        return {"conversation_id": str(conv.id), "messages": [m.summary() for m in conv.messages]}

    @app.post("/feedback")
    def feedback(body: FeedbackRequest, user: User) -> dict[str, Any]:
        msg = store.find_message(str(body.message_id), user.username)
        if msg is None:
            raise HTTPException(404, "message not found")
        done = msg.get("done") or {}
        plan = next((e for e in reversed(msg.events) if e["type"] == "plan"), None)
        metric_query = plan["args"] if plan and plan.get("route") == "metric" else None
        row = {
            "message_id": body.message_id,
            "username": user.username,
            "rating": body.rating,
            "comment": body.comment,
            "question": msg.question,
            "route": done.get("route"),
            "metric_query": json.dumps(metric_query) if metric_query else None,
            "candidate_example": body.rating == 1
            and done.get("status") == "ok"
            and metric_query is not None,
        }
        (feedback_writer or _write_feedback)(row)
        return {"ok": True, "candidate_example": row["candidate_example"]}

    @app.get("/export/{message_id}")
    def export_result(
        message_id: uuid.UUID, user: User, format: Literal["xlsx", "pdf"] = "xlsx"
    ) -> Response:
        """The result the user was shown, as Excel or PDF. Same rows (already scoped by row
        security); nothing is re-queried. Owner-only, and every export is audited."""
        found = store.locate(str(message_id), user.username)
        if found is None:
            raise HTTPException(404, "message not found")
        conv_id, msg = found
        result = msg.get("result")
        if result is None or not result.get("columns"):
            raise HTTPException(404, "this answer has no data to export")
        answer = msg.get("answer_delta") or {}
        definition = msg.get("definition") or {}
        doc = export.ExportDoc(
            question=msg.question,
            answer=answer.get("text", ""),
            definition=definition.get("text", ""),
            columns=result["columns"],
            rows=result["rows"],
            unit=result.get("unit", ""),
            label=result.get("label", ""),
            totals_columns=result.get("totals_columns") or [],
            totals=result.get("totals") or [],
            row_count=result.get("row_count", len(result["rows"])),
            truncated=bool(result.get("truncated")),
            requested_by=f"{user.display_name} ({user.username})",
        )
        body = export.render(doc, format)
        try:
            audit(
                AuditRecord(
                    username=user.username,
                    user_role=user.role,
                    question=f"[export {format}] {msg.question}"[:4000],
                    route="export",
                    status="ok",
                    row_count=doc.row_count,
                    conversation_id=conv_id,
                    message_id=message_id,
                )
            )
        except Exception:
            log.exception("AUDIT WRITE FAILED (export)")
            raise HTTPException(503, "export unavailable: audit could not be written") from None
        return Response(
            body,
            media_type=export.FORMATS[format],
            headers={
                "Content-Disposition": f'attachment; filename="{export.filename(format)}"',
                "Cache-Control": "no-store",
            },
        )

    @app.get("/handbook")
    def handbook_page(user: User) -> dict[str, Any]:
        """The developer handbook for the portal's Handbook page (any logged-in user)."""
        try:
            return {
                "title": handbook.TITLE,
                "updated": handbook.updated_at(),
                "markdown": handbook.load_markdown(inline_images=True),
            }
        except handbook.HandbookMissing as e:
            raise HTTPException(404, "handbook not found") from e

    @app.get("/handbook/download")
    def handbook_download(user: User, format: Literal["md", "html"] = "md") -> Response:
        """Self-contained download: Markdown or HTML with the diagrams inlined."""
        try:
            text = handbook.load_markdown(inline_images=True)
        except handbook.HandbookMissing as e:
            raise HTTPException(404, "handbook not found") from e
        body = handbook.to_html(text) if format == "html" else text
        media = "text/html" if format == "html" else "text/markdown"
        return Response(
            body.encode("utf-8"),
            media_type=f"{media}; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="bank-data-agent-handbook.{format}"',
                "Cache-Control": "no-store",
            },
        )

    @app.get("/health")
    async def health() -> JSONResponse:
        checks: dict[str, Any] = {}

        def db() -> dict[str, Any]:
            from assistants.data.semantic.atum_decoder import decode_status
            from assistants.data.settings import get_settings

            with psycopg.connect(get_settings().agent_url, connect_timeout=3) as c:
                c.execute("SELECT 1")
            with psycopg.connect(get_settings().decoder_url, connect_timeout=3) as c:
                return decode_status(c)

        try:
            checks["db"] = {"ok": True, "decoder": await asyncio.to_thread(db)}
        except Exception as e:
            checks["db"] = {"ok": False, "error": type(e).__name__}
        s = get_llm_settings()
        llm: dict[str, Any] = {"provider": s.provider}
        if s.provider == "ollama":
            try:
                async with httpx.AsyncClient(timeout=3) as c:
                    r = await c.get(s.ollama_base_url.removesuffix("/v1") + "/api/version")
                llm |= {"ok": r.status_code == 200, "model": s.ollama_model}
            except httpx.HTTPError as e:
                llm |= {"ok": False, "error": type(e).__name__}
        else:
            llm |= {"ok": bool(getattr(s, f"{s.provider}_api_key", "")), "check": "configured"}
        checks["llm"] = llm
        ok = checks["db"]["ok"] and llm.get("ok", False)
        return JSONResponse(
            {"status": "ok" if ok else "degraded", **checks}, status_code=200 if ok else 503
        )

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            {"code": exc.status_code, "message": exc.detail}, status_code=exc.status_code
        )

    return app


def _write_feedback(row: dict[str, Any]) -> None:
    cols = list(row)
    sql = (
        f"INSERT INTO app.feedback ({', '.join(cols)}) "
        f"VALUES ({', '.join(f'%({c})s' for c in cols)})"
    )
    with psycopg.connect(gateway_db_url(), connect_timeout=5) as conn:
        conn.execute(sql, row)


app = create_app()
