"""工具层抽象：统一基类、结果封装与零依赖 schema 校验。

设计目标
--------
1. 每个工具是一个「声明式对象」：显式声明 name / description / stage /
   input_schema / output_schema，而不是裸函数。
2. 统一入口 ``invoke(ctx, **kwargs) -> ToolOutput``，
   执行前按 input_schema 校验参数（对应"保证工具参数不出错"中的格式校验）。
3. 保留 ``__call__`` 兼容旧写法 ``TOOL_REGISTRY[name](ctx, **args) -> dict``，
   使既有调用点无需一次性改写。
4. schema 校验**自研轻量实现**，不引入 jsonschema 等第三方依赖。
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar

__all__ = [
    "ToolOutput",
    "ToolArgError",
    "BaseTool",
    "validate_against_schema",
    "tool_json_schemas",
]


class ToolArgError(ValueError):
    """工具参数不符合 input_schema。

    这是"参数格式错误"类异常的载体，会被工具层转换为用户可理解的字段级提示。
    """

    def __init__(self, tool_name: str, errors: list[str]):
        self.tool_name = tool_name
        self.errors = errors
        super().__init__(f"工具 {tool_name} 参数不合法: " + "；".join(errors))


@dataclass
class ToolOutput:
    """工具执行的统一结果封装。

    ``to_dict()`` 保持与历史返回结构兼容：``{"ok": bool, **data}``，
    这样既有读取 ``out["verification"]`` / ``out.get("partial_scopes")``
    的代码无需改动。
    """

    ok: bool
    tool_name: str
    data: dict[str, Any] = field(default_factory=dict)
    stage: str = ""
    duration_s: float = 0.0
    error: str | None = None
    error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": self.ok}
        payload.update(self.data)
        return payload

    def __getitem__(self, key: str) -> Any:
        # 兼容 dict 式读取：out["verification"]
        if key == "ok":
            return self.ok
        return self.data[key]

    def get(self, key: str, default: Any = None) -> Any:
        if key == "ok":
            return self.ok
        return self.data.get(key, default)


# --------------------------------------------------------------------------
# 零依赖 JSON Schema 校验（仅支持本项目所需的核心关键字）
# --------------------------------------------------------------------------
_TYPE_CHECKS: dict[str, Any] = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    return type(value).__name__


def validate_against_schema(value: dict[str, Any], schema: dict[str, Any], path: str = "") -> list[str]:
    """轻量 JSON Schema 校验，返回错误列表（空列表表示通过）。

    支持：type / required / properties / additionalProperties / enum /
          minimum / maximum / minLength。未支持的关键字直接忽略（宽松），
          保证不会因 schema 写得过细而误拦合法参数。
    """
    errors: list[str] = []
    if not isinstance(schema, dict):
        return errors

    expected = schema.get("type")
    if expected:
        checker = _TYPE_CHECKS.get(expected)
        if checker and not checker(value):
            return [f"{path or '参数'}: 类型应为 {expected}，实际为 {_type_name(value)}"]

    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path or '参数'}: 取值必须是 {schema['enum']} 之一，实际为 {value!r}")

    if expected == "string" and isinstance(value, str):
        min_len = schema.get("minLength")
        if min_len is not None and len(value) < min_len:
            errors.append(f"{path or '参数'}: 长度不能小于 {min_len}")

    if expected in ("number", "integer") and isinstance(value, (int, float)) and not isinstance(value, bool):
        lo, hi = schema.get("minimum"), schema.get("maximum")
        if lo is not None and value < lo:
            errors.append(f"{path or '参数'}: 不能小于 {lo}，实际为 {value}")
        if hi is not None and value > hi:
            errors.append(f"{path or '参数'}: 不能大于 {hi}，实际为 {value}")

    if expected == "object" and isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"缺少必填参数 `{key}`")
        props = schema.get("properties", {})
        for key, sub_schema in props.items():
            if key in value:
                errors.extend(validate_against_schema(value[key], sub_schema, path=key))
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in props:
                    errors.append(f"出现未声明的参数 `{key}`（允许: {sorted(props)}）")

    if expected == "array" and isinstance(value, list):
        item_schema = schema.get("items")
        if item_schema:
            for i, item in enumerate(value):
                errors.extend(validate_against_schema(item, item_schema, path=f"{path}[{i}]"))

    return errors


# --------------------------------------------------------------------------
# 工具基类
# --------------------------------------------------------------------------
class BaseTool(ABC):
    """所有潮流工具的统一基类。

    子类必须声明 ``name`` / ``description`` / ``stage`` / ``input_schema`` /
    ``output_schema``，并实现 ``run()``。

    执行链：``invoke()`` → 参数校验 → ``run()`` → ``ToolOutput``。
    ``__call__`` 仅为向后兼容保留，内部走同一条链，不产生第二套语义。
    """

    name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    stage: ClassVar[str] = ""
    input_schema: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}, "required": []}
    output_schema: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}}

    # ---------------- 子类实现 ----------------
    @abstractmethod
    def run(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        """执行工具逻辑，返回 data dict（不含 ok 字段，由 ToolOutput 统一包装）。"""
        raise NotImplementedError

    # ---------------- 统一入口 ----------------
    def validate_args(self, kwargs: dict[str, Any]) -> None:
        errors = validate_against_schema(kwargs, self.input_schema)
        if errors:
            raise ToolArgError(self.name, errors)

    def invoke(self, ctx: Any, **kwargs: Any) -> ToolOutput:
        """规范入口：校验参数 → 执行 → 封装结果。"""
        self.validate_args(kwargs)
        started = time.perf_counter()
        data = self.run(ctx, **kwargs)
        return ToolOutput(
            ok=True,
            tool_name=self.name,
            data=data or {},
            stage=self.stage,
            duration_s=round(time.perf_counter() - started, 4),
        )

    # ---------------- 向后兼容 ----------------
    def __call__(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        """兼容旧调用 ``TOOL_REGISTRY[name](ctx, **args) -> dict``。"""
        return self.invoke(ctx, **kwargs).to_dict()

    # ---------------- 内省 ----------------
    def spec(self) -> dict[str, Any]:
        """对外暴露的工具契约（可用于生成 Prompt 中的工具描述）。"""
        return {
            "name": self.name,
            "description": self.description,
            "stage": self.stage,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
        }

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"<Tool {self.name} stage={self.stage}>"


def tool_json_schemas(registry: dict[str, BaseTool]) -> list[dict[str, Any]]:
    """把注册表导出为 OpenAI function-calling 风格的工具声明列表。"""
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.input_schema,
            },
        }
        for t in registry.values()
    ]
