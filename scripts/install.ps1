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

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "Need uv to install xian-cli"
}

Invoke-Step @("uv", "tool", "install", "--force", $packageSpec)
Write-Host "Installed $packageSpec. Run 'xian --help' to verify the CLI."
