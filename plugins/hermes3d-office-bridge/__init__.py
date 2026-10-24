"""Publish finished agent turns so a Hermes3D office can visualise them.

Hermes3D renders each backend profile as a character at a desk. To show who is
talking — and to gather the speakers into a conversation circle — the office
needs to know when an agent replied and what it said. Message events on the
JSON-RPC gateway are delivered only to the client that submitted the prompt, so
an office watching from the outside sees nothing when you chat somewhere else.

This plugin closes that gap from inside the backend. It observes every turn and
republishes small frames on the dashboard's event bus, which fans out to any
subscriber: ``agent.turn.start`` when a turn begins (``pre_llm_call``), the
reply as ``agent.turn`` when it finishes (``post_llm_call``), and
``agent.turn.end`` once the turn is over however it ended (``on_session_end``,
which Hermes fires at the end of every turn, interrupted or failed included).
Start and end let the office sit an agent at its desk while it works. Chat
wherever you like — the desktop app, the TUI, the CLI — and the office animates
it.

Install it into each profile you want on screen; plugins are scoped to a
``HERMES_HOME``, so a profile without it stays silent. See ``install.sh``.

Observation only: the hooks never mutate the turn, never block it, return
nothing (so ``pre_llm_call`` injects no context), and swallow their own
failures.
"""

from __future__ import annotations

import atexit
import logging
import time
from typing import Any, Optional
from urllib.parse import quote

from .publisher import OfficePublisher

_log = logging.getLogger(__name__)

FRAME_VERSION = 1

TURN_START_KIND = "agent.turn.start"
TURN_END_KIND = "agent.turn.end"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9119
DEFAULT_CHANNEL = "hermes3d"

# The office shows a short preview, not a transcript. Truncating keeps a long
# answer from dominating the event bus.
MAX_TEXT_CHARS = 2_000

_publisher: Optional[OfficePublisher] = None


def build_turn_frame(
    *,
    profile: str,
    text: str,
    session_id: str = "",
    platform: str = "",
    at_ms: Optional[int] = None,
) -> dict:
    """Build the wire frame for one finished turn.

    ``profile`` is the agent identity: Hermes3D names each character after the
    backend profile it represents, so the two line up without a lookup table.
    """
    body = (text or "").strip()
    if len(body) > MAX_TEXT_CHARS:
        body = body[:MAX_TEXT_CHARS].rstrip() + "…"
    return {
        "v": FRAME_VERSION,
        "kind": "agent.turn",
        "profile": profile or "",
        "text": body,
        "sessionId": session_id or "",
        "platform": platform or "",
        "atMs": int(at_ms if at_ms is not None else time.time() * 1000),
    }


def build_activity_frame(
    *,
    kind: str,
    profile: str,
    session_id: str = "",
    platform: str = "",
    at_ms: Optional[int] = None,
) -> dict:
    """Build the wire frame marking a turn's start or end (``kind``).

    It carries no text: the office only needs to know the agent is busy, and
    ``session_id`` pairs the end with its start when one agent has several
    conversations open at once.
    """
    return {
        "v": FRAME_VERSION,
        "kind": kind,
        "profile": profile or "",
        "sessionId": session_id or "",
        "platform": platform or "",
        "atMs": int(at_ms if at_ms is not None else time.time() * 1000),
    }


def build_publish_url(*, host: str, port: int, channel: str, token: str) -> str:
    """URL for the dashboard publisher endpoint.

    ``/api/pub`` accepts ``?token=`` only on an ungated (loopback) bind, which
    is the topology this plugin targets — it always publishes to the local
    backend, never across a network.
    """
    return (
        f"ws://{host}:{int(port)}/api/pub"
        f"?channel={quote(channel, safe='')}&token={quote(token, safe='')}"
    )


def _default_home_token(key: str) -> str:
    """Read ``key`` out of the default home's ``.env``.

    Profiles each get their own ``HERMES_HOME`` and their own ``.env``, so a
    profile-scoped process never sees a token pinned in the default home. That
    matters here because this token is not profile-level config: it identifies
    the one office backend every profile on the machine publishes to. Asking
    users to copy the same secret into five ``.env`` files would be worse.
    """
    from pathlib import Path

    try:
        from hermes_constants import get_default_hermes_root

        root = Path(get_default_hermes_root())
    except Exception:
        root = Path.home() / ".hermes"

    try:
        for line in (root / ".env").read_text(encoding="utf-8").splitlines():
            name, sep, value = line.partition("=")
            if sep and name.strip() == key:
                return value.strip().strip("'\"")
    except OSError:
        pass
    return ""


def _resolve_token() -> str:
    """The office backend's session token, which is a secret and lives in env.

    Pin it in ``~/.hermes/.env`` as ``HERMES3D_OFFICE_TOKEN`` — an unpinned
    backend mints a random one per start, which no subscriber can know.

    The office backend's own token variable, ``HERMES_DASHBOARD_SESSION_TOKEN``,
    deliberately is NOT the key to pin. ``.env`` loads with ``override=True``, so
    pinning that name hands the same fixed token to *every* backend on the
    machine — including the one the desktop app spawns, which mints a fresh
    token per launch and then cannot authenticate against its own gateway. Under
    a dedicated name the office backend still receives it (passed inline on the
    command line) while every other backend keeps minting its own.
    """
    import os

    token = (os.environ.get("HERMES3D_OFFICE_TOKEN") or "").strip()
    if token:
        return token
    token = _default_home_token("HERMES3D_OFFICE_TOKEN")
    if token:
        return token
    # Back-compat for setups written against the earlier guide, which pinned the
    # dashboard token. Only the pinned file counts: this process's own
    # ``HERMES_DASHBOARD_SESSION_TOKEN`` authenticates whichever backend it
    # happens to be, which is rarely the office backend it publishes to.
    return _default_home_token("HERMES_DASHBOARD_SESSION_TOKEN")


def _resolve_profile() -> str:
    """Which profile produced this turn.

    One ``hermes serve`` backend runs every profile, so the answer is per-turn
    rather than per-process; ``get_active_profile_name()`` reflects the profile
    scope in force while the hook runs.
    """
    try:
        from hermes_cli.profiles import get_active_profile_name

        return str(get_active_profile_name() or "")
    except Exception as exc:
        _log.debug("hermes3d-office-bridge: profile lookup failed: %s", exc)
        return ""


def _handle_post_llm_call(**kwargs: Any) -> None:
    if _publisher is None:
        return
    try:
        text = str(kwargs.get("assistant_response") or "").strip()
        if not text:
            return
        frame = build_turn_frame(
            profile=_resolve_profile(),
            text=text,
            session_id=str(kwargs.get("session_id") or ""),
            platform=str(kwargs.get("platform") or ""),
        )
        _publisher.publish(frame)
    except Exception as exc:
        # An office that cannot draw is never a reason to disturb a turn.
        _log.debug("hermes3d-office-bridge: publish skipped: %s", exc)


def _publish_activity(kind: str, kwargs: dict) -> None:
    if _publisher is None:
        return
    try:
        _publisher.publish(
            build_activity_frame(
                kind=kind,
                profile=_resolve_profile(),
                session_id=str(kwargs.get("session_id") or ""),
                platform=str(kwargs.get("platform") or ""),
            )
        )
    except Exception as exc:
        _log.debug("hermes3d-office-bridge: activity publish skipped: %s", exc)


def _handle_pre_llm_call(**kwargs: Any) -> None:
    # Returning None is deliberate: anything else would be injected as context.
    _publish_activity(TURN_START_KIND, kwargs)


def _handle_on_session_end(**kwargs: Any) -> None:
    _publish_activity(TURN_END_KIND, kwargs)


def register(ctx: Any) -> None:
    global _publisher

    token = _resolve_token()
    if not token:
        _log.warning(
            "hermes3d-office-bridge: HERMES3D_OFFICE_TOKEN is unset; turns will "
            "not be published. Pin it in ~/.hermes/.env."
        )
        return

    def setting(key: str, fallback: Any) -> Any:
        try:
            value = ctx.get_config(key, fallback)
        except Exception:
            return fallback
        return fallback if value in (None, "") else value

    _publisher = OfficePublisher(
        build_publish_url(
            host=str(setting("host", DEFAULT_HOST)),
            port=int(setting("port", DEFAULT_PORT)),
            channel=str(setting("channel", DEFAULT_CHANNEL)),
            token=token,
        )
    )
    # A one-shot CLI turn ends its process the moment the hook returns, well
    # before a background send completes. Give the queue a bounded chance to
    # drain so those turns reach the office too, not just long-lived backends.
    atexit.register(_publisher.flush)
    ctx.register_hook("pre_llm_call", _handle_pre_llm_call)
    ctx.register_hook("post_llm_call", _handle_post_llm_call)
    ctx.register_hook("on_session_end", _handle_on_session_end)
