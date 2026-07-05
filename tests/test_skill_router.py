from pathlib import Path

from auto_eval.config import load_config
from auto_eval.judges.skill_router import SkillRouter


EXPECTED = {
    "automotive": "汽车",
    "chinese": "语文",
    "default": "通用",
    "digital_3c": "电子数码",
    "document": "文档",
    "film_tv": "影视",
    "lbs_travel": "LBS",
    "math_solving": "数学解题",
    "meta_service": "元服务",
    "music": "音乐",
    "news": "新闻",
    "operation": "操作类",
    "phone_tips": "玩机技巧",
    "professional_tech": "专业技术",
    "search": "搜索",
    "sports": "体育",
    "weather": "天气",
}


def _router():
    config_dir = Path(__file__).resolve().parents[1] / "config"
    return SkillRouter(load_config(config_dir).domain_skills)


def test_domain_skill_set_matches_expected():
    router = _router()
    assert {name: skill.display for name, skill in router.domain.items()} == EXPECTED


def test_every_domain_has_weighted_rubrics_and_subdimensions():
    router = _router()
    for name, skill in router.domain.items():
        assert skill.rubrics, name
        assert all(dim.weight > 0 for dim in skill.rubrics)
        # default 和 operation 使用一级直接评分维度，其余垂域要求配置二级维度。
        if name not in {"default", "operation"}:
            assert all(dim.sub_dimensions for dim in skill.rubrics), name


def test_display_name_uses_skill_config():
    assert _router().display_of("digital_3c") == "电子数码"
