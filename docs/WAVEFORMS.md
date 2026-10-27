# 原始波形支持范围

当前接口只提供 analyses/signals/read 和原始点窗口筛选。测量、sample、插值、保持值推算及重采样已移除。产品返回原始数据，独立验收脚本可在产品之外做比较。


| 来源 | 数据路线 | 本轮证据与限制 |
|---|---|---|
| 实数 transient ASCII PSF | 本地 psf-utils 1.11 | 非均匀轴、单位、重复点、缺曲线/损坏拒绝；仅单 sweep 实数 transient |
| BINPSF transient | 远端原生 psf → ASCII → 本地读取 | 真实 RC 的 tb.vin/tb.vout；保留来源成员、工具/参数、输出哈希及下载回执 |
| 数字 VCD | 本地 pyvcd 0.5.1 | 真实 RC、四态总线、别名、位序、同 tick 事件、巨大整数 tick；不接受 real/string/event 变量 |
| 数字 SHM/SST2 | 远端 simvisdbutil → VCD | 真实 RC 数字源/观测/总线；要求精确信号及显式秒窗口；转换可能量化时间、重排跨信号同 tick 顺序 |
| PSFXL 实数 transient | 远端 Cadence srrutil → 原生 VCSV → 本地读取 | 真实 RC 两路各 8,018 点、与独立原生导出一致；支持原库自有临时链接，曲线读取不要求先归档原库 |
| AC、噪声、complex、多 sweep | 未支持 | 请求明确失败，不静默套用 transient 读取 |


## 查询

```json
{"operation":"read","artifact":"digital.vcd","format":"vcd","analysis":"tran","signals":["tb.source.d"],"window":[0,8e-8],"limits":{"points":30000,"bytes":16777216,"timeout_s":30}}
```

Python `waveforms(job, request)` 与 CLI `waveforms JOB REQUEST.json` 共用流程。`read_source(path, request)` 直接读取本地 ASCII PSF/VCD/原生 VCSV，不需要 artifact 字段、作业记录、远端连接或历史结果。BINPSF/SHM 转换后的普通本地数据也可以直接读取；原生 VCSV 本地读取使用 `format="auto"`。

窗口默认 `[start, stop)`，`include_end=true` 包含右端；无窗口返回整个观测。数字 `time_unit="tick"` 接受原生整数/整数字符串；默认秒坐标使用 Decimal 转换。窗口不产生保持值或边界事件；窗口内无事件可以返回空点列，不能把它冒充高层必需的非空曲线。数字 coverage 是源观测范围，区别于返回事件范围。

模拟曲线包含 name、unit、time_unit、coverage、points；数字曲线另含 width、range、timescale、id_code、别名及源顺序。不同声明/分析以 source_id 和 analysis 区分，不静默合并同名曲线。

V5 的单 bit VCD 声明保留位索引：`dc [5]`、`dc [4]`、`dc [0]` 分别公开为 `top.dc[5]`、`top.dc[4]`、`top.dc[0]`，signals 与 read 使用相同精确名称。完整向量仍使用原名称及 range，合法别名保留；真正的完整名称重复仍报错。不自动拼接分散 bit、不补位，不要求增加 observer 改写原 dump。旧配置若请求了原文件不存在的整条总线名，重读仍报告 missing_signal，不修改冻结请求来替代它。

对已结束作业可使用 Python `read_results(job)` 或 CLI `read-results JOB` 按原声明重读，复用已有原生导出并使用当前 reader。它不重新求解，不改变原始波形/日志/冻结输入；只更新派生读取记录和当前汇总。单个新信号选择继续使用 waveforms 接口。执行终态和必需数据是否完整分别判断。

## 读取与预算

ASCII PSF 由第三方库完整解码，再只为选择的信号/窗口构造点对象；VCD 完整扫描 token，只物化所选事件。SHM 在原生转换时下推信号/窗口。PSFXL 按精确信号名导出完整源点，没有已验证的窗口下推选项；窗口在本地筛选。V5.1 保留一次远端脚本内逐信号调用 srrutil，随后将本次所选 VCSV 与大小/哈希清单放在一次有界 tar 流中下载，复用安全解包和既有发布机制。不是全库下载，也不代表全流程只有一个 SSH 连接；元数据、原生工具和操作租约仍有独立控制调用。

多个 VCSV 在一个可终止的解析子进程中顺序读取，点数和输入/输出字节预算按整个请求累计；错误指出文件。原生点序、重复点、单位和各曲线独立时间轴保持不变，不插值或重采样。已缓存导出的重读没有远端导出/传输，同样只启动一个解析子进程，不新增解析结果缓存。单文件 read_source 和其他格式保持既有接口。


points 限制整个解析源中的点/事件（包含 VCD aliases），bytes 限制输入文件/所选数据库及返回 JSON；timeout_s 限制解析子进程、转换和操作。JSON 字节上限不是进程 RSS 硬上限。格式不支持、超预算、缺信号、覆盖不足和损坏均明确失败。

首次冻结/取得/转换发布时做完整性验证与原子发布。常规读取依据已发布来源与成员的 size/mtime 等轻量状态复用；发现变化再校验内容。转换缓存命中不重新访问远端或全量哈希输出。新远端导出位于各作业 `work/.derived/<id>`；本地文件与来源记录继续保存在 `derived/<id>`，已有发布记录按其位置复用。清理 remote work 会删除本轮远端临时导出，本地曲线仍可读。已发布文件不原地修改；输入/输出损坏不会变成空数组或默认零值。

完整原库归档仍使用严格成员规则，拒绝链接/越界/特殊成员。波形查询可直接读取 job/work 的源；PSFXL 的原生工具在服务器读取自身数据库链接，派生普通文件经过下载完整性校验。原库目录的严格归档规则不放宽。显式完整原库请求失败不能用部分曲线冒充完整归档。

数字读取沿用既有 VCD/SHM 范围。V4 混合 PSFXL 库的 SST2 数字分区虽然能由 simvisdbutil 导出，但实测导出的 VCD timescale 为 `0.0001fs`，超出当前 VCD reader 支持范围，会返回 `unsupported_format` 并保留原生输出。不能由 PSFXL 实数读取成功推断这个数字分区也可读；默认格式示例的整数 timescale VCD/SHM 已另行验证。
