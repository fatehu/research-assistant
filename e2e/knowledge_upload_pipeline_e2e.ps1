param(
  [string]$BackendBaseUrl = "http://localhost:8888",
  [int]$PollIntervalSeconds = 3,
  [int]$MaxWaitSeconds = 600
)

$ErrorActionPreference = "Stop"
$apiBase = "$BackendBaseUrl/api"

function New-TestUser {
  $timestamp = Get-Date -Format "yyyyMMddHHmmss"
  return @{
    email = "kb_e2e_$timestamp@example.com"
    username = "kb_e2e_$timestamp"
    password = "KbE2e!$timestamp"
    full_name = "Knowledge E2E User"
  }
}

function Invoke-JsonPost {
  param(
    [Parameter(Mandatory = $true)] [string]$Uri,
    [Parameter(Mandatory = $true)] $Body,
    [hashtable]$Headers
  )
  return Invoke-RestMethod -Method Post -Uri $Uri -Headers $Headers -ContentType "application/json" -Body ($Body | ConvertTo-Json -Depth 10)
}

function Invoke-FileUpload {
  param(
    [Parameter(Mandatory = $true)] [string]$Uri,
    [Parameter(Mandatory = $true)] [string]$Token,
    [Parameter(Mandatory = $true)] [string]$FilePath
  )
  $authHeader = "Authorization: Bearer $Token"
  $uploadRaw = & curl.exe -sS -X POST $Uri -H $authHeader -F "file=@$FilePath;type=text/plain"
  if ($LASTEXITCODE -ne 0) {
    throw "curl upload failed"
  }
  return $uploadRaw | ConvertFrom-Json
}

Write-Host "=== Knowledge Upload Pipeline E2E ===" -ForegroundColor Cyan
Write-Host "Backend: $BackendBaseUrl"

$user = New-TestUser
$tokenResp = Invoke-JsonPost -Uri "$apiBase/auth/register" -Body $user
$token = $tokenResp.access_token
if (-not $token) {
  throw "register succeeded but access_token is empty"
}
$headers = @{ Authorization = "Bearer $token" }
Write-Host "Registered test user: $($user.email)" -ForegroundColor Green

$kbResp = Invoke-JsonPost -Uri "$apiBase/knowledge/knowledge-bases" -Headers $headers -Body @{
  name = "knowledge-e2e-$((Get-Date).ToString('HHmmss'))"
  description = "upload processing pipeline e2e check"
  embedding_model = "BAAI/bge-m3"
  chunk_size = 500
  chunk_overlap = 50
}
$kbId = $kbResp.id
if (-not $kbId) {
  throw "create knowledge base failed: missing id"
}
Write-Host "Created knowledge base: $kbId" -ForegroundColor Green

New-Item -ItemType Directory -Force "output/e2e" | Out-Null
$samplePath = "output/e2e/knowledge_upload_pipeline_sample.txt"
@"
This is an end-to-end upload pipeline sample for regression validation.
The pipeline should go through extraction, chunking, embedding, and persistence.
If this test passes, the document status should become completed and search should return at least one hit.
"@ | Set-Content -Path $samplePath -Encoding UTF8

$uploadResp = Invoke-FileUpload -Uri "$apiBase/knowledge/knowledge-bases/$kbId/documents/upload" -Token $token -FilePath $samplePath
$docId = $uploadResp.id
if (-not $docId) {
  throw "upload failed: missing document id"
}
Write-Host "Uploaded document id: $docId" -ForegroundColor Green

$deadline = (Get-Date).AddSeconds($MaxWaitSeconds)
$finalStatus = $null
while ((Get-Date) -lt $deadline) {
  Start-Sleep -Seconds $PollIntervalSeconds
  $statusResp = Invoke-RestMethod -Method Get -Uri "$apiBase/knowledge/knowledge-bases/$kbId/documents/$docId/status" -Headers $headers
  $finalStatus = $statusResp
  Write-Host ("status={0}, progress={1}, chunks={2}" -f $statusResp.status, $statusResp.progress, $statusResp.chunk_count)

  if ($statusResp.status -eq "completed") {
    break
  }
  if ($statusResp.status -eq "failed") {
    throw ("document processing failed: {0}" -f $statusResp.error)
  }
}

if (-not $finalStatus -or $finalStatus.status -ne "completed") {
  throw "document did not complete within timeout"
}

$searchResp = Invoke-JsonPost -Uri "$apiBase/knowledge/search" -Headers $headers -Body @{
  query = "upload pipeline sample"
  knowledge_base_ids = @($kbId)
  top_k = 3
  score_threshold = 0.0
}
if (-not $searchResp.results -or $searchResp.results.Count -lt 1) {
  throw "search returned no results for uploaded document"
}

Write-Host "E2E success: upload -> processing -> search passed." -ForegroundColor Green

