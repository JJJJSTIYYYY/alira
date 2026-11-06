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
- Centralize genuinely reusable utility functions in the appropriate `utils` directory.
- Search for and reuse existing utilities before introducing new ones.
- Do not create utility wrappers that merely delegate to an existing function without adding meaningful functionality.
- Remove obsolete code and unnecessary compatibility layers when they are no longer required.

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