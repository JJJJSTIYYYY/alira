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

- **模块化运行**：按需选择启动模块，自动加载前置模块，依赖在部署时显式安装。
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
uv run alira --modules web
```

默认可访问 <http://localhost:2712/health> 检查服务状态。
启动仅校验已部署依赖，不联网安装。

PyPI 发行包名为 `alira-harness`，Python 导入名和命令均为 `alira`。

## 常用命令

| 命令 | 用途 |
| --- | --- |
| `uv run alira` | 只启动内核 |
| `uv run alira --help` | 查看命令与启动参数 |
| `uv run alira list` | 只列出模块目录，不导入模块或读取依赖记录 |
| `uv run alira sync` | 全量重建依赖记录，同步环境并清理不再需要的包 |
| `uv run alira sync --no-install` | 只解析依赖并更新记录 |
| `uv run alira add web` | 管理已有 web 模块及前置模块的 Python 依赖 |
| `uv run alira add web --no-install` | 只更新指定模块的依赖记录 |
| `uv run alira remove web` | 移除依赖管理记录并清理独占依赖，保留源码 |
| `uv run alira install ./example.zip` | 安装模块源码并自动执行 add |
| `uv run alira install https://github.com/owner/repo --ref v1.0.0` | 从 GitHub 安装模块及依赖 |
| `uv run alira install -r package.yml` | 批量安装清单中的模块，再一次性执行全量 sync |
| `uv run alira uninstall example` | 清理依赖并将模块源码送入系统回收站 |
| `uv run alira --modules web` | 启动 web，自动加载前置模块 |
| `uv run alira --modules web --install-missing` | 显式先部署选定模块依赖，再启动 web |

批量安装清单示例（本地 ZIP 路径相对于 `package.yml`）：

```yaml
modules:
  - source: ./packages/example.zip
  - source: https://github.com/owner/another-module
    ref: v1.0.0
```

命令会先检查全部源码和前置模块，再安装源码、统一同步依赖。解析失败会撤回新增源码；安装中断后运行 `uv run alira sync` 补齐。
源码安装被强制中断时，重跑同一清单即可继续，内容不同的已有模块会报错并保留。

## 文档

[模块开发与依赖管理](https://github.com/JJJJSTIYYYY/alira/blob/develop/docs/README.md)：了解模块声明、依赖关系和生命周期。

## 社区

[QQ群](https://qun.qq.com/universal-share/share?ac=1&authKey=ommoQrT2zhzHU%2FUxv8pfGCJbNifW%2BJyUAFBkNdzkHTPUxdxCnlgxm5aNgGslTmdE&busi_data=eyJncm91cENvZGUiOiI2Mzk0NTkxNzIiLCJ0b2tlbiI6Im9ZZkdNUWZnSVV1Y2REeUhKNnlTbWEwc05Bb093djRzUXdXNE55dklBVnlBQk9XbGNpS0ZXSDlzK3orSW1sQ3YiLCJ1aW4iOiIzMTI5NDI0NTcyIn0%3D&data=OGTchcr80RAQg8Z8_GZTdvBb7kZDeM9B3hHcNqLaAX2ZK_KYq260C4CubblEBT1bK5fP6zgtnCk2D8fIoph1ZQ&svctype=4&tempid=h5_group_info) · [Discord](https://discord.gg/tUCQthwK5)

欢迎通过 [Issues](https://github.com/JJJJSTIYYYY/alira/issues) 反馈问题、讨论想法，或提交 Pull Request。

## 许可证

[GNU GPL v3.0](https://github.com/JJJJSTIYYYY/alira/blob/develop/LICENSE)
