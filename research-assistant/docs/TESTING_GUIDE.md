# Stage 1 Testing Guide

## Overview

Due to Python 3.13 compatibility issues with some packages on Windows, we provide two testing approaches:
1. **Docker-based testing** - Run tests inside Docker containers
2. **API testing** - Test APIs using PowerShell/curl commands

---

## Method 1: Docker-Based Backend Testing

### Step 1: Start Services
```powershell
cd D:\Codefield\agent-platform\research-assistant
docker-compose up -d
```

### Step 2: Run Backend Tests in Docker
```powershell
# Enter the backend container
docker exec -it research_backend bash

# Inside container, run tests
cd /app
pytest -v --tb=short

# Exit container
exit
```

### Step 3: Run Specific Test Files
```powershell
# Test authentication
docker exec research_backend pytest tests/test_auth.py -v

# Test chat functionality  
docker exec research_backend pytest tests/test_chat.py -v

# Test all with coverage
docker exec research_backend pytest --cov=app --cov-report=term-missing
```

---

## Method 2: API Testing with PowerShell

### Prerequisites
Make sure services are running:
```powershell
docker-compose up -d
```

### Test 1: Health Check
```powershell
# Test health endpoint
Invoke-RestMethod -Uri "http://localhost:8000/health" -Method Get

# Expected: status = "healthy"
```

### Test 2: User Registration
```powershell
$body = @{
    email = "testuser@example.com"
    username = "testuser"
    password = "testpassword123"
    full_name = "Test User"
} | ConvertTo-Json

$response = Invoke-RestMethod -Uri "http://localhost:8000/api/auth/register" -Method Post -Body $body -ContentType "application/json"
$response

# Save token for later tests
$token = $response.access_token
```

### Test 3: User Login
```powershell
$body = @{
    email = "testuser@example.com"
    password = "testpassword123"
} | ConvertTo-Json

$response = Invoke-RestMethod -Uri "http://localhost:8000/api/auth/login" -Method Post -Body $body -ContentType "application/json"
$token = $response.access_token
Write-Host "Token: $token"
```

### Test 4: Get Current User
```powershell
$headers = @{
    Authorization = "Bearer $token"
}

Invoke-RestMethod -Uri "http://localhost:8000/api/auth/me" -Method Get -Headers $headers
```

### Test 5: Create Conversation
```powershell
$headers = @{
    Authorization = "Bearer $token"
}
$body = @{
    title = "Test Conversation"
} | ConvertTo-Json

$conv = Invoke-RestMethod -Uri "http://localhost:8000/api/chat/conversations" -Method Post -Headers $headers -Body $body -ContentType "application/json"
$conv

# Save conversation ID
$convId = $conv.id
```

### Test 6: List Conversations
```powershell
$headers = @{
    Authorization = "Bearer $token"
}

Invoke-RestMethod -Uri "http://localhost:8000/api/chat/conversations" -Method Get -Headers $headers
```

### Test 7: Send Message (Non-Streaming)
```powershell
$headers = @{
    Authorization = "Bearer $token"
}
$body = @{
    message = "Hello, how are you?"
    conversation_id = $convId
    stream = $false
} | ConvertTo-Json

Invoke-RestMethod -Uri "http://localhost:8000/api/chat/send" -Method Post -Headers $headers -Body $body -ContentType "application/json"
```

### Test 8: Get Conversation Details
```powershell
$headers = @{
    Authorization = "Bearer $token"
}

Invoke-RestMethod -Uri "http://localhost:8000/api/chat/conversations/$convId" -Method Get -Headers $headers
```

### Test 9: Delete Conversation
```powershell
$headers = @{
    Authorization = "Bearer $token"
}

Invoke-RestMethod -Uri "http://localhost:8000/api/chat/conversations/$convId" -Method Delete -Headers $headers
```

### Test 10: Get LLM Providers
```powershell
$headers = @{
    Authorization = "Bearer $token"
}

Invoke-RestMethod -Uri "http://localhost:8000/api/users/llm-providers" -Method Get -Headers $headers
```

---

## Method 3: Complete PowerShell Test Script

Save the following as `test_api.ps1` and run it:

```powershell
# test_api.ps1 - API Test Script

$baseUrl = "http://localhost:8000"
$testEmail = "apitest_$(Get-Random)@example.com"
$testUsername = "apitest_$(Get-Random)"
$testPassword = "testpassword123"

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "Research Assistant API Tests" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

# Test 1: Health Check
Write-Host "`n[Test 1] Health Check..." -ForegroundColor Yellow
try {
    $health = Invoke-RestMethod -Uri "$baseUrl/health" -Method Get
    if ($health.status -eq "healthy") {
        Write-Host "[PASS] Health check passed" -ForegroundColor Green
    } else {
        Write-Host "[FAIL] Health check failed" -ForegroundColor Red
    }
} catch {
    Write-Host "[FAIL] Health check error: $_" -ForegroundColor Red
    exit 1
}

# Test 2: Registration
Write-Host "`n[Test 2] User Registration..." -ForegroundColor Yellow
try {
    $regBody = @{
        email = $testEmail
        username = $testUsername
        password = $testPassword
    } | ConvertTo-Json
    
    $regResponse = Invoke-RestMethod -Uri "$baseUrl/api/auth/register" -Method Post -Body $regBody -ContentType "application/json"
    $token = $regResponse.access_token
    
    if ($token) {
        Write-Host "[PASS] Registration successful" -ForegroundColor Green
    } else {
        Write-Host "[FAIL] No token received" -ForegroundColor Red
    }
} catch {
    Write-Host "[FAIL] Registration error: $_" -ForegroundColor Red
    exit 1
}

# Test 3: Login
Write-Host "`n[Test 3] User Login..." -ForegroundColor Yellow
try {
    $loginBody = @{
        email = $testEmail
        password = $testPassword
    } | ConvertTo-Json
    
    $loginResponse = Invoke-RestMethod -Uri "$baseUrl/api/auth/login" -Method Post -Body $loginBody -ContentType "application/json"
    $token = $loginResponse.access_token
    
    if ($token) {
        Write-Host "[PASS] Login successful" -ForegroundColor Green
    } else {
        Write-Host "[FAIL] Login failed" -ForegroundColor Red
    }
} catch {
    Write-Host "[FAIL] Login error: $_" -ForegroundColor Red
    exit 1
}

$headers = @{ Authorization = "Bearer $token" }

# Test 4: Get Current User
Write-Host "`n[Test 4] Get Current User..." -ForegroundColor Yellow
try {
    $me = Invoke-RestMethod -Uri "$baseUrl/api/auth/me" -Method Get -Headers $headers
    if ($me.email -eq $testEmail) {
        Write-Host "[PASS] Get user successful" -ForegroundColor Green
    } else {
        Write-Host "[FAIL] User mismatch" -ForegroundColor Red
    }
} catch {
    Write-Host "[FAIL] Get user error: $_" -ForegroundColor Red
}

# Test 5: Create Conversation
Write-Host "`n[Test 5] Create Conversation..." -ForegroundColor Yellow
try {
    $convBody = @{ title = "Test Conversation" } | ConvertTo-Json
    $conv = Invoke-RestMethod -Uri "$baseUrl/api/chat/conversations" -Method Post -Headers $headers -Body $convBody -ContentType "application/json"
    $convId = $conv.id
    
    if ($convId) {
        Write-Host "[PASS] Conversation created (ID: $convId)" -ForegroundColor Green
    } else {
        Write-Host "[FAIL] No conversation ID" -ForegroundColor Red
    }
} catch {
    Write-Host "[FAIL] Create conversation error: $_" -ForegroundColor Red
}

# Test 6: List Conversations
Write-Host "`n[Test 6] List Conversations..." -ForegroundColor Yellow
try {
    $convList = Invoke-RestMethod -Uri "$baseUrl/api/chat/conversations" -Method Get -Headers $headers
    if ($convList -is [array]) {
        Write-Host "[PASS] Listed $($convList.Count) conversations" -ForegroundColor Green
    } else {
        Write-Host "[PASS] Conversations retrieved" -ForegroundColor Green
    }
} catch {
    Write-Host "[FAIL] List conversations error: $_" -ForegroundColor Red
}

# Test 7: Get Conversation Detail
Write-Host "`n[Test 7] Get Conversation Detail..." -ForegroundColor Yellow
try {
    $convDetail = Invoke-RestMethod -Uri "$baseUrl/api/chat/conversations/$convId" -Method Get -Headers $headers
    if ($convDetail.id -eq $convId) {
        Write-Host "[PASS] Got conversation detail" -ForegroundColor Green
    } else {
        Write-Host "[FAIL] Conversation ID mismatch" -ForegroundColor Red
    }
} catch {
    Write-Host "[FAIL] Get conversation error: $_" -ForegroundColor Red
}

# Test 8: Get LLM Providers
Write-Host "`n[Test 8] Get LLM Providers..." -ForegroundColor Yellow
try {
    $providers = Invoke-RestMethod -Uri "$baseUrl/api/users/llm-providers" -Method Get -Headers $headers
    if ($providers.providers) {
        Write-Host "[PASS] Got $($providers.providers.Count) providers" -ForegroundColor Green
    } else {
        Write-Host "[FAIL] No providers returned" -ForegroundColor Red
    }
} catch {
    Write-Host "[FAIL] Get providers error: $_" -ForegroundColor Red
}

# Test 9: Delete Conversation
Write-Host "`n[Test 9] Delete Conversation..." -ForegroundColor Yellow
try {
    $null = Invoke-RestMethod -Uri "$baseUrl/api/chat/conversations/$convId" -Method Delete -Headers $headers
    Write-Host "[PASS] Conversation deleted" -ForegroundColor Green
} catch {
    Write-Host "[FAIL] Delete conversation error: $_" -ForegroundColor Red
}

# Test 10: Verify Deletion
Write-Host "`n[Test 10] Verify Deletion..." -ForegroundColor Yellow
try {
    $null = Invoke-RestMethod -Uri "$baseUrl/api/chat/conversations/$convId" -Method Get -Headers $headers
    Write-Host "[FAIL] Conversation still exists" -ForegroundColor Red
} catch {
    Write-Host "[PASS] Conversation properly deleted (404)" -ForegroundColor Green
}

Write-Host "`n========================================" -ForegroundColor Cyan
Write-Host "API Tests Completed!" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
```

---

## Method 4: Manual Testing Checklist

### Frontend Testing (Browser)

1. **Open Application**: http://localhost:3000

2. **Registration Test**
   - [ ] Navigate to register page
   - [ ] Fill in email, username, password
   - [ ] Submit and verify redirect to dashboard

3. **Login Test**
   - [ ] Logout if logged in
   - [ ] Navigate to login page
   - [ ] Enter credentials
   - [ ] Verify successful login

4. **Dashboard Test**
   - [ ] Verify welcome message shows username
   - [ ] Check statistics cards display
   - [ ] Test quick input box
   - [ ] Click quick action buttons

5. **Chat Test**
   - [ ] Click "New Chat" button
   - [ ] Type a message and send
   - [ ] Verify streaming response appears
   - [ ] Check ReAct thinking panel (if enabled)
   - [ ] Test Markdown rendering
   - [ ] Test code syntax highlighting

6. **Conversation Management**
   - [ ] Create multiple conversations
   - [ ] Switch between conversations
   - [ ] Delete a conversation
   - [ ] Archive a conversation

7. **UI/UX Test**
   - [ ] Toggle sidebar collapse
   - [ ] Check responsive layout
   - [ ] Verify dark theme renders correctly
   - [ ] Test loading states

---

## Troubleshooting

### Issue: Services not starting
```powershell
# Check logs
docker-compose logs backend
docker-compose logs postgres

# Restart services
docker-compose down
docker-compose up -d --build
```

### Issue: Database connection error
```powershell
# Check if postgres is healthy
docker exec research_postgres pg_isready -U research_user

# Recreate database
docker-compose down -v
docker-compose up -d
```

### Issue: API returns 401
- Token may have expired
- Re-login to get a new token

### Issue: CORS error in browser
- Make sure backend is running on port 8000
- Check browser console for details

---

## Test Results Template

| Test | Status | Notes |
|------|--------|-------|
| Health Check | [ ] Pass / [ ] Fail | |
| User Registration | [ ] Pass / [ ] Fail | |
| User Login | [ ] Pass / [ ] Fail | |
| Get Current User | [ ] Pass / [ ] Fail | |
| Create Conversation | [ ] Pass / [ ] Fail | |
| List Conversations | [ ] Pass / [ ] Fail | |
| Send Message | [ ] Pass / [ ] Fail | |
| Stream Message | [ ] Pass / [ ] Fail | |
| Delete Conversation | [ ] Pass / [ ] Fail | |
| Frontend Loads | [ ] Pass / [ ] Fail | |
| Chat UI Works | [ ] Pass / [ ] Fail | |
