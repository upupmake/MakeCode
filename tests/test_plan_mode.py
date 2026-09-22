from utils.plan_mode import (
    PLAN_MODE_ALLOWED_COMMANDS,
    is_plan_mode_command_allowed,
)


def test_plan_mode_allows_common_read_only_search_commands():
    for command in (
        "grep -n needle src",
        "rg -n needle src",
        "find /workspace -type f -name '*.py'",
        "ls -la /workspace",
        "cat README.md",
        "head -n 20 main.py",
        "git status",
        "pwd",
        "stat main.py",
        "wc -l prompts.py",
        "diff a.py b.py",
        "jq . package.json",
        "findstr /n needle src\\app.py",
        "Get-ChildItem -Recurse -File",
        "gci -Recurse -File",
        "Select-String -Path *.py -Pattern needle",
        "sls -Path *.py -Pattern needle",
        "Get-Content README.md",
        "gc README.md",
        "Get-Item main.py",
        "Test-Path main.py",
        "Resolve-Path .",
        "Get-Location",
        "tasklist",
        "systeminfo",
        "where python",
        "C:\\Windows\\System32\\findstr.exe /n needle file.txt",
        "FINDSTR /n needle file.txt",
        '"C:\\Windows\\System32\\findstr.exe" /n needle file.txt',
        '"C:\\Program Files\\findstr.exe" /n needle file.txt',
    ):
        assert is_plan_mode_command_allowed(command), command


def test_plan_mode_still_blocks_write_and_destructive_prefixes():
    for command in (
        "rm -rf tmp",
        "mv a.py b.py",
        "cp a.py b.py",
        "chmod 777 secret",
        "chown root file",
        "kill 1",
        "sudo ls",
        "bash -c 'echo hi'",
        "sh -c 'echo hi'",
        "curl https://example.com",
        "wget https://example.com",
        "python3.13 -c 'print(1)'",
        "echo hello",
        "touch new.txt",
        "mkdir out",
        "tee out.txt",
        "python -c 'print(1)'",
        "node -e 'console.log(1)'",
        "less README.md",
        "more README.md",
        "top",
        "htop",
        "Set-Content out.txt hi",
        "Set-ItemProperty HKCU:\\Env Name Value",
        "New-Item out.txt",
        "Remove-Item tmp",
        "Copy-Item a.py b.py",
        "Move-Item a.py b.py",
        "Rename-Item a.py b.py",
        "Out-File out.txt",
        "Invoke-WebRequest https://example.com",
        "Invoke-Expression Get-Date",
        "iex Get-Date",
        "Start-Process notepad",
        "Stop-Process -Name python",
        "del tmp.txt",
        "erase tmp.txt",
        "copy a.py b.py",
        "move a.py b.py",
        "ren a.py b.py",
        "rd tmp",
        "powershell -Command Get-Date",
        "pwsh -Command Get-Date",
        "cmd /c dir",
        "select",
        "",
    ):
        assert not is_plan_mode_command_allowed(command), command


def test_plan_mode_allowed_commands_include_search_tools():
    for prefix in ("grep", "rg", "find", "ls", "cat", "head", "git"):
        assert prefix in PLAN_MODE_ALLOWED_COMMANDS
    for prefix in (
        "findstr",
        "Get-ChildItem",
        "Select-String",
        "Get-Content",
        "Test-Path",
    ):
        assert prefix in PLAN_MODE_ALLOWED_COMMANDS
