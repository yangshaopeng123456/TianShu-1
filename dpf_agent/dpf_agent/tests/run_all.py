"""统一测试入口：一次跑完所有自包含回归测试。

用法：
    python -m dpf_agent.tests.run_all          # 全部
    python -m dpf_agent.tests.run_all --list   # 只列出

**为什么需要它**：这些回归测试之前散在多个 `python -m ...` 命令里，改动后容易漏跑。
本项目已经反复吃过"改了实现但没跑测试"的亏（例如 eval 参考值根本没加载，却一直
显示"全绿"）。统一入口 + 退出码，是把它接进 CI 的前提。
"""
from __future__ import annotations

import importlib
import sys
import time

# 控制台默认是 GBK：套件里打印的 ⑪ / ⚠ / − 这类字符会直接抛 UnicodeEncodeError，
# 于是**通过中的套件被判成「执行异常」**。实测：不设这个，17 个套件里有 5 个"失败"，
# 逐个单跑却全通过 —— 假红比不跑更危险，它会训练人忽略红色。
# 只把 errors 改成 replace（**不动 encoding**）：中文照常显示，个别生僻符号变 "?"，
# 而 PASS/FAIL 前缀是 ASCII，判定不受影响。若强行改成 utf-8，在 936 代码页的
# 控制台上反而会满屏乱码。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:  # noqa: BLE001 - 老解释器/特殊流没有 reconfigure 就维持原样
        pass

# (模块名, 说明)  —— 全部为自包含测试，不依赖引擎、不依赖网络
SUITES: list[tuple[str, str]] = [
    ("test_release_gate", "自动交付门禁：退出码 / 中断 / 原生证据身份 / 完整发布范围"),
    ("test_process_isolation", "任务COM隔离：真实子进程 / 超时取消回收 / Job父崩溃 / 持久上下文"),
    ("test_batch_scheduler", "批量队列：SQLite持久化 / 逐项取消重试 / 依赖冻结 / 重启恢复"),
    ("test_task_recovery_api", "任务详情：在途空状态 / 当前锁归属 / 重启中断只读恢复 / 终态保留"),
    ("test_case_data_dependencies", "DSS数据依赖：CSV/二进制冻结 / 内容指纹 / 缺失与逃逸拒绝"),
    ("test_conversion_fidelity", "转换扩展：Gs/Bs / π充电 / 正序相移 / Q策略 / 铭牌来源"),
    ("test_fidelity_mapping", "保真结果映射：源对象身份 / 真实端口聚合 / 并联导纳分列"),
    ("test_phase_modeling", "分相（三相不平衡）建模：相标解析 / 节点命名 / 零序参数 / 拒绝与零回归"),
    ("test_unbalance", "不平衡度算法：对称分量法 / 拒判条件 / 旋转不变性"),
    ("test_case_converters_modeling", "转换器建模口径：Vm / Model=3 / Vg / ratio→tap / kVA 基准"),
    ("test_matpower_parser", "MATPOWER 静态字面量：同行矩阵 / 续行 / 严格拒绝 / 整数身份"),
    ("test_conversion_validation", "转换输入完整性：有限值 / 单位 / 标识 / 引用 / 显式空负荷"),
    ("test_conversion_semantics", "转换语义保真：源侧负荷 / 变比方向 / 零出力 PV / 原子写入"),
    ("test_conversion_publication", "转换发布：附件隔离 / 目录回滚 / 源内容变化 / 警告传递"),
    ("test_conversion_cache", "转换产物版本戳与惰性重建"),
    ("test_case_product_guard", "算例产物引用守卫：非法属性拦阻 / 引用路径不许绕过校验"),
    ("test_crash_observability", "崩溃可观测 + 状态增量落盘 + interrupted 终态 + 诊断接口"),
    ("test_narration", "执行过程思考流：动作行 / 模板取值 / 失败态 / 可复现 / 不许编内容"),
    ("test_solve_cache_key", "求解缓存键：内容决定 / 与路径无关 / 参数齐备"),
    ("test_load_caliber", "总负荷口径：元件求和 / 净负荷分列 / 含发电功率平衡"),
    ("test_report_caliber", "对外能力口径 + 报告「本次求解口径声明」一致性"),
    ("test_engine_resilience", "引擎中毒自愈：超时后重建引擎 / 恢复可用 / 零额外开销"),
    ("test_watchdog", "引擎看门狗：复检判据 / 熔断防重启风暴 / 卡死兜底 / 不误杀边界"),
    ("test_circuit_validation", "Compile 后电路校验：规则单点定义 / 母线与基准判定 / 各通道不重复实现"),
    ("test_network_profile", "网络画像：收集与组装分离 / 纯数据可离线复算 / 闭包写法不许回归"),
    ("test_cli_runner", "CLI 驱动层：命令白名单 / banner 版本 / 编码 / 算例镜像 / 零删除契约"),
    ("test_cli_adapter", "CLI 通道适配器：export 解析 / 口径复用 / 接口形态契约（不启进程）"),
    ("test_case_upload", "算例上传：多选链路契约 / 同名文件整体拒绝（先校验后写盘）"),
    ("test_storage_admin", "历史存储清理：不可清理项守卫 / 分类判据 / 归档拒绝规则 / 可回滚"),
    ("test_focus_answer", "聊天回复定向取数：问什么答什么 / 取不到就不说 / 能力边界明说 / 可复现"),
    ("test_recovery_verification", "恢复策略：元素类型 / 模型身份 / 硬约束 / 二次求解与关键结果"),
    ("test_upload_model_integrity", "上传模型：ZIP 隔离 / 停运机组 / 重建失败保留旧产物"),
    ("test_service_lifecycle", "任务生命周期：进度归属 / 失败状态 / 报告串行 / 取消与判据"),
    ("test_chat_cancellation", "取消终态：取消原因 / 聊天状态 / 独立提示 / 故障诊断保留"),
    ("test_snapshot_stability", "数值稳定化：累计预算 / 独立最终复算 / 身份 / 取消 / 权威产物一致"),
    ("test_history_fastpath", "历史指标快路由：精确意图 / 已验证身份 / 不绕过显式重算"),
    # 放在最后：它是"测测试的"那种套件 —— 先跑完所有业务断言，再保证
    # "写判据这件事本身"没有被写坏（三态判定 + 自检非空洞 + 判据按量的性质分派）。
    ("test_judgment_selftest", "判据自检门禁：三态判定 / 自检非空洞（注入错误判据必须被抓）"),
]


def main(argv: list[str]) -> int:
    if "--list" in argv:
        for module, note in SUITES:
            print(f"  {module:<32} {note}")
        return 0

    total_pass = total_fail = 0
    failed_suites: list[str] = []
    width = max(len(m) for m, _ in SUITES)
    print("=" * 78)
    for module, note in SUITES:
        name = f"dpf_agent.tests.{module}"
        t0 = time.time()
        try:
            mod = importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001
            print(f"  [加载失败] {module:<{width}}  {type(exc).__name__}: {exc}")
            failed_suites.append(module)
            continue
        try:
            rc = int(mod.main() or 0)
        except Exception as exc:  # noqa: BLE001
            print(f"  [执行异常] {module:<{width}}  {type(exc).__name__}: {exc}")
            failed_suites.append(module)
            continue
        # 各套测试自己会打印 "=== 结果：通过 X / 失败 Y ==="，这里只汇总与判定
        elapsed = time.time() - t0
        status = "OK  " if rc == 0 else "FAIL"
        print(f"  [{status}] {module:<{width}}  {elapsed:.2f}s   {note}")
        if rc != 0:
            failed_suites.append(module)

    print("=" * 78)
    if failed_suites:
        print(f"存在失败套件（{len(failed_suites)}）：{', '.join(failed_suites)}")
        return 1
    print(f"全部 {len(SUITES)} 个套件通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
