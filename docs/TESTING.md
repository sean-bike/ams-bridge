# 核心测试

测试集中在四个模块，输入直接在测试中构造，不需要历史运行记录或数据库。

| 模块 | 覆盖范围 |
|---|---|
| `test_inputs.py` | 输入快照、依赖声明、路径隔离、配置校验及 CLI prepare |
| `test_execution.py` | 许可证协商、正常完成、原生错误优先级、警告传播、run 返回数据与等待行为 |
| `test_waveforms.py` | PSF/VCD 格式、四态和点序、窗口、预算、PSFXL 批量传输及离线复用 |
| `test_remote.py` | 可选 SSH 冒烟：提交/收集、取消隔离、断连超时及清理 |

## 本地运行

安装 `.[waveforms]` 后执行：

```sh
python scripts/test.py --output runs/local-tests.json
```

不设置 `AMS_BRIDGE_TEST_CONFIG` 时，SSH 冒烟测试不运行。报告区分通过、失败和未运行，并核对测试前后源码是否一致。GitHub Actions 运行本地测试和包构建。

## SSH 冒烟

从公开模板创建自己的配置并填写连接参数，然后执行：

```powershell
$env:AMS_BRIDGE_TEST_CONFIG = 'config/my-project.local.json'
python scripts/test.py --pattern test_remote.py --output runs/remote-tests.json
```

这些测试使用 shell 工作负载创建专用远端作业，不调用电路仿真。只取消或清理测试自身创建的作业。

## 真实 AMS

按 README 的 RC 示例运行 `validate` 和 `run`，验证许可证、原生求解器和波形转换器。个人工程使用公共 run/waveforms 接口及独立配置。

核心回归不再包含完整远端故障注入、调度压力和历史版本验收矩阵，也不验证电路指标、完整工程精度等价或所有 Cadence 版本。
