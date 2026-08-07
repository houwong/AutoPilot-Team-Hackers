param(
    [string]$ApiUrl = $(if ($env:NEXT_PUBLIC_API_URL) { $env:NEXT_PUBLIC_API_URL } else { "http://localhost:8001" }),
    [string]$Token = $env:QUEUE_TICK_TOKEN
)

$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($Token)) {
    $envFile = Join-Path $PSScriptRoot "..\.env"
    if (Test-Path -LiteralPath $envFile) {
        $line = Get-Content -LiteralPath $envFile | Where-Object { $_ -match '^QUEUE_TICK_TOKEN=' } | Select-Object -First 1
        if ($line) { $Token = $line.Substring('QUEUE_TICK_TOKEN='.Length).Trim() }
    }
}
if ([string]::IsNullOrWhiteSpace($Token)) {
    throw "QUEUE_TICK_TOKEN is not configured. Refusing to call the queue endpoint."
}

$headers = @{ "X-Queue-Token" = $Token }
$response = Invoke-RestMethod `
    -Method Post `
    -Uri "$($ApiUrl.TrimEnd('/'))/api/queue/tick" `
    -Headers $headers `
    -ContentType "application/json" `
    -Body "{}"

$response | ConvertTo-Json -Depth 8
