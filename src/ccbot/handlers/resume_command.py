"""Resume command — browse and resume past Claude Code sessions.

Implements /resume: scans all sessions-index files under ~/.claude/projects/,
groups them by project directory, and shows a paginated inline keyboard.
On selection, creates a tmux window with `claude --resume <id>` and binds
the current topic.

Key functions:
  - resume_command: /resume handler
  - handle_resume_command_callback: callback dispatcher for resume UI
  - scan_all_sessions: discover all resumable sessions across all projects
"""

import json
import structlog
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from telegram import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from ..config import config
from ..providers import get_provider, get_provider_for_window, resolve_launch_command
from ..session import session_manager
from ..tmux_manager import tmux_manager
from .callback_data import CB_RESUME_CANCEL, CB_RESUME_PAGE, CB_RESUME_PICK
from .callback_helpers import get_thread_id
from .message_sender import safe_edit, safe_reply
from .user_state import RESUME_SESSIONS

logger = structlog.get_logger()

_SESSIONS_PER_PAGE = 6

# Minimum file size to include in resume list.
# Sessions under this are typically automated hooks (e.g. auto-review).
_MIN_SESSION_BYTES = 50_000

_IndexParseError = (json.JSONDecodeError, OSError)


@dataclass
class ResumeEntry:
    """A resumable session discovered from JSONL files."""

    session_id: str
    summary: str
    cwd: str
    timestamp: str = ""  # human-readable timestamp (e.g. "02/27 10:42")


def _extract_session_info(jsonl_path: Path) -> tuple[str, str]:
    """Extract cwd and first user message summary from a JSONL session file.

    Reads lines until it finds a cwd and a human message, then stops early
    to avoid reading entire large files.
    """
    cwd = ""
    summary = ""
    try:
        with jsonl_path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Extract cwd from any line that has it
                if not cwd:
                    c = d.get("cwd")
                    if c:
                        cwd = c

                # Extract summary from first human message
                if not summary:
                    msg = d.get("message")
                    if isinstance(msg, dict) and msg.get("role") in (
                        "human",
                        "user",
                    ):
                        content = msg.get("content", "")
                        if isinstance(content, list):
                            texts = [
                                b.get("text", "")
                                for b in content
                                if isinstance(b, dict) and b.get("type") == "text"
                            ]
                            content = " ".join(texts)
                        if isinstance(content, str):
                            for cline in content.split("\n"):
                                cline = cline.strip()
                                if (
                                    cline
                                    and not cline.startswith("<")
                                    and not cline.startswith("/")
                                    and not cline.endswith(">")
                                    and len(cline) > 5
                                ):
                                    summary = cline[:80]
                                    break

                if cwd and summary:
                    break
    except OSError:
        pass
    return cwd, summary


def scan_all_sessions() -> list[ResumeEntry]:
    """Scan JSONL session files under ~/.claude/projects/ for resumable sessions.

    Falls back to sessions-index.json if present, but primarily discovers
    sessions by scanning .jsonl files directly (Claude Code >= v2.1.31
    no longer writes sessions-index.json).

    Returns entries sorted by file mtime (most recent first),
    deduplicated by session_id.
    """
    if not config.claude_projects_path.exists():
        return []

    candidates: list[tuple[float, ResumeEntry]] = []
    seen_ids: set[str] = set()

    for project_dir in config.claude_projects_path.iterdir():
        if not project_dir.is_dir():
            continue

        # Derive the original project path from the directory name.
        # Claude Code encodes paths as e.g. "-root" for /root,
        # "-root-repos-myproject" for /root/repos/myproject.
        dir_name = project_dir.name
        if dir_name == "-":
            original_path = "/"
        elif dir_name.startswith("-"):
            original_path = "/" + dir_name[1:].replace("-", "/")
        else:
            original_path = ""

        # Primary: scan .jsonl files directly
        try:
            jsonl_files = sorted(project_dir.glob("*.jsonl"))
        except OSError:
            jsonl_files = []

        for jsonl_file in jsonl_files:
            session_id = jsonl_file.stem
            if session_id in seen_ids:
                continue

            try:
                stat = jsonl_file.stat()
                mtime = stat.st_mtime
                fsize = stat.st_size
            except OSError:
                continue

            # Skip small sessions (automated hooks, auto-reviews)
            if fsize < _MIN_SESSION_BYTES:
                continue

            cwd, summary = _extract_session_info(jsonl_file)
            if not cwd:
                cwd = original_path
            if not summary:
                summary = session_id[:12]

            ts = datetime.fromtimestamp(mtime).strftime("%m/%d %H:%M")
            seen_ids.add(session_id)
            candidates.append(
                (mtime, ResumeEntry(session_id, summary, cwd, timestamp=ts))
            )

        # Fallback: also check sessions-index.json for any entries not
        # found via JSONL scan (e.g. older sessions with deleted files)
        index_file = project_dir / "sessions-index.json"
        if index_file.exists():
            try:
                index_data = json.loads(index_file.read_text(encoding="utf-8"))
            except _IndexParseError:
                continue

            idx_original = index_data.get("originalPath", "")
            for entry in index_data.get("entries", []):
                session_id = entry.get("sessionId", "")
                full_path = entry.get("fullPath", "")
                if not session_id or session_id in seen_ids:
                    continue
                if full_path and not Path(full_path).exists():
                    continue

                try:
                    mtime = Path(full_path).stat().st_mtime if full_path else 0.0
                except OSError:
                    mtime = 0.0

                idx_cwd = entry.get("projectPath", idx_original)
                idx_summary = entry.get("summary", "") or session_id[:12]
                seen_ids.add(session_id)
                candidates.append(
                    (mtime, ResumeEntry(session_id, idx_summary, idx_cwd))
                )

    candidates.sort(key=lambda c: c[0], reverse=True)
    return [entry for _, entry in candidates]


def _build_resume_keyboard(
    sessions: list[dict[str, str]],
    page: int = 0,
) -> InlineKeyboardMarkup:
    """Build inline keyboard for resume session picker with pagination."""
    total = len(sessions)
    start = page * _SESSIONS_PER_PAGE
    end = min(start + _SESSIONS_PER_PAGE, total)
    page_sessions = sessions[start:end]

    rows: list[list[InlineKeyboardButton]] = []
    current_cwd = ""
    for idx_offset, entry in enumerate(page_sessions):
        global_idx = start + idx_offset
        cwd = entry.get("cwd", "")
        # Show project header when cwd changes
        if cwd != current_cwd:
            current_cwd = cwd
            short_path = Path(cwd).name if cwd else "unknown"
            rows.append(
                [
                    InlineKeyboardButton(
                        f"\U0001f4c1 {short_path}",
                        callback_data="noop",
                    )
                ]
            )
        ts = entry.get("timestamp", "")
        summary_text = entry.get("summary", "")[:32] or entry["session_id"][:12]
        label = f"{ts}  {summary_text}" if ts else summary_text
        rows.append(
            [
                InlineKeyboardButton(
                    label,
                    callback_data=f"{CB_RESUME_PICK}{global_idx}"[:64],
                )
            ]
        )

    # Pagination row
    nav_buttons: list[InlineKeyboardButton] = []
    if page > 0:
        nav_buttons.append(
            InlineKeyboardButton(
                "\u2b05 Prev",
                callback_data=f"{CB_RESUME_PAGE}{page - 1}"[:64],
            )
        )
    total_pages = (total + _SESSIONS_PER_PAGE - 1) // _SESSIONS_PER_PAGE
    if page < total_pages - 1:
        nav_buttons.append(
            InlineKeyboardButton(
                "Next \u27a1",
                callback_data=f"{CB_RESUME_PAGE}{page + 1}"[:64],
            )
        )
    nav_buttons.append(
        InlineKeyboardButton("\u2716 Cancel", callback_data=CB_RESUME_CANCEL)
    )
    rows.append(nav_buttons)

    return InlineKeyboardMarkup(rows)


async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /resume — show all resumable sessions grouped by project."""
    if not update.message:
        return

    user = update.effective_user
    if not user or not config.is_user_allowed(user.id):
        return

    thread_id = get_thread_id(update)
    if thread_id is None:
        await safe_reply(
            update.message,
            "\u274c Please use /resume in a named topic.",
        )
        return

    # Check resume capability using per-window provider (or global fallback)
    window_id = session_manager.get_window_for_thread(user.id, thread_id)
    provider = get_provider_for_window(window_id) if window_id else get_provider()
    if not provider.capabilities.supports_resume:
        await safe_reply(
            update.message,
            "\u274c Resume is not supported by the current provider.",
        )
        return

    sessions = scan_all_sessions()
    if not sessions:
        await safe_reply(update.message, "\u274c No past sessions found.")
        return

    session_dicts = [
        {
            "session_id": s.session_id,
            "summary": s.summary,
            "cwd": s.cwd,
            "timestamp": s.timestamp,
        }
        for s in sessions
    ]
    if context.user_data is not None:
        context.user_data[RESUME_SESSIONS] = session_dicts

    keyboard = _build_resume_keyboard(session_dicts, page=0)
    await safe_reply(
        update.message,
        "\U0001f4c2 Select a session to resume:",
        reply_markup=keyboard,
    )


async def handle_resume_command_callback(
    query: CallbackQuery,
    user_id: int,
    data: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Dispatch resume command callbacks."""
    if data.startswith(CB_RESUME_PICK):
        await _handle_pick(query, user_id, data, update, context)
    elif data.startswith(CB_RESUME_PAGE):
        await _handle_page(query, user_id, data, update, context)
    elif data == CB_RESUME_CANCEL:
        await _handle_cancel(query, context)


async def _create_resume_window(
    user_id: int,
    thread_id: int,
    session_id: str,
    cwd: str,
) -> tuple[bool, str, str, str]:
    """Unbind old window, create a new one with resume args.

    Returns (success, message, window_name, window_id).
    """
    old_window_id = session_manager.get_window_for_thread(user_id, thread_id)
    if old_window_id:
        session_manager.unbind_thread(user_id, thread_id)
        from .status_polling import clear_dead_notification

        clear_dead_notification(user_id, thread_id)

    provider = (
        get_provider_for_window(old_window_id) if old_window_id else get_provider()
    )
    launch_args = provider.make_launch_args(resume_id=session_id)
    launch_command = resolve_launch_command(provider.capabilities.name)
    success, message, created_wname, created_wid = await tmux_manager.create_window(
        cwd, agent_args=launch_args, launch_command=launch_command
    )
    if success:
        # Mark as pending bind BEFORE waiting for hook (prevents auto-topic race)
        session_manager.mark_pending_bind(created_wid)
        if provider.capabilities.supports_hook:
            await session_manager.wait_for_session_map_entry(created_wid)
        session_manager.set_window_provider(created_wid, provider.capabilities.name)

    return success, message, created_wname, created_wid


async def _handle_pick(
    query: CallbackQuery,
    user_id: int,
    data: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Handle session selection from the resume picker."""
    idx_str = data[len(CB_RESUME_PICK) :]
    try:
        idx = int(idx_str)
    except ValueError:
        await query.answer("Invalid selection", show_alert=True)
        return

    thread_id = get_thread_id(update)
    if thread_id is None:
        await query.answer("Use in a topic", show_alert=True)
        return

    stored = context.user_data.get(RESUME_SESSIONS) if context.user_data else None
    if not stored or idx < 0 or idx >= len(stored):
        await query.answer("Invalid session index", show_alert=True)
        return

    picked = stored[idx]
    session_id = picked["session_id"]
    cwd = picked.get("cwd", "")

    if not cwd or not Path(cwd).is_dir():
        await safe_edit(query, "\u274c Project directory no longer exists.")
        _clear_resume_state(context.user_data)
        await query.answer("Failed")
        return

    success, message, created_wname, created_wid = await _create_resume_window(
        user_id, thread_id, session_id, cwd
    )
    if not success:
        await safe_edit(query, f"\u274c {message}")
        _clear_resume_state(context.user_data)
        await query.answer("Failed")
        return

    session_manager.bind_thread(
        user_id, thread_id, created_wid, window_name=created_wname
    )

    # Store group chat_id for routing
    chat = query.message.chat if query.message else None
    if chat and chat.type in ("group", "supergroup"):
        session_manager.set_group_chat_id(user_id, thread_id, chat.id)

    # Patched: skip topic rename to preserve user's topic name.
    pass

    summary_short = picked.get("summary", "")[:40]
    await safe_edit(
        query,
        f"\u2705 Resuming session: {summary_short}\n\U0001f4c2 `{cwd}`",
    )
    _clear_resume_state(context.user_data)
    await query.answer("Resumed")


async def _handle_page(
    query: CallbackQuery,
    _user_id: int,
    data: str,
    _update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Handle pagination in resume picker."""
    page_str = data[len(CB_RESUME_PAGE) :]
    try:
        page = int(page_str)
    except ValueError:
        await query.answer("Invalid page", show_alert=True)
        return

    stored = context.user_data.get(RESUME_SESSIONS) if context.user_data else None
    if not stored:
        await query.answer("No sessions available", show_alert=True)
        return

    keyboard = _build_resume_keyboard(stored, page=page)
    await safe_edit(
        query,
        "\U0001f4c2 Select a session to resume:",
        reply_markup=keyboard,
    )
    await query.answer()


async def _handle_cancel(
    query: CallbackQuery,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Handle cancel in resume picker."""
    _clear_resume_state(context.user_data)
    await safe_edit(query, "Resume cancelled.")
    await query.answer("Cancelled")


def _clear_resume_state(user_data: dict | None) -> None:
    """Remove resume-related keys from user_data."""
    if user_data is None:
        return
    user_data.pop(RESUME_SESSIONS, None)
