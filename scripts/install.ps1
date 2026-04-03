[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$packageName = if ($env:XIAN_CLI_PACKAGE_NAME) {
    $env:XIAN_CLI_PACKAGE_NAME
} else {
    "xian-tech-cli"
}
$version = if ($env:XIAN_CLI_VERSION) {
    $env:XIAN_CLI_VERSION.TrimStart("v")
} else {
    ""
}
$packageSpec = if ($version) {
    "$packageName==$version"
} else {
    $packageName
}
$dryRun = $env:XIAN_CLI_DRY_RUN -eq "1"

function Invoke-Step {
    param([string[]]$Command)

    Write-Host ">>> $($Command -join ' ')"
    if ($dryRun) {
        return
    }

    & $Command[0] @($Command[1..($Command.Length - 1)])
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code $LASTEXITCODE"
    }
}

function Get-PythonCommand {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        return @("py", "-3")
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        return @("python")
    }
    return $null
}

if (Get-Command uv -ErrorAction SilentlyContinue) {
    Invoke-Step @("uv", "tool", "install", "--force", $packageSpec)
} elseif (Get-Command pipx -ErrorAction SilentlyContinue) {
    Invoke-Step @("pipx", "install", "--force", $packageSpec)
} else {
    $python = Get-PythonCommand
    if (-not $python) {
        throw "Need one of: uv, pipx, py, or python"
    }

    Invoke-Step @($python + @("-m", "pip", "install", "--user", "--upgrade", $packageSpec))

    $scriptsDir = & $python[0] @($python[1..($python.Length - 1)] + @(
        "-c",
        "import site, sys; from pathlib import Path; print(Path(site.USER_BASE) / ('Scripts' if sys.platform == 'win32' else 'bin'))"
    ))
    Write-Host "Installed with user-site pip. Add $scriptsDir to PATH if xian is not available yet."
}

Write-Host "Installed $packageSpec. Run 'xian --help' to verify the CLI."
