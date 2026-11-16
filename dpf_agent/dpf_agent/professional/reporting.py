"""Pure, offline projections of a committed professional result.

Copyright (c) yangshaopeng. Rendering never evaluates rules or electrical data.
"""
from __future__ import annotations

from collections.abc import Mapping
from collections import Counter
import csv
import html
import io
import json
from typing import Any


REPORT_SCHEMA_VERSION = "1.1"
_HTML_ROW_LIMIT = 300
_HTML_DETAIL_LIMIT = 16000
_HTML_RESULT_LIMIT = 80000
_CHECK_FIELDS = (
    "task_id", "check_id", "object_id", "phase", "terminal", "metric", "unit",
    "measured", "lower", "upper", "status", "reason", "criteria_source",
    "measurement_basis", "node", "conductor", "kind", "rating", "rating_unit",
    "loading_percent", "evidence_ref", "evidence_refs", "criteria_id",
    "criteria_version", "criteria_digest", "criteria_confirmed_by",
    "criteria_confirmed_at", "scope", "attempt_id", "item_json",
)
_METRIC_FIELDS = (
    "task_id", "attempt_id", "request_id", "metric", "object_id", "terminal",
    "conductor", "node", "phase", "value", "unit", "measurement_basis",
    "status", "reason", "rating", "rating_unit", "loading_percent",
    "evidence_ref", "evidence_refs", "engineering_version", "scope",
    "numerical_validity", "input_fingerprint", "metric_json",
)
_STATUS_LABELS = {
    "created": "已创建", "queued": "排队中", "running": "执行中",
    "cancelling": "正在取消", "finished": "执行结束", "failed": "执行失败",
    "cancelled": "已取消", "interrupted": "已中断", "not_run": "未计算",
    "converged_valid": "数值验证通过", "non_converged": "未收敛",
    "validation_failed": "数值验证未通过", "not_assessed": "未评估",
    "partially_assessed": "部分评估", "violations_found": "发现越限",
    "evaluated_rules_passed": "已评估规则通过", "pass": "通过",
    "violation": "越限", "not_applicable": "不适用",
}


def _mapping(value: Any) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _rows(value: Any) -> tuple[Mapping, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(row for row in value if isinstance(row, Mapping))


def _json(value: Any, *, pretty: bool = False) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      indent=2 if pretty else None, separators=None if pretty else (",", ":"))


def _text(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, (Mapping, list, tuple)):
        return _json(value)
    return str(value)


def _escape(value: Any) -> str:
    return html.escape(_text(value), quote=True)


def _csv_cell(value: Any) -> Any:
    """Keep numbers numeric; neutralize formulas only in supplied text cells."""
    if isinstance(value, (Mapping, list, tuple)):
        value = _json(value)
    if isinstance(value, str) and value.lstrip()[:1] in {"=", "+", "-", "@"}:
        return "'" + value
    return value


def safe_csv_cell(value: Any) -> Any:
    """Protect text in other same-source CSV projections without changing JSON."""
    return _csv_cell(value)


def _csv(fields: tuple[str, ...], rows: list[Mapping]) -> str:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\r\n")
    writer.writerow(fields)
    for row in rows:
        writer.writerow([_csv_cell(row.get(field)) for field in fields])
    return stream.getvalue()


def _status(value: Any) -> str:
    label = _STATUS_LABELS.get(value) if isinstance(value, str) else None
    return (label + "（" + str(value) + "）") if label else _text(value)


_METRIC_LABELS = {
    "voltage":"电压", "line_current":"线路导体电流", "transformer_current":"变压器端口导体电流",
    "transformer_kva":"变压器端口总视在功率", "active_loss":"被动网络有功损耗",
    "voltage_magnitude_v":"节点电压", "native_voltage_pu":"原生基值电压标幺值",
    "native_minimum_voltage_pu":"最低原生电压标幺值", "native_maximum_voltage_pu":"最高原生电压标幺值",
    "terminal_conductor_current_a":"端口导体电流", "transformer_terminal_kva":"变压器端口总视在功率",
    "network_active_loss_kw":"网络有功损耗", "network_net_reactive_absorption_kvar":"网络净无功吸收（含补偿）",
    "line_transformer_active_loss_kw":"线路及变压器有功损耗",
    "line_transformer_net_reactive_absorption_kvar":"线路及变压器净无功吸收",
    "fixed_compensation_active_loss_kw":"固定补偿有功损耗",
    "fixed_compensation_net_reactive_absorption_kvar":"固定补偿净无功吸收",
    "project_voltage_pu":"项目确认基值电压标幺值",
    "line_current_loading_percent":"线路导体负载率", "transformer_current_loading_percent":"变压器端口导体负载率",
    "transformer_kva_loading_percent":"变压器端口等值容量负载率",
    "maximum_line_current_loading_percent":"已评估线路最大导体负载率",
    "maximum_transformer_current_loading_percent":"已评估变压器最大导体负载率",
    "maximum_transformer_kva_loading_percent":"已评估变压器最大端口等值负载率",
    "element_active_loss_kw":"设备有功损耗", "element_net_reactive_absorption_kvar":"设备净无功吸收",
    "element_class_net_p_kw":"同类设备净有功流入", "element_class_net_q_kvar":"同类设备净无功流入",
}
_BASIS_LABELS = {"node_to_ground":"节点对地", "node_to_node":"指定节点对",
    "node_to_ground_native_ln_base":"节点对地／原生明确基值", "terminal_conductor_rms":"端口导体有效值",
    "terminal_complex_power_magnitude":"端口复功率合计的模", "passive_network_active_loss":"线路、变压器及固定补偿边界",
    "passive_network_net_reactive_exchange":"被动网络净无功交换", "sum_signed_terminal_power_into_element":"全部端口有符号功率合计",
    "sum_signed_terminal_power_into_elements":"指定设备组有符号功率合计", "signed_power_into_elements":"进入设备为正"}
_REASON_LABELS = {"within_confirmed_limits":"项目限值范围内", "outside_confirmed_limits":"超出项目限值",
    "confirmed_voltage_base_missing":"缺少项目确认电压基值", "confirmed_equipment_rating_missing":"缺少项目确认设备额定值",
    "confirmed_criteria_missing_or_check_not_evaluated":"缺少对应判据或尚未完成校核",
    "confirmed_criteria_missing":"缺少已确认判据", "verified_device_disabled":"已核实设备停用",
    "derived_from_verified_native_observations":"来自已验证原生观测", "matching_measurement_missing":"缺少匹配观测",
    "matching_node_missing_or_ambiguous":"节点缺失或身份不唯一", "matching_terminal_conductor_missing_or_ambiguous":"端口导体缺失或身份不唯一"}


def _location(row: Mapping) -> str:
    parts = []
    if row.get("node") is not None:
        node = "节点" + str(row["node"])
        if row.get("reference_node") is not None:
            node += " → " + ("地" if row["reference_node"] == 0 else "节点" + str(row["reference_node"]))
        parts.append(node)
    if row.get("terminal") is not None: parts.append("端口" + str(row["terminal"]))
    if row.get("conductor") is not None: parts.append("导体" + str(row["conductor"]))
    if row.get("phase") is not None: parts.append("标签" + str(row["phase"]))
    return "／".join(parts) or "—"


def _basis(row: Mapping) -> str:
    basis = row.get("measurement_basis")
    return _text(row.get("unit")) + "；" + _BASIS_LABELS.get(basis, _text(basis))


def _number_display(value: Any) -> str:
    return format(value, ".8g") if isinstance(value, (int, float)) and not isinstance(value, bool) else _text(value)


def _evidence_label(row: Mapping) -> str:
    refs = row.get("evidence_refs") or ([row["evidence_ref"]] if row.get("evidence_ref") else [])
    return str(len(refs)) + "条原始证据；见完整合同" if isinstance(refs, (list, tuple)) and refs else "无记录"


def _table(headers: tuple[str, ...], rows: list[tuple[Any, ...]]) -> str:
    if not rows:
        return "<p class='muted'>无记录；没有记录不能作为校核通过的依据。</p>"
    return ("<div class='table-wrap'><table><thead><tr>"
            + "".join("<th>" + _escape(value) + "</th>" for value in headers)
            + "</tr></thead><tbody>"
            + "".join("<tr>" + "".join(("<td class='numeric'>" if isinstance(value, (int, float)) and not isinstance(value, bool) else "<td>")
                      + _escape(_number_display(value)) + "</td>" for value in row)
                      + "</tr>" for row in rows)
            + "</tbody></table></div>")


def _bounded_json(value: Any, limit: int) -> str | None:
    """Avoid materializing a second multi-megabyte runtime proof in HTML."""
    chunks = []
    size = 0
    encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2)
    for chunk in encoder.iterencode(value):
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return "".join(chunks)


def _structure_preview(value: Any, depth: int = 0) -> Any:
    """Marked structural preview, never a replacement for complete evidence."""
    if isinstance(value, Mapping):
        if depth >= 3:
            return {"HTML预览说明": "对象详情未内嵌；见完整结果", "字段数": len(value)}
        pairs = list(value.items())
        preview = {str(key): _structure_preview(item, depth + 1) for key, item in pairs[:24]}
        if len(pairs) > 24:
            preview["HTML预览未展示字段数"] = len(pairs) - 24
        return preview
    if isinstance(value, (list, tuple)):
        return {"HTML预览说明": "列表预览；未展示项仍保留在完整结果", "记录总数": len(value),
                "展示记录数": min(len(value), 3), "未展示记录数": max(0, len(value) - 3),
                "展示记录": [_structure_preview(item, depth + 1) for item in value[:3]]}
    if isinstance(value, str) and len(value) > 1000:
        return {"HTML预览说明": "长文本未内嵌；见完整结果", "字符数": len(value)}
    return value


def _details(title: str, value: Any, *, limit: int = _HTML_DETAIL_LIMIT) -> str:
    text = _bounded_json(value, limit)
    notice = ""
    if text is None:
        notice = ("<p class='notice'>本节完整证据较大，以下仅展示有明确省略标记的结构预览；"
                  "预览不能代替完整证据。请从“完整产物”查看 result.json；"
                  "若本次未请求 JSON，需另行请求该产物。</p>")
        text = _bounded_json(_structure_preview(value), limit)
        if text is None:
            text = _json({"HTML预览说明": "结构预览仍超过显示预算，详情全部保留在完整结果；此处不据此作通过判断。"}, pretty=True)
    return ("<details><summary>" + _escape(title) + "</summary>" + notice + "<pre>"
            + html.escape(text, quote=True) + "</pre></details>")


def _count_rows(rows: tuple[Mapping, ...]) -> Counter:
    return Counter(_text(row.get("status")) for row in rows)


def _row_preview(rows: tuple[Mapping, ...], *, metrics: bool = False) -> tuple[Mapping, ...]:
    def priority(row):
        status = _text(row.get("status"))
        if status == "violation":
            return 0
        if status == "not_assessed":
            return 1
        if status not in {"pass", "available", "not_applicable"}:
            return 2
        if metrics and row.get("object_id") in (None, "system"):
            return 3
        return 4
    return tuple(sorted(rows, key=priority)[:_HTML_ROW_LIMIT])


def _preview_note(rows: tuple[Mapping, ...], shown: tuple[Mapping, ...], filename: str) -> str:
    text = ("明细共 " + str(len(rows)) + " 条，HTML 展示 " + str(len(shown)) + " 条；未展示 "
            + str(len(rows) - len(shown)) + " 条。违规、未评估和异常状态优先展示；同组保持原始顺序。"
            "这是显示范围，不是重新评估。完整记录见“完整产物”中的 " + filename + " / result.json。")
    if len(shown) < len(rows):
        counts, displayed = _count_rows(rows), _count_rows(shown)
        text += " 未展示的状态计数：" + "；".join(
            _status(status) + "=" + str(count - displayed[status])
            for status, count in sorted(counts.items()) if count > displayed[status]) + "。"
    return "<p class='notice'>" + _escape(text) + "</p>"


def _artifact_links(result: Mapping) -> str:
    formats = _mapping(result.get("request")).get("output_formats")
    formats = formats if isinstance(formats, (list, tuple)) else ()
    names = [("result.json", "权威完整结果及数值验证证据"), ("checks.csv", "完整项目校核明细")]
    if "engineering_analysis" in result:
        names.append(("engineering.csv", "完整工程量明细"))
    if "professional_observations" in result:
        names.extend((("observations.json", "完整原生观测合同"), ("observations.csv", "完整原生观测表")))
    entries = []
    for name, label in names:
        # Both the target and label are application literals, never supplied paths.
        if name.rsplit(".", 1)[1] in formats:
            entries.append("<li><a href='" + name + "' download>" + name + "</a> — " + label + "</li>")
        else:
            entries.append("<li>" + name + " — " + label + "；本次请求未选择该格式或未记录格式，未提供入口。</li>")
    return ("<h2>完整产物</h2><p>HTML 是便于阅读的显示投影；大规模明细与运行证明保留在完整 JSON/CSV，"
            "不在本页重复内嵌。产物应与本报告一同保存；下列入口使用同目录固定文件名。"
            "如需完整可追溯交付，请同时请求 HTML、JSON 和 CSV。</p><ul>" + "".join(entries) + "</ul>")


def _metric_summary(metrics: tuple[Mapping, ...]) -> str:
    groups = {}
    for metric in metrics:
        key = tuple(_text(metric.get(name)) for name in ("metric", "unit", "measurement_basis", "status"))
        group = groups.setdefault(key, {"count": 0, "numbers": []})
        group["count"] += 1
        value = metric.get("value")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            group["numbers"].append((value, metric))
    rows = []
    for (metric, unit, basis, status), group in list(groups.items())[:100]:
        numbers = group["numbers"]
        low = min(numbers, key=lambda row: row[0]) if numbers else None
        high = max(numbers, key=lambda row: row[0]) if numbers else None
        rows.append((_METRIC_LABELS.get(metric, metric), unit, _BASIS_LABELS.get(basis, basis),
                     _status(status), group["count"], len(numbers),
                     low[0] if low else None,
                     _text(low[1].get("object_id")) + "／" + _location(low[1]) if low else None,
                     high[0] if high else None,
                     _text(high[1].get("object_id")) + "／" + _location(high[1]) if high else None))
    return ("<h3>工程量分类摘要</h3><p class='muted'>仅对同物理量、单位、基准和状态的记录显示最小/最大值，"
            "不新增校核或工程合格结论。分类共 " + str(len(groups)) + " 组，展示 " + str(len(rows))
            + " 组；其余分类及全部记录见 engineering.csv / result.json。</p>"
            + _table(("物理量", "单位", "基准", "状态", "记录数", "数值记录数", "最小值", "最小值位置", "最大值", "最大值位置"), rows))


def _check_summary(items: tuple[Mapping, ...]) -> str:
    counts = _count_rows(items)
    groups = Counter((_text(item.get("status")), _text(item.get("reason")))
                     for item in items if item.get("status") != "pass")
    reasons = [(_status(status), _REASON_LABELS.get(reason, reason), count)
               for (status, reason), count in list(groups.items())[:100]]
    return ("<h3>按实际明细记录统计</h3>"
            + _table(("状态", "记录数"), [(_status(status), count) for status, count in sorted(counts.items())])
            + "<h3>越限、未评估及其他非通过原因</h3><p class='muted'>原因共 " + str(len(groups))
            + " 组，展示 " + str(len(reasons)) + " 组；完整原因及对象见 checks.csv / result.json。"
            "规则记录数不等于已覆盖设备数；没有规则不能视作合格。</p>"
            + _table(("状态", "已记录原因", "记录数"), reasons))


def render_assessment_outputs(result: Mapping[str, Any]) -> dict[str, str]:
    """Return HTML/check CSV and, when present, engineering-metric CSV.

    The caller owns output-format selection and atomic persistence. No files,
    environment, clock, solver, evaluator, or network are consulted. Historical
    results retain their own assessment and version; absent P3 data is shown as
    absent rather than regenerated using newer rules.
    """
    if not isinstance(result, Mapping):
        raise TypeError("result must be a mapping")
    # Validate all supplied content up front, including non-rendered fields.
    # NaN/Infinity must not turn a malformed result into a plausible report.
    # Stream across the entire result, so a nonfinite value hidden beyond an
    # HTML preview limit is still rejected without allocating another full copy.
    for _ in json.JSONEncoder(ensure_ascii=False, allow_nan=False, sort_keys=True).iterencode(result):
        pass
    request = _mapping(result.get("request"))
    criteria = _mapping(request.get("criteria"))
    inputs = _mapping(result.get("input"))
    assessment = _mapping(result.get("assessment"))
    items = _rows(assessment.get("items"))
    shown_items = _row_preview(items)
    engineering = _mapping(result.get("engineering_analysis"))
    metrics = _rows(engineering.get("metrics"))
    shown_metrics = _row_preview(metrics, metrics=True)
    observations = _mapping(result.get("professional_observations"))

    check_rows = []
    for item in items:
        row = dict(item)
        row.update(task_id=result.get("task_id"), attempt_id=result.get("attempt_id"),
                   criteria_id=assessment.get("criteria_id"),
                   criteria_version=assessment.get("criteria_version"),
                   criteria_digest=assessment.get("criteria_digest"),
                   criteria_confirmed_by=criteria.get("confirmed_by"),
                   criteria_confirmed_at=criteria.get("confirmed_at"),
                   scope=assessment.get("scope"), item_json=item)
        if row.get("criteria_source") is None:
            row["criteria_source"] = criteria.get("source")
        check_rows.append(row)
    output = {"checks.csv": _csv(_CHECK_FIELDS, check_rows)}
    if "engineering_analysis" in result:
        metric_rows = []
        for metric in metrics:
            row = dict(metric)
            row.update(task_id=result.get("task_id"), attempt_id=result.get("attempt_id"),
                       request_id=result.get("request_id"), engineering_version=engineering.get("schema_version", engineering.get("version")),
                       scope=engineering.get("scope"), numerical_validity=engineering.get("numerical_validity"),
                       input_fingerprint=inputs.get("fingerprint"), metric_json=metric)
            metric_rows.append(row)
        output["engineering.csv"] = _csv(_METRIC_FIELDS, metric_rows)

    sections = [
        "<h1>天智五号工程校核报告</h1>",
        "<p class='notice'>本报告按权威结果中记录的范围展示数值与项目判据校核。"
        "未评估不表示合格；已评估规则通过不代表全网、工程适用性或工程验收通过。"
        "确认记录是调用者声明，不构成签名认证。</p>",
        "<h2>任务状态</h2>",
        _table(("状态维度", "已记录结论"), [
            ("执行状态", _status(result.get("execution_status"))),
            ("数值有效性", _status(result.get("numerical_validity"))),
            ("项目判据校核", _status(assessment.get("status"))),
            ("整体工程合规", _status(assessment.get("overall_compliance"))),
        ]),
        _artifact_links(result),
        "<h2>任务身份与版本</h2>",
        _table(("字段", "值"), [
            ("任务ID", result.get("task_id")), ("尝试ID", result.get("attempt_id")),
            ("请求ID", result.get("request_id")), ("请求摘要", result.get("request_digest")),
            ("结果合同版本", result.get("schema_version")),
            ("专业核心版本", result.get("professional_version")),
            ("工程分析版本", engineering.get("schema_version", engineering.get("version"))),
            ("报告投影版本", REPORT_SCHEMA_VERSION),
            ("输入指纹", inputs.get("fingerprint")),
            ("原始模型指纹", inputs.get("imported_model_fingerprint")),
            ("输入入口", inputs.get("master")),
            ("算例/快照", inputs.get("snapshot_id") or inputs.get("case_id")),
            ("工况", request.get("scenario")),
            ("求解参数", request.get("solver")),
            ("请求结果域", request.get("result_scopes")),
        ]),
        "<h2>项目判据与确认记录</h2>",
        _table(("字段", "值"), [
            ("判据ID", assessment.get("criteria_id")),
            ("判据版本", assessment.get("criteria_version")),
            ("判据摘要", assessment.get("criteria_digest")),
            ("判据来源", criteria.get("source")),
            ("确认人（调用者声明）", criteria.get("confirmed_by")),
            ("确认日期（调用者声明）", criteria.get("confirmed_at")),
            ("已评估范围", assessment.get("scope")),
            ("合同记录的判据校核计数", assessment.get("counts")),
        ]),
        _details("工况声明与核对", {"declared": request.get("operating_conditions"),
                                      "verification": result.get("operating_conditions_verification")}),
        "<h2>校核明细与未评估项</h2>",
        _check_summary(items),
        _preview_note(items, shown_items, "checks.csv"),
        "<p class='muted'>表中数值按8位有效数字显示；完整精度、额定值、负载率、来源和证据保留在完整合同及JSON/CSV中。</p>",
        _table(("规则/对象", "节点/端口/导体", "物理量与基准", "实测", "下限", "上限", "结论", "原因/依据"), [
            (_text(item.get("check_id")) + "／" + _text(item.get("object_id")),
             _location(item),
             _METRIC_LABELS.get(item.get("metric"), _text(item.get("metric"))) + "；" + _basis(item),
             item.get("measured"), item.get("lower"), item.get("upper"), _status(item.get("status")),
             _REASON_LABELS.get(item.get("reason"), _text(item.get("reason"))) + "；" + _evidence_label(item))
            for item in shown_items
        ]),
        _details("校核合同预览（完整额定量、负载率及扩展字段见产物）", assessment),
        "<h2>工程量与网损</h2>",
    ]
    if "engineering_analysis" not in result:
        sections.append("<p>该历史结果未记录工程分析合同；本报告没有补算或重新评估。</p>")
    else:
        sections.extend([
            _metric_summary(metrics),
            _preview_note(metrics, shown_metrics, "engineering.csv"),
            _table(("物理量", "对象/端口", "数值", "单位/基准", "状态/原因", "证据"), [
                (_METRIC_LABELS.get(metric.get("metric"), _text(metric.get("metric"))), _text(metric.get("object_id")) + "／" + _location(metric),
                 metric.get("value"), _basis(metric),
                 _status(metric.get("status")) + "；" + _REASON_LABELS.get(metric.get("reason"), _text(metric.get("reason"))),
                 _evidence_label(metric)) for metric in shown_metrics
            ]),
            _details("工程量覆盖与限制", {key: engineering.get(key) for key in ("scope", "numerical_validity", "coverage", "limitations")}),
        ])
    sections.extend([
        "<h2>供电连通与孤岛诊断</h2>",
        _details("母线级供电连通（不替代分相供电及孤岛运行校核）", result.get("topology_diagnosis", {"status":"not_recorded"})),
        "<h2>数值证据、覆盖与限制</h2>",
        _table(("验证维度", "已记录状态", "passed", "publication_authorized"), [
            (label, _status(_mapping(result.get(key)).get("status")),
             _mapping(result.get(key)).get("passed"), _mapping(result.get(key)).get("publication_authorized"))
            for label, key in (("数值验证", "verification"), ("设备连接验证", "device_connections_verification"),
                               ("域观测验证", "domain_observations_verification"), ("工况核对", "operating_conditions_verification"))
        ]),
        _details("观测来源与覆盖", {"provenance": observations.get("provenance"),
                                     "coverage": observations.get("coverage"), "issues": observations.get("issues")}),
        _details("数值验证与设备/域检查", {key: result.get(key) for key in (
            "verification", "device_connections_verification", "domain_observations_verification")}),
        _details("限制与错误", {"limitations": result.get("limitations"), "error": result.get("error")}),
        _details("权威结构化结果原文（大结果仅预览，全文见 result.json）", result, limit=_HTML_RESULT_LIMIT),
    ])
    title = "天智五号工程校核报告 — " + _text(result.get("task_id"))
    output["report.html"] = (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'\">"
        "<title>" + html.escape(title, quote=True) + "</title><style>"
        "body{max-width:1180px;margin:30px auto;padding:0 20px;color:#182633;background:#fff;font:15px/1.7 system-ui,sans-serif}"
        "h1{font-size:27px}h2{margin-top:30px;border-bottom:1px solid #d7e0e8;padding-bottom:6px}"
        ".notice{padding:14px;background:#fff5db;border-left:4px solid #9f7416}.muted{color:#566675}"
        ".table-wrap{overflow-x:auto}table{width:100%;border-collapse:collapse;margin:12px 0}"
        "th,td{border:1px solid #d7e0e8;padding:8px;text-align:left;vertical-align:top;overflow-wrap:anywhere}"
        "th{background:#eef3f8}td.numeric{white-space:nowrap;font-variant-numeric:tabular-nums}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}"
        "details{margin:12px 0}summary{cursor:pointer;font-weight:600}"
        "@media print{body{max-width:none;margin:0;padding:0}details{break-inside:avoid}th{background:#eee}}"
        "</style></head><body>" + "".join(sections) + "</body></html>"
    )
    return output
