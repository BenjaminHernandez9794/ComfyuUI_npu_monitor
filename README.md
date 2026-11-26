# ComfyUI NPU Monitor

在 ComfyUI 界面上显示一个**可随意拖动的悬浮状态栏**，实时监控华为昇腾（Ascend）NPU：

| 指标 | 来源 |
| --- | --- |
| NPU 显存（HBM / 设备内存）已用 / 总量 / 占比 | `npu-smi info`（无 npu-smi 时回退 `torch_npu.npu.mem_get_info`） |
| AI Core 算力利用率 (%) | `npu-smi info` |
| 芯片温度 (°C) | `npu-smi info` |
| 功耗 (W，可选) | `npu-smi info` |
| 系统内存 RAM / CPU 占用 | `psutil` |

支持 910A / 910B / 310P（含 310P Duo 一卡多芯），多卡时每颗芯片一组指标。

## 安装

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/BenjaminHernandez9794/ComfyuUI_npu_monitor.git
pip install -r ComfyuUI_npu_monitor/requirements.txt
```

重启 ComfyUI 即可。确认运行 ComfyUI 的用户能执行 `npu-smi info`（一般在 `/usr/local/sbin/npu-smi`）。
如果不在 PATH 中，可设置环境变量 `NPU_SMI_PATH=/path/to/npu-smi`。
Docker 里运行时需要把 `npu-smi` 及驱动目录挂载进容器（`/usr/local/sbin/npu-smi`、`/usr/local/Ascend/driver`）。

## 使用

- **拖动**：按住状态栏任意位置（或左侧 `⠿`）拖到任意位置，松开后位置自动保存，刷新页面后保持。
- **复位**：双击 `⠿` 回到右下角。
- **折叠**：点右侧 `–` 折叠/展开。
- **颜色**：绿 < 70%，橙 70–90%，红 ≥ 90%（温度：70°C / 85°C）。
- **设置**：ComfyUI 设置 → 搜索 `NPU Monitor`，可开关状态栏、调整刷新间隔（0.5–10 秒）、选择显示哪些指标、切换横向/竖向布局。

### 节点 `NPU Monitor (Stats)`

位于 `utils/NPU Monitor`。执行时采样一次并输出文本（`STRING`），同时显示在节点上并打印到控制台。
可选输入 `any_input` 可以接任意节点的输出，用来在某一步执行完成时记录资源占用。

## 后端接口

- `GET  /npu_monitor/stats` → JSON（`devices[]`、`system`、`ts`、`error`）
- `POST /npu_monitor/config` `{"interval": 1.0}` → 修改采样间隔

采样在后台线程进行，不阻塞 ComfyUI；没有浏览器在轮询时 15 秒后自动停止采样。
环境变量 `NPU_MONITOR_INTERVAL` 可设置默认采样间隔（秒）。

## 测试

```bash
python tests/test_parser.py
```

## 运行日志（内存 / 显存 / 每个节点耗时）

每次执行工作流都会自动记录一份日志，无需额外节点：

- 点状态栏上的 **☰** 按钮打开「运行日志」面板：总耗时、设备显存峰值、torch 显存峰值、进程内存峰值、AI Core 平均利用率，以及**耗时最长的 10 个节点**（每个节点的耗时、显存峰值、AI Core、内存）。
- 面板里可下载两份 CSV：
  - `节点明细`：每个实际执行的节点一行（开始时间、耗时、torch_npu 峰值已分配/已保留显存、设备显存峰值、AI Core 平均/峰值、进程内存峰值与增量）。
  - `时间线`：执行期间每 0.5 秒一行（当前节点、设备显存、AI Core、温度、功耗、torch 已分配/已保留、ComfyUI 进程 RSS、系统/容器内存、CPU）。
- 文件同时保存在服务器 `ComfyUI/output/npu_monitor_logs/`，默认保留最近 50 次。

环境变量：`NPU_MONITOR_LOG=0` 关闭记录；`NPU_MONITOR_LOG_INTERVAL` 采样间隔（秒，默认 0.5）；`NPU_MONITOR_MAX_RUNS` 保留次数。

说明：「设备显存」来自 npu-smi（整卡占用，含其他进程）；「torch 峰值」来自 `torch.npu.max_memory_reserved`，是本进程在该节点内的真实峰值，比 1 秒一次的采样更准。
