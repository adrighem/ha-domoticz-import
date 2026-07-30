# General Code Style

- Prefer clear, small units with explicit responsibilities.
- Keep protocol parsing strict and validate at trust boundaries.
- Use deterministic identifiers for cross-system resources.
- Avoid speculative abstraction and compatibility code.
- Keep comments focused on decisions and constraints.
- Preserve existing public behavior unless the active plan changes it.
- Never log credentials or secret-bearing payloads.
- Add regression coverage for every corrected defect when feasible.
