"""可观测性：轻量 JSONL trace 记录器。

记录每个节点的执行、每次 LLM 调用的延迟与 token 用量，
输出到 traces/run_<id>.jsonl，可被评测脚本聚合。
（README 中说明了与 Langfuse 的对接点：record_llm 即埋点位置。）
"""
import json
import threading
import time
import uuid
from contextvars import ContextVar
from pathlib import Path
from typing import Optional

from . import config


class TraceRecorder:
    def __init__(self, run_id: Optional[str] = None):
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.t0 = time.time()
        config.TRACE_DIR.mkdir(exist_ok=True)
        self.path = config.TRACE_DIR / f"run_{self.run_id}.jsonl"
        self._events: list[dict] = []
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0

    def _append(self, event: dict):
        event["run_id"] = self.run_id
        event["elapsed_ms"] = int((time.time() - self.t0) * 1000)
        self._events.append(event)
        with _write_lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def record_node(self, node: str, input_summary: dict, output_summary: dict, ok: bool = True):
        self._append({"type": "node", "node": node,
                      "input": input_summary, "output": output_summary, "ok": ok})

    def record_llm(self, node: str, purpose: str, result):
        self.total_prompt_tokens += result.prompt_tokens
        self.total_completion_tokens += result.completion_tokens
        self._append({
            "type": "llm", "node": node, "purpose": purpose,
            "latency_ms": result.latency_ms, "mock": result.mock,
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
        })

    def record_event(self, **kw):
        self._append({"type": "event", **kw})

    def summary(self) -> dict:
        llm_calls = [e for e in self._events if e["type"] == "llm"]
        nodes = [e for e in self._events if e["type"] == "node"]
        return {
            "run_id": self.run_id,
            "total_ms": int((time.time() - self.t0) * 1000),
            "llm_calls": len(llm_calls),
            "avg_llm_latency_ms": round(
                sum(e["latency_ms"] for e in llm_calls) / len(llm_calls)) if llm_calls else 0,
            "nodes_executed": [n["node"] for n in nodes],
            "prompt_tokens": self.total_prompt_tokens,
            "completion_tokens": self.total_completion_tokens,
            "failed_nodes": [n["node"] for n in nodes if not n.get("ok", True)],
        }


# 每个执行上下文一个 recorder。ContextVar 避免并发请求互相覆盖 run_id。
_recorder: ContextVar[Optional[TraceRecorder]] = ContextVar(
    "contentops_trace_recorder", default=None
)
_write_lock = threading.Lock()


def new_recorder(run_id: Optional[str] = None) -> TraceRecorder:
    recorder = TraceRecorder(run_id)
    _recorder.set(recorder)
    return recorder


def get_recorder() -> TraceRecorder:
    recorder = _recorder.get()
    if recorder is None:
        recorder = TraceRecorder()
        _recorder.set(recorder)
    return recorder
