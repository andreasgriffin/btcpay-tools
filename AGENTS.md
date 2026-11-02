# Agent Guidelines

- write clean, maintainable, strong typing (type hints) code.  Never use `getattr` or `setattr`;
- Less code is better to understand the logic and to reduce bugs. Avoid boilerplate, and think about how to make changes in a simpler, straight forward way.
- modularize behavior into the most relevant class/module; do not centralize unrelated functionality in one class.
- avoid keyword-only `*` in method/function signatures unless explicitly requested.
- Before you commit, run pre-commit ruff format. commit and push the changes (use a dedicated branch for each session). If the pre-commit returns errors, fix them. For the pre-commit to work you have to cd into the current project and activate the environment.
- Ensure git hooks can resolve `python`: run commit/pre-commit commands with the project venv first on `PATH`, e.g. `PATH="$(poetry env info -p)/bin:$PATH" poetry run pre-commit run ruff-format --files <files>` and `PATH="$(poetry env info -p)/bin:$PATH" git commit -m "<message>"`.
- do not use someenum(str, Enum),  but instead use someenum(Enum), then (de)serailization works type safe via BaseSaveableClass
- do not use Callable arguments if possible
- take every change you do as an opportunity to improve architecture: think about if the change does introduce or remove coupling and what architecture improvements this could be used for 
- for GUI changes take into account that it should look nice in light and darkmode. (black) color in Icons should be named currrentColor (see bitcoin_safe/gui/icons/theme.csv) 
- for UI try to use existing classes that provide the styling to ensure a unified design across the application
