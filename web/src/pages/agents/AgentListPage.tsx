import { useDeferredValue, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  App as AntApp,
  Button,
  Dropdown,
  Empty,
  Input,
  Select,
  Skeleton,
  Table,
  Tag,
  Typography,
  type MenuProps,
  type TableColumnsType,
} from "antd";
import {
  Archive,
  BarChart3,
  Bot,
  ChevronRight,
  Copy,
  MoreHorizontal,
  Plus,
  Search,
  ShieldAlert,
} from "lucide-react";
import { Link, useNavigate } from "react-router-dom";
import { formatDistanceToNow } from "date-fns";
import { enUS, zhCN } from "date-fns/locale";
import { useTranslation } from "react-i18next";

import { agentErrorDetail, archiveAgent, copyAgent, listAgents } from "@/api/agents";
import type { AgentStatus, AgentSummary } from "@/types/agents";
import "./agent-studio.css";

const { Text, Title } = Typography;

function statusColor(status: AgentStatus): string {
  if (status === "active") return "green";
  if (status === "archived") return "default";
  if (status === "deleted") return "red";
  return "blue";
}

function AgentActions({
  agent,
  onCopy,
  onArchive,
}: {
  agent: AgentSummary;
  onCopy: (agent: AgentSummary) => void;
  onArchive: (agent: AgentSummary) => void;
}) {
  const navigate = useNavigate();
  const { t } = useTranslation();
  const items: MenuProps["items"] = [
    {
      key: "open",
      label: t("agents.list.openStudio"),
      icon: <ChevronRight size={14} />,
      onClick: () => navigate(`/agents/${agent.agent_id}`),
    },
    {
      key: "analytics",
      label: t("agents.analytics.title"),
      icon: <BarChart3 size={14} />,
      onClick: () => navigate(`/agents/${agent.agent_id}/analytics`),
    },
    { key: "copy", label: t("agents.list.copyAgent"), icon: <Copy size={14} />, disabled: agent.caller_role === "viewer", onClick: () => onCopy(agent) },
    { key: "archive", label: t("agents.list.archive"), icon: <Archive size={14} />, disabled: agent.caller_role !== "owner", onClick: () => onArchive(agent) },
  ];
  return (
    <Dropdown menu={{ items }} trigger={["click"]}>
      <Button
        type="text"
        icon={<MoreHorizontal size={17} />}
        aria-label={t("agents.list.actionsFor", { name: agent.name })}
      />
    </Dropdown>
  );
}

function AgentMobileRow({
  agent,
  onCopy,
  onArchive,
}: {
  agent: AgentSummary;
  onCopy: (agent: AgentSummary) => void;
  onArchive: (agent: AgentSummary) => void;
}) {
  const { t, i18n } = useTranslation();
  const dateLocale = i18n.resolvedLanguage === "zh-CN" ? zhCN : enUS;
  return (
    <article className="agent-mobile-row">
      <span className="agent-avatar" aria-hidden><Bot size={18} /></span>
      <Link className="agent-mobile-copy" to={`/agents/${agent.agent_id}`}>
        <strong>{agent.name}</strong>
        <span>{agent.description || t("agents.list.noDescription")}</span>
        <span className="agent-mobile-meta">
          <Tag color={statusColor(agent.status)}>{t(`agents.common.statuses.${agent.status}`)}</Tag>
          <span>{t("agents.list.draftRevision", { revision: agent.draft_revision ?? 0 })}</span>
          <span>{formatDistanceToNow(new Date(agent.updated_at), { addSuffix: true, locale: dateLocale })}</span>
        </span>
      </Link>
      <AgentActions agent={agent} onCopy={onCopy} onArchive={onArchive} />
    </article>
  );
}

export function AgentListPage() {
  const { t, i18n } = useTranslation();
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState<string | undefined>();
  const [owner, setOwner] = useState<string | undefined>();
  const deferredSearch = useDeferredValue(search.trim());
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { message, modal } = AntApp.useApp();
  const query = useQuery({
    queryKey: ["agents", { search: deferredSearch, status, owner }],
    queryFn: () =>
      listAgents({
        limit: 100,
        search: deferredSearch || undefined,
        status,
        owner_id: owner,
      }),
    retry: false,
  });
  const error = query.error ? agentErrorDetail(query.error) : null;
  const agents = useMemo(() => query.data?.items ?? [], [query.data?.items]);
  const ownerOptions = useMemo(
    () =>
      Array.from(new Set(agents.map((agent) => agent.owner_id))).map((ownerId) => ({
        value: ownerId,
        label: ownerId,
      })),
    [agents],
  );
  const filtersActive = Boolean(deferredSearch || status || owner);
  const dateLocale = i18n.resolvedLanguage === "zh-CN" ? zhCN : enUS;
  const copyMutation = useMutation({
    mutationFn: (agent: AgentSummary) => copyAgent(agent.agent_id),
    onSuccess: (copied) => {
      message.success(t("agents.list.copySuccess"));
      navigate(`/agents/${copied.agent_id}`);
    },
    onError: () => message.error(t("agents.list.copyError")),
  });
  const archiveMutation = useMutation({
    mutationFn: (agent: AgentSummary) => archiveAgent(agent.agent_id),
    onSuccess: async () => {
      message.success(t("agents.list.archiveSuccess"));
      await queryClient.invalidateQueries({ queryKey: ["agents"] });
    },
    onError: () => message.error(t("agents.list.archiveError")),
  });
  const confirmArchive = (agent: AgentSummary) => {
    modal.confirm({
      title: t("agents.list.archiveConfirmTitle", { name: agent.name }),
      content: t("agents.list.archiveConfirmDescription"),
      okText: t("agents.list.archiveConfirm"),
      okButtonProps: { danger: true },
      cancelText: t("agents.common.cancel"),
      onOk: () => archiveMutation.mutateAsync(agent),
    });
  };

  const columns: TableColumnsType<AgentSummary> = [
    {
      title: t("agents.list.columns.agent"),
      dataIndex: "name",
      key: "agent",
      width: "38%",
      render: (_, agent) => (
        <div className="agent-name-cell">
          <span className="agent-avatar" aria-hidden><Bot size={18} /></span>
          <span className="min-w-0">
            <Link className="agent-name-link" to={`/agents/${agent.agent_id}`}>{agent.name}</Link>
            <Text type="secondary" className="agent-row-description">{agent.description || t("agents.list.noDescription")}</Text>
          </span>
        </div>
      ),
    },
    {
      title: t("agents.list.columns.status"),
      dataIndex: "status",
      width: 118,
      render: (value: AgentStatus) => <Tag color={statusColor(value)}>{t(`agents.common.statuses.${value}`)}</Tag>,
    },
    { title: t("agents.list.columns.owner"), dataIndex: "owner_id", ellipsis: true },
    {
      title: t("agents.list.columns.draft"),
      dataIndex: "draft_revision",
      width: 96,
      render: (value: number | null) => `r${value ?? 0}`,
    },
    {
      title: t("agents.list.columns.updated"),
      dataIndex: "updated_at",
      width: 150,
      render: (value: string) => formatDistanceToNow(new Date(value), { addSuffix: true, locale: dateLocale }),
    },
    {
      title: <span className="sr-only">{t("agents.list.columns.actions")}</span>,
      key: "actions",
      align: "right",
      width: 64,
      render: (_, agent) => <AgentActions agent={agent} onCopy={(row) => copyMutation.mutate(row)} onArchive={confirmArchive} />,
    },
  ];

  return (
    <main className="agent-page ui-page" data-testid="agents-page">
      <div className="agent-page-heading">
        <div>
          <Title level={2}>{t("agents.list.title")}</Title>
          <Text type="secondary">{t("agents.list.subtitle")}</Text>
        </div>
        {error?.status !== 403 && (
          <Button
            type="primary"
            size="large"
            icon={<Plus size={17} />}
            onClick={() => navigate("/agents/new")}
          >
            {t("agents.common.createAgent")}
          </Button>
        )}
      </div>

      <section className="agent-directory" aria-label={t("agents.list.directoryLabel")}>
        <div className="agent-toolbar">
          <Input
            allowClear
            prefix={<Search size={16} aria-hidden />}
            placeholder={t("agents.list.searchPlaceholder")}
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            aria-label={t("agents.list.searchLabel")}
          />
          <Select
            allowClear
            placeholder={t("agents.list.allStatuses")}
            value={status}
            onChange={setStatus}
            aria-label={t("agents.list.filterStatus")}
            options={[
              { value: "draft", label: t("agents.common.statuses.draft") },
              { value: "active", label: t("agents.common.statuses.active") },
              { value: "archived", label: t("agents.common.statuses.archived") },
            ]}
          />
          <Select
            allowClear
            showSearch
            placeholder={t("agents.list.allOwners")}
            value={owner}
            onChange={setOwner}
            aria-label={t("agents.list.filterOwner")}
            options={ownerOptions}
          />
          <Select
            value="updated"
            aria-label={t("agents.list.sortLabel")}
            options={[{ value: "updated", label: t("agents.list.recentlyUpdated") }]}
          />
        </div>

        {query.isLoading && (
          <div className="agent-loading" role="status" aria-label={t("agents.list.loading")}>
            <Skeleton active paragraph={{ rows: 6 }} />
          </div>
        )}

        {error?.status === 403 && (
          <div className="agent-permission-state" role="alert">
            <ShieldAlert size={30} />
            <Title level={3}>{t("agents.list.accessTitle")}</Title>
            <Text type="secondary">{t("agents.list.accessDescription")}</Text>
          </div>
        )}

        {error && error.status !== 403 && (
          <Alert
            type="error"
            showIcon
            title={t("agents.list.loadError")}
            description={error.message}
            action={<Button onClick={() => void query.refetch()}>{t("agents.common.retry")}</Button>}
          />
        )}

        {!query.isLoading && !error && agents.length === 0 && (
          <Empty
            className="agent-empty"
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={
              filtersActive
                ? t("agents.list.filteredEmpty")
                : t("agents.list.empty")
            }
          >
            {filtersActive ? (
              <Button onClick={() => { setSearch(""); setStatus(undefined); setOwner(undefined); }}>
                {t("agents.list.clearFilters")}
              </Button>
            ) : (
              <div className="agent-empty-actions">
                <Button type="primary" icon={<Plus size={16} />} onClick={() => navigate("/agents/new")}>
                  {t("agents.list.createBlank")}
                </Button>
                <Button onClick={() => navigate("/agents/new?template=support")}>{t("agents.list.supportTemplate")}</Button>
                <Button onClick={() => navigate("/agents/new?template=knowledge")}>{t("agents.list.knowledgeTemplate")}</Button>
              </div>
            )}
          </Empty>
        )}

        {!query.isLoading && !error && agents.length > 0 && (
          <>
            <div className="agent-table-desktop">
              <Table
                rowKey="agent_id"
                columns={columns}
                dataSource={agents}
                pagination={false}
                size="middle"
              />
            </div>
            <div className="agent-list-mobile" aria-label={t("agents.list.mobileLabel")}>
              {agents.map((agent) => (
                <AgentMobileRow
                  key={agent.agent_id}
                  agent={agent}
                  onCopy={(row) => copyMutation.mutate(row)}
                  onArchive={confirmArchive}
                />
              ))}
            </div>
            <div className="agent-directory-footer">{t(agents.length === 1 ? "agents.list.countOne" : "agents.list.countOther", { count: agents.length })}</div>
          </>
        )}
      </section>
    </main>
  );
}
