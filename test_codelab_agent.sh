#!/bin/bash
# CodeLab Agent 快速测试脚本
# 用法: ./test_codelab_agent.sh [token]

set -e

BASE_URL="${BASE_URL:-http://localhost:8000}"
TOKEN="${1:-}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo_success() { echo -e "${GREEN}✓ $1${NC}"; }
echo_fail() { echo -e "${RED}✗ $1${NC}"; }
echo_info() { echo -e "${YELLOW}→ $1${NC}"; }

# 检查 jq 是否安装
if ! command -v jq &> /dev/null; then
    echo_fail "jq 未安装，请先安装: apt install jq"
    exit 1
fi

echo "============================================"
echo "  CodeLab Agent 快速测试"
echo "  Base URL: $BASE_URL"
echo "============================================"

# 1. 健康检查
echo ""
echo_info "1. 健康检查"
HEALTH=$(curl -s $BASE_URL/api/health)
if echo "$HEALTH" | jq -e '.status == "healthy"' > /dev/null 2>&1; then
    echo_success "服务健康"
else
    echo_fail "服务不健康: $HEALTH"
    exit 1
fi

# 如果没有 token，尝试登录
if [ -z "$TOKEN" ]; then
    echo ""
    echo_info "2. 登录获取 Token"
    
    # 尝试注册（可能已存在）
    curl -s -X POST $BASE_URL/api/auth/register \
        -H "Content-Type: application/json" \
        -d '{"email":"agent_test@test.com","username":"agent_test","password":"test123456"}' > /dev/null 2>&1 || true
    
    # 登录
    LOGIN_RESP=$(curl -s -X POST $BASE_URL/api/auth/login \
        -H "Content-Type: application/json" \
        -d '{"username":"agent_test","password":"test123456"}')
    
    TOKEN=$(echo "$LOGIN_RESP" | jq -r '.access_token // empty')
    
    if [ -z "$TOKEN" ]; then
        echo_fail "登录失败: $LOGIN_RESP"
        exit 1
    fi
    echo_success "登录成功"
fi

AUTH_HEADER="Authorization: Bearer $TOKEN"

# 3. 创建 Notebook
echo ""
echo_info "3. 创建 Notebook"
NB_RESP=$(curl -s -X POST $BASE_URL/api/codelab/notebooks \
    -H "$AUTH_HEADER" \
    -H "Content-Type: application/json" \
    -d '{"title":"Agent 测试 Notebook"}')

NOTEBOOK_ID=$(echo "$NB_RESP" | jq -r '.id // empty')
if [ -z "$NOTEBOOK_ID" ]; then
    echo_fail "创建 Notebook 失败: $NB_RESP"
    exit 1
fi
echo_success "Notebook ID: $NOTEBOOK_ID"

# 4. 执行代码创建变量
echo ""
echo_info "4. 执行代码创建变量"
EXEC_RESP=$(curl -s -X POST "$BASE_URL/api/codelab/notebooks/$NOTEBOOK_ID/execute" \
    -H "$AUTH_HEADER" \
    -H "Content-Type: application/json" \
    -d '{"code":"x = 42\ny = [1, 2, 3]\nprint(f\"x = {x}, y = {y}\")"}')

if echo "$EXEC_RESP" | jq -e '.success == true' > /dev/null 2>&1; then
    echo_success "代码执行成功"
    echo "   输出: $(echo "$EXEC_RESP" | jq -r '.outputs[0].content // "无"')"
else
    echo_fail "代码执行失败: $EXEC_RESP"
fi

# 5. Agent 对话 (未授权 - 查看变量)
echo ""
echo_info "5. Agent 对话 (未授权 - 查看变量)"
AGENT_RESP=$(curl -s -X POST "$BASE_URL/api/codelab/notebooks/$NOTEBOOK_ID/agent/chat" \
    -H "$AUTH_HEADER" \
    -H "Content-Type: application/json" \
    -d '{"message":"当前有哪些变量？","user_authorized":false,"stream":false}')

if [ -n "$AGENT_RESP" ]; then
    echo_success "Agent 响应成功"
    # 尝试解析流式响应
    echo "   响应长度: $(echo "$AGENT_RESP" | wc -c) 字节"
else
    echo_fail "Agent 无响应"
fi

# 6. Agent 对话 (已授权 - 执行代码)
echo ""
echo_info "6. Agent 对话 (已授权 - 执行代码)"
AGENT_RESP2=$(curl -s -X POST "$BASE_URL/api/codelab/notebooks/$NOTEBOOK_ID/agent/chat" \
    -H "$AUTH_HEADER" \
    -H "Content-Type: application/json" \
    -d '{"message":"计算 x * 2 并打印结果","user_authorized":true,"stream":false}')

if [ -n "$AGENT_RESP2" ]; then
    echo_success "Agent 响应成功"
    echo "   响应长度: $(echo "$AGENT_RESP2" | wc -c) 字节"
else
    echo_fail "Agent 无响应"
fi

# 7. 验证变量状态
echo ""
echo_info "7. 验证变量状态"
VARS_RESP=$(curl -s "$BASE_URL/api/codelab/notebooks/$NOTEBOOK_ID/variables" \
    -H "$AUTH_HEADER")

if echo "$VARS_RESP" | jq -e '.variables' > /dev/null 2>&1; then
    echo_success "变量获取成功"
    echo "   变量: $(echo "$VARS_RESP" | jq -r '.variables | keys | join(", ")')"
else
    echo_fail "变量获取失败: $VARS_RESP"
fi

# 8. 清理 - 删除 Notebook
echo ""
echo_info "8. 清理测试 Notebook"
DEL_RESP=$(curl -s -X DELETE "$BASE_URL/api/codelab/notebooks/$NOTEBOOK_ID" \
    -H "$AUTH_HEADER")
echo_success "已删除"

echo ""
echo "============================================"
echo_success "所有测试完成!"
echo "============================================"
