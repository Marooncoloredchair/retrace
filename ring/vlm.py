"""Vision-language calls for hold / release object naming.

Provider is Config.vlm_provider ("auto", "openai", "anthropic", or "none").
API keys come from the environment (OPENAI_API_KEY / ANTHROPIC_API_KEY).
Runs off the engine thread via a bounded queue.
"""

from __future__ import annotations

import json
import os
import queue
import re
import threading
from dataclasses import dataclass
from typing import Callable


HOLD_PROMPT = (
    "These images are from a camera on the palm side of a finger ring. "
    "Order: a few frames just before grasp, up to two mid-hold frames, "
    "then frames just after release. "
    "Decide whether the wearer put an object down. "
    "Reply with JSON only, no markdown:\n"
    '{"put_down": true, "object": "short name", "place": "one sentence saying where the object is"}\n'
    "or\n"
    '{"put_down": false, "object": "", "place": ""}\n'
    "Use put_down false when the hand was only resting, waving, or covering the lens."
)

VLM_MISSING_KEY_WARNING = (
    "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n"
    "!!  VLM DISABLED: neither ANTHROPIC_API_KEY nor OPENAI_API_KEY is set.\n"
    "!!  Hold naming will not run. Set one of those env vars and restart.\n"
    "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
)


@dataclass
class VlmAnswer:
    raw: str
    name: str  # empty when none / failed
    description: str
    is_none: bool
    error: str = ""
    put_down: bool | None = None
    place: str = ""


def resolve_vlm_provider(requested: str = "auto", model: str = "") -> tuple[str, str, str]:
    """Return (provider, model, warning). warning is non-empty when disabled for missing keys."""
    req = (requested or "auto").strip().lower()
    anthropic_key = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
    openai_key = bool(os.environ.get("OPENAI_API_KEY", "").strip())

    if req == "none":
        return "none", model, VLM_MISSING_KEY_WARNING if not (anthropic_key or openai_key) else (
            "VLM provider forced to none in config."
        )

    if req == "anthropic":
        if not anthropic_key:
            return "none", model, VLM_MISSING_KEY_WARNING
        return "anthropic", model or "claude-sonnet-4-5", ""
    if req == "openai":
        if not openai_key:
            return "none", model, VLM_MISSING_KEY_WARNING
        return "openai", model or "gpt-4o-mini", ""

    # auto
    if anthropic_key:
        return "anthropic", model or "claude-sonnet-4-5", ""
    if openai_key:
        return "openai", model or "gpt-4o-mini", ""
    return "none", model, VLM_MISSING_KEY_WARNING


def parse_vlm_text(text: str) -> VlmAnswer:
    cleaned = (text or "").strip()
    if not cleaned:
        return VlmAnswer(raw=cleaned, name="", description="", is_none=True, put_down=False)

    # Prefer JSON object anywhere in the reply.
    json_match = re.search(r"\{[^{}]*\}", cleaned, flags=re.DOTALL)
    if json_match:
        try:
            payload = json.loads(json_match.group(0))
            put_down = bool(payload.get("put_down"))
            obj = str(payload.get("object") or "").strip()
            place = str(payload.get("place") or "").strip()
            if not put_down or obj.lower() in ("", "none", "null"):
                return VlmAnswer(
                    raw=cleaned,
                    name="",
                    description="",
                    is_none=True,
                    put_down=False,
                    place="",
                )
            description = place or cleaned[:240]
            return VlmAnswer(
                raw=cleaned,
                name=obj[:40],
                description=description[:240],
                is_none=False,
                put_down=True,
                place=place[:120],
            )
        except json.JSONDecodeError:
            pass

    lower = cleaned.lower()
    if lower == "none" or lower.startswith("none.") or re.match(r"^none\b", lower):
        return VlmAnswer(raw=cleaned, name="", description="", is_none=True, put_down=False)
    name_match = re.search(r"(?im)^\s*(?:object|name)\s*[:\-]\s*(.+)$", cleaned)
    desc_match = re.search(r"(?im)^\s*(?:description|surface|scene|place)\s*[:\-]\s*(.+)$", cleaned)
    if name_match:
        name = name_match.group(1).strip().strip(".")
        description = desc_match.group(1).strip() if desc_match else cleaned
        if name.lower() == "none":
            return VlmAnswer(raw=cleaned, name="", description="", is_none=True, put_down=False)
        return VlmAnswer(
            raw=cleaned,
            name=name[:40],
            description=description[:240],
            is_none=False,
            put_down=True,
            place=(desc_match.group(1).strip()[:120] if desc_match else ""),
        )
    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    name = lines[0][:40]
    if name.lower() == "none":
        return VlmAnswer(raw=cleaned, name="", description="", is_none=True, put_down=False)
    description = " ".join(lines[1:])[:240] if len(lines) > 1 else cleaned[:240]
    return VlmAnswer(
        raw=cleaned,
        name=name,
        description=description,
        is_none=False,
        put_down=True,
        place="",
    )


def _openai_call(jpeg_list: list[bytes], model: str) -> str:
    from openai import OpenAI
    import base64

    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    client = OpenAI(api_key=key)
    content: list[dict] = [{"type": "text", "text": HOLD_PROMPT}]
    for jpeg in jpeg_list:
        b64 = base64.b64encode(jpeg).decode("ascii")
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": content}],
        max_tokens=160,
        temperature=0,
    )
    return (response.choices[0].message.content or "").strip()


def _anthropic_call(jpeg_list: list[bytes], model: str) -> str:
    from anthropic import Anthropic
    import base64

    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    client = Anthropic(api_key=key)
    content: list[dict] = [{"type": "text", "text": HOLD_PROMPT}]
    for jpeg in jpeg_list:
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": base64.b64encode(jpeg).decode("ascii"),
                },
            }
        )
    response = client.messages.create(
        model=model,
        max_tokens=160,
        messages=[{"role": "user", "content": content}],
    )
    parts = []
    for block in response.content:
        if getattr(block, "type", "") == "text":
            parts.append(block.text)
    return "\n".join(parts).strip()


def call_vlm(provider: str, jpeg_list: list[bytes], model: str) -> VlmAnswer:
    if provider in ("", "none", "auto"):
        return VlmAnswer(raw="", name="", description="", is_none=True, error="vlm disabled", put_down=False)
    try:
        if provider == "openai":
            text = _openai_call(jpeg_list, model or "gpt-4o-mini")
        elif provider == "anthropic":
            text = _anthropic_call(jpeg_list, model or "claude-sonnet-4-5")
        else:
            return VlmAnswer(
                raw="",
                name="",
                description="",
                is_none=True,
                error=f"unknown provider {provider}",
                put_down=False,
            )
        return parse_vlm_text(text)
    except Exception as error:
        message = f"{type(error).__name__}: {error}"
        # If the preferred key is bad, fall through to the other provider when available.
        lower = message.lower()
        if "auth" in lower or "401" in lower or ("invalid" in lower and "key" in lower):
            if provider == "anthropic" and os.environ.get("OPENAI_API_KEY", "").strip():
                return call_vlm("openai", jpeg_list, "" if "claude" in (model or "").lower() else model)
            if provider == "openai" and os.environ.get("ANTHROPIC_API_KEY", "").strip():
                return call_vlm("anthropic", jpeg_list, "" if "gpt" in (model or "").lower() else model)
        return VlmAnswer(
            raw="",
            name="",
            description="",
            is_none=True,
            error=message,
            put_down=False,
        )


class VlmWorker:
    def __init__(
        self,
        on_result: Callable[[dict, VlmAnswer], None],
        provider: str = "none",
        model: str = "",
        queue_size: int = 4,
    ) -> None:
        self.on_result = on_result
        self.provider = provider
        self.model = model
        self.queue: queue.Queue = queue.Queue(maxsize=max(1, queue_size))
        self._stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.done = 0
        self.dropped = 0
        self.error = ""

    def start(self) -> None:
        if self.provider in ("", "none"):
            return
        self.thread = threading.Thread(target=self._run, daemon=True, name="ring-vlm")
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self.queue.put_nowait(None)
        except queue.Full:
            pass
        if self.thread is not None:
            self.thread.join(timeout=8)

    def submit(self, job: dict) -> bool:
        if self.provider in ("", "none"):
            self.on_result(job, VlmAnswer(raw="", name="", description="", is_none=True, error="vlm disabled", put_down=False))
            return True
        try:
            self.queue.put_nowait(job)
            return True
        except queue.Full:
            self.dropped += 1
            self.on_result(
                job,
                VlmAnswer(raw="", name="", description="", is_none=True, error="vlm queue full", put_down=False),
            )
            return False

    def status(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model,
            "done": self.done,
            "dropped": self.dropped,
            "queue": self.queue.qsize(),
            "error": self.error,
        }

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                job = self.queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if job is None:
                return
            answer = call_vlm(self.provider, job.get("jpegs") or [], self.model)
            if answer.error:
                self.error = answer.error
            self.done += 1
            try:
                self.on_result(job, answer)
            except Exception as error:
                self.error = f"{type(error).__name__}: {error}"
