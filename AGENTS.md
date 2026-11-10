## Code Design Principles

### 1. Performance and Resource Efficiency

- Avoid unnecessary loops, repeated traversals, deep copies, and intermediate data structures.
- Prefer direct data access and lightweight transformations over redundant processing.
- Use deep copies only when independent mutable state is genuinely required.
- Do not introduce caching, batching, or other performance optimizations without a clear benefit.

### 2. Avoid Over-Defensive Programming

- Trust established upstream contracts and validated inputs.
- Do not repeatedly validate inputs or add fallback branches within internal methods when their preconditions are guaranteed by upstream logic.
- Avoid speculative exception handling, redundant null checks, and silent fallback values that conceal programming errors.
- Validate data at system boundaries; keep internal implementations focused on their explicit responsibilities.
- Add defensive handling only for realistic failure scenarios, not hypothetical misuse.

### 3. Clean and Maintainable Code

- Keep implementations simple, readable, and free of redundant logic, dead code, and unnecessary abstractions.
- Prefer straightforward control flow over excessive branching and deeply nested conditions.
- Search for and reuse existing functions before introducing new ones.
- Place module-owned reusable methods and public types in that module's `src/shared`; keep application-wide infrastructure in `src/alira/shared` and business-agnostic utilities in `src/alira/utils`.
- Avoid unnecessary wrappers, obsolete code, and compatibility layers that no longer serve a purpose.

### 4. Abstraction and Module Boundaries

- Place shared functionality at the lowest common architectural layer accessible to all intended consumers.
- When reusing functionality currently defined inside a specific module, evaluate whether it should be promoted to a shared module or an appropriate `utils` directory.
- Avoid cross-module dependencies on private implementation details.
- Keep domain-specific logic within its owning module; extract only functionality with genuine reuse potential.
- Preserve clear dependency direction and avoid circular dependencies.

### 5. Reuse Existing Framework Capabilities Provided by `my-llm-py` and `apixis`

- Prefer capabilities already provided by `my-llm-py` and `apixis` over custom implementations.
- Before implementing protocol adaptation, message processing, graph scheduling, event handling, or related infrastructure, check whether the existing dependencies already provide the required functionality.
- Avoid duplicating framework behavior or introducing parallel abstractions without a concrete requirement.
- When existing framework capabilities are sufficient, integrate them directly rather than wrapping or reimplementing them unnecessarily.

### 6. Layer Dependency Direction and Module Communication

- The layer order from lower to upper is `utils -> shared -> kernel -> modules`. Dependencies point toward lower layers:
  - `utils` must not depend on `shared`, `kernel`, or business modules.
  - `shared` may depend on `utils`, but must not depend on `kernel` or business modules.
  - `kernel` may depend on `shared` and `utils`, but must not import concrete business module implementations. Module discovery may import declarative `main.py` entries through `ModuleManager`.
  - Business modules may depend on `kernel`, `shared`, and `utils`.
- These boundaries apply to absolute imports, relative imports, dynamic imports, and imports under `TYPE_CHECKING`; do not bypass them by changing the import mechanism.
- Global `src/alira/shared` owns application-wide configuration, logging, and version support. Business-specific contracts remain owned by their module, even when other modules use them.
- Modules communicate through Apixis events. Business data requests, delivery, and results must not bypass event communication through direct calls to another module's internal services, caches, or mutable state.
- A module's `src/shared` only defines reusable methods and public types. Other modules may import these definitions, but must not use them to exchange runtime data or access the owning module's internal state. Instances of shared types carrying business data are transmitted through Apixis events.
- Declare runtime module dependencies through `BaseModule.required_modules` so prerequisite modules are ready before dependents start. Modules awaiting alignment retain their current implementation until their alignment is explicitly requested.
- Preserve dependency-direction and import-order checks in `tests/test_package_exports.py` when changing package exports or moving implementations.
