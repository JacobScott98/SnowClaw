/**
 * cortex-code — OpenClaw plugin
 *
 * Bridges Cortex Code and the OpenClaw gateway.
 * Exposes an MCP-compatible endpoint that Cortex Code can connect to as an MCP server.
 *
 * Approach: Plugin registers an HTTP route that speaks the MCP protocol,
 * allowing Cortex Code to discover and invoke OpenClaw agents/tools.
 */

export default function cortexCodePlugin(api) {
  // Register the MCP endpoint
  api.registerHttpRoute({
    method: "POST",
    path: "/mcp",
    handler: async (req, res) => {
      const body = req.body;

      if (body.method === "initialize") {
        return res.json({
          jsonrpc: "2.0",
          id: body.id,
          result: {
            protocolVersion: "2024-11-05",
            serverInfo: {
              name: "snowclaw-cortex-code",
              version: "0.1.0",
            },
            capabilities: {
              tools: { listChanged: false },
            },
          },
        });
      }

      if (body.method === "tools/list") {
        return res.json({
          jsonrpc: "2.0",
          id: body.id,
          result: {
            tools: getExposedTools(api),
          },
        });
      }

      if (body.method === "tools/call") {
        const { name, arguments: args } = body.params;
        const result = await invokeTool(api, name, args);
        return res.json({
          jsonrpc: "2.0",
          id: body.id,
          result: {
            content: [{ type: "text", text: JSON.stringify(result) }],
          },
        });
      }

      return res.status(400).json({
        jsonrpc: "2.0",
        id: body.id ?? null,
        error: { code: -32601, message: "Method not found" },
      });
    },
  });
}

function getExposedTools(api) {
  // TODO: Enumerate registered tools from OpenClaw api and return MCP-formatted list
  return [];
}

async function invokeTool(api, name, args) {
  // TODO: Route tool invocation through OpenClaw's tool execution pipeline
  return { status: "not_implemented", tool: name, args };
}
