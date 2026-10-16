# -*- coding: utf-8 -*-
"""方案 B 回归测试：引擎中毒看门狗 + 后端自动重启的判据与安全边界。

全程离线、不需要真引擎、不需要网络。用临时目录 + 打桩适配器做沙箱。

## 为什么这些断言值得写

看门狗是**唯一会主动终止后端进程**的组件，判错方向的代价不对称：

* 误杀（引擎其实健康却重启）→ 用户看到界面自己重启一下，损失十几秒；
* 漏杀（引擎已中毒却不重启）→ 后续每个任务各卡一个超时周期，
  实测 9/13 是**整机瘫痪 4 分钟**，用户只能手动关掉应用。

所以本套测试的重点不是"能不能重启"，而是**判据的边界**：
什么情况下必须重启、什么情况下**绝不能**重启、以及熔断有没有生效。

分组：
- A 组：复检判据（健康→继续服务 / 无响应→重启 / 连复检线程都不回来→重启）
- B 组：熔断（窗口内限流、窗口外不限制）
- C 组：任务卡死兜底判据（应对连超时机制都失效的情况）
- D 组：安全边界（永不抛出、不误判业务失败、可整体禁用）
- E 组：接线与静态契约（server 安装、service 上报、main.js 认退出码）
"""
from __future__ import annotations

import inspect
import json
import os
import shutil
import tempfile
import time

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {name}" + (f"  {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"[FAIL] {name}" + (f"  {detail}" if detail else ""))


class _Adapter:
    """打桩适配器：mode 决定 run_with_engine 的行为。"""

    def __init__(self, mode: str, version: str = "11.0.0.1") -> None:
        self.mode = mode
        self.version = version
        self.calls = 0

    def run_with_engine(self, fn, timeout_s=None):  # noqa: ANN001, ANN201
        self.calls += 1
        if self.mode == "ok":
            return self.version
        if self.mode == "raise":
            raise RuntimeError("引擎无响应")
        if self.mode == "sysexit":
            raise SystemExit("原生库要求立即退出")
        time.sleep(30)  # hang：模拟原生调用永不返回
        return "never"


def main() -> int:  # noqa: C901 - 测试集合，线性展开最易读
    global PASS, FAIL
    PASS = FAIL = 0

    from dpf_agent.runtime import crashlog, watchdog

    tmp = tempfile.mkdtemp(prefix="wdtest_")
    hist = os.path.join(tmp, "restarts.json")
    logp = os.path.join(tmp, "crash.log")

    # ---- 沙箱重定向与打桩（退出码被"接住"，测试进程不会被真的杀掉）----
    orig_hist = watchdog._history_path
    orig_pt = watchdog._PROBE_TIMEOUT_S
    orig_pg = watchdog._PROBE_GRACE_S
    orig_bmax = watchdog._BREAKER_MAX
    exits: list[int] = []
    orig_exit = os._exit

    watchdog._history_path = lambda: hist          # type: ignore[assignment]
    watchdog._PROBE_TIMEOUT_S = 0.2                # 压缩等待，测试才跑得快
    watchdog._PROBE_GRACE_S = 0.3
    os._exit = lambda code: exits.append(code)     # type: ignore[assignment]
    crashlog._reset_for_tests()
    crashlog.install(logp, max_bytes=1 << 20)

    def _reset_hist() -> None:
        if os.path.isfile(hist):
            os.remove(hist)

    def _write_hist(stamps: list[float]) -> None:
        with open(hist, "w", encoding="utf-8") as f:
            json.dump({"restart_at": stamps}, f)

    try:
        # ================================================== A. 复检判据
        print("--- A. 引擎复检判据 ---")

        # A1/A2：健康 → 继续服务（这是"算例慢"与"引擎中毒"的分水岭）
        watchdog._reset_for_tests()
        exits.clear()
        _reset_hist()
        a_ok = _Adapter("ok")
        res = watchdog.note_engine_failure(a_ok, "TIMEOUT: Solve 超时 (120.0s)")
        check("A1 复检健康时判定继续服务", res["action"] == "kept-running", str(res))
        check("A2 复检健康时绝不退出进程", exits == [], str(exits))
        check("A3 确实执行了复检（不是跳过）", a_ok.calls == 1, str(a_ok.calls))
        check("A4 复检结论进入诊断",
              "健康" in watchdog.diagnostics()["last_probe"],
              watchdog.diagnostics()["last_probe"])

        # A5/A6：复检抛错 → 判定中毒 → 以约定退出码退出
        # 注意：这里**不**重置内部计数，A11/A12 要验证的正是累计口径。
        exits.clear()
        _reset_hist()
        res = watchdog.note_engine_failure(_Adapter("raise"), "TIMEOUT: 引擎超时")
        check("A5 复检无响应时请求重启", res["action"] == "restart", str(res))
        check("A6 退出码为约定的 70", exits == [70], str(exits))
        check("A7 重启时间戳已落盘", os.path.isfile(hist), hist)

        # A8：复检线程自己也不回来（比"超时返回"更严重）
        exits.clear()
        _reset_hist()
        res = watchdog.note_engine_failure(_Adapter("hang"), "TIMEOUT: 卡住")
        check("A8 连复检线程都不返回时同样判定中毒", res["action"] == "restart", str(res))
        check("A9 该情形也触发重启", exits == [70], str(exits))
        check("A10 诊断记录「未返回」字样",
              "未返回" in watchdog.diagnostics()["last_probe"],
              watchdog.diagnostics()["last_probe"])

        # A11/A12：计数口径
        d = watchdog.diagnostics()
        check("A11 复检次数已累计", d["probes"] == 3, str(d["probes"]))
        check("A12 失败复检次数已累计", d["probe_failures"] == 2, str(d["probe_failures"]))

        # ================================================== B. 熔断
        print("--- B. 熔断（防重启风暴） ---")
        watchdog._reset_for_tests()
        exits.clear()
        now = time.time()
        _write_hist([now - 10, now - 20])   # 窗口内已有 2 次
        res = watchdog.note_engine_failure(_Adapter("raise"), "TIMEOUT: 再次卡死")
        check("B1 窗口内重启已达上限时不再退出", exits == [], str(exits))
        check("B2 此时报告为已熔断", res["action"] == "restart" and exits == [], str(res))
        check("B3 熔断次数被计数",
              watchdog.diagnostics()["breaker_blocked"] == 1,
              str(watchdog.diagnostics()["breaker_blocked"]))

        # B4：窗口外的时间戳不应计入（重启解决不了的问题不会永久锁死熔断）
        watchdog._reset_for_tests()
        exits.clear()
        old = now - watchdog._BREAKER_WINDOW_S - 60
        _write_hist([old, old - 10])
        watchdog.note_engine_failure(_Adapter("raise"), "TIMEOUT: 窗口外历史")
        check("B4 窗口外的历史不影响本次重启", exits == [70], str(exits))

        # B5：熔断状态可观测
        open_now, recent = watchdog._breaker_open(time.time())
        check("B5 熔断状态可查询", isinstance(open_now, bool) and recent >= 0,
              f"open={open_now} recent={recent}")

        # ================================================== C. 任务卡死兜底
        print("--- C. 任务卡死兜底判据 ---")
        from dpf_agent.application import service as svc

        orig_lock_fn = svc.current_task_lock
        try:
            watchdog._reset_for_tests()
            exits.clear()
            _reset_hist()

            # C1：锁被占用超过阈值（模拟连超时机制都失效）
            svc.current_task_lock = lambda: {  # type: ignore[assignment]
                "locked": True, "task_id": "pf-stuck", "since": time.time() - 10000,
            }
            watchdog._check_once()
            check("C1 锁长时间未释放时请求重启", exits == [70], str(exits))

            # C2：锁占用但还没到阈值（正常的大算例不能被杀）
            watchdog._reset_for_tests()
            exits.clear()
            _reset_hist()
            svc.current_task_lock = lambda: {  # type: ignore[assignment]
                "locked": True, "task_id": "pf-busy", "since": time.time(),
            }
            watchdog._check_once()
            check("C2 任务仍在正常时间窗内时不重启", exits == [], str(exits))

            # C3：空闲
            watchdog._reset_for_tests()
            exits.clear()
            _reset_hist()
            svc.current_task_lock = lambda: {  # type: ignore[assignment]
                "locked": False, "task_id": None, "since": None,
            }
            watchdog._check_once()
            check("C3 无任务在跑时不动作", exits == [], str(exits))

            # C4：锁信息残缺（since 为 None）不得误判
            watchdog._reset_for_tests()
            exits.clear()
            _reset_hist()
            svc.current_task_lock = lambda: {  # type: ignore[assignment]
                "locked": True, "task_id": "pf-x", "since": None,
            }
            watchdog._check_once()
            check("C4 锁信息残缺时不误判", exits == [], str(exits))
        finally:
            svc.current_task_lock = orig_lock_fn  # type: ignore[assignment]

        # ================================================== D. 安全边界
        print("--- D. 安全边界 ---")
        watchdog._reset_for_tests()
        exits.clear()
        _reset_hist()

        # D1：适配器抛 SystemExit 也不得穿透（诊断不能变成新故障）
        raised = None
        try:
            watchdog.note_engine_failure(_Adapter("sysexit"), "TIMEOUT: 奇怪退出")
        except BaseException as exc:  # noqa: BLE001
            raised = exc
        check("D1 SystemExit 不穿透看门狗", raised is None, repr(raised))
        check("D2 该情形仍被判为中毒并重启", exits == [70], str(exits))

        # D3~D5：adapter 缺失 / 接口不符 → 判定"无法判定"，绝不能重启
        # （配置类错误不应该被当成"引擎中毒"，否则会引发无谓重启）
        watchdog._reset_for_tests()
        exits.clear()
        _reset_hist()
        res_none = {"action": None}
        raised = None
        try:
            res_none = watchdog.note_engine_failure(None, "TIMEOUT: 无适配器")
        except BaseException as exc:  # noqa: BLE001
            raised = exc
        check("D3 适配器缺失时不抛异常", raised is None, repr(raised))
        check("D4 适配器缺失时判为「无法判定」而非中毒",
              res_none.get("action") == "unavailable", str(res_none))
        check("D5 适配器缺失时绝不重启", exits == [], str(exits))

        class _NoProbe:
            """有适配器外形，但没有复检接口。"""

        exits.clear()
        _reset_hist()
        res_bad = watchdog.note_engine_failure(_NoProbe(), "TIMEOUT: 接口不符")
        check("D6 接口不符时同样判为「无法判定」",
              res_bad.get("action") == "unavailable", str(res_bad))
        check("D7 接口不符时不重启", exits == [], str(exits))

        # D8/D9：可整体禁用
        orig_enabled = watchdog._ENABLED
        watchdog._ENABLED = False
        try:
            check("D8 禁用开关生效（install 返回 False）",
                  watchdog.install() is False)
            _reset_hist()
            exits.clear()
            res = watchdog.note_engine_failure(_Adapter("raise"), "TIMEOUT: 禁用态")
            check("D9 禁用态下复检被跳过", res["probed"] is False, str(res))
            check("D10 禁用态下绝不退出", exits == [], str(exits))
        finally:
            watchdog._ENABLED = orig_enabled

        # D11：历史文件损坏时不影响决策（宁可少一次熔断计数，也不能拒绝重启）
        watchdog._reset_for_tests()
        exits.clear()
        with open(hist, "w", encoding="utf-8") as f:
            f.write("{ 这不是合法 JSON")
        watchdog.note_engine_failure(_Adapter("raise"), "TIMEOUT: 历史损坏")
        check("D11 历史损坏时仍能重启", exits == [70], str(exits))

        # ================================================== E. 接线与契约
        print("--- E. 接线与静态契约 ---")
        watchdog._reset_for_tests()
        exits.clear()
        _reset_hist()

        d = watchdog.diagnostics()
        check("E1 诊断含关键字段",
              all(k in d for k in ("enabled", "started", "probes", "probe_failures",
                                   "triggers", "breaker_open", "params")),
              str(sorted(d.keys())))

        # E2：install 幂等（重复调用不能起两个线程）
        watchdog._reset_for_tests()
        first = watchdog.install()
        second = watchdog.install()
        check("E2 install 幂等", first is True and second is True, f"{first}/{second}")

        # E3：crashlog 认识 watchdog 类别（否则会被静默归入 manual）
        check("E3 crashlog 支持 watchdog 类别",
              "watchdog" in crashlog._VALID_KINDS, str(crashlog._VALID_KINDS))

        # E4：看门狗决策写进了崩溃日志
        text = open(logp, encoding="utf-8").read() if os.path.isfile(logp) else ""
        check("E4 决策已写入崩溃日志（kind=watchdog）", "kind=watchdog" in text)
        # E4b：启用公告**不得**写进崩溃日志
        # （否则后端每启动一次就多一条疑似异常的记录，用户打开日志会以为反复出问题）
        check("E4b 启用公告不污染崩溃日志",
              "看门狗已启动" not in text,
              (text[-140:].replace("\n", " ") if text else "<日志为空>"))

        # E5/E6：server 启动时装看门狗，且早于 create_app
        from dpf_agent.web import server as web_server

        src_server = inspect.getsource(web_server)
        check("E5 server 提供 _install_watchdog", "_install_watchdog" in src_server)
        src_main = inspect.getsource(web_server.main)
        i_wd = src_main.find("_install_watchdog")
        i_app = src_main.find("create_app()")
        check("E6 看门狗装在 create_app 之前", 0 < i_wd < i_app, f"wd@{i_wd} app@{i_app}")
        check("E7 诊断接口暴露看门狗段", '"watchdog"' in src_server)

        # E8/E9：service 只对引擎类错误码上报（避免业务失败白花十几秒）
        from dpf_agent.application.service import ApplicationService

        src_report = inspect.getsource(ApplicationService._report_engine_failure)
        check("E8 只对 TIMEOUT / ENGINE_ERROR 上报",
              "ErrorCode.TIMEOUT" in src_report and "ErrorCode.ENGINE_ERROR" in src_report)
        check("E9 上报函数自身吞掉异常（失败路径不得再抛）",
              "except Exception" in src_report)
        src_run = inspect.getsource(ApplicationService.run)
        check("E10 run() 的两条失败分支都已接线",
              src_run.count("_report_engine_failure") >= 2,
              str(src_run.count("_report_engine_failure")))

        # E11/E12：Electron 侧的路由（退出码约定 + 等端口释放）
        ui = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.dirname(os.path.abspath(__file__))))),
            "tianshu_ui", "main.js",
        )
        if os.path.isfile(ui):
            js = open(ui, encoding="utf-8").read()
            check("E11 Electron 识别看门狗退出码 70",
                  "WATCHDOG_EXIT_CODE = 70" in js, ui)
            lifecycle_path = os.path.join(os.path.dirname(ui), "backend_lifecycle.js")
            lifecycle = open(lifecycle_path, encoding="utf-8").read() if os.path.isfile(lifecycle_path) else ""
            check("E12 重启前等待端口释放（防重启风暴）",
                  "require('./backend_lifecycle')" in js
                  and "backendLifecycle.recover(record)" in js
                  and "await this.drain(record" in lifecycle
                  and "this.listening(record.base)" in lifecycle
                  and "port-released=true" in lifecycle,
                  lifecycle_path)
            check("E13 反复启动失败时停止自动重启（不下无尽转圈）",
                  "RESTART_MAX" in js and "backendRestartAt" in js)
        else:
            check("E11 Electron 识别看门狗退出码 70", False, f"找不到 {ui}")
            check("E12 重启前等待端口释放（防重启风暴）", False, f"找不到 {ui}")
            check("E13 反复启动失败时停止自动重启（不下无尽转圈）", False, f"找不到 {ui}")

    finally:
        # ---- 还原一切外部状态，避免污染同进程内其它测试套件 ----
        watchdog._history_path = orig_hist          # type: ignore[assignment]
        watchdog._PROBE_TIMEOUT_S = orig_pt
        watchdog._PROBE_GRACE_S = orig_pg
        watchdog._BREAKER_MAX = orig_bmax
        os._exit = orig_exit                        # type: ignore[assignment]
        watchdog._reset_for_tests()
        crashlog._reset_for_tests()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n=== 结果：通过 {PASS} / 失败 {FAIL} ===")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
