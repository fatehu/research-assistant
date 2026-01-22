#!/bin/bash
#
# CodeLab API 快速测试脚本
# 用法: ./test_codelab.sh [API_URL]
#

set -e

API_URL="${1:-http://localhost:8000}"
TOKEN=""
NOTEBOOK_ID=""

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info() { echo -e "${BLUE}ℹ️  $1${NC}"; }
log_success() { echo -e "${GREEN}✅ $1${NC}"; }
log_error() { echo -e "${RED}❌ $1${NC}"; }
log_warning() { echo -e "${YELLOW}⚠️  $1${NC}"; }

echo "=========================================="
echo "CodeLab API 快速测试"
echo "API URL: $API_URL"
echo "=========================================="

# 1. 健康检查
echo -e "\n${BLUE}🧪 测试: 健康检查${NC}"
HEALTH=$(curl -s -o /dev/null -w "%{http_code}" "$API_URL/health")
if [ "$HEALTH" = "200" ]; then
    log_success "健康检查通过"
else
    log_error "健康检查失败 (HTTP $HEALTH)"
    exit 1
fi

# 2. 用户注册/登录
echo -e "\n${BLUE}🧪 测试: 用户认证${NC}"

# 尝试登录
LOGIN_RESP=$(curl -s -X POST "$API_URL/api/auth/login" \
    -H "Content-Type: application/json" \
    -d '{"email":"codelab_test@example.com","password":"test123456"}')

if echo "$LOGIN_RESP" | grep -q "access_token"; then
    TOKEN=$(echo "$LOGIN_RESP" | grep -o '"access_token":"[^"]*' | cut -d'"' -f4)
    log_success "登录成功"
else
    # 尝试注册
    REGISTER_RESP=$(curl -s -X POST "$API_URL/api/auth/register" \
        -H "Content-Type: application/json" \
        -d '{"email":"codelab_test@example.com","username":"codelab_tester","password":"test123456"}')
    
    if echo "$REGISTER_RESP" | grep -q "access_token"; then
        TOKEN=$(echo "$REGISTER_RESP" | grep -o '"access_token":"[^"]*' | cut -d'"' -f4)
        log_success "注册成功"
    else
        log_error "认证失败: $REGISTER_RESP"
        exit 1
    fi
fi

# 3. 获取 Notebook 列表
echo -e "\n${BLUE}🧪 测试: 获取 Notebook 列表${NC}"
LIST_RESP=$(curl -s -X GET "$API_URL/api/codelab/notebooks" \
    -H "Authorization: Bearer $TOKEN")

if echo "$LIST_RESP" | grep -qE '^\['; then
    COUNT=$(echo "$LIST_RESP" | grep -o '"id"' | wc -l)
    log_success "获取列表成功 (共 $COUNT 个)"
else
    log_error "获取列表失败: $LIST_RESP"
fi

# 4. 创建 Notebook
echo -e "\n${BLUE}🧪 测试: 创建 Notebook${NC}"
CREATE_RESP=$(curl -s -X POST "$API_URL/api/codelab/notebooks" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"title":"测试 Notebook","description":"自动化测试"}')

if echo "$CREATE_RESP" | grep -q '"id"'; then
    NOTEBOOK_ID=$(echo "$CREATE_RESP" | grep -o '"id":"[^"]*' | head -1 | cut -d'"' -f4)
    log_success "创建成功 (ID: ${NOTEBOOK_ID:0:8}...)"
else
    log_error "创建失败: $CREATE_RESP"
    exit 1
fi

# 5. 执行简单代码
echo -e "\n${BLUE}🧪 测试: 执行简单代码${NC}"
EXEC_RESP=$(curl -s -X POST "$API_URL/api/codelab/notebooks/$NOTEBOOK_ID/execute" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"code":"print(\"Hello, CodeLab!\")\n2 + 2","timeout":10}')

if echo "$EXEC_RESP" | grep -q '"success":true'; then
    TIME=$(echo "$EXEC_RESP" | grep -o '"execution_time_ms":[0-9]*' | cut -d':' -f2)
    log_success "执行成功 (${TIME}ms)"
else
    log_warning "执行失败或返回错误"
    echo "$EXEC_RESP" | head -c 200
fi

# 6. 执行 NumPy 代码
echo -e "\n${BLUE}🧪 测试: 执行 NumPy 代码${NC}"
NUMPY_CODE='import numpy as np\narr = np.array([1,2,3,4,5])\nprint(f\"Mean: {np.mean(arr)}\")\narr.sum()'
NUMPY_RESP=$(curl -s -X POST "$API_URL/api/codelab/notebooks/$NOTEBOOK_ID/execute" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"code\":\"$NUMPY_CODE\",\"timeout\":15}")

if echo "$NUMPY_RESP" | grep -q '"success":true'; then
    log_success "NumPy 代码执行成功"
else
    log_warning "NumPy 可能未安装或执行失败"
fi

# 7. 执行 Pandas 代码
echo -e "\n${BLUE}🧪 测试: 执行 Pandas 代码${NC}"
PANDAS_CODE='import pandas as pd\ndf = pd.DataFrame({\"A\":[1,2,3],\"B\":[4,5,6]})\nprint(df.to_string())'
PANDAS_RESP=$(curl -s -X POST "$API_URL/api/codelab/notebooks/$NOTEBOOK_ID/execute" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"code\":\"$PANDAS_CODE\",\"timeout\":15}")

if echo "$PANDAS_RESP" | grep -q '"success":true'; then
    log_success "Pandas 代码执行成功"
else
    log_warning "Pandas 可能未安装或执行失败"
fi

# 8. 测试超时处理
echo -e "\n${BLUE}🧪 测试: 超时处理${NC}"
TIMEOUT_RESP=$(curl -s -X POST "$API_URL/api/codelab/notebooks/$NOTEBOOK_ID/execute" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"code":"import time\ntime.sleep(10)","timeout":3}')

if echo "$TIMEOUT_RESP" | grep -q '"success":false'; then
    log_success "超时处理正确"
else
    log_warning "超时处理可能有问题"
fi

# 9. 删除 Notebook
echo -e "\n${BLUE}🧪 测试: 删除 Notebook${NC}"
DELETE_RESP=$(curl -s -o /dev/null -w "%{http_code}" -X DELETE \
    "$API_URL/api/codelab/notebooks/$NOTEBOOK_ID" \
    -H "Authorization: Bearer $TOKEN")

if [ "$DELETE_RESP" = "200" ]; then
    log_success "删除成功"
else
    log_error "删除失败 (HTTP $DELETE_RESP)"
fi

echo -e "\n=========================================="
echo "测试完成!"
echo "=========================================="
