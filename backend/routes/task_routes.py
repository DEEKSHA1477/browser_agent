"""Task REST endpoints + the per-task WebSocket channel."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from agent.capabilities import CATALOG, build_task, selection_instruction
from browser.browser_manager import browser_manager
from browser.security import check_url
from agent.planner import InvalidInstruction, validate_instruction
from agent.state import TaskRecord, task_store
from agent.workflow import runner
from config import settings
from models.task_models import ClarifyRequest, CreateTaskRequest, SelectRequest, TaskCreatedResponse, TaskStatus
from realtime.connection_manager import manager
from services.llm_service import llm_service

router = APIRouter(prefix="/api", tags=["tasks"])
ws_router = APIRouter()


def _get(task_id: str) -> TaskRecord:
    record = task_store.get(task_id)
    if record is None:
        raise HTTPException(404, "Task not found")
    return record


@router.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "llm_configured": llm_service.available,
        "llm_provider": settings.llm_provider,
        "llm_model": settings.llm_model,
        "headless": settings.headless,
        "vision_mode": settings.vision_mode,
    }


@router.get("/capabilities")
async def list_capabilities() -> list[dict]:
    """The task-launcher catalog shown on the dashboard's home screen."""
    return [c.public() for c in CATALOG]


@router.post("/tasks", response_model=TaskCreatedResponse)
async def create_task(body: CreateTaskRequest) -> TaskCreatedResponse:
    details: dict[str, str] = {}
    try:
        if body.capability:
            instruction, details = build_task(body.capability, body.details or {})
        elif body.instruction:
            instruction = body.instruction
        else:
            raise InvalidInstruction("Choose a task or describe what you would like me to do.")
        instruction = validate_instruction(instruction)
    except InvalidInstruction as exc:
        raise HTTPException(400, str(exc)) from exc
    if not llm_service.available:
        key = "OPENAI_API_KEY" if settings.llm_provider == "openai" else "GEMINI_API_KEY"
        raise HTTPException(503, f"No LLM is configured. Add {key} to the .env file and restart the backend.")
    record = task_store.create(instruction, capability=body.capability, details=details)
    runner.start(record)
    return TaskCreatedResponse(task_id=record.id, snapshot=record.snapshot())


@router.get("/tasks")
async def list_tasks() -> list[dict]:
    return [
        {"id": r.id, "instruction": r.instruction, "status": r.status.value, "created_at": r.created_at, "summary": r.summary}
        for r in task_store.all()
    ]


@router.get("/tasks/{task_id}")
async def get_task(task_id: str) -> dict:
    return _get(task_id).snapshot()


@router.post("/tasks/{task_id}/clarify")
async def clarify(task_id: str, body: ClarifyRequest) -> dict:
    record = _get(task_id)
    await runner.wait_until_suspended(task_id)
    waiting = await runner.waiting_for_input(task_id)
    if waiting not in ("clarification", "question") or runner.is_running(task_id):
        raise HTTPException(409, "This task is not waiting for an answer.")
    await record.say(body.answer.strip(), role="user")
    runner.resume(record, body.answer.strip())
    return record.snapshot()


@router.post("/tasks/{task_id}/select")
async def select_result(task_id: str, body: SelectRequest) -> dict:
    """The user picked one of the verified results on the dashboard.

    * If the agent is waiting for the user's choice, the selection is the answer.
    * If the task has finished, a follow-up task acts on the selected item in the
      same browser window (play it / open it), still behind the approval gate.
    Returns ``{"task_id", "snapshot"}`` of the task that now handles the selection.
    """
    record = _get(task_id)
    found = next(((c, i) for c in record.results for i in c.items if i.id == body.item_id), None)
    if found is None:
        raise HTTPException(404, "That result is no longer available.")
    collection, item = found
    label = (item.fields.get(collection.columns[0]) if collection.columns else None) or next(iter(item.fields.values()))
    url = item.url
    if url and not check_url(url, internal_ok=False)[0]:
        url = None  # never hand the agent a link the navigation policy would refuse

    await runner.wait_until_suspended(task_id)
    if record.status == TaskStatus.AWAITING_CLARIFICATION and not runner.is_running(task_id):
        answer = f'I choose "{label}"' + (f" ({url})" if url else "") + "."
        await record.say(answer, role="user")
        runner.resume(record, answer)
        return {"task_id": record.id, "snapshot": record.snapshot()}
    if not record.is_terminal or runner.is_running(task_id):
        raise HTTPException(409, "NOVA is still working on this task. Wait for it to finish, or stop it first.")
    if not llm_service.available:
        raise HTTPException(503, "No LLM is configured.")

    try:
        instruction = validate_instruction(selection_instruction(record.capability, label, url))
    except InvalidInstruction as exc:
        raise HTTPException(400, str(exc)) from exc
    details = {"Selected": label[:300], **({"Link": url[:300]} if url else {})}
    follow = task_store.create(instruction, capability=record.capability, details=details)
    if await browser_manager.adopt(record.id, follow):
        await record.publish()
    runner.start(follow)
    return {"task_id": follow.id, "snapshot": follow.snapshot()}


@router.post("/tasks/{task_id}/pause")
async def pause(task_id: str) -> dict:
    record = _get(task_id)
    if record.is_terminal:
        raise HTTPException(409, "Task already finished.")
    if not record.control.paused:
        record.control.pause()
        record.status_before_pause = record.status
        await record.log("Paused by you. The agent will stop before its next action.", "warning")
        await record.set_status(TaskStatus.PAUSED)
    return record.snapshot()


@router.post("/tasks/{task_id}/resume")
async def resume(task_id: str) -> dict:
    record = _get(task_id)
    if record.control.paused:
        record.control.resume()
        await record.log("Resumed.", "info")
        await record.set_status(record.status_before_pause or TaskStatus.RUNNING)
    return record.snapshot()


@router.post("/tasks/{task_id}/stop")
async def stop(task_id: str) -> dict:
    record = _get(task_id)
    if not record.is_terminal:
        await runner.stop(record)
    return record.snapshot()


@router.post("/tasks/{task_id}/retry")
async def retry(task_id: str) -> dict:
    record = _get(task_id)
    if not record.can_retry or runner.is_running(task_id):
        raise HTTPException(409, "This task cannot be retried right now.")
    await runner.retry(record)
    return record.snapshot()


@router.post("/tasks/{task_id}/human-done")
async def human_done(task_id: str) -> dict:
    record = _get(task_id)
    if record.status != TaskStatus.AWAITING_HUMAN_ACTION:
        raise HTTPException(409, "The agent is not waiting for you.")
    record.control.human_completed()
    return record.snapshot()


@router.post("/tasks/{task_id}/close-browser")
async def close_browser(task_id: str) -> dict:
    record = _get(task_id)
    if runner.is_running(task_id):
        raise HTTPException(409, "Stop the task before closing its browser.")
    await runner.close_browser(record)
    return record.snapshot()


@ws_router.websocket("/ws/tasks/{task_id}")
async def task_socket(websocket: WebSocket, task_id: str) -> None:
    record = task_store.get(task_id)
    if record is None:
        await websocket.close(code=4404)
        return
    await manager.connect(task_id, websocket)
    try:
        await manager.send(websocket, {"type": "snapshot", "task_id": task_id, "data": record.snapshot()})
        if record.last_screenshot:
            await manager.send(websocket, {"type": "screenshot", "task_id": task_id, "data": record.last_screenshot})
        while True:
            message = await websocket.receive_text()  # client pings keep the connection alive
            if message == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await manager.disconnect(task_id, websocket)
