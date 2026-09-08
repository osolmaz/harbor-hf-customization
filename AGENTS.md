# Repository instructions

- Keep harness-specific behavior here, not in the Harbor-HF service.
- Use native Harbor source manifests, uv lockfiles, ACP, and documented Pi APIs.
- Pin executable dependencies and verify runtime payloads before publication.
- Never publish credentials, operator deployment values, private run records, or local paths.
- Do not run real model inference locally. Tests use fake protocol peers.
- Before paid remote work, verify task authorization, cost bounds, durable outputs, and cleanup.
- Use Python 3.12+, uv, Ruff, ty, pytest, and the configured Slophammer checks.
- Keep unit test coverage at or above 85%.
- Use Conventional Commits. Do not add coding-agent branding to commits.
- Do not merge a service integration until its explicitly authorized remote canary passes.
