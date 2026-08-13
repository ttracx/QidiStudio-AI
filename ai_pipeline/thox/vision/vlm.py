"""Language-model providers: Ollama (local and cloud) and OpenAI.

They differ only in wire format, so the response handling is shared and each
class supplies its own request shape and availability rule.

Four behaviours were established by measurement against a live host rather than
from documentation, and each one silently breaks detection if ignored:

1. **Never send Ollama ``format: "json"``.** With it, ``qwen3-vl`` returned an
   empty string after 183 s. Without it, the same request returned well-formed
   JSON in 4.9 s.

2. **The token budget must clear the reasoning phase.** ``qwen3-vl`` writes its
   chain of thought to a separate ``thinking`` field and only then answers. At
   ``num_predict`` 400 and 1200 it hit ``done_reason: "length"`` mid-thought and
   returned ``""``; at 3000 it succeeded, but reasoning length varies run to run,
   so the default is 6000. Neither ``think: false`` nor a ``/no_think`` prefix
   suppressed reasoning on the abliterated build.

3. **Models get evicted despite ``keep_alive``.** ``/api/ps`` showed nothing
   resident between calls, so a later request paid a full cold reload (354 s
   observed against ~5 s warm). Timeouts are sized for a cold load.

4. **Not every model advertising vision has a working vision path.** The
   mistral3-family models on the measured host returned ``""`` after 270 s.
   A provider returning nothing is reported as *skipped*, never as "no defects
   found" - "I could not look" and "I looked and it is fine" must never collapse
   into the same answer, because one of them would silently green-light a
   failing print.

Frames leave the local network for the cloud and OpenAI providers. Both stay
inactive until a key is configured, and :attr:`sends_frames_offsite` lets the UI
say so plainly.
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any

import requests

from ..config import ThoxSettings
from ..defects import Detection, DefectKind, ProviderReport, parse_kind
from .base import FrameContext
from .prompts import health_prompt


def extract_json(text: str) -> dict[str, Any]:
    """Pull the first complete JSON object out of a model response.

    Brace-matching that skips string literals, because models wrap JSON in
    prose or fences however firmly the prompt forbids it, and a regex trips over
    braces inside a ``note`` field.
    """
    if not text or not text.strip():
        raise ValueError("empty response")
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in response")

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                parsed = json.loads(text[start : index + 1])
                if not isinstance(parsed, dict):
                    raise ValueError("top-level JSON value is not an object")
                return parsed
    raise ValueError("unterminated JSON object in response")


def _norm_bbox(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        numbers = [float(v) for v in value]
    except (TypeError, ValueError):
        return None
    if any(n != n for n in numbers):
        return None
    # Models routinely answer in pixels despite being asked for fractions.
    if max(numbers) > 1.5:
        width = max(numbers[0], numbers[2], 1.0)
        height = max(numbers[1], numbers[3], 1.0)
        numbers = [
            numbers[0] / width,
            numbers[1] / height,
            numbers[2] / width,
            numbers[3] / height,
        ]
    x0, x1 = sorted((numbers[0], numbers[2]))
    y0, y1 = sorted((numbers[1], numbers[3]))
    clamp = lambda v: max(0.0, min(1.0, v))  # noqa: E731
    return (clamp(x0), clamp(y0), clamp(x1), clamp(y1))


def parse_defects(payload: dict[str, Any], provider: str) -> tuple[list[Detection], str]:
    """Turn a parsed model response into detections plus a summary."""
    detections: list[Detection] = []
    raw = payload.get("defects")
    if isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            kind = parse_kind(entry.get("kind"))
            if kind is None:
                # Unknown label: dropped rather than guessed at. An invented
                # defect kind has no fix mapping and nothing sane to do with it.
                continue
            try:
                confidence = float(entry.get("confidence", 0.5))
            except (TypeError, ValueError):
                confidence = 0.5
            detections.append(
                Detection(
                    kind=kind,
                    confidence=confidence,
                    provider=provider,
                    note=str(entry.get("note") or "")[:300],
                    bbox_norm=_norm_bbox(entry.get("bbox_norm")),
                )
            )

    if payload.get("frame_usable") is False:
        detections.append(
            Detection(
                kind=DefectKind.CAMERA_FAULT,
                confidence=0.7,
                provider=provider,
                note=str(payload.get("overall_note") or "model reports frame unusable")[:300],
            )
        )
    return detections, str(payload.get("overall_note") or "")[:300]


class _VlmProvider:
    """Shared behaviour for the language-model members."""

    name = "vlm"
    sends_frames_offsite = True
    #: Seconds to minutes per call, so held back until something looks wrong.
    fast = False

    def __init__(self, settings: ThoxSettings) -> None:
        self.settings = settings
        self._session = requests.Session()

    # -- subclass hooks -----------------------------------------------------

    def available(self) -> tuple[bool, str]:
        raise NotImplementedError

    def _endpoint(self) -> str:
        raise NotImplementedError

    def _payload(self, jpeg: bytes, context: FrameContext) -> dict[str, Any]:
        raise NotImplementedError

    def _headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json"}

    def _extract_text(self, body: dict[str, Any]) -> tuple[str, str]:
        """Return ``(answer_text, diagnostic)``."""
        raise NotImplementedError

    # -- shared -------------------------------------------------------------

    def warmup(self) -> None:
        return None

    def inspect(self, jpeg: bytes, context: FrameContext) -> ProviderReport:
        started = time.perf_counter()
        ok, why = self.available()
        if not ok:
            return ProviderReport(provider=self.name, ok=False, skipped_reason=why)

        try:
            response = self._session.post(
                self._endpoint(),
                json=self._payload(jpeg, context),
                headers=self._headers(),
                timeout=self.settings.provider_timeout_s,
            )
        except requests.Timeout:
            return ProviderReport(
                provider=self.name,
                ok=False,
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
                skipped_reason=(
                    f"timed out after {self.settings.provider_timeout_s:.0f}s "
                    "(the model may be cold-loading)"
                ),
            )
        except requests.RequestException as exc:
            return ProviderReport(
                provider=self.name,
                ok=False,
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
                skipped_reason=f"transport error ({type(exc).__name__})",
            )

        elapsed = (time.perf_counter() - started) * 1000.0
        if response.status_code == 401:
            return ProviderReport(
                provider=self.name, ok=False, elapsed_ms=elapsed,
                skipped_reason="HTTP 401: API key rejected",
            )
        if response.status_code == 429:
            return ProviderReport(
                provider=self.name, ok=False, elapsed_ms=elapsed,
                skipped_reason="HTTP 429: rate limited",
            )
        if response.status_code >= 400:
            return ProviderReport(
                provider=self.name, ok=False, elapsed_ms=elapsed,
                skipped_reason=f"HTTP {response.status_code}",
            )

        try:
            body = response.json()
        except ValueError:
            return ProviderReport(
                provider=self.name, ok=False, elapsed_ms=elapsed,
                skipped_reason="non-JSON envelope",
            )

        text, diagnostic = self._extract_text(body)
        if not text.strip():
            return ProviderReport(
                provider=self.name, ok=False, elapsed_ms=elapsed,
                skipped_reason=(
                    f"model returned no answer ({diagnostic}). Reported as "
                    "skipped, NOT as a healthy print."
                ),
            )

        try:
            payload = extract_json(text)
        except ValueError as exc:
            return ProviderReport(
                provider=self.name, ok=False, elapsed_ms=elapsed,
                skipped_reason=f"unparseable response ({exc})",
            )

        detections, summary = parse_defects(payload, self.name)
        return ProviderReport(
            provider=self.name,
            ok=True,
            elapsed_ms=elapsed,
            detections=detections,
            summary=summary or ("healthy" if not detections else ""),
        )


class OllamaLocalHealth(_VlmProvider):
    """Vision on the operator's own hardware. Frames stay on the LAN."""

    name = "ollama_local"
    sends_frames_offsite = False

    def available(self) -> tuple[bool, str]:
        if not self.settings.ollama_base_url:
            return False, "ollama_local: set THOX_OLLAMA_BASE_URL"
        if not self.settings.ollama_model:
            return False, "ollama_local: no model configured"
        return True, f"ollama_local: {self.settings.ollama_model}"

    def _endpoint(self) -> str:
        return f"{self.settings.ollama_base_url}/api/generate"

    def _payload(self, jpeg: bytes, context: FrameContext) -> dict[str, Any]:
        return {
            "model": self.settings.ollama_model,
            "prompt": health_prompt(context),
            "images": [base64.b64encode(jpeg).decode("ascii")],
            "stream": False,
            # NO "format": "json". See the module docstring.
            "options": {
                "temperature": 0.1,
                "num_predict": self.settings.ollama_num_predict,
            },
            "keep_alive": "30m",
        }

    def _extract_text(self, body: dict[str, Any]) -> tuple[str, str]:
        text = str(body.get("response") or "")
        if text.strip():
            return text, ""
        # A thinking model that exhausted its budget leaves the answer, if it
        # got that far, only in `thinking`. Worth salvaging before discarding a
        # call that may have taken minutes.
        thinking = str(body.get("thinking") or "")
        if thinking.strip():
            try:
                extract_json(thinking)
                return thinking, "recovered from the reasoning channel"
            except ValueError:
                pass
        return "", (
            f"done_reason={body.get('done_reason')!r}; if this is 'length', "
            "raise THOX_OLLAMA_NUM_PREDICT"
        )

    def warmup(self) -> None:
        """Pay the cold-load cost once, before monitoring starts."""
        ok, _ = self.available()
        if not ok:
            return
        try:
            self._session.post(
                self._endpoint(),
                json={
                    "model": self.settings.ollama_model,
                    "prompt": "ready",
                    "stream": False,
                    "options": {"num_predict": 1},
                    "keep_alive": "30m",
                },
                timeout=self.settings.provider_timeout_s,
            )
        except requests.RequestException:
            # A failed warmup is not an error; the first real call pays the cost.
            return


class OllamaCloudHealth(OllamaLocalHealth):
    """Ollama Cloud. Frames leave the LAN, so it is off until a key is set."""

    name = "ollama_cloud"
    sends_frames_offsite = True

    def available(self) -> tuple[bool, str]:
        if not self.settings.ollama_cloud_api_key:
            return False, (
                "ollama_cloud: no API key (THOX_OLLAMA_CLOUD_API_KEY). Frames "
                "would leave the LAN, so this stays off until configured."
            )
        return True, f"ollama_cloud: {self.settings.ollama_cloud_model}"

    def _endpoint(self) -> str:
        return f"{self.settings.ollama_cloud_base_url}/api/generate"

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.settings.ollama_cloud_api_key}",
        }

    def _payload(self, jpeg: bytes, context: FrameContext) -> dict[str, Any]:
        payload = super()._payload(jpeg, context)
        payload["model"] = self.settings.ollama_cloud_model
        return payload

    def warmup(self) -> None:
        # Nothing is resident locally; a warmup call costs tokens and buys
        # nothing.
        return None


class OpenAIHealth(_VlmProvider):
    """OpenAI vision. An independent model family, so it fails differently.

    That independence is the point of including it: an ensemble whose members
    share a lineage also shares blind spots, and then it is just a slow single
    model.
    """

    name = "openai"
    sends_frames_offsite = True

    def available(self) -> tuple[bool, str]:
        if not self.settings.openai_api_key:
            return False, (
                "openai: no API key (THOX_OPENAI_API_KEY). Note that the "
                "placeholder value 'ollama' is treated as unset."
            )
        return True, f"openai: {self.settings.openai_model}"

    def _endpoint(self) -> str:
        return f"{self.settings.openai_base_url}/chat/completions"

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.settings.openai_api_key}",
        }

    def _payload(self, jpeg: bytes, context: FrameContext) -> dict[str, Any]:
        data_uri = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")
        return {
            "model": self.settings.openai_model,
            "temperature": 0.1,
            "max_tokens": 700,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": health_prompt(context)},
                        {
                            "type": "image_url",
                            # "low" detail: the source is 640x480, so
                            # high-detail tiling spends tokens re-encoding
                            # information the sensor never captured.
                            "image_url": {"url": data_uri, "detail": "low"},
                        },
                    ],
                }
            ],
        }

    def _extract_text(self, body: dict[str, Any]) -> tuple[str, str]:
        try:
            return str(body["choices"][0]["message"]["content"]), ""
        except (KeyError, IndexError, TypeError):
            return "", "unexpected response shape"
