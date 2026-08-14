# Regular package on purpose: as a namespace package, `tests` loses to
# any stray regular `tests` package in site-packages (Python resolves
# those first), which breaks `from tests.test_ui_runner import ...`.
