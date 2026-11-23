# Alira 模块开发指南

> 面向新模块作者，对齐当前 Alira 模块契约与 Apixis **3.0.0**。
> 通信接口统一从 `alira.kernel.comlink` 导入；内核负责事件运行时的启动和停止。

## 0. 先看全景

```text
你的模块                                  其他模块
┌─────────────┐       发布业务事件         ┌─────────────┐
│ business.py │ ───────────────────────▶ │ business.py │
│             │       接收业务事件         │             │
│             │ ◀─────────────────────── │             │
└──────┬──────┘                          └──────┬──────┘
       │ 只导入公开类型与可复用方法                 |
       └─────────────▶ src/shared/ ◀────────────┘
```

模块作者需要声明生命周期、发布事件、订阅事件。跨模块业务请求、数据交付和结果返回都经过事件；
其他模块的内部服务、缓存、单例和可变状态不作为调用入口。

Apixis 3.0.0 提供以下选择：

| 场景 | 接口 | 完成条件 |
| --- | --- | --- |
| 发通知 | `post_event` | 事件入队，可能等待队列容量 |
| 发布并等待本次事件处理完 | `post_and_wait` | 本次事件的前台处理器链执行完 |
| 通过上下文注册监听并等待事件 | `expect_event` | 进入上下文即完成监听注册，收集匹配事件 |
| 只等待其他地方产生的事件 | `wait_event` | 协程开始执行后注册，收到匹配事件 |

`post_and_wait` 按生成的 `event_id` 关联结果，同名并发发布不会串单。
需要独立回复事件承载返回值时，仍要在业务契约中关联请求与回复。

本文的 `echo` 模块和事件都是新模块示例，不代表现有 Agent、工具或数据库已提供这些事件。
当前标准模块入口以源码为准；不要仅凭目录存在就假定模块已经实现生命周期和业务订阅。

## 1. 建一个最小模块

先生成模块骨架：

```bash
uv run alira init echo
```

命令在当前 Alira 包的模块目录下创建 `echo`，输出实际路径；已有同名文件或目录时会报错并保留原内容。
生成的 `main.py` 声明版本为 `0.1.0`，预留 `startup()` / `shutdown()` / `health()` 钩子，
`requirements.txt` 为空，`src/shared/events.py` 预留事件契约。初始化不导入模块、不安装依赖，也不写依赖记录。
下文展示如何填充骨架并添加 `src/runtime.py`，完成一个接收并输出文本的 Echo 模块。

在源码仓库中使用以下目录；安装后的对应目录为 `alira/modules/echo/`。

```text
src/alira/modules/echo/
├── __init__.py
├── main.py
├── requirements.txt
└── src/
    ├── __init__.py
    ├── shared/
    │   ├── __init__.py
    │   └── events.py
    └── ...
```

所有 `__init__.py` 只负责包标记和接口导出，不执行注册或业务初始化。
本例没有额外 Python 依赖，`requirements.txt` 留空；需要配置时再添加模块自己的 `config.py`。

### `main.py`：声明与生命周期

```python
from alira.kernel.comlink import is_registered
from alira.kernel.module import BaseModule, ModuleStatus

__version__ = "0.1.0"


class EchoModule(BaseModule):
    def __init__(self) -> None:
        super().__init__(
            name="echo",
            version=__version__,
            description="Echo event example",
            required_modules=[],
    required_kernel=">=3.0.0",
        )
        self._runtime = None

    async def startup(self) -> None:
        from alira.modules.echo.src.runtime import EchoRuntime

        self._runtime = EchoRuntime()
        await self._runtime.start()

    async def shutdown(self) -> None:
        if self._runtime is not None:
            await self._runtime.stop()
            self._runtime = None

    async def health(self) -> ModuleStatus:
        """Report whether the Echo event handler is registered."""
        healthy = self._runtime is not None and is_registered("echo_on_received")
        return {
            "health": healthy,
            "message": "" if healthy else "Echo handler is not registered.",
        }


module = EchoModule()
```

入口必须导出一个已完成 `BaseModule.__init__()` 的实例，名称与模块目录一致。
构造方法只声明元数据和初始状态，**不初始化业务资源**。

- `startup()` 导入业务实现、注册订阅并获取资源，返回表示模块就绪。模块仅在进程启动时按需加载一次。
- `shutdown()` 停止业务、注销订阅并释放资源，需支持启动失败或取消后的部分清理。失败模块立即清理，成功模块在应用退出时按启动顺序的逆序关闭；清理异常即时记录后继续关闭其他模块，不自动重试。
- `health()` 检查模块自有资源，返回 `health: bool` 与 `message: str`，支持启动前和关闭后调用。
  基类默认返回 `health=False`，并在 `message` 中说明尚未实现检查；上例根据 Echo 处理器的注册状态报告健康。

管理器不会自动轮询健康状态。`await module_manager.check_health(*names)` 汇总已登记模块的检查结果，
不传名称时检查全部已登记模块；单个健康钩子抛出异常时，该模块返回 `health=False` 和错误说明。
聚合检查与加载、卸载互斥，健康钩子不能调用会获取管理器操作锁的接口；检查超时由调用方控制。

### `src/shared/events.py`：公开事件契约

```python
from dataclasses import dataclass

# EchoRuntime 订阅；发布者发送需要输出的文本。
ECHO_RECEIVED = "alira.echo.received"


@dataclass(frozen=True, slots=True)
class EchoReceived:
    text: str
```

### `src/runtime.py`：订阅与处理

```python
from alira.kernel.comlink import ApixEvent, subscribe, unsubscribe
from alira.modules.echo.src.shared.events import ECHO_RECEIVED, EchoReceived


class EchoRuntime:
    async def start(self) -> None:
        subscribe(ECHO_RECEIVED, group="echo", exist_ok=False)(self.echo_on_received)

    async def stop(self) -> None:
        unsubscribe(group="echo")

    async def echo_on_received(self, event: ApixEvent) -> None:
        payload: EchoReceived = event.context
        print(payload.text)
```

可使用模块专属的 `group` 统一注销；处理器名仍在整个进程中唯一，组不会隔离同名处理器。
`exist_ok=False` 会让重复注册明确报错，便于发现生命周期或命名冲突。

依赖部署和启动是两个步骤：

```bash
uv run alira add echo
uv run alira run echo
```

模块名称是 `run` 后的位置参数，可一次选择多个；例如 `uv run alira run echo reporter`。
需要在启动前显式安装缺失依赖时，追加 `--install-missing` 或其简写 `-i`。

`add` 管理模块 Python 依赖，运行期默认只验证已部署依赖；启动不会自动安装缺包。
部署操作的完整说明见[标准模块与依赖管理](README.md)。

## 2. 发通知：`post_event`

```python
from alira.kernel.comlink import EventType, post_event
from alira.modules.echo.src.shared.events import ECHO_RECEIVED, EchoReceived


async def notify_echo(text: str) -> None:
    await post_event(
        event_type=EventType.WORKFLOW,
        event_name=ECHO_RECEIVED,
        context=EchoReceived(text=text),
    )
```

`post_event` 等待队列接纳事件，队列满时会等待；返回不表示处理器已执行，也不会把处理器异常抛回调用方。
需要限制发布等待时间时，在调用外使用 `asyncio.timeout(...)`。

`event_type` 使用 `EventType` 枚举。一般业务流程可用 `WORKFLOW`，高频或带唯一后缀的事件可用 `TRANSIENT`。
`TRANSIENT` 正常分发，但不记录事件名；它不表示后台执行或低优先级。
3.0.0 的直接发布接口默认使用 `EventType.DEFAULT`，示例显式标明业务分类。

## 3. 等待处理完成或事件到达

### 3.1 同一个事件处理完：`post_and_wait`

```python
from alira.kernel.comlink import EventType, post_and_wait
from alira.modules.echo.src.shared.events import ECHO_RECEIVED, EchoReceived


async def echo_and_confirm(text: str) -> None:
    event = await post_and_wait(
        event_type=EventType.WORKFLOW,
        event_name=ECHO_RECEIVED,
        context=EchoReceived(text=text),
        time_out=5.0,
    )
    if event is None:
        raise RuntimeError("Event processing was cancelled or the core closed")
    if event.has_error:
        raise RuntimeError(f"Echo processing failed: {event.error_stack}")
```

`post_and_wait` 发布一个事件，等待其处理结果并返回处理后的事件；事件取消或核心关闭时返回 `None`。

| 参数 | 说明 |
| --- | --- |
| `event_type` | 事件分类，使用 `EventType` 枚举，默认 `EventType.DEFAULT`；示例为 `EventType.WORKFLOW`。 |
| `event_name` | 必填，发布的事件名；示例为 `ECHO_RECEIVED`。按生成的 `event_id` 等待本次事件，同名并发事件不会混淆结果。 |
| `context` | 事件携带的业务数据，默认 `None`；示例为 `EchoReceived(text=text)`。 |
| `time_out` | 等待秒数，覆盖事件入队和前台处理的总时间，默认 `None` 表示无限等待。默认超时抛出 `TimeoutError`，不等待后台任务或处理器另行发布的事件。 |

**超时或取消等待不会撤回已经入队的事件，也不会取消其处理器。**
涉及写入或重试时，由业务定义幂等键、重复请求处理和取消协议。

### 3.2 先注册监听，再等待事件：`expect_event`

进入上下文时完成监听注册，等待其他地方产生的匹配事件：

```python
from alira.kernel.comlink import expect_event
from alira.modules.echo.src.shared.events import ECHO_RECEIVED


async def observe_echo(expected_text: str) -> str:
    async with expect_event(
        ECHO_RECEIVED,
        point="processed",
        predicate=lambda event: event.context.text == expected_text,
        time_out=5.0,
    ) as waiter:
        event = await waiter.wait()
    if event is None:
        raise RuntimeError("Event processing was cancelled or the core closed")
    if event.has_error:
        raise RuntimeError(f"Echo processing failed: {event.error_stack}")
    return event.context.text
```

| 参数 | 说明 |
| --- | --- |
| `event_name` | 必填，区分大小写的事件名或通配符模式；示例为 `ECHO_RECEIVED`。字符串返回一个事件，非空列表或集合返回以模式为键的事件字典。 |
| `point` | `"received"` 表示在普通前台处理器执行前匹配；`"processed"` 表示在前台处理完成后匹配，不等待后台任务。默认 `"received"`。 |
| `filter` | 要排除的事件名模式，支持字符串或字符串列表；默认不排除。 |
| `predicate` | 同步事件筛选函数，返回 `True` 时接受事件，返回 `False` 时忽略；默认不筛选。示例只接受文本等于 `expected_text` 的事件。 |
| `time_out` | 从注册时开始计时的秒数，默认 `None` 表示无限等待。`get_result()` 沿用此截止时间；默认超时抛出 `TimeoutError`，不会取消上下文中正在执行的操作。 |

### 3.3 只等待别人产生的事件：`wait_event`

```python
from alira.kernel.comlink import ApixEvent, wait_event
from alira.modules.echo.src.shared.events import ECHO_RECEIVED


async def wait_for_echo() -> ApixEvent | None:
    return await wait_event(ECHO_RECEIVED, point="processed", time_out=5.0)
```

`wait_event` 从协程实际开始执行时注册，不重放已经错过的事件。
其参数与第 3.2 节相同，`time_out` 从注册时开始计时，超时抛出 `TimeoutError`。
`asyncio.create_task(wait_event(...))` 返回时不保证已经注册；需要明确监听已就绪时，使用 `expect_event`。

### 3.4 等待器与清理

- 返回的 `ApixEvent` 是原始可变对象，不是快照。
- 每个模式只保留首次匹配，全部模式匹配后才返回结果；列表或集合即使只有一个模式也返回字典。
- `waiter.wait()` 与 `get_result()` 使用注册时固定的截止时间，不接收超时参数，也不会重置预算。
- 正常完成、超时、取消和退出上下文都会清理监听；`predicate` 抛出异常也会使等待失败并清理监听。
- 匹配事件被取消或核心关闭时返回 `None`；等待任务取消和显式关闭未完成等待器仍抛出 `CancelledError`。

## 4. 订阅与生命周期

推荐在运行时 `start()` 注册，在 `stop()` 按模块组清理，再由模块 `shutdown()` 调用。
不要在导入阶段使用装饰器注册业务订阅。

| 参数 | 用途与约束 |
| --- | --- |
| `group="echo"` | 模块拥有的订阅组；`unsubscribe(group="echo")` 批量移除 |
| `exist_ok=False` | 同名处理器冲突时报错；默认 `True` 会替换原注册 |
| `priority=10` | 数值越大越先执行；默认值为 1，同级按注册顺序 |
| `between_handlers=(None, "echo_on_received")` | 插在已注册处理器之前；不能同时指定 `priority` |
| `time_out=5.0` | 限制每次处理器回调执行；不限制等待事件到来的时间 |
| `stop_when_error=True` | 事件已有上游错误时跳过当前处理器的核心回调 |
| `background=True` | 不等待此处理器完成；适合允许独立完成的观测任务 |

前台处理器异常记录在 `error_stack`；后台失败会记录日志，但不进入该事件的 `error_stack`。
因此需要发布方确认的存储、索引等副作用应使用前台处理器，或设计明确的业务完成回复。
降低 `priority` 只影响执行顺序，不表示失败可以被忽略。

注销影响后续取出的事件，已经捕获处理器链的事件可能继续执行。
模块仍需在关闭时停止或等待自己拥有的任务、关闭连接，再让内核停止事件核心。
内核 `ApixisCore.start()` 封装 `create_core() → start_core()`，返回时核心为 `RUNNING`，然后才启动模块。
`stop()` 封装 `stop_core() → close_core()`，返回时为 `CLOSED`，活动事件工作和等待器均已清理。
关闭后再启动会创建新核心，原订阅和编译图不能继续复用。
不要在模块里调用全局 `create_core()` / `start_core()` / `stop_core()` / `close_core()`，否则会影响其他模块。

通信接口在核心所属的 asyncio 循环调用。Apixis 3.0.0 不再提供工作线程桥接接口。

## 5. `shared` 与模块边界

模块的 `src/shared` 定义可供复用的方法和公共类型，业务数据实例仍通过事件传递。

| 可以放 | 不应放 |
| --- | --- |
| dataclass、TypedDict、Enum、Protocol、事件名常量 | 私有服务、缓存、运行中的业务单例 |
| 具有真实复用需求的方法、公共基类 | 用来绕过事件通信读取内部状态的函数或对象 |
| 事件 payload、请求与结果的公开契约 | 模块启动、订阅注册、实际业务请求处理 |

`shared` 可以包含实现方法，并依赖本模块其他共享定义或其他模块的公开共享契约，
但不能依赖私有实现或形成循环依赖。
跨模块共享依赖也不能成为传递运行时业务数据的旁路。

```python
from alira.modules.echo.src.shared.events import EchoReceived  # 公开契约
```

其他模块不直接导入 `EchoRuntime`、读取 Echo 模块的运行时实例或调用其内部业务方法。
公共类型可以导入，携带业务数据的实例通过事件交付。

## 6. 事件契约与依赖声明

### 事件契约

事件名建议使用 `alira.<模块>.<领域>.<动作>`，常量定义在拥有该业务契约的模块 `src/shared` 中。
例如，事实通知可以使用 `created`、`persisted`；请求和回复可以使用 `requested`、`replied`。

- 写明 payload 类型、订阅者或预期消费者、回复格式、失败语义，以及是否要求幂等。
- 示例使用 `frozen=True` 的 dataclass。它只阻止字段重新赋值，不会冻结字段内部的列表或字典；
  订阅方按只读契约使用，不通过修改 payload 偷渡返回值。
- 通配符用于契约允许的请求/回复前缀或观测范围，避免用 `alira.*` 接管无关业务。

### 运行时前置模块

依赖 Echo 服务的消费模块可以在自己的声明中写：

```python
from alira.kernel.module import BaseModule

module = BaseModule(name="reporter", version="0.1.0", required_modules=["echo"])
```

`alira run reporter` 会先使 `echo` 就绪，再启动 `reporter`；关闭时先关闭依赖方。
单独启动 `echo` 不会反向启动 `reporter`。`required_modules` 填模块名，不能制造循环依赖。

缺少声明意味着管理器不知道这个顺序约束；它不会自动从事件名或 Python import 推导依赖。
声明解决启动顺序，事件和明确回复解决运行时因果。它不会自动验证对方实现了某个事件契约。

## 7. 常见错误

| 错误 | 后果 | 正确做法 |
| --- | --- | --- |
| 在 `main.py` 顶部导入业务实现 | 扫描或导入声明时提前加载可选依赖 | 在 `startup()` 内导入 |
| 将 `stop()` 作为模块清理钩子 | 模块关闭时没有调用自定义清理 | 覆盖 `shutdown()` |
| `post_event` 返回后就认为已保存 | 只完成入队 | 使用 `post_and_wait` 并检查错误，或等待业务回复 |
| 只按固定事件名收集并发处理结果 | 可能拿到另一请求的事件 | 同事件处理使用 `post_and_wait`；独立回复使用关联 ID |
| 给 `waiter.wait()` 传 `time_out` | 最新接口不接收此参数 | 在 `expect_event(..., time_out=...)` 注册时设置截止时间 |
| 将等待超时当作操作已取消 | 已入队操作仍可能继续 | 定义业务取消、幂等与重试策略 |
| 同名处理器使用默认 `exist_ok=True` | 原处理器可能被替换 | 模块前缀命名、`exist_ok=False`、专属组清理 |
| 必须完成的副作用设为后台处理 | 完成等待和 `error_stack` 无法确认它 | 使用前台处理或显式完成回复 |
| 跨模块读取单例、缓存或私有服务 | 绕过事件契约 | 通过公开类型构造事件请求 |

## 8. 提交前自查

- [ ] 入口名称与目录一致，并导出一个初始化完成的 `BaseModule` 实例。
- [ ] 业务导入与订阅在 `startup()` 内进行，`shutdown()` 能清理部分启动状态。
- [ ] Python 依赖写入模块 `requirements.txt`，前置模块声明在 `required_modules`。
- [ ] 通信 API 从 `alira.kernel.comlink` 导入。
- [ ] 处理器名具有模块前缀、使用专属组，关闭时注销并清理自有任务。
- [ ] 等处理使用 `post_and_wait`，检查 `has_error`，并设置明确的等待预算。
- [ ] 若有独立回复，在业务契约中明确关联条件，并说明超时后原操作可能继续。
- [ ] `src/shared` 只暴露可复用方法与公共类型，不暴露内部服务或状态访问旁路。
- [ ] 模块测试验证自己的契约、参数传递和业务结果，不重复验证 Apixis 内部实现。

## 9. 一页速查

| 需求 | 写法 |
| --- | --- |
| 声明模块 | `main.py` 导出 `module = MyModule(...)` |
| 初始化源码骨架 | `uv run alira init <name>` |
| 获取、清理资源 | `startup()` / `shutdown()` |
| 报告模块健康 | `await module.health()`，返回 `ModuleStatus` |
| 汇总模块健康 | `await module_manager.check_health(*names)`，返回 `dict[str, ModuleStatus]` |
| 导入通信 API | `from alira.kernel.comlink import ...` |
| 发通知 | `await post_event(...)` |
| 发事件并等前台完成 | `await post_and_wait(..., time_out=5.0)`，检查 `has_error` |
| 通过上下文监听事件 | `async with expect_event(name, time_out=5.0) as waiter: ...`，使用 `wait()` 读取事件 |
| 只等一个外部事件 | `await wait_event(name, time_out=5.0)` |
| 整次请求限制时间 | 外层 `asyncio.timeout(...)` |
| 批量清理订阅 | `unsubscribe(group="模块名")` |
| 跨模块使用契约 | 导入对方 `src/shared` 的公共定义，业务数据走事件 |
| 确定依赖模块与启动顺序 | `required_modules` |
| 查询事件运行时概况 | 核心所属循环调用 `get_event_runtime_status()`，不启动或修改核心 |

模块可以声明 `required_modules=["agent>=1.0,<2.0"]` 与 `required_kernel=">=3.0.0,<4"`。
模块版本须使用静态字符串或顶层常量，kernel 默认要求为 `>=3.0.0`。版本不满足、消费者约束冲突或循环依赖，均在依赖解析阶段报错，任何模块的 `startup()` 都不会被调用。
