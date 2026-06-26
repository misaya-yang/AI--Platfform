import { Alert, Button, Empty, Form, Input, InputNumber, Select, Space, Spin, Tag, Typography } from "antd";
import { Send } from "lucide-react";
import { useMemo } from "react";
import { useTranslation } from "react-i18next";

import type { AgentTraceScore, AgentTraceScoreCreate, ScoreType } from "@/api/eval";

const { Text } = Typography;

interface TraceScorePanelProps {
  traceId?: string;
  scores: AgentTraceScore[];
  loading: boolean;
  submitting: boolean;
  error?: Error | null;
  onSubmit: (payload: AgentTraceScoreCreate) => Promise<void>;
}

interface ScoreFormValues {
  score_name: string;
  score_type: ScoreType;
  numeric_value?: number | null;
  boolean_value?: boolean | null;
  categorical_value?: string;
  text_value?: string;
  label?: string;
  explanation?: string;
}

function formatDate(value: string | null | undefined, locale: string) {
  if (!value) return "-";
  return new Date(value).toLocaleString(locale);
}

function renderScoreValue(score: AgentTraceScore) {
  if (score.score_type === "numeric") return score.numeric_value ?? "-";
  if (score.score_type === "boolean") return score.boolean_value === null ? "-" : String(score.boolean_value);
  if (score.score_type === "categorical") return score.categorical_value || "-";
  return score.text_value || "-";
}

export function TraceScorePanel({
  traceId,
  scores,
  loading,
  submitting,
  error,
  onSubmit,
}: TraceScorePanelProps) {
  const { t, i18n } = useTranslation();
  const [form] = Form.useForm<ScoreFormValues>();
  const scoreType = Form.useWatch("score_type", form) || "numeric";
  const defaultValues = useMemo<ScoreFormValues>(
    () => ({
      score_name: "quality",
      score_type: "numeric",
      numeric_value: 0.8,
      label: "reviewed",
    }),
    []
  );

  const submit = async (values: ScoreFormValues) => {
    const payload: AgentTraceScoreCreate = {
      score_name: values.score_name,
      score_type: values.score_type,
      numeric_value: values.score_type === "numeric" ? values.numeric_value ?? null : null,
      boolean_value: values.score_type === "boolean" ? values.boolean_value ?? null : null,
      categorical_value: values.score_type === "categorical" ? values.categorical_value || null : null,
      text_value: values.score_type === "text" ? values.text_value || null : null,
      label: values.label || null,
      explanation: values.explanation || null,
      scorer_type: "human",
      metadata: { source: "eval_console" },
    };
    await onSubmit(payload);
    form.resetFields();
    form.setFieldsValue(defaultValues);
  };

  return (
    <aside className="eval-panel eval-score-panel" aria-label="Trace scoring">
      <div className="eval-panel-heading">
        <div>
          <h2>{t("eval.score.title")}</h2>
          <p>{t("eval.score.records", { count: scores.length })}</p>
        </div>
        <Tag>{traceId ? t("eval.score.traceSelected") : t("eval.score.noTrace")}</Tag>
      </div>

      {error ? (
        <Alert
          type="error"
          showIcon
          title={t("eval.score.unavailableTitle")}
          description={error.message || t("eval.score.unavailableDescription")}
        />
      ) : null}

      <div className="eval-score-list" aria-label="Trace score records">
        {loading ? (
          <div className="eval-score-state">
            <Spin size="small" />
          </div>
        ) : scores.length > 0 ? (
          scores.map((score) => (
            <article className="eval-score-record" key={score.score_id}>
              <Space size={6} wrap>
                <strong>{score.score_name}</strong>
                <Tag>{t(`eval.score.types.${score.score_type}`, score.score_type)}</Tag>
                {score.label ? <Tag color="blue">{score.label}</Tag> : null}
              </Space>
              <div className="eval-score-description">
                <strong>{renderScoreValue(score)}</strong>
                <span>{score.explanation || t("eval.score.noExplanation")}</span>
                <Text type="secondary">
                  {score.created_by} · {formatDate(score.created_at, i18n.language)}
                </Text>
              </div>
            </article>
          ))
        ) : (
          <div className="eval-score-state">
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t("eval.score.empty")} />
          </div>
        )}
      </div>

      <Form
        form={form}
        className="eval-score-form"
        layout="vertical"
        initialValues={defaultValues}
        onFinish={submit}
        disabled={!traceId || submitting}
      >
        <div className="eval-score-form-grid">
          <Form.Item
            label={t("eval.score.form.scoreName")}
            name="score_name"
            rules={[{ required: true, message: t("eval.score.form.scoreNameRequired") }]}
          >
            <Input maxLength={96} placeholder={t("eval.score.form.scoreNamePlaceholder")} />
          </Form.Item>
          <Form.Item label={t("eval.score.form.type")} name="score_type" rules={[{ required: true }]}>
            <Select
              options={[
                { label: t("eval.score.types.numeric"), value: "numeric" },
                { label: t("eval.score.types.boolean"), value: "boolean" },
                { label: t("eval.score.types.categorical"), value: "categorical" },
                { label: t("eval.score.types.text"), value: "text" },
              ]}
            />
          </Form.Item>
        </div>

        {scoreType === "numeric" ? (
          <Form.Item label={t("eval.score.form.numericValue")} name="numeric_value">
            <InputNumber min={0} max={1} step={0.05} style={{ width: "100%" }} />
          </Form.Item>
        ) : null}
        {scoreType === "boolean" ? (
          <Form.Item label={t("eval.score.form.booleanValue")} name="boolean_value">
            <Select
              options={[
                { label: t("common.true", "True"), value: true },
                { label: t("common.false", "False"), value: false },
              ]}
            />
          </Form.Item>
        ) : null}
        {scoreType === "categorical" ? (
          <Form.Item label={t("eval.score.form.category")} name="categorical_value">
            <Input maxLength={96} placeholder={t("eval.score.form.categoryPlaceholder")} />
          </Form.Item>
        ) : null}
        {scoreType === "text" ? (
          <Form.Item label={t("eval.score.form.textValue")} name="text_value">
            <Input.TextArea maxLength={2000} rows={3} placeholder={t("eval.score.form.textPlaceholder")} />
          </Form.Item>
        ) : null}

        <Form.Item label={t("eval.score.form.label")} name="label">
          <Input maxLength={96} placeholder={t("eval.score.form.labelPlaceholder")} />
        </Form.Item>
        <Form.Item label={t("eval.score.form.explanation")} name="explanation">
          <Input.TextArea maxLength={2000} rows={3} placeholder={t("eval.score.form.explanationPlaceholder")} />
        </Form.Item>

        <Button
          type="primary"
          htmlType="submit"
          icon={<Send size={15} />}
          loading={submitting}
          disabled={!traceId}
          block
        >
          {t("eval.score.form.submit")}
        </Button>
      </Form>
    </aside>
  );
}
