import { useState, useMemo } from "react";
import { useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
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
  PlaySquareOutlined,
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
import { copyToClipboard } from "@/lib/clipboard";

const { Text, Title, Paragraph } = Typography;

// 类型 Tab 配置
const typeOptions = [
  { value: "all", labelKey: "knowledge.datasets.typeAll", icon: <DatabaseOutlined /> },
  { value: "document", labelKey: "knowledge.datasets.typeDocument", icon: <FileTextOutlined /> },
  { value: "data", labelKey: "knowledge.datasets.typeData", icon: <TableOutlined /> },
  { value: "image", labelKey: "knowledge.datasets.typeImage", icon: <PictureOutlined /> },
  { value: "audio_video", labelKey: "knowledge.datasets.typeAudioVideo", icon: <PlaySquareOutlined /> },
];

// 根据知识库类型获取图标
function getKBTypeIcon(kbType?: string) {
  switch (kbType) {
    case "data":
      return <TableOutlined />;
    case "image":
      return <PictureOutlined />;
    case "audio_video":
      return <PlaySquareOutlined />;
    case "document":
    default:
      return <FileTextOutlined />;
  }
}

// 根据知识库类型获取标签翻译 key
function getKBTypeLabelKey(kbType?: string) {
  switch (kbType) {
    case "data":
      return "knowledge.datasets.typeDataQuery";
    case "image":
      return "knowledge.datasets.typeImageQA";
    case "audio_video":
      return "knowledge.datasets.typeAVSearch";
    case "document":
    default:
      return "knowledge.datasets.typeDocSearch";
  }
}

// 获取知识库类型的颜色
function getKBTypeColor(kbType?: string) {
  switch (kbType) {
    case "data":
      return { bg: "#e6f7e6", color: "#52c41a" };
    case "image":
      return { bg: "#f3e8ff", color: "#9333ea" };
    case "audio_video":
      return { bg: "#fff7e6", color: "#fa8c16" };
    case "document":
    default:
      return { bg: "#e6f4ff", color: "#1677ff" };
  }
}

// 知识库卡片组件 - 优化设计
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
  const { t } = useTranslation();

  const copyId = (e: React.MouseEvent) => {
    e.stopPropagation();
    copyToClipboard(dataset.dataset_id);
    setCopied(true);
    message.success(t("knowledge.datasets.idCopied"));
    setTimeout(() => setCopied(false), 2000);
  };

  const menuItems: MenuProps["items"] = [
    {
      key: "view",
      label: t("knowledge.datasets.viewDetail"),
      icon: <EyeOutlined />,
    },
    {
      key: "edit",
      label: t("knowledge.datasets.editConfig"),
      icon: <EditOutlined />,
    },
    {
      key: "test",
      label: t("knowledge.datasets.hitTest"),
      icon: <ExperimentOutlined />,
    },
    { type: "divider" },
    {
      key: "delete",
      label: t("knowledge.datasets.deleteKB"),
      icon: <DeleteOutlined />,
      danger: true,
    },
  ];

  const menuActions: Record<string, () => void> = {
    view: onViewDetail,
    edit: onEdit,
    test: onHitTest,
    delete: onDelete,
  };

  const handleMenuClick: MenuProps["onClick"] = ({ key, domEvent }) => {
    domEvent.stopPropagation();
    menuActions[String(key)]?.();
  };

  const kbTypeColor = getKBTypeColor(dataset.kb_type);
  const docCount = dataset.statistics?.document_count ?? 0;
  const segCount = dataset.statistics?.segment_count ?? 0;

  return (
    <Card
      hoverable
      onClick={onViewDetail}
      style={{
        borderRadius: 12,
        border: `1px solid ${darkMode ? colors.neutral[700] : colors.neutral[200]}`,
        background: darkMode ? colors.neutral[800] : "#ffffff",
        cursor: "pointer",
        overflow: "hidden",
      }}
      styles={{
        body: { padding: 0 },
      }}
    >
      {/* 顶部彩色条 */}
      <div
        style={{
          height: 4,
          background: kbTypeColor.color,
        }}
      />

      <div style={{ padding: "16px 20px" }}>
        {/* 头部：类型标签 + 更多操作 */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            marginBottom: 12,
          }}
        >
          <Tag
            style={{
              borderRadius: 4,
              padding: "2px 8px",
              fontSize: 11,
              border: "none",
              background: darkMode ? `${kbTypeColor.color}20` : kbTypeColor.bg,
              color: kbTypeColor.color,
              fontWeight: 500,
            }}
          >
            {t(getKBTypeLabelKey(dataset.kb_type))}
          </Tag>

          <Dropdown
            menu={{ items: menuItems, onClick: handleMenuClick }}
            trigger={["click"]}
            placement="bottomRight"
          >
            <div
              onClick={(e) => e.stopPropagation()}
              onMouseDown={(e) => e.stopPropagation()}
              style={{
                width: 28,
                height: 28,
                borderRadius: 6,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                cursor: "pointer",
                background: "transparent",
                transition: "background 0.2s",
              }}
              onMouseEnter={(e) => {
                e.currentTarget.style.background = darkMode
                  ? colors.neutral[700]
                  : colors.neutral[100];
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.background = "transparent";
              }}
            >
              <MoreOutlined style={{ fontSize: 14, color: colors.neutral[400] }} />
            </div>
          </Dropdown>
        </div>

        {/* 标题和描述 */}
        <div style={{ marginBottom: 16 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
            <span
              style={{
                fontSize: 20,
                color: kbTypeColor.color,
                display: "flex",
                alignItems: "center",
              }}
            >
              {getKBTypeIcon(dataset.kb_type)}
            </span>
            <Title
              level={5}
              style={{
                margin: 0,
                fontSize: 16,
                fontWeight: 600,
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
                flex: 1,
              }}
            >
              {dataset.name}
            </Title>
          </div>
          <Paragraph
            type="secondary"
            ellipsis={{ rows: 2 }}
            style={{
              fontSize: 13,
              margin: 0,
              minHeight: 40,
              lineHeight: "20px",
            }}
          >
            {dataset.description || t("knowledge.datasets.noDescription")}
          </Paragraph>
        </div>

        {/* 统计数据 - 清晰的水平布局 */}
        <div
          style={{
            display: "flex",
            gap: 24,
            padding: "12px 0",
            borderTop: `1px solid ${darkMode ? colors.neutral[700] : colors.neutral[100]}`,
            borderBottom: `1px solid ${darkMode ? colors.neutral[700] : colors.neutral[100]}`,
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <FileTextOutlined style={{ fontSize: 16, color: colors.neutral[400] }} />
            <div>
              <Text strong style={{ fontSize: 18, lineHeight: 1 }}>{docCount}</Text>
              <Text type="secondary" style={{ fontSize: 11, marginLeft: 4 }}>{t("knowledge.datasets.documents")}</Text>
            </div>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <NodeIndexOutlined style={{ fontSize: 16, color: colors.neutral[400] }} />
            <div>
              <Text strong style={{ fontSize: 18, lineHeight: 1 }}>{segCount}</Text>
              <Text type="secondary" style={{ fontSize: 11, marginLeft: 4 }}>{t("knowledge.datasets.segments")}</Text>
            </div>
          </div>
        </div>

        {/* 底部：ID + 可见性 */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            paddingTop: 12,
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
            <Text
              type="secondary"
              style={{
                fontSize: 11,
                fontFamily: "monospace",
              }}
            >
              {dataset.dataset_id.slice(0, 10)}...
            </Text>
            <Tooltip title={copied ? t("knowledge.datasets.copied") : t("knowledge.datasets.copyId")}>
              <span
                onClick={copyId}
                style={{ cursor: "pointer", display: "flex" }}
              >
                {copied ? (
                  <CheckOutlined style={{ fontSize: 11, color: colors.primary[500] }} />
                ) : (
                  <CopyOutlined style={{ fontSize: 11, color: colors.neutral[400] }} />
                )}
              </span>
            </Tooltip>
          </div>
          <Tag
            style={{
              borderRadius: 4,
              padding: "1px 6px",
              fontSize: 10,
              border: "none",
              background: darkMode ? colors.neutral[700] : colors.neutral[100],
              color: darkMode ? colors.neutral[300] : colors.neutral[500],
            }}
          >
            {dataset.visibility === "private"
              ? t("knowledge.datasets.visPrivate")
              : dataset.visibility === "tenant"
                ? t("knowledge.datasets.visTenant")
                : t("knowledge.datasets.visPublic")}
          </Tag>
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
  const { t } = useTranslation();

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
        {t("knowledge.datasets.emptyTitle")}
      </Title>
      <Paragraph
        type="secondary"
        style={{
          maxWidth: 400,
          margin: "0 auto 24px",
        }}
      >
        {t("knowledge.datasets.emptyDesc")}
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
          {t("knowledge.datasets.create")}
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
          {t("knowledge.datasets.import")}
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
  const { t } = useTranslation();

  const [searchQuery, setSearchQuery] = useState("");
  const [typeFilter, setTypeFilter] = useState<string>("all");
  const [deleteModalOpen, setDeleteModalOpen] = useState(false);
  const [deletingDataset, setDeletingDataset] = useState<Dataset | null>(null);
  const [deletePassword, setDeletePassword] = useState("");
  const [deleteSubmitting, setDeleteSubmitting] = useState(false);

  const resetDeleteModal = () => {
    setDeleteModalOpen(false);
    setDeletingDataset(null);
    setDeletePassword("");
  };

  // 过滤后的数据集
  const filteredDatasets = useMemo(() => {
    return datasets.filter((d) => {
      const matchesSearch =
        d.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
        d.dataset_id.toLowerCase().includes(searchQuery.toLowerCase()) ||
        (d.description || "").toLowerCase().includes(searchQuery.toLowerCase());
      const matchesType =
        typeFilter === "all" || (d.kb_type || "document") === typeFilter;
      return matchesSearch && matchesType;
    });
  }, [datasets, searchQuery, typeFilter]);

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
    if (!deletingDataset || deleteSubmitting) return;
    if (!deletePassword) {
      message.warning(t("knowledge.datasets.deletePasswordRequired"));
      return;
    }
    setDeleteSubmitting(true);
    try {
      await deleteDataset(deletingDataset.dataset_id, {
        password: deletePassword,
      });
      await qc.invalidateQueries({ queryKey: ["kb-datasets"] });
      message.success(t("knowledge.datasets.deleteSuccess"));
      resetDeleteModal();
    } catch (e) {
      message.error(t("knowledge.datasets.deleteFailed") + ": " + (e instanceof Error ? e.message : String(e)));
    } finally {
      setDeleteSubmitting(false);
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
              {t("knowledge.datasets.title")}
            </Title>
            <Text type="secondary" style={{ fontSize: 14 }}>
              {t("knowledge.datasets.subtitle")}
            </Text>
          </div>

          <Space size={12}>
            <Button
              icon={<ReloadOutlined spin={datasetsQuery.isFetching} />}
              onClick={() => qc.invalidateQueries({ queryKey: ["kb-datasets"] })}
              style={{ borderRadius: 6 }}
            >
              {t("knowledge.datasets.refresh")}
            </Button>
            <Button
              type="primary"
              icon={<PlusOutlined />}
              onClick={() => nav("/knowledge/create")}
              style={{ borderRadius: 6 }}
            >
              {t("knowledge.datasets.create")}
            </Button>
          </Space>
        </div>
      </div>

      {/* 统计卡片 */}
      <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
        <Col xs={24} sm={8}>
          <StatCard
            title={t("knowledge.datasets.totalKB")}
            value={stats.total}
            icon={<DatabaseOutlined />}
            color={colors.primary[500]}
            index={0}
          />
        </Col>
        <Col xs={24} sm={8}>
          <StatCard
            title={t("knowledge.datasets.totalDocs")}
            value={stats.documents}
            icon={<FileTextOutlined />}
            color={colors.primary[400]}
            index={1}
          />
        </Col>
        <Col xs={24} sm={8}>
          <StatCard
            title={t("knowledge.datasets.totalSegments")}
            value={stats.segments}
            icon={<NodeIndexOutlined />}
            color={colors.primary[600]}
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
            placeholder={t("knowledge.datasets.searchPlaceholder")}
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
                  <span>{t(opt.labelKey)}</span>
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
            <Text type="secondary">{t("knowledge.datasets.loading")}</Text>
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
                  setDeletePassword("");
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
            <DeleteOutlined style={{ color: "#EF4444" }} />
            <span>{t("knowledge.datasets.deleteConfirmTitle")}</span>
          </Space>
        }
        open={deleteModalOpen}
        onCancel={() => {
          if (deleteSubmitting) {
            return;
          }
          resetDeleteModal();
        }}
        onOk={handleDelete}
        confirmLoading={deleteSubmitting}
        maskClosable={!deleteSubmitting}
        keyboard={!deleteSubmitting}
        okText={t("knowledge.datasets.deleteConfirm")}
        cancelText={t("common.cancel")}
        okButtonProps={{
          danger: true,
          disabled: !deletePassword || deleteSubmitting,
          loading: deleteSubmitting,
        }}
        cancelButtonProps={{
          disabled: deleteSubmitting,
        }}
      >
        <div style={{ padding: "12px 0" }}>
          <Text>
            {t("knowledge.datasets.deleteConfirmText", { name: deletingDataset?.name })}
          </Text>
          <br />
          <Text type="secondary" style={{ fontSize: 13 }}>
            {t("knowledge.datasets.deleteWarning")}
          </Text>
          <div style={{ marginTop: 12 }}>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {t("knowledge.datasets.deletePasswordLabel")}
            </Text>
            <Input.Password
              value={deletePassword}
              onChange={(e) => setDeletePassword(e.target.value)}
              placeholder={t("knowledge.datasets.deletePasswordPlaceholder")}
              autoComplete="current-password"
              disabled={deleteSubmitting}
              onPressEnter={() => {
                if (!deleteSubmitting && deletePassword) {
                  void handleDelete();
                }
              }}
              style={{ marginTop: 6 }}
            />
          </div>
        </div>
      </Modal>
    </div>
  );
}
