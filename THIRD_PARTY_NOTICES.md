# 第三方依赖与资料说明

核对日期：2026-10-16。整理：codex。工作名称：开源候选版第三方许可与资料边界整理。

本候选版自有项目源码采用根目录 [Apache License 2.0](LICENSE)。第三方依赖及其组件维持各自许可；本文件不将它们重新授权为 Apache-2.0。原始可识别的版权与来源标注应继续保留。

这是源码候选版，未附带 Python 环境、Node.js、`node_modules`、Python wheels、Electron 运行时、OpenDSS 引擎或操作系统组件。依赖由使用者通过包管理器或上游安装包另行安装。推荐的 Windows COM 方案由 [`requirements-com.txt`](dpf_agent/requirements-com.txt) 声明，实际验证版本锁定在 [`requirements-windows-py313.lock.txt`](dpf_agent/requirements-windows-py313.lock.txt)（Windows x64 / CPython 3.13.14，31 个包）；UI 使用 [`package-lock.json`](tianshu_ui/package-lock.json)。下表中的 Python 直接依赖版本均已与该 COM 锁文件核对一致，许可依据来自相同版本的原开发环境安装包。依赖安装与验证以随附锁定文件和目标平台为准。

## 直接依赖

| 依赖 | 候选版锁定版本 | 检查到的许可 | 上游来源 |
|---|---|---|---|
| Electron | 44.8.0（Node 锁文件） | MIT；运行时还带有 Chromium、Node.js 等组件的独立许可 | [Electron](https://github.com/electron/electron/tree/v44.8.0) / [该版本许可](https://github.com/electron/electron/blob/v44.8.0/LICENSE) |
| pydantic | 2.13.5 | MIT | [Pydantic](https://github.com/pydantic/pydantic) |
| fastapi | 0.141.1 | MIT | [FastAPI](https://github.com/fastapi/fastapi) |
| uvicorn | 0.52.4 | BSD-3-Clause | [Uvicorn](https://github.com/Kludex/uvicorn) |
| python-multipart | 0.0.32 | Apache-2.0 | [python-multipart](https://github.com/Kludex/python-multipart) |
| pandas | 3.0.5 | BSD-3-Clause；附带组件见其发行包 LICENSE | [pandas](https://github.com/pandas-dev/pandas) |
| openpyxl | 3.1.5 | MIT（安装包元数据） | [openpyxl](https://foss.heptapod.net/openpyxl/openpyxl) |
| pywin32（Windows COM 通道） | 312 | 元数据为 PSF；各组件存在独立许可，不能以该单一标签替代所有许可 | [pywin32](https://github.com/mhammond/pywin32) |
| starlette | 1.6.0 | BSD-3-Clause | [Starlette](https://github.com/Kludex/starlette) |
| requests | 2.34.2 | Apache-2.0；发行包另附 NOTICE | [Requests](https://github.com/psf/requests) |
| numpy | 2.5.3 | 实际元数据表达式：`BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0` | [NumPy](https://github.com/numpy/numpy) |

pandas 安装包保留 AQR Capital Management、Lambda Foundry、PyData Development Team 与后续开源贡献者的版权声明。Electron 的许可保留 Electron contributors 与 GitHub Inc. 的版权声明。上游完整许可、免责声明与 notices 以安装的对应发行版为准。

### 可选 DSS-Python 引擎

兼容清单 [`requirements.txt`](dpf_agent/requirements.txt) 另声明 `dss-python>=0.15`，供适用主机选择替代引擎；推荐 COM 方案及上述 31 包锁文件均不包含 `dss-python` 或 `dss-python-backend`。本轮未把该可选原生引擎纳入候选版的新环境验收。

原开发环境中另行核对过 **dss-python 0.15.6** 的 BSD-3-Clause 许可与 **dss-python-backend 0.14.4** 安装包的 BSD 许可；相关许可署名包括 Paulo Meira 和 DSS-Python contributors。参见 [DSS-Python 上游](https://github.com/dss-extensions/DSS-Python)。这些可选版本是许可检查记录，并非候选 COM 方案的锁定依赖；其原生引擎与附带组件仍须按实际安装发行版保留全部许可。

## 组件与间接依赖

本次实际检查还确认以下情况，不能仅按顶层包的单一许可标签判断未来的二进制分发：

- **pywin32** 安装包包含 COM、win32、PythonWin、Scintilla、adodbapi 等不同组件许可。COM 部分保留 Greg Stein、Mark Hammond 等作者声明；`adodbapi/license.txt` 是 LGPL-2.1。参见上游 [COM 许可](https://github.com/mhammond/pywin32/blob/main/com/License.txt) 和 [adodbapi 许可](https://github.com/mhammond/pywin32/blob/main/adodbapi/license.txt)。本项目使用 COM 接口，不附带该依赖发行包。
- **certifi** 的 CA 数据来自 Mozilla，其安装包附 MPL-2.0 说明；它是 Requests 的间接依赖。参见 [certifi LICENSE](https://github.com/certifi/python-certifi/blob/master/LICENSE)。
- **NumPy、pandas 及其他二进制 wheels** 可能附带其各自子组件与原生数学库许可，不能只复制本表的顶层 SPDX 名称后分发整个 wheel。
- **Electron** 源码仓库的 MIT 许可不代替 Chromium、Node.js 等运行时组件的许可。若以后制作桌面安装包，应完整保留实际 Electron 发行版的 `LICENSE`、`LICENSES.chromium.html` 及其他随包 notices。

UI 的 `package-lock.json` 已精确锁定 Electron 44.8.0，记录 13 项外部包：9 项 MIT、1 项 BSD-2-Clause、2 项 ISC、1 项 Apache-2.0。已与独立验证环境中实际安装的各包 `package.json` 核对，版本与许可字段一致。该统计反映锁文件与包元数据，未代表已逐文件审计所有组件；顶层项目采用 Apache-2.0 不会修改这些依赖许可。

| Node 包 | 锁定版本 | 该版本包元数据许可 |
|---|---|---|
| `electron` | 44.8.0 | MIT |
| `@electron/get` | 5.1.0 | MIT |
| `@electron-internal/extract-zip` | 1.0.5 | BSD-2-Clause |
| `@types/node` | 24.19.2 | MIT |
| `debug` | 4.4.3 | MIT |
| `env-paths` | 3.0.0 | MIT |
| `graceful-fs` | 4.2.11 | ISC |
| `ms` | 2.1.3 | MIT |
| `progress` | 2.0.3 | MIT |
| `semver` | 7.8.5 | ISC |
| `sumchecker` | 3.0.1 | Apache-2.0 |
| `undici` | 7.30.1 | MIT |
| `undici-types` | 7.24.6 | MIT |

`@electron/get` 的许可保留 Contributors to the Electron project 的版权，`undici` 的许可保留 Matteo Collina 与 Undici contributors 的版权；`sumchecker` 由 Mark Lee 维护，发行包附 Apache-2.0 完整文本，参见 [sumchecker 3.0.1 LICENSE](https://github.com/malept/sumchecker/blob/v3.0.1/LICENSE)。`@electron-internal/extract-zip` **1.0.5** 的安装包元数据和 README 声明 BSD-2-Clause，该版本源码 [package.json](https://github.com/electron/extract-zip/blob/v1.0.5/package.json) 也作此声明；所检查安装包未附独立许可全文。不能用上游 `main` 分支后来的许可替换该锁定版本声明；若将它纳入二进制分发，需要先核对并补齐适用于该版本的完整版权、许可和原生组件 notices。本源码候选版不附带这些包。

目前仅提供源码及依赖安装指引。本文件不是完整 SBOM，也不表示已经完成所有平台、所有传递依赖、未来版本或二进制安装包的法律审计。以后若将依赖、引擎或安装包纳入发布，应根据锁定后的实际分发内容补齐完整许可文本、版权声明、notices 以及适用的源码提供义务。

## 外置仿真引擎与格式支持

- **EPRI OpenDSS**：通过用户另行安装的引擎使用 COM 或 CLI。本候选版未包含 EPRI DLL、EXE、安装包、官方测试馈线或手册。OpenDSS [官方发行项目](https://sourceforge.net/projects/electricdss/)标注 BSD；所核对的 EPRI 引擎许可为 BSD-3-Clause，并保留 Electric Power Research Institute, Inc. 的版权。若未来再分发引擎，需按对应发行版保留全部许可、版权与免责声明，不能借 EPRI 或贡献者名义背书。
- **MATPOWER**：本项目静态解析部分 MATPOWER case v2 格式；未捆绑 MATLAB、Octave 或 MATPOWER 求解器。支持格式不表示拥有外部算例数据的权利。[MATPOWER 官方许可](https://matpower.org/license/)明确区分 BSD-3-Clause 软件代码与不在该 BSD 许可覆盖范围内的 case 文件。导入或发布算例时仍需逐项核对来源、数据许可和引用要求。

## 保留的知识摘要与合成素材

`dpf_agent/dpf_agent/knowledge/knowledge_items.json` 的 5 项内容是简短 API 事实、工程约定和本项目发布规则的摘要，并非标准或论文全文：

- `KB-ODSS-001` 与 `KB-ODSS-002` 保留 OpenDSS / DSS-Python 来源标记；可参阅 EPRI 的 [Powers](https://opendss.epri.com/Powers.html) 与 [kVBase](https://opendss.epri.com/kVBase.html) API 说明。原文档的版权及商标归原权利人，不因本项目引用事实而转移。
- `KB-RULE-001`、`KB-RULE-002` 与 `KB-MAP-001` 描述本项目的状态区分、缺失值和字段映射约定。

随附 `case4`、`case_tx`、`case_broken`、`case_nonconv`、`bus_reference_probe`、`pvsystem_probe`、`storage_discharge_probe` 是小型功能或故障探针，不应称作 IEEE 官方基准或独立的外部数值真值。转换器与解析器测试主要使用程序化构造的少节点输入；出现 MATPOWER/IEEE 名称不表示附带了整个外部数据集。测试中用于识别引擎版本输出的 EPRI 版权行仍保留原归属。

## 未纳入本候选版的外部资料

为避免将可公开访问误当成可任意再分发，以下内容未随本源码候选版提供，也未被本项目 Apache-2.0 许可覆盖：

- Baran & Wu 来源的 `ieee33` 内置网络数据；
- 从 OpenDSS 安装包复制的 IEEE 官方馈线数据、期望结果及其原始 `.doc`、`.xls`、`.zip` 取证包；
- 国标、行标、论文、书籍的摘编 Markdown 知识资料；
- OpenDSS 参考指南、潮流原理参考 PDF、内部设计/汇报/日报和办公文档。

需要这些资料时，请从原发布方获取并遵守其适用条款。以后补充外部资料或数据，应记录具体来源、版本、原作者、许可及必要引用，得到相应依据后再纳入发布。
