"""Topic icon status updates via editForumTopic.

Updates topic icons (NOT names) to reflect session state:
  - Active (working): lightning bolt icon
  - Idle (waiting): writing icon
  - Done (Claude exited): checkmark icon
  - Dead (window gone): exclamation icon

Never touches the topic name — users' chosen topic names are preserved.
Tracks per-topic state to avoid redundant API calls. Debounces transitions
to prevent rapid active/idle toggling from flooding the chat with rename
messages. Gracefully degrades when the bot lacks editForumTopic permission.

Key functions:
  - update_topic_emoji: Update icon for a specific topic (debounced)
  - clear_topic_emoji_state: Clean up tracking for a topic
"""

import structlog
import time

from telegram import Bot
from telegram.error import BadRequest, TelegramError

logger = structlog.get_logger()

# Custom emoji IDs for topic icons (from Telegram's forum topic icon stickers)
_ICON_ACTIVE = "5312016608254762256"  # ⚡ Lightning bolt
_ICON_IDLE = "5238156910363950406"  # ✍️ Writing / pen
_ICON_DONE = "5237699328843200968"  # ✅ Checkmark
_ICON_DEAD = "5379748062124056162"  # ❗ Exclamation

# Legacy name-based emoji prefixes (for stripping from existing topic names)
EMOJI_ACTIVE = "\U0001f7e2"  # Green circle
EMOJI_IDLE = "\U0001f4a4"  # Zzz / sleeping
EMOJI_DONE = "\u2705"  # Check mark
EMOJI_DEAD = "\u274c"  # Cross mark
_EMOJI_DEAD_OLD = "\u26ab"  # Legacy dead emoji (black circle, pre-2026-02)

# Debounce: state must be stable for this many seconds before updating topic icon.
# Prevents rapid active↔idle toggling from flooding the chat.
DEBOUNCE_SECONDS = 5.0

# Topic state tracking: (chat_id, thread_id) -> current_state
_topic_states: dict[tuple[int, int], str] = {}

# Pending transitions: (chat_id, thread_id) -> (desired_state, first_seen_monotonic)
_pending_transitions: dict[tuple[int, int], tuple[str, float]] = {}

# Chats where editForumTopic is disabled due to permission errors
_disabled_chats: set[int] = set()


def set_user_topic_name(chat_id: int, thread_id: int, name: str) -> None:
    """No-op — kept for API compatibility. Topic names are no longer modified."""
    pass


async def update_topic_emoji(
    bot: Bot,
    chat_id: int,
    thread_id: int,
    state: str,
    display_name: str,
) -> None:
    """Update topic icon to reflect session state. Never changes the topic name.

    Uses icon_custom_emoji_id to set the topic icon, leaving name=None so
    Telegram preserves the user's original topic name.

    Debounces transitions: the new state must be requested consistently for
    DEBOUNCE_SECONDS before the API call is made.

    Args:
        bot: Telegram Bot instance
        chat_id: Group chat ID
        thread_id: Forum topic thread ID
        state: One of "active", "idle", "done", "dead"
        display_name: Unused (kept for API compatibility)
    """
    if chat_id in _disabled_chats:
        return

    key = (chat_id, thread_id)

    # Already in this state — no transition needed
    if _topic_states.get(key) == state:
        _pending_transitions.pop(key, None)
        return

    icon_id = {
        "active": _ICON_ACTIVE,
        "idle": _ICON_IDLE,
        "done": _ICON_DONE,
        "dead": _ICON_DEAD,
    }.get(state)

    if not icon_id:
        return

    # Debounce: require the new state to be stable before applying
    now = time.monotonic()
    pending = _pending_transitions.get(key)
    if pending is None or pending[0] != state:
        # New or changed desired state — start debounce timer
        _pending_transitions[key] = (state, now)
        return

    if now - pending[1] < DEBOUNCE_SECONDS:
        # Not stable long enough yet
        return

    # Debounce passed — execute the transition
    _pending_transitions.pop(key, None)

    try:
        # Only set icon — name=None preserves the user's topic name
        await bot.edit_forum_topic(
            chat_id=chat_id,
            message_thread_id=thread_id,
            icon_custom_emoji_id=icon_id,
        )
        _topic_states[key] = state
        logger.debug(
            "Updated topic icon: chat=%d thread=%d state=%s",
            chat_id,
            thread_id,
            state,
        )
    except BadRequest as e:
        if "Not enough rights" in e.message:
            _disabled_chats.add(chat_id)
            logger.info(
                "Topic icon disabled for chat %d: insufficient permissions",
                chat_id,
            )
        elif (
            "topic_not_modified" in e.message.lower()
            or "Topic_id_invalid" in e.message
        ):
            # Expected no-ops: already correct icon or invalid topic
            _topic_states[key] = state
        else:
            logger.debug("Failed to update topic icon: %s", e)
    except TelegramError:
        pass


def strip_emoji_prefix(name: str) -> str:
    """Remove known legacy emoji prefix from a topic name."""
    for emoji in (EMOJI_ACTIVE, EMOJI_IDLE, EMOJI_DONE, EMOJI_DEAD, _EMOJI_DEAD_OLD):
        prefix = f"{emoji} "
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


async def rename_topic(
    bot: Bot,
    chat_id: int,
    thread_id: int,
    new_display_name: str,
) -> None:
    """No-op — topic names are never overwritten by the bot.

    Users' Telegram topic names take priority over tmux window names.
    """
    logger.debug(
        "rename_topic skipped (preserving user topic name): "
        "chat=%d thread=%d requested_name='%s'",
        chat_id,
        thread_id,
        new_display_name,
    )


def clear_topic_emoji_state(chat_id: int, thread_id: int) -> None:
    """Clear icon tracking for a topic (called on topic cleanup)."""
    key = (chat_id, thread_id)
    _topic_states.pop(key, None)
    _pending_transitions.pop(key, None)


def reset_all_state() -> None:
    """Reset all tracking state (for testing)."""
    _topic_states.clear()
    _pending_transitions.clear()
    _disabled_chats.clear()
