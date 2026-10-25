"""验证插件 main.py 能以 AstrBot 的模块路径（data.plugins.<name>.main）导入。

用法：
    python tools/check_import.py <AstrBot 核心目录>

    # 例：
    python tools/check_import.py C:/Users/me/.astrbot/core

不传参数时读环境变量 ASTRBOT_CORE_DIR。
"""

import importlib.util
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_NAME = "astrbot_plugin_napcat"


def main(argv: list[str]) -> int:
    core = argv[1] if len(argv) > 1 else os.environ.get("ASTRBOT_CORE_DIR", "")
    if not core:
        print(__doc__)
        return 2
    core = os.path.abspath(core)
    sys.path.insert(0, core)

    plugin_main = os.path.join(REPO_ROOT, "main.py")
    spec = importlib.util.spec_from_file_location(
        f"data.plugins.{PLUGIN_NAME}.main", plugin_main
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    classes = [c for c in dir(module) if c.endswith(("Plugin", "Tool"))]
    print("IMPORT_OK:", classes)
    assert "NapcatPlugin" in classes, "缺少 NapcatPlugin"
    assert "OneBotActionTool" in classes, "缺少 OneBotActionTool"
    print("ALL_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
