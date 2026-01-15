# test_api.ps1 - Research Assistant API Test Script
# Usage: .\test_api.ps1

param(
    [string]$BaseUrl = "http://localhost:8000"
)

$testEmail = "apitest_$(Get-Random)@example.com"
$testUsername = "apitest_$(Get-Random)"
$testPassword = "testpassword123"
$passed = 0
$failed = 0

function Test-Api {
    param(
        [string]$Name,
        [scriptblock]$TestBlock
    )
    
    Write-Host "`n[$Name]" -ForegroundColor Yellow
    try {
        $result = & $TestBlock
        if ($result) {
            Write-Host "  PASS" -ForegroundColor Green
            $script:passed++
        } else {
            Write-Host "  FAIL" -ForegroundColor Red
            $script:failed++
        }
    } catch {
        Write-Host "  FAIL: $_" -ForegroundColor Red
        $script:failed++
    }
}

Write-Host "========================================" -ForegroundColor Cyan
Write-Host " Research Assistant - API Tests" -ForegroundColor Cyan
Write-Host " Base URL: $BaseUrl" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

# Test 1: Health Check
Test-Api "Health Check" {
    $response = Invoke-RestMethod -Uri "$BaseUrl/health" -Method Get -TimeoutSec 10
    return $response.status -eq "healthy"
}

# Test 2: Root Endpoint
Test-Api "Root Endpoint" {
    $response = Invoke-RestMethod -Uri "$BaseUrl/" -Method Get -TimeoutSec 10
    return $response.status -eq "running"
}

# Test 3: Registration
$token = $null
Test-Api "User Registration" {
    $body = @{
        email = $testEmail
        username = $testUsername
        password = $testPassword
    } | ConvertTo-Json
    
    $response = Invoke-RestMethod -Uri "$BaseUrl/api/auth/register" -Method Post -Body $body -ContentType "application/json" -TimeoutSec 10
    $script:token = $response.access_token
    return $null -ne $script:token
}

# Test 4: Duplicate Registration (should fail)
Test-Api "Duplicate Registration Blocked" {
    $body = @{
        email = $testEmail
        username = "different_$testUsername"
        password = $testPassword
    } | ConvertTo-Json
    
    try {
        $null = Invoke-RestMethod -Uri "$BaseUrl/api/auth/register" -Method Post -Body $body -ContentType "application/json" -TimeoutSec 10
        return $false  # Should have thrown
    } catch {
        return $true  # Expected to fail
    }
}

# Test 5: Login
Test-Api "User Login" {
    $body = @{
        email = $testEmail
        password = $testPassword
    } | ConvertTo-Json
    
    $response = Invoke-RestMethod -Uri "$BaseUrl/api/auth/login" -Method Post -Body $body -ContentType "application/json" -TimeoutSec 10
    $script:token = $response.access_token
    return $null -ne $script:token
}

# Test 6: Wrong Password (should fail)
Test-Api "Wrong Password Blocked" {
    $body = @{
        email = $testEmail
        password = "wrongpassword"
    } | ConvertTo-Json
    
    try {
        $null = Invoke-RestMethod -Uri "$BaseUrl/api/auth/login" -Method Post -Body $body -ContentType "application/json" -TimeoutSec 10
        return $false
    } catch {
        return $true
    }
}

$headers = @{ Authorization = "Bearer $token" }

# Test 7: Get Current User
Test-Api "Get Current User" {
    $response = Invoke-RestMethod -Uri "$BaseUrl/api/auth/me" -Method Get -Headers $headers -TimeoutSec 10
    return $response.email -eq $testEmail
}

# Test 8: Unauthorized Access (should fail)
Test-Api "Unauthorized Access Blocked" {
    try {
        $null = Invoke-RestMethod -Uri "$BaseUrl/api/auth/me" -Method Get -TimeoutSec 10
        return $false
    } catch {
        return $true
    }
}

# Test 9: Get User Profile
Test-Api "Get User Profile" {
    $response = Invoke-RestMethod -Uri "$BaseUrl/api/users/profile" -Method Get -Headers $headers -TimeoutSec 10
    return $response.username -eq $testUsername
}

# Test 10: Get LLM Providers
Test-Api "Get LLM Providers" {
    $response = Invoke-RestMethod -Uri "$BaseUrl/api/users/llm-providers" -Method Get -Headers $headers -TimeoutSec 10
    return $response.providers.Count -gt 0
}

# Test 11: Create Conversation
$convId = $null
Test-Api "Create Conversation" {
    $body = @{ title = "Test Conversation" } | ConvertTo-Json
    $response = Invoke-RestMethod -Uri "$BaseUrl/api/chat/conversations" -Method Post -Headers $headers -Body $body -ContentType "application/json" -TimeoutSec 10
    $script:convId = $response.id
    return $null -ne $script:convId
}

# Test 12: List Conversations
Test-Api "List Conversations" {
    $response = Invoke-RestMethod -Uri "$BaseUrl/api/chat/conversations" -Method Get -Headers $headers -TimeoutSec 10
    return $response.Count -ge 1
}

# Test 13: Get Conversation Detail
Test-Api "Get Conversation Detail" {
    $response = Invoke-RestMethod -Uri "$BaseUrl/api/chat/conversations/$convId" -Method Get -Headers $headers -TimeoutSec 10
    return $response.id -eq $convId
}

# Test 14: Get Non-existent Conversation (should fail)
Test-Api "Non-existent Conversation Returns 404" {
    try {
        $null = Invoke-RestMethod -Uri "$BaseUrl/api/chat/conversations/99999" -Method Get -Headers $headers -TimeoutSec 10
        return $false
    } catch {
        return $true
    }
}

# Test 15: Archive Conversation
Test-Api "Archive Conversation" {
    $response = Invoke-RestMethod -Uri "$BaseUrl/api/chat/conversations/$convId/archive" -Method Put -Headers $headers -TimeoutSec 10
    return $response.is_archived -eq 1
}

# Test 16: Unarchive Conversation
Test-Api "Unarchive Conversation" {
    $response = Invoke-RestMethod -Uri "$BaseUrl/api/chat/conversations/$convId/archive" -Method Put -Headers $headers -TimeoutSec 10
    return $response.is_archived -eq 0
}

# Test 17: Delete Conversation
Test-Api "Delete Conversation" {
    $null = Invoke-RestMethod -Uri "$BaseUrl/api/chat/conversations/$convId" -Method Delete -Headers $headers -TimeoutSec 10
    return $true
}

# Test 18: Verify Deletion
Test-Api "Verify Conversation Deleted" {
    try {
        $null = Invoke-RestMethod -Uri "$BaseUrl/api/chat/conversations/$convId" -Method Get -Headers $headers -TimeoutSec 10
        return $false
    } catch {
        return $true
    }
}

# Summary
Write-Host "`n========================================" -ForegroundColor Cyan
Write-Host " Test Results" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host " Passed: $passed" -ForegroundColor Green
Write-Host " Failed: $failed" -ForegroundColor $(if ($failed -gt 0) { "Red" } else { "Green" })
Write-Host " Total:  $($passed + $failed)" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

if ($failed -gt 0) {
    exit 1
}
