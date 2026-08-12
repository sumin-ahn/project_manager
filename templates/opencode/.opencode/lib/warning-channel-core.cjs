// Shared non-blocking warning delivery for OpenCode guard plugins.
// Toast reaches the person immediately; pending system context reaches the
// model on its next turn.  Either channel may be unavailable without blocking.

function sessionKey(value) {
  return value ? String(value) : "__global__";
}

function createWarningChannel(client) {
  const pending = new Map();

  async function publish(sessionID, text) {
    const key = sessionKey(sessionID);
    pending.set(key, String(text));
    try {
      if (client && client.tui && client.tui.showToast) {
        await client.tui.showToast({
          body: { message: String(text), variant: "warning" },
        });
      }
    } catch {
      // The pending model-facing warning remains available when toast fails.
    }
  }

  function inject(input, output) {
    const key = sessionKey(input && input.sessionID);
    const text = pending.get(key) || pending.get("__global__");
    if (!text || !output || !Array.isArray(output.system)) return;
    output.system.push(text);
    pending.delete(key);
    if (key !== "__global__") pending.delete("__global__");
  }

  return { inject, publish };
}

module.exports = { createWarningChannel };
