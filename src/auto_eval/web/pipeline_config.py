"""任务类阶段资源配置，不改变批跑接口的 concurrency 含义。"""
from dataclasses import asdict, dataclass, fields
from functools import lru_cache
import json
import os
from pathlib import Path
import tempfile
import threading

import yaml

from ..paths import PROJECT_ROOT, RUNS_DIR


CONFIG_PATH = PROJECT_ROOT / "config" / "operation_pipeline.yaml"


@dataclass(frozen=True)
class OperationPipelineConfig:
    download_concurrency: int = 10
    extraction_concurrency: int = 0
    model_concurrency: int = 0


def validate_pipeline_config(data: dict) -> OperationPipelineConfig:
    names = {field.name for field in fields(OperationPipelineConfig)}
    if not isinstance(data, dict) or set(data) - names:
        raise ValueError("operation_pipeline.yaml 配置字段无效")
    for name, value in data.items():
        minimum = 1 if name == "download_concurrency" else 0
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= 128:
            raise ValueError(f"{name} 必须是 {minimum}–128 的整数")
    return OperationPipelineConfig(**data)


@lru_cache(maxsize=1)
def load_pipeline_config() -> OperationPipelineConfig:
    """部署默认值；Web 保存的配置优先，默认值本身不被改写。"""
    data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
    return validate_pipeline_config({} if data is None else data)


class PipelineConfigStore:
    """原子持久化服务级并发配置，运行中的调度器直接读取内存快照。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._config: OperationPipelineConfig | None = None

    def get(self) -> OperationPipelineConfig:
        with self._lock:
            if self._config is None:
                self._config = (
                    validate_pipeline_config(json.loads(self.path.read_text(encoding="utf-8")))
                    if self.path.exists() else load_pipeline_config()
                )
            return self._config

    def save(self, data: dict) -> OperationPipelineConfig:
        config = validate_pipeline_config(data)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            name = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=self.path.parent,
                    prefix=".operation_pipeline_", suffix=".tmp", delete=False,
                ) as handle:
                    name = handle.name
                    json.dump(asdict(config), handle, ensure_ascii=False, indent=2)
                os.replace(name, self.path)
                self._config = config
            finally:
                if name is not None:
                    Path(name).unlink(missing_ok=True)
        return config


_SETTINGS = PipelineConfigStore(RUNS_DIR / "settings" / "operation_pipeline.json")


def current_pipeline_config() -> OperationPipelineConfig:
    return _SETTINGS.get()


def save_pipeline_config(data: dict) -> OperationPipelineConfig:
    return _SETTINGS.save(data)
