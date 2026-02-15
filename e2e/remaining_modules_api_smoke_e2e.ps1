param(
  [string]$BackendBaseUrl = "http://localhost:8888",
  [string]$DbContainer = "research_postgres",
  [string]$DbUser = "",
  [string]$DbName = "",
  [string]$EnvFilePath = ".env",
  [int]$PollIntervalSeconds = 3,
  [int]$MaxWaitSeconds = 600
)

$ErrorActionPreference = "Stop"
$apiBase = "$BackendBaseUrl/api/v1"

function Get-EnvValue {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Path,
    [Parameter(Mandatory = $true)]
    [string]$Key
  )

  if (-not (Test-Path $Path)) { return "" }
  $line = Select-String -Path $Path -Pattern "^\s*$Key=(.*)$" | Select-Object -First 1
  if (-not $line) { return "" }
  return $line.Matches[0].Groups[1].Value.Trim()
}

function Assert-True {
  param(
    [Parameter(Mandatory = $true)]
    [bool]$Condition,
    [Parameter(Mandatory = $true)]
    [string]$Message
  )
  if (-not $Condition) {
    throw $Message
  }
}

function Run-Test {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Name,
    [Parameter(Mandatory = $true)]
    [scriptblock]$Script
  )

  Write-Host "Running Test: $Name..." -NoNewline
  try {
    & $Script
    Write-Host " [PASS]" -ForegroundColor Green
  }
  catch {
    Write-Host " [FAIL]" -ForegroundColor Red
    throw
  }
}

function Invoke-JsonRequest {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Method,
    [Parameter(Mandatory = $true)]
    [string]$Uri,
    [hashtable]$Headers,
    $Body
  )

  if ($null -ne $Body) {
    return Invoke-RestMethod -Method $Method -Uri $Uri -Headers $Headers -ContentType "application/json" -Body ($Body | ConvertTo-Json -Depth 40)
  }
  return Invoke-RestMethod -Method $Method -Uri $Uri -Headers $Headers
}

function Get-ErrorStatusCode {
  param(
    [Parameter(Mandatory = $true)]
    $ErrorRecord
  )

  try {
    if ($null -ne $ErrorRecord.Exception.Response -and $null -ne $ErrorRecord.Exception.Response.StatusCode) {
      return [int]$ErrorRecord.Exception.Response.StatusCode
    }
  }
  catch {}

  $msg = [string]$ErrorRecord.Exception.Message
  if ($msg -match "(\d{3})") {
    return [int]$matches[1]
  }
  return -1
}

function Assert-RequestFails {
  param(
    [Parameter(Mandatory = $true)]
    [scriptblock]$Script,
    [Parameter(Mandatory = $true)]
    [string]$Message,
    [int[]]$ExpectedStatusCodes = @()
  )

  $caught = $null
  try {
    & $Script | Out-Null
  }
  catch {
    $caught = $_
  }

  Assert-True -Condition ($null -ne $caught) -Message "$Message (request unexpectedly succeeded)"

  if ($ExpectedStatusCodes.Count -gt 0) {
    $status = Get-ErrorStatusCode -ErrorRecord $caught
    Assert-True -Condition ($ExpectedStatusCodes -contains $status) -Message "$Message (expected status $($ExpectedStatusCodes -join ',') but got $status)"
  }

  return $caught
}

function Register-User {
  param(
    [Parameter(Mandatory = $true)]
    [hashtable]$User
  )
  return Invoke-JsonRequest -Method Post -Uri "$apiBase/auth/register" -Body $User
}

function Login-User {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Email,
    [Parameter(Mandatory = $true)]
    [string]$Password
  )
  return Invoke-JsonRequest -Method Post -Uri "$apiBase/auth/login" -Body @{
    email = $Email
    password = $Password
  }
}

function Set-DbRole {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Email,
    [Parameter(Mandatory = $true)]
    [string]$Role
  )
  $sql = "UPDATE users SET role='$Role' WHERE email='$Email';"
  docker exec $DbContainer psql -U $dbUserResolved -d $dbNameResolved -c $sql | Out-Null
  $verify = docker exec $DbContainer psql -U $dbUserResolved -d $dbNameResolved -t -A -c "SELECT role FROM users WHERE email='$Email';"
  Assert-True -Condition ($verify -match "^$Role$") -Message "Role update failed for $Email, expected $Role, got $verify"
}

function Invoke-CurlMultipartUpload {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Uri,
    [Parameter(Mandatory = $true)]
    [string]$Token,
    [Parameter(Mandatory = $true)]
    [string]$FilePath,
    [string]$MimeType = "text/plain"
  )

  $tmpOut = New-TemporaryFile
  try {
    $authHeader = "Authorization: Bearer $Token"
    $statusText = & curl.exe -sS -o $tmpOut -w "%{http_code}" -X POST $Uri -H $authHeader -F "file=@$FilePath;type=$MimeType"
    if ($LASTEXITCODE -ne 0) {
      throw "curl upload failed with exit code $LASTEXITCODE"
    }

    $raw = ""
    if (Test-Path $tmpOut) {
      $raw = Get-Content -Path $tmpOut -Raw
    }

    $json = $null
    try {
      if (-not [string]::IsNullOrWhiteSpace($raw)) {
        $json = $raw | ConvertFrom-Json
      }
    }
    catch {}

    return @{
      status = [int]$statusText
      text = $raw
      json = $json
    }
  }
  finally {
    Remove-Item -Path $tmpOut -Force -ErrorAction SilentlyContinue
  }
}

function Wait-DocumentTerminal {
  param(
    [Parameter(Mandatory = $true)]
    [int]$KbId,
    [Parameter(Mandatory = $true)]
    [int]$DocId,
    [Parameter(Mandatory = $true)]
    [hashtable]$Headers
  )

  $deadline = (Get-Date).AddSeconds($MaxWaitSeconds)
  while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds $PollIntervalSeconds
    $statusResp = Invoke-JsonRequest -Method Get -Uri "$apiBase/knowledge/knowledge-bases/$KbId/documents/$DocId/status" -Headers $Headers
    Write-Host ("  doc_status={0}, progress={1}, chunks={2}" -f $statusResp.status, $statusResp.progress, $statusResp.chunk_count)

    if ($statusResp.status -eq "completed") {
      return $statusResp
    }
    if ($statusResp.status -eq "failed") {
      throw "Document processing failed: $($statusResp.error)"
    }
  }

  throw "Document $DocId in KB $KbId did not reach terminal status within timeout"
}

function New-PaperPayload {
  param(
    [Parameter(Mandatory = $true)]
    [string]$ExternalId,
    [Parameter(Mandatory = $true)]
    [string]$Title,
    [int]$Year = 2026
  )

  return @{
    source = "manual"
    external_id = $ExternalId
    title = $Title
    abstract = "API black-box smoke paper: $Title"
    authors = @(
      @{
        name = "Smoke Author"
      }
    )
    year = $Year
    venue = "SmokeConf"
    citation_count = 1
    reference_count = 1
    url = "https://example.com/$ExternalId"
    pdf_url = $null
    arxiv_id = $null
    doi = $null
    fields_of_study = @("computer science")
    raw_data = @{
      smoke = $true
      external = $ExternalId
    }
    collection_ids = @()
  }
}

$dbUserResolved = if ([string]::IsNullOrWhiteSpace($DbUser)) { Get-EnvValue -Path $EnvFilePath -Key "POSTGRES_USER" } else { $DbUser }
$dbNameResolved = if ([string]::IsNullOrWhiteSpace($DbName)) { Get-EnvValue -Path $EnvFilePath -Key "POSTGRES_DB" } else { $DbName }
if ([string]::IsNullOrWhiteSpace($dbUserResolved)) { $dbUserResolved = "research_user" }
if ([string]::IsNullOrWhiteSpace($dbNameResolved)) { $dbNameResolved = "research_assistant" }

$seed = Get-Date -Format "yyyyMMddHHmmss"
$rand = Get-Random -Minimum 100 -Maximum 999
$suffix = "$seed$rand"

$baseUser = @{
  email = "base_api_$suffix@example.com"
  username = "base_api_$suffix"
  password = "BaseApi!$suffix"
  full_name = "Base API $suffix"
}
$adminUser = @{
  email = "admin_api2_$suffix@example.com"
  username = "admin_api2_$suffix"
  password = "AdminApi2!$suffix"
  full_name = "Admin API2 $suffix"
}
$mentorUser = @{
  email = "mentor_api2_$suffix@example.com"
  username = "mentor_api2_$suffix"
  password = "MentorApi2!$suffix"
  full_name = "Mentor API2 $suffix"
}
$studentUser = @{
  email = "student_api2_$suffix@example.com"
  username = "student_api2_$suffix"
  password = "StudentApi2!$suffix"
  full_name = "Student API2 $suffix"
}
$outsiderUser = @{
  email = "outsider_api_$suffix@example.com"
  username = "outsider_api_$suffix"
  password = "OutsiderApi!$suffix"
  full_name = "Outsider API $suffix"
}
$disabledUser = @{
  email = "disabled_api_$suffix@example.com"
  username = "disabled_api_$suffix"
  password = "DisabledApi!$suffix"
  full_name = "Disabled API $suffix"
}

$script:baseHeaders = $null
$script:adminHeaders = $null
$script:mentorHeaders = $null
$script:studentHeaders = $null
$script:outsiderHeaders = $null
$script:adminId = 0
$script:mentorId = 0
$script:studentId = 0
$script:disabledUserId = 0

$script:chatConversationId = 0
$script:chatMessageId = 0

$script:kbTestId = 0
$script:kbTestDocId = 0
$script:shareKbId = 0

$script:sharedNotebookId = ""
$script:sharedCodeCellId = ""
$script:sharedMarkdownCellId = ""

$script:paperAId = 0
$script:paperBId = 0
$script:paperTempId = 0
$script:shareCollectionId = 0

$script:groupId = 0
$script:pendingInvitationId = 0

$script:sharePaperGroupId = 0
$script:shareCollectionShareId = 0
$script:shareNotebookShareId = 0
$script:shareKbShareId = 0

Write-Host "=== Remaining Modules API Smoke E2E ===" -ForegroundColor Cyan
Write-Host "Backend: $BackendBaseUrl"
Write-Host "DB: $DbContainer / $dbUserResolved / $dbNameResolved"

Register-User -User $baseUser | Out-Null
Register-User -User $adminUser | Out-Null
Register-User -User $mentorUser | Out-Null
Register-User -User $studentUser | Out-Null
$outsiderReg = Register-User -User $outsiderUser
$disabledReg = Register-User -User $disabledUser
$script:disabledUserId = $disabledReg.user.id

Set-DbRole -Email $adminUser.email -Role "admin"
Set-DbRole -Email $mentorUser.email -Role "mentor"

$baseLogin = Login-User -Email $baseUser.email -Password $baseUser.password
$adminLogin = Login-User -Email $adminUser.email -Password $adminUser.password
$mentorLogin = Login-User -Email $mentorUser.email -Password $mentorUser.password
$studentLogin = Login-User -Email $studentUser.email -Password $studentUser.password
$outsiderLogin = Login-User -Email $outsiderUser.email -Password $outsiderUser.password

$script:baseHeaders = @{ Authorization = "Bearer $($baseLogin.access_token)" }
$script:adminHeaders = @{ Authorization = "Bearer $($adminLogin.access_token)" }
$script:mentorHeaders = @{ Authorization = "Bearer $($mentorLogin.access_token)" }
$script:studentHeaders = @{ Authorization = "Bearer $($studentLogin.access_token)" }
$script:outsiderHeaders = @{ Authorization = "Bearer $($outsiderLogin.access_token)" }

$script:adminId = $adminLogin.user.id
$script:mentorId = $mentorLogin.user.id
$script:studentId = $studentLogin.user.id

Run-Test "A-02 Duplicate registration is rejected" {
  Assert-RequestFails -Message "duplicate email register should fail" -ExpectedStatusCodes @(400) -Script {
    Register-User -User @{
      email = $baseUser.email
      username = "dup_api_$suffix"
      password = "DupApi!$suffix"
      full_name = "Duplicate"
    } | Out-Null
  } | Out-Null
}

Run-Test "A-04 Wrong password login is rejected" {
  Assert-RequestFails -Message "wrong password login should fail" -ExpectedStatusCodes @(401) -Script {
    Login-User -Email $baseUser.email -Password "WrongPass!$suffix" | Out-Null
  } | Out-Null
}

Run-Test "A-06 Unauthenticated private endpoint blocked" {
  Assert-RequestFails -Message "unauthenticated profile access should fail" -ExpectedStatusCodes @(401, 403) -Script {
    Invoke-JsonRequest -Method Get -Uri "$apiBase/users/profile" | Out-Null
  } | Out-Null
}

Run-Test "A-03 Login success and A-05 logout endpoint" {
  $login = Login-User -Email $baseUser.email -Password $baseUser.password
  Assert-True -Condition (-not [string]::IsNullOrWhiteSpace($login.access_token)) -Message "login token missing"
  $logout = Invoke-JsonRequest -Method Post -Uri "$apiBase/auth/logout" -Headers $baseHeaders
  Assert-True -Condition (-not [string]::IsNullOrWhiteSpace($logout.message)) -Message "logout message missing"
}

Run-Test "L-01 Profile read and auth me" {
  $me = Invoke-JsonRequest -Method Get -Uri "$apiBase/auth/me" -Headers $baseHeaders
  Assert-True -Condition ($me.id -gt 0) -Message "/auth/me should return user id"
  $profile = Invoke-JsonRequest -Method Get -Uri "$apiBase/users/profile" -Headers $baseHeaders
  Assert-True -Condition ($profile.email -eq $baseUser.email) -Message "profile email mismatch"
}

Run-Test "L-02/L-04/L-05 Profile update and preference persistence" {
  $updated = Invoke-JsonRequest -Method Put -Uri "$apiBase/users/profile" -Headers $baseHeaders -Body @{
    full_name = "Base API Updated $suffix"
    department = "QA Lab"
    research_direction = "Black Box Automation"
    preferred_llm_provider = "ollama"
    preferences = @{
      notifications = @{
        in_app = $true
        email = $false
      }
    }
  }
  Assert-True -Condition ($updated.full_name -eq "Base API Updated $suffix") -Message "full_name not updated"
  Assert-True -Condition ($updated.preferred_llm_provider -eq "ollama") -Message "preferred_llm_provider not updated"

  $verify = Invoke-JsonRequest -Method Get -Uri "$apiBase/users/profile" -Headers $baseHeaders
  Assert-True -Condition ($verify.department -eq "QA Lab") -Message "department not persisted"
}

Run-Test "L-07 Password change with wrong old password rejected" {
  Assert-RequestFails -Message "wrong old password should fail" -ExpectedStatusCodes @(400) -Script {
    Invoke-JsonRequest -Method Put -Uri "$apiBase/users/password" -Headers $baseHeaders -Body @{
      old_password = "WrongPass!$suffix"
      new_password = "BaseApiNew!$suffix"
    } | Out-Null
  } | Out-Null
}

$newBasePassword = "BaseApiNew!$suffix"
Run-Test "L-06/A-10/M-08 Password change success and old password invalidated" {
  $changed = Invoke-JsonRequest -Method Put -Uri "$apiBase/users/password" -Headers $baseHeaders -Body @{
    old_password = $baseUser.password
    new_password = $newBasePassword
  }
  Assert-True -Condition (-not [string]::IsNullOrWhiteSpace($changed.message)) -Message "password change message missing"

  Assert-RequestFails -Message "old password login should fail after password change" -ExpectedStatusCodes @(401) -Script {
    Login-User -Email $baseUser.email -Password $baseUser.password | Out-Null
  } | Out-Null

  $relogin = Login-User -Email $baseUser.email -Password $newBasePassword
  Assert-True -Condition (-not [string]::IsNullOrWhiteSpace($relogin.access_token)) -Message "new password login failed"
  $baseUser.password = $newBasePassword
  $script:baseHeaders = @{ Authorization = "Bearer $($relogin.access_token)" }
}

Run-Test "L-04 LLM provider list endpoint is readable" {
  $providers = Invoke-JsonRequest -Method Get -Uri "$apiBase/users/llm-providers" -Headers $baseHeaders
  Assert-True -Condition (@($providers.providers).Count -ge 1) -Message "llm providers list should not be empty"
}

Run-Test "A-09/M-07 Disabled account login rejected by backend" {
  $disabled = Invoke-JsonRequest -Method Put -Uri "$apiBase/admin/users/$disabledUserId/toggle-active" -Headers $adminHeaders
  Assert-True -Condition ($disabled.is_active -eq $false) -Message "disabled user should become inactive"

  Assert-RequestFails -Message "disabled account login should fail" -ExpectedStatusCodes @(403) -Script {
    Login-User -Email $disabledUser.email -Password $disabledUser.password | Out-Null
  } | Out-Null

  $enabled = Invoke-JsonRequest -Method Put -Uri "$apiBase/admin/users/$disabledUserId/toggle-active" -Headers $adminHeaders
  Assert-True -Condition ($enabled.is_active -eq $true) -Message "disabled user should be re-enabled for cleanup"
}

Run-Test "C-05 Create conversation" {
  $conv = Invoke-JsonRequest -Method Post -Uri "$apiBase/chat/conversations" -Headers $baseHeaders -Body @{
    title = "Chat Smoke $suffix"
    llm_provider = "ollama"
  }
  $script:chatConversationId = [int]$conv.id
  Assert-True -Condition ($chatConversationId -gt 0) -Message "conversation id missing"
}

Run-Test "C-08/C-12 Save stopped message and list messages" {
  $message = Invoke-JsonRequest -Method Post -Uri "$apiBase/chat/messages/stopped" -Headers $baseHeaders -Body @{
    conversation_id = $chatConversationId
    content = "Stopped message keyword-$suffix"
    thought = "smoke thought"
    react_steps = @()
  }
  $script:chatMessageId = [int]$message.id
  Assert-True -Condition ($chatMessageId -gt 0) -Message "stopped message id missing"

  $messages = Invoke-JsonRequest -Method Get -Uri "$apiBase/chat/conversations/$chatConversationId/messages?limit=50" -Headers $baseHeaders
  Assert-True -Condition ((@($messages | Where-Object { $_.id -eq $chatMessageId }).Count) -ge 1) -Message "messages list should include stopped message"
}

Run-Test "C-07 Message search finds saved content" {
  $q = [uri]::EscapeDataString("keyword-$suffix")
  $search = Invoke-JsonRequest -Method Get -Uri "$apiBase/chat/messages/search?q=$q&limit=20" -Headers $baseHeaders
  Assert-True -Condition ([int]$search.total -ge 1) -Message "message search should return at least one result"
}

Run-Test "C-01/C-06 Conversation archive toggle and delete" {
  $archived = Invoke-JsonRequest -Method Put -Uri "$apiBase/chat/conversations/$chatConversationId/archive" -Headers $baseHeaders
  Assert-True -Condition ([int]$archived.is_archived -eq 1) -Message "conversation should be archived"

  $archivedList = Invoke-JsonRequest -Method Get -Uri "$apiBase/chat/conversations?archived=true&limit=50" -Headers $baseHeaders
  Assert-True -Condition ((@($archivedList | Where-Object { $_.id -eq $chatConversationId }).Count) -ge 1) -Message "archived list should include conversation"

  $unarchived = Invoke-JsonRequest -Method Put -Uri "$apiBase/chat/conversations/$chatConversationId/archive" -Headers $baseHeaders
  Assert-True -Condition ([int]$unarchived.is_archived -eq 0) -Message "conversation should be unarchived"

  $deleted = Invoke-JsonRequest -Method Delete -Uri "$apiBase/chat/conversations/$chatConversationId" -Headers $baseHeaders
  Assert-True -Condition (-not [string]::IsNullOrWhiteSpace($deleted.message)) -Message "delete conversation message missing"

  Assert-RequestFails -Message "deleted conversation should not be retrievable" -ExpectedStatusCodes @(404) -Script {
    Invoke-JsonRequest -Method Get -Uri "$apiBase/chat/conversations/$chatConversationId" -Headers $baseHeaders | Out-Null
  } | Out-Null
}

Run-Test "G-01 Create notebook and G-02 update notebook title" {
  $nb = Invoke-JsonRequest -Method Post -Uri "$apiBase/codelab/notebooks" -Headers $mentorHeaders -Body @{
    title = "Shared Notebook $suffix"
    description = "Notebook for share smoke"
  }
  $script:sharedNotebookId = [string]$nb.id
  Assert-True -Condition (-not [string]::IsNullOrWhiteSpace($sharedNotebookId)) -Message "shared notebook id missing"

  $updated = Invoke-JsonRequest -Method Patch -Uri "$apiBase/codelab/notebooks/$sharedNotebookId" -Headers $mentorHeaders -Body @{
    title = "Shared Notebook Updated $suffix"
  }
  Assert-True -Condition ($updated.title -eq "Shared Notebook Updated $suffix") -Message "notebook title not updated"
}

Run-Test "G-03/G-04 Add code and markdown cells" {
  $codeCell = Invoke-JsonRequest -Method Post -Uri "$apiBase/codelab/notebooks/$sharedNotebookId/cells?cell_type=code" -Headers $mentorHeaders
  $mdCell = Invoke-JsonRequest -Method Post -Uri "$apiBase/codelab/notebooks/$sharedNotebookId/cells?cell_type=markdown" -Headers $mentorHeaders
  $script:sharedCodeCellId = [string]$codeCell.id
  $script:sharedMarkdownCellId = [string]$mdCell.id
  Assert-True -Condition (-not [string]::IsNullOrWhiteSpace($sharedCodeCellId)) -Message "code cell id missing"
  Assert-True -Condition (-not [string]::IsNullOrWhiteSpace($sharedMarkdownCellId)) -Message "markdown cell id missing"
}

Run-Test "G-11/G-12 Sync cell source and types" {
  $cellsPayload = @(
    @{
      id = $sharedCodeCellId
      cell_type = "code"
      source = "x = 41`nprint(x + 1)"
      outputs = @()
      metadata = @{}
    },
    @{
      id = $sharedMarkdownCellId
      cell_type = "markdown"
      source = "## Smoke markdown $suffix"
      outputs = @()
      metadata = @{}
    }
  )
  $patched = Invoke-JsonRequest -Method Patch -Uri "$apiBase/codelab/notebooks/$sharedNotebookId" -Headers $mentorHeaders -Body @{
    cells = $cellsPayload
  }
  Assert-True -Condition (@($patched.cells).Count -ge 2) -Message "patched notebook should contain cells"
}

Run-Test "G-05 Execute code cell and G-08 run-all" {
  $exec = Invoke-JsonRequest -Method Post -Uri "$apiBase/codelab/notebooks/$sharedNotebookId/execute" -Headers $mentorHeaders -Body @{
    code = "x = 41`nprint(x + 1)"
    cell_id = $sharedCodeCellId
    timeout = 20
  }
  Assert-True -Condition ($exec.success -eq $true) -Message "execute cell should succeed"

  $runAll = Invoke-JsonRequest -Method Post -Uri "$apiBase/codelab/notebooks/$sharedNotebookId/run-all" -Headers $mentorHeaders
  Assert-True -Condition (@($runAll.results).Count -ge 1) -Message "run-all should execute at least one code cell"
}

Run-Test "G-09 Restart kernel and G-10 delete cell" {
  $status = Invoke-JsonRequest -Method Get -Uri "$apiBase/codelab/notebooks/$sharedNotebookId/kernel-status" -Headers $mentorHeaders
  Assert-True -Condition (-not [string]::IsNullOrWhiteSpace($status.status)) -Message "kernel status missing"

  $restart = Invoke-JsonRequest -Method Post -Uri "$apiBase/codelab/notebooks/$sharedNotebookId/restart-kernel" -Headers $mentorHeaders
  Assert-True -Condition (-not [string]::IsNullOrWhiteSpace($restart.message)) -Message "restart kernel message missing"

  $deletedCell = Invoke-JsonRequest -Method Delete -Uri "$apiBase/codelab/notebooks/$sharedNotebookId/cells/$sharedMarkdownCellId" -Headers $mentorHeaders
  Assert-True -Condition (-not [string]::IsNullOrWhiteSpace($deletedCell.message)) -Message "delete cell message missing"
}

Run-Test "G-13 Delete notebook" {
  $tempNb = Invoke-JsonRequest -Method Post -Uri "$apiBase/codelab/notebooks" -Headers $mentorHeaders -Body @{
    title = "Temp Notebook $suffix"
    description = "delete smoke"
  }
  $tempNbId = [string]$tempNb.id
  Assert-True -Condition (-not [string]::IsNullOrWhiteSpace($tempNbId)) -Message "temp notebook id missing"

  $deleted = Invoke-JsonRequest -Method Delete -Uri "$apiBase/codelab/notebooks/$tempNbId" -Headers $mentorHeaders
  Assert-True -Condition (-not [string]::IsNullOrWhiteSpace($deleted.message)) -Message "delete notebook message missing"
}

Run-Test "D-01 Create knowledge base and D-06 empty search" {
  $kb = Invoke-JsonRequest -Method Post -Uri "$apiBase/knowledge/knowledge-bases" -Headers $baseHeaders -Body @{
    name = "KB Smoke $suffix"
    description = "knowledge smoke"
    embedding_model = "BAAI/bge-m3"
    chunk_size = 500
    chunk_overlap = 50
  }
  $script:kbTestId = [int]$kb.id
  Assert-True -Condition ($kbTestId -gt 0) -Message "knowledge base id missing"

  $search = Invoke-JsonRequest -Method Post -Uri "$apiBase/knowledge/search" -Headers $baseHeaders -Body @{
    query = "should not match anything"
    knowledge_base_ids = @($kbTestId)
    top_k = 3
    score_threshold = 0.0
  }
  Assert-True -Condition ([int]$search.total -eq 0) -Message "empty KB search should return zero results"
}

Run-Test "D-16 Invalid file upload is rejected" {
  New-Item -ItemType Directory -Path "output/e2e" -Force | Out-Null
  $invalidPath = "output/e2e/invalid_upload_$suffix.exe"
  "not-a-supported-document" | Set-Content -Path $invalidPath -Encoding UTF8

  $resp = Invoke-CurlMultipartUpload -Uri "$apiBase/knowledge/knowledge-bases/$kbTestId/documents/upload" -Token ($baseHeaders.Authorization -replace "^Bearer ", "") -FilePath $invalidPath -MimeType "application/octet-stream"
  Assert-True -Condition ($resp.status -ge 400) -Message "invalid upload should fail"
}

Run-Test "D-03/D-04 Upload document and wait until completed" {
  New-Item -ItemType Directory -Path "output/e2e" -Force | Out-Null
  $samplePath = "output/e2e/remaining_modules_sample_$suffix.txt"
  @"
This is a black-box smoke sample.
Knowledge upload should complete and become searchable.
"@ | Set-Content -Path $samplePath -Encoding UTF8

  $upload = Invoke-CurlMultipartUpload -Uri "$apiBase/knowledge/knowledge-bases/$kbTestId/documents/upload" -Token ($baseHeaders.Authorization -replace "^Bearer ", "") -FilePath $samplePath -MimeType "text/plain"
  Assert-True -Condition ($upload.status -eq 200) -Message "document upload should return HTTP 200"
  Assert-True -Condition ($null -ne $upload.json -and [int]$upload.json.id -gt 0) -Message "uploaded document id missing"
  $script:kbTestDocId = [int]$upload.json.id

  $terminal = Wait-DocumentTerminal -KbId $kbTestId -DocId $kbTestDocId -Headers $baseHeaders
  Assert-True -Condition ($terminal.status -eq "completed") -Message "document should complete processing"
}

Run-Test "D-15 document list refresh and D-05 delete document" {
  $docs = Invoke-JsonRequest -Method Get -Uri "$apiBase/knowledge/knowledge-bases/$kbTestId/documents?limit=100" -Headers $baseHeaders
  Assert-True -Condition ((@($docs.items | Where-Object { $_.id -eq $kbTestDocId }).Count) -ge 1) -Message "documents list should contain uploaded document"

  $deleted = Invoke-JsonRequest -Method Delete -Uri "$apiBase/knowledge/knowledge-bases/$kbTestId/documents/$kbTestDocId" -Headers $baseHeaders
  Assert-True -Condition (-not [string]::IsNullOrWhiteSpace($deleted.message)) -Message "delete document message missing"

  $docsAfter = Invoke-JsonRequest -Method Get -Uri "$apiBase/knowledge/knowledge-bases/$kbTestId/documents?limit=100" -Headers $baseHeaders
  Assert-True -Condition ((@($docsAfter.items | Where-Object { $_.id -eq $kbTestDocId }).Count) -eq 0) -Message "deleted document should disappear from list"
}

Run-Test "D-02 Delete knowledge base" {
  $deleted = Invoke-JsonRequest -Method Delete -Uri "$apiBase/knowledge/knowledge-bases/$kbTestId" -Headers $baseHeaders
  Assert-True -Condition (-not [string]::IsNullOrWhiteSpace($deleted.message)) -Message "delete KB message missing"

  Assert-RequestFails -Message "deleted KB should not be retrievable" -ExpectedStatusCodes @(404) -Script {
    Invoke-JsonRequest -Method Get -Uri "$apiBase/knowledge/knowledge-bases/$kbTestId" -Headers $baseHeaders | Out-Null
  } | Out-Null
}

Run-Test "F-01 Initialize literature module and F-06 create collection" {
  $init = Invoke-JsonRequest -Method Post -Uri "$apiBase/literature/init" -Headers $mentorHeaders
  Assert-True -Condition (-not [string]::IsNullOrWhiteSpace($init.message)) -Message "literature init message missing"

  $collection = Invoke-JsonRequest -Method Post -Uri "$apiBase/literature/collections" -Headers $mentorHeaders -Body @{
    name = "Share Collection $suffix"
    description = "for share tests"
    color = "#2463eb"
    icon = "folder"
    collection_type = "custom"
  }
  $script:shareCollectionId = [int]$collection.id
  Assert-True -Condition ($shareCollectionId -gt 0) -Message "share collection id missing"
}

Run-Test "F-07 Delete collection" {
  $tempCollection = Invoke-JsonRequest -Method Post -Uri "$apiBase/literature/collections" -Headers $mentorHeaders -Body @{
    name = "Temp Collection $suffix"
    description = "delete me"
    color = "#999999"
    icon = "folder"
    collection_type = "custom"
  }
  $tempCollectionId = [int]$tempCollection.id
  Assert-True -Condition ($tempCollectionId -gt 0) -Message "temp collection id missing"

  $deleted = Invoke-JsonRequest -Method Delete -Uri "$apiBase/literature/collections/$tempCollectionId" -Headers $mentorHeaders
  Assert-True -Condition (-not [string]::IsNullOrWhiteSpace($deleted.message)) -Message "delete collection message missing"
}

Run-Test "F-04 Save papers and F-05 delete paper" {
  $paperA = Invoke-JsonRequest -Method Post -Uri "$apiBase/literature/papers" -Headers $mentorHeaders -Body (New-PaperPayload -ExternalId "paperA-$suffix" -Title "Paper A $suffix")
  $paperB = Invoke-JsonRequest -Method Post -Uri "$apiBase/literature/papers" -Headers $mentorHeaders -Body (New-PaperPayload -ExternalId "paperB-$suffix" -Title "Paper B $suffix")
  $paperTemp = Invoke-JsonRequest -Method Post -Uri "$apiBase/literature/papers" -Headers $mentorHeaders -Body (New-PaperPayload -ExternalId "paperTemp-$suffix" -Title "Paper Temp $suffix")

  $script:paperAId = [int]$paperA.id
  $script:paperBId = [int]$paperB.id
  $script:paperTempId = [int]$paperTemp.id

  Assert-True -Condition ($paperAId -gt 0 -and $paperBId -gt 0 -and $paperTempId -gt 0) -Message "paper ids should all exist"

  $deleted = Invoke-JsonRequest -Method Delete -Uri "$apiBase/literature/papers/$paperTempId" -Headers $mentorHeaders
  Assert-True -Condition (-not [string]::IsNullOrWhiteSpace($deleted.message)) -Message "delete paper message missing"
}

Run-Test "F-08 Add/remove paper in collection and F-13 paper detail" {
  $added = Invoke-JsonRequest -Method Post -Uri "$apiBase/literature/collections/add-paper" -Headers $mentorHeaders -Body @{
    paper_id = $paperBId
    collection_ids = @($shareCollectionId)
  }
  Assert-True -Condition (-not [string]::IsNullOrWhiteSpace($added.message)) -Message "add-paper message missing"

  $removed = Invoke-JsonRequest -Method Post -Uri "$apiBase/literature/collections/remove-paper" -Headers $mentorHeaders -Body @{
    paper_id = $paperBId
    collection_id = $shareCollectionId
  }
  Assert-True -Condition (-not [string]::IsNullOrWhiteSpace($removed.message)) -Message "remove-paper message missing"

  $readded = Invoke-JsonRequest -Method Post -Uri "$apiBase/literature/collections/add-paper" -Headers $mentorHeaders -Body @{
    paper_id = $paperBId
    collection_ids = @($shareCollectionId)
  }
  Assert-True -Condition (-not [string]::IsNullOrWhiteSpace($readded.message)) -Message "re-add paper message missing"

  $paper = Invoke-JsonRequest -Method Get -Uri "$apiBase/literature/papers/$paperAId" -Headers $mentorHeaders
  Assert-True -Condition ($paper.id -eq $paperAId) -Message "paper detail id mismatch"
}

Run-Test "M-01 Student apply mentor and mentor accept" {
  $apply = Invoke-JsonRequest -Method Post -Uri "$apiBase/student/mentor/apply" -Headers $studentHeaders -Body @{
    mentor_id = $mentorId
    message = "Apply for mentor relation in smoke test"
  }
  Assert-True -Condition ([int]$apply.invitation_id -gt 0) -Message "student apply should return invitation id"

  $received = Invoke-JsonRequest -Method Get -Uri "$apiBase/invitations/received?status=pending&limit=50" -Headers $mentorHeaders
  $target = @($received | Where-Object { $_.from_user_id -eq $studentId -and $_.status -eq "pending" }) | Select-Object -First 1
  Assert-True -Condition ($null -ne $target) -Message "mentor should see pending invitation from student"
  $script:pendingInvitationId = [int]$target.id

  $accepted = Invoke-JsonRequest -Method Post -Uri "$apiBase/invitations/$pendingInvitationId/accept" -Headers $mentorHeaders
  Assert-True -Condition (-not [string]::IsNullOrWhiteSpace($accepted.message)) -Message "accept invitation message missing"
}

Run-Test "I-07/I-10 Create group and add student member" {
  $group = Invoke-JsonRequest -Method Post -Uri "$apiBase/mentor/groups" -Headers $mentorHeaders -Body @{
    name = "Share Group $suffix"
    description = "group for share smoke"
    max_members = 20
  }
  $script:groupId = [int]$group.id
  Assert-True -Condition ($groupId -gt 0) -Message "group id missing"

  $added = Invoke-JsonRequest -Method Post -Uri "$apiBase/mentor/groups/$groupId/members" -Headers $mentorHeaders -Body @{
    user_id = $studentId
  }
  Assert-True -Condition (-not [string]::IsNullOrWhiteSpace($added.message)) -Message "add group member message missing"
}

Run-Test "D-01 (mentor) Create KB for sharing" {
  $kb = Invoke-JsonRequest -Method Post -Uri "$apiBase/knowledge/knowledge-bases" -Headers $mentorHeaders -Body @{
    name = "Share KB $suffix"
    description = "shared kb smoke"
    embedding_model = "BAAI/bge-m3"
    chunk_size = 500
    chunk_overlap = 50
  }
  $script:shareKbId = [int]$kb.id
  Assert-True -Condition ($shareKbId -gt 0) -Message "share KB id missing"
}

$script:sharedCountBefore = 0
Run-Test "H-06 Capture student shared-with-me count baseline" {
  $count = Invoke-JsonRequest -Method Get -Uri "$apiBase/share/shared-with-me/count" -Headers $studentHeaders
  $script:sharedCountBefore = [int]$count.total
  Assert-True -Condition ($sharedCountBefore -ge 0) -Message "shared count baseline should be numeric"
}

Run-Test "H-02 Share single paper to research group" {
  $share = Invoke-JsonRequest -Method Post -Uri "$apiBase/share/" -Headers $mentorHeaders -Body @{
    resource_type = "paper"
    resource_id = $paperAId
    shared_with_type = "group"
    shared_with_id = $groupId
    permission = "read"
  }
  $script:sharePaperGroupId = [int]$share.id
  Assert-True -Condition ($sharePaperGroupId -gt 0) -Message "group share id missing"
}

Run-Test "H-04 Share knowledge base to all_students" {
  $share = Invoke-JsonRequest -Method Post -Uri "$apiBase/share/" -Headers $mentorHeaders -Body @{
    resource_type = "knowledge_base"
    resource_id = $shareKbId
    shared_with_type = "all_students"
    permission = "read"
  }
  $script:shareKbShareId = [int]$share.id
  Assert-True -Condition ($shareKbShareId -gt 0) -Message "all_students KB share id missing"
}

Run-Test "M-04 Share notebook to student" {
  $share = Invoke-JsonRequest -Method Post -Uri "$apiBase/share/" -Headers $mentorHeaders -Body @{
    resource_type = "notebook"
    resource_id = $sharedNotebookId
    shared_with_type = "user"
    shared_with_id = $studentId
    permission = "read"
  }
  $script:shareNotebookShareId = [int]$share.id
  Assert-True -Condition ($shareNotebookShareId -gt 0) -Message "notebook share id missing"
}

Run-Test "H-03 Share collection and batch share papers" {
  $single = Invoke-JsonRequest -Method Post -Uri "$apiBase/share/" -Headers $mentorHeaders -Body @{
    resource_type = "paper_collection"
    resource_id = $shareCollectionId
    shared_with_type = "user"
    shared_with_id = $studentId
    permission = "read"
  }
  $script:shareCollectionShareId = [int]$single.id
  Assert-True -Condition ($shareCollectionShareId -gt 0) -Message "collection share id missing"

  $batch = Invoke-JsonRequest -Method Post -Uri "$apiBase/share/batch" -Headers $mentorHeaders -Body @{
    resource_type = "paper"
    resource_ids = @($paperAId, $paperBId)
    shared_with_type = "all_students"
    permission = "read"
  }
  Assert-True -Condition ([int]$batch.success_count -ge 1) -Message "batch share should share at least one paper"
}

Run-Test "H-13 Resource search endpoints for sharing modal" {
  $paperSearch = Invoke-JsonRequest -Method Get -Uri "$apiBase/share/my-papers?search=$([uri]::EscapeDataString('Paper A'))&limit=50" -Headers $mentorHeaders
  Assert-True -Condition ((@($paperSearch | Where-Object { $_.id -eq $paperAId }).Count) -ge 1) -Message "my-papers search should find paper A"

  $collectionSearch = Invoke-JsonRequest -Method Get -Uri "$apiBase/share/my-collections?search=$([uri]::EscapeDataString('Share Collection'))" -Headers $mentorHeaders
  Assert-True -Condition ((@($collectionSearch | Where-Object { $_.id -eq $shareCollectionId }).Count) -ge 1) -Message "my-collections search should find collection"

  $kbSearch = Invoke-JsonRequest -Method Get -Uri "$apiBase/share/my-knowledge-bases?search=$([uri]::EscapeDataString('Share KB'))" -Headers $mentorHeaders
  Assert-True -Condition ((@($kbSearch | Where-Object { $_.id -eq $shareKbId }).Count) -ge 1) -Message "my-knowledge-bases search should find KB"

  $notebookSearch = Invoke-JsonRequest -Method Get -Uri "$apiBase/share/my-notebooks?search=$([uri]::EscapeDataString('Shared Notebook'))" -Headers $mentorHeaders
  Assert-True -Condition ((@($notebookSearch | Where-Object { $_.id -eq $sharedNotebookId }).Count) -ge 1) -Message "my-notebooks search should find notebook"
}

Run-Test "H-14 Empty batch selection rejected" {
  Assert-RequestFails -Message "empty batch share should fail" -ExpectedStatusCodes @(400) -Script {
    Invoke-JsonRequest -Method Post -Uri "$apiBase/share/batch" -Headers $mentorHeaders -Body @{
      resource_type = "paper"
      resource_ids = @()
      shared_with_type = "group"
      shared_with_id = $groupId
      permission = "read"
    } | Out-Null
  } | Out-Null
}

Run-Test "H-01 Mentor my-shares list contains recent shares" {
  $shares = Invoke-JsonRequest -Method Get -Uri "$apiBase/share/my-shares?limit=100" -Headers $mentorHeaders
  Assert-True -Condition ((@($shares | Where-Object { $_.id -eq $sharePaperGroupId }).Count) -ge 1) -Message "my-shares should include paper group share"
  Assert-True -Condition ((@($shares | Where-Object { $_.id -eq $shareCollectionShareId }).Count) -ge 1) -Message "my-shares should include collection share"
  Assert-True -Condition ((@($shares | Where-Object { $_.id -eq $shareNotebookShareId }).Count) -ge 1) -Message "my-shares should include notebook share"
}

Run-Test "H-15 shared-with-me count increases for student" {
  $after = Invoke-JsonRequest -Method Get -Uri "$apiBase/share/shared-with-me/count" -Headers $studentHeaders
  Assert-True -Condition ([int]$after.total -gt $sharedCountBefore) -Message "student shared count should increase after mentor shares"
}

Run-Test "H-06/H-07 Student shared paper list and copy paper" {
  $papers = Invoke-JsonRequest -Method Get -Uri "$apiBase/share/shared-with-me?resource_type=paper&limit=100" -Headers $studentHeaders
  Assert-True -Condition (@($papers).Count -ge 1) -Message "student should have at least one shared paper"

  $targetShare = @($papers) | Select-Object -First 1
  Assert-True -Condition ([int]$targetShare.id -gt 0) -Message "target paper share id missing"

  $detail = Invoke-JsonRequest -Method Get -Uri "$apiBase/share/detail/$($targetShare.id)" -Headers $studentHeaders
  Assert-True -Condition ($detail.resource_type -eq "paper") -Message "paper share detail type mismatch"
  Assert-True -Condition ($null -ne $detail.paper) -Message "paper detail payload missing"

  $copied = Invoke-JsonRequest -Method Post -Uri "$apiBase/share/copy-to-library/$($targetShare.id)" -Headers $studentHeaders
  Assert-True -Condition ([int]$copied.paper_id -gt 0) -Message "copy shared paper should return new paper id"
}

Run-Test "H-08 Collection detail and copy papers" {
  $detail = Invoke-JsonRequest -Method Get -Uri "$apiBase/share/detail/$shareCollectionShareId" -Headers $studentHeaders
  Assert-True -Condition ($detail.resource_type -eq "paper_collection") -Message "collection share detail type mismatch"
  Assert-True -Condition ($null -ne $detail.collection) -Message "collection payload missing"

  $copied = Invoke-JsonRequest -Method Post -Uri "$apiBase/share/copy-collection-papers/$shareCollectionShareId" -Headers $studentHeaders
  Assert-True -Condition (([int]$copied.success_count + [int]$copied.skip_count) -ge 1) -Message "copy collection papers should process at least one paper"
}

Run-Test "H-10 Shared notebook detail readable" {
  $detail = Invoke-JsonRequest -Method Get -Uri "$apiBase/share/detail/$shareNotebookShareId" -Headers $studentHeaders
  Assert-True -Condition ($detail.resource_type -eq "notebook") -Message "notebook share detail type mismatch"
  Assert-True -Condition ($detail.notebook.id -eq $sharedNotebookId) -Message "shared notebook id mismatch in detail"
}

Run-Test "M-03 Shared KB visible in student available list" {
  $detail = Invoke-JsonRequest -Method Get -Uri "$apiBase/share/detail/$shareKbShareId" -Headers $studentHeaders
  Assert-True -Condition ($detail.resource_type -eq "knowledge_base") -Message "KB share detail type mismatch"
  Assert-True -Condition ([int]$detail.knowledge_base.id -eq $shareKbId) -Message "shared KB id mismatch in detail"

  $available = Invoke-JsonRequest -Method Get -Uri "$apiBase/knowledge/available?include_shared=true" -Headers $studentHeaders
  $sharedList = @()
  if ($null -ne $available.shared) {
    $sharedList = @($available.shared)
  }
  Assert-True -Condition ((@($sharedList | Where-Object { $_.id -eq $shareKbId }).Count) -ge 1) -Message "student available KBs should include shared KB"
}

Run-Test "H-12 Unauthorized user cannot read shared detail" {
  Assert-RequestFails -Message "outsider should not access share detail" -ExpectedStatusCodes @(404) -Script {
    Invoke-JsonRequest -Method Get -Uri "$apiBase/share/detail/$sharePaperGroupId" -Headers $outsiderHeaders | Out-Null
  } | Out-Null
}

Run-Test "H-05 Cancel share removes access" {
  $removed = Invoke-JsonRequest -Method Delete -Uri "$apiBase/share/$shareNotebookShareId" -Headers $mentorHeaders
  Assert-True -Condition (-not [string]::IsNullOrWhiteSpace($removed.message)) -Message "remove share message missing"

  Assert-RequestFails -Message "student should not access removed notebook share" -ExpectedStatusCodes @(404) -Script {
    Invoke-JsonRequest -Method Get -Uri "$apiBase/share/detail/$shareNotebookShareId" -Headers $studentHeaders | Out-Null
  } | Out-Null
}

Run-Test "MCP negative validation: L-11 invalid JSON and L-14 invalid save" {
  Assert-RequestFails -Message "MCP validate should reject invalid raw_json" -ExpectedStatusCodes @(400) -Script {
    Invoke-JsonRequest -Method Post -Uri "$apiBase/mcp/config/validate" -Headers $baseHeaders -Body @{
      raw_json = "{invalid-json"
    } | Out-Null
  } | Out-Null

  Assert-RequestFails -Message "MCP save should reject empty servers payload" -ExpectedStatusCodes @(400) -Script {
    Invoke-JsonRequest -Method Put -Uri "$apiBase/mcp/config" -Headers $baseHeaders -Body @{
      servers = @()
    } | Out-Null
  } | Out-Null
}

Run-Test "I-09 Cleanup mentor group" {
  $deleted = Invoke-JsonRequest -Method Delete -Uri "$apiBase/mentor/groups/$groupId" -Headers $mentorHeaders
  Assert-True -Condition (-not [string]::IsNullOrWhiteSpace($deleted.message)) -Message "delete mentor group message missing"
}

Write-Host "E2E success: remaining modules API smoke passed." -ForegroundColor Green
