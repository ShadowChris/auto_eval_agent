"""Web 评估台的可切换 LLM Provider 配置与密钥存储。"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, Field, field_validator, model_validator

from ..config import AppConfig


PROVIDER_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
_STORE_LOCK = threading.RLock()


class LLMProviderPayload(BaseModel):
    """Provider 新增/编辑请求；api_key 留空表示编辑时保留原密钥。"""

    id: str
    name: str
    base_url: str
    models: list[str] = Field(default_factory=list)
    default_model: str = ""
    api_key: str | None = None
    enabled: bool = True

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        normalized = value.strip()
        if not PROVIDER_ID_RE.fullmatch(normalized):
            raise ValueError("仅支持 1-64 位字母、数字、下划线和连字符")
        return normalized

    @field_validator("name", "base_url")
    @classmethod
    def nonempty_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("不能为空")
        return normalized

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("必须以 http:// 或 https:// 开头")
        return value.rstrip("/")

    @field_validator("models")
    @classmethod
    def normalize_models(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        for value in values:
            normalized = str(value).strip()
            if normalized and normalized not in result:
                result.append(normalized)
        return result

    @model_validator(mode="after")
    def normalize_default_model(self):
        self.default_model = self.default_model.strip()
        if self.default_model and self.default_model not in self.models:
            self.models.append(self.default_model)
        if not self.default_model and self.models:
            self.default_model = self.models[0]
        return self


class ProviderResolution(BaseModel):
    id: str
    name: str
    base_url: str
    model: str
    api_key: str = Field(repr=False, exclude=True)
    revision: str
    builtin: bool = False


def _revision(data: dict[str, Any]) -> str:
    content = json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(content).hexdigest()[:12]


def _atomic_json_write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


class LLMProviderStore:
    """原子保存 Provider；API Key 使用 Fernet 加密且从不进入公开结果。"""

    def __init__(self, settings_dir: Path):
        self.settings_dir = Path(settings_dir)
        self.path = self.settings_dir / "llm_providers.json"
        self.key_path = self.settings_dir / ".llm_provider_key"

    def _fernet(self) -> Fernet:
        configured = os.getenv("AUTO_EVAL_CREDENTIAL_KEY", "").strip()
        if configured:
            try:
                raw = configured.encode("ascii")
                if len(base64.urlsafe_b64decode(raw)) == 32:
                    return Fernet(raw)
            except Exception:
                pass
            derived = base64.urlsafe_b64encode(
                hashlib.sha256(configured.encode("utf-8")).digest()
            )
            return Fernet(derived)

        with _STORE_LOCK:
            if self.key_path.is_file():
                return Fernet(self.key_path.read_bytes().strip())
            self.settings_dir.mkdir(parents=True, exist_ok=True)
            key = Fernet.generate_key()
            self.key_path.write_bytes(key)
            try:
                self.key_path.chmod(0o600)
            except OSError:
                pass
            return Fernet(key)

    def _load(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Provider 配置文件损坏：{self.path}") from exc
        if not isinstance(data, list):
            raise ValueError(f"Provider 配置必须是数组：{self.path}")
        return [dict(item) for item in data if isinstance(item, dict)]

    def _save(self, records: list[dict[str, Any]]) -> None:
        _atomic_json_write(self.path, records)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    @staticmethod
    def _public(record: dict[str, Any], *, builtin: bool = False) -> dict[str, Any]:
        public = {
            "id": record.get("id") or "",
            "name": record.get("name") or record.get("id") or "",
            "base_url": record.get("base_url") or "",
            "models": list(record.get("models") or []),
            "default_model": record.get("default_model") or "",
            "has_api_key": bool(record.get("has_api_key") or record.get("api_key_encrypted")),
            "enabled": bool(record.get("enabled", True)),
            "builtin": builtin,
            "created_at": record.get("created_at"),
            "updated_at": record.get("updated_at"),
        }
        public["revision"] = record.get("revision") or _revision({
            key: public[key]
            for key in ("id", "name", "base_url", "models", "default_model", "enabled")
        })
        return public

    @staticmethod
    def builtin_records(cfg: AppConfig) -> list[dict[str, Any]]:
        """按 base_url + 密钥配置聚合角色连接，避免把裁判误当 Provider。"""
        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        for judge in cfg.judges:
            if not judge.base_url:
                continue
            base_url = judge.base_url.rstrip("/")
            api_key_env = str(judge.api_key_env or "")
            group_key = (base_url, api_key_env)
            model = str(judge.model or judge.name)
            record = grouped.get(group_key)
            if record is None:
                digest = hashlib.sha256(
                    f"{base_url}|{api_key_env}".encode("utf-8")
                ).hexdigest()[:12]
                if api_key_env:
                    label = re.sub(r"_?API_KEY$", "", api_key_env, flags=re.I)
                    label = " ".join(
                        part for part in label.split("_") if part
                    ).title()
                else:
                    label = urlparse(base_url).hostname or "内置服务"
                record = {
                    "id": f"builtin-{digest}",
                    "name": f"{label}（配置默认）",
                    "base_url": base_url,
                    "models": [],
                    "default_model": model,
                    "has_api_key": False,
                    "enabled": True,
                    "api_key_env": api_key_env,
                    "aliases": [],
                    "_api_key": None,
                }
                grouped[group_key] = record
            if model not in record["models"]:
                record["models"].append(model)
            record["aliases"].append(f"builtin-{judge.name}")
            api_key = judge.api_key()
            if api_key and not record["_api_key"]:
                record["_api_key"] = api_key
            record["has_api_key"] = bool(record["_api_key"])

        records = list(grouped.values())
        for record in records:
            record["revision"] = _revision({
                key: record[key]
                for key in (
                    "id", "name", "base_url", "models", "default_model",
                    "enabled", "api_key_env",
                )
            })
        return records

    def list_public(self, cfg: AppConfig) -> list[dict[str, Any]]:
        with _STORE_LOCK:
            custom = [self._public(record) for record in self._load()]
        builtins = [self._public(record, builtin=True) for record in self.builtin_records(cfg)]
        return builtins + custom

    def create(self, payload: LLMProviderPayload) -> dict[str, Any]:
        if payload.id.startswith("builtin-"):
            raise ValueError("builtin- 前缀保留给内置 Provider")
        with _STORE_LOCK:
            records = self._load()
            if any(record.get("id") == payload.id for record in records):
                raise ValueError(f"Provider 已存在：{payload.id}")
            now = time.time()
            record = payload.model_dump(exclude={"api_key"})
            record.update({"created_at": now, "updated_at": now})
            if payload.api_key:
                record["api_key_encrypted"] = self._fernet().encrypt(
                    payload.api_key.encode("utf-8")
                ).decode("ascii")
            record["revision"] = _revision(record)
            records.append(record)
            self._save(records)
            return self._public(record)

    def update(self, provider_id: str, payload: LLMProviderPayload) -> dict[str, Any]:
        if provider_id.startswith("builtin-"):
            raise ValueError("内置 Provider 不能编辑，请新建自定义 Provider")
        if payload.id != provider_id:
            raise ValueError("Provider id 创建后不能修改")
        with _STORE_LOCK:
            records = self._load()
            index = next(
                (index for index, record in enumerate(records) if record.get("id") == provider_id),
                None,
            )
            if index is None:
                raise KeyError(provider_id)
            previous = records[index]
            record = payload.model_dump(exclude={"api_key"})
            record["created_at"] = previous.get("created_at") or time.time()
            record["updated_at"] = time.time()
            encrypted = previous.get("api_key_encrypted")
            if payload.api_key:
                encrypted = self._fernet().encrypt(payload.api_key.encode("utf-8")).decode("ascii")
            if encrypted:
                record["api_key_encrypted"] = encrypted
            record["revision"] = _revision(record)
            records[index] = record
            self._save(records)
            return self._public(record)

    def delete(self, provider_id: str) -> bool:
        if provider_id.startswith("builtin-"):
            raise ValueError("内置 Provider 不能删除")
        with _STORE_LOCK:
            records = self._load()
            remaining = [record for record in records if record.get("id") != provider_id]
            if len(remaining) == len(records):
                return False
            self._save(remaining)
            return True

    def resolve(
        self,
        provider_id: str,
        model: str,
        cfg: AppConfig,
        *,
        base_url_snapshot: str = "",
    ) -> ProviderResolution:
        builtin = next(
            (
                record for record in self.builtin_records(cfg)
                if record.get("id") == provider_id
                or provider_id in (record.get("aliases") or [])
            ),
            None,
        )
        if builtin is not None:
            api_key = builtin.get("_api_key")
            record = builtin
            is_builtin = True
        else:
            with _STORE_LOCK:
                record = next(
                    (item for item in self._load() if item.get("id") == provider_id),
                    None,
                )
            if record is None:
                raise KeyError(provider_id)
            encrypted = str(record.get("api_key_encrypted") or "")
            if encrypted:
                try:
                    api_key = self._fernet().decrypt(encrypted.encode("ascii")).decode("utf-8")
                except (InvalidToken, ValueError) as exc:
                    raise ValueError(f"Provider[{provider_id}] API Key 无法解密") from exc
            else:
                api_key = None
            is_builtin = False

        if not record.get("enabled", True):
            raise ValueError(f"Provider[{provider_id}] 已停用")
        selected_model = model.strip() or str(record.get("default_model") or "").strip()
        if not selected_model:
            raise ValueError(f"Provider[{provider_id}] 未指定模型")
        if not api_key:
            raise ValueError(f"Provider[{provider_id}] 未配置 API Key")
        base_url = base_url_snapshot.strip() or str(record.get("base_url") or "").strip()
        if not base_url:
            raise ValueError(f"Provider[{provider_id}] 未配置 base_url")
        return ProviderResolution(
            id=provider_id,
            name=str(record.get("name") or provider_id),
            base_url=base_url.rstrip("/"),
            model=selected_model,
            api_key=api_key,
            revision=str(record.get("revision") or _revision(record)),
            builtin=is_builtin,
        )
