"""An explicit allowed_providers list can span OpenRouter and Venice."""

from types import MethodType, SimpleNamespace

from opencode_xmpp import bot as botmod


def _bridge(allowed=(), admin="openrouter/deepseek/x", guest=""):
    fake = SimpleNamespace(
        cfg=SimpleNamespace(
            policy=SimpleNamespace(owners=("local@example.org",),
                                   allowed_providers=allowed),
            admin=SimpleNamespace(model=admin),
            guest=SimpleNamespace(model=guest),
        ),
        _default_cards={},
    )
    for name in ("_allowed_provider", "_allowed_providers", "_provider_label",
                 "_model_allowed"):
        setattr(fake, name, MethodType(getattr(botmod.Bridge, name), fake))
    return fake


def test_empty_list_derives_the_single_provider():
    fake = _bridge()
    assert fake._allowed_providers() == ("openrouter",)
    assert fake._model_allowed("openrouter/minimax/minimax-m3.1-flash")
    assert not fake._model_allowed("venice/deepseek-v4-1-flash-1017-uncensored")


def test_explicit_list_allows_both_cloud_providers():
    fake = _bridge(allowed=("openrouter", "venice"))
    assert fake._allowed_providers() == ("openrouter", "venice")
    assert fake._model_allowed("openrouter/minimax/minimax-m3.1-flash")
    assert fake._model_allowed("venice/deepseek-v4-1-flash-1017-uncensored")
    assert not fake._model_allowed("ollama/gemma4:26b")
    assert fake._provider_label() == "openrouter, venice"
