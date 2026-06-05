<div align="center">

<h1>Claw-Anything：看见一切，做到一切·。</h1>

[![arXiv](https://img.shields.io/badge/Arxiv-2605.26086-b31b1b.svg?logo=arXiv)](https://arxiv.org/pdf/2605.26086)
[![Dataset](https://img.shields.io/badge/🤗%20Dataset-Claw--Anything-yellow.svg)](https://huggingface.co/datasets/LiberCoders/Claw-Anything)
[![ModelScope](https://img.shields.io/badge/ModelScope-MyRepo-624aff?logo=modelscope)](https://modelscope.cn/datasets/LiberCoders/Claw-Anything)
[![Benchmark](https://img.shields.io/badge/Benchmark-200%20-success.svg)](benchmark/)
[![Environments](https://img.shields.io/badge/Environments-2%2C000%20-blueviolet.svg)](#-quick-start)
[![Views](https://komarev.com/ghpvc/?username=LiberCoders-CLaw-Anything&label=Views&color=brightgreen&style=flat)](https://github.com/LiberCoders/CLaw-Anything)

[English](README.md) | [中文](README.zh.md)

<img src="assets/claw-anything-logo.png" width="260" alt="Claw-Anything logo">

</div>

本仓库是论文 [Claw-Anything: Benchmarking Always-On Personal Assistants with Broader Access to the User's Digital World](https://arxiv.org/pdf/2605.26086) 及其后续工作的官方实现。

> [!IMPORTANT]
> _我们认为，常驻型（always-on）LLM 智能体的下一次飞跃，在于扩展智能体的上下文 —— 拓宽助手能够持续感知、推理并执行操作的用户数字世界的范围。_

Claw-Anything 将这一理念落地，沿着三个真实世界上下文维度评估常驻型 LLM 智能体：**长周期事件流**、各种**相互关联的服务**，以及**跨设备交互**（例如 GUI 与 CLI）。即便最强模型 GPT-5.5，pass@1 也仅达到 **34.5%**，暴露出显著的能力差距。除评测基准外，我们还发布了一个**自动化数据生成流水线**，可产出 **2,000 个训练环境**，并将基线模型提升 **23.7%**。

> <div align="center">
>
> Claw-Anything：评测能够更广泛访问用户数字世界的常驻型个人助手
>
> [Yusong Lin](https://github.com/icexiaoche)、[Xinyuan Liang](https://github.com/xuan112358)、[Haiyang Wang](https://haiyang-w.github.io/)<sup>†</sup>、[Qipeng Gu](https://openreview.net/profile?id=~Qipeng_Gu2)、[Siqi Cheng](https://openreview.net/profile?id=~Siqi_Cheng3)<br>
> [Jiangui Chen](https://chriskuei.github.io/)、[Shuzhe Wu](https://scholar.google.com/citations?user=CkqRXikAAAAJ&hl=en)、[Feiyang Pan](https://feiyang.github.io/)、[Lue Fan](https://lue.fan/)、[Sanyuan Zhao](https://scholar.google.com/citations?user=t7dAaE8AAAAJ&hl=zh-CN)<sup>†</sup>、[Dandan Tu](https://scholar.google.com/citations?user=nf8bdFYAAAAJ&hl=zh-CN)<sup>†</sup>
>
><sup>†</sup> 通讯作者。
>
> 主要联系人：Yusong Lin (linyusong4@huawei.com)，Haiyang Wang (haiyang.wang@huawei.com)
> </div>
>

<div align="center">

<img src="assets/demo_merge_2.png" width="92%" alt="Claw-Anything overview">

</div>


## 新闻
- 🛠️ [2026-06-01] 支持 CLI + GUI 任务自动化评测。
- 📄 [2026-05-26] [arXiv](https://arxiv.org/pdf/2605.26086) 预印本已发布。
- 🚀 [2026-05-26] 数据流水线已发布 —— 两阶段 `build-persona` → `gen-eval` 流程可扩展至 2,000 个训练环境，为评测基准提供数据生成能力。
- 📊 [2026-05-26] 评测基准与训练环境已发布。

## 目录

- [项目概览](#-项目概览)
- [上下文的三个维度](#-上下文的三个维度)
- [架构](#-架构)
- [安装](#-安装)
- [快速上手](#-快速上手)
  - [运行 benchmark 套件](#运行-benchmark-套件)
  - [运行单个任务](#运行单个任务)
  - [生成你自己的任务](#生成你自己的任务)
  - [跑 CLI + GUI 任务](#跑-cli--gui-任务)
- [从零开始的完整 GUI 评测流程](#-从零开始的完整-gui-评测流程)
- [Benchmark 数据](#-benchmark-数据)
- [命令行速查](#-命令行速查)
- [代码结构](#-代码结构)
- [编写任务](#-编写任务)
- [引用](#-引用)
- [License](#-license)


## 💡 项目概览

**Claw-Anything** 用同一套代码同时做两件事：

1. **评测**：在贴近真实"全天候个人助理"场景下评测智能体 —— 长时段活动历史、数十个相互依赖的后端服务、跨设备的 GUI+CLI 协同。
2. **生成**：从一份 persona 描述自动生成上述任务 —— 自动模拟数月的用户活动、持久化 fixture、可执行的 grader，并刻意引入噪声（无关事件 / 冲突信号）。

执行评测的模拟器同时也是生产数据的引擎，把"基准评测"与"数据集构造"合到同一个工具链。

| 模块 | 作用 |
|------|------|
| 🧪 **[`benchmark/`](benchmark/)** | **评测** —— 200 个人工验证任务，分为 `skill/`（智能体按需动态加载工具）和 `tool/`（智能体预加载完整工具集） |
| 🏗️ **[`gen/`](src/claw_anything/gen/)** | **构建数据** —— `build-persona` + `gen-eval` 两阶段流水线；规模化生成 2,000 个训练环境 |
| 🤖 **[`runner/`](src/claw_anything/runner/)** | **执行** —— Think → Act → Observe 循环，OpenAI 兼容的模型后端，按 trial 隔离端口的 Docker 沙盒 |
| 📋 **[`graders/`](src/claw_anything/graders/)** | **评分** —— 多维度评分（完成度·鲁棒性·沟通·安全）+ LLM-as-judge + Pass^k 聚合 |
| 🛠️ **[`mock_services/`](mock_services/)** | **模拟** —— 35 个 FastAPI 模拟服务（Gmail、Calendar、Slack、Notion、Feishu、WeChat、Zotero……），共享统一的时间冻结环境数据 |


## 🔭 上下文的三个维度

现有评测往往只暴露用户状态的一小片。Claw-Anything 沿三个维度同时扩展智能体的上下文：

<div align="center">
<img src="assets/Figure1.png" width="92%" alt="上下文的三个维度">
</div>

- **长时段事件流** —— 数月级的细粒度活动记录，串起过去与现在，支持基于演进上下文的推理。
- **相互依赖的后端服务** —— 任务需要跨服务协调，而不是单一 API 调用。
- **跨设备 GUI + CLI** —— 异质交互界面让智能体整合分布式信息和动作。

扩展后的上下文范围还解锁了对**主动协助（proactive assistance）** 的评测：奖励在用户显式请求**之前**就采取行动的智能体。


## 🏗️ 架构

<div align="center">
<img src="assets/Figure2_v3.png" width="95%" alt="Claw-Anything 架构与数据生成管线">
</div>

**左侧 —— 环境**：相互连接的设备产生系统级事件流；多个服务持有持久状态与各自的历史。

**右侧 —— 自动化数据管线**：从一个 persona 锚定的初始状态出发，迭代采样任务模板与噪声模板，由 LLM 模拟器更新事件与世界状态。最终一轮模拟产出任务问题、参考解和 grader；自动过滤后得到任务实例，benchmark 任务再走一道人工核验。


## 📦 安装

需要 **Python 3.11+**，开 trial-in-container 沙箱时额外需要 **Docker**。本项目用 [**uv**](https://github.com/astral-sh/uv) 管理依赖。

```bash
# 1. 一次性安装 uv（已装可跳过）
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. clone 仓库并进入包目录
git clone https://github.com/LiberCoders/CLaw-Anything.git
cd CLaw-Anything

# 3. 创建虚拟环境并安装依赖
uv venv --python 3.11
source .venv/bin/activate
uv pip install -e ".[mock,sandbox]"

# 4. 配置模型 endpoint
cp config.example.yaml config.yaml
# 编辑 config.yaml：api_key / base_url / model_id

# 5. 构建 trial-in-container 镜像（一次性；按使用的 agent backend 选）
claw-anything build-image                       # 默认 --agent openharness-ext（镜像 claw-anything-oh-ext）
claw-anything build-image --agent loop          # 最精简镜像：claw-anything-loop
claw-anything build-image --agent openharness   # vanilla OH 镜像：claw-anything-oh
```

> 构建 OH-Ext 镜像需要 `adb` 二进制和 [OpenHarnessExtended](https://github.com/LiberCoders/OpenHarnessExtended) 源码。要么让脚本把 OH-Ext clone 到 `vendor/`、仅提供 `ADB_PATH`，要么两者都显式指定：
> ```bash
> OH_EXT_DIR=$HOME/code/OpenHarnessExtended \
> ADB_PATH=$HOME/android-sdk/platform-tools/adb \
>   scripts/build_oh_ext_image.sh
> ```
> 镜像期望 OH-Ext 工作副本位于 **`main-clawgui`** 分支 —— 不在该分支时构建脚本会打印 WARNING。OH settings 样例文件：[`examples/oh-settings.example.json`](examples/oh-settings.example.json)（复制后填好 `api_key`、`base_url` 等字段后即可使用）。

**可选 extras**（定义在 `pyproject.toml`）：

| Extra | 何时安装 | 引入的依赖 |
|-------|---------|-----------|
| `mock` | **必装** —— 所有 `run` / `batch` / `gen-*` 命令都需要 | `fastapi`、`uvicorn`、`pypdf`、`trafilatura`、`requests` |
| `sandbox` | **建议** —— `--trial-in-container` 必需 | `docker` |
| `openharness` | 可选 —— 仅在使用 `agent_type: openharness` 或 `openharness-ext` 时需要 | `openharness-ai` |
| `dev` | 可选 —— 仅在跑 `pytest tests/` 时需要 | `pytest` |

所以**典型安装命令**就是 `uv pip install -e ".[mock,sandbox]"`。要跑测试套件再加 `,dev`；要用 OH agent 后端再加 `,openharness`。

> 装完后既可以 `source .venv/bin/activate` 然后直接用 `claw-anything ...`，也可以用 `uv run claw-anything ...` 让 uv 自动接管环境。


## 🚀 快速上手

### 运行 benchmark 套件

Benchmark 拆成三个子集。`claw-anything batch` 不带 `--tasks-dir` 时默认跑**完整 200 任务套件**：

- `skill`（100 个，CLI，`prompt.skill_mode = true`）
- `tool`（50 个，CLI，`prompt.skill_mode = false`）
- `gui`（50 个，Android GUI，强制 `openharness-ext` —— 需要模拟器 + `--oh-settings`；见[跑 CLI + GUI 任务](#跑-cli--gui-任务)）

各子集写到独立的 trace 子目录。加 `--cli-only` 只跑 CLI 两个子集（150 任务）。注意 `batch` 始终在容器里跑 trial —— 这里没有 `--trial-in-container` flag（只有 `run` 有）。

```bash
# 完整 benchmark（200 任务：skill + tool + gui）
claw-anything batch \
  --config config.yaml \
  --oh-settings /path/to/oh-settings.json \
  --trials 3 \
  --parallel 10

# 只跑 CLI 子集（150 任务：skill + tool）
claw-anything batch \
  --config config.yaml \
  --cli-only \
  --trials 3 \
  --parallel 10
```

输出结构：

```
traces/loop_<model>_<ts>/
├── skill/  # benchmark/skill, prompt.skill_mode = true
│   ├── batch_results.json
│   └── batch_summary.json
├── tool/   # benchmark/tool,  prompt.skill_mode = false
│   ├── batch_results.json
│   └── batch_summary.json
└── gui/    # benchmark/gui,   强制 openharness-ext（加 --cli-only 时跳过）
    ├── batch_results.json
    └── batch_summary.json
```

也可以单独跑某个子集：

```bash
claw-anything batch --tasks-dir benchmark/skill --config config.yaml --trials 3 --parallel 10
claw-anything batch --tasks-dir benchmark/tool  --config config.yaml --trials 3 --parallel 10
claw-anything batch --tasks-dir benchmark/gui   --config config.yaml --agent openharness-ext --oh-settings /path/to/oh-settings.json --trials 3 --parallel 10
```

如果要在之前的某次 batch 上**续跑或修复**，指向旧 trace 目录：

```bash
claw-anything batch --tasks-dir benchmark/skill --trace-dir traces/<prev_run>/ --continue       # 跳过已完成
claw-anything batch --tasks-dir benchmark/skill --trace-dir traces/<prev_run>/ --rerun-errors   # 仅重跑失败
```

### 运行单个任务

```bash
# Loop agent —— 不开容器（mock 服务在本机起）
claw-anything run --task examples/ready_to_run/T001_demo --config config.yaml

# Loop agent —— 容器内运行（trial-in-container）
claw-anything run --task examples/ready_to_run/T001_demo --config config.yaml --trial-in-container

# OpenHarness agent（vanilla，容器内）
# 先构建镜像（一次性）：scripts/build_oh_image.sh
claw-anything run \
  --task examples/ready_to_run/T001_demo \
  --config config.yaml \
  --agent openharness \
  --trial-in-container \
  --oh-settings /path/to/oh-settings.json

# OpenHarness-Ext agent（GUI/手机任务，容器内）
# 先构建镜像（一次性）：scripts/build_oh_ext_image.sh
claw-anything run \
  --task examples/ready_to_run/T001_demo \
  --config config.yaml \
  --agent openharness-ext \
  --trial-in-container \
  --oh-settings /path/to/oh-settings.json

# 用任务定义重评一个已存在的 trace
claw-anything grade --trace traces/<dir>/<trace>.jsonl --task examples/ready_to_run/T001_demo
```

### 生成你自己的任务

两阶段管线把一份 persona YAML 变成完整的数字世界 + 可执行 grader 的评测任务。

```bash
# Phase 1 —— 为 persona 构建 gold environment
claw-anything build-persona \
  --persona personas/sarah_chen_pm_persona.yaml \
  --seed-tasks seed_tasks/ \
  --rounds 30 \
  --seed-noise seed_noise/ \
  --noise-ratio 2 \
  --output gold_envs/sarah_chen_pm/ \
  --config config.yaml

# Phase 2 —— 从 gold environment 生成评测任务
claw-anything gen-eval \
  --env gold_envs/sarah_chen_pm/ \
  --seed-tasks seed_tasks/ \
  --output gen_tasks/sarah_chen_pm_simple/ \
  --max-tasks 20 \
  --difficulty simple \
  --execution-date 2026-04-03 \
  --config config.yaml

# 评测生成出来的任务
claw-anything batch \
  --tasks-dir gen_tasks/sarah_chen_pm_simple/ \
  --config config.yaml \
  --trials 3 --parallel 10
```

### 跑 mobile GUI / Android 任务

凡是 `task.yaml` 里声明了 `task_env: [mobile_gui]` 的任务，都要通过 `adb` 驱动一台真实 Android 设备。必须用 OH-Ext agent 和镜像。设备可以来自**两种自动拉起后端**之一——按你主机内核的能力来选：

| 主机能力 | `android.backend` | 设备镜像 |
|---|---|---|
| **/dev/kvm**（硬件虚拟化） | `kvm`（默认） | `claw_anything:latest` |
| **binder**（`binder_linux` 模块，无需 KVM） | `redroid` | `redroid-claw_anything:13` |

两者都是已 root 的 Android，预装了所有注入目标，任务和 grader 完全一致——只是设备来源不同。完整流程见下方 [从零开始的完整 GUI 评测流程](#-从零开始的完整-gui-评测流程)。简版：

```bash
# config.yaml 里二选一——要么列出已经起好的设备序列号：
# android:
#   emulator_pool:
#     - emulator-5554
#     - 127.0.0.1:5555      # TCP 形式的序列号会在 trial 前 `adb connect`
#
# 要么让框架在每次 run/batch 时自动拉起设备容器：
# android:
#   backend: kvm            # 'kvm'（默认）或 'redroid'——按主机选（见上表）
#   auto_launch_count: 1    # >0 ⇒ 起 N 个设备容器，分发，结束后回收

claw-anything run \
  --task gen_tasks/<mobile_gui_task>/ \
  --config config.yaml \
  --agent openharness-ext \
  --trial-in-container \
  --oh-settings /path/to/oh-settings.json
```

主机端会先调用 `init_gui_task()` 把日历事件、联系人等注入到设备，然后 trial 容器里跑 OH-Ext agent 与已经准备好的设备交互。


## 🤖 从零开始的完整 GUI 评测流程

本节是在一台干净机器上从头评测 **CLI + GUI** 基准的完整食谱——需要什么硬件、怎么把 Android 设备（KVM 模拟器**或** redroid）跑起来、怎么接 `adb`、以及一个 GUI 任务需要的两个模型端点怎么配。纯 CLI 评测可跳过大部分内容（直接看[第 6 步](#6-跑评测)）。

### GUI 运行需要的镜像

一次 GUI trial 依赖**两个** docker 镜像：

| 镜像 | 怎么获取 |
|---|---|
| **设备镜像** | **redroid**（无需 KVM，推荐）：`docker pull ghcr.io/libercoders/redroid-claw_anything:13`;                              **kvm**：`docker pull ghcr.io/libercoders/claw_anything:latest` |
| **OH-Ext 运行器** `claw-anything-oh-ext` | 本地构建：`claw-anything build-image --agent openharness-ext`（详见[第 4 步](#4-构建-oh-ext-runner-镜像)） |

### 架构：谁在跟谁通信

一次 GUI trial 有**四个部件**：

```
┌─────────────────────────────────────────────────────────────────────┐
│ 宿主机                                                                │
│                                                                       │
│  claw-anything CLI ──┬── 设备池 ────────▶ 设备容器                     │
│  （编排器）          │   （自动拉起）     kvm:     claw_anything        │
│                      │                    redroid: redroid-claw…       │
│                      │                          ▲                     │
│                      └── trial 容器 ──────adb──┘                      │
│                          (claw-anything-oh-ext)                       │
│                            │         │                                │
│                     planner LLM   GUI grounding LLM                   │
│                    （OpenAI API） （GUI-Owl，视觉）                   │
└─────────────────────────────────────────────────────────────────────┘
```

1. **编排器** —— 宿主机上的 `claw-anything` CLI。负责 GUI 状态注入、workspace 准备、config 改写、评分。
2. **设备** —— 一台已 root 的 Android。要么你自己预先起好（`emulator_pool`），要么框架替你在容器里自动拉起（`auto_launch_count`）。后端（`android.backend`）选 **`kvm`**（QEMU AVD，`claw_anything:latest`）或 **`redroid`**（容器内 Android，`redroid-claw_anything`）。
3. **trial runner** —— 跑 OH-Ext agent、通过 `adb` 驱动设备的 `claw-anything-oh-ext` 容器。
4. **两个模型端点**，都写在 `--oh-settings` 里：
   - **planner** —— 一个 OpenAI 兼容的对话模型（agent 的"大脑"）。
   - **GUI grounding** —— 把截图转成点击/滑动坐标的视觉模型。标准选择是 **GUI-Owl**（`gui_plus` 后端）。

### 1. 硬件与宿主机前置

按主机内核的能力**二选一** Android 后端：

| 后端 | 主机要求 | 为什么 | 检查命令 |
|---|---|---|---|
| **`kvm`**（默认） | `/dev/kvm` 存在、CPU 有 `vmx`/`svm` | QEMU Android 模拟器需要硬件虚拟化；没有它 AVD 在合理时间内根本启动不完 | `ls /dev/kvm && egrep -c '(vmx\|svm)' /proc/cpuinfo` |
| **`redroid`** | 加载了 binder 内核模块（`binder_linux`）；**无需 KVM** | redroid 直接在主机内核上跑 Android（binder/ashmem），所以在无硬件虚拟化的机器（多数云 CI、开发笔记本）上也能用 | `grep -w binder /proc/filesystems \|\| lsmod \| grep binder` |

> **选哪个后端？** 有 `/dev/kvm` 就用 `kvm`（默认）；没有但主机带 binder 模块就用 `redroid`——任务和 grader 都一样，只是设备更轻、秒级启动。

### 2. 安装 adb

OH-Ext **镜像里已经带了 `adb`**（在 `/usr/local/bin/adb`），所以 trial 容器无需额外安装。只有**宿主机上**需要装 `adb`——因为主机要直接跑 `init_gui_task` 注入（以及设备池的启动探测）：

```bash
# Android platform-tools（提供 adb）
wget https://dl.google.com/android/repository/platform-tools-latest-linux.zip
unzip platform-tools-latest-linux.zip
export PATH="$PWD/platform-tools:$PATH"
adb version    # → Android Debug Bridge version 1.0.41
```

### 3. 获取设备镜像

两种安卓镜像都预装了所有目标应用（Fossify 日历/短信/笔记、Loop Habits、My Expenses、Markor、OpenTracks、Gmail……），任务和 grader 对两者完全一致。按你的 `android.backend` 只拉对应的那一个：

**`backend: kvm`** → `claw_anything:latest`

```bash
docker pull ghcr.io/libercoders/claw_anything:latest
```

**`backend: redroid`** → `redroid-claw_anything`

```bash
docker pull ghcr.io/libercoders/redroid-claw_anything:13
```

### 4. 构建 OH-Ext runner 镜像

该镜像**未发布到 registry**——需本地构建（一次性）。它需要 [OpenHarnessExtended](https://github.com/LiberCoders/OpenHarnessExtended) 源码（分支 `main-clawgui`）+ 一个 `adb` 二进制打进镜像。`OH_EXT_DIR` 不设时构建会**自动** clone OH-Ext 到 `vendor/`，所以你唯一必须提供的是 `ADB_PATH`——指向[第 2 步](#2-安装-adb)下载的**官方** platform-tools `adb`（自包含二进制；发行版那种链接 `libadb.so.0` 的 adb 在 slim 镜像里会失败）：

```bash
# 最简——让 CLI 驱动构建脚本（环境变量会被继承）：
ADB_PATH=$PWD/platform-tools/adb \
  claw-anything build-image --agent openharness-ext   # → claw-anything-oh-ext:latest

# 或直接调脚本以获得完全控制（例如已 clone 好的 OH-Ext）：
OH_EXT_DIR=$HOME/code/OpenHarnessExtended \
ADB_PATH=$PWD/platform-tools/adb \
  scripts/build_oh_ext_image.sh
```

### 5. 配置 `config.yaml` 和 `oh-settings.json`

**`config.yaml`** —— 编排器配置。这里的 `model` / `judge` 块用于 **loop** agent 和 **LLM-judge 评分器**；OH-Ext agent 会忽略 `model`（它读自己的 `--oh-settings`）。加一个 `android` 块来启用自动拉起：

```yaml
model:                       # 给 loop agent 用 + （仅 model_id）用于 trace 目录命名
  api_key: ${OPENAI_API_KEY}
  base_url: https://api.openai.com/v1
  model_id: gpt-4o-mini

judge:                       # 通信质量评分用的 LLM-as-judge
  api_key: ${OPENAI_API_KEY} # ⚠️ 这里 401 只会让 judge 评分失效；规则维度照常打分
  base_url: https://api.openai.com/v1
  model_id: gpt-4o-mini
  enabled: true

agent:
  agent_type: loop           # CLI 默认；GUI 运行在命令行上覆盖成 openharness-ext

# ── Android 设备——二选一后端块 ──

# (a) KVM 模拟器（默认；需要 /dev/kvm）
android:
  backend: kvm
  emulator_image: claw_anything:latest
  auto_launch_count: 1       # >0 ⇒ 框架起 N 个设备容器并在结束时回收
  container_adb_port: 5556   # 该镜像的 adb 监听 5556（不是上游的 5555）
  host_port_start: 5556      # 主机端口分配的下界（实际端口动态分配）
  boot_timeout_s: 600        # 该镜像首次启动约需 3 分钟

# (b) redroid（无需 KVM；需要主机 binder 模块）
# android:
#   backend: redroid
#   redroid_image: ghcr.io/libercoders/redroid-claw_anything:13
#   auto_launch_count: 1     # 秒级启动；container_adb_port 固定为 5555
#   boot_timeout_s: 300

# 或者完全跳过自动拉起，直接指向一台已经在跑的设备
# （与后端无关——静态池永远优先于自动拉起）：
# android:
#   emulator_pool: ["127.0.0.1:5555"]   # 例如你手动起的一个 redroid 容器
```

**`oh-settings.json`** —— OH-Ext agent 自包含的配置（复制 [`examples/oh-settings.example.json`](examples/oh-settings.example.json)）。**两个模型端点**写在这里。框架会在每个 trial 自动填 `mobile_gui.device_serial`、并在容器模式下把 `localhost`→`host.docker.internal` 改写好，所以你只需提供端点：

```jsonc
{
  "active_profile": "default",
  "api_key": "EMPTY",
  "max_tokens": 8192,
  "mobile_gui": {
    "device_transport": "adb",
    "device_serial": "",                       // ← 每个 trial 自动填；留空
    "adb_path": "/usr/local/bin/adb",          // OH-Ext 镜像里的 adb
    "gui_backend": {                           // ← GUI grounding（视觉）模型
      "type": "gui_plus",
      "base_url": "http://localhost:7267/v1",  // GUI-Owl 端点
      "api_key": "EMPTY",
      "model": "GUI-Owl-1.5-4B-Instruct",
      "tls_verify": false,
      "max_tokens": 2048,
      "history_n": 4
    }
  },
  "profiles": {
    "default": {                               // ← planner（agent 的大脑）
      "label": "planner",
      "provider": "openai",
      "api_format": "openai",
      "auth_source": "openai_api_key",
      "default_model": "your-planner-model",
      "last_model": "your-planner-model",
      "base_url": "http://localhost:7266/v1",  // planner 端点
      "allowed_models": ["your-planner-model"]
    }
  }
}
```

两个端点都必须**能从 trial 容器访问到**——通过 `host.docker.internal`（launcher 会把 `localhost`→`host.docker.internal` 改写，并加 `--add-host=host.docker.internal:host-gateway`）。如果你的模型只绑在 `127.0.0.1`，把它桥接到 docker 网关（例如在 `172.17.0.1:PORT → 127.0.0.1:PORT` 上起一个小 TCP 转发器）。

> 用 vLLM 自部署模型？典型的一对：
> ```bash
> # planner（任意支持工具调用的对话模型）
> vllm serve <planner-model> --served-model-name your-planner-model --port 7266 \
>   --enable-auto-tool-choice --tool-call-parser hermes
> # GUI grounding
> vllm serve GUI-Owl-1.5-4B-Instruct --served-model-name GUI-Owl-1.5-4B-Instruct --port 7267 \
>   --limit-mm-per-prompt '{"image": 5}'
> ```

### 6. 跑评测

```bash
# ── 单个 GUI 任务（冒烟测试）────────────────────────────────────────────
claw-anything run \
  --task benchmark/gui/TGUI01_myexpenses_overbudget_finance_email \
  --config config.yaml \
  --agent openharness-ext \
  --trial-in-container \
  --oh-settings oh-settings.json

# ── 完整 200 任务基准（skill + tool + gui）──────────────────────────────
claw-anything batch \
  --config config.yaml \
  --oh-settings oh-settings.json \
  --trials 3 \
  --parallel 10

# ── 仅 CLI 子集（150 任务；无需模拟器 / oh-settings）────────────────────
claw-anything batch --config config.yaml --cli-only --trials 3 --parallel 10

# ── 仅 GUI 子集（50 任务）───────────────────────────────────────────────
claw-anything batch \
  --tasks-dir benchmark/gui \
  --config config.yaml \
  --agent openharness-ext \
  --oh-settings oh-settings.json \
  --trials 3 --parallel 4         # parallel ≤ android.auto_launch_count（每个 worker 一台设备）
```

批量跑 GUI 时，把 `android.auto_launch_count` 设到至少等于 `--parallel`，保证每个 worker 都有自己的设备。健康的运行会在开头打印设备池启动日志（`kvm` 是 `[emu-pool] booted: …`，`redroid` 是 `[redroid-pool] ready (rooted): …`）、结尾打印 `… stop_all: removed N container(s)`；每个 trial 的分数块会打印 `completion / robustness / communication / safety / task_score / passed`。

### 7. 清理

`claw-anything cleanup` 会同时删掉 trial 容器（`app=claw-anything`）和任何泄漏的设备容器（`app=claw-anything-emu`，`kvm` 与 `redroid` 两个池都用这个 label）。设备池本身在 `finally` 块里就会回收容器，所以只有在硬崩溃 / `Ctrl-C` 之后才需要 cleanup。

```bash
claw-anything cleanup
```

### 排错

| 现象 | 原因 / 解法 |
|---|---|
| `[emu-pool]`（kvm）一直不打印 "booted"、超时 | 没 KVM，或 `boot_timeout_s` 太小。确认 `/dev/kvm`；`claw_anything:latest` 首次启动约 3 分钟。没有 KVM 的机器请改用 `android.backend: redroid`。 |
| `[redroid-pool]` 容器退出 / 起不来 | 主机缺 binder 模块——`grep -w binder /proc/filesystems`，用 `sudo modprobe binder_linux` 加载。容器需要 `--privileged`（池已自动传）。 |
| trial 容器连不上模型 | `base_url` 写的是 `localhost` 但模型只绑了 `127.0.0.1`。把它桥接到 docker 网关 `172.17.0.1`，或让服务绑 `0.0.0.0`。 |
| trial 里 `adb connect` 失败 | 模拟器的 adb 在它容器里绑死 `127.0.0.1`；launcher 期望它在 `host.docker.internal:<port>` 可达。确保主机端口映射（或桥接）把它暴露到 `0.0.0.0`。 |


## 📊 Benchmark 数据

<div align="center">
<img src="assets/distribution.png" width="92%" alt="Benchmark 统计">
</div>

- **200 个人工核验的评测任务**，覆盖巡视、决策、跨服务协调等类别。
- **2,000 个由管线生成的训练环境**，可直接用于下游训练。
- **当前最先进的前沿模型**在"全天候个人助理"任务上仍留有显著提升空间。

完整数字与按模型拆分的结果见 [`paper/`](paper/)。


## 🛠️ 命令行速查

| 分组    | 命令 / 脚本                          | 用途 |
|---------|--------------------------------------|------|
| 运行    | `run`                                | 在单个任务上运行一次 agent（loop：`--trial-in-container`；OH：`--agent openharness[‑ext] --trial-in-container --oh-settings`） |
| 运行    | `batch`                              | 在 `--tasks-dir` 下并行跑 N trials，始终在容器内（没有 `--trial-in-container` flag）。**省略 `--tasks-dir` 时默认跑完整 200 任务套件（skill + tool + gui）**；加 `--cli-only` 只跑 CLI 两个子集（150 任务）。支持对已有 `--trace-dir` 的 `--continue` 与 `--rerun-errors`。 |
| 运行    | `grade`                              | 用任务定义重评一份已存在的 trace JSONL |
| 运行    | `list`                               | 列出 `--tasks-dir` 下所有任务 id |
| 镜像    | `build-image`                        | 为指定 agent 构建容器镜像（`--agent loop\|openharness\|openharness-ext`，默认：`openharness-ext`） |
| 镜像    | `scripts/build_{loop,oh,oh_ext}_image.sh` | 更底层的 shell 构建脚本。`build_oh_ext_image.sh` 需要 `OH_EXT_DIR` 与 `ADB_PATH`。 |
| 容器    | `cleanup`                            | 清理所有 claw-anything trial 容器（label `app=claw-anything`） |
| 数据生成 | `build-persona`                     | **Phase 1** —— 把 seed task 适配到 persona，构建 gold environment |
| 数据生成 | `gen-eval`                          | **Phase 2** —— 从 gold environment 生成评测任务 |

`run` 常用 flag：`--agent {loop, openai-compat, openharness, openharness-ext}` · `--trial-in-container` · `--docker-image`（覆盖镜像名）· `--oh-settings PATH`（OH 专用）· `--oh-disable-builtin-tools`（禁用 OH 内置工具，只暴露 claw-anything 自己的工具）· `--proxy URL`（模型/judge API 走代理）· `--judge-model` / `--no-judge`。

每个命令都可用 `claw-anything <cmd> --help` 查看完整参数。


## 📁 代码结构

```
src/claw_anything/      # 核心包
  ├─ cli.py             # 所有 CLI 子命令
  ├─ runner/            # container_launcher、ServiceManager、dispatcher、OH plugin 生成
  ├─ agents/            # agent 后端（loop · openharness · openharness-ext）
  ├─ task/mobile_gui/   # Android GUI 初始化 + adb 注入工具（日历 / 联系人 / …）
  ├─ graders/           # 评分框架（规则 + LLM judge）
  ├─ gen/               # build-persona + gen-eval 任务生成管线
  ├─ models/            # pydantic 模型（task, message, trace, scoring）
  └─ trace/             # JSONL trace 读写
mock_services/          # FastAPI mock 服务（CLI + GUI 应用镜像）
docker/oh/              # patch_*.py —— 构建期注入 OH 镜像的补丁脚本
                        #   patch_print_mode_usage.py    —— stream-json 中透出每轮 `usage`
                        #   patch_openai_client.py       —— 工具调用时保留 `stream_options.include_usage`
                        #   patch_environment_date.py    —— 让 OH 读取 CLAW_TASK_EXECUTION_DATE 环境变量
scripts/                # build_{loop,oh,oh_ext}_image.sh
Dockerfile.{loop,oh,oh_ext}   # 每个 agent backend 一个 Dockerfile
benchmark/              # 200 个人工核验任务
  ├─ skill/             # 100 个 skill 模式 CLI 任务（智能体按需动态加载工具）
  ├─ tool/              # 50 个 tool 模式 CLI 任务（智能体预加载完整工具集）
  └─ gui/               # 50 个 CLI + GUI 任务
personas/               # 手写 persona YAML（build-persona 的输入）
seed_tasks/             # 抽象任务模板（M000–Mxxx）
seed_noise/             # persona 构建时注入的噪声模板
gold_envs/              # build-persona 的产物（persona + fixtures）
gen_tasks/              # gen-eval 的产物
examples/               # 最小可运行示例 + oh-settings.example.json（OH settings 模板）
template/               # 任务作者用的 task.yaml / grader.py 模板
docs/                   # 任务编写文档
```


## ✍️ 编写任务

- 手写任务：以 `template/task_template.yaml` 与 `template/grader_template.py` 作为起点改写。
- 自动生成任务：建议直接走[两阶段管线](#生成你自己的任务)，而不是手写。

完整流程见 [CONTRIBUTING.md](CONTRIBUTING.md)。欢迎提交 bug 修复、新的 mock 服务、新的 seed task、persona 模板。


## 📝 引用

```bibtex
@article{lin2026clawanything,
  title   = {Claw-Anything: Benchmarking Always-On Personal Assistants with Broader Access to User’s Digital World},
  author  = {Lin, Yusong and Liang, Xinyuan and Wang, Haiyang and Gu, Qipeng and Cheng, Siqi and Chen, Jiangui and Wu, Shuzhe and Pan, Feiyang and Fan, Lue and Zhao, Sanyuan and Tu, Dandan},
  year    = {2026},
  journal = {arXiv preprint arXiv:2605.26086}
}
```


## 📄 License

本项目使用 [MIT License](LICENSE)。
