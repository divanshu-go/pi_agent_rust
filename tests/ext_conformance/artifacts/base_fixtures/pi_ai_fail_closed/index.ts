import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import * as ai from "@mariozechner/pi-ai";

const unsupportedChecks = [
  ["complete", () => ai.complete("model", [{ role: "user", content: "hi" }])],
  ["completeSimple", () => ai.completeSimple("model", "hi")],
  ["streamSimpleAnthropic", () => ai.streamSimpleAnthropic()],
  ["streamSimpleOpenAIResponses", () => ai.streamSimpleOpenAIResponses()],
  ["streamSimpleOpenAICompletions", () => ai.streamSimpleOpenAICompletions()],
  ["getModel", () => ai.getModel()],
  ["getApiProvider", () => ai.getApiProvider()],
  ["getModels", () => ai.getModels()],
  ["loginOpenAICodex", () => ai.loginOpenAICodex({})],
  ["refreshOpenAICodexToken", () => ai.refreshOpenAICodexToken("refresh")],
  ["getOAuthApiKey", () => ai.getOAuthApiKey("openai")],
];

async function classifyUnsupported(name: string, call: () => unknown): Promise<string> {
  try {
    await call();
    return `${name}:unexpected-success`;
  } catch (err) {
    const message = String((err as Error)?.message ?? err ?? "");
    const failClosed =
      message.includes(name) && message.includes("refusing to return placeholder data");
    return `${name}:${failClosed ? "fail-closed" : "wrong-error"}`;
  }
}

export default function(pi: ExtensionAPI) {
  pi.registerTool({
    name: "pi_ai_contract",
    description: "Checks @mariozechner/pi-ai compatibility helper behavior",
    parameters: {
      type: "object",
      properties: {},
    },
    execute: async () => {
      const lines = [
        `getEnvApiKey:export:${typeof ai.getEnvApiKey}`,
        `getOAuthApiKey:export:${typeof ai.getOAuthApiKey}`,
      ];

      for (const [name, call] of unsupportedChecks) {
        lines.push(await classifyUnsupported(String(name), call as () => unknown));
      }

      return {
        content: [{ type: "text", text: lines.join("\n") }],
        details: { lines },
      };
    },
  });
}
