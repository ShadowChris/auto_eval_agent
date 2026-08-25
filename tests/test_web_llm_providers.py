import json
from pathlib import Path

from auto_eval.config import AppConfig, EvalOptions, JudgeConfig
from auto_eval.web import server
from auto_eval.web.history import jsonl_export_rows
from auto_eval.web.llm_providers import LLMProviderPayload, LLMProviderStore


def _cfg() -> AppConfig:
    return AppConfig(
        models=[],
        judges=[JudgeConfig(
            name="judge_2",
            display="终端用户",
            base_url="https://default.test/v1",
            api_key_value="default-key",
            model="default-model",
            persona="end_user",
        )],
        rubrics=[],
        eval_options=EvalOptions(
            classify_model="old-classifier",
            classify_base_url="https://classifier.test/v1",
        ),
    )


def _payload(provider_id: str, base_url: str, model: str, key: str):
    return LLMProviderPayload(
        id=provider_id,
        name=provider_id.upper(),
        base_url=base_url,
        models=[model],
        default_model=model,
        api_key=key,
    )


def test_provider_store_encrypts_key_and_never_returns_it(tmp_path: Path):
    store = LLMProviderStore(tmp_path / "settings")
    public = store.create(_payload(
        "kimi",
        "https://api.example.test/v1/",
        "kimi-model",
        "secret-key-value",
    ))

    raw = store.path.read_text(encoding="utf-8")
    assert "secret-key-value" not in raw
    assert "api_key" not in public
    assert public["base_url"] == "https://api.example.test/v1"
    assert public["has_api_key"] is True
    assert store.key_path.is_file()

    resolved = store.resolve("kimi", "", _cfg())
    assert resolved.api_key == "secret-key-value"
    assert resolved.model == "kimi-model"


def test_provider_update_with_empty_key_keeps_existing_secret(tmp_path: Path):
    store = LLMProviderStore(tmp_path / "settings")
    store.create(_payload("p1", "https://one.test/v1", "m1", "key-1"))
    updated = store.update(
        "p1",
        LLMProviderPayload(
            id="p1",
            name="Provider One",
            base_url="https://two.test/v1",
            models=["m2"],
            default_model="m2",
            api_key=None,
        ),
    )

    resolved = store.resolve("p1", "", _cfg())
    assert updated["has_api_key"] is True
    assert resolved.api_key == "key-1"
    assert resolved.base_url == "https://two.test/v1"
    assert resolved.model == "m2"


def test_builtin_providers_are_grouped_by_connection_not_judge_role(
    tmp_path: Path,
):
    cfg = AppConfig(
        models=[],
        judges=[
            JudgeConfig(
                name="judge_1", display="研发人员",
                base_url="https://proxy.test/v1", api_key_env="PROXY_API_KEY",
                api_key_value="proxy-key", model="model-a",
            ),
            JudgeConfig(
                name="judge_2", display="终端用户",
                base_url="https://silicon.test/v1", api_key_env="SILICON_API_KEY",
                api_key_value="silicon-key", model="model-b",
            ),
            JudgeConfig(
                name="judge_3", display="产品专家",
                base_url="https://proxy.test/v1", api_key_env="PROXY_API_KEY",
                api_key_value="proxy-key", model="model-c",
            ),
        ],
        rubrics=[],
    )
    store = LLMProviderStore(tmp_path / "settings")

    providers = store.list_public(cfg)

    assert len(providers) == 2
    assert all("配置默认" not in item["name"] for item in providers)
    assert all(role not in json.dumps(providers, ensure_ascii=False) for role in (
        "研发人员", "终端用户", "产品专家",
    ))
    proxy = next(item for item in providers if item["base_url"] == "https://proxy.test/v1")
    assert proxy["models"] == ["model-a", "model-c"]
    # 已经写入历史的旧 builtin-judge_X 仍可继续解析和重跑。
    legacy = store.resolve("builtin-judge_3", "model-c", cfg)
    assert legacy.api_key == "proxy-key"
    assert legacy.model == "model-c"


def test_task_runtime_provider_binding_is_sanitized_and_isolated(
    tmp_path: Path,
    monkeypatch,
):
    store = LLMProviderStore(tmp_path / "settings")
    store.create(_payload("p1", "https://one.test/v1", "m1", "key-1"))
    store.create(_payload("p2", "https://two.test/v1", "m2", "key-2"))
    monkeypatch.setattr(server, "_llm_provider_store", lambda: store)
    app_cfg = _cfg()

    options1, runtime1 = server._normalize_eval_options(
        app_cfg,
        {"judges": ["judge_2"], "judge_backend": {"provider_id": "p1", "model": "m1"}},
    )
    options2, runtime2 = server._normalize_eval_options(
        app_cfg,
        {"judges": ["judge_2"], "judge_backend": {"provider_id": "p2", "model": "m2"}},
    )

    assert app_cfg.judges[0].base_url == "https://default.test/v1"
    assert runtime1.judges[0].base_url == "https://one.test/v1"
    assert runtime1.judges[0].model == "m1"
    assert runtime1.judges[0].api_key() == "key-1"
    assert runtime1.eval_options.classify_model == "m1"
    assert runtime2.judges[0].base_url == "https://two.test/v1"
    assert runtime2.judges[0].model == "m2"
    assert runtime2.judges[0].api_key() == "key-2"
    assert "key-1" not in json.dumps(options1, ensure_ascii=False)
    assert "key-2" not in json.dumps(options2, ensure_ascii=False)
    assert options1["judge_backend"] == {
        "provider_id": "p1",
        "provider_name": "P1",
        "model": "m1",
        "base_url_snapshot": "https://one.test/v1",
        "provider_revision": options1["judge_backend"]["provider_revision"],
        "builtin": False,
    }


def test_frontend_exposes_provider_switch_and_management():
    root = Path(__file__).resolve().parents[1]
    html = (root / "src/auto_eval/web/static/index.html").read_text(encoding="utf-8")
    js = (root / "src/auto_eval/web/static/app.js").read_text(encoding="utf-8")

    assert "Provider：" in html
    assert "Provider / 模型" in html
    assert "管理模型服务" in html
    assert 'fetch("/api/llm-providers")' in js
    assert "judge_backend" in js
    assert "providerModelOptions" in js
    assert "providerApiErrorText" in js
    assert "Provider ID 仅支持" in js


def test_jsonl_export_records_provider_without_secret():
    rows = jsonl_export_rows({
        "task_id": "task-1",
        "mode": "operation",
        "status": "done",
        "items": [{"id": "q1", "query": "打开设置"}],
        "results": [],
        "options": {
            "judge_backend": {
                "provider_id": "p1",
                "provider_name": "Provider One",
                "model": "m1",
                "base_url_snapshot": "https://one.test/v1",
                "provider_revision": "rev-1",
            },
        },
    })

    eval_run = rows[0]["eval_run"]
    assert eval_run["judge_provider"] == "Provider One"
    assert eval_run["judge_provider_id"] == "p1"
    assert eval_run["judge_model"] == "m1"
    assert eval_run["judge_provider_revision"] == "rev-1"
    assert "api_key" not in json.dumps(rows, ensure_ascii=False)
