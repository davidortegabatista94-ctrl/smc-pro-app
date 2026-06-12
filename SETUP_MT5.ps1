# ═══════════════════════════════════════════════════════════
#  SETUP_MT5.ps1 — Configura el servicio MT5 en Railway
#  Haz doble clic en este archivo para ejecutarlo
# ═══════════════════════════════════════════════════════════

Set-Location "C:\Users\david\Downloads\smc_tool (1)\smc_tool"

Write-Host ""
Write-Host "══════════════════════════════════════════" -ForegroundColor Cyan
Write-Host "  PASO 1: Subiendo código a GitHub..." -ForegroundColor Cyan
Write-Host "══════════════════════════════════════════" -ForegroundColor Cyan

git add mt5_service/
git commit -m "refactor: replace Wine with MetaAPI cloud REST API"
git push origin master

Write-Host ""
Write-Host "✅ Código subido. Railway empezará a construir en ~1 min." -ForegroundColor Green
Write-Host ""
Write-Host "══════════════════════════════════════════" -ForegroundColor Yellow
Write-Host "  PASO 2: Crear cuenta MetaAPI (GRATIS)" -ForegroundColor Yellow
Write-Host "══════════════════════════════════════════" -ForegroundColor Yellow
Write-Host ""
Write-Host "Se abrirá la página de MetaAPI en tu navegador." -ForegroundColor White
Write-Host "Regístrate y añade esta cuenta MT5:" -ForegroundColor White
Write-Host ""
Write-Host "  Login:    5049942150" -ForegroundColor Green
Write-Host "  Password: @ilaKg1n" -ForegroundColor Green
Write-Host "  Server:   MetaQuotes-Demo" -ForegroundColor Green
Write-Host ""
Write-Host "Cuando esté conectada, copia:" -ForegroundColor White
Write-Host "  - Tu TOKEN de API (arriba a la derecha)" -ForegroundColor Yellow
Write-Host "  - El ACCOUNT ID de la cuenta MT5 (UUID largo)" -ForegroundColor Yellow
Write-Host ""

Start-Process "https://app.metaapi.cloud/sign-up"
Start-Sleep -Seconds 3

Write-Host "══════════════════════════════════════════" -ForegroundColor Magenta
Write-Host "  PASO 3: Pega tus credenciales MetaAPI" -ForegroundColor Magenta
Write-Host "══════════════════════════════════════════" -ForegroundColor Magenta
Write-Host ""

$META_TOKEN = Read-Host "Pega tu METAAPI_TOKEN aquí"
$META_ACCT  = Read-Host "Pega tu METAAPI_ACCOUNT_ID aquí"

Write-Host ""
Write-Host "Configurando variables en Railway..." -ForegroundColor Cyan

$RAILWAY_TOKEN = "7f510a3d-c94f-4939-bf9d-c58d1e8a51aa"
$PROJECT_ID    = "7f65a11c-0302-4861-8d0d-8a3672315760"

# Obtener el environment ID y service ID via Railway API
$headers = @{
    "Authorization" = "Bearer $RAILWAY_TOKEN"
    "Content-Type"  = "application/json"
}

# GraphQL: obtener environments del proyecto
$query = @{
    query = @"
query {
  project(id: "$PROJECT_ID") {
    environments { edges { node { id name } } }
    services { edges { node { id name } } }
  }
}
"@
} | ConvertTo-Json

try {
    $resp = Invoke-RestMethod -Uri "https://backboard.railway.com/graphql/v2" `
        -Method POST -Headers $headers -Body $query -ContentType "application/json"

    $envId     = ($resp.data.project.environments.edges | Where-Object { $_.node.name -eq "production" }).node.id
    $serviceId = ($resp.data.project.services.edges | Where-Object { $_.node.name -eq "mt5-trading" }).node.id

    if (-not $envId) { $envId = $resp.data.project.environments.edges[0].node.id }
    if (-not $serviceId) {
        Write-Host "Servicios disponibles:" -ForegroundColor Yellow
        $resp.data.project.services.edges | ForEach-Object { Write-Host "  - $($_.node.name) ($($_.node.id))" }
        $serviceId = Read-Host "Pega el ID del servicio mt5-trading"
    }

    Write-Host "  Environment: $envId" -ForegroundColor Gray
    Write-Host "  Service: $serviceId" -ForegroundColor Gray

    # Upsert variables
    foreach ($pair in @(
        @{ name = "METAAPI_TOKEN";      value = $META_TOKEN },
        @{ name = "METAAPI_ACCOUNT_ID"; value = $META_ACCT  }
    )) {
        $mut = @{
            query = @"
mutation {
  variableUpsert(input: {
    projectId: "$PROJECT_ID"
    environmentId: "$envId"
    serviceId: "$serviceId"
    name: "$($pair.name)"
    value: "$($pair.value)"
  })
}
"@
        } | ConvertTo-Json

        $r = Invoke-RestMethod -Uri "https://backboard.railway.com/graphql/v2" `
            -Method POST -Headers $headers -Body $mut -ContentType "application/json"

        if ($r.data.variableUpsert) {
            Write-Host "  ✅ $($pair.name) configurado" -ForegroundColor Green
        } else {
            Write-Host "  ⚠️  $($pair.name): $($r | ConvertTo-Json)" -ForegroundColor Yellow
        }
    }

    Write-Host ""
    Write-Host "══════════════════════════════════════════" -ForegroundColor Green
    Write-Host "  ✅ TODO LISTO" -ForegroundColor Green
    Write-Host "══════════════════════════════════════════" -ForegroundColor Green
    Write-Host ""
    Write-Host "Railway redesplegará automáticamente en ~1 minuto." -ForegroundColor White
    Write-Host "Después ejecuta esto para verificar:" -ForegroundColor White
    Write-Host ""
    Write-Host '  Invoke-WebRequest -UseBasicParsing -Uri "https://mt5-trading-production-5ffd.up.railway.app/health" | Select-Object -ExpandProperty Content' -ForegroundColor Cyan
    Write-Host ""

} catch {
    Write-Host "Error con Railway API: $_" -ForegroundColor Red
    Write-Host ""
    Write-Host "Añade las variables manualmente en Railway:" -ForegroundColor Yellow
    Write-Host "  METAAPI_TOKEN      = $META_TOKEN" -ForegroundColor White
    Write-Host "  METAAPI_ACCOUNT_ID = $META_ACCT" -ForegroundColor White
}

Write-Host ""
Read-Host "Pulsa Enter para cerrar"
