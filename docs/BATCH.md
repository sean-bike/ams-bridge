# 有界批次

保留原调度器的 start/status/resume/cancel/rerun、持久输入快照和必要尝试记录。单作业执行器不依赖批次；批次不初始化清理策略。

输入见 [batch.example.json](../config/batch.example.json)：schema_version=2、一个 execution、正整数 concurrency、显式 entries，每项包含唯一 id 和 prepared 路径。开始前固定快照、输入指纹和各项 run_id；原输入移走仍可恢复。相同输入的不同条目也使用不同作业目录。

```powershell
python -m ams_bridge prepare config/my-project.local.json --output prepared/rc-batch
python -m ams_bridge batch start config/batch.example.json --output runs/rc-batch
python -m ams_bridge batch status runs/rc-batch
python -m ams_bridge batch resume runs/rc-batch
python -m ams_bridge batch cancel runs/rc-batch
python -m ams_bridge batch rerun runs/rc-batch --entry rc_a
```

Python 对应 batch_start(spec, directory)、batch_status(directory)、batch_resume(directory)、batch_cancel(directory)、batch_rerun(directory, entry_id)。status 只查询，不派发或收集。resume 沿用原 UUID；仅显式 rerun 新建 UUID 并保留 replaces_run_id。取消保存意图，未获准项不再启动；取消请求不等于进程已结束。重叠恢复返回 busy。

submitting/running/unknown 以及进程清理未确认的作业占用求解名额。确认远端结束后，数据状态 pending/processing/complete/failed 与求解占位分开；一个数据工作者收集/转换/读取，协调者更新 batch.json。慢收集不会阻止后续派发；数据中断后可恢复取得数据，不重新求解。Windows 短暂 JSON 读锁导致的原子替换冲突有有界重试。

每次尝试的 summary 为 simulation、data、required_complete、ok；result 引用该作业当前 result.json，不依赖 evaluation。完整曲线保存在单作业的数据源/读取记录中，批次不同时装载所有曲线。finished 表示无待派发、未知执行或在途数据任务；不代表每项成功。unknown 返回 attention 并继续保留容量；单项已确认失败不阻断其他条目。批次 ok 仅当各最新尝试均满足执行及必需数据成功。

V5 的 execution.wall_timeout_s 可省略或为 null，表示单个远端 worker 没有硬时限，批次仍按实际终态释放名额；正数保留显式时限。run 的客户端默认等待 300 秒与 batch 容量、远端硬时限分别处理，不因客户端停止等待而释放一个仍运行或未知的作业名额。

`read-results JOB` 仅重读单作业，可能更新其 result/simulation 汇总，不隐式改写 batch.json。批次自身的恢复和数据汇总仍使用 batch resume：显式 resume 重新校验当前作业结果的 run_id、输入指纹和记录结构，再沿用数据阶段刷新引用与汇总，不新建求解。普通 status 不隐式接纳变更的结果哈希；收到该不一致提示时使用 resume，身份不符的结果仍拒绝。明确重新求解才使用 rerun。V5 不新增跨版本记录迁移。

CLI start/resume/rerun：成功退出 0，已确认单项失败 4，attention/提交不足 3；本地输入错误 2，busy 5。旧测量/validation/电路 passed 汇总和测量 evaluation 已移除。测试方法见 [TESTING.md](TESTING.md)。
