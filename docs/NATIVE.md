# 原生参数、脚本与工程库

## 两种输入模式

`dependency_mode="explicit"` 按 `entry_files`、`additional_inputs` 和 `external_dependencies` 打包/引用输入。参数文件和脚本保持原字节，`case.arguments` 保持有序 argv、重复项、大小写及值边界。原生工具解释 `-f/-F`、嵌套参数和 Tcl，不把未知选项的值猜成文件。

用户需要完整声明脚本引用的输入，并让仿真输出位于本作业 work。bridge 不执行 Tcl 来发现依赖，也不承诺发现动态读取的全部文件。bridge 自己的上传、argv 引用、收集和清理范围检查仍然执行。

`dependency_mode="resolve"` 自动发现已有支持的 include、参数文件和已知路径引用。不支持的依赖语法会提示改用 explicit。其静态扫描范围不等于目标模拟器的完整语言/选项范围。

## 可运行的 PSFXL 示例

[rc.example.json](../config/rc.example.json) 使用自包含的 [RC 示例](../examples/rc)，不需要预编译工程库。配置中的 SSH alias、SSH 配置及工具路径需对应目标服务器。

```powershell
python -m ams_bridge run config/rc.example.json
```

示例通过原生 `-amsformat psfxl_all`、`-svseed 42`、`-notimingchecks`、`-spectre_args "+aps +mt=2"` 配置求解。Tcl 使用变量、层次 probe 和两段 run；`.scs` 保留温度、reltol、errpreset 和 maxstep。bridge 不追加保存格式、seed 或精度设置。

实际验证的组合是 Xrun 18.03-s001、其 AMS 日志中的 Spectre 17.1.0.161.isr2，以及 IC6.1.8-64b.83 的 srrutil。日志确认 APS Enabled；RC 很小，求解器明确关闭多线程。`requested_threads=2` 与 `multithreading="disabled"` 分别记录，不将请求的线程数冒充实际工作线程数。

其他原生设置（包括不同 `-amsformat`、`-delay_mode`、Spectre X `+preset=...`）可以原样传入；是否可运行取决于实际 AMS/工具版本和许可证。Spectre X/preset 本轮没有原生求解成功证据。`-amsformat` 是 AMS 选项，不能等同于独立 Spectre 的 `-format`。

单作业 `+mt` 和 batch 的 `concurrency` 是两个独立设置。编译或 elaboration 原生选项可透传，但不因此新增分阶段提交、编译缓存或跨作业快照复用接口。

## 库与外部依赖

服务器预编译库通过原生 `-reflib <目录>:<库名>` 引用，同时在 `external_dependencies` 声明目录；作业内的源库可以使用 `-makelib <库名> ... -endlib` 编译。库的实际路径和编译方式由工程提供，输出位于各作业 work。

```json
{"external_dependencies": [
  {"type":"directory","path":"/server/project/xcelium.d/prebuilt_lib","purpose":"precompiled HDL library"}
]}
```

目录声明只记录路径和用途，检查目录存在，不递归复制或哈希。文件声明继续使用 `path`、`purpose`、`sha256`（可选 `type="file"`）。`-v/-y/-libext`、include 目录、SDF 和 DPI 配套输入均可按原生参数声明；DPI 的编译、ABI 和目标平台适配由用户负责。SDF/DPI 本轮验证的是声明与参数传递，未宣称完成对应电路的回标/DPI 功能验收。

## 工具和结果来源

通过 `execution.xrun_path`、`environment`、`environment_setup` 选择实际环境。入口检查使用配置的 Xrun；PATH 缺少独立 Spectre 不单独阻止 AMS。运行日志中的 Xrun/Spectre 版本、实际 APS 提示和线程提示用于结果来源；缺少证据的字段为 `unconfirmed` 或 `null`。


最终 argv 在作业 `request.json` 的 prepared case 中，原生输入在准备快照中，实际工具输出在收集的日志中。运行环境中的 `STANDALONE_SPECTRE_PATH` 只是 PATH 查询，不代表 AMS 实际求解器。

设置透传、原库取得和波形解析分别判断：原生工具能运行 AC/PSS，不表示本版能解析这些曲线。当前数字读取走 VCD/SHM，PSFXL reader 只取原生 `srrDouble` 实数瞬态，混合库的逻辑类型会明确报告为不支持。用户要原库时另在 `case.artifacts` 声明；曲线源不要求先归档原库。详见 [WAVEFORMS.md](WAVEFORMS.md)。

## 纯文本 AMS 接入步骤（V5）

公共起点仍使用 `examples/rc` 的参数文件、Tcl 和配置结构，无需复制一套模板。对于私有工程，另建本地配置并复用当前 execution；共享 PDK/预编译库只以 external_dependencies 引用，不复制到本仓库。完整输入由 explicit 声明。

1. 编写 VAMS 顶层，声明 electrical 模拟节点、数字 wire 及相应 discipline，实例化数字控制、Verilog-A 模型和文本 Spectre 子电路。模块名、子电路名与端口顺序必须对应；不依赖活动的 Virtuoso/CIW/Maestro 会话或 OA config。
2. 在 Spectre 文本中定义激励及物理子电路，显式写模型 section、温度、求解精度和 transient 终点。使用 AMS 文本绑定将边界 cell 交给 Spectre；下面仅示意结构，名称和电压需替换为自己的工程：

```spectre
simulator lang=spectre
include "/server/pdk/models.scs" section=top_tt
include "stimulus.scs"
include "physical.scs"
tran tran stop=100n errpreset=liberal method=trap
amsd {
    ie vsup=1.1 discipline=logic
    portmap subckt=text_stimulus
    config cell=text_stimulus use=spice
}
```

3. 使用参数文件传递顶层、源码、`-amscontrolfile` 或原工程已使用的 `.scs` 输入方式、`-input` Tcl、库映射和原生选项。保持每个选项的 argv 边界，不把多个参数拼成 shell。原生环境不同应核对工具本身支持的写法。
4. 模拟信号沿用公共示例的原生 database/probe 和 PSFXL 设置，waveforms 指向实际输出目录。数字信号使用直接 VCD dump，例如在 Verilog 测试台中 `$dumpfile("digital.vcd"); $dumpvars(0, tb.u_logic);`，实例路径按实际层次填写。无需为 reader 新增 observer 寄存器。先用 signals 列表核对精确名字，分散 bit 用 `tb.u_logic.dc[5]`、`tb.u_logic.dc[0]`，不是自动拼成 `tb.u_logic.dc`。
5. 配置中分别声明必需模拟和数字输出，coverage 与本次短运行一致。模型/源码/脚本按输入清单准备，外部模型目录仅检查存在，不递归复制/哈希。prepare 核对清单，validate 核对入口，然后显式 run。
6. 正常长仿真可以省略 wall_timeout_s 或写 null；资源上限由用户用正数主动开启。客户端默认等待独立的 300 秒，超出后保留句柄，用 status/details 查看，再用 read-results 取得结果，不重复求解。真实集成只需短启动运行，核对执行、信号活动和数据完整，不将其当作锁定、校准、相噪或数值收敛验证。

```powershell
python -m ams_bridge status JOB --details
python -m ams_bridge read-results JOB
```

read-results 使用原冻结的输出声明。若旧声明中的名称在原数据里不存在，会如实报告 missing_signal；需要不同精确名称时使用 waveforms 查询，不能改冻结声明或自动拼总线来制造成功。缺少必需数据时 ok=false；有可用本地终态和导出时可离线重读。

### 已遇到的原生输入注意事项

以下经验来自 Xrun 18.03-s001 / AMS Spectre 17.1.0.161.isr2 的真实接入，仅说明该路径经过验证，不替代其他版本的原生文档：

| 问题 | 本次可用的原生处理 |
|---|---|
| cds.lib 注释 | 使用 `#` 注释；本次 `//` 写法被工具拒绝 |
| 直接参数文件中的 `.va` | 本次以 `.vams` 文件名提交相同 Verilog-A 模型字节并编译；bridge 不自动改后缀 |
| PDK 全局参数与局部节点名冲突 | 对应原生错误提示下显式使用 `-spice_global_param_opt`；不修改共享模型，也不作为所有工程的默认值 |
| Spectre 兼容语法 | 原生参数明确提供 `-simcompatible_ams spectre`；未做单项 A/B，不能声称它改善性能或是此前耗时的唯一原因 |

V5 识别日志中的普通 `APS Enabled.` 与 `Fast APS Enabled.`，分别记录 APS/Fast APS 及启用来源；只有请求参数时保持 unconfirmed。请求线程数与实际启用线程数分开记录。明确的 wall_timeout/取消优先于早期许可证协商提示；具体原生错误与 collection/data 后继错误仍分别保留。

## V5.1：实际完成与辅助预处理诊断

已知 `Spectre_AMSD_Lk ... - license checkout failed` 只有在同次运行随后启动并正常完成模拟时才作为协商上下文；不再强制要求 SIE 模式提示行。无模式证据仍返回 unconfirmed，其他许可证错误不忽略。原生 `Simulation complete via $finish(0/1/2) at time ...` 与正常 Xrun 结束、数字运行开始共同作为完成证据；消息级别不当作退出码，原生 ERROR/assertion/fatal 仍可使执行失败。details 从实际完成行取时间，不从计划终点推算进度。

Xrun 18.03-s001 / AMS Spectre 17.1.0.161.isr2 的一个大型主文件触发了原生 NDB 关闭 cpp 输出管道的事件，bridge 不修复工具内部管道，也不把所有 Broken pipe 或 fatal 视为无害。

只有启动日志中已知的编译器输出失败形式，位于 NDB Parsing 完成之后、首次 Tcl run 之前，同次运行后续有完整模拟和 Xrun 完成证据，且已有 worker completed/退出 0、没有其他未处理原生错误时，才记为 `native_preprocess_pipe_closed` 警告。保留原始 fatal、预处理阶段、原生文件/行列及日志来源；缺少上下文、非零退出、运行中异常、超时/取消或真正编译错误仍按原事实处理。必需数据失败继续使 ok=false。

可选的原生输入组织方式是把很长的 PWL 源语句原样放到 include 文件中，保持较小的主文件。本次独立短启动中，四条 PWL 从 4.9 MB 主文件移到 include 后未再出现该事件，两组均正常退出；未完成全程数值等价对照，也未证明文件大小是唯一触发条件。bridge 不自动拆分输入、修改模型、添加求解参数或改动工具安装。
