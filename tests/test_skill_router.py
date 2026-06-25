from pathlib import Path

from auto_eval.config import load_config
from auto_eval.judges.skill_router import SkillRouter


EXPECTED = {
    "math_solving": "数学解题",
    "music": "音乐",
    "film_tv": "影视",
    "sports": "体育",
    "news": "新闻",
    "lbs_travel": "LBS（旅行规划）",
    "automotive": "汽车",
    "digital_3c": "数码3C",
    "search": "搜索",
    "document": "文档",
    "default": "通用",
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
        if name != "default":
            assert all(dim.sub_dimensions for dim in skill.rubrics), name


def test_display_name_uses_skill_config():
    assert _router().display_of("digital_3c") == "数码3C"