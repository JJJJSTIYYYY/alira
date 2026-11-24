# 标准模块与依赖管理

本文保留当前已确定的模块设计。其余开发期参考文档集中在 `reference/`，暂不纳入版本管理。

新模块的生命周期、事件通信与 Apixis 3.0.0 示例见[Alira 模块开发指南](alira-module-development.md)。

`ModuleManager` 扫描 `alira/modules/*/main.py`，导入入口后检查是否导出了已完成 `BaseModule` 初始化的实例。
没有有效实例的入口记为不可用；当前只有 `web` 提供标准模块入口，Agent、工具和数据库继续使用原有接口。

## 模块目录

每个模块的业务代码集中在自己的 `src/` 下，模块根目录保留配置、依赖清单和生命周期入口：

```text
src/alira/modules/<module>/
├── __init__.py       # 包标记，不导入业务代码
├── main.py           # 模块声明与生命周期入口
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

标准模块的 `main.py` 必须导出一个已完成 `BaseModule.__init__()` 的实例；只有类定义或未调用基类初始化的实例均不登记。
导出多个有效实例会报声明冲突；模块可用性不根据入口文件是否为空判断。模块目录的 `__init__.py`、
`main.py` 和构造方法只声明元数据，不导入业务子包或第三方业务依赖；所有业务导入、
路由注册和服务初始化都放在 `startup()`。`startup()` 返回表示模块已就绪。
`startup()` 和 `shutdown()` 都有默认实现；无清理需求的模块可以只覆盖 `startup()`。
有资源的模块必须在 `shutdown()` 中清理，包括启动失败或取消时已获取的部分资源。

模块可以覆盖 `async health() -> ModuleStatus` 报告自身健康，返回 `health: bool` 与 `message: str`。
`ModuleStatus` 从 `alira.kernel.module` 或 `alira.kernel` 导入。基类默认返回
`{"health": False, "message": "Health check is not implemented."}`；`True` 表示健康，`False` 表示不健康或尚未确认健康，具体模块按自有资源定义检查逻辑。
此接口不改变生命周期，管理器不会自动调用；检查应支持启动前和关闭后调用，超时由调用方控制。

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
可使用版本约束，例如 `required_modules=["agent>=1.0,<2.0"]`；不写约束表示仅要求模块存在。
`required_kernel` 声明 kernel 的 PEP 440 版本要求，默认 `">=3.0.0"`。模块版本必须为静态版本字符串。
选定模块及前置模块的版本、所有消费者约束和循环依赖，会在启动任何模块前统一检查。
`run`、`add`、`lock`、`sync`、单模块/清单 `install` 均复用同一模块图解析器；`--no-sync` 用于 add 时仍解析并写锁，用于 run 时跳过依赖同步。
`add` 还会重新读取已有部署的当前源码声明，避免沿用过期约束；`remove` / `uninstall` 校验保留模块的当前依赖图，并允许删除有问题的模块来修复环境。
`list`、`init`、帮助命令不解析依赖图，以便在依赖损坏时仍可查看目录或创建骨架。
`sync` 只管理声明中的模块，保留未声明源码；损坏的锁可用 `lock` 重建，声明损坏应修复 modules.json 或从 journal 快照恢复。
例如同时要求 `agent>1.0` 和 `agent<=1.0` 会报模块版本冲突。预发布版本按 PEP 440 规则比较。
发现阶段只登记声明；加载时只检查选中模块及其前置模块的缺失依赖和循环依赖。前置模块就绪后才启动依赖它的模块，
管理阶段按源码根串行写入，独立 requirements 的闭包解析和运行时独立模块启动可并行执行。
启动失败只跳过依赖该模块的后续模块，其他分支继续启动。所有启动任务结束后，直接打印各条依赖链的汇总结果：
失败模块显示为红色 `name[x]`，跳过模块显示为灰色；共享前置模块只启动一次，但会出现在各条分支中。
启动计划仅用于展示，调度仍由拓扑排序决定。模块启动异常由 kernel 捕获后立即通过日志器逐条打印，不集中输出异常日志。
失败或取消的模块立即调用 `shutdown()` 清理部分资源；前置模块和无关模块继续运行，CLI 不因单模块启动失败退出。
每个管理器只执行一次模块加载，运行期间不再加载或卸载模块。退出时按实际启动顺序的逆序关闭模块；清理异常即时记录后继续关闭其他模块，不保留重试状态。
子模块不能通过覆盖 `install()` / `uninstall()` 改变依赖管理，安装和卸载由管理器负责。

## 模块通信与共享接口

模块间通过 Apixis 通信，业务数据的发送、请求和结果返回均通过事件完成。
模块不通过直接调用其他模块的内部服务、访问其缓存或共享可变状态来交换数据。

插件统一从 `alira.kernel.comlink` 导入通信能力，无需直接从 Apixis 导包：

```python
from alira.kernel.comlink import *


async def request_example(payload):
    async with bus.expect_event("example.reply", time_out=30) as waiter:
        await bus.post_event(
            event_type=EventType.TRANSIENT,
            event_name="example.request",
            context=payload,
        )
        event = await waiter.wait()
        if event is None:
            raise RuntimeError("Reply cancelled or the event core closed")
        return event.context
```

`comlink` 通过 `__all__` 重导出 Apixis 的公开事件、图和类型接口，`import *` 不导出内部实现。
`bus` 提供 `post_event`、`post_and_wait`、`subscribe`、`unsubscribe`、`expect_event`、`wait_event`、
`get_handler_meta`、`is_registered` 和 `get_event_runtime_status`，均直接复用 Apixis 3.0.0 的公开接口，保留签名、默认值和返回类型。
`post_event` 只等待事件入队，`post_and_wait` 等待本次事件前台处理完并返回事件，调用方先处理取消时的 `None`，再检查 `has_error`。
请求/响应场景先进入 `expect_event` 再发送请求，使用可关联到本次请求的结果事件名；上例的固定名称只用于演示。
只等待其他地方产生的事件使用 `wait_event`；查询运行时概况使用核心所属循环上的 `get_event_runtime_status()`。
通信接口在核心所属的 asyncio 循环调用；内核统一管理核心生命周期。
模块在 `startup()` 中通过 `bus.subscribe(..., group="模块名")` 注册处理器，
在 `shutdown()` 中通过 `bus.unsubscribe(group="模块名")` 清理；内核 `ApixisCore.start()` 执行 `create_core() → start_core()`，返回时状态为 `RUNNING`；
`stop()` 执行 `stop_core() → close_core()`，返回时状态为 `CLOSED`，释放订阅并取消活动事件工作。
关闭后再次启动会创建新核心，模块需要重新注册订阅并编译图。

各模块的 `src/shared/` 只定义可供其他模块复用的共享方法和公共类型，不承担模块间的数据传递。
其他模块可以导入这些定义；消息、请求等类型的实例承载业务数据时，仍通过 Apixis 事件传递。
共享方法不作为绕过事件通信、读取或修改所属模块运行时数据的入口。

Agent 的身份契约、消息模型和循环配置类型位于 `alira.modules.agent.src.shared`；
Web 的请求与载荷模型位于 `alira.modules.web.src.shared`。
生成上下文属于 Agent 内部运行状态，不作为跨模块共享数据的通道。
全局 `alira.shared` 保留应用公共配置、日志和版本支持。
Uvicorn 日志配置由 `web/src/logging.py` 管理，应用日志和通用日志处理器位于 `alira.shared.logger`。
日志文件统一写入 `~/.alira/logs/{Logger.name}/`，按日期、进程启动后缀和文件大小轮转。
`Logger.write_log()` 的目录参数相对于该 Logger 的日志目录；留空时直接写入该目录。

依赖遵循分层方向：模块依赖 kernel、shared 和 utils，kernel 依赖 shared 和 utils，shared 依赖 utils。
utils 不依赖其他 Alira 层；shared 不依赖 kernel 或业务模块；kernel 不导入具体业务模块实现。

需要其他模块提供运行时能力时，通过 `required_modules` 声明依赖，以保证前置模块先就绪。
上述通信边界是模块对齐的设计约束；待对齐模块当前保留现有实现，后续对齐时再落实事件通信和模块依赖声明。

## 管理命令

依赖管理按“声明 → 解析 → 锁定 → 安装”分阶段执行。模块源码只在 checkout 的
`src/alira/modules/`，或 `ALIRA_MODULES_DIR` 指定的现有目录；不会安装到用户状态目录。
从 checkout 子目录运行时向上查找源码根。发行 wheel 可提供命令，但依赖命令需要源码根。
帮助、`list` 和 `init` 保持原有行为；仅 `list` 导入声明获取版本，其他管理操作只做静态检查。

| 命令 | 行为 |
| --- | --- |
| `alira init example` | 创建最小源码骨架，不声明、锁定或安装 |
| `alira list` | 列出源码名称和版本，不读取依赖状态或启动服务 |
| `alira add web` | 声明 web 及前置模块、解析、写锁、安装 |
| `alira add web --no-sync` | 声明、解析、写锁，跳过包安装 |
| `alira add ./example.zip` | 安装 ZIP 源码，并走同一依赖流水线 |
| `alira add ./module-directory` | 安装本地模块目录，拒绝符号链接 |
| `alira add https://github.com/owner/repo --ref v1` | 安装指定 GitHub 修订 |
| `alira add --manifest package.yml` | 添加清单中的多个源码，再统一解析 |
| `alira lock` | 仅解析并生成锁；输入未变时保留既有解析结果 |
| `alira lock --check` | 不写文件；锁缺失、声明或 requirements 变化时失败 |
| `alira sync` | 必要时重锁，按精确版本和归档哈希安装，清理多余包 |
| `alira sync --locked` | 禁止重锁；锁过期时失败，声明和锁保持原样 |
| `alira sync --frozen` | 跳过声明新鲜度检查，直接使用现有锁同步 |
| `alira remove web` | 删除声明、重算锁并卸载独占包；源码保留 |
| `alira remove web --purge` | 同时将源码送入系统回收站 |
| `alira uninstall web` | 与 remove --purge 相同 |
| `alira tree` | 必要时锁定，显示模块依赖和包闭包；支持 --locked/--frozen |
| `alira migrate` | 显式迁移旧全局状态，不安装 Python 包 |

`install <source>` 保留为源码安装别名；`install --package FILE` / `-r FILE` 保留为清单入口。
新命令使用 `add --manifest FILE`。旧 `sync --no-install` 对应 `lock`，旧 `add --no-install` 对应 `add --no-sync`。
`remove --no-sync` 只更新声明和锁，保留 Python 包供稍后 sync。

### 启动行为

`alira run web` 在导入模块入口和启动内核前检查声明、源码、requirements 指纹和环境，
必要时更新锁并安装缺包。它保留无关 Python 包；`sync` 执行精确同步并清理这些包。
已声明模块的前置模块自动加载。未声明模块需先执行 `alira add`。

- `run --locked`：要求锁最新，允许按锁补齐环境。
- `run --frozen`：使用已有锁，允许补齐环境，不检查当前声明是否更新。
- `run --no-sync`：跳过依赖状态和环境检查，由应用自行承担缺包风险。
- `run --sync`：显式执行一次同步，然后启动。同步结束即释放文件锁。

这些默认行为与 [uv 的锁定和同步规则](https://docs.astral.sh/uv/concepts/projects/sync/) 一致。
不带模块名时只启动内核，不加载业务模块；`run --no-sync` 也可在迁移旧状态前启动。
管理命令不启动服务、不注册信号处理器、不打印启动 Logo。应用 Ctrl+C 会清理资源，退出码为 130。

### 项目状态和依赖保护

Alira 目录固定为 `~/.alira`。状态目录为
`~/.alira/projects/<sha256(realpath(modules_root))[:16]>/`，包含：

- `modules.json`：format 2 声明，记录模块版本、来源、模块依赖和 requirements 指纹，不含 Python 包版本。
- `modules.lock.json`：format 1 解析结果，记录内核/Python 身份、输入哈希、模块源码哈希、全部受管包及归档哈希。
- `.lock`：filelock 互斥文件；命令执行期间另有 `lock.info` 记录 pid、主机、时间和操作。
- `journal.json`：操作执行期间保存文件快照、快照哈希和已发布源码名称，成功后删除。

Python 包安装到当前 `sys.executable`。每个 checkout 应使用独立虚拟环境。
内核发行包 `alira-harness` 及其运行时闭包由已安装元数据保护，若 uv 同环境安装也一并保护；
内核包不写入受管 packages。不同发行包的卸载记录若与内核共享文件，先解除这些文件的卸载声明。

requirements 支持 uv 的 includes、constraints、extras 和 markers；本地 `-r` / `-c` 递归指纹可检测输入变化。
解析器策略版本、规范化声明、内核版本和 Python 完整版本共同组成 `inputs-hash`。
锁内保留完整 PEP 751 归档信息，安装时临时生成 pylock.toml，状态目录不保留第二份锁。
现有锁中的包版本作为解析偏好；约束需要时仍可升级。
内核依赖只有 Apixis、packaging、PyYAML、Send2Trash 和 filelock，业务包由模块声明。

### 原子写入、取消和恢复

`add/remove/lock/sync/install/uninstall/migrate` 的修改过程持有 filelock，最多等待 30 秒。
超时输出拥有者信息并返回 2。运行稳定环境时只读检查不持有长期锁；遇到正在写入的状态会等待同步完成。
不支持跨主机共享 `~/.alira`。

文件使用临时同目录文件、fsync 和 os.replace 原子替换。失败或取消时，在 shield 保护下恢复声明和锁原始字节。
包安装可能部分完成，后续 sync 可幂等修复；恢复失败仍保留原错误退出码并提示 `alira lock`。
崩溃残留 journal 且状态变化时，下一次修改命令会提示 `alira sync --repair`，该选项先还原快照再继续同步。
journal 的死进程未改状态时可自动清理；`.lock` 留在磁盘不代表锁仍被占用。

退出码：成功 0；声明、解析、锁过期 1；并发锁冲突 2；依赖安装或卸载失败 3；用户取消 130。

## 源码安装与卸载

ZIP 根或一层包装目录必须包含 `main.py` 和 `src/`；也支持本地模块目录和公共 GitHub 仓库。
静态声明支持 BaseModule 构造调用、继承构造方法、super/显式基类初始化和顶层字面量常量，绝不执行源码推导元数据。
模块名须是合法 Python 包标识符并匹配目标目录。拒绝路径穿越、绝对归档路径、符号链接和特殊文件。

源码在模块根 `.alira-staging/` 内暂存，校验后通过 os.replace 原子发布；状态目录不保存模块源码。
源码逐个发布；批量操作后续项目失败时，已发布源码保留。声明和锁仍回到操作前状态，重试原命令即可继续。
`.alira-staging` 的发布记录使相同输入且已发布内容未变时跳过重新下载、解压和复制；本地 ZIP 变化会使记录失效。
目标目录内容不同则报错并保留。源码树 SHA256 按相对路径和文件摘要稳定计算，忽略 .git、__pycache__ 和 pyc。

```yaml
modules:
  - source: ./packages/example.zip
  - source: https://github.com/owner/another-module
    ref: v1.0.0
```

本地 source 相对清单位置；清单内 ref 单独设置。源码全部到位后，联合求解本次选择和已有声明，
清单不会把其他未声明源码加入依赖管理。固定 git 提交可在原始来源仍可访问时恢复同一源码。
`remove` 后 sync 不会把保留源码重新加入；`uninstall` / `remove --purge` 使用真正系统回收站。
回收站失败时尝试还原源码，不永久删除。

### 旧状态迁移

运行 `alira migrate` 将 `~/.alira/modules.json` 与 `pylock.toml` 转成当前源码根的新状态。
校验旧 JSON 中的 lock_hash 后复用旧包版本、下载地址、归档哈希和所属模块，避免重新下载或求解。
旧源码树模块记为 builtin，旧文件保留，另写 `.v1.bak` 备份。失败不替换旧文件，新状态文件回滚。
迁移需要原来记录的全部模块源码和相同的 requirements 输入；输入已变时明确拒绝复用旧包结果，可重新 add。
手写声明中的相对 source.path 以模块源码根为基准；add 生成的本地 source.path 始终为绝对路径。
新状态已有声明或锁时拒绝覆盖。

## 模块配置

模块设置由各自目录的 `config.py` 读取同目录的 `config.yaml`，与启动时的工作目录无关：

| 配置文件 | 内容 |
| --- | --- |
| `src/alira/modules/web/config.yaml` | `base_url`；`startup_timeout`、`shutdown_timeout`，默认各 30 秒 |
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
Alira 状态与日志固定保存在用户主目录的 `.alira` 中，通过 `Path.home()` 获取主目录。
`SERVER.base_dir` 仍通过 `ALIRA_BASE_DIR` 提供给业务模块使用，保留配置原值；路径解释由模块负责，
不会改变 Alira 状态或日志的位置。应用根配置仍从工作目录的 `config.yaml` 读取。
模块配置缺失或某项为 `null` 时使用 `config.py` 中的默认值；配置在导入时读取，修改后需重启。
`config.yaml` 均为本地文件，不纳入 Git 和发行包；安装后可在对应模块目录创建，未创建时直接使用默认值。
模块声明阶段不导入业务配置；模块实现使用自己的 `config.py`，共享应用配置不导入任何业务模块。

## 管理接口

内核实现按职责组织，模块仍通过 `alira.kernel.module` 的公共接口接入：

```text
kernel/
├── initialize.py             # 内核服务注册顺序和整体生命周期
├── comlink/
│   ├── __init__.py           # 插件通信统一导入入口，重导出 Apixis 公开 API
│   └── bus.py        # Apixis 生命周期和事件通信接口
├── lifespan/
│   ├── auto_init.py          # 通用服务注册、一次启动和退出关闭
│   └── resource_cleaner.py   # 资源清理调度
└── module/
    ├── base_module.py        # 模块声明和生命周期契约
    ├── module_manager.py     # 单例公共入口和操作互斥
    ├── discovery/
    │   ├── catalog.py        # 无导入的目录枚举与静态模块声明
    │   └── resolution.py     # 模块名称、版本约束与依赖图解析
    ├── runtime/
    │   ├── registry.py       # 声明发现、登记、路径和依赖图
    │   └── lifecycle.py      # 模块并行启动、退出和失败清理
    ├── sources/
    │   ├── scaffold.py       # 最小模块源码骨架生成
    │   ├── archive.py        # 源码准备、安装、卸载与失败回滚
    │   └── package.py        # 清单校验、整批源码安装及可重复执行
    └── dependencies/
        ├── types.py          # 依赖信息、解析结果和错误契约
        ├── deployment.py     # 依赖归属、校验、同步和释放
        ├── uv.py             # uv 子进程和联合依赖解析
        ├── ledger.py         # 声明、JSON 锁与事务日志文件
        └── requirements.py   # requirements 输入指纹
```

声明、运行状态和部署记录分别由对应组件持有，`ModuleManager` 统一串行化公开操作。
组件不会单独注册到 `auto_init`；整体生命周期仍由模块管理器负责。
测试目录 `tests/kernel/module/` 同步按职责组织，静态声明、源码操作和依赖测试分别位于
`discovery/`、`sources/` 和 `dependencies/` 下；公共管理器及运行生命周期由管理器测试覆盖。

```python
from alira.kernel.module import module_manager

manager = module_manager
await manager.init("custom")       # 创建 custom 模块源码骨架，不部署依赖
await manager.install("./example.zip")  # 安装源码并管理依赖
await manager.install_package("package.yml")  # 整批安装源码，再统一解析已声明模块并同步
await manager.add("example")        # 管理选定模块及前置模块依赖
await manager.sync()                # 按声明更新锁并同步环境
await manager.start()               # 此时才导入并登记运行时模块
await manager.load("example")     # 离线只读校验依赖后启动
print(await manager.check_health("example"))  # dict[str, ModuleStatus]
print(manager.loaded)
print(manager.dependencies)        # DependencyInfo: version、modules、ref_count、preinstalled
await manager.stop()               # 退出时按启动顺序的逆序关闭模块，保留部署
await manager.remove("example")   # 释放依赖部署，保留源码；共享包保留至最后一个引用释放
```

`module_manager` 是进程内全局单例；`ModuleManager()` 返回同一个对象，重复构造不会重置其状态。
`await manager.init("名称")` 按[模块开发指南](alira-module-development.md#1-建一个最小模块)生成源码并返回模块目录。
生成的声明版本为 `0.1.0`，提供可编辑的 `startup()` / `shutdown()` / `health()`，`requirements.txt` 为空，
`src/shared/events.py` 预留公开事件契约。初始化不注册或加载模块、不读取或修改依赖记录。
模块名须为有效 Python 标识符，不能是关键字或以 `__` 开头；已有文件、目录或符号链接不会被覆盖。
文件在模块根目录的临时目录中准备完成后迁入目标；写入失败不会留下可发现的半成品模块。
`await manager.start()` 只发现、登记运行时模块；`install("来源")` 安装源码和依赖，`add("名称")` 管理已有模块依赖，`sync()` 按声明同步，`load("名称")` 校验后启动模块。
模块发现成功后不再重复扫描；CLI 预检查与内核启动复用同一份声明。发现前需设置好模块目录与导入包名。
直接调用不带参数的 `load()` 会加载全部已登记模块；命令行未选择模块时不会调用它。
`await manager.check_health(*names)` 调用指定模块的 `health()`，按模块名返回 `ModuleStatus`；
不传名称时检查全部已登记模块，包括尚未加载的模块，空登记表返回空字典。指定名称不自动包含前置模块，重复名称只检查一次。
接口不自动发现模块或读取部署记录；未知名称在任何检查开始前报 `ModuleLoadError`。
检查按选择顺序执行，并与加载、关闭等操作互斥；检查钩子不能调用会获取管理器操作锁的接口。
单个模块检查抛出异常时，该模块返回 `health=False`，`message` 包含异常类型和说明，其他模块继续检查。
调用方可以用 `asyncio.timeout(...)` 限制整次检查；调用方取消仍正常传播并释放操作锁。
`load()` 在同一管理器上只能执行一次，重复调用报 `RuntimeError`；应用运行期间不提供模块加载或卸载接口。
启动任务按拓扑关系调度，单模块失败只跳过后续依赖模块；该模块的部分资源立即清理，已成功启动的模块保持运行。
`stop()` 只在退出时统一关闭模块，逐项记录清理异常并继续；重复调用不再执行已处理的关闭钩子。
依赖包与部署引用保留到显式 remove，部署移除失败时执行 `sync()` 按已还原的声明和锁修复环境。
管理器不清除 `sys.modules`、Python 类定义或自动撤销 Apixis handler；模块必须在 `shutdown()` 中自行注销 handler。
升级代码或依赖后需要重启进程。
测试可通过私有 `_new_for_tests()` 工厂创建独立管理器/生命周期实例，不重置或丢弃正在运行的全局单例。

## web 模块与进程入口

`web/main.py` 只声明 `WebModule`。启动时才导入 `web/src/application.py` 和 `web/src/runtime.py`，
创建 FastAPI 应用并自动扫描 `alira.modules.web.src.router`，注册各子模块或子包导出的 `APIRouter`，
随后启动 Uvicorn，保留 `/health` 接口。路由文件统一放在 `src/alira/modules/web/src/router/`。
监听地址由 `web/config.yaml` 的 `base_url` 设置，默认端口为 2712。

```bash
uv run alira run web
```

通过 `run` 后的位置参数选择启动模块，例如 `uv run alira run web example`。
`example` 要求存在声明名为 `example` 的标准模块；选择没有可用实例的入口会在启动内核前报错。
`uv run alira --help` 查看子命令；`uv run alira run --help` 查看位置参数与启动选项，不扫描或导入模块，也不读取部署记录。
未知模块和未知选项报错，不接受缩写；模块名称直接放在 `run` 后，不加连字符。
`run` 默认自动同步；显式同步使用 `uv run alira run web example --sync`，跳过使用 `--no-sync`。
不带模块名的 `uv run alira run` 只运行内核；`uv run alira` 缺少子命令会报错。

CLI 先解析并校验命令行参数；管理命令直接执行并退出，不发现运行时模块，不进入内核生命周期。
应用运行路径先检查并按需同步声明和锁，再导入入口、启动内核服务，按依赖顺序执行 startup()。
`--no-sync` 跳过依赖同步；模块图仍会在任何 startup() 前检查。
未声明源码不会被 sync 加入；已声明模块的无效 requirements 会使同步阶段失败。

运行完整开发测试时显式提供所有业务模块依赖（汇总文件只供开发测试使用）：

```bash
uv run --with-requirements tests/requirements.txt pytest
```

`tests/kernel/module/test_isolated_commands.py` 构建当前 wheel，在独立解释器和临时用户目录中运行真实 `alira` 入口与 uv，
覆盖源码根和状态隔离、共享依赖、版本更新、元数据修复、跨进程竞争、强制终止后的 journal 修复，以及失败时的状态恢复和源码保留。
默认使用离线本地 wheel；额外的联网与系统回收站测试需要显式启用：

```bash
ALIRA_TEST_NETWORK=1 ALIRA_TEST_SYSTEM_TRASH=1 uv run --with-requirements tests/requirements.txt pytest tests/kernel/module/test_isolated_commands.py
```

联网测试安装官方 PyPI 的 Transformers 和 uv，验证 GitHub 仓库的下载与结构检查；
回收站测试只移动临时生成的测试模块，使用真正的系统回收站。

`auto_init.py` 与 `resource_cleaner.py` 位于 `kernel/lifespan/`。
`kernel/initialize.py` 集中注册 kernel 服务，顺序为日志、事件核心、资源清理器、全局模块管理器。
重复初始化不会重复注册，也不导入或迁移尚未对齐的业务模块。
该文件的 `lifespan()` 通过 `auto_init.start()` / `auto_init.stop()` 统一启停服务。
`main.py` 只保留程序入口。`cli/entrypoint.py` 负责分发，`cli/parser.py` 构建参数解析器，
每个管理命令位于独立的 `cli/commands/` 文件中。`cli/application.py` 只在运行应用时导入，
负责启动 Logo、进程信号和内核生命周期；管理命令不注册信号处理器。FastAPI 逻辑由 web 模块拥有。
内核服务只启动一次，按注册顺序启动；某个基础服务启动失败时立即记录异常并终止应用启动。
退出时按注册顺序的逆序逐项关闭所有已登记服务，因此先关闭模块，再关闭事件核心和日志服务；关闭异常即时记录后继续，不重试。
Ctrl+C 和 SIGTERM 都会进入这一清理流程；Uvicorn 的最后一条退出日志在 `logger.stop()` 排空并关闭写入线程前产生，服务器无需额外的日志作用域。

发布前可执行 `python scripts/check_distribution.py dist/<wheel文件名>.whl` 核对发行名和规范版本。
