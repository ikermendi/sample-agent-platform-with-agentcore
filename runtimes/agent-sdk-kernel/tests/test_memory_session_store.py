"""Headless kernel: the SDK transcript lives in AgentCore Memory and is resumed.

Two layers, both without AWS or the real SDK:

- ``AgentCoreMemorySessionStore`` against a fake Memory data plane that
  applies ListEvents metadata filters, returns newest first and paginates.
- ``main.invoke`` with stubbed ``claude_agent_sdk`` / ``bedrock_agentcore`` /
  ``boto3``: the system prompt no longer changes with memory, per-message
  context goes into the user turn, and the session is named on its first run
  and resumed on later ones.
"""

import asyncio
import copy
import importlib.util
import sys
import types
import unittest
from dataclasses import dataclass, field
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from memory_session_store import AgentCoreMemorySessionStore


class FakeMemory:
    """Enough of the bedrock-agentcore data plane for the kernel."""

    def __init__(self, page_size: int = 2):
        self.events: list[dict] = []
        self.page_size = page_size
        self.records: list[str] = []

    def create_event(self, **kw):
        assert 1 <= len(kw["payload"]) <= 100
        self.events.append(copy.deepcopy(kw))
        return {"event": {"eventId": f"e{len(self.events)}"}}

    def _match(self, ev: dict, kw: dict) -> bool:
        if (ev["memoryId"], ev["actorId"], ev["sessionId"]) != (
            kw["memoryId"], kw["actorId"], kw["sessionId"],
        ):
            return False
        meta = ev.get("metadata") or {}
        for f in (kw.get("filter") or {}).get("eventMetadata", []):
            k, op = f["left"]["metadataKey"], f["operator"]
            if op == "EQUALS_TO" and meta.get(k) != f["right"]["metadataValue"]:
                return False
            if op == "NOT_EXISTS" and k in meta:
                return False
            if op == "EXISTS" and k not in meta:
                return False
        return True

    def list_events(self, **kw):
        evs = [e for e in self.events if self._match(e, kw)][::-1]  # newest first
        size = min(kw.get("maxResults", 100), self.page_size)
        start = int(kw.get("nextToken") or 0)
        page = evs[start : start + size]
        out = {"events": [
            {"payload": e["payload"], "metadata": e.get("metadata", {}),
             "eventTimestamp": e["eventTimestamp"]}
            for e in page
        ]}
        if start + size < len(evs) and kw.get("maxResults", 100) > size:
            out["nextToken"] = str(start + size)
        return out

    def retrieve_memory_records(self, **kw):
        return {"memoryRecordSummaries": [{"content": {"text": t}} for t in self.records]}


def run(coro):
    return asyncio.run(coro)


def entry(i: int) -> dict:
    return {"type": "user", "uuid": f"u{i}", "message": {"role": "user", "content": f"m{i}"}}


KEY = {"project_key": "-tmp-agent-work", "session_id": "11111111-1111-5111-8111-111111111111"}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.mem = FakeMemory()
        self.store = AgentCoreMemorySessionStore(self.mem, "mem-123456789", "alice", "chat-1")

    def test_round_trip_keeps_order_across_batches_and_pages(self):
        for b in range(5):
            run(self.store.append(KEY, [entry(b * 3 + j) for j in range(3)]))
        fresh = AgentCoreMemorySessionStore(self.mem, "mem-123456789", "alice", "chat-1")
        loaded = run(fresh.load(KEY))
        self.assertEqual([e["uuid"] for e in loaded], [f"u{i}" for i in range(15)])

    def test_large_batches_split_at_the_payload_limit(self):
        run(self.store.append(KEY, [entry(i) for i in range(250)]))
        self.assertEqual([len(e["payload"]) for e in self.mem.events], [100, 100, 50])
        fresh = AgentCoreMemorySessionStore(self.mem, "mem-123456789", "alice", "chat-1")
        self.assertEqual(len(run(fresh.load(KEY))), 250)

    def test_events_are_tagged_and_skip_long_term_extraction(self):
        run(self.store.append(KEY, [entry(0)]))
        ev = self.mem.events[0]
        self.assertEqual(ev["extractionMode"], "SKIP")
        self.assertEqual(ev["metadata"]["kind"], {"stringValue": "sdk-transcript"})
        self.assertEqual(ev["metadata"]["subpath"], {"stringValue": "main"})
        self.assertEqual(ev["sessionId"], "chat-1")
        self.assertEqual(ev["actorId"], "alice")

    def test_transcripts_do_not_mix(self):
        run(self.store.append(KEY, [entry(0)]))
        run(self.store.append({**KEY, "subpath": "subagents/agent-x"}, [entry(1)]))
        other_sdk = {**KEY, "session_id": "22222222-2222-5222-8222-222222222222"}
        run(self.store.append(other_sdk, [entry(2)]))
        bob = AgentCoreMemorySessionStore(self.mem, "mem-123456789", "bob", "chat-1")
        other_chat = AgentCoreMemorySessionStore(self.mem, "mem-123456789", "alice", "chat-2")
        fresh = AgentCoreMemorySessionStore(self.mem, "mem-123456789", "alice", "chat-1")
        self.assertEqual([e["uuid"] for e in run(fresh.load(KEY))], ["u0"])
        self.assertEqual(
            [e["uuid"] for e in run(fresh.load({**KEY, "subpath": "subagents/agent-x"}))], ["u1"]
        )
        self.assertIsNone(run(bob.load(KEY)))
        self.assertIsNone(run(other_chat.load(KEY)))

    def test_conversational_events_are_not_part_of_the_transcript(self):
        self.mem.create_event(
            memoryId="mem-123456789", actorId="alice", sessionId="chat-1",
            eventTimestamp=None,
            payload=[{"conversational": {"role": "USER", "content": {"text": "hi"}}}],
        )
        self.assertIsNone(run(self.store.load(KEY)))
        self.assertFalse(run(self.store.has_transcript(KEY["session_id"])))

    def test_probe_then_append_then_load_sees_new_entries(self):
        self.assertFalse(run(self.store.has_transcript(KEY["session_id"])))
        run(self.store.append(KEY, [entry(0)]))
        self.assertEqual(len(run(self.store.load(KEY))), 1)
        run(self.store.append(KEY, [entry(1)]))
        self.assertEqual(len(run(self.store.load(KEY))), 2)

    def test_project_key_is_not_part_of_the_key(self):
        # cwd changes when skills are mounted; the transcript must still resume
        run(self.store.append(KEY, [entry(0)]))
        fresh = AgentCoreMemorySessionStore(self.mem, "mem-123456789", "alice", "chat-1")
        self.assertTrue(run(fresh.load({**KEY, "project_key": "-somewhere-else"})))


# ----------------------------------------------------------- main.invoke


@dataclass
class _Options:
    system_prompt: str = ""
    max_turns: int = 0
    mcp_servers: dict = field(default_factory=dict)
    allowed_tools: list = field(default_factory=list)
    env: dict = field(default_factory=dict)
    model: str | None = None
    cwd: str | None = None
    setting_sources: list | None = None
    session_store: object = None
    resume: str | None = None
    session_id: str | None = None


class _Result:
    def __init__(self, text):
        self.result, self.is_error = text, False
        self.duration_ms, self.num_turns, self.total_cost_usd = 1, 1, 0.0


def load_main(mem: FakeMemory, calls: list):
    sdk = types.ModuleType("claude_agent_sdk")
    sdk.ClaudeAgentOptions = _Options
    sdk.AssistantMessage = type("AssistantMessage", (), {})
    sdk.TextBlock = type("TextBlock", (), {})
    sdk.ResultMessage = _Result

    async def query(prompt, options):
        calls.append((prompt, options))
        yield _Result(f"answer {len(calls)}")

    sdk.query = query

    class App:
        def entrypoint(self, fn):
            return fn

    rt = types.ModuleType("bedrock_agentcore.runtime")
    rt.BedrockAgentCoreApp = App
    bac = types.ModuleType("bedrock_agentcore")
    bac.runtime = rt
    boto3 = types.ModuleType("boto3")
    boto3.client = lambda *a, **k: mem
    shim = types.ModuleType("llm_shim")
    shim.release = lambda *_: None
    shim.start = lambda *_: None
    for name, mod in {
        "claude_agent_sdk": sdk, "bedrock_agentcore": bac,
        "bedrock_agentcore.runtime": rt, "boto3": boto3, "llm_shim": shim,
    }.items():
        sys.modules[name] = mod
    spec = importlib.util.spec_from_file_location("kernel_main_under_test", SRC / "main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Ctx:
    session_id = "chat-1"


class InvokeTests(unittest.TestCase):
    def setUp(self):
        self.mem = FakeMemory(page_size=100)
        self.calls: list = []
        self.main = load_main(self.mem, self.calls)
        self.payload = {
            "prompt": "what next?",
            "system": "You are the test agent.",
            "memory": {"memory_id": "mem-123456789", "actor_id": "alice"},
        }

    def invoke(self, payload=None):
        return run(self.main.invoke(payload or self.payload, Ctx()))

    def test_first_run_names_the_session_and_later_runs_resume_it(self):
        self.invoke()
        _, first = self.calls[-1]
        sid = self.main.sdk_session_uuid(self.payload["memory"], "chat-1")
        self.assertEqual(first.session_id, sid)
        self.assertIsNone(first.resume)
        self.assertIsNotNone(first.session_store)
        # the SDK mirrors the transcript through the store during the run
        run(first.session_store.append({"project_key": "p", "session_id": sid}, [entry(0)]))

        self.invoke()  # a new invocation: fresh store, as after a recycle
        _, second = self.calls[-1]
        self.assertEqual(second.resume, sid)
        self.assertIsNone(second.session_id)

    def test_system_prompt_does_not_change_with_memory(self):
        self.mem.records = ["prefers short answers"]
        self.invoke()
        prompt, opts = self.calls[-1]
        self.assertEqual(opts.system_prompt, "You are the test agent.")
        self.assertIn("<user_memory>", prompt)
        self.assertIn("prefers short answers", prompt)
        self.assertTrue(prompt.endswith("what next?"))

    def test_legacy_sessions_replay_turns_in_the_user_turn_until_a_transcript_exists(self):
        self.main.store_memory_event(self.payload["memory"], "chat-1", "old q", "old a")
        self.invoke()
        prompt, opts = self.calls[-1]
        self.assertIn("<earlier_conversation>", prompt)
        self.assertIn("old a", prompt)
        self.assertEqual(opts.system_prompt, "You are the test agent.")
        sid = self.main.sdk_session_uuid(self.payload["memory"], "chat-1")
        run(opts.session_store.append({"project_key": "p", "session_id": sid}, [entry(0)]))
        self.invoke()
        prompt, _ = self.calls[-1]
        self.assertNotIn("<earlier_conversation>", prompt)

    def test_the_exchange_event_stores_the_raw_prompt(self):
        self.mem.records = ["prefers short answers"]
        self.invoke()
        conv = [e for e in self.mem.events if "metadata" not in e]
        self.assertEqual(len(conv), 1)
        texts = [p["conversational"]["content"]["text"] for p in conv[0]["payload"]]
        self.assertEqual(texts, ["what next?", "answer 1"])

    def test_memory_failure_degrades_to_an_unpersisted_session(self):
        def boom(**_):
            raise RuntimeError("memory down")

        self.mem.list_events = boom
        out = self.invoke()
        self.assertTrue(out["ok"])
        _, opts = self.calls[-1]
        self.assertIsNone(opts.session_store)
        self.assertIsNone(opts.resume)

    def test_no_memory_binding_means_no_store(self):
        self.invoke({"prompt": "hi"})
        prompt, opts = self.calls[-1]
        self.assertEqual(prompt, "hi")
        self.assertIsNone(opts.session_store)


if __name__ == "__main__":
    unittest.main()
