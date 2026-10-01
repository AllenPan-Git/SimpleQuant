"""
策略模板注册表：每个模板声明参数说明，界面据此自动生成参数表单和参数优化范围。
label / description / help 为 {"zh": ..., "en": ...}。
"""

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class Param:
    name: str
    label: dict
    default: float | int
    min: float | int | None = None
    max: float | int | None = None
    step: float | int | None = None
    help: dict | None = None

    @property
    def is_int(self) -> bool:
        return isinstance(self.default, int) and not isinstance(self.default, bool)


@dataclass
class Template:
    key: str
    label: dict
    description: dict
    cls: type
    params: list[Param] = field(default_factory=list)
    min_assets: int = 1      # 至少需要几个标的
    # 参数组合是否有意义（如短均线必须短于长均线），参数优化时用来跳过无效组合
    constraint: Callable[[dict], bool] | None = None

    def defaults(self) -> dict:
        return {p.name: p.default for p in self.params}


TEMPLATES: dict[str, Template] = {}


def register(template: Template) -> Template:
    TEMPLATES[template.key] = template
    return template
