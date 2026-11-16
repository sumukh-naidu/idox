# CLAUDE.md: Python standards (mandatory)

You are working on a Python service of the IDP platform. Every line of Python you write or change **MUST** follow the standards in `PYTHON_STANDARDS.md` (imported below).

- If a request conflicts with a MUST rule, say so and follow the rule. Only the user can override it, citing the Python lead's approval.
- Make only the change that was asked for. Keep it simple: no layers or abstractions "for later".
- Before finishing, make sure `ruff format`, `ruff check`, `mypy` and `pytest` pass (run them, or ask the developer to). Then summarise what changed, why, how it was tested, and any security or personal-data impact.

@PYTHON_STANDARDS.md
