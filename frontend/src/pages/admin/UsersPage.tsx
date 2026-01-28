/**
 * 管理员 - 用户管理页面
 * 设计风格: 学术深空 - 深色背景配合优雅的金色与蓝色点缀
 */
import React, { useEffect, useState } from 'react';
import { 
  Table, Tag, Space, Button, Input, Select, Modal, message, 
  Card, Row, Col, Avatar, Tooltip, Dropdown, Badge, Form,
  Typography, Spin, Empty
} from 'antd';
import {
  UserOutlined, SearchOutlined, TeamOutlined,
  EditOutlined, DeleteOutlined,
  StopOutlined, CheckCircleOutlined, CrownOutlined, ReadOutlined,
  MoreOutlined, ReloadOutlined, UserAddOutlined, MailOutlined,
  LockOutlined, BankOutlined, AimOutlined
} from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import { useRoleStore, UserRole, UserInfo } from '../../stores/roleStore';
import api from '../../services/api';

const { Title, Text } = Typography;
const { Option } = Select;

// 角色颜色映射
const roleColors: Record<UserRole, string> = {
  [UserRole.ADMIN]: '#D4AF37',
  [UserRole.MENTOR]: '#4A90D9',
  [UserRole.STUDENT]: '#6B8E9F',
};

const roleLabels: Record<UserRole, string> = {
  [UserRole.ADMIN]: '管理员',
  [UserRole.MENTOR]: '导师',
  [UserRole.STUDENT]: '学生',
};

const roleIcons: Record<UserRole, React.ReactNode> = {
  [UserRole.ADMIN]: <CrownOutlined />,
  [UserRole.MENTOR]: <ReadOutlined />,
  [UserRole.STUDENT]: <UserOutlined />,
};

const UsersPage: React.FC = () => {
  const { 
    users, usersLoading, statistics, statisticsLoading,
    fetchUsers, fetchStatistics, updateUserRole, toggleUserActive, deleteUser
  } = useRoleStore();

  const [searchText, setSearchText] = useState('');
  const [roleFilter, setRoleFilter] = useState<UserRole | undefined>();
  const [activeFilter, setActiveFilter] = useState<boolean | undefined>();
  const [roleModalVisible, setRoleModalVisible] = useState(false);
  const [createModalVisible, setCreateModalVisible] = useState(false);
  const [selectedUser, setSelectedUser] = useState<UserInfo | null>(null);
  const [newRole, setNewRole] = useState<UserRole>(UserRole.STUDENT);
  const [pagination, setPagination] = useState({ current: 1, pageSize: 20 });
  const [createLoading, setCreateLoading] = useState(false);
  const [editModalVisible, setEditModalVisible] = useState(false);
  const [editLoading, setEditLoading] = useState(false);
  const [createForm] = Form.useForm();
  const [editForm] = Form.useForm();

  useEffect(() => {
    fetchStatistics();
    loadUsers();
  }, []);

  // 当筛选条件改变时重新加载
  useEffect(() => {
    loadUsers({ current: 1 });
    setPagination(p => ({ ...p, current: 1 }));
  }, [roleFilter, activeFilter]);

  const loadUsers = (params?: any) => {
    fetchUsers({
      skip: ((params?.current || pagination.current) - 1) * (params?.pageSize || pagination.pageSize),
      limit: params?.pageSize || pagination.pageSize,
      role: roleFilter,
      search: searchText || undefined,
      is_active: activeFilter,
    });
  };

  const handleSearch = () => {
    setPagination({ ...pagination, current: 1 });
    loadUsers({ current: 1 });
  };

  const handleRoleChange = async () => {
    if (!selectedUser) return;
    try {
      await updateUserRole(selectedUser.id, newRole);
      message.success(`用户角色已更新为 ${roleLabels[newRole]}`);
      setRoleModalVisible(false);
      loadUsers();
      fetchStatistics();
    } catch (error: any) {
      message.error(error.response?.data?.detail || '更新失败');
    }
  };

  const handleToggleActive = async (user: UserInfo) => {
    try {
      await toggleUserActive(user.id);
      message.success(user.is_active ? '用户已禁用' : '用户已启用');
      fetchStatistics();
    } catch (error: any) {
      message.error(error.response?.data?.detail || '操作失败');
    }
  };

  const handleDelete = async (user: UserInfo) => {
    try {
      await deleteUser(user.id);
      message.success('用户已删除');
      loadUsers();
      fetchStatistics();
    } catch (error: any) {
      message.error(error.response?.data?.detail || '删除失败');
    }
  };

  const handleCreateUser = async (values: any) => {
    setCreateLoading(true);
    try {
      await api.post('/api/admin/users', values);
      message.success('用户创建成功');
      setCreateModalVisible(false);
      createForm.resetFields();
      loadUsers();
      fetchStatistics();
    } catch (error: any) {
      message.error(error.response?.data?.detail || '创建失败');
    } finally {
      setCreateLoading(false);
    }
  };

  const handleEditUser = async (values: any) => {
    if (!selectedUser) return;
    setEditLoading(true);
    try {
      await api.put(`/api/admin/users/${selectedUser.id}`, values);
      message.success('用户信息已更新');
      setEditModalVisible(false);
      editForm.resetFields();
      loadUsers();
      fetchStatistics();
    } catch (error: any) {
      message.error(error.response?.data?.detail || '更新失败');
    } finally {
      setEditLoading(false);
    }
  };

  // 表格列定义
  const columns: ColumnsType<UserInfo> = [
    {
      title: '用户',
      key: 'user',
      width: 240,
      render: (_, record) => (
        <Space>
          <Avatar 
            src={record.avatar} 
            icon={<UserOutlined />}
            style={{ backgroundColor: roleColors[record.role] }}
          />
          <div>
            <div style={{ fontWeight: 500, color: '#E8E8E8' }}>
              {record.full_name || record.username}
            </div>
            <Text style={{ fontSize: 12, color: '#8899A6' }}>
              @{record.username}
            </Text>
          </div>
        </Space>
      ),
    },
    {
      title: '邮箱',
      dataIndex: 'email',
      key: 'email',
      width: 180,
      render: (email) => <Text style={{ color: '#8899A6', fontSize: 13 }}>{email}</Text>,
    },
    {
      title: '角色',
      dataIndex: 'role',
      key: 'role',
      width: 100,
      render: (role: UserRole) => (
        <Tag 
          icon={roleIcons[role]}
          color={roleColors[role]}
          style={{ borderRadius: 12, padding: '2px 10px' }}
        >
          {roleLabels[role]}
        </Tag>
      ),
    },
    {
      title: '部门/方向',
      key: 'department',
      width: 160,
      render: (_, record) => (
        <div>
          {record.department && (
            <div style={{ color: '#8899A6', fontSize: 12 }}>{record.department}</div>
          )}
          {record.research_direction && (
            <div style={{ color: '#6B8E9F', fontSize: 11 }}>{record.research_direction}</div>
          )}
          {!record.department && !record.research_direction && (
            <Text style={{ color: '#4A5568' }}>-</Text>
          )}
        </div>
      ),
    },
    {
      title: '状态',
      dataIndex: 'is_active',
      key: 'is_active',
      width: 80,
      render: (isActive) => (
        <Badge 
          status={isActive ? 'success' : 'error'} 
          text={<span style={{ color: isActive ? '#52c41a' : '#ff4d4f', fontSize: 12 }}>
            {isActive ? '正常' : '禁用'}
          </span>}
        />
      ),
    },
    {
      title: '注册时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 100,
      render: (date) => (
        <Text style={{ color: '#8899A6', fontSize: 12 }}>
          {new Date(date).toLocaleDateString('zh-CN')}
        </Text>
      ),
    },
    {
      title: '操作',
      key: 'action',
      width: 60,
      fixed: 'right',
      render: (_, record) => (
        <Dropdown
          menu={{
            items: [
              {
                key: 'edit',
                icon: <EditOutlined />,
                label: '编辑信息',
                onClick: () => {
                  setSelectedUser(record);
                  editForm.setFieldsValue({
                    full_name: record.full_name,
                    department: record.department,
                    research_direction: record.research_direction,
                    role: record.role,
                  });
                  setEditModalVisible(true);
                },
              },
              {
                key: 'role',
                icon: <CrownOutlined />,
                label: '修改角色',
                onClick: () => {
                  setSelectedUser(record);
                  setNewRole(record.role);
                  setRoleModalVisible(true);
                },
              },
              {
                key: 'toggle',
                icon: record.is_active ? <StopOutlined /> : <CheckCircleOutlined />,
                label: record.is_active ? '禁用账户' : '启用账户',
                onClick: () => handleToggleActive(record),
              },
              { type: 'divider' },
              {
                key: 'delete',
                icon: <DeleteOutlined />,
                label: '删除用户',
                danger: true,
                onClick: () => {
                  Modal.confirm({
                    title: '确认删除',
                    content: `确定要删除用户 "${record.username}" 吗？`,
                    okText: '删除',
                    okType: 'danger',
                    cancelText: '取消',
                    onOk: () => handleDelete(record),
                  });
                },
              },
            ],
          }}
        >
          <Button type="text" icon={<MoreOutlined />} size="small" />
        </Dropdown>
      ),
    },
  ];

  // 紧凑型统计项
  const MiniStat: React.FC<{
    label: string;
    value: number;
    color: string;
    icon: React.ReactNode;
  }> = ({ label, value, color, icon }) => (
    <div style={{ 
      display: 'flex', 
      alignItems: 'center', 
      gap: 6,
      padding: '6px 12px',
      background: `${color}10`,
      borderRadius: 6,
      border: `1px solid ${color}30`,
    }}>
      <span style={{ color, fontSize: 14 }}>{icon}</span>
      <span style={{ fontSize: 15, fontWeight: 600, color }}>{value}</span>
      <span style={{ fontSize: 11, color: '#8899A6' }}>{label}</span>
    </div>
  );

  return (
    <div style={{ 
      padding: '20px 28px',
      minHeight: '100vh',
      background: 'linear-gradient(180deg, #0D1117 0%, #161B22 100%)',
    }}>
      {/* 页面标题和统计 */}
      <div style={{ 
        display: 'flex', 
        justifyContent: 'space-between', 
        alignItems: 'center',
        marginBottom: 20,
        flexWrap: 'wrap',
        gap: 12,
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
          <Title level={4} style={{ 
            margin: 0, 
            color: '#E8E8E8',
            fontWeight: 600,
            display: 'flex',
            alignItems: 'center',
            gap: 8,
          }}>
            <CrownOutlined style={{ color: '#D4AF37' }} />
            用户管理
          </Title>
          
          {/* 紧凑统计 */}
          <Spin spinning={statisticsLoading} size="small">
            <div style={{ display: 'flex', gap: 8 }}>
              <MiniStat label="总计" value={statistics?.total_users || 0} color="#4A90D9" icon={<TeamOutlined />} />
              <MiniStat label="管理员" value={statistics?.admin_count || 0} color="#D4AF37" icon={<CrownOutlined />} />
              <MiniStat label="导师" value={statistics?.mentor_count || 0} color="#52c41a" icon={<ReadOutlined />} />
              <MiniStat label="学生" value={statistics?.student_count || 0} color="#6B8E9F" icon={<UserOutlined />} />
            </div>
          </Spin>
        </div>
        
        <Button 
          type="primary"
          icon={<UserAddOutlined />}
          onClick={() => setCreateModalVisible(true)}
          style={{ backgroundColor: '#D4AF37', borderColor: '#D4AF37' }}
        >
          添加用户
        </Button>
      </div>

      {/* 用户表格 */}
      <Card
        style={{
          backgroundColor: '#161B22',
          borderColor: '#30363D',
          borderRadius: 10,
        }}
        styles={{ body: { padding: 0 } }}
      >
        {/* 工具栏 */}
        <div style={{ 
          padding: '12px 16px', 
          borderBottom: '1px solid #30363D',
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          gap: 12,
        }}>
          <Space size="small">
            <Input
              placeholder="搜索用户..."
              prefix={<SearchOutlined style={{ color: '#8899A6' }} />}
              value={searchText}
              onChange={(e) => setSearchText(e.target.value)}
              onPressEnter={handleSearch}
              style={{ 
                width: 180,
                backgroundColor: '#0D1117',
                borderColor: '#30363D',
              }}
              allowClear
            />
            <Select
              placeholder="角色"
              allowClear
              style={{ width: 90 }}
              value={roleFilter}
              onChange={(v) => { setRoleFilter(v); }}
            >
              <Option value={UserRole.ADMIN}>管理员</Option>
              <Option value={UserRole.MENTOR}>导师</Option>
              <Option value={UserRole.STUDENT}>学生</Option>
            </Select>
            <Select
              placeholder="状态"
              allowClear
              style={{ width: 80 }}
              value={activeFilter}
              onChange={(v) => { setActiveFilter(v); }}
            >
              <Option value={true}>正常</Option>
              <Option value={false}>禁用</Option>
            </Select>
            <Button 
              type="primary" 
              icon={<SearchOutlined />}
              onClick={handleSearch}
              size="small"
              style={{ backgroundColor: '#4A90D9' }}
            >
              搜索
            </Button>
          </Space>
          <Tooltip title="刷新">
            <Button 
              icon={<ReloadOutlined />} 
              onClick={() => { loadUsers(); fetchStatistics(); }}
              size="small"
              style={{ borderColor: '#30363D' }}
            />
          </Tooltip>
        </div>

        {/* 表格 */}
        <Table
          columns={columns}
          dataSource={users}
          rowKey="id"
          loading={usersLoading}
          scroll={{ x: 900 }}
          size="small"
          pagination={{
            ...pagination,
            size: 'small',
            showSizeChanger: true,
            showQuickJumper: true,
            showTotal: (total) => `共 ${total} 条`,
            onChange: (page, pageSize) => {
              setPagination({ current: page, pageSize });
              loadUsers({ current: page, pageSize });
            },
          }}
          locale={{
            emptyText: (
              <Empty 
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={<span style={{ color: '#8899A6' }}>暂无用户</span>}
              />
            ),
          }}
        />
      </Card>

      {/* 修改角色弹窗 */}
      <Modal
        title={<span style={{ color: '#E8E8E8' }}><EditOutlined style={{ marginRight: 8, color: '#D4AF37' }} />修改用户角色</span>}
        open={roleModalVisible}
        onOk={handleRoleChange}
        onCancel={() => setRoleModalVisible(false)}
        okText="确认"
        cancelText="取消"
        width={400}
        styles={{
          content: { backgroundColor: '#161B22', border: '1px solid #30363D' },
          header: { backgroundColor: '#161B22', borderBottom: '1px solid #30363D' },
          body: { backgroundColor: '#161B22' },
          footer: { backgroundColor: '#161B22', borderTop: '1px solid #30363D' },
        }}
      >
        {selectedUser && (
          <div style={{ padding: '16px 0' }}>
            <div style={{ marginBottom: 16, display: 'flex', alignItems: 'center', gap: 10 }}>
              <Avatar src={selectedUser.avatar} icon={<UserOutlined />} style={{ backgroundColor: roleColors[selectedUser.role] }} />
              <div>
                <div style={{ fontWeight: 600, color: '#E8E8E8' }}>{selectedUser.full_name || selectedUser.username}</div>
                <Text style={{ color: '#8899A6', fontSize: 12 }}>@{selectedUser.username}</Text>
              </div>
            </div>
            <div style={{ marginBottom: 8, color: '#8899A6', fontSize: 13 }}>选择新角色：</div>
            <Select value={newRole} onChange={setNewRole} style={{ width: '100%' }}>
              <Option value={UserRole.ADMIN}><Space><CrownOutlined style={{ color: '#D4AF37' }} />管理员</Space></Option>
              <Option value={UserRole.MENTOR}><Space><ReadOutlined style={{ color: '#4A90D9' }} />导师</Space></Option>
              <Option value={UserRole.STUDENT}><Space><UserOutlined style={{ color: '#6B8E9F' }} />学生</Space></Option>
            </Select>
          </div>
        )}
      </Modal>

      {/* 创建用户弹窗 */}
      <Modal
        title={<span style={{ color: '#E8E8E8' }}><UserAddOutlined style={{ marginRight: 8, color: '#D4AF37' }} />添加新用户</span>}
        open={createModalVisible}
        onOk={() => createForm.submit()}
        onCancel={() => { setCreateModalVisible(false); createForm.resetFields(); }}
        okText="创建"
        cancelText="取消"
        confirmLoading={createLoading}
        width={460}
        styles={{
          content: { backgroundColor: '#161B22', border: '1px solid #30363D' },
          header: { backgroundColor: '#161B22', borderBottom: '1px solid #30363D' },
          body: { backgroundColor: '#161B22' },
          footer: { backgroundColor: '#161B22', borderTop: '1px solid #30363D' },
        }}
      >
        <Form form={createForm} layout="vertical" onFinish={handleCreateUser} style={{ marginTop: 16 }} initialValues={{ role: 'student' }}>
          <Row gutter={12}>
            <Col span={12}>
              <Form.Item name="username" label={<span style={{ color: '#8899A6' }}>用户名</span>} rules={[{ required: true, message: '请输入' }, { min: 2, message: '至少2字符' }]}>
                <Input prefix={<UserOutlined style={{ color: '#8899A6' }} />} placeholder="唯一用户名" style={{ backgroundColor: '#0D1117', borderColor: '#30363D' }} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="email" label={<span style={{ color: '#8899A6' }}>邮箱</span>} rules={[{ required: true, message: '请输入' }, { type: 'email', message: '格式错误' }]}>
                <Input prefix={<MailOutlined style={{ color: '#8899A6' }} />} placeholder="email@example.com" style={{ backgroundColor: '#0D1117', borderColor: '#30363D' }} />
              </Form.Item>
            </Col>
          </Row>
          <Row gutter={12}>
            <Col span={12}>
              <Form.Item name="password" label={<span style={{ color: '#8899A6' }}>密码</span>} rules={[{ required: true, message: '请输入' }, { min: 6, message: '至少6位' }]}>
                <Input.Password prefix={<LockOutlined style={{ color: '#8899A6' }} />} placeholder="至少6位" style={{ backgroundColor: '#0D1117', borderColor: '#30363D' }} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="role" label={<span style={{ color: '#8899A6' }}>角色</span>}>
                <Select><Option value="student">学生</Option><Option value="mentor">导师</Option><Option value="admin">管理员</Option></Select>
              </Form.Item>
            </Col>
          </Row>
          <Form.Item name="full_name" label={<span style={{ color: '#8899A6' }}>姓名</span>}>
            <Input placeholder="可选" style={{ backgroundColor: '#0D1117', borderColor: '#30363D' }} />
          </Form.Item>
          <Row gutter={12}>
            <Col span={12}>
              <Form.Item name="department" label={<span style={{ color: '#8899A6' }}>部门</span>}>
                <Input prefix={<BankOutlined style={{ color: '#8899A6' }} />} placeholder="可选" style={{ backgroundColor: '#0D1117', borderColor: '#30363D' }} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="research_direction" label={<span style={{ color: '#8899A6' }}>研究方向</span>}>
                <Input prefix={<AimOutlined style={{ color: '#8899A6' }} />} placeholder="可选" style={{ backgroundColor: '#0D1117', borderColor: '#30363D' }} />
              </Form.Item>
            </Col>
          </Row>
        </Form>
      </Modal>

      {/* 编辑用户弹窗 */}
      <Modal
        title={<span style={{ color: '#E8E8E8' }}><EditOutlined style={{ marginRight: 8, color: '#4A90D9' }} />编辑用户信息</span>}
        open={editModalVisible}
        onOk={() => editForm.submit()}
        onCancel={() => { setEditModalVisible(false); editForm.resetFields(); }}
        okText="保存"
        cancelText="取消"
        confirmLoading={editLoading}
        width={460}
        styles={{
          content: { backgroundColor: '#161B22', border: '1px solid #30363D' },
          header: { backgroundColor: '#161B22', borderBottom: '1px solid #30363D' },
          body: { backgroundColor: '#161B22' },
          footer: { backgroundColor: '#161B22', borderTop: '1px solid #30363D' },
        }}
      >
        {selectedUser && (
          <div style={{ marginBottom: 16, display: 'flex', alignItems: 'center', gap: 10 }}>
            <Avatar src={selectedUser.avatar} icon={<UserOutlined />} style={{ backgroundColor: roleColors[selectedUser.role] }} />
            <div>
              <div style={{ fontWeight: 600, color: '#E8E8E8' }}>@{selectedUser.username}</div>
              <Text style={{ color: '#8899A6', fontSize: 12 }}>{selectedUser.email}</Text>
            </div>
          </div>
        )}
        <Form form={editForm} layout="vertical" onFinish={handleEditUser}>
          <Form.Item name="full_name" label={<span style={{ color: '#8899A6' }}>姓名</span>}>
            <Input placeholder="用户姓名" style={{ backgroundColor: '#0D1117', borderColor: '#30363D' }} />
          </Form.Item>
          <Row gutter={12}>
            <Col span={12}>
              <Form.Item name="department" label={<span style={{ color: '#8899A6' }}>部门/院系</span>}>
                <Input prefix={<BankOutlined style={{ color: '#8899A6' }} />} placeholder="所属部门" style={{ backgroundColor: '#0D1117', borderColor: '#30363D' }} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="research_direction" label={<span style={{ color: '#8899A6' }}>研究方向</span>}>
                <Input prefix={<AimOutlined style={{ color: '#8899A6' }} />} placeholder="研究领域" style={{ backgroundColor: '#0D1117', borderColor: '#30363D' }} />
              </Form.Item>
            </Col>
          </Row>
          <Form.Item name="role" label={<span style={{ color: '#8899A6' }}>角色</span>}>
            <Select>
              <Option value={UserRole.STUDENT}>学生</Option>
              <Option value={UserRole.MENTOR}>导师</Option>
              <Option value={UserRole.ADMIN}>管理员</Option>
            </Select>
          </Form.Item>
        </Form>
      </Modal>

      {/* 样式覆盖 */}
      <style>{`
        .ant-table { background: #161B22 !important; }
        .ant-table-thead > tr > th { background: #0D1117 !important; color: #8899A6 !important; border-bottom: 1px solid #30363D !important; font-weight: 500; padding: 10px 12px !important; }
        .ant-table-tbody > tr > td { background: #161B22 !important; color: #E8E8E8 !important; border-bottom: 1px solid #21262D !important; padding: 8px 12px !important; }
        .ant-table-tbody > tr:hover > td { background: #1C2128 !important; }
        .ant-pagination-item { background: #0D1117 !important; border-color: #30363D !important; }
        .ant-pagination-item a { color: #8899A6 !important; }
        .ant-pagination-item-active { border-color: #4A90D9 !important; }
        .ant-pagination-item-active a { color: #4A90D9 !important; }
        .ant-select-selector { background: #0D1117 !important; border-color: #30363D !important; }
        .ant-select-selection-item, .ant-select-selection-placeholder { color: #E8E8E8 !important; }
        .ant-input { color: #E8E8E8 !important; }
        .ant-input::placeholder { color: #8899A6 !important; }
        .ant-dropdown-menu { background: #161B22 !important; border: 1px solid #30363D !important; }
        .ant-dropdown-menu-item { color: #E8E8E8 !important; }
        .ant-dropdown-menu-item:hover { background: #1C2128 !important; }
      `}</style>
    </div>
  );
};

export default UsersPage;
