# 标准模块与依赖管理

本文保留当前已确定的模块设计。其余开发期参考文档集中在 `reference/`，暂不纳入版本管理。

`ModuleManager` 扫描 `alira/modules/*/main.py`。缺少或仅包含空白的 `main.py` 不参与标准模块加载；
当前只有 `web` 已对齐，Agent、工具和数据库继续使用原有接口。

## 模块目录

每个模块的业务代码集中在自己的 `src/` 下，模块根目录保留配置、依赖清单和生命周期入口：

```text
src/alira/modules/<module>/
├── __init__.py       # 包标记，不导入业务代码
├── main.py           # 模块声明与生命周期；未对齐时为空
├── config.py         # 模块配置读取（按需提供）
├── config.yaml       # 本地配置，不提交或打包（按需提供）
├── requirements.txt  # 模块专属 Python 依赖
└── src/
    ├── __init__.py
    ├── shared/       # 共享方法和公共类型（按需提供）
    └── ...           # 业务实现
```

业务代码通过 `alira.modules.<module>.src` 导入，模块配置仍从
`alira.modules.<module>.config` 导入。测试中的业务用例对应放在 `tests/modules/<module>/src/`。

已对齐模块的 `main.py` 必须导出一个继承 `BaseModule` 的实例；未对齐模块保留空文件占位。模块目录的 `__init__.py`、
`main.py` 和构造方法只声明元数据，不导入业务子包或第三方业务依赖；所有业务导入、
路由注册和服务初始化都放在 `startup()`。`startup()` 返回表示模块已就绪。
`shutdown()` 还应能清理 `startup()` 失败或取消时已经获取的资源。

```python
from alira.kernel.module import BaseModule


class ExampleModule(BaseModule):
    async def startup(self):
        self.service = None
        from .src.service import service

        self.service = service
        await service.start()

    async def shutdown(self):
        if self.service is not None:
            await self.service.stop()


module = ExampleModule(
    name="example",
    version="1.0.0",
    required_modules=["web"],
)
```

`required_modules` 使用模块声明的 `name`，不是 Python 包名或 PyPI 包名。
发现阶段只登记声明；加载时只检查选中模块及其前置模块的缺失依赖和循环依赖。前置模块就绪后才启动依赖它的模块，
其余模块的安装和启动任务并行执行；共享 Python 环境的文件写入锁由 uv 管理。
子模块不能通过覆盖 `install()` / `uninstall()` 改变依赖管理，安装和卸载由管理器负责。

## 模块通信与共享接口

模块间通过 Apixis 通信，业务数据的发送、请求和结果返回均通过事件完成。
模块不通过直接调用其他模块的内部服务、访问其缓存或共享可变状态来交换数据。

各模块的 `src/shared/` 只定义可供其他模块复用的共享方法和公共类型，不承担模块间的数据传递。
其他模块可以导入这些定义；消息、请求等类型的实例承载业务数据时，仍通过 Apixis 事件传递。
共享方法不作为绕过事件通信、读取或修改所属模块运行时数据的入口。

Agent 的身份契约、消息模型和循环配置类型位于 `alira.modules.agent.src.shared`；
Web 的请求与载荷模型位于 `alira.modules.web.src.shared`。
生成上下文属于 Agent 内部运行状态，不作为跨模块共享数据的通道。
全局 `alira.shared` 保留应用公共配置、日志和版本支持。
Uvicorn 日志配置由 `web/src/logging.py` 管理，应用日志和通用日志处理器位于 `alira.shared.logger`。

依赖遵循分层方向：模块依赖 kernel、shared 和 utils，kernel 依赖 shared 和 utils，shared 依赖 utils。
utils 不依赖其他 Alira 层；shared 不依赖 kernel 或业务模块；kernel 不导入具体业务模块实现。

需要其他模块提供运行时能力时，通过 `required_modules` 声明依赖，以保证前置模块先就绪。
上述通信边界是模块对齐的设计约束；待对齐模块当前保留现有实现，后续对齐时再落实事件通信和模块依赖声明。

## Python 依赖

默认读取 `main.py` 同目录的 `requirements.txt`，不存在时无需安装。
`requirements_txt` 可指定绝对路径，或相对于 `alira/modules` 的路径；显式指定但不存在时报错。
默认命名的 `requirements.txt` 随模块一起打包；自定义文件名需在 package-data 中声明。
运行环境需要 PATH 中可用的 uv，且当前 Python 环境可写。
项目默认依赖仅保留内核启动所需的 `apixis`、`packaging`、`pyyaml`。
业务依赖分别声明在 agent、database、tool、web 的 requirements.txt 中；未对齐模块的业务实现和启动入口保持原状。
未对齐模块的依赖可显式安装，例如 `uv pip install -r src/alira/modules/agent/requirements.txt`。

管理器复用 [uv 的依赖求解](https://docs.astral.sh/uv/pip/compile/)，支持版本范围、
extras、环境标记、`-r` 包含文件和 `-c` 约束文件。先联合解析本次待加载模块，
得到兼容的版本集合，再生成各模块自己的锁文件和完整依赖集合。
这里的依赖包含传递依赖，不只统计 requirements.txt 中直接声明的包。
本地包建议使用绝对 file URL；所有输入联合解析时的工作目录是 `alira/modules`。
不支持锁文件中缺少版本号的依赖；它们无法参与版本固定和引用登记。

管理器创建时已经安装的包，以及此前由本管理器加载的包，其版本均作为约束，
禁止在运行期间隐式升级或降级。冲突在安装和 `startup()` 前报告。
需要更改这些版本时，应先调整环境再重新启动应用。

包名按照 Python 包规范归一化。相同包只登记一次，每个模块对同一包最多计一次引用。
加载前为整批模块预留引用，防止并行加载或失败清理时删除其他模块正在使用的包。
卸载时先 `shutdown()`，再释放引用并卸载引用归零的包；管理器创建前已经存在的包始终保留。
安装、启动、关闭或卸载失败时，尚未完成的资源清理和引用登记保留为可重试状态。
本管理器只负责自身引入的运行时依赖，不应同时由另一个管理器或外部安装器修改同一环境。

## 模块配置

模块设置由各自目录的 `config.py` 读取同目录的 `config.yaml`，与启动时的工作目录无关：

| 配置文件 | 内容 |
| --- | --- |
| `src/alira/modules/web/config.yaml` | `base_url`，默认 `http://localhost:2712` |
| `src/alira/modules/database/config.yaml` | `worker_count`、`sqlite` 和 `cache` |
| `src/alira/modules/agent/config.yaml` | `generation_ttl`、`container_ttl` 和 `graph_cache_ttl` |

例如，数据库模块的本地配置：

```yaml
worker_count: 4
sqlite:
  embedding_enable: false
  embedding_model: ""
  embedding_options: {}
  vector_dimension: 640
cache:
  hot_cache_default_expire_seconds: 600
  static_cache_default_expire_seconds: 604800
```

根目录 `config.yaml` 只管理公共设置：`SERVER.base_dir`、`LOG`、`RUNTIME.cache_clean_interval`，
以及由 Apixis 自行读取的 `APIXIS`。旧的根目录模块设置不再读取。
模块配置缺失或某项为 `null` 时使用 `config.py` 中的默认值；配置在导入时读取，修改后需重启。
`config.yaml` 均为本地文件，不纳入 Git 和发行包；安装后可在对应模块目录创建，未创建时直接使用默认值。
模块声明阶段不导入业务配置；模块实现使用自己的 `config.py`，共享应用配置不导入任何业务模块。

## 管理接口

```python
from alira.kernel.module import module_manager

manager = module_manager
await manager.start()               # 只发现、登记模块，不安装依赖或启动业务
await manager.load("example")     # 包括 required_modules 中的前置模块
print(manager.loaded)
print(manager.dependencies)        # DependencyInfo: version、modules、ref_count、preinstalled
await manager.unload("example")   # 有其他模块依赖它时拒绝卸载
await manager.stop()               # 按依赖的逆序关闭、卸载所有已加载模块
```

`module_manager` 是进程内全局单例；`ModuleManager()` 返回同一个对象，重复构造不会重置其状态。
`await manager.start()` 只发现、登记模块；之后显式调用 `load("名称")` 才安装依赖和启动模块。
直接调用不带参数的 `load()` 会加载全部已登记模块；命令行未选择模块时不会调用它。
重复加载已运行模块不会重复启动或增加引用。`auto_init.start(restart=True)` 重启内核后仍需显式加载所需模块。
公开的加载、卸载操作互斥，批内按依赖关系调度任务。加载失败时取消尚在执行的任务并清理
未成功启动的模块；已经启动成功的独立模块保留运行，可通过 `stop()` 统一关闭。
关闭失败时保留模块和依赖，卸载失败时保留依赖引用，调用 `unload()` / `stop()` 重试。

## web 模块与进程入口

`web/main.py` 只声明 `WebModule`。启动时才导入 `web/src/application.py` 和 `web/src/runtime.py`，
创建 FastAPI 应用并自动扫描 `alira.modules.web.src.router`，注册各子模块或子包导出的 `APIRouter`，
随后启动 Uvicorn，保留 `/health` 接口。路由文件统一放在 `src/alira/modules/web/src/router/`。
监听地址由 `web/config.yaml` 的 `base_url` 设置，默认端口为 2712。

```bash
uv run alira --web
```

可通过多个模块名称参数选择启动项，例如 `uv run alira --web --example`。
`--example` 要求存在声明名为 `example` 的标准模块；未对齐目录不会生成命令行选项。
`uv run alira --help` 展示已发现模块，未知参数报错，不接受模块名缩写。
不带模块参数的 `uv run alira` 只运行内核，所有已发现模块都保持未加载状态。

启动顺序为：内核服务启动、模块管理器发现声明、解析命令行参数、联合解析选中模块及其
前置模块的依赖、安装依赖、按依赖顺序执行 startup()。未选中模块不会解析或安装依赖；
其缺失前置模块或 requirements 文件也不会阻止其他模块加载。

运行完整开发测试时显式提供所有业务模块依赖（汇总文件只供开发测试使用）：

```bash
uv run --with-requirements tests/requirements.txt pytest
```

`auto_init.py` 与 `resource_cleaner.py` 位于 `kernel/lifespan/`。
`kernel/initialize.py` 集中注册 kernel 服务，顺序为日志、事件核心、资源清理器、全局模块管理器。
重复初始化不会重复注册，也不导入或迁移尚未对齐的业务模块。
该文件的 `lifespan()` 通过 `auto_init.start()` / `auto_init.stop()` 统一启停服务。
`main.py` 负责进程信号、命令行模块选择和这一生命周期入口，FastAPI 逻辑由 web 模块拥有。
所有服务按注册顺序启动、逆序停止，因此退出时先关闭模块，再关闭事件核心和日志服务。
Ctrl+C 和 SIGTERM 都会进入这一清理流程；Uvicorn 的最后一条退出日志在 `logger.stop()` 排空并关闭写入线程前产生，服务器无需额外的日志作用域。
