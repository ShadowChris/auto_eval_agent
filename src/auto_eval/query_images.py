"""任务类附件图片地址的共享读取规则；不在导入时访问网络。"""
from __future__ import annotations

import math


ATTACHMENT_URL_FIELDS = ("attachment_url_domain", "attachment_url_ip")


def attachment_image_fields(obj: dict) -> dict[str, str]:
    """兼容顶层、metadata 和已解析 source_data；同名字段以外层为准。"""
    source = obj.get("source_data")
    source = source if isinstance(source, dict) else {}
    containers = (obj, obj.get("metadata"), source, source.get("metadata"))
    result: dict[str, str] = {}
    for field in ATTACHMENT_URL_FIELDS:
        for container in containers:
            if not isinstance(container, dict) or field not in container:
                continue
            value = container[field]
            if value is None or (isinstance(value, float) and not math.isfinite(value)):
                value = ""
            if not isinstance(value, str):
                raise ValueError(f"{field} 必须是 HTTP/HTTPS 图片链接字符串或空值")
            result[field] = value.strip()
            break
    return result


def attachment_image_urls(obj: dict) -> list[str]:
    """同一张图片的域名/IP 备选地址，域名优先、空值跳过、去重。"""
    fields = attachment_image_fields(obj)
    return list(dict.fromkeys(value for value in fields.values() if value))
