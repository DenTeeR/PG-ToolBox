"""Extract routes and input fields from an official, checked-out panel release.

Usage: python scripts/update_api_contract.py /path/to/PasarGuard-panel
No panel packages, database, or services are imported or started.
"""

import ast
import json
import subprocess
import sys
from pathlib import Path


def extract(root):
    models = {}
    for file in (root / "app/models").glob("*.py"):
        tree = ast.parse(file.read_text())
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            fields, required = [], []
            for statement in node.body:
                if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
                    fields.append(statement.target.id)
                    if statement.value is None:
                        required.append(statement.target.id)
            models[node.name] = {
                "bases": [base.id for base in node.bases if isinstance(base, ast.Name)],
                "fields": fields,
                "required": required,
            }
    keep = {
        "UserCreate",
        "UserModify",
        "AdminCreate",
        "AdminModify",
        "NodeModify",
        "SettingsSchema",
        "UsersResponse",
        "NodesResponse",
        "GroupsResponse",
        "AdminsResponse",
        "AdminRolesResponse",
        "UserSubscriptionUpdateList",
        "UserSubscriptionUpdateSchema",
        "UserSubscriptionUpdateChartSegment",
    }
    pending = list(keep)
    while pending:
        for parent in models.get(pending.pop(), {}).get("bases", []):
            if parent in models and parent not in keep:
                keep.add(parent)
                pending.append(parent)
    models = {name: model for name, model in models.items() if name in keep}
    routes = []
    for file in (root / "app/routers").glob("*.py"):
        tree = ast.parse(file.read_text())
        prefix = ""
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                if isinstance(node.value.func, ast.Name) and node.value.func.id == "APIRouter":
                    prefix_arg = next((k.value for k in node.value.keywords if k.arg == "prefix"), None)
                    if isinstance(prefix_arg, ast.Constant):
                        prefix = prefix_arg.value
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if (
                    isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and isinstance(decorator.func.value, ast.Name)
                    and decorator.func.value.id == "router"
                    and decorator.func.attr in {"get", "post", "put", "delete"}
                    and decorator.args
                    and isinstance(decorator.args[0], ast.Constant)
                ):
                    path = prefix + decorator.args[0].value
                    if path.startswith("/api/"):
                        routes.append({"method": decorator.func.attr.upper(), "path": path})
    commit = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    return {
        "source": "https://github.com/PasarGuard/panel",
        "tag": "v5.6.0",
        "commit": commit,
        "routes": sorted(routes, key=lambda route: (route["path"], route["method"])),
        "models": models,
    }


if __name__ == "__main__":
    output = Path(__file__).resolve().parents[1] / "tests/contracts/pasarguard-v5.6.0.json"
    output.write_text(json.dumps(extract(Path(sys.argv[1])), indent=2) + "\n")
