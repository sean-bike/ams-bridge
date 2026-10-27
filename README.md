# AMS bridge

通过 Python API 或命令行，经 SSH 在 Linux 服务器运行 Cadence AMS，取得原始模拟波形、数字四态事件及诊断日志。无需活动的 Virtuoso、CIW 或 Maestro 会话，无需部署远端 bridge 服务。

当前版本：`0.5.1.dev1`。许可证：[MIT](LICENSE)。

## 功能与范围

- 声明和冻结仿真输入，原样传递原生参数、脚本及求解设置。
- 提交、查询、等待、取消、恢复、收集；支持批量调度和显式清理。
- 读取实数瞬态 ASCII PSF、BINPSF、PSFXL，以及数字 VCD、SHM。
- PSFXL 所选导出采用一次批量数据传输、一个解析子进程；已发布导出可离线重读。
- 返回原始时间轴、单位、点序和来源。测量、重采样及电路指标判断由调用方完成。

AC、噪声、复数及多 sweep 波形尚不支持。不同格式的读取边界见 [波形文档](docs/WAVEFORMS.md)。

## 环境要求

客户端需要 Python 3.11+ 和系统 OpenSSH；远端需要 Linux、Bash、GNU coreutils、tar、util-linux、`/proc`，以及可运行 Xrun/AMS 的 Cadence 安装和许可证。Cadence 软件、PDK、预编译工程库均不随仓库分发。

原生格式转换按需使用服务器上的 `psf`（BINPSF）、`simvisdbutil`（SHM）或 `srrutil`（PSFXL）。基本客户端无 Python 第三方依赖；本地 PSF/VCD 读取需要安装 `waveforms` 可选依赖。

## 安装

从仓库根目录执行，以下示例使用 PowerShell：

```powershell
python -m venv .venv
$py = '.\.venv\Scripts\python.exe'
& $py -m pip install -e ".[waveforms]"
& $py -m ams_bridge --help
```

Linux/macOS 的 Python 路径为 `.venv/bin/python`，其余 Python 命令相同。

## 配置与首次运行

复制示例为自己的本机配置：

```powershell
Copy-Item config/rc.example.json config/my-project.local.json
Copy-Item config/ssh.example config/ssh.local
```

编辑 `config/ssh.local` 的 HostName、User、IdentityFile；编辑 JSON 的 `execution.ssh_host`、`xrun_path`、初始化设置及 `case.waveforms[].converter`，使之对应目标服务器。示例工具路径只是参考安装位置。配置中的路径相对配置文件，输入源码中的路径仍按原生工具规则解释。

RC 示例不依赖工程预编译库，声明两条模拟曲线。先检查环境，再求解：

```powershell
& $py -m ams_bridge validate config/my-project.local.json
# validate 成功后运行：
& $py -m ams_bridge run config/my-project.local.json
```

`validate` 不启动仿真。`run` 成功标准为退出码 0、`ok=true`、`simulation.status="success"`、`data.required_complete=true`；示例应返回 `native_tb.vin` 和 `native_tb.vout`。这表示执行和必需数据取得成功，不表示电路指标通过。

```python
from ams_bridge import run_simulation, read_results

result = run_simulation("config/my-project.local.json")
if result["ok"]:
    analog = result["data"]["analog"]
    digital = result["data"]["digital"]
else:
    print(result["errors"], result["job"])

# 使用原作业句柄重读，不重新提交：
result = read_results(result["job"]["local_directory"])
```

## 示例与配置

仓库只保留一个自包含示例：[examples/rc](examples/rc)。数字脉冲经混合信号接口驱动 RC 电路，运行 80 ns，读取输入和输出两路 PSFXL 瞬态曲线。

| 模板 | 用途 |
|---|---|
| [rc.example.json](config/rc.example.json) | 完整的 RC 运行与波形读取配置 |
| [batch.example.json](config/batch.example.json) | 批量运行已准备的输入快照 |
| [storage.example.json](config/storage.example.json) | 存储查询与显式清理 |
| [unl.example.json](config/unl.example.json) | 导入服务器上的原生工程导出 |
| [ssh.example](config/ssh.example) | SSH 连接模板 |

配置按用途命名，文件名不绑定开发版本；配置内部的 schema_version 仍用于结构校验。

## 接入自己的工程

1. 设置 `case.input_root` 和相对的 `working_directory`。
2. 优先采用 `dependency_mode="explicit"`，完整声明 `entry_files`、`additional_inputs`；共享模型和预编译库用 `external_dependencies` 引用。
3. `case.arguments` 使用有序字符串数组，保留原生参数边界。保存格式、精度、probe 和停止条件由输入脚本明确指定。
4. 声明日志、可选归档，以及至少一项必需波形。信号名、分析、coverage、converter 和预算应对应自己的工程。
5. 先 `prepare` 检查输入清单，再 `validate`、`run`。每次使用独立输出目录。

详见 [原生输入](docs/NATIVE.md)、[UNL 导入](docs/UNL.md)、[批量调度](docs/BATCH.md) 和 [波形接口](docs/WAVEFORMS.md)。

## 查询与恢复

```powershell
& $py -m ams_bridge status JOB
& $py -m ams_bridge status JOB --details
& $py -m ams_bridge logs JOB --limit-bytes 65536
& $py -m ams_bridge cancel JOB
& $py -m ams_bridge collect JOB
& $py -m ams_bridge read-results JOB
& $py -m ams_bridge waveforms JOB REQUEST.json
```

`JOB` 是作业目录或 `job.json`。`collect` 取得并验证产物；`read-results` 按原冻结声明解析或复用结果，不重新提交。unknown 状态不代表没有启动，应先查询原句柄。

`execution.wall_timeout_s` 省略或设为 null 表示没有远端硬时限；正数限制远端执行总时长。客户端默认等待 300 秒，使用 `--wait-timeout-s` 单独设置。客户端等待到期不会取消远端作业。SSH、传输及解析有各自的操作预算。

## 测试

安装 `.[waveforms]` 后运行不连接服务器的回归：

```powershell
& $py scripts/test.py --output runs/local-tests.json
```

远端测试默认不启用。需要时指定自己的连接配置，测试会创建专用服务器作业：

```powershell
$env:AMS_BRIDGE_TEST_CONFIG = 'config/my-project.local.json'
& $py scripts/test.py --pattern test_remote.py --output runs/remote-tests.json
```

测试分层、原生固定夹具要求及验证限制见 [测试指南](docs/TESTING.md)。GitHub Actions 运行本地测试和包构建，不连接私有服务器。

## 项目布局

| 路径 | 内容 |
|---|---|
| `src/ams_bridge/` | Python API、CLI 与实现 |
| `tests/` | 自动测试及最小固定输入 |
| `examples/` | 自包含的混合信号 RC 示例 |
| `config/` | 可复制的公开配置模板 |
| `docs/` | 使用、格式与开发说明 |
| `scripts/` | 测试及数据对照工具 |

本机配置、凭据、运行输出及构建产物由 Git 忽略。开发约定见 [AGENTS.md](AGENTS.md)。
