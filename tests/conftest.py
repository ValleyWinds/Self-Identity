"""隔离加载 plugin.py 供测试使用（不需要 MaiBot 宿主）。

本插件是单文件、无相对导入，直接 spec_from_file_location 即可；
多文件插件需要 submodule_search_locations + __path__ 的完整姿势，
见 maibot-plugin-dev 技能 references/isolated-import-testing.md。

跑法（借用本地 MaiBot 实例 venv，SDK / pillow / pytest 全有）：

    uv run --project D:/ZCode/Code/MaiBot-Project/MaiBot python -m pytest tests -q
"""

import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).parents[1]


@pytest.fixture(scope="session")
def plugin_module():
    spec = spec_from_file_location("valleywinds_self_identity", PLUGIN_DIR / "plugin.py")
    module = module_from_spec(spec)
    sys.modules["valleywinds_self_identity"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def plugin(plugin_module):
    return plugin_module.create_plugin()
