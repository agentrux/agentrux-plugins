import logging

from dify_plugin import Plugin, DifyPluginEnv

logger = logging.getLogger(__name__)

plugin = Plugin(DifyPluginEnv())

# Re-spawn SSE workers for any subscription that was active when this
# plugin process last exited. Persisted state lives in
# .agentrux_sse_*.json under the plugin's working directory; see
# trigger/sse_worker.py. Webhook-mode subscriptions don't need any
# resume — Dify replays incoming HTTP to the plugin on its own.
try:
    from trigger import sse_worker
    n = sse_worker.resume_persisted()
    if n:
        logger.info("agentrux-trigger: resumed %d SSE subscription(s)", n)
except Exception as e:
    logger.warning("agentrux-trigger: SSE resume failed: %s", e)


if __name__ == "__main__":
    plugin.run()
