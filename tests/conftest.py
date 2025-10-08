import importlib
import sys

# Ensure real optional dependencies are imported before tests that install stubs
# so that available packages (like ebooklib, bs4) aren't replaced with dummy modules.
for module_name in ("ebooklib", "bs4"):
    if module_name not in sys.modules:
        try:
            importlib.import_module(module_name)
        except Exception:
            # On environments without the optional dependency, downstream tests
            # will install lightweight stubs as needed.
            pass
