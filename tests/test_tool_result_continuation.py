from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nekro_agent.models.db_exec_code import ExecStopType
from nekro_agent.services.agent.tool_registry import ToolDoc, build_files, render_index
from nekro_agent.services.plugin.schema import SandboxMethodType
from nekro_agent.services.sandbox.runner import CODE_RUN_END_FLAGS, run_container_with_timeout


def test_index_document_reads_explicitly_request_continuation() -> None:
    docs = [ToolDoc(name="identify_image_character", module_name="character_vectors", method_type="tool")]
    index = render_index(docs)
    assert "print(open('/app/tools/index.md').read()); exit(9)" in index
    assert "print(open('/app/tools/by-plugin/<插件>/<工具>.md').read()); exit(9)" in index
    assert "单独 print 不会触发续答" in index
    files = build_files(docs)
    assert '"type": "tool"' in files["index.json"]


@pytest.mark.asyncio
async def test_print_exit_nine_returns_observation_with_manual_stop() -> None:
    process = subprocess.run(
        [sys.executable, "-c", 'print(\'{"status":"uncertain","character":null}\'); exit(9)'],
        capture_output=True, text=True, check=False,
    )
    assert process.returncode == 9
    container = SimpleNamespace(
        id="fake-only", wait=AsyncMock(), delete=AsyncMock(),
        log=AsyncMock(return_value=[process.stdout, CODE_RUN_END_FLAGS[ExecStopType.MANUAL]]),
    )
    output, stop_type = await run_container_with_timeout(container, timeout=2)
    assert stop_type is ExecStopType.MANUAL
    assert '"status":"uncertain"' in output
    container.delete.assert_awaited_once()


def _load_character_plugin() -> ModuleType:
    path = Path(os.environ["CHARACTER_PLUGIN_PATH"])
    spec = importlib.util.spec_from_file_location("_character_continuation_test", path, submodule_search_locations=[str(path.parent)])
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_character_tool_keeps_tool_semantics_and_explains_continuation(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_character_plugin()
    method = next(method for method in module.plugin.sandbox_methods if method.func.__name__ == "identify_image_character")
    assert method.method_type is SandboxMethodType.TOOL
    monkeypatch.setattr(module.plugin, "get_config", lambda _: SimpleNamespace(API_KEY="fake-not-used"))
    prompt = await module.prompt(None)
    assert "print(result)" in prompt and "exit(9)" in prompt and "send_msg_text" in prompt
    assert "exit(9)" in method.func.__doc__