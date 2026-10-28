"""Toggle Ollama thinking for the models this bridge is configured to use.

OpenCode reads ``providers.ollama.models.<id>.capabilities.reasoning`` and the
per-model request ``body``.  ``think-off`` sets ``reasoning.supported = false``
and ``body.think = false``; ``think-on`` clears those overrides.  This only
touches the matching ``ollama/`` models, so it is a no-op for OpenRouter models.
"""

from __future__ import annotations

from typing import Iterable


def ollama_model_id(model: str) -> str | None:
    """``gemma4:26b-64k`` for ``ollama/gemma4:26b-64k[#variant]``, else None."""
    provider, sep, rest = (model or "").strip().partition("/")
    if not sep or provider.strip().lower() != "ollama":
        return None
    model_id = rest.split("#", 1)[0].strip()
    return model_id or None


def apply_thinking(config: dict, models: Iterable[str], enabled: bool) -> list[str]:
    """Update *config* in place and return the affected ``ollama/<id>`` names."""
    changed: list[str] = []
    for model in models:
        model_id = ollama_model_id(model)
        if model_id is None:
            continue

        models_cfg = (
            config.setdefault("providers", {})
            .setdefault("ollama", {})
            .setdefault("models", {})
        )
        model_cfg = models_cfg.setdefault(model_id, {})

        if enabled:
            capabilities = model_cfg.get("capabilities")
            if isinstance(capabilities, dict):
                reasoning = capabilities.get("reasoning")
                if isinstance(reasoning, dict):
                    reasoning["supported"] = True
                    if not reasoning:
                        capabilities.pop("reasoning", None)
                if not capabilities:
                    model_cfg.pop("capabilities", None)
            body = model_cfg.get("body")
            if isinstance(body, dict):
                body.pop("think", None)
                if not body:
                    model_cfg.pop("body", None)
        else:
            capabilities = model_cfg.setdefault("capabilities", {})
            if not isinstance(capabilities, dict):
                capabilities = model_cfg["capabilities"] = {}
            reasoning = capabilities.get("reasoning")
            if not isinstance(reasoning, dict):
                reasoning = capabilities["reasoning"] = {}
            reasoning["supported"] = False
            body = model_cfg.setdefault("body", {})
            if not isinstance(body, dict):
                body = model_cfg["body"] = {}
            body["think"] = False

        changed.append(f"ollama/{model_id}")

    return changed
