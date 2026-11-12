# 标准模块与依赖管理

本文保留当前已确定的模块设计。其余开发期参考文档集中在 `reference/`，暂不纳入版本管理。

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
部署时独立模块的安装任务并行执行，运行时独立模块的启动任务并行执行；共享 Python 环境的文件写入锁由 uv 管理。
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
日志文件统一写入 `{ALIRA_BASE_DIR}/logs/{Logger.name}/`，按日期、进程启动后缀和文件大小轮转。
`Logger.write_log()` 的目录参数相对于该 Logger 的日志目录；留空时直接写入该目录。

依赖遵循分层方向：模块依赖 kernel、shared 和 utils，kernel 依赖 shared 和 utils，shared 依赖 utils。
utils 不依赖其他 Alira 层；shared 不依赖 kernel 或业务模块；kernel 不导入具体业务模块实现。

需要其他模块提供运行时能力时，通过 `required_modules` 声明依赖，以保证前置模块先就绪。
上述通信边界是模块对齐的设计约束；待对齐模块当前保留现有实现，后续对齐时再落实事件通信和模块依赖声明。

## 管理命令

所有管理命令均在应用生命周期外执行，不注册或启停内核服务，不打印 Logo，不执行模块入口代码。
运行入口 `uv run alira --modules web` 维持原有生命周期：内核启动后才导入、加载所选模块及前置模块。
管理命令读取目录和静态声明，运行时模块发现则通过已初始化的 `BaseModule` 实例判断可用性。

| 命令 | 职责 |
| --- | --- |
| `uv run alira list` | 只列出 `alira/modules` 中含 `main.py` 的模块目录；不读取 JSON/lock，不解析模块依赖或 Python 依赖 |
| `uv run alira sync` | 全量扫描模块并联合求解 Python 依赖，重建 JSON/lock，安装缺包、更新版本、卸载不再需要的包 |
| `uv run alira sync --no-install` | 只解析并重建记录，不修改 Python 环境 |
| `uv run alira add web` | 解析已有 web 及前置模块的依赖，纳入管理并安装；复用已有管理记录处理共享依赖，不扫描其他未管理模块 |
| `uv run alira add web --no-install` | 只解析并更新记录，不安装、更新或卸载包 |
| `uv run alira remove web` | 从记录移除 web，卸载其他模块和内核均不再需要的依赖；保留源码 |
| `uv run alira install ./example.zip` | 检查、安装源码，再复用 add 安装依赖 |
| `uv run alira install https://github.com/owner/repo --ref v1.0.0` | 从 GitHub 指定分支、标签或提交安装；省略 ref 使用默认分支 |
| `uv run alira install -r package.yml` | 先准备整份清单的源码，再执行一次全量 sync |
| `uv run alira uninstall example` | 复用 remove 清理依赖，将源码移入系统回收站 |

`add/remove/uninstall` 支持多个名称。名称不存在、前置模块缺失、依赖冲突或移除被依赖模块时明确报错。
`remove` 不修改源码；随后执行全量 sync 时，源码仍在的模块会重新纳入依赖管理。
尚无运行时实例的模块也参与 Python 依赖管理，不需要先初始化 `BaseModule`。

管理命令不启动内核、不导入模块入口、不打印启动 Logo。`add/remove/uninstall` 支持多个模块名。
`remove` 保留源码，后续全量 `sync` 会重新纳入这些模块；`uninstall` 将源码送入真正的系统回收站。
依赖记录固定保存在 `Path.home() / ".alira"` 下的 `modules.json` 和 `pylock.toml`。
记录损坏或安装中断后，可执行 `uv run alira sync`，从实际模块源码重新解析并同步环境。
`sync` 会清理当前解释器环境中不属于模块或内核的包；部署、更新或移除依赖前应先停止应用。
Ctrl+C 停止服务并清理资源，保留已部署依赖。

## 源码安装与卸载

ZIP 根目录或一层包装目录内至少包含 `main.py` 和 `src/`。
安装在临时目录下载、解压及静态检查，禁止归档绝对路径、路径穿越、Windows 驱动器路径、符号链接和特殊文件。
静态声明支持 `BaseModule(...)`、继承其构造方法的模块实例，以及模块类中的 `super().__init__(...)` 或显式基类初始化；支持导入别名和限定名称。
模块名称、`required_modules` 和 `requirements_txt` 使用字面量或模块顶层常量（可带类型注解）；不执行函数来推断元数据。
声明名必须是合法 Python 包标识符，并与最终目录名一致；没有名称声明时使用 ZIP/仓库名。
`__init__.py` 缺失时补空文件，不执行入口或任何模块源码。模块目录为当前 Alira 包的 `modules/<name>/`，
源码开发环境即 `src/alira/modules/<name>/`；单模块 install 遇到已有目录一律报错，不覆盖、不静默跳过。
验证后通过目标文件系统中的暂存目录原子移动到模块目录，然后调用 add。

单模块 install/uninstall 失败时尽量恢复源码、依赖记录和此前 Python 包版本；优先使用原锁文件中的下载地址与哈希恢复依赖。
即使旧包恢复失败，仍继续清理新装包并恢复记录，同时报告原始错误和恢复错误，提示执行 sync。
uninstall 使用 Send2Trash 调用真正的操作系统回收站；回收站不可用时明确失败并尝试回滚，绝不改为永久删除源码。
源码操作不会删除或移动其他模块；依赖管理命令不会删除源码。

### 从 package.yml 批量安装

```bash
uv run alira install -r package.yml
# 等同于 install --package package.yml
```

```yaml
modules:
  - source: ./packages/example.zip
  - source: https://github.com/owner/another-module
    ref: v1.0.0
```

清单必须包含非空的 `modules` 列表，每项包含 `source` 和可选的 GitHub `ref`，不接受其他字段或重复键。
本地路径相对于清单所在目录，支持 `~`；`ref` 可指定分支、标签或提交，建议使用固定提交以便重复安装。
模块名由源码静态声明决定，不能重复。清单内的前置模块无需按依赖顺序排列；外部前置模块必须已经存在。
全部归档先下载、解压、校验结构、名称、路径和模块依赖图，随后按前置模块优先的顺序逐个原子迁移源码；不导入或执行模块入口。
源码准备失败不会安装任何新模块。普通迁移错误会尝试撤回本次新迁移的目录，已有源码不受影响。

整批源码到位后复用 `sync`，全量解析所有实际模块，重新写入记录、锁文件并同步环境，保护内核依赖。
这具有完整 sync 的语义：源码仍在但曾被 remove 的模块重新纳入管理，不属于内核或模块的 Python 包会被清理。
依赖解析失败时撤回本次新增源码，保留此前的模块、环境和记录，避免新引入的冲突阻塞后续 sync。
解析成功后，安装失败或 Ctrl+C 中断时保留已经校验的源码；恢复网络后执行 `uv run alira sync` 即可补齐。
损坏或缺失的旧记录不会阻止批量安装后的全量同步，因此这一步无需回滚 JSON/lock 或 Python 包。

若 SIGKILL 或掉电发生在源码迁移过程中，可能仅有部分模块到位；这些模块的前置源码已先迁入，可通过 sync 修复当前集合，
重新执行同一条 `install -r` 则会补齐清单中的剩余模块再同步。
重复执行时会逐文件核对已有模块内容（忽略生成的 `__pycache__`），完全相同则复用，不覆盖；任何内容差异或符号链接均报错。
这一重试规则仅适用于清单安装，单模块 install 的同名冲突行为不变。强制中断遗留的隐藏暂存目录不参与模块发现或依赖扫描。

## Python 依赖与恢复

模块默认读取 `main.py` 同目录的 `requirements.txt`，显式 `requirements_txt` 可以是绝对路径或相对于 `alira/modules` 的路径。
uv 处理版本范围、extras、环境标记、`-r` 包含和 `-c` 约束；本地包含文件递归记录输入指纹。
远程包含只在管理阶段解析，运行期不联网；更新远程输入后再次执行 sync。
项目主依赖仅包含内核使用的 Apixis、packaging、PyYAML 和 Send2Trash。

记录固定保存在 `Path.home() / ".alira" / "modules.json"`，完整 PEP 751 锁文件为同目录的 `pylock.toml`。
路径不随 cwd、Python 环境或 `ALIRA_BASE_DIR` 改变；项目按一台机器部署一个实例设计，不区分多个 Python 环境的账本。
JSON 记录每个模块的 requirements 路径、输入指纹、前置模块和依赖闭包，并记录锁文件的摘要。
两份文件分别原子写入；若中断造成不一致，其他依赖管理命令明确提示 sync，sync 无须读取旧文件即可重建。

sync 以实际模块源码为唯一依赖管理输入，完全替换旧记录，不依赖旧 managed、版本或锁文件。
包安装目标始终为运行 Alira 的 `sys.executable`；完整 sync 清理该环境中不属于模块和内核的包。
内核及其依赖闭包根据已安装 Alira 自身的发行元数据确定，固定当前内核版本，不信任可编辑的 JSON 来决定内核保护范围。
若 uv 安装在同一解释器中，也会保留该依赖管理工具，避免 sync 卸载自身后无法继续管理依赖。
卸载其他发行包前会解除其对内核共有文件的卸载声明，保留仍由内核拥有的命令入口和源码；
包改名前的旧发行包可以正常清理，共享文件不会随之被删除。该保护也覆盖卸载被中断的情况。
插件与内核版本要求冲突时，解析阶段报错，不修改环境；remove 始终保留内核和其他已管理模块需要的包。
add 更新指定模块时，会将已有管理记录中的 requirements 一并参与联合求解，保证共享依赖仍满足其他模块。
add 不清理无关包，环境的完整收敛由 sync 完成。

安装前先提交新记录，解析或安装中断后可再次 sync，从源码重新解析、补齐缺包和清理多余包。
安装中断遗留的缺失或不完整依赖元数据不会阻止 sync；对应的模块依赖会被重新安装。
默认前台等待 uv 并继承终端，实时显示安装/更新/卸载进度；管理接口保留 `background=True` 的异步子进程执行方式供后续使用。
`--no-install` 仍会写入新的解析结果；这些记录不表示当前环境已经完成同步。
运行时 load 只读取记录、requirements 指纹和已安装包元数据，不联网、不写记录或 Python 环境；缺包或版本不符时提示 `uv run alira sync`。
显式 `--install-missing` 对选定模块调用 add 后才执行 load，部署或升级后应重启进程。

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
默认应用数据目录为当前用户主目录下的 `.alira`（Linux/macOS 为 `~/.alira`，Windows 为 `%USERPROFILE%\.alira`）；
通过 `pathlib.Path.home()` 获取主目录并使用平台原生路径。`SERVER.base_dir` 中的相对路径也以用户 home 为基准，
`~` 会展开，结果统一解析为绝对路径，启动日志会显示最终目录。
应用根配置仍从工作目录的 `config.yaml` 读取；只有显式选择不同配置时才可能改变数据目录。
模块配置缺失或某项为 `null` 时使用 `config.py` 中的默认值；配置在导入时读取，修改后需重启。
`config.yaml` 均为本地文件，不纳入 Git 和发行包；安装后可在对应模块目录创建，未创建时直接使用默认值。
模块声明阶段不导入业务配置；模块实现使用自己的 `config.py`，共享应用配置不导入任何业务模块。

## 管理接口

内核实现按职责组织，模块仍通过 `alira.kernel.module` 的公共接口接入：

```text
kernel/
├── initialize.py             # 内核服务注册顺序和整体生命周期
├── lifespan/
│   ├── auto_init.py          # 通用服务注册、启停和失败恢复
│   ├── apixis_core.py        # Apixis 核心生命周期
│   └── resource_cleaner.py   # 资源清理调度
└── module/
    ├── base_module.py        # 模块声明和生命周期契约
    ├── module_manager.py     # 单例公共入口和操作互斥
    ├── catalog.py            # 无导入的目录枚举与静态模块声明
    ├── registry.py           # 声明发现、登记、路径和依赖图
    ├── runtime.py            # 模块并行启动、退出和失败清理
    ├── sources.py            # ZIP/GitHub 源码安装与失败回滚
    ├── package.py            # 清单校验、整批源码安装及可重复执行
    └── dependencies/
        ├── types.py          # 依赖信息、解析结果和错误契约
        ├── deployment.py     # 依赖归属、校验、同步和释放
        ├── uv.py             # uv 子进程和联合依赖解析
        ├── ledger.py         # JSON 与持久 PEP 751 锁文件
        └── requirements.py   # requirements 输入指纹
```

声明、运行状态和部署记录分别由对应组件持有，`ModuleManager` 统一串行化公开操作。
组件不会单独注册到 `auto_init`；整体生命周期仍由模块管理器负责。
测试目录 `tests/kernel/module/` 对应模块管理实现，依赖相关测试集中在其 `dependencies/` 下。

```python
from alira.kernel.module import module_manager

manager = module_manager
await manager.install("./example.zip")  # 安装源码并管理依赖
await manager.install_package("package.yml")  # 整批安装源码，再执行全量同步
await manager.add("example")        # 管理选定模块及前置模块依赖
await manager.sync()                # 从源码全量重建并同步环境
await manager.start()               # 此时才导入并登记运行时模块
await manager.load("example")     # 离线只读校验依赖后启动
print(manager.loaded)
print(manager.dependencies)        # DependencyInfo: version、modules、ref_count、preinstalled
await manager.unload("example")   # 有其他模块依赖它时拒绝卸载
await manager.stop()               # 按依赖的逆序关闭所有运行服务，保留部署
await manager.remove("example")   # 释放依赖部署，保留源码；共享包保留至最后一个引用释放
```

`module_manager` 是进程内全局单例；`ModuleManager()` 返回同一个对象，重复构造不会重置其状态。
`await manager.start()` 只发现、登记运行时模块；`install("来源")` 安装源码和依赖，`add("名称")` 管理已有模块依赖，`sync()` 全量重建，`load("名称")` 校验后启动模块。
直接调用不带参数的 `load()` 会加载全部已登记模块；命令行未选择模块时不会调用它。
重复加载已运行模块不会重复启动或增加引用。`auto_init.start(restart=True)` 重启内核后仍需显式加载所需模块。
公开的部署、加载和卸载操作互斥，批内按依赖关系调度任务。加载失败时取消尚在执行的任务并清理
未成功启动的模块；已经启动成功的独立模块保留运行，可通过 `stop()` 统一关闭。
关闭失败时保留运行清理状态，调用 `unload()` / `stop()` 重试；部署移除失败时执行 `sync()` 从实际源码恢复环境。
`unload()` / `stop()` 只停止服务和释放运行资源，依赖包与部署引用保留到显式 remove。
它们不清除 `sys.modules`、Python 类定义或自动撤销 Apixis handler；模块必须在 shutdown 中自行注销 handler。
这不是热重载：同进程再次 load 会复用已导入的定义，升级代码或依赖后需要重启进程。
测试可通过私有 `_new_for_tests()` 工厂创建独立管理器/生命周期实例，不重置或丢弃正在运行的全局单例。

## web 模块与进程入口

`web/main.py` 只声明 `WebModule`。启动时才导入 `web/src/application.py` 和 `web/src/runtime.py`，
创建 FastAPI 应用并自动扫描 `alira.modules.web.src.router`，注册各子模块或子包导出的 `APIRouter`，
随后启动 Uvicorn，保留 `/health` 接口。路由文件统一放在 `src/alira/modules/web/src/router/`。
监听地址由 `web/config.yaml` 的 `base_url` 设置，默认端口为 2712。

```bash
uv run alira --modules web
```

通过 `--modules` 后的模块名称选择启动项，例如 `uv run alira --modules web example`。
`example` 要求存在声明名为 `example` 的标准模块；不可用入口不作为可选模块名称。
`uv run alira --help` 展示已登记模块，并列出不可用入口；声明发现不输出日志。
未知参数报错，不接受模块名缩写。
不带模块参数的 `uv run alira` 只运行内核，所有已发现模块都保持未加载状态。

CLI 先解析并校验命令行参数；管理命令直接执行并退出，不发现运行时模块，不进入内核生命周期。
应用运行路径才会启动内核服务，随后只读校验选中模块及其前置模块的部署依赖，按依赖顺序执行 startup()。
只有显式依赖管理操作或 --install-missing 会解析、安装依赖；
其缺失前置模块或 requirements 文件也不会阻止其他模块加载。

运行完整开发测试时显式提供所有业务模块依赖（汇总文件只供开发测试使用）：

```bash
uv run --with-requirements tests/requirements.txt pytest
```

`tests/kernel/module/test_isolated_commands.py` 构建当前 wheel，在独立解释器和临时用户目录中运行真实 `alira` 入口与 uv，
覆盖环境隔离、共享依赖、版本更新、损坏记录和元数据修复、解析/安装/卸载强制终止后的 sync，以及安装/卸载失败回滚。
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
所有服务按注册顺序启动、逆序停止，因此退出时先关闭模块，再关闭事件核心和日志服务。
Ctrl+C 和 SIGTERM 都会进入这一清理流程；Uvicorn 的最后一条退出日志在 `logger.stop()` 排空并关闭写入线程前产生，服务器无需额外的日志作用域。

发布前可执行 `python scripts/check_distribution.py dist/<wheel文件名>.whl` 核对发行名和规范版本。
