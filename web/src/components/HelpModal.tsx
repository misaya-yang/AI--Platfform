import { Modal, Typography, Collapse, Space, Tag, Divider } from "antd";
import {
  DashboardOutlined,
  CloudServerOutlined,
  DatabaseOutlined,
  ThunderboltOutlined,
  UnorderedListOutlined,
  SettingOutlined,
  RocketOutlined,
  BookOutlined,
  ApiOutlined,
  BulbOutlined,
} from "@ant-design/icons";
import { colors } from "@/theme/themeConfig";

const { Title, Text, Paragraph } = Typography;

interface HelpModalProps {
  open: boolean;
  onClose: () => void;
}

export function HelpModal({ open, onClose }: HelpModalProps) {
  const features = [
    {
      key: "dashboard",
      label: (
        <Space>
          <DashboardOutlined style={{ color: colors.primary[500] }} />
          <span>仪表盘</span>
        </Space>
      ),
      children: (
        <div>
          <Paragraph>
            仪表盘是系统的概览页面，展示所有已注册服务的状态和关键指标。
          </Paragraph>
          <ul style={{ paddingLeft: 20, margin: "8px 0" }}>
            <li>查看所有服务的健康状态</li>
            <li>监控服务调用次数和响应时间</li>
            <li>快速了解系统整体运行情况</li>
          </ul>
        </div>
      ),
    },
    {
      key: "services",
      label: (
        <Space>
          <CloudServerOutlined style={{ color: colors.cyan[500] }} />
          <span>服务管理</span>
        </Space>
      ),
      children: (
        <div>
          <Paragraph>
            管理所有已注册的 AI 服务，包括 LLM 模型、LangGraph Agent 等。
          </Paragraph>
          <ul style={{ paddingLeft: 20, margin: "8px 0" }}>
            <li><strong>注册服务</strong>：添加新的 AI 服务端点</li>
            <li><strong>配置参数</strong>：设置服务的 API 密钥、模型参数等</li>
            <li><strong>健康检查</strong>：监控服务可用性</li>
            <li><strong>服务类型</strong>：支持 OpenAI、Anthropic、LangGraph 等</li>
          </ul>
        </div>
      ),
    },
    {
      key: "knowledge",
      label: (
        <Space>
          <DatabaseOutlined style={{ color: colors.purple[500] }} />
          <span>知识库</span>
          <Tag color="blue" style={{ marginLeft: 4 }}>核心功能</Tag>
        </Space>
      ),
      children: (
        <div>
          <Paragraph>
            构建和管理 AI 知识库，支持文档上传、向量检索和智能问答。
          </Paragraph>
          <Title level={5} style={{ marginTop: 12 }}>主要功能：</Title>
          <ul style={{ paddingLeft: 20, margin: "8px 0" }}>
            <li><strong>创建知识库</strong>：选择嵌入模型，配置检索策略</li>
            <li><strong>上传文档</strong>：支持 PDF、Word、TXT、Markdown 等格式</li>
            <li><strong>URL 导入</strong>：从网页地址抓取内容</li>
            <li><strong>段落管理</strong>：查看和编辑文档分块</li>
            <li><strong>命中测试</strong>：测试检索效果，优化参数</li>
            <li><strong>QA 问答</strong>：基于知识库的智能问答</li>
          </ul>
          <Title level={5} style={{ marginTop: 12 }}>检索模式：</Title>
          <Space wrap style={{ marginTop: 8 }}>
            <Tag>向量检索</Tag>
            <Tag>关键词检索</Tag>
            <Tag>混合检索</Tag>
            <Tag>BM25</Tag>
            <Tag>重排序</Tag>
          </Space>
        </div>
      ),
    },
    {
      key: "playground",
      label: (
        <Space>
          <ThunderboltOutlined style={{ color: colors.orange[500] }} />
          <span>智能对话</span>
        </Space>
      ),
      children: (
        <div>
          <Paragraph>
            与 AI 服务进行交互式对话，测试模型效果。
          </Paragraph>
          <ul style={{ paddingLeft: 20, margin: "8px 0" }}>
            <li><strong>多轮对话</strong>：支持上下文记忆的连续对话</li>
            <li><strong>流式输出</strong>：实时显示 AI 响应内容</li>
            <li><strong>会话管理</strong>：保存和恢复历史对话</li>
            <li><strong>工具调用</strong>：查看 Agent 的工具调用过程</li>
            <li><strong>多模态</strong>：支持文本、图片等多种输入</li>
          </ul>
        </div>
      ),
    },
    {
      key: "tasks",
      label: (
        <Space>
          <UnorderedListOutlined style={{ color: colors.primary[400] }} />
          <span>任务管理</span>
        </Space>
      ),
      children: (
        <div>
          <Paragraph>
            追踪和管理异步任务的执行状态。
          </Paragraph>
          <ul style={{ paddingLeft: 20, margin: "8px 0" }}>
            <li>查询任务执行状态</li>
            <li>获取任务执行结果</li>
            <li>支持长时间运行的异步任务</li>
          </ul>
        </div>
      ),
    },
    {
      key: "settings",
      label: (
        <Space>
          <SettingOutlined style={{ color: colors.neutral[500] }} />
          <span>系统设置</span>
        </Space>
      ),
      children: (
        <div>
          <Paragraph>
            配置系统级参数和高级选项。
          </Paragraph>
          <ul style={{ paddingLeft: 20, margin: "8px 0" }}>
            <li>LLM 模型配置</li>
            <li>嵌入模型设置</li>
            <li>系统参数调整</li>
          </ul>
        </div>
      ),
    },
  ];

  return (
    <Modal
      title={
        <Space>
          <BookOutlined style={{ color: colors.primary[500] }} />
          <span>帮助文档</span>
        </Space>
      }
      open={open}
      onCancel={onClose}
      footer={null}
      width={680}
      styles={{
        body: { maxHeight: "70vh", overflow: "auto" },
      }}
    >
      {/* 项目介绍 */}
      <div style={{
        padding: "16px 20px",
        background: `linear-gradient(135deg, ${colors.primary[500]}10, ${colors.cyan[500]}10)`,
        borderRadius: 12,
        marginBottom: 20,
      }}>
        <Space align="start">
          <RocketOutlined style={{ fontSize: 32, color: colors.primary[500], marginTop: 4 }} />
          <div>
            <Title level={4} style={{ margin: 0 }}>AI Platform</Title>
            <Text type="secondary">统一的 AI 服务管理平台</Text>
            <Paragraph style={{ marginTop: 8, marginBottom: 0 }}>
              AI Platform 是一个企业级的 AI 服务管理平台，提供统一的服务注册、
              智能路由、知识库管理和对话测试能力。帮助您快速构建和部署 AI 应用。
            </Paragraph>
          </div>
        </Space>
      </div>

      {/* 快速开始 */}
      <div style={{ marginBottom: 20 }}>
        <Title level={5}>
          <BulbOutlined style={{ marginRight: 8, color: colors.orange[500] }} />
          快速开始
        </Title>
        <div style={{
          display: "grid",
          gridTemplateColumns: "repeat(2, 1fr)",
          gap: 12,
        }}>
          <div style={{
            padding: "12px 16px",
            background: colors.neutral[50],
            borderRadius: 8,
            border: `1px solid ${colors.neutral[200]}`,
          }}>
            <Text strong>1. 注册服务</Text>
            <br />
            <Text type="secondary" style={{ fontSize: 12 }}>
              在服务管理中添加您的 AI 服务
            </Text>
          </div>
          <div style={{
            padding: "12px 16px",
            background: colors.neutral[50],
            borderRadius: 8,
            border: `1px solid ${colors.neutral[200]}`,
          }}>
            <Text strong>2. 创建知识库</Text>
            <br />
            <Text type="secondary" style={{ fontSize: 12 }}>
              上传文档构建 AI 知识体系
            </Text>
          </div>
          <div style={{
            padding: "12px 16px",
            background: colors.neutral[50],
            borderRadius: 8,
            border: `1px solid ${colors.neutral[200]}`,
          }}>
            <Text strong>3. 测试对话</Text>
            <br />
            <Text type="secondary" style={{ fontSize: 12 }}>
              在智能对话中测试 AI 效果
            </Text>
          </div>
          <div style={{
            padding: "12px 16px",
            background: colors.neutral[50],
            borderRadius: 8,
            border: `1px solid ${colors.neutral[200]}`,
          }}>
            <Text strong>4. 集成 API</Text>
            <br />
            <Text type="secondary" style={{ fontSize: 12 }}>
              通过 API 将 AI 能力集成到应用
            </Text>
          </div>
        </div>
      </div>

      <Divider style={{ margin: "16px 0" }} />

      {/* 功能模块 */}
      <Title level={5}>
        <ApiOutlined style={{ marginRight: 8, color: colors.primary[500] }} />
        功能模块
      </Title>
      <Collapse
        items={features}
        defaultActiveKey={["knowledge"]}
        style={{ background: "transparent" }}
        bordered={false}
      />

      {/* 技术支持 */}
      <div style={{
        marginTop: 20,
        padding: "12px 16px",
        background: colors.neutral[50],
        borderRadius: 8,
        textAlign: "center",
      }}>
        <Text type="secondary">
          如需技术支持，请联系系统管理员或查阅 API 文档
        </Text>
      </div>
    </Modal>
  );
}
