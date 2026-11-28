<div align="center">

<img src="https://raw.githubusercontent.com/JJJJSTIYYYY/alira/develop/docs/image/alira.png" alt="Alira" height="300">

[![PyPI](https://img.shields.io/pypi/v/alira-harness.svg)](https://pypi.org/project/alira-harness/)
![Python](https://img.shields.io/badge/Python-3.12+-3776AB?style=flat&logo=python)
[![License](https://img.shields.io/badge/License-GPL%203.0-blue?style=flat)](https://github.com/JJJJSTIYYYY/alira/blob/develop/LICENSE)
[![Discord](https://img.shields.io/badge/Discord-Join%20Us-5865F2?style=flat&logo=discord&logoColor=white)](https://discord.gg/tUCQthwK5)
[![QQ群](https://custom-icon-badges.demolab.com/badge/QQ群-639459172-00BFFF?style=flat&logo=tencent-qq)](https://qun.qq.com/universal-share/share?ac=1&authKey=ommoQrT2zhzHU%2FUxv8pfGCJbNifW%2BJyUAFBkNdzkHTPUxdxCnlgxm5aNgGslTmdE&busi_data=eyJncm91cENvZGUiOiI2Mzk0NTkxNzIiLCJ0b2tlbiI6Im9ZZkdNUWZnSVV1Y2REeUhKNnlTbWEwc05Bb093djRzUXdXNE55dklBVnlBQk9XbGNpS0ZXSDlzK3orSW1sQ3YiLCJ1aW4iOiIzMTI5NDI0NTcyIn0%3D&data=OGTchcr80RAQg8Z8_GZTdvBb7kZDeM9B3hHcNqLaAX2ZK_KYq260C4CubblEBT1bK5fP6zgtnCk2D8fIoph1ZQ&svctype=4&tempid=h5_group_info)

[快速开始](https://github.com/JJJJSTIYYYY/alira/blob/develop/README.md#快速开始) · [模块文档](https://github.com/JJJJSTIYYYY/alira/blob/develop/docs/README.md) · [问题反馈](https://github.com/JJJJSTIYYYY/alira/issues)

</div>

Alira 为构建 AI Agent 应用提供模型调用、上下文管理、工具接入与消息存储能力。
通过模块组合所需功能，让同一套运行时适应不同的应用场景。

项目处于早期开发阶段，接口与功能仍在演进。

## 主要能力

- **模块化运行**：按需选择启动模块，自动加载前置模块，依赖通过声明和锁文件管理。
- **Agent 编排**：基于 [Apixis](https://github.com/JJJJSTIYYYY/apixis) 的事件与图执行能力，组织 Agent 的执行流程。
- **工具接入**：支持函数工具与 MCP，扩展 Agent 可调用的能力。
- **消息与存储**：提供上下文管理、消息持久化与可选的 SQLite 向量检索。

## 快速开始

需要 Python 3.12+ 和 [uv](https://docs.astral.sh/uv/)。从源码启动：

```bash
git clone https://github.com/JJJJSTIYYYY/alira.git
cd alira
uv sync --locked
uv run alira add web
uv run alira run web
```

默认可访问 <http://localhost:2712/health> 检查服务状态。
启动前默认检查锁文件并补齐环境；使用 `alira run --no-sync` 可跳过同步。

PyPI 发行包名为 `alira-harness`，Python 导入名和命令均为 `alira`。

## 常用命令

| 命令 | 用途 |
| --- | --- |
| `uv run alira run` | 只启动内核 |
| `uv run alira --help` | 查看子命令 |
| `uv run alira run --help` | 查看模块位置参数与启动选项 |
| `uv run alira init example` | 在当前 Alira 模块目录创建最小源码骨架，不安装依赖 |
| `uv run alira list` | 列出模块名和版本，仅导入声明入口，不启动模块或读取依赖记录 |
| `uv run alira lock` | 解析已声明模块并写锁，不安装 Python 包 |
| `uv run alira lock --check` | 只检查锁是否最新，适用于 CI |
| `uv run alira sync` | 按锁同步已声明模块，必要时重锁，并清理多余 Python 包 |
| `uv run alira sync --locked` | 锁过期时报错，保持声明和锁不变 |
| `uv run alira sync --frozen` | 不检查声明，直接按已有锁安装 |
| `uv run alira add web` | 声明 web 及前置模块、写锁并安装依赖 |
| `uv run alira add web --no-sync` | 只更新声明和锁 |
| `uv run alira add ./example.zip` | 发布模块源码并声明、锁定、安装依赖 |
| `uv run alira add https://github.com/owner/repo --ref v1.0.0` | 从 GitHub 安装模块及依赖 |
| `uv run alira add --manifest package.yml` | 批量添加清单中的源码 |
| `uv run alira remove web` | 移除声明并释放独占依赖，保留源码；后续 sync 不会重新添加 |
| `uv run alira remove web --purge` | 同时将源码送入系统回收站，与 uninstall 等价 |
| `uv run alira uninstall example` | 清理依赖并将源码送入系统回收站 |
| `uv run alira run web` | 默认检查并同步环境，启动 web 和前置模块 |
| `uv run alira run web --no-sync` | 跳过依赖同步和检查，直接启动 |
| `uv run alira run web --locked` | 要求锁最新，允许补齐缺失包 |
| `uv run alira run web --frozen` | 使用已有锁并补齐环境，跳过声明新鲜度检查 |
| `uv run alira tree` | 显示模块及其 Python 包依赖 |
| `uv run alira migrate` | 将旧全局状态迁入当前源码根的独立状态目录 |

`install` 保留为 `add` 的源码安装别名，支持 `--package FILE` / `-r FILE`。
旧 `--no-install` 改用 `add --no-sync` 或独立的 `lock`；启动安装选项改用 `run --sync`。

依赖命令必须从包含 `src/alira/modules/` 的源码 checkout 运行，或设置 `ALIRA_MODULES_DIR`。
源码留在模块根；声明 `modules.json` 和锁 `modules.lock.json` 保存在
`~/.alira/projects/<sha256(realpath(modules_root))[:16]>/`，根目录固定且不可配置。
各 checkout 状态独立；Python 包仍安装到运行命令的解释器，建议每个 checkout 使用独立虚拟环境。
`alira sync` 清理时保留项目 `pyproject.toml` 中 `[dependency-groups].dev` 已安装的包及其传递依赖，
包括引用的依赖组和 extras；缺失的开发依赖仍通过 `uv sync --locked --inexact` 安装。
`remove --purge` 和 `uninstall` 优先将源码送入回收站；回收站不可用时提示永久删除，
仅输入 `y` / `Y` 确认，其他输入或无法读取输入均取消，保留源码、声明和依赖包。

批量安装清单示例（本地 ZIP 路径相对于 `package.yml`）：

```yaml
modules:
  - source: ./packages/example.zip
  - source: https://github.com/owner/another-module
    ref: v1.0.0
```

源码逐个校验并原子发布，全部到位后统一解析。失败或取消时恢复声明和锁，已发布源码保留；重跑同一命令会跳过内容相同的源码。
崩溃导致状态未提交时，按提示运行 `alira sync --repair` 恢复快照，再重试原命令。并发写入等待最多 30 秒；不支持跨主机共享 `~/.alira`。

## 文档

[模块开发与依赖管理](https://github.com/JJJJSTIYYYY/alira/blob/develop/docs/README.md)：了解模块声明、依赖关系和生命周期。

## 社区

[QQ群](https://qun.qq.com/universal-share/share?ac=1&authKey=ommoQrT2zhzHU%2FUxv8pfGCJbNifW%2BJyUAFBkNdzkHTPUxdxCnlgxm5aNgGslTmdE&busi_data=eyJncm91cENvZGUiOiI2Mzk0NTkxNzIiLCJ0b2tlbiI6Im9ZZkdNUWZnSVV1Y2REeUhKNnlTbWEwc05Bb093djRzUXdXNE55dklBVnlBQk9XbGNpS0ZXSDlzK3orSW1sQ3YiLCJ1aW4iOiIzMTI5NDI0NTcyIn0%3D&data=OGTchcr80RAQg8Z8_GZTdvBb7kZDeM9B3hHcNqLaAX2ZK_KYq260C4CubblEBT1bK5fP6zgtnCk2D8fIoph1ZQ&svctype=4&tempid=h5_group_info) · [Discord](https://discord.gg/tUCQthwK5)

欢迎通过 [Issues](https://github.com/JJJJSTIYYYY/alira/issues) 反馈问题、讨论想法，或提交 Pull Request。

## 许可证

```md
[GNU GPL v3.0](https://github.com/JJJJSTIYYYY/alira/blob/develop/LICENSE)
```

<div align="center">

<img src="https://raw.githubusercontent.com/JJJJSTIYYYY/alira/develop/docs/image/alira-icon.png" alt="Alira" height="150">

</div>
