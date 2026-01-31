import React, { useState, useEffect } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import {
  Card, Button, Tag, Space, Avatar, Typography, Empty, Spin,
  message, Tooltip, Descriptions, Checkbox, Divider, List,
} from 'antd';
import {
  ArrowLeftOutlined, FileTextOutlined, FolderOutlined, DatabaseOutlined,
  UserOutlined, CalendarOutlined, LinkOutlined, DownloadOutlined,
  CopyOutlined, CheckOutlined, FileOutlined, FilePdfOutlined,
  CodeOutlined, PlayCircleOutlined,
} from '@ant-design/icons';
import { shareApi } from '@/services/api';

const { Title, Text, Paragraph } = Typography;

interface SharedDetail {
  share_id: number;
  resource_type: string;
  resource_id: number;
  owner_id: number;
  owner_name: string;
  owner_avatar?: string;
  shared_at: string;
  permission: string;
  paper?: {
    id: number; title: string; abstract?: string; authors: any[];
    year?: number; venue?: string; journal?: string; url?: string;
    pdf_url?: string; arxiv_url?: string; doi?: string;
    citation_count?: number; reference_count?: number;
    fields_of_study?: string[]; notes?: string; tags?: string[];
  };
  collection?: {
    id: number; name: string; description?: string; color?: string; paper_count: number;
  };
  papers?: {
    id: number; title: string; authors: string[]; year?: number;
    venue?: string; citation_count?: number; url?: string; pdf_url?: string;
  }[];
  knowledge_base?: {
    id: number; name: string; description?: string; document_count: number;
    embedding_model?: string;
  };
  documents?: {
    id: number; filename: string; file_type: string; file_size: number;
    chunk_count: number; status: string; created_at?: string;
  }[];
  notebook?: {
    id: string; title: string; description?: string; execution_count: number;
    created_at?: string; updated_at?: string; cell_count: number;
  };
  cells?: {
    id: string; cell_type: string; source: string; outputs: any[];
    execution_count?: number; position: number;
  }[];
}

const resourceTypeConfig: Record<string, { label: string; icon: React.ReactNode; color: string }> = {
  paper: { label: '论文', icon: <FileTextOutlined />, color: '#4A9EE8' },
  paper_collection: { label: '文献集', icon: <FolderOutlined />, color: '#52c41a' },
  knowledge_base: { label: '知识库', icon: <DatabaseOutlined />, color: '#722ed1' },
  notebook: { label: '笔记本', icon: <CodeOutlined />, color: '#fa8c16' },
};

const permissionConfig: Record<string, { label: string; color: string }> = {
  read: { label: '只读', color: 'blue' },
  write: { label: '可编辑', color: 'green' },
};

const SharedResourceViewPage: React.FC = () => {
  const { shareId } = useParams<{ shareId: string }>();
  const navigate = useNavigate();
  const [loading, setLoading] = useState(true);
  const [detail, setDetail] = useState<SharedDetail | null>(null);
  const [copyLoading, setCopyLoading] = useState(false);
  const [selectedPapers, setSelectedPapers] = useState<number[]>([]);

  useEffect(() => {
    if (shareId) {
      loadDetail();
    }
  }, [shareId]);

  const loadDetail = async () => {
    setLoading(true);
    try {
      const data = await shareApi.getSharedDetail(Number(shareId));
      setDetail(data);
    } catch (error: any) {
      message.error(error.response?.data?.detail || '加载失败');
      navigate('/shared');
    } finally {
      setLoading(false);
    }
  };

  const handleCopyPaperToLibrary = async () => {
    if (!detail) return;
    setCopyLoading(true);
    try {
      await shareApi.copyToLibrary(detail.share_id);
      message.success('已添加到您的文献库');
    } catch (error: any) {
      message.error(error.response?.data?.detail || '添加失败');
    } finally {
      setCopyLoading(false);
    }
  };

  const handleCopySelectedPapers = async () => {
    if (!detail || selectedPapers.length === 0) return;
    setCopyLoading(true);
    try {
      const result = await shareApi.copyCollectionPapers(
        detail.share_id,
        selectedPapers.length === detail.papers?.length ? undefined : selectedPapers
      );
      message.success(result.message);
      setSelectedPapers([]);
    } catch (error: any) {
      message.error(error.response?.data?.detail || '添加失败');
    } finally {
      setCopyLoading(false);
    }
  };

  const handleSelectAll = () => {
    if (!detail?.papers) return;
    if (selectedPapers.length === detail.papers.length) {
      setSelectedPapers([]);
    } else {
      setSelectedPapers(detail.papers.map(p => p.id));
    }
  };

  const formatFileSize = (bytes: number) => {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
  };

  if (loading) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', height: '60vh' }}>
        <Spin size="large" />
      </div>
    );
  }

  if (!detail) {
    return (
      <div style={{ padding: 24 }}>
        <Empty description="资源不存在或无权访问" />
        <div style={{ textAlign: 'center', marginTop: 16 }}>
          <Button onClick={() => navigate('/shared')}>返回列表</Button>
        </div>
      </div>
    );
  }

  const typeConfig = resourceTypeConfig[detail.resource_type] || { label: '未知', icon: null, color: '#999' };
  const permConfig = permissionConfig[detail.permission] || { label: '只读', color: 'default' };

  return (
    <div style={{ 
      padding: 24, 
      maxWidth: 1000, 
      margin: '0 auto', 
      paddingBottom: 80,
    }}>
      {/* 返回按钮 */}
      <Button 
        type="text" 
        icon={<ArrowLeftOutlined />} 
        onClick={() => navigate(-1)}
        style={{ marginBottom: 16, color: 'var(--text-secondary)' }}
      >
        返回
      </Button>

      {/* 头部卡片 */}
      <Card style={{ marginBottom: 24, background: 'var(--bg-elevated)', border: '1px solid var(--border)', borderRadius: 16 }}>
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: 16 }}>
          <div style={{
            width: 64, height: 64, borderRadius: 16,
            background: `${typeConfig.color}15`,
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            fontSize: 28, color: typeConfig.color,
          }}>
            {typeConfig.icon}
          </div>
          <div style={{ flex: 1 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
              <Tag color={typeConfig.color} style={{ borderRadius: 12 }}>{typeConfig.label}</Tag>
              <Tag color={permConfig.color} style={{ borderRadius: 12 }}>{permConfig.label}</Tag>
            </div>
            <Title level={4} style={{ margin: 0, color: 'var(--text-primary)' }}>
              {detail.paper?.title || detail.collection?.name || detail.knowledge_base?.name || detail.notebook?.title}
            </Title>
            <div style={{ marginTop: 12, display: 'flex', alignItems: 'center', gap: 16 }}>
              <Space size={8}>
                <Avatar size={24} src={detail.owner_avatar} icon={<UserOutlined />} />
                <Text style={{ color: 'var(--text-secondary)' }}>{detail.owner_name}</Text>
              </Space>
              <Space size={4}>
                <CalendarOutlined style={{ color: 'var(--text-muted)' }} />
                <Text style={{ color: 'var(--text-muted)', fontSize: 12 }}>
                  {new Date(detail.shared_at).toLocaleDateString()}
                </Text>
              </Space>
            </div>
          </div>
        </div>
      </Card>

      {/* 论文详情 */}
      {detail.resource_type === 'paper' && detail.paper && (
        <Card style={{ background: 'var(--bg-elevated)', border: '1px solid var(--border)', borderRadius: 16 }}>
          <div style={{ marginBottom: 20 }}>
            <Button type="primary" icon={<CopyOutlined />} onClick={handleCopyPaperToLibrary} loading={copyLoading}>
              添加到我的文献库
            </Button>
            {detail.paper.pdf_url && (
              <Button icon={<DownloadOutlined />} style={{ marginLeft: 8 }} onClick={() => window.open(detail.paper!.pdf_url, '_blank')}>
                下载PDF
              </Button>
            )}
            {detail.paper.url && (
              <Button icon={<LinkOutlined />} style={{ marginLeft: 8 }} onClick={() => window.open(detail.paper!.url, '_blank')}>
                查看原文
              </Button>
            )}
          </div>

          <Descriptions column={2} size="small" style={{ marginBottom: 20 }}>
            <Descriptions.Item label="作者">
              {detail.paper.authors?.map((a: any) => typeof a === 'string' ? a : a.name).join(', ') || '未知'}
            </Descriptions.Item>
            <Descriptions.Item label="年份">{detail.paper.year || '未知'}</Descriptions.Item>
            <Descriptions.Item label="期刊/会议">{detail.paper.venue || detail.paper.journal || '未知'}</Descriptions.Item>
            <Descriptions.Item label="被引次数">{detail.paper.citation_count ?? 0}</Descriptions.Item>
            {detail.paper.doi && <Descriptions.Item label="DOI">{detail.paper.doi}</Descriptions.Item>}
          </Descriptions>

          {detail.paper.abstract && (
            <React.Fragment>
              <Divider style={{ margin: '16px 0' }} />
              <div>
                <Text strong style={{ color: 'var(--text-primary)', display: 'block', marginBottom: 8 }}>摘要</Text>
                <Paragraph style={{ color: 'var(--text-secondary)', marginBottom: 0 }} ellipsis={{ rows: 6, expandable: true, symbol: '展开' }}>
                  {detail.paper.abstract}
                </Paragraph>
              </div>
            </React.Fragment>
          )}

          {detail.paper.tags && detail.paper.tags.length > 0 && (
            <div style={{ marginTop: 16 }}>
              <Text strong style={{ color: 'var(--text-primary)', display: 'block', marginBottom: 8 }}>标签</Text>
              <Space wrap>
                {detail.paper.tags.map((tag, idx) => (
                  <Tag key={idx} style={{ borderRadius: 12 }}>{tag}</Tag>
                ))}
              </Space>
            </div>
          )}

          {detail.paper.notes && (
            <div style={{ marginTop: 16 }}>
              <Text strong style={{ color: 'var(--text-primary)', display: 'block', marginBottom: 8 }}>笔记</Text>
              <div style={{ padding: 12, background: 'var(--bg-surface)', borderRadius: 8 }}>
                <Text style={{ color: 'var(--text-secondary)' }}>{detail.paper.notes}</Text>
              </div>
            </div>
          )}
        </Card>
      )}

      {/* 文献集详情 */}
      {detail.resource_type === 'paper_collection' && detail.collection && (
        <Card style={{ background: 'var(--bg-elevated)', border: '1px solid var(--border)', borderRadius: 16 }}>
          {detail.collection.description && (
            <Paragraph style={{ color: 'var(--text-secondary)', marginBottom: 20 }}>
              {detail.collection.description}
            </Paragraph>
          )}

          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
            <div>
              <Text strong style={{ color: 'var(--text-primary)', fontSize: 16 }}>
                论文列表 ({detail.papers?.length || 0} 篇)
              </Text>
              {selectedPapers.length > 0 && (
                <Text style={{ color: 'var(--primary)', marginLeft: 12 }}>
                  已选择 {selectedPapers.length} 篇
                </Text>
              )}
            </div>
            <Space>
              <Button onClick={handleSelectAll}>
                {selectedPapers.length === detail.papers?.length ? '取消全选' : '全选'}
              </Button>
              <Button
                type="primary"
                icon={<CopyOutlined />}
                onClick={handleCopySelectedPapers}
                loading={copyLoading}
                disabled={selectedPapers.length === 0}
              >
                添加到我的库 {selectedPapers.length > 0 && `(${selectedPapers.length})`}
              </Button>
            </Space>
          </div>

          <List
            dataSource={detail.papers || []}
            renderItem={(paper) => (
              <List.Item
                style={{
                  padding: '12px 16px',
                  background: selectedPapers.includes(paper.id) ? 'rgba(74, 158, 232, 0.1)' : 'transparent',
                  borderRadius: 10,
                  marginBottom: 8,
                  border: '1px solid var(--border)',
                  cursor: 'pointer',
                }}
                onClick={() => {
                  setSelectedPapers(prev =>
                    prev.includes(paper.id) ? prev.filter(id => id !== paper.id) : [...prev, paper.id]
                  );
                }}
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: 12, width: '100%' }}>
                  <Checkbox checked={selectedPapers.includes(paper.id)} />
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontWeight: 500, color: 'var(--text-primary)', marginBottom: 4 }}>
                      {paper.title}
                    </div>
                    <Space size={8} wrap>
                      <Text style={{ fontSize: 12, color: 'var(--text-muted)' }}>
                        {paper.authors?.slice(0, 3).join(', ')}{paper.authors?.length > 3 ? ' 等' : ''}
                      </Text>
                      {paper.year && <Tag style={{ fontSize: 10, borderRadius: 10 }}>{paper.year}</Tag>}
                      {paper.venue && <Tag color="blue" style={{ fontSize: 10, borderRadius: 10 }}>{paper.venue}</Tag>}
                      {paper.citation_count !== undefined && (
                        <Tag color="gold" style={{ fontSize: 10, borderRadius: 10 }}>被引 {paper.citation_count}</Tag>
                      )}
                    </Space>
                  </div>
                  <Space>
                    {paper.pdf_url && (
                      <Tooltip title="下载PDF">
                        <Button type="text" size="small" icon={<DownloadOutlined />} onClick={(e) => { e.stopPropagation(); window.open(paper.pdf_url, '_blank'); }} />
                      </Tooltip>
                    )}
                    {paper.url && (
                      <Tooltip title="查看原文">
                        <Button type="text" size="small" icon={<LinkOutlined />} onClick={(e) => { e.stopPropagation(); window.open(paper.url, '_blank'); }} />
                      </Tooltip>
                    )}
                  </Space>
                </div>
              </List.Item>
            )}
            locale={{ emptyText: <Empty description="暂无论文" /> }}
          />
        </Card>
      )}

      {/* 知识库详情 - 引用模式 */}
      {detail.resource_type === 'knowledge_base' && detail.knowledge_base && (
        <Card style={{ background: 'var(--bg-elevated)', border: '1px solid var(--border)', borderRadius: 16 }}>
          {/* 直接引用提示 */}
          <div style={{ 
            marginBottom: 20, 
            padding: 16, 
            background: 'linear-gradient(135deg, rgba(114, 46, 209, 0.1), rgba(74, 158, 232, 0.1))',
            borderRadius: 12,
            border: '1px solid rgba(114, 46, 209, 0.2)',
          }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 8 }}>
              <div style={{
                width: 40, height: 40, borderRadius: 10,
                background: 'rgba(114, 46, 209, 0.2)',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                color: '#722ed1', fontSize: 20,
              }}>
                <CheckOutlined />
              </div>
              <div>
                <Text strong style={{ color: 'var(--text-primary)', fontSize: 15 }}>可直接使用（引用模式）</Text>
                <div style={{ color: 'var(--text-muted)', fontSize: 12 }}>此知识库已共享给您，无需复制数据</div>
              </div>
            </div>
            <Text style={{ color: 'var(--text-secondary)', fontSize: 13 }}>
              前往 <a onClick={() => navigate('/chat')} style={{ color: '#722ed1', cursor: 'pointer' }}>AI 对话</a> 页面，
              在知识库选择中可以看到此共享知识库，直接选择即可使用。数据保持与原库实时同步。
            </Text>
          </div>

          {detail.knowledge_base.description && (
            <Paragraph style={{ color: 'var(--text-secondary)', marginBottom: 20 }}>
              {detail.knowledge_base.description}
            </Paragraph>
          )}

          <Descriptions column={2} size="small" style={{ marginBottom: 20 }}>
            <Descriptions.Item label="文档数量">{detail.knowledge_base.document_count}</Descriptions.Item>
            <Descriptions.Item label="嵌入模型">{detail.knowledge_base.embedding_model || '默认'}</Descriptions.Item>
          </Descriptions>

          <Divider style={{ margin: '16px 0' }} />

          <Text strong style={{ color: 'var(--text-primary)', fontSize: 16, display: 'block', marginBottom: 16 }}>
            文档列表 ({detail.documents?.length || 0} 个)
          </Text>

          <div style={{ background: 'var(--bg-surface)', borderRadius: 12, padding: 4 }}>
            <List
              dataSource={detail.documents || []}
              renderItem={(doc) => (
                <List.Item style={{ padding: '12px 16px', border: 'none' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 12, width: '100%' }}>
                    <div style={{
                      width: 40, height: 40, borderRadius: 10,
                      background: doc.file_type === 'pdf' ? '#ff4d4f15' : '#1890ff15',
                      display: 'flex', alignItems: 'center', justifyContent: 'center',
                      color: doc.file_type === 'pdf' ? '#ff4d4f' : '#1890ff',
                    }}>
                      {doc.file_type === 'pdf' ? <FilePdfOutlined /> : <FileOutlined />}
                    </div>
                    <div style={{ flex: 1 }}>
                      <div style={{ fontWeight: 500, color: 'var(--text-primary)' }}>{doc.filename}</div>
                      <Space size={8}>
                        <Text style={{ fontSize: 12, color: 'var(--text-muted)' }}>{formatFileSize(doc.file_size)}</Text>
                        <Text style={{ fontSize: 12, color: 'var(--text-muted)' }}>{doc.chunk_count} 个分块</Text>
                        <Tag color={doc.status === 'completed' ? 'green' : doc.status === 'processing' ? 'blue' : 'default'} style={{ fontSize: 10, borderRadius: 10 }}>
                          {doc.status === 'completed' ? '已处理' : doc.status === 'processing' ? '处理中' : '待处理'}
                        </Tag>
                      </Space>
                    </div>
                  </div>
                </List.Item>
              )}
              locale={{ emptyText: <Empty description="暂无文档" /> }}
            />
          </div>
        </Card>
      )}

      {/* Notebook 详情 - 只读查看 */}
      {detail.resource_type === 'notebook' && detail.notebook && (
        <Card style={{ background: 'var(--bg-elevated)', border: '1px solid var(--border)', borderRadius: 16, marginBottom: 24 }}>
          {/* 只读提示 */}
          <div style={{ 
            marginBottom: 20, 
            padding: 16, 
            background: 'linear-gradient(135deg, rgba(250, 140, 22, 0.1), rgba(255, 169, 64, 0.1))',
            borderRadius: 12,
            border: '1px solid rgba(250, 140, 22, 0.2)',
          }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
              <div style={{
                width: 40, height: 40, borderRadius: 10,
                background: 'rgba(250, 140, 22, 0.2)',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                color: '#fa8c16', fontSize: 20,
              }}>
                <CodeOutlined />
              </div>
              <div>
                <Text strong style={{ color: 'var(--text-primary)', fontSize: 15 }}>只读模式</Text>
                <div style={{ color: 'var(--text-muted)', fontSize: 12 }}>您可以查看此笔记本的内容，但无法编辑或执行代码</div>
              </div>
            </div>
          </div>

          {detail.notebook.description && (
            <Paragraph style={{ color: 'var(--text-secondary)', marginBottom: 20 }}>
              {detail.notebook.description}
            </Paragraph>
          )}

          <Descriptions column={3} size="small" style={{ marginBottom: 20 }}>
            <Descriptions.Item label="单元格数">{detail.notebook.cell_count}</Descriptions.Item>
            <Descriptions.Item label="执行次数">{detail.notebook.execution_count}</Descriptions.Item>
            <Descriptions.Item label="更新时间">{detail.notebook.updated_at ? new Date(detail.notebook.updated_at).toLocaleString() : '-'}</Descriptions.Item>
          </Descriptions>

          <Divider style={{ margin: '16px 0' }} />

          <Text strong style={{ color: 'var(--text-primary)', fontSize: 16, display: 'block', marginBottom: 16 }}>
            单元格列表 ({detail.cells?.length || 0} 个)
          </Text>

          <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            {(detail.cells || []).map((cell, index) => (
              <div key={cell.id} style={{ 
                background: 'var(--bg-surface)', 
                borderRadius: 12, 
                border: '1px solid var(--border)',
                overflow: 'hidden',
              }}>
                {/* 单元格头部 */}
                <div style={{ 
                  padding: '8px 16px', 
                  background: cell.cell_type === 'code' ? 'rgba(250, 140, 22, 0.05)' : 'rgba(82, 196, 26, 0.05)',
                  borderBottom: '1px solid var(--border)',
                  display: 'flex',
                  alignItems: 'center',
                  gap: 8,
                }}>
                  <Tag color={cell.cell_type === 'code' ? 'orange' : 'green'} style={{ margin: 0, borderRadius: 8 }}>
                    {cell.cell_type === 'code' ? 'Code' : 'Markdown'}
                  </Tag>
                  {cell.cell_type === 'code' && cell.execution_count && (
                    <Text style={{ fontSize: 12, color: 'var(--text-muted)' }}>
                      [执行 #{cell.execution_count}]
                    </Text>
                  )}
                  <Text style={{ fontSize: 12, color: 'var(--text-muted)', marginLeft: 'auto' }}>
                    #{index + 1}
                  </Text>
                </div>
                
                {/* 单元格内容 */}
                <div style={{ padding: 16 }}>
                  {cell.cell_type === 'code' ? (
                    <pre style={{ 
                      margin: 0, 
                      padding: 12, 
                      background: '#1e1e2e', 
                      borderRadius: 8,
                      overflow: 'auto',
                      maxHeight: 500,
                      fontFamily: 'Monaco, Consolas, "Courier New", monospace',
                      fontSize: 13,
                      lineHeight: 1.5,
                      color: '#cdd6f4',
                    }}>
                      <code>{cell.source || '# 空单元格'}</code>
                    </pre>
                  ) : (
                    <div style={{ 
                      color: 'var(--text-secondary)', 
                      fontSize: 14,
                      lineHeight: 1.6,
                      whiteSpace: 'pre-wrap',
                    }}>
                      {cell.source || '*空内容*'}
                    </div>
                  )}
                  
                  {/* 代码输出 - 修复输出显示 */}
                  {cell.cell_type === 'code' && cell.outputs && cell.outputs.length > 0 && (
                    <div style={{ marginTop: 12 }}>
                      <Text style={{ fontSize: 12, color: 'var(--text-muted)', marginBottom: 8, display: 'block' }}>
                        输出 ({cell.outputs.length} 项):
                      </Text>
                      {cell.outputs.map((output: any, outIdx: number) => {
                        // 获取输出内容 - 兼容多种格式
                        const outputContent = output.content || output.text || output.data || '';
                        const outputText = typeof outputContent === 'string' 
                          ? outputContent 
                          : (outputContent?.['text/plain'] || JSON.stringify(outputContent, null, 2));
                        
                        return (
                          <div key={outIdx} style={{ 
                            padding: 12, 
                            background: output.output_type === 'error' ? 'rgba(255, 77, 79, 0.1)' : 'var(--bg-hover)',
                            borderRadius: 8,
                            marginTop: 4,
                            overflow: 'auto',
                            maxHeight: 400,
                          }}>
                            {/* 标准输出流 */}
                            {output.output_type === 'stream' && (
                              <pre style={{ 
                                margin: 0, 
                                fontFamily: 'Monaco, Consolas, monospace', 
                                fontSize: 12,
                                color: 'var(--text-secondary)',
                                whiteSpace: 'pre-wrap',
                                wordBreak: 'break-all',
                              }}>
                                {outputText}
                              </pre>
                            )}
                            {/* 执行结果 */}
                            {output.output_type === 'execute_result' && (
                              <pre style={{ 
                                margin: 0, 
                                fontFamily: 'Monaco, Consolas, monospace', 
                                fontSize: 12,
                                color: '#52c41a',
                                whiteSpace: 'pre-wrap',
                                wordBreak: 'break-all',
                              }}>
                                {outputText}
                              </pre>
                            )}
                            {/* 错误信息 */}
                            {output.output_type === 'error' && (
                              <pre style={{ 
                                margin: 0, 
                                fontFamily: 'Monaco, Consolas, monospace', 
                                fontSize: 12,
                                color: '#ff4d4f',
                                whiteSpace: 'pre-wrap',
                                wordBreak: 'break-all',
                              }}>
                                {output.traceback ? output.traceback.join('\n') : outputText}
                              </pre>
                            )}
                            {/* 图片显示 - content 是完整的 data URI */}
                            {output.output_type === 'display_data' && (
                              <>
                                {/* 检查是否是图片：通过 mime_type 或 content 内容判断 */}
                                {(output.mime_type === 'image/png' || 
                                  (typeof output.content === 'string' && output.content.includes('image/png'))) && output.content && (
                                  <img 
                                    src={typeof output.content === 'string' && output.content.startsWith('data:') 
                                      ? output.content 
                                      : `data:image/png;base64,${output.content}`
                                    } 
                                    alt="output" 
                                    style={{ maxWidth: '100%', borderRadius: 4 }}
                                  />
                                )}
                                {/* 非图片的 display_data */}
                                {!(output.mime_type === 'image/png' || 
                                  (typeof output.content === 'string' && output.content.includes('image/png'))) && outputText && (
                                  <pre style={{ margin: 0, fontFamily: 'Monaco, Consolas, monospace', fontSize: 12, whiteSpace: 'pre-wrap' }}>
                                    {outputText}
                                  </pre>
                                )}
                              </>
                            )}
                            {/* 未知类型 - 通用显示 */}
                            {!['stream', 'execute_result', 'error', 'display_data'].includes(output.output_type) && (
                              <pre style={{ 
                                margin: 0, 
                                fontFamily: 'Monaco, Consolas, monospace', 
                                fontSize: 12,
                                color: 'var(--text-secondary)',
                                whiteSpace: 'pre-wrap',
                              }}>
                                {outputText || JSON.stringify(output, null, 2)}
                              </pre>
                            )}
                          </div>
                        );
                      })}
                    </div>
                  )}
                </div>
              </div>
            ))}
            
            {(!detail.cells || detail.cells.length === 0) && (
              <Empty description="暂无单元格" />
            )}
          </div>
        </Card>
      )}

      <style>{`
        .ant-descriptions-item-label { color: var(--text-muted) !important; }
        .ant-descriptions-item-content { color: var(--text-secondary) !important; }
        .ant-card { background: var(--bg-elevated) !important; }
        .ant-list-item:hover { background: var(--bg-hover) !important; }
      `}</style>
    </div>
  );
};

export default SharedResourceViewPage;
