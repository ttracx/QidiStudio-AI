"""Vision-provider adapters for OpenAI, local Ollama, and Ollama Cloud."""
from __future__ import annotations

import base64
import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .models import ProviderAssessment

logger = logging.getLogger("thoxforge.print_health.providers")


ASSESSMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "quality_score": {"type": "integer", "minimum": 0, "maximum": 100},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "severity": {"type": "string", "enum": ["ok", "warning", "critical"]},
        "recommended_action": {
            "type": "string",
            "enum": ["continue", "pause", "cancel", "emergency_stop", "ask_user"],
        },
        "diagnosis": {"type": "string"},
        "detections": {
            "type": "array",
            "maxItems": 16,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "defect": {
                        "type": "string",
                        "enum": [
                            "spaghetti", "detachment", "first_layer", "adhesion",
                            "warping", "layer_shift", "stringing", "under_extrusion",
                            "over_extrusion", "blob", "clog", "support_failure",
                            "collision", "smoke_or_fire", "unknown",
                        ],
                    },
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "severity": {"type": "string", "enum": ["ok", "warning", "critical"]},
                    "bbox": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "x1": {"type": "integer", "minimum": 0, "maximum": 1000},
                            "y1": {"type": "integer", "minimum": 0, "maximum": 1000},
                            "x2": {"type": "integer", "minimum": 0, "maximum": 1000},
                            "y2": {"type": "integer", "minimum": 0, "maximum": 1000},
                        },
                        "required": ["x1", "y1", "x2", "y2"],
                    },
                    "note": {"type": "string"},
                },
                "required": ["defect", "confidence", "severity", "bbox", "note"],
            },
        },
    },
    "required": [
        "quality_score", "confidence", "severity", "recommended_action",
        "diagnosis", "detections",
    ],
}

SYSTEM_PROMPT = """You are THOX Print Health, a conservative visual inspector for FDM 3D printing.
Use only evidence visible in the supplied printer camera frame plus telemetry. Detect and
localize spaghetti/detachment, first-layer or adhesion problems, warping, layer shifts,
stringing, under/over-extrusion, blobs, clogs, support failures, collisions, and visible
smoke/fire. Bboxes use normalized 0..1000 coordinates. Do not infer hidden failures.

Severity policy:
- ok: no actionable defect is visible.
- warning: quality degradation or uncertainty worth watching; normally continue.
- critical: high probability the print is failing or could damage the machine; recommend pause.
- emergency_stop only for credible visible smoke/fire or an immediate collision hazard.

Be conservative: an ambiguous single frame is not enough to recommend cancel. Return only
schema-compliant JSON; keep diagnosis concise and evidence-based."""


class ProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    model: str
    endpoint: str
    api_key: str = ""
    strict_schema: bool = True
    timeout_s: float = 45.0


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str], timeout_s: float) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as response:
            raw = response.read(8 * 1024 * 1024)
    except urllib.error.HTTPError as exc:
        body = exc.read(4096).decode("utf-8", errors="replace")
        raise ProviderError(f"HTTP {exc.code} from {url}: {body[:500]}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ProviderError(f"request to {url} failed: {exc}") from exc
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderError(f"non-JSON response from {url}") from exc
    if not isinstance(parsed, dict):
        raise ProviderError(f"unexpected response type from {url}")
    return parsed


def _extract_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            value = json.loads(text[start:end + 1])
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass
    raise ProviderError("model response did not contain a valid JSON object")


class OpenAIProvider:
    name = "openai"

    def __init__(self, model: str, api_key: str, base_url: str, timeout_s: float) -> None:
        self.model = model
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    def analyze(self, image: bytes, mime_type: str, context: str) -> ProviderAssessment:
        if not self.api_key:
            raise ProviderError("OPENAI_API_KEY is not configured")
        encoded = base64.b64encode(image).decode("ascii")
        payload = {
            "model": self.model,
            "store": False,
            "input": [
                {
                    "role": "system",
                    "content": [{"type": "input_text", "text": SYSTEM_PROMPT}],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": f"Printer telemetry/context:\n{context}"},
                        {
                            "type": "input_image",
                            "image_url": f"data:{mime_type};base64,{encoded}",
                            "detail": "high",
                        },
                    ],
                },
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "print_health_assessment",
                    "strict": True,
                    "schema": ASSESSMENT_SCHEMA,
                }
            },
            "max_output_tokens": 1200,
        }
        body = _post_json(
            self.base_url + "/responses",
            payload,
            {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            self.timeout_s,
        )
        text = ""
        for output in body.get("output", []):
            if not isinstance(output, dict) or output.get("type") != "message":
                continue
            for part in output.get("content", []):
                if isinstance(part, dict) and part.get("type") == "output_text":
                    text += str(part.get("text", ""))
        if not text:
            raise ProviderError("OpenAI response contained no output_text")
        return ProviderAssessment.from_json(self.name, self.model, _extract_json_object(text))


class OllamaProvider:
    def __init__(
        self,
        name: str,
        model: str,
        base_url: str,
        timeout_s: float,
        api_key: str = "",
        structured_outputs: bool = True,
    ) -> None:
        self.name = name
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.api_key = api_key
        self.structured_outputs = structured_outputs

    def analyze(self, image: bytes, mime_type: str, context: str) -> ProviderAssessment:
        del mime_type
        if not self.model:
            raise ProviderError(f"{self.name} model is not configured")
        prompt = (
            f"{SYSTEM_PROMPT}\n\nPrinter telemetry/context:\n{context}\n"
            "Return the assessment now."
        )
        if not self.structured_outputs:
            prompt += " Return one JSON object only, with no markdown fences."
        payload: dict[str, Any] = {
            "model": self.model,
            "stream": False,
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                    "images": [base64.b64encode(image).decode("ascii")],
                }
            ],
            "options": {"temperature": 0},
        }
        if self.structured_outputs:
            payload["format"] = ASSESSMENT_SCHEMA
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = _post_json(
            self.base_url + "/api/chat",
            payload,
            headers,
            self.timeout_s,
        )
        message = body.get("message", {})
        text = str(message.get("content", "")) if isinstance(message, dict) else ""
        if not text:
            raise ProviderError(f"{self.name} response contained no message content")
        return ProviderAssessment.from_json(self.name, self.model, _extract_json_object(text))


def build_providers(timeout_s: float) -> list[Any]:
    """Build enabled providers entirely from environment variables.

    OpenAI and local Ollama are enabled when their credentials/model are present.
    Direct Ollama Cloud is enabled only when OLLAMA_API_KEY is configured. Cloud
    intentionally does not request Ollama structured outputs because that feature is
    currently documented as local-only; JSON is validated after the response instead.
    """
    providers: list[Any] = []
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    if openai_key:
        providers.append(
            OpenAIProvider(
                model=os.getenv("THOX_OPENAI_VISION_MODEL", "gpt-5.6-luna"),
                api_key=openai_key,
                base_url=os.getenv("THOX_OPENAI_BASE_URL", "https://api.openai.com/v1"),
                timeout_s=timeout_s,
            )
        )

    local_model = os.getenv("THOX_OLLAMA_LOCAL_MODEL", "qwen3-vl:4b").strip()
    if local_model:
        providers.append(
            OllamaProvider(
                name="ollama-local",
                model=local_model,
                base_url=os.getenv("THOX_OLLAMA_LOCAL_URL", "http://127.0.0.1:11434"),
                timeout_s=timeout_s,
                structured_outputs=True,
            )
        )

    cloud_key = os.getenv("OLLAMA_API_KEY", "").strip()
    cloud_model = os.getenv("THOX_OLLAMA_CLOUD_MODEL", "qwen3-vl:235b").strip()
    if cloud_key and cloud_model:
        providers.append(
            OllamaProvider(
                name="ollama-cloud",
                model=cloud_model,
                base_url=os.getenv("THOX_OLLAMA_CLOUD_URL", "https://ollama.com"),
                timeout_s=timeout_s,
                api_key=cloud_key,
                structured_outputs=False,
            )
        )
    return providers
