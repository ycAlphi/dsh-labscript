# dsh-labscript（中文说明）

在 DeepSeek Harness（DSH）里控制 labscript-suite 实验。只用 stock 接口：
runmanager remote（面板 globals、n_shots、engage、abort、run_shots、
error_in_globals）+ shot HDF5。不需要改 BLACS/lyse；实验室私有的分析、协议、
hard limits 全部通过配置接入。

## 工具（9 个）

| 工具 | 作用 |
|---|---|
| labs_doctor | 只读诊断：python、numpy/h5py、labscript/runmanager、路径、审批、版本 |
| labs_status | 读面板：labscript、globals、n_shots、error_in_globals、run_shots、pending 进度 |
| labs_set | 写面板（标量 / raw 表达式 / reps 可一次混用），写前全量快照；不运行 |
| labs_engage | 提交当前面板，立即返回 n_shots_at_submit 与 expected_n_shots |
| labs_wait | 等待 pending run（可超时）；若是协议则收尾：postprocess → finish 写回 → restore |
| labs_abort | 中止当前提交（不排队，不会被 labs_wait 堵住） |
| labs_data | 读 run 目录 HDF5：首发 globals、按扫描轴的逐发表格 |
| labs_protocol | list / show / run YAML 固定操作协议 |
| labs_restore | 用最近一次写前快照恢复面板（默认只恢复该次写过的键） |

配套 runtime skill（labscript）：先看现场、稳定流程优先用协议、提交后可以结束回合、
同一时间只跑一个、不猜面板状态；新装或报配置错误时先跑 labs_doctor。

## 安装与配置

- 要求：DSH >= 0.2、Node ^22.19/24；Python >= 3.9。mock 模式只要 numpy+h5py；
  真机用 labscript-suite 环境。详见 docs/INSTALL.zh.md。
- 默认 backend 是 mock-labscript；真机必须显式配置 backend: labscript。
- runmanager 的 host/port/timeout 可以不填：不填就读 labscript 的 labconfig
  （`[servers]/[ports] runmanager`，端口默认 42523；`[timeouts] communication_timeout`,
  默认 60 s），和 runmanager 自己的客户端一致；填了就以配置为准。插件页留空即走 labconfig。
- 安装后在 DSH 插件页填 backend、python、runmanager host/port、protocolsDir；
  其余高级配置（scriptsFile、hard limits、postprocess、approval、pathMap、
  engage 的超时/早退参数）写在 profile 的 cordis.patch.yml。

## 协议 YAML

    name: demo_scan
    labscript: /path/to/sequence.py     # 或 joint_script: 登记名
    set: { do_acquire: true, amplitude: 0.5 }
    scan: { duration_test_us: np.linspace(0, 8.0, 5) }   # 表达式/数字/列表
    reps: 2
    postprocess: demo.peaks              # 全局配置一次，协议里只写名字
    finish:
      restore: true
      panel:  { duration_test_us: fit.center }
      history: { demo.peaks.center_us: fit.center }

postprocess 是后处理：跑完后调用配置的 Python 函数（entry + pythonPaths），
返回 dict 供 finish 写回。旧协议里的 analysis: 字段当作 postprocess 别名。

## 边界

- 真机 engage / protocol run 会请求 DSH 审批（一次），abort 和面板写入不请求；
  插件不是硬件联锁。
- labscript abort 只停止编译/提交新的 shot，已进 BLACS 队列或正在跑的 shot 不会被停。
- 完成判定基于 HDF5 数量、大小稳定和最后一发可读。shot 的 h5 文件在编译阶段就写了出来，
  所以 labs_status 分别报 h5（文件数）和 executed（真正采到数据的枪数），完成要求最后一发
  有采集数据；BLACS paused 会报 compiled_not_executed 诊断。
- 编译 abort 时 labs_wait 不再等满 timeout：距今没有任何新 shot 超过 engage.noShotS
  （默认 25 s）就返回 no_shots / compile_stalled 诊断，去 runmanager 看编译错误；如果只是
  编译慢，再调一次 labs_wait 继续等。
- 一条一枪都没产生的 pending 提交，超过 engage.staleS（默认 180 s）会被下一次 labs_engage
  丢弃并报 dropped_stale_pending，不再永久挡住提交；RUN_PENDING 的错误里也会写清年龄和两种出路。
- 配了 pathMap 时 pending 记录映射后的目录（原始路径保留为 remote_run_folder），所以
  labs_wait 传映射后目录或原始目录都能对上，不会再说"run folder does not exist"。
- **shot 数以面板 `n_shots` 为准**。工具报的 scan axes / n_shots_from_arrays 只是猜测：
  runmanager 会把 list/array 展开成轴，但 **tuple 不展开**，而且 expansion 被设成 `none` 时
  数组就不再是轴——这两件事从 remote 接口都看不到。两个数字不一致时按 `n_shots` 报，并说明差别。
- `labs_status` 会显示 `shuffle`。shuffle 打开时 shot 顺序不等于轴顺序，每发参数要从该发 h5 的
  globals 读，不要按序号推。
- 编译报错只出现在 runmanager 的 output tab：编译器子进程把回溯写到自己的 stderr，runmanager
  把它转到这个 Qt 界面上（内存里，不落盘），`runmanager.remote` 没有任何读取接口。所以插件只能报
  "n 秒内没有出现 shot 文件"，并请操作员贴 output tab，不猜原因。
- shot 文件在 sequence 开始时就写出来了；`images/`、`results/` 是执行后才写，`n_executed`
  数的就是这个。BLACS 可能因为连接表不是实验室连接表的子集而拒收、或设备编程超时把 shot 放回队首，
  BLACS 的 Repeat all / Repeat last 会把跑完的 shot 重跑而不新增文件——这些插件都看不到。

