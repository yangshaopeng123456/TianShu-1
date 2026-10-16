"""引擎中毒看门狗（方案 B）：判定后端已无力自愈时主动退出，交由 Electron 重启。

## 为什么需要它（实测证据）

引擎（OpenDSS COM）一旦挂死，**进程内无法真正恢复**：

* `_ComWorker._reset()` 能换掉工作线程与引擎实例，但换出来的"新引擎"
  未必是好的——9/15 Step 0 已实测：`OpenDSSEngine.dll` 在本机命令行起的
  进程里**加载即挂死**（同目录的 `KLUSolve.dll` 却 0.4s 秒加载）。
  一旦进入这个状态，后续每个任务都会各卡一个完整超时周期。
* 9/13 实测：连续 4 个任务各卡 120s，**整机瘫痪约 4 分钟**。

线程卡在原生调用里时，Python 层**无法强杀**——只有进程边界能清理资源。
因此本模块的策略是：判定进程已无力自愈时**主动退出**，
由 Electron 主进程接管（`main.js` 已有 `exit` 监听 + 自动重启逻辑）。

## 两条判据（互补，缺一不可）

1. **引擎复检（主）** —— 任务因引擎故障失败后，立刻复检一次引擎通道：
   用短超时问一句 `Version`。通道干净则秒回；通道中毒则超时。
   这条能**精准区分「算例本身就慢」与「引擎中毒」**，避免误杀正常的大算例。
2. **任务卡死（兜底）** —— 全进程串行锁被占用超过阈值仍未释放。
   应对"连超时机制都失效"的极端情况（主线程被原生库彻底卡住，
   Python 代码根本没有机会执行，只有本条能救）。

## 防风暴（必须，勿删）

重启解决不了的问题（例如某个算例本身就要跑 10 分钟以上）会在重启后
**立刻复发**，若不限制就会陷入「重启—超时—重启」死循环，比不重启更糟。
因此把每次重启的时间戳落盘，窗口内超过上限就**只记录、不退出**，
把问题如实暴露给用户，而不是用无限重启掩盖它。

## 设计原则（勿改错）

- **看门狗自身绝不能成为故障源**：所有入口全程 try 包裹，任何异常都只是
  少做一次检查，绝不允许影响主流程。
- **探针必须发生在引擎资源释放之前**：调用方（`service.run`）须在
  `finally` 释放 circuit 之前调用 `note_engine_failure()`。
- **退出前必须把日志 fsync 落盘**：`os._exit` 不跑 atexit / finally，
  缓冲区里的内容会直接丢失（`crashlog` 已做 fsync，这里依赖它）。
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

# ---------------------------------------------------------------- 可调参数
# 全部支持环境变量覆盖，便于现场调试与按机器调优；默认值按"桌面单用户"场景定。
_INTERVAL_S = float(os.environ.get("DPF_WATCHDOG_INTERVAL_S", "5"))
_PROBE_TIMEOUT_S = float(os.environ.get("DPF_WATCHDOG_PROBE_TIMEOUT_S", "15"))
# 复检外层线程的额外宽限：内层超时后线程可能仍要一小会儿才真正退出，
# 这里多等一点再判定"连复检线程都没回来"（比超时返回更严重）。
_PROBE_GRACE_S = float(os.environ.get("DPF_WATCHDOG_PROBE_GRACE_S", "5"))
_TASK_STUCK_S = float(os.environ.get("DPF_WATCHDOG_TASK_STUCK_S", "300"))
_EXIT_CODE = int(os.environ.get("DPF_WATCHDOG_EXIT_CODE", "70"))
_BREAKER_WINDOW_S = float(os.environ.get("DPF_WATCHDOG_BREAKER_WINDOW_S", "1800"))
_BREAKER_MAX = int(os.environ.get("DPF_WATCHDOG_BREAKER_MAX", "2"))
_ENABLED = os.environ.get("DPF_WATCHDOG_ENABLED", "1").strip().lower() not in ("0", "false", "off", "no")

_lock = threading.Lock()
_state: dict[str, Any] = {
    "enabled": _ENABLED,
    "started": False,
    "probes": 0,           # 引擎复检次数
    "probe_failures": 0,   # 复检判定"中毒"的次数
    "triggers": 0,         # 本进程内触发退出请求的次数（正常最多 1 次）
    "breaker_blocked": 0,  # 因熔断而放弃重启的次数
    "last_probe": "",
    "last_reason": "",
    "last_trigger_at": None,
}

_loop_thread: threading.Thread | None = None


# ------------------------------------------------------------------ 小工具
def _now() -> float:
    return time.time()


def _history_path() -> str:
    """重启时间戳落盘位置。放 outputs/_logs/，与崩溃日志同处（不污染源码树）。"""
    from ..config import PROJECT_ROOT

    return os.path.join(PROJECT_ROOT, "outputs", "_logs", "watchdog_restarts.json")


def _read_history() -> list[float]:
    try:
        with open(_history_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return [float(x) for x in (data.get("restart_at") or [])][-20:]
    except Exception:  # noqa: BLE001 - 历史不可读就当没有，宁可少一次熔断计数
        return []


def _append_history(stamp: float) -> None:
    """追加一次重启时间戳。写失败不影响退出决策（重启本身仍要执行）。"""
    try:
        stamps = _read_history()
        stamps.append(float(stamp))
        path = _history_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"restart_at": stamps[-20:]}, f, ensure_ascii=False, indent=2)
            f.flush()
            try:
                os.fsync(f.fileno())
            except (OSError, ValueError):
                pass
    except Exception:  # noqa: BLE001
        pass


def _breaker_open(now: float | None = None) -> tuple[bool, int]:
    """熔断是否已打开。返回 (是否打开, 窗口内已重启次数)。"""
    t = _now() if now is None else now
    recent = [x for x in _read_history() if t - x < _BREAKER_WINDOW_S]
    return len(recent) >= _BREAKER_MAX, len(recent)


def _log(reason: str, extra: dict[str, Any] | None = None) -> None:
    """把一次看门狗决策写进崩溃日志（kind=watchdog，便于事后检索）。"""
    try:
        from . import crashlog

        body = "看门狗决策：%s" % reason
        if extra:
            body += "\n" + "\n".join(f"{k}：{v}" for k, v in extra.items())
        crashlog.record_text(body, kind="watchdog")
    except Exception:  # noqa: BLE001 - 日志失败绝不阻断决策
        pass


# -------------------------------------------------------------- 对外接口
def install() -> bool:
    """启动看门狗后台线程。幂等，重复调用无副作用。返回是否处于启用状态。"""
    global _loop_thread

    if not _ENABLED:
        return False
    with _lock:
        if _state["started"]:
            return True
        _state["started"] = True

    try:
        _loop_thread = threading.Thread(target=_loop, name="engine-watchdog", daemon=True)
        _loop_thread.start()
    except Exception:  # noqa: BLE001 - 线程都起不来时静默降级，不能拖垮启动
        with _lock:
            _state["started"] = False
        return False
    _log_startup_banner()
    return True


def _log_startup_banner() -> None:
    """把启用信息打到标准输出，**不写崩溃日志**。

    ## 为什么不能写崩溃日志

    崩溃日志的语义是"出事了"。把"看门狗已启动"写进去会造成一个很实际的
    误判：后端每启动一次就多一条看起来像异常的记录，用户打开日志会以为
    反复出问题（实测已发生）。启用与否属于**正常状态**，
    看 `/api/diagnostics` 的 `watchdog.enabled/started` 即可，
    或看启动横幅，不该占用故障日志。

    真正需要落进崩溃日志的是**决策**：即将退出、或因熔断放弃重启。
    """
    try:
        print(
            "[watchdog] 已启用 interval=%ss probe=%ss task_stuck=%ss exit=%s "
            "breaker=%s/%smin pid=%s"
            % (_INTERVAL_S, _PROBE_TIMEOUT_S, _TASK_STUCK_S, _EXIT_CODE,
               _BREAKER_MAX, int(_BREAKER_WINDOW_S / 60), os.getpid())
        )
    except Exception:  # noqa: BLE001 - 打印失败无关紧要
        pass


def note_engine_failure(adapter: Any, detail: str) -> dict[str, Any]:
    """任务因引擎故障失败后调用：复检引擎通道，判定进程是否已中毒。

    **必须在释放引擎资源之前调用**（`service.run` 的 finally 之前），
    否则复检对象已经不可用，会得到假阳性。

    返回结论快照（同时写入内部状态，供 `/api/diagnostics` 观测）。
    本函数**不抛异常**：它跑在任务失败路径上，不能把"诊断"变成"新故障"。
    """
    result: dict[str, Any] = {"probed": False, "healthy": None, "action": "none", "detail": detail}
    if not _ENABLED:
        return result
    try:
        ok, probe_detail = _probe(adapter)
        result["probed"] = ok is not None
        result["healthy"] = ok
        if ok is None:
            # 复检条件都不具备（适配器缺失/接口不符）：对引擎状态一无所知。
            # 此时**不能重启**——那会把配置类错误变成无谓重启。
            result["action"] = "unavailable"
            result["detail"] = f"{detail}（{probe_detail}）"
            with _lock:
                _state["last_probe"] = f"无法判定（{probe_detail}）"
            return result
        with _lock:
            _state["probes"] = int(_state["probes"]) + 1
            _state["last_probe"] = (
                f"{'健康' if ok else '无响应'}（{probe_detail}）"
                if probe_detail
                else ("健康" if ok else "无响应")
            )
            if not ok:
                _state["probe_failures"] = int(_state["probe_failures"]) + 1
        if ok:
            # 通道是干净的 → 这次失败属于业务层面（算例本身问题），不能重启后端。
            result["action"] = "kept-running"
            return result
        # 通道也死了 → 进程内已无自愈可能。
        result["action"] = "restart"
        _trigger(
            "引擎复检失败：任务已超时，且引擎通道无响应",
            {"失败原因": detail, "复检结果": probe_detail or "超时未返回"},
        )
    except Exception as exc:  # noqa: BLE001 - 诊断自身绝不抛出
        result["action"] = "error"
        result["detail"] = f"{detail}（复检异常：{type(exc).__name__}: {exc}）"
    return result


def diagnostics() -> dict[str, Any]:
    """观测入口：供 `/api/diagnostics` 暴露看门狗状态。"""
    try:
        open_now, recent = _breaker_open()
    except Exception:  # noqa: BLE001
        open_now, recent = False, -1
    with _lock:
        out = dict(_state)
    out["breaker_open"] = open_now
    out["restarts_in_window"] = recent
    out["params"] = {
        "interval_s": _INTERVAL_S,
        "probe_timeout_s": _PROBE_TIMEOUT_S,
        "task_stuck_s": _TASK_STUCK_S,
        "exit_code": _EXIT_CODE,
        "breaker_max": _BREAKER_MAX,
        "breaker_window_s": _BREAKER_WINDOW_S,
    }
    return out


# ---------------------------------------------------------------- 内部实现
def _probe(adapter: Any) -> tuple[bool | None, str]:
    """带硬超时复检引擎通道。返回 (是否健康, 说明)。

    三态：`True` 健康 / `False` 无响应（判定中毒）/ `None` 无法判定。
    `None` 这一态很重要：适配器缺失或不具备复检接口时，我们对引擎状态
    **一无所知**——此时重启后端毫无依据，只会把配置类错误变成无谓重启。

    双层超时：
      * 内层用 `run_with_engine(..., timeout_s)`——COM 适配器会把它转成
        `_ComWorker.call` 的超时（公开接口，不依赖私有成员）；
      * 外层再套线程 + join——因为 dss-python 适配器的 `run_with_engine`
        会忽略 timeout 参数，单靠内层可能永久挂住。
    """
    if adapter is None:
        return None, "未提供适配器，无法复检"
    if not hasattr(adapter, "run_with_engine") and not hasattr(adapter, "probe_version"):
        return None, f"适配器缺少 run_with_engine（{type(adapter).__name__}）"

    box: dict[str, Any] = {}

    def _run() -> None:
        try:
            engine_version = adapter.probe_version(timeout_s=_PROBE_TIMEOUT_S) if hasattr(adapter, "probe_version") else adapter.run_with_engine(
                lambda eng: str(eng.Version), timeout_s=_PROBE_TIMEOUT_S
            )
            box["ok"] = True
            box["detail"] = f"引擎响应正常（{str(engine_version).strip()}）"
        except BaseException as exc:  # noqa: BLE001 - 原生库可能抛任意异常
            box["ok"] = False
            box["detail"] = f"{type(exc).__name__}: {exc}"

    t = threading.Thread(target=_run, name="engine-watchdog-probe", daemon=True)
    t.start()
    budget = _PROBE_TIMEOUT_S + _PROBE_GRACE_S
    t.join(budget)
    if t.is_alive():
        # 复检线程自己也没回来：比"超时返回"更严重，同样判定通道不可用。
        return False, f"复检超过 {budget:.1f} 秒未返回"
    if "ok" not in box:
        return False, "复检未产生结论"
    return bool(box["ok"]), str(box.get("detail") or "")


def _loop() -> None:  # pragma: no cover - 线程体
    while True:
        try:
            time.sleep(_INTERVAL_S)
            _check_once()
        except Exception:  # noqa: BLE001 - 单轮失败不影响后续轮次
            pass


def _check_once() -> None:
    """兜底判据：串行锁被长时间占用仍未释放。"""
    try:
        from ..application.service import current_task_lock

        info = current_task_lock()
    except Exception:  # noqa: BLE001
        return
    if not info.get("locked"):
        return
    since = info.get("since")
    if not since:
        return
    try:
        held = _now() - float(since)
    except (TypeError, ValueError):
        return
    if held < _TASK_STUCK_S:
        return
    _trigger(
        "任务长时间未结束：引擎调用连超时机制都未生效",
        {
            "task_id": info.get("task_id") or "未知",
            "已持续秒数": f"{held:.0f}",
            "阈值秒数": f"{int(_TASK_STUCK_S)}",
        },
    )


def _trigger(reason: str, extra: dict[str, Any]) -> None:
    """请求退出进程，交由 Electron 重启。受熔断保护。"""
    now = _now()
    with _lock:
        _state["triggers"] = int(_state["triggers"]) + 1
        _state["last_reason"] = reason
        _state["last_trigger_at"] = now

    try:
        open_now, recent = _breaker_open(now)
    except Exception:  # noqa: BLE001
        open_now, recent = False, 0

    if open_now:
        # 熔断：重启解决不了的问题，无限重启只会掩盖它。
        with _lock:
            _state["breaker_blocked"] = int(_state["breaker_blocked"]) + 1
        _log(
            "已放弃重启（熔断打开）",
            {**extra, "窗口内重启次数": recent},
        )
        return

    _append_history(now)
    _log(
        "即将退出进程以触发后端重启",
        {**extra, "退出码": _EXIT_CODE, "pid": os.getpid()},
    )

    # 用 os._exit 而非 sys.exit：不跑 finally / atexit，果断放弃已中毒的进程。
    # 日志已由 crashlog 逐条 fsync，不需要额外的清理窗口。
    os._exit(_EXIT_CODE)


def _reset_for_tests() -> None:
    """仅供测试：清空内部计数（不还原线程，避免测试间相互污染更难查）。"""
    with _lock:
        _state.update(
            {
                "started": False,
                "probes": 0,
                "probe_failures": 0,
                "triggers": 0,
                "breaker_blocked": 0,
                "last_probe": "",
                "last_reason": "",
                "last_trigger_at": None,
            }
        )
