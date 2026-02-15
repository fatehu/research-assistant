param(
  [string]$BackendBaseUrl = "http://localhost:8888",
  [string]$DbContainer = "research_postgres",
  [string]$DbUser = "",
  [string]$DbName = "",
  [string]$EnvFilePath = ".env"
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
    return Invoke-RestMethod -Method $Method -Uri $Uri -Headers $Headers -ContentType "application/json" -Body ($Body | ConvertTo-Json -Depth 20)
  }
  return Invoke-RestMethod -Method $Method -Uri $Uri -Headers $Headers
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

$dbUserResolved = if ([string]::IsNullOrWhiteSpace($DbUser)) { Get-EnvValue -Path $EnvFilePath -Key "POSTGRES_USER" } else { $DbUser }
$dbNameResolved = if ([string]::IsNullOrWhiteSpace($DbName)) { Get-EnvValue -Path $EnvFilePath -Key "POSTGRES_DB" } else { $DbName }
if ([string]::IsNullOrWhiteSpace($dbUserResolved)) { $dbUserResolved = "research_user" }
if ([string]::IsNullOrWhiteSpace($dbNameResolved)) { $dbNameResolved = "research_assistant" }

$ts = Get-Date -Format "yyyyMMddHHmmss"
$adminUser = @{
  email = "admin_api_$ts@example.com"
  username = "admin_api_$ts"
  password = "AdminApi!$ts"
  full_name = "Admin API $ts"
}
$mentorUser = @{
  email = "mentor_api_$ts@example.com"
  username = "mentor_api_$ts"
  password = "MentorApi!$ts"
  full_name = "Mentor API $ts"
}
$studentUser = @{
  email = "student_api_$ts@example.com"
  username = "student_api_$ts"
  password = "StudentApi!$ts"
  full_name = "Student API $ts"
}

Write-Host "=== Role Business API Smoke E2E ===" -ForegroundColor Cyan
Write-Host "Backend: $BackendBaseUrl"
Write-Host "DB: $DbContainer / $dbUserResolved / $dbNameResolved"

$adminReg = Register-User -User $adminUser
$mentorReg = Register-User -User $mentorUser
$studentReg = Register-User -User $studentUser

Set-DbRole -Email $adminUser.email -Role "admin"
Set-DbRole -Email $mentorUser.email -Role "mentor"

$adminToken = (Login-User -Email $adminUser.email -Password $adminUser.password).access_token
$mentorLoginResp = Login-User -Email $mentorUser.email -Password $mentorUser.password
$mentorToken = $mentorLoginResp.access_token
$studentLoginResp = Login-User -Email $studentUser.email -Password $studentUser.password
$studentToken = $studentLoginResp.access_token

$adminHeaders = @{ Authorization = "Bearer $adminToken" }
$mentorHeaders = @{ Authorization = "Bearer $mentorToken" }
$studentHeaders = @{ Authorization = "Bearer $studentToken" }

$mentorId = $mentorLoginResp.user.id
$studentId = $studentLoginResp.user.id

$adminManagedUser = @{
  email = "managed_user_$ts@example.com"
  username = "managed_user_$ts"
  password = "Managed!$ts"
  full_name = "Managed User $ts"
  role = "student"
}

$script:managedUserId = $null
$managedUserNewPassword = "ManagedNew!$ts"
$script:groupId = $null
$script:announcementId = $null

Run-Test "K-01 Admin list users" {
  $users = Invoke-JsonRequest -Method Get -Uri "$apiBase/admin/users?skip=0&limit=20" -Headers $adminHeaders
  Assert-True -Condition (@($users).Count -ge 3) -Message "admin users list should include seeded users"
}

Run-Test "K-02 Admin search/filter users" {
  $search = [uri]::EscapeDataString($mentorUser.username)
  $bySearch = Invoke-JsonRequest -Method Get -Uri "$apiBase/admin/users?search=$search&limit=20" -Headers $adminHeaders
  Assert-True -Condition ((@($bySearch | Where-Object { $_.email -eq $mentorUser.email }).Count) -ge 1) -Message "search should find mentor user"

  $byRole = Invoke-JsonRequest -Method Get -Uri "$apiBase/admin/users?role=mentor&limit=50" -Headers $adminHeaders
  Assert-True -Condition ((@($byRole | Where-Object { $_.email -eq $mentorUser.email }).Count) -ge 1) -Message "role filter should include mentor"
}

Run-Test "K-03 Admin create user" {
  $created = Invoke-JsonRequest -Method Post -Uri "$apiBase/admin/users" -Headers $adminHeaders -Body $adminManagedUser
  $script:managedUserId = $created.id
  Assert-True -Condition ($managedUserId -gt 0) -Message "created managed user id missing"
}

Run-Test "K-04 Admin update user profile" {
  $updated = Invoke-JsonRequest -Method Put -Uri "$apiBase/admin/users/$managedUserId" -Headers $adminHeaders -Body @{
    full_name = "Managed User Updated $ts"
    department = "Test Dept"
  }
  Assert-True -Condition ($updated.full_name -eq "Managed User Updated $ts") -Message "full_name not updated"
}

Run-Test "K-05 Admin update user role" {
  $roleToMentor = Invoke-JsonRequest -Method Put -Uri "$apiBase/admin/users/$managedUserId/role" -Headers $adminHeaders -Body @{ role = "mentor" }
  Assert-True -Condition ($roleToMentor.message -match "mentor") -Message "role update to mentor failed"
  $roleToStudent = Invoke-JsonRequest -Method Put -Uri "$apiBase/admin/users/$managedUserId/role" -Headers $adminHeaders -Body @{ role = "student" }
  Assert-True -Condition ($roleToStudent.message -match "student") -Message "role rollback to student failed"
}

Run-Test "K-06 Admin toggle user active" {
  $disabled = Invoke-JsonRequest -Method Put -Uri "$apiBase/admin/users/$managedUserId/toggle-active" -Headers $adminHeaders
  Assert-True -Condition ($disabled.is_active -eq $false) -Message "toggle disable failed"
  $enabled = Invoke-JsonRequest -Method Put -Uri "$apiBase/admin/users/$managedUserId/toggle-active" -Headers $adminHeaders
  Assert-True -Condition ($enabled.is_active -eq $true) -Message "toggle enable failed"
}

Run-Test "K-07 Admin reset user password" {
  $resp = Invoke-JsonRequest -Method Put -Uri "$apiBase/admin/users/$managedUserId/password" -Headers $adminHeaders -Body @{ password = $managedUserNewPassword }
  Assert-True -Condition (-not [string]::IsNullOrWhiteSpace($resp.message)) -Message "password reset response missing message"
  $login = Login-User -Email $adminManagedUser.email -Password $managedUserNewPassword
  Assert-True -Condition (-not [string]::IsNullOrWhiteSpace($login.access_token)) -Message "managed user login with new password failed"
}

Run-Test "K-09 Admin statistics consistency" {
  $stats = Invoke-JsonRequest -Method Get -Uri "$apiBase/admin/statistics" -Headers $adminHeaders
  $sumRoles = [int]$stats.admin_count + [int]$stats.mentor_count + [int]$stats.student_count
  Assert-True -Condition ([int]$stats.total_users -eq $sumRoles) -Message "total_users should equal role counts sum"
}

Run-Test "K-08 Admin delete user" {
  $deleted = Invoke-JsonRequest -Method Delete -Uri "$apiBase/admin/users/$managedUserId" -Headers $adminHeaders
  Assert-True -Condition (-not [string]::IsNullOrWhiteSpace($deleted.message)) -Message "delete user response missing message"
  $loginFailed = $false
  try {
    Login-User -Email $adminManagedUser.email -Password $managedUserNewPassword | Out-Null
  }
  catch {
    $loginFailed = $true
  }
  Assert-True -Condition $loginFailed -Message "deleted user should not be able to login"
}

Run-Test "J-02 Student search mentor" {
  $q = [uri]::EscapeDataString($mentorUser.username)
  $result = Invoke-JsonRequest -Method Get -Uri "$apiBase/student/mentors/search?query=$q" -Headers $studentHeaders
  Assert-True -Condition ((@($result | Where-Object { $_.id -eq $mentorId }).Count) -ge 1) -Message "mentor search should include mentor"
}

Run-Test "J-03 Student apply to mentor" {
  $apply = Invoke-JsonRequest -Method Post -Uri "$apiBase/student/mentor/apply" -Headers $studentHeaders -Body @{ mentor_id = $mentorId; message = "API smoke apply" }
  Assert-True -Condition ($apply.invitation_id -gt 0) -Message "apply should return invitation_id"
}

$script:pendingInvitationId = $null
Run-Test "I-04 Mentor accept student application" {
  $received = Invoke-JsonRequest -Method Get -Uri "$apiBase/invitations/received?status=pending&limit=50" -Headers $mentorHeaders
  $target = @($received | Where-Object { $_.from_user_id -eq $studentId -and $_.status -eq "pending" }) | Select-Object -First 1
  Assert-True -Condition ($null -ne $target) -Message "mentor should receive pending student application"
  $script:pendingInvitationId = $target.id
  $accepted = Invoke-JsonRequest -Method Post -Uri "$apiBase/invitations/$pendingInvitationId/accept" -Headers $mentorHeaders
  Assert-True -Condition (-not [string]::IsNullOrWhiteSpace($accepted.message)) -Message "accept invitation response missing message"
}

Run-Test "I-01 Mentor students list includes student" {
  $students = Invoke-JsonRequest -Method Get -Uri "$apiBase/mentor/students" -Headers $mentorHeaders
  Assert-True -Condition ((@($students | Where-Object { $_.id -eq $studentId }).Count) -ge 1) -Message "mentor students list should include accepted student"
}

Run-Test "J-01 Student mentor relation visible after accept" {
  $myMentor = Invoke-JsonRequest -Method Get -Uri "$apiBase/student/mentor" -Headers $studentHeaders
  Assert-True -Condition ($myMentor.id -eq $mentorId) -Message "student mentor info should match accepted mentor"
}

Run-Test "I-07 Mentor create group" {
  $group = Invoke-JsonRequest -Method Post -Uri "$apiBase/mentor/groups" -Headers $mentorHeaders -Body @{ name = "API Smoke Group $ts"; description = "role business smoke"; max_members = 10 }
  $script:groupId = $group.id
  Assert-True -Condition ($groupId -gt 0) -Message "create group should return id"
}

Run-Test "I-10 Mentor add group member" {
  $resp = Invoke-JsonRequest -Method Post -Uri "$apiBase/mentor/groups/$groupId/members" -Headers $mentorHeaders -Body @{ user_id = $studentId }
  Assert-True -Condition (-not [string]::IsNullOrWhiteSpace($resp.message)) -Message "add group member response missing message"
}

Run-Test "I-01 Mentor group members contains student" {
  $members = Invoke-JsonRequest -Method Get -Uri "$apiBase/mentor/groups/$groupId/members" -Headers $mentorHeaders
  Assert-True -Condition ((@($members | Where-Object { $_.user_id -eq $studentId }).Count) -ge 1) -Message "group members should contain student"
}

Run-Test "I-12 Mentor publish announcement" {
  $announcement = Invoke-JsonRequest -Method Post -Uri "$apiBase/announcements/" -Headers $mentorHeaders -Body @{ title = "API Smoke Announcement $ts"; content = "role business smoke announcement"; group_id = $groupId; is_pinned = $true }
  $script:announcementId = $announcement.id
  Assert-True -Condition ($announcementId -gt 0) -Message "create announcement should return id"
}

$script:unreadBefore = 0
Run-Test "J-07 Student announcement list and unread count" {
  $unread = Invoke-JsonRequest -Method Get -Uri "$apiBase/student/announcements/unread-count" -Headers $studentHeaders
  $script:unreadBefore = [int]$unread.count
  $annList = Invoke-JsonRequest -Method Get -Uri "$apiBase/student/announcements?limit=50" -Headers $studentHeaders
  Assert-True -Condition ((@($annList | Where-Object { $_.id -eq $announcementId }).Count) -ge 1) -Message "student announcements should include mentor announcement"
}

Run-Test "J-08 Student mark announcement read" {
  $mark = Invoke-JsonRequest -Method Post -Uri "$apiBase/student/announcements/$announcementId/read" -Headers $studentHeaders
  Assert-True -Condition (-not [string]::IsNullOrWhiteSpace($mark.message)) -Message "mark read response missing message"
  $unreadAfter = [int](Invoke-JsonRequest -Method Get -Uri "$apiBase/student/announcements/unread-count" -Headers $studentHeaders).count
  Assert-True -Condition ($unreadAfter -le $unreadBefore) -Message "unread count should not increase after mark read"
}

Run-Test "I-13 Mentor update announcement state" {
  $updated = Invoke-JsonRequest -Method Put -Uri "$apiBase/announcements/$announcementId" -Headers $mentorHeaders -Body @{ is_pinned = $false }
  Assert-True -Condition ($updated.is_pinned -eq $false) -Message "announcement pin state should update to false"
}

Run-Test "I-14 Mentor delete announcement" {
  $deleted = Invoke-JsonRequest -Method Delete -Uri "$apiBase/announcements/$announcementId" -Headers $mentorHeaders
  Assert-True -Condition (-not [string]::IsNullOrWhiteSpace($deleted.message)) -Message "delete announcement response missing message"
}

Run-Test "I-11 Mentor remove group member" {
  $removed = Invoke-JsonRequest -Method Delete -Uri "$apiBase/mentor/groups/$groupId/members/$studentId" -Headers $mentorHeaders
  Assert-True -Condition (-not [string]::IsNullOrWhiteSpace($removed.message)) -Message "remove group member response missing message"
}

Run-Test "I-09 Mentor delete group" {
  $deleted = Invoke-JsonRequest -Method Delete -Uri "$apiBase/mentor/groups/$groupId" -Headers $mentorHeaders
  Assert-True -Condition (-not [string]::IsNullOrWhiteSpace($deleted.message)) -Message "delete group response missing message"
}

Run-Test "J-06 Student leave mentor" {
  $left = Invoke-JsonRequest -Method Post -Uri "$apiBase/student/mentor/leave" -Headers $studentHeaders
  Assert-True -Condition (-not [string]::IsNullOrWhiteSpace($left.message)) -Message "leave mentor response missing message"
}

Run-Test "I-01 Mentor students list excludes student after leave" {
  $students = Invoke-JsonRequest -Method Get -Uri "$apiBase/mentor/students" -Headers $mentorHeaders
  Assert-True -Condition ((@($students | Where-Object { $_.id -eq $studentId }).Count) -eq 0) -Message "student should be removed from mentor list after leave"
}

Write-Host "E2E success: role business API smoke passed." -ForegroundColor Green
