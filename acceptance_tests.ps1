
$baseUrl = "http://localhost:8888/api/v1"
$email = "test_acceptance_v2@example.com"
$password = "Password123!"

function Run-Test {
    param($Name, $ScriptBlock)
    Write-Host "Running Test: $Name..." -NoNewline
    try {
        & $ScriptBlock
        Write-Host " [PASS]" -ForegroundColor Green
    } catch {
        Write-Host " [FAIL]" -ForegroundColor Red
        Write-Host "Error: $_" -ForegroundColor Red
        if ($_.Exception.Response) {
             # safely try to read response body
             try {
                $stream = $_.Exception.Response.GetResponseStream()
                if ($stream) {
                    $reader = New-Object System.IO.StreamReader $stream
                    $responseBody = $reader.ReadToEnd()
                    Write-Host "Response Body: $responseBody" -ForegroundColor Yellow
                }
             } catch {}
        }
        exit 1
    }
}

# 1. Auth
$token = $null
try {
    Write-Host "Attempting Register..."
    $body = @{
        email = $email
        username = "test_acceptance_v2"
        password = $password
        full_name = "Test Acceptance V2"
    } | ConvertTo-Json
    $response = Invoke-RestMethod -Uri "$baseUrl/auth/register" -Method Post -Body $body -ContentType "application/json" -ErrorAction Stop
    $token = $response.access_token
} catch {
    Write-Host "Register failed, trying Login..."
    try {
        $body = @{
            email = $email
            password = $password
        } | ConvertTo-Json
        $response = Invoke-RestMethod -Uri "$baseUrl/auth/login" -Method Post -Body $body -ContentType "application/json" -ErrorAction Stop
        $token = $response.access_token
    } catch {
        Write-Host "Login failed: $_" -ForegroundColor Red
        exit 1
    }
}

if (-not $token) {
    Write-Host "Failed to get token." -ForegroundColor Red
    exit 1
}
$headers = @{ Authorization = "Bearer $token" }
Write-Host "Got Token."

# Test A: API Presets
Run-Test "A. API Presets" {
    $response = Invoke-RestMethod -Uri "$baseUrl/chunking/presets" -Method Get
    $presets = $response.presets
    if ($presets.Count -ne 5) { throw "Expected 5 presets, got $($presets.Count)" }
}

# Test B: Chunking Preview (Academic)
Run-Test "B. Chunking Preview (Academic)" {
    $text = "# Abstract`nThis is a new method.`n`n# 1. Introduction`nDeep learning is fast.`n`n# References`n[1] Vaswani A, et al. Attention. 2017."
    $body = @{
        text = $text
        preset = "academic"
    } | ConvertTo-Json -Depth 10
    
    $resp = Invoke-RestMethod -Uri "$baseUrl/chunking/preview" -Method Post -Headers $headers -Body $body -ContentType "application/json"
    
    if ($resp.strategy -ne "academic") { throw "Expected strategy 'academic', got '$($resp.strategy)'" }
    if ($resp.stats.total_chunks -eq 0) { throw "No chunks returned" }
}

# Test C: Document Analysis
Run-Test "C. Document Analysis" {
    $text = "# Abstract`nThis is a new method.`n# 1. Introduction`nContext here."
    $body = @{ text = $text } | ConvertTo-Json
    $resp = Invoke-RestMethod -Uri "$baseUrl/chunking/analyze" -Method Post -Headers $headers -Body $body -ContentType "application/json"
    
    if (-not $resp.is_academic) { throw "Expected is_academic=true" }
    if ($resp.recommended_strategy -ne "academic") { throw "Expected recommended_strategy 'academic', got '$($resp.recommended_strategy)'" }
}

# Test D: Backward Compatibility
Run-Test "D. Backward Compatibility" {
    $body = @{
        text = "Test text. Simple content."
        config = @{
            strategy = "fixed"
            base_chunk_size = 200
            semantic_threshold = 0.65
        }
    } | ConvertTo-Json
    
    $resp = Invoke-RestMethod -Uri "$baseUrl/chunking/preview" -Method Post -Headers $headers -Body $body -ContentType "application/json"
}

# Test E: New Parameter breakpoint_percentile
Run-Test "E. New Parameter breakpoint_percentile" {
    $longText = "Sentence 1. " * 20 + "Sentence 2. " * 20
    $body = @{
        text = $longText
        config = @{
            strategy = "semantic"
            breakpoint_percentile = 85.0
        }
    } | ConvertTo-Json
    
    $resp = Invoke-RestMethod -Uri "$baseUrl/chunking/preview" -Method Post -Headers $headers -Body $body -ContentType "application/json"
    if ($resp.strategy -ne "semantic") { throw "Expected strategy 'semantic'" }
}

# Test F: Concurrency
Run-Test "F. Concurrency" {
    # Using ForEach-Object -Parallel requires PS7. Using Start-Job for compatibility.
    $jobs = @()
    for ($i=1; $i -le 10; $i++) {
        $jobs += Start-Job -ScriptBlock {
            param($url, $h, $i)
            # Reconstruct headers inside job as passing hashtable in args works but let's be safe
            # Actually just passing $h should work
            $json = '{"text": "Concurrency test", "preset": "fast"}'
            try {
               $r = Invoke-RestMethod -Uri "$url/chunking/preview" -Method Post -Headers $h -Body $json -ContentType "application/json" -ErrorAction Stop
               return "OK"
            } catch {
               return "FAIL: $_"
            }
        } -ArgumentList $baseUrl, $headers, $i
    }
    
    $results = $jobs | Receive-Job -Wait
    $failures = $results | Where-Object { $_ -ne "OK" }
    if ($failures) {
        throw "Concurrency failures: $failures"
    }
}

Write-Host "ALL TESTS PASSED" -ForegroundColor Cyan
