from vigil.identity import compute_agent_version, git_sha


def test_hash_is_stable_and_behavior_sensitive():
    a = compute_agent_version(
        prompts={"system": "You are helpful."},
        tools=[{"name": "search", "input_schema": {"type": "object"}}],
        model="claude-haiku-4-5",
        params={"temperature": 0.2, "max_tokens": 1024},
    )
    b = compute_agent_version(
        prompts={"system": "You are helpful."},
        tools=[{"name": "search", "input_schema": {"type": "object"}}],
        model="claude-haiku-4-5",
        params={"temperature": 0.2, "max_tokens": 1024},
    )
    assert a == b
    # A one-character prompt change flips the hash.
    c = compute_agent_version(
        prompts={"system": "You are helpful!"},
        tools=[{"name": "search", "input_schema": {"type": "object"}}],
        model="claude-haiku-4-5",
        params={"temperature": 0.2, "max_tokens": 1024},
    )
    assert a != c
    # A model change flips the hash.
    d = compute_agent_version(prompts={"system": "You are helpful."}, model="claude-sonnet-5")
    assert a != d


def test_params_key_order_independent():
    a = compute_agent_version(prompts={}, model="m", params={"temperature": 0.2, "top_p": 0.9})
    b = compute_agent_version(prompts={}, model="m", params={"top_p": 0.9, "temperature": 0.2})
    assert a == b


def test_prompt_whitespace_is_not_normalized():
    one_space = compute_agent_version(prompts={"p": "a b"}, model="m")
    two_spaces = compute_agent_version(prompts={"p": "a  b"}, model="m")
    trailing = compute_agent_version(prompts={"p": "a b "}, model="m")
    assert one_space != two_spaces
    assert one_space != trailing


def test_code_paths_hashed(tmp_path):
    f = tmp_path / "agent.py"
    f.write_text("print(1)\n")
    before = compute_agent_version(prompts={}, model="m", code_paths=[str(f)])
    f.write_text("print(2)\n")
    after = compute_agent_version(prompts={}, model="m", code_paths=[str(f)])
    assert before != after


def test_git_sha_prefers_env(monkeypatch):
    monkeypatch.setenv("VIGIL_AGENT_GIT_SHA", "deadbeefcafe")
    assert git_sha() == "deadbeefcafe"
