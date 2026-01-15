import { useState, useMemo } from "react";
import { useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import {
  Card,
  Input,
  Button,
  Space,
  Tag,
  Typography,
  Row,
  Col,
  Dropdown,
  Modal,
  Tooltip,
  Segmented,
  message,
  Spin,
} from "antd";
import type { MenuProps } from "antd";
import {
  PlusOutlined,
  SearchOutlined,
  ReloadOutlined,
  DatabaseOutlined,
  FileTextOutlined,
  TableOutlined,
  PictureOutlined,
  MoreOutlined,
  EditOutlined,
  DeleteOutlined,
  ExperimentOutlined,
  EyeOutlined,
  CopyOutlined,
  CheckOutlined,
  FolderOpenOutlined,
  CloudUploadOutlined,
  NodeIndexOutlined,
} from "@ant-design/icons";

import { useDatasets } from "@/hooks/useKnowledge";
import { deleteDataset } from "@/api/knowledge";
import type { Dataset } from "@/types/knowledge";
import { useAppStore } from "@/store/useAppStore";
import { colors } from "@/theme/themeConfig";

const { Text, Title, Paragraph } = Typography;

// 类型 Tab 配置
const typeOptions = [
  { value: "all", label: "全部", icon: <DatabaseOutlined /> },
  { value: "document", label: "文档", icon: <FileTextOutlined /> },
  { value: "data", label: "数据", icon: <TableOutlined /> },
  { value: "image", label: "图片", icon: <PictureOutlined /> },
];

// 知识库卡片组件 - 扁平设计
function DatasetCard({
  dataset,
  onViewDetail,
  onHitTest,
  onEdit,
  onDelete,
}: {
  dataset: Dataset;
  index: number;
  onViewDetail: () => void;
  onHitTest: () => void;
  onEdit: () => void;
  onDelete: () => void;
}) {
  const [copied, setCopied] = useState(false);
  const { darkMode } = useAppStore();

  const copyId = (e: React.MouseEvent) => {
    e.stopPropagation();
    navigator.clipboard.writeText(dataset.dataset_id);
    setCopied(true);
    message.success("ID 已复制到剪贴板");
    setTimeout(() => setCopied(false), 2000);
  };

  const menuItems: MenuProps["items"] = [
    {
      key: "view",
      label: "查看详情",
      icon: <EyeOutlined />,
      onClick: () => onViewDetail(),
    },
    {
      key: "edit",
      label: "编辑配置",
      icon: <EditOutlined />,
      onClick: () => onEdit(),
    },
    {
      key: "test",
      label: "命中测试",
      icon: <ExperimentOutlined />,
      onClick: () => onHitTest(),
    },
    { type: "divider" },
    {
      key: "delete",
      label: "删除知识库",
      icon: <DeleteOutlined />,
      danger: true,
      onClick: () => onDelete(),
    },
  ];

  return (
    <Card
      hoverable
      onClick={onViewDetail}
      style={{
        borderRadius: 8,
        border: `1px solid ${darkMode ? colors.neutral[700] : colors.neutral[200]}`,
        background: darkMode ? colors.neutral[800] : "#ffffff",
        cursor: "pointer",
      }}
      styles={{
        body: { padding: "20px" },
      }}
    >
      {/* 头部：图标 + 标题 + 更多操作 */}
      <div
        style={{
          display: "flex",
          alignItems: "flex-start",
          justifyContent: "space-between",
          marginBottom: 16,
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          {/* 图标容器 - 扁平背景 */}
          <div
            style={{
              width: 40,
              height: 40,
              borderRadius: 8,
              background: darkMode ? colors.neutral[700] : colors.neutral[100],
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              flexShrink: 0,
            }}
          >
            <DatabaseOutlined
              style={{
                fontSize: 18,
                color: colors.primary[500],
              }}
            />
          </div>

          <div style={{ minWidth: 0 }}>
            <Title
              level={5}
              style={{
                margin: 0,
                fontSize: 15,
                fontWeight: 600,
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
              }}
            >
              {dataset.name}
            </Title>
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: 4,
                marginTop: 4,
              }}
            >
              <Text
                type="secondary"
                style={{
                  fontSize: 12,
                  fontFamily: "monospace",
                }}
              >
                {dataset.dataset_id.slice(0, 8)}...
              </Text>
              <Tooltip title={copied ? "已复制" : "复制 ID"}>
                <span
                  onClick={copyId}
                  style={{ cursor: "pointer", display: "flex" }}
                >
                  {copied ? (
                    <CheckOutlined
                      style={{ fontSize: 12, color: colors.primary[500] }}
                    />
                  ) : (
                    <CopyOutlined
                      style={{
                        fontSize: 12,
                        color: colors.neutral[400],
                      }}
                    />
                  )}
                </span>
              </Tooltip>
            </div>
          </div>
        </div>

        {/* 更多操作 */}
        <Dropdown
          menu={{ items: menuItems }}
          trigger={["click"]}
          placement="bottomRight"
        >
          <div
            onClick={(e) => e.stopPropagation()}
            style={{
              width: 32,
              height: 32,
              borderRadius: 6,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              cursor: "pointer",
              background: darkMode
                ? colors.neutral[700]
                : colors.neutral[100],
            }}
          >
            <MoreOutlined style={{ fontSize: 16 }} />
          </div>
        </Dropdown>
      </div>

      {/* 描述 */}
      <Paragraph
        type="secondary"
        ellipsis={{ rows: 2 }}
        style={{
          fontSize: 13,
          marginBottom: 16,
          minHeight: 40,
        }}
      >
        {dataset.description || "暂无描述"}
      </Paragraph>

      {/* 标签和统计 */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          paddingTop: 12,
          borderTop: `1px solid ${darkMode ? colors.neutral[700] : colors.neutral[100]}`,
        }}
      >
        <Space size={6}>
          <Tag
            color="blue"
            style={{
              borderRadius: 4,
              padding: "2px 8px",
              fontSize: 11,
              border: "none",
            }}
          >
            {dataset.visibility === "private"
              ? "私有"
              : dataset.visibility === "tenant"
                ? "租户"
                : "公开"}
          </Tag>
          <Tag
            style={{
              borderRadius: 4,
              padding: "2px 8px",
              fontSize: 11,
              background: darkMode
                ? colors.neutral[700]
                : colors.neutral[100],
              border: "none",
              color: darkMode ? colors.neutral[300] : colors.neutral[600],
            }}
          >
            {dataset.embedding_model || "默认模型"}
          </Tag>
        </Space>

        <Space size={16}>
          <Tooltip title="文档数量">
            <Space size={4}>
              <FileTextOutlined
                style={{ color: colors.neutral[400], fontSize: 14 }}
              />
              <Text strong style={{ fontSize: 13 }}>
                {dataset.statistics?.document_count ?? 0}
              </Text>
            </Space>
          </Tooltip>
          <Tooltip title="段落数量">
            <Space size={4}>
              <NodeIndexOutlined
                style={{ color: colors.neutral[400], fontSize: 14 }}
              />
              <Text strong style={{ fontSize: 13 }}>
                {dataset.statistics?.segment_count ?? 0}
              </Text>
            </Space>
          </Tooltip>
        </Space>
      </div>

      {/* 底部快捷操作 */}
      <div
        style={{
          display: "flex",
          marginTop: 16,
          paddingTop: 12,
          borderTop: `1px solid ${darkMode ? colors.neutral[700] : colors.neutral[100]}`,
        }}
      >
        <div
          onClick={(e) => {
            e.stopPropagation();
            onViewDetail();
          }}
          style={{
            flex: 1,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            gap: 6,
            cursor: "pointer",
            padding: "4px 0",
          }}
        >
          <EyeOutlined style={{ fontSize: 14, color: colors.primary[500] }} />
          <Text style={{ fontSize: 13, color: colors.primary[500] }}>
            详情
          </Text>
        </div>

        <div
          style={{
            width: 1,
            background: darkMode ? colors.neutral[700] : colors.neutral[200],
          }}
        />

        <div
          onClick={(e) => {
            e.stopPropagation();
            onHitTest();
          }}
          style={{
            flex: 1,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            gap: 6,
            cursor: "pointer",
            padding: "4px 0",
          }}
        >
          <ExperimentOutlined
            style={{ fontSize: 14, color: colors.cyan[500] }}
          />
          <Text style={{ fontSize: 13, color: colors.cyan[500] }}>测试</Text>
        </div>
      </div>
    </Card>
  );
}

// 统计卡片组件 - 扁平设计
function StatCard({
  title,
  value,
  icon,
  color,
}: {
  title: string;
  value: number;
  icon: React.ReactNode;
  color: string;
  gradient?: string;
  index: number;
}) {
  const { darkMode } = useAppStore();

  return (
    <Card
      style={{
        borderRadius: 8,
        border: `1px solid ${darkMode ? colors.neutral[700] : colors.neutral[200]}`,
        background: darkMode ? colors.neutral[800] : "#ffffff",
      }}
      styles={{ body: { padding: "16px 20px" } }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
        <div
          style={{
            width: 40,
            height: 40,
            borderRadius: 8,
            background: darkMode ? colors.neutral[700] : colors.neutral[100],
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
          }}
        >
          <span style={{ fontSize: 18, color }}>{icon}</span>
        </div>
        <div>
          <Text type="secondary" style={{ fontSize: 13 }}>
            {title}
          </Text>
          <div
            style={{ fontSize: 24, fontWeight: 600, lineHeight: 1.2 }}
          >
            {value.toLocaleString()}
          </div>
        </div>
      </div>
    </Card>
  );
}

// 空状态组件
function EmptyState({ onCreateClick }: { onCreateClick: () => void }) {
  const { darkMode } = useAppStore();

  return (
    <div
      style={{
        textAlign: "center",
        padding: "80px 24px",
      }}
    >
      <div
        style={{
          width: 80,
          height: 80,
          margin: "0 auto 24px",
          borderRadius: 12,
          background: darkMode ? colors.neutral[700] : colors.neutral[100],
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
        }}
      >
        <FolderOpenOutlined
          style={{
            fontSize: 32,
            color: colors.neutral[400],
          }}
        />
      </div>

      <Title level={4} style={{ marginBottom: 8 }}>
        还没有知识库
      </Title>
      <Paragraph
        type="secondary"
        style={{
          maxWidth: 400,
          margin: "0 auto 24px",
        }}
      >
        创建您的第一个知识库，上传文档或添加 URL 构建 AI 知识体系，
        让 AI 更智能地理解您的业务
      </Paragraph>

      <Space size={12}>
        <Button
          type="primary"
          size="large"
          icon={<PlusOutlined />}
          onClick={onCreateClick}
          style={{
            borderRadius: 6,
            height: 40,
            paddingInline: 20,
          }}
        >
          创建知识库
        </Button>
        <Button
          size="large"
          icon={<CloudUploadOutlined />}
          style={{
            borderRadius: 6,
            height: 40,
            paddingInline: 20,
          }}
        >
          导入知识库
        </Button>
      </Space>
    </div>
  );
}

// 主页面组件
export function KnowledgeDatasetsPage() {
  const nav = useNavigate();
  const qc = useQueryClient();
  const datasetsQuery = useDatasets();
  const datasets = datasetsQuery.data || [];
  const { darkMode } = useAppStore();

  const [searchQuery, setSearchQuery] = useState("");
  const [typeFilter, setTypeFilter] = useState<string>("all");
  const [deleteModalOpen, setDeleteModalOpen] = useState(false);
  const [deletingDataset, setDeletingDataset] = useState<Dataset | null>(null);

  // 过滤后的数据集
  const filteredDatasets = useMemo(() => {
    return datasets.filter((d) => {
      const matchesSearch =
        d.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
        d.dataset_id.toLowerCase().includes(searchQuery.toLowerCase()) ||
        (d.description || "").toLowerCase().includes(searchQuery.toLowerCase());
      return matchesSearch;
    });
  }, [datasets, searchQuery]);

  // 统计数据
  const stats = useMemo(() => {
    const totalDocs = datasets.reduce(
      (sum, d) => sum + (d.statistics?.document_count ?? 0),
      0
    );
    const totalSegments = datasets.reduce(
      (sum, d) => sum + (d.statistics?.segment_count ?? 0),
      0
    );
    return {
      total: datasets.length,
      documents: totalDocs,
      segments: totalSegments,
    };
  }, [datasets]);

  // 删除操作
  const handleDelete = async () => {
    if (!deletingDataset) return;
    try {
      await deleteDataset(deletingDataset.dataset_id);
      await qc.invalidateQueries({ queryKey: ["kb-datasets"] });
      message.success("知识库已删除");
      setDeleteModalOpen(false);
      setDeletingDataset(null);
    } catch (e) {
      message.error("删除失败: " + (e instanceof Error ? e.message : String(e)));
    }
  };

  return (
    <div style={{ maxWidth: 1400, margin: "0 auto" }}>
      {/* 页面标题区 */}
      <div style={{ marginBottom: 24 }}>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            marginBottom: 8,
          }}
        >
          <div>
            <Title level={3} style={{ margin: 0, fontWeight: 600 }}>
              知识库管理
            </Title>
            <Text type="secondary" style={{ fontSize: 14 }}>
              管理和检索您的 AI 知识资产
            </Text>
          </div>

          <Space size={12}>
            <Button
              icon={<ReloadOutlined spin={datasetsQuery.isFetching} />}
              onClick={() => qc.invalidateQueries({ queryKey: ["kb-datasets"] })}
              style={{ borderRadius: 6 }}
            >
              刷新
            </Button>
            <Button
              type="primary"
              icon={<PlusOutlined />}
              onClick={() => nav("/knowledge/create")}
              style={{ borderRadius: 6 }}
            >
              创建知识库
            </Button>
          </Space>
        </div>
      </div>

      {/* 统计卡片 */}
      <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
        <Col xs={24} sm={8}>
          <StatCard
            title="知识库总数"
            value={stats.total}
            icon={<DatabaseOutlined />}
            color={colors.primary[500]}
            index={0}
          />
        </Col>
        <Col xs={24} sm={8}>
          <StatCard
            title="文档总数"
            value={stats.documents}
            icon={<FileTextOutlined />}
            color={colors.cyan[500]}
            index={1}
          />
        </Col>
        <Col xs={24} sm={8}>
          <StatCard
            title="段落总数"
            value={stats.segments}
            icon={<NodeIndexOutlined />}
            color={colors.purple[500]}
            index={2}
          />
        </Col>
      </Row>

      {/* 搜索和筛选栏 */}
      <Card
        style={{
          marginBottom: 24,
          borderRadius: 8,
          border: `1px solid ${darkMode ? colors.neutral[700] : colors.neutral[200]}`,
          background: darkMode ? colors.neutral[800] : "#ffffff",
        }}
        styles={{ body: { padding: "12px 16px" } }}
      >
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            flexWrap: "wrap",
            gap: 16,
          }}
        >
          {/* 左侧：搜索框 */}
          <Input
            placeholder="搜索知识库名称、ID 或描述..."
            prefix={<SearchOutlined style={{ color: colors.neutral[400] }} />}
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            allowClear
            style={{
              width: 320,
              borderRadius: 6,
            }}
          />

          {/* 右侧：类型筛选 */}
          <Segmented
            value={typeFilter}
            onChange={(value) => setTypeFilter(value as string)}
            options={typeOptions.map((opt) => ({
              value: opt.value,
              label: (
                <Space size={6}>
                  {opt.icon}
                  <span>{opt.label}</span>
                </Space>
              ),
            }))}
            style={{
              background: darkMode
                ? colors.neutral[700]
                : colors.neutral[100],
              borderRadius: 6,
              padding: 2,
            }}
          />
        </div>
      </Card>

      {/* 知识库列表 */}
      {datasetsQuery.isLoading ? (
        <div style={{ textAlign: "center", padding: "80px 0" }}>
          <Spin size="large" />
          <div style={{ marginTop: 16 }}>
            <Text type="secondary">加载中...</Text>
          </div>
        </div>
      ) : filteredDatasets.length === 0 ? (
        <EmptyState onCreateClick={() => nav("/knowledge/create")} />
      ) : (
        <Row gutter={[16, 16]}>
          {filteredDatasets.map((dataset, index) => (
            <Col key={dataset.dataset_id} xs={24} sm={12} lg={8} xl={8}>
              <DatasetCard
                dataset={dataset}
                index={index}
                onViewDetail={() => nav(`/knowledge/${dataset.dataset_id}`)}
                onHitTest={() =>
                  nav(`/knowledge/${dataset.dataset_id}?tab=retrieval`)
                }
                onEdit={() =>
                  nav(`/knowledge/${dataset.dataset_id}?tab=settings`)
                }
                onDelete={() => {
                  setDeletingDataset(dataset);
                  setDeleteModalOpen(true);
                }}
              />
            </Col>
          ))}
        </Row>
      )}

      {/* 删除确认弹窗 */}
      <Modal
        title={
          <Space>
            <DeleteOutlined style={{ color: colors.orange[500] }} />
            <span>确认删除</span>
          </Space>
        }
        open={deleteModalOpen}
        onCancel={() => {
          setDeleteModalOpen(false);
          setDeletingDataset(null);
        }}
        onOk={handleDelete}
        okText="确认删除"
        cancelText="取消"
        okButtonProps={{
          danger: true,
        }}
      >
        <div style={{ padding: "12px 0" }}>
          <Text>
            确定要删除知识库{" "}
            <Text strong>"{deletingDataset?.name}"</Text> 吗？
          </Text>
          <br />
          <Text type="secondary" style={{ fontSize: 13 }}>
            此操作不可恢复，知识库中的所有文档和段落都将被删除。
          </Text>
        </div>
      </Modal>
    </div>
  );
}
