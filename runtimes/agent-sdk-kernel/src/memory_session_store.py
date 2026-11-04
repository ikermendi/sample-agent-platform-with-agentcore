"""Claude Agent SDK ``SessionStore`` backed by AgentCore Memory short-term events.

Why: the CLI keeps each conversation as a JSONL transcript on local disk, and a
recycled microVM loses it. Mirroring that transcript into AgentCore Memory lets
the next invocation of the same session resume the *real* conversation — tool
calls and their results included — instead of a text summary pasted into the
system prompt (which lost the tool results and broke prompt caching).

Layout: every transcript batch is one ``CreateEvent`` on the session's
(actorId, sessionId) — the same pair the conversational exchange events use —
with one ``blob`` payload per JSONL entry (at most 100 per event) and metadata
that keeps it apart from those events:

    kind        = "sdk-transcript"
    sdk_session = the SDK session UUID
    subpath     = "main" or the subagent subpath
    seq         = zero-padded, strictly increasing within a process

Transcript events are stored with ``extractionMode=SKIP``: long-term extraction
keeps running on the conversational USER/ASSISTANT events only, not on raw
tool output. ``seq`` orders the entries on load; ``eventTimestamp`` alone is
not precise enough to order batches written within the same millisecond.

The kernel binds one store instance per invocation (one memory, actor and
session), so the SDK's ``project_key`` (derived from the cwd, which changes
when skills are mounted) is deliberately not part of the stored key.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("agent-sdk-kernel.session-store")

KIND = "sdk-transcript"
MAIN = "main"
MAX_PAYLOADS_PER_EVENT = 100  # CreateEvent payload list maximum
MAX_LOAD_EVENTS = 5000  # safety stop while paginating ListEvents


def _meta(value: str) -> dict:
    return {"stringValue": value}


class AgentCoreMemorySessionStore:
    """Duck-typed ``claude_agent_sdk.SessionStore`` (``append`` + ``load``)."""

    def __init__(self, client: Any, memory_id: str, actor_id: str, session_id: str):
        self._client = client
        self._memory_id = memory_id
        self._actor_id = actor_id
        self._session_id = session_id[:100]
        self._last_seq = 0
        # load() results, keyed by (sdk_session, subpath). The kernel probes
        # for an existing transcript before deciding resume vs. new session;
        # the SDK's own load() during resume then reuses that read.
        self._loaded: dict[tuple[str, str], list[dict] | None] = {}

    # ------------------------------------------------------------- helpers

    def _next_seq(self) -> str:
        seq = time.time_ns()
        if seq <= self._last_seq:
            seq = self._last_seq + 1
        self._last_seq = seq
        return f"{seq:020d}"

    @staticmethod
    def _subpath(key: dict) -> str:
        return key.get("subpath") or MAIN

    def _create_events(self, sdk_session: str, subpath: str, entries: list[dict]) -> None:
        for i in range(0, len(entries), MAX_PAYLOADS_PER_EVENT):
            chunk = entries[i : i + MAX_PAYLOADS_PER_EVENT]
            self._client.create_event(
                memoryId=self._memory_id,
                actorId=self._actor_id,
                sessionId=self._session_id,
                eventTimestamp=datetime.now(timezone.utc),
                payload=[{"blob": json.dumps(e, separators=(",", ":"))} for e in chunk],
                metadata={
                    "kind": _meta(KIND),
                    "sdk_session": _meta(sdk_session),
                    "subpath": _meta(subpath),
                    "seq": _meta(self._next_seq()),
                },
                extractionMode="SKIP",
            )

    def _list_entries(self, sdk_session: str, subpath: str) -> list[dict] | None:
        def eq(k: str, v: str) -> dict:
            return {
                "left": {"metadataKey": k},
                "operator": "EQUALS_TO",
                "right": {"metadataValue": _meta(v)},
            }

        events: list[dict] = []
        kwargs: dict = {
            "memoryId": self._memory_id,
            "actorId": self._actor_id,
            "sessionId": self._session_id,
            "includePayloads": True,
            "maxResults": 100,
            "filter": {
                "eventMetadata": [
                    eq("kind", KIND),
                    eq("sdk_session", sdk_session),
                    eq("subpath", subpath),
                ]
            },
        }
        while True:
            resp = self._client.list_events(**kwargs)
            events.extend(resp.get("events", []))
            token = resp.get("nextToken")
            if not token or len(events) >= MAX_LOAD_EVENTS:
                break
            kwargs["nextToken"] = token
        if not events:
            return None

        def seq_of(ev: dict) -> str:
            return ((ev.get("metadata") or {}).get("seq") or {}).get("stringValue", "")

        events.sort(key=seq_of)
        entries: list[dict] = []
        for ev in events:
            for p in ev.get("payload", []) or []:
                blob = p.get("blob")
                if blob is None:
                    continue
                entries.append(json.loads(blob) if isinstance(blob, (str, bytes)) else blob)
        return entries or None

    # --------------------------------------------------- SessionStore API

    async def append(self, key: dict, entries: list[dict]) -> None:
        if not entries:
            return
        sdk_session, subpath = key["session_id"], self._subpath(key)
        await asyncio.to_thread(self._create_events, sdk_session, subpath, list(entries))
        cached = self._loaded.get((sdk_session, subpath))
        if cached is not None:
            cached.extend(entries)
        else:
            self._loaded.pop((sdk_session, subpath), None)  # re-read on next load

    async def load(self, key: dict) -> list[dict] | None:
        k = (key["session_id"], self._subpath(key))
        if k not in self._loaded:
            self._loaded[k] = await asyncio.to_thread(self._list_entries, *k)
        cached = self._loaded[k]
        return None if cached is None else list(cached)

    async def has_transcript(self, sdk_session: str) -> bool:
        """True when a main transcript for ``sdk_session`` exists in Memory."""
        return bool(await self.load({"project_key": "", "session_id": sdk_session}))
