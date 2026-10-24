param(
    [string]$TestDatabaseUrl = $env:TEST_DATABASE_URL
)

if ([string]::IsNullOrWhiteSpace($TestDatabaseUrl)) {
    throw "Set TEST_DATABASE_URL to a disposable PostgreSQL database named ap_test."
}

$parsed = [Uri]$TestDatabaseUrl.Replace("postgresql+psycopg://", "postgresql://")
if (-not $parsed.AbsolutePath.TrimStart('/').Equals("ap_test", [StringComparison]::Ordinal)) {
    throw "TEST_DATABASE_URL must target a database named exactly ap_test."
}

$composeFile = Join-Path $PSScriptRoot "..\docker-compose.yml"
$postgresUser = if ([string]::IsNullOrWhiteSpace($env:POSTGRES_USER)) {
    "ap_user"
} else {
    $env:POSTGRES_USER
}
$databaseExists = docker compose -f $composeFile exec -T postgres psql -U $postgresUser -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = 'ap_test'"
if ($LASTEXITCODE -ne 0) {
    throw "Could not check the compose PostgreSQL service. Start it before running scenarios."
}
if (($databaseExists | Out-String).Trim() -ne "1") {
    docker compose -f $composeFile exec -T postgres createdb -U $postgresUser ap_test
    if ($LASTEXITCODE -ne 0) {
        throw "Could not create the disposable ap_test database."
    }
}

docker compose -f $composeFile exec -e "TEST_DATABASE_URL=$TestDatabaseUrl" core uv run --no-sync pytest -q tests/integration/test_flow_scenarios.py
exit $LASTEXITCODE
