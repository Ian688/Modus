import ast
from pathlib import Path


def test_mode_orchestration_lives_in_leaf_runner_modules():
    root = Path(__file__).parents[1] / "src/modus/desktop"
    for module, functions in {
        "default_runner.py": {"stream_to_ws"},
        "moa_runner.py": {"run_moa_stream"},
        "peri_runner.py": {"run_peri_stream"},
    }.items():
        tree = ast.parse((root / module).read_text())
        names = {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        assert functions <= names


def test_runners_do_not_import_websocket_server_module():
    root = Path(__file__).parents[1] / "src/modus/desktop"
    for module in ("default_runner.py", "moa_runner.py", "peri_runner.py"):
        assert "modus.desktop.server" not in (root / module).read_text()
