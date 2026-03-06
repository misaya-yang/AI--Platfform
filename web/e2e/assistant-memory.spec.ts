import { expect, test } from "@playwright/test";
import { buildAuthHeaders, getApiUrl } from "./support/helpers";

type AssistantSseEvent = {
  event_type?: string;
  data?: unknown;
};

function parseAssistantSse(body: string): AssistantSseEvent[] {
  return body
    .split(/\n\n+/)
    .map((chunk) =>
      chunk
        .split("\n")
        .find((line) => line.startsWith("data: "))
        ?.slice(6)
        .trim()
    )
    .filter((payload): payload is string => Boolean(payload) && payload !== "[DONE]")
    .map((payload) => {
      try {
        return JSON.parse(payload) as AssistantSseEvent;
      } catch {
        return { event_type: "unknown", data: payload };
      }
    });
}

function collectAssistantText(events: AssistantSseEvent[]): string {
  return events
    .filter((event) => event.event_type === "text_delta")
    .map((event) => {
      if (typeof event.data === "string") {
        return event.data;
      }
      if (event.data && typeof event.data === "object") {
        const record = event.data as Record<string, unknown>;
        if (typeof record.text === "string") {
          return record.text;
        }
        if (typeof record.data === "string") {
          return record.data;
        }
      }
      return "";
    })
    .join("");
}

test("assistant retains user identity across fresh sessions", async ({ request }) => {
  test.setTimeout(90_000);

  const headers = await buildAuthHeaders(request);
  const apiUrl = getApiUrl();
  const uniqueSuffix = Date.now().toString(36);
  const preferredName = `测试用户-${uniqueSuffix}`;
  const cityLabel = "悉尼";
  const location = `${cityLabel}-${uniqueSuffix}`;

  const rememberResponse = await request.post(`${apiUrl}/api/v1/assistant/chat/stream`, {
    headers: {
      ...headers,
      "content-type": "application/json",
      accept: "text/event-stream",
    },
    data: {
      message: `请记住：我的名字是${preferredName}，我来自${location}。只回复“已记住”。`,
      memory_mode: "strict",
    },
  });

  expect(rememberResponse.ok()).toBeTruthy();
  const rememberText = collectAssistantText(
    parseAssistantSse(await rememberResponse.text())
  );
  expect(rememberText).not.toContain("error");

  const recallResponse = await request.post(`${apiUrl}/api/v1/assistant/chat/stream`, {
    headers: {
      ...headers,
      "content-type": "application/json",
      accept: "text/event-stream",
    },
    data: {
      message: "你还记得我的名字和城市吗？直接回答。",
      memory_mode: "strict",
    },
  });

  expect(recallResponse.ok()).toBeTruthy();
  const recallText = collectAssistantText(
    parseAssistantSse(await recallResponse.text())
  );

  expect(recallText).toContain(preferredName);
  expect(recallText).toContain(cityLabel);
});
