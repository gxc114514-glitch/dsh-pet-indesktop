"""ACP modules must be packaged for chat variants and omitted from no-chat variants."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SHELLS = {
    "linux": REPO / "scripts" / "build_linux.sh",
    "macos": REPO / "scripts" / "build_macos.sh",
}
PS1 = REPO / "scripts" / "build_onedir.ps1"
VARIANTS = ("webm-chat", "webm", "gif-chat", "gif")
ACP_MODULES = tuple(
    path.relative_to(REPO).with_suffix("").as_posix().replace("/", ".").removesuffix(".__init__")
    for path in sorted((REPO / "pet" / "chat" / "acp").glob("*.py"))
) + (
    "pet.chat.acp_provider",
    "pet.chat.permission_card",
    "pet.chat.acp_settings_section",
)


def _hidden_imports(args: list[str]) -> list[str]:
    return [value for flag, value in zip(args, args[1:]) if flag == "--hidden-import"]


def _assert_variant_args(args: list[str], variant: str) -> None:
    acp_imports = [module for module in _hidden_imports(args) if module in ACP_MODULES]
    excluded = [value for flag, value in zip(args, args[1:]) if flag == "--exclude-module"]
    if variant.endswith("-chat"):
        assert acp_imports == list(ACP_MODULES)
        assert "pet.chat" not in excluded
    else:
        assert acp_imports == []
        assert "pet.chat" in excluded


def _capture_shell_args(script: Path, variant: str, tmp_path: Path) -> list[str]:
    """Run only the real shell assembly; the Python stub stops at PyInstaller."""
    root = tmp_path / script.stem / variant
    (root / "scripts").mkdir(parents=True)
    (root / "packaging").mkdir()
    shutil.copy2(script, root / "scripts" / script.name)
    rapidocr = root / "rapidocr"
    rapidocr.mkdir()
    (rapidocr / "config.yaml").write_text("", encoding="utf-8")
    (rapidocr / "default_models.yaml").write_text("", encoding="utf-8")
    capture = root / "args.json"
    python_stub = root / "python-stub"
    python_stub.write_text(
        f"""#!{sys.executable}
import json
import os
import sys
from pathlib import Path

if sys.argv[1:3] == ["-m", "PyInstaller"]:
    Path(os.environ["ACP_ARGS_CAPTURE"]).write_text(json.dumps(sys.argv[3:]), encoding="utf-8")
    raise SystemExit(86)
if len(sys.argv) > 2 and sys.argv[1] == "-c" and "rapidocr" in sys.argv[2]:
    print(os.environ["FAKE_RAPIDOCR_DIR"])
""",
        encoding="utf-8",
    )
    python_stub.chmod(0o755)
    if script.name == "build_linux.sh":
        fcitx_stub = root / "scripts" / "build_pyside6_fcitx_plugin.sh"
        fcitx_stub.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        fcitx_stub.chmod(0o755)
    completed = subprocess.run(
        ["bash", str(root / "scripts" / script.name), "--variants", variant],
        cwd=root,
        env={
            **os.environ,
            "PYTHON_BIN": str(python_stub),
            "ACP_ARGS_CAPTURE": str(capture),
            "FAKE_RAPIDOCR_DIR": str(rapidocr),
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 86, completed.stdout + completed.stderr
    return json.loads(capture.read_text(encoding="utf-8"))


@pytest.mark.parametrize("variant", VARIANTS)
@pytest.mark.parametrize("platform", sorted(SHELLS))
def test_shell_variant_arguments(platform: str, variant: str, tmp_path: Path) -> None:
    args = _capture_shell_args(SHELLS[platform], variant, tmp_path)
    _assert_variant_args(args, variant)


def _ps_no_chat_array(text: str, variable: str, no_chat: bool) -> list[str]:
    match = re.search(
        rf"\${variable}\s*=\s*if \(\$noChat\) \{{\s*@\((.*?)\)\s*\}} "
        rf"else \{{\s*@\((.*?)\)\s*\}}",
        text,
        re.DOTALL,
    )
    assert match is not None, f"${variable} must select an array using $noChat"
    branch = match.group(1 if no_chat else 2)
    return re.findall(r"'([^']+)'", branch)


def _windows_variant_args(variant: str) -> list[str]:
    text = PS1.read_text(encoding="utf-8-sig")
    no_chat = not variant.endswith("-chat")
    args = _ps_no_chat_array(text, "acpHidden", no_chat)
    args += _ps_no_chat_array(text, "excludes", no_chat)
    command = text[text.index("python -m PyInstaller") : text.index("$entry\n")]
    assert "@acpHidden" in command, "The ACP array must reach the real PyInstaller command"
    assert "@excludes" in command
    return args


@pytest.mark.parametrize("variant", VARIANTS)
def test_windows_variant_arguments(variant: str) -> None:
    _assert_variant_args(_windows_variant_args(variant), variant)


def test_acp_module_inventory_is_complete() -> None:
    assert ACP_MODULES == (
        "pet.chat.acp",
        "pet.chat.acp.client",
        "pet.chat.acp.manager",
        "pet.chat.acp.presets",
        "pet.chat.acp.protocol",
        "pet.chat.acp.transport",
        "pet.chat.acp_provider",
        "pet.chat.permission_card",
        "pet.chat.acp_settings_section",
    )
