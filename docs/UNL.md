# UNL 导入

UNL 导入器读取服务器上的既有原生导出目录，将已识别的输入与只读模型引用组织为可冻结的本地工程。它不需要活动的 Virtuoso、Maestro、CIW 或 OCEAN 会话。

从 [unl.example.json](../config/unl.example.json) 复制 spec，填写 Linux 源目录、调用目录、模型根和输出声明。连接参数取自己的 execution 配置：

```sh
python -m ams_bridge import-unl config/my-unl.local.json --execution config/my-project.local.json --output runs/my-unl-import
python -m ams_bridge prepare runs/my-unl-import/config.json --output prepared/my-unl
python -m ams_bridge submit runs/my-unl-import/config.json --prepared prepared/my-unl
```

导入产物包含 config.json、输入报告及原始字节副本。报告记录路径、哈希、引用位置和改写原因；不会修改服务器原工程或共享模型。文件数量、单文件字节、总字节和读取时间均有预算。

支持的输入是已识别的 Xrun 参数文件、HDL/Spectre 字面量引用、绑定与模型布局；未知参数、动态引用或任意 OA/config/Maestro 状态不属于通用导入承诺。`stop_s` 只能显式裁剪原瞬态时长。原生 explicit 输入模式的通用规则见 [NATIVE.md](NATIVE.md)。
