<div align="center">

# Alira

[GitHub 仓库](https://github.com/JJJJSTIYYYY/alira)

![Python](https://img.shields.io/badge/Python-3.12+-3776AB?style=flat&logo=python)
![Electron](https://img.shields.io/badge/Electron-37-47848F?style=flat&logo=electron)
![Vue](https://img.shields.io/badge/Vue-3.5-4FC08D?style=flat&logo=vue.js)
[![License](https://img.shields.io/badge/License-GPL%203.0-blue?style=flat)](./LICENSE)
[![Discord](https://img.shields.io/badge/Discord-Join%20Us-5865F2?style=flat&logo=discord&logoColor=white)](https://discord.gg/tUCQthwK5)
[![QQ群](https://custom-icon-badges.demolab.com/badge/QQ群-639459172-00BFFF?style=flat&logo=tencent-qq)](https://qun.qq.com/universal-share/share?ac=1&authKey=ommoQrT2zhzHU%2FUxv8pfGCJbNifW%2BJyUAFBkNdzkHTPUxdxCnlgxm5aNgGslTmdE&busi_data=eyJncm91cENvZGUiOiI2Mzk0NTkxNzIiLCJ0b2tlbiI6Im9ZZkdNUWZnSVV1Y2REeUhKNnlTbWEwc05Bb093djRzUXdXNE55dklBVnlBQk9XbGNpS0ZXSDlzK3orSW1sQ3YiLCJ1aW4iOiIzMTI5NDI0NTcyIn0%3D&data=OGTchcr80RAQg8Z8_GZTdvBb7kZDeM9B3hHcNqLaAX2ZK_KYq260C4CubblEBT1bK5fP6zgtnCk2D8fIoph1ZQ&svctype=4&tempid=h5_group_info)

**全自研的 Agent 运行时与 SDK。**

</div>

---

## 🎯 这是什么？

Alira 是一个可拓展的**全栈的 AI Agent 协作平台**。它是一套完整的 Agent 底座与运行时——支持多智能体并行协作、沙箱代码执行、知识库检索以及高自由度的插件系统。

## 🌟 有什么新内容？

- [x] Alira 的事件与图执行底座由独立项目提供。**[-> Apixis](https://github.com/JJJJSTIYYYY/Apixis.git)**
- [x] 存储层使用内存缓存，持久化数据通过 SQLAlchemy + aiosqlite 存入 SQLite。
- [ ] 更简化的部署流程，使用 内存 + Sqlite 即可实现pip一键安装。
- [ ] CLI Agent支持，无需使用 Electron 客户端，即可完成你的工作。
- [x] 自研 Agent 底座，更小的外部依赖以及更可控的版本迭代。
- [x] 自由的插件拓展点，你可以在 Agent 运行时的任何节点，自由的订阅事件以实现各种高自由度的插件，全凭你的想象！
- [ ] **Alira预计加入分布式Agent集群，敬请期待！！！**

- 消息存储支持可选的 SQLite 向量检索；配置与分页接口见[消息检索文档](./docs/message-search.md)。其他向量库与检索工具可通过插件接入。
- 沙箱预计沿用2.x的docker沙箱机制，但不再作为强制依赖。

## 后端开发

克隆仓库后，在仓库根目录执行：

```bash
git clone git@github.com:JJJJSTIYYYY/alira.git
cd alira
uv sync --locked
uv run pytest
uv run python -m alira.server
```

项目目录：

```text
alira/
├── pyproject.toml    # 打包、依赖与测试配置
├── MANIFEST.in       # 源码分发内容
├── uv.lock          # 依赖锁文件
├── .python-version  # Python 版本
├── src/
│   └── alira/       # 应用源码，版本号统一定义在 _version.py
├── tests/           # 测试
└── docs/            # 文档
```

本地 `config.yaml` 放在仓库根目录，运行数据默认保存在 `.alira_data/`；
两者均不纳入版本控制。已有 `config.yaml` 中的数据路径继续生效。`uv sync` 在根目录创建 `.venv/`。

`uv sync --locked` 会将本地项目安装为可编辑包，源码修改立即生效。测试从已安装的包导入，
启动方式仍为 `uv run python -m alira.server`。包版本通过 `alira.__version__` 获取；
项目元数据、FastAPI 版本和启动横幅均使用 `src/alira/_version.py` 中的 `__version__`。
构建 wheel 和源码包时执行 `uv build`。

事件与图调度通过 PyPI 依赖 `apixis` 提供；Alira 保留 Agent SDK、工具、存储与服务层。

## 📚 使用文档

- [总览](./docs/README.md)
- [Apixis 事件与图接口](https://github.com/JJJJSTIYYYY/Apixis/tree/master/docs)
- 事件与图执行底座：[Apixis（Apache 协议开源）](https://github.com/JJJJSTIYYYY/Apixis.git)

---

## 📄 许可证

本项目基于 **GNU GPL v3.0** 协议开源。

---

__🏘️ 加入社区__

[QQ群](https://qun.qq.com/universal-share/share?ac=1&authKey=ommoQrT2zhzHU%2FUxv8pfGCJbNifW%2BJyUAFBkNdzkHTPUxdxCnlgxm5aNgGslTmdE&busi_data=eyJncm91cENvZGUiOiI2Mzk0NTkxNzIiLCJ0b2tlbiI6Im9ZZkdNUWZnSVV1Y2REeUhKNnlTbWEwc05Bb093djRzUXdXNE55dklBVnlBQk9XbGNpS0ZXSDlzK3orSW1sQ3YiLCJ1aW4iOiIzMTI5NDI0NTcyIn0%3D&data=OGTchcr80RAQg8Z8_GZTdvBb7kZDeM9B3hHcNqLaAX2ZK_KYq260C4CubblEBT1bK5fP6zgtnCk2D8fIoph1ZQ&svctype=4&tempid=h5_group_info) | [Discord](https://discord.gg/tUCQthwK5)


> ⭐️ 如果你喜欢我们的项目，欢迎你的Star!
