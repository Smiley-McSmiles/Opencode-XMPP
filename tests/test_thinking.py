from opencode_xmpp import approvals, thinking


def test_think_command_is_a_bare_word():
    assert approvals.think_command("think-off") == "off"
    assert approvals.think_command("think_on:") == "on"
    assert approvals.think_command("think off") is None
    assert approvals.think_command("think-off please") is None
    assert approvals.think_command("restart") is None


def test_ollama_model_id():
    assert thinking.ollama_model_id("ollama/gemma4:26b-64k") == "gemma4:26b-64k"
    assert thinking.ollama_model_id("ollama/gemma4:26b-64k#none") == "gemma4:26b-64k"
    assert thinking.ollama_model_id("openrouter/deepseek/deepseek-v4.1-flash-1017#low") is None
    assert thinking.ollama_model_id("ollama/") is None


def test_apply_thinking_turns_thinking_off_and_back_on():
    cfg = {"providers": {"ollama": {"models": {"gemma4:26b-64k": {"limit": {"context": 65536}}}}}}
    changed = thinking.apply_thinking(cfg, ["ollama/gemma4:26b-64k"], False)
    model = cfg["providers"]["ollama"]["models"]["gemma4:26b-64k"]
    assert changed == ["ollama/gemma4:26b-64k"]
    assert model["capabilities"]["reasoning"] == {"supported": False}
    assert model["body"] == {"think": False}
    assert model["limit"] == {"context": 65536}

    changed = thinking.apply_thinking(cfg, ["ollama/gemma4:26b-64k"], True)
    assert changed == ["ollama/gemma4:26b-64k"]
    assert model["capabilities"]["reasoning"] == {"supported": True}
    assert "body" not in model


def test_apply_thinking_ignores_openrouter_models():
    cfg = {"providers": {"ollama": {"models": {}}}}
    assert thinking.apply_thinking(cfg, ["openrouter/deepseek/deepseek-v4.1-flash-1017"], False) == []
    assert cfg["providers"]["ollama"]["models"] == {}


def test_help_and_model_commands():
    assert approvals.help_command("help") is True
    assert approvals.help_command("?") is True
    assert approvals.help_command("help me") is False
    assert approvals.model_command("model ollama/gemma4:26b-64k") == "ollama/gemma4:26b-64k"
    assert approvals.model_command("model") == ""
    assert approvals.model_command("model this later") == "this later"
    assert approvals.model_command("mode") is None


def test_model_cards_load_find_render_and_apply(tmp_path):
    import json
    from opencode_xmpp import cards

    path = tmp_path / "cards.json"
    path.write_text(json.dumps([{
        "id": "ada",
        "name": "Ada (Experimental)",
        "base_model_id": "gemma4:26b-64k",
        "params": {
            "system": "You are {{char}}. Talk to {{user}}.",
            "think": False,
            "num_ctx": 65536,
            "temperature": 0.7,
            "top_p": 0.9,
            "top_k": 40,
            "repeat_penalty": 1.1,
            "max_tokens": 2048,
            "stop": "<turn|>",
        },
    }]), encoding="utf-8")

    found = cards.load_cards(tmp_path)
    assert len(found) == 1
    card = cards.find_card(found, "ADA")
    assert card is not None
    assert card.base_model == "ollama/gemma4:26b-64k"
    assert card.model == card.base_model
    assert cards.render_system(card, "local@example.org") == (
        "You are Ada. Talk to local.")

    cfg = {"providers": {"ollama": {"models": {}}}}
    changed = cards.apply_params(cfg, card)
    model = cfg["providers"]["ollama"]["models"]["gemma4:26b-64k"]
    assert "ollama/gemma4:26b-64k" in changed
    assert model["limit"]["context"] == 65536
    assert model["capabilities"]["reasoning"]["supported"] is False
    assert model["body"]["think"] is False
    options = model["body"]["options"]
    assert options["num_ctx"] == 65536
    assert options["temperature"] == 0.7
    assert options["top_p"] == 0.9
    assert options["top_k"] == 40
    assert options["repeat_penalty"] == 1.1
    assert options["num_predict"] == 2048
    assert options["stop"] == ["<turn|>"]


def test_opencode_config_accepts_jsonc_comments(tmp_path):
    from opencode_xmpp.bot import load_opencode_config, strip_jsonc

    path = tmp_path / "opencode.jsonc"
    path.write_text('''{
      // a line comment
      "providers": {"ollama": {"models": {}}}, /* a block comment */
      "update": "disable",
    }''', encoding="utf-8")
    assert load_opencode_config(path) == {
        "providers": {"ollama": {"models": {}}},
        "update": "disable",
    }
    # A URL inside a string is not a comment.
    assert strip_jsonc('{"url": "http://127.0.0.1:11434/v1"}') == (
        '{"url": "http://127.0.0.1:11434/v1"}')
