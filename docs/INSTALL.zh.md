# 安装（DSH desktop profile）

## 0. 环境要求

- DSH >= 0.2，Node.js ^22.19 或 >= 24。
- Python >= 3.9。
  - mock 模式：只需 numpy >= 1.23、h5py >= 3.8；
  - 真机模式：用 labscript-suite 的 Python 环境（labscript、runmanager、
    labscript_utils、zprocess 可导入）。
  - 开发时实测：Python 3.11.0、numpy 2.2.5、h5py 3.13.0。

## 1. 安装插件

### 方式 A：从 npm 安装（推荐）

在 DSH desktop profile 的 `package.json` 依赖里加：

    "dsh-labscript": "^0.2.0"

在 profile 目录执行 `pnpm install`，重启 DSH。升级时改版本号再 `pnpm install`。

### 方式 B：本地 link（开发用）

把仓库放到本机任意目录（下称 `<repo>`），在 DSH desktop profile 里 link 安装：

1. 编辑 `%USERPROFILE%\.dsh\profiles\desktop\package.json`：

       "dependencies": {
         ...,
         "dsh-labscript": "link:<repo>"
       }

   并在 `dsh.profile.bundles` 里加入 `"dsh-labscript"`。

2. 在 profile 目录执行 `pnpm install`，重启 DSH。
3. 新会话里应出现 9 个 `labs_*` 工具和 `labscript` skill。

实验机建议固定 tag：`git checkout v0.2.0` 后再安装，避免 main 变动影响硬件行为。

## 2. 配置

优先在 DSH 里配置：侧边栏 → 插件 → dsh-labscript，填 backend、python、
runmanager host/port、protocolsDir，保存后下一次工具调用生效（不用重启）。

高级配置（scriptsFile、hard limits、postprocess、approval、pathMap、超时等）
仍写在 profile 的 `cordis.patch.yml`：

    - id: dsh-labscript
      name: dsh-labscript
      config:
        scriptsFile: <path>/scripts.json
        hardLimitsFile: <path>/hard_limits.json
        postprocess:
          entry: my_lab.analysis:analyze
          pythonPaths: [<path>/my_lab]
        approval: { engage: true, protocolRun: true }

## 3. 冒烟顺序

1. `labs_doctor`：确认 python/依赖/路径/版本身份；
2. mock：`labs_status` → `labs_protocol list` → `labs_protocol run demo_scan` → `labs_wait`；
3. 真机：只读 `labs_status` → 单键 `labs_set` + `labs_restore` → ≤10 shots →
   `labs_data` → 一条协议。

