/**
 * AgenTrux Channel Plugin for OpenClaw.
 *
 * File delivery (Slack pattern):
 *   1. Auto-merge: tools with details.media.mediaUrls (image_generate,
 *      browser.screenshot) or MEDIA:<path> in text (exec) are auto-
 *      merged into payload.mediaUrls → sendPayload → MinIO upload.
 *   2. actions.upload-file: agent calls message(action=upload-file,
 *      filePath=...) → handleAction → publishOutboundPayload direct.
 *      This mirrors Slack's handleSlackMessageAction upload-file path.
 *
 * Tools (LLM-callable):
 *   - agentrux_activate, agentrux_publish, agentrux_read,
 *     agentrux_send_message, agentrux_redeem_grant, agentrux_write
 *
 * Channel:
 *   - registerChannel("agentrux") — SSE + Pull monitoring, SDK reply pipeline
 *
 * Credentials: ~/.agentrux/credentials.json (0600)
 */

import { loadCredentials, saveCredentials, type Credentials } from "./credentials";
import { httpJson, authRequest, invalidateToken, uploadFile } from "./http-client";
import { agentruxGateway, type AgenTruxAccount } from "./gateway";
import { setPluginRuntime } from "./runtime";
import * as fs from "fs";
import * as path from "path";
import * as os from "os";

// Module-level state for tools
let credentials: Credentials | null = null;

// Active request context. gateway.ts stashes the per-event identity
// (request_id, conversation_key, result topic) here before dispatching
// to OpenClaw, and sendPayload reads it back when it needs to publish
// the openclaw.response. OpenClaw processes events sequentially on the
// channel side, so a single-slot context is sufficient.
interface ActiveRequestContext {
  requestId: string;
  conversationKey: string;
  resultTopicId: string;
}
const activeRequestStack: ActiveRequestContext[] = [];

export function setActiveRequest(ctx: ActiveRequestContext): void {
  activeRequestStack.push(ctx);
}

export function clearActiveRequest(requestId: string): void {
  for (let i = activeRequestStack.length - 1; i >= 0; i--) {
    if (activeRequestStack[i].requestId === requestId) {
      activeRequestStack.splice(i, 1);
      return;
    }
  }
}

export function getActiveRequest(): ActiveRequestContext | null {
  return activeRequestStack.length > 0 ? activeRequestStack[activeRequestStack.length - 1] : null;
}

function getCredentials(): Credentials {
  if (!credentials) {
    credentials = loadCredentials();
    if (!credentials) throw new Error("Not connected to AgenTrux. Use activate first.");
  }
  return credentials;
}


// ---------------------------------------------------------------------------
// Config adapter
// ---------------------------------------------------------------------------

function resolveAccountFromPluginConfig(pluginConfig: any): AgenTruxAccount {
  return {
    commandTopicId: pluginConfig.commandTopicId ?? "",
    resultTopicId: pluginConfig.resultTopicId ?? "",
    agentId: pluginConfig.agentId ?? "",
    baseUrl: pluginConfig.baseUrl ?? "https://api.agentrux.com",
    pollIntervalMs: pluginConfig.pollIntervalMs ?? 60_000,
    maxConcurrency: pluginConfig.maxConcurrency ?? 3,
    subagentTimeoutMs: pluginConfig.subagentTimeoutMs ?? 120_000,
    execPolicy: pluginConfig.execPolicy ?? { enabled: false, allowedCommands: [] },
  };
}

// ---------------------------------------------------------------------------
// Outbound helpers used by the sendPayload adapter below
// ---------------------------------------------------------------------------

const OUTBOUND_MIME_BY_EXT: Record<string, string> = {
  ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  ".pdf": "application/pdf",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".gif": "image/gif",
  ".svg": "image/svg+xml",
  ".webp": "image/webp",
  ".csv": "text/csv",
  ".json": "application/json",
  ".txt": "text/plain",
  ".md": "text/markdown",
  ".html": "text/html",
  ".xml": "application/xml",
  ".zip": "application/zip",
  ".mp3": "audio/mpeg",
  ".wav": "audio/wav",
  ".mp4": "video/mp4",
  ".mov": "video/quicktime",
};
const OUTBOUND_TEXT_MIME = /^(text\/|application\/(json|xml|javascript|typescript))/;
const OUTBOUND_TEXT_INLINE_MAX_BYTES = 50 * 1024;

/** Normalize a reply-payload media reference into an absolute local
 *  path on disk. OpenClaw emits absolute paths for tool-produced media
 *  today (e.g. /home/.../tool-image-generation/xxx.png) but it may also
 *  emit `file://` URLs in some flows, so we accept both. */
function normalizeLocalMediaPath(ref: string | undefined | null): string | null {
  if (!ref || typeof ref !== "string") return null;
  const trimmed = ref.trim();
  if (!trimmed) return null;
  if (trimmed.startsWith("file://")) return trimmed.slice(7);
  if (trimmed.startsWith("/")) return trimmed;
  return null; // any other scheme (http, sandbox:, relative paths) is out of scope
}

/** Collect the local media paths a reply payload is carrying, in the
 *  order OpenClaw intends for delivery. Handles both the scalar
 *  `mediaUrl` and the array `mediaUrls`. Deduplicates. */
function collectPayloadMediaPaths(payload: any): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  const push = (ref: unknown) => {
    if (typeof ref !== "string") return;
    const p = normalizeLocalMediaPath(ref);
    if (!p || seen.has(p)) return;
    seen.add(p);
    out.push(p);
  };
  if (Array.isArray(payload?.mediaUrls)) payload.mediaUrls.forEach(push);
  push(payload?.mediaUrl);
  return out;
}

/** Replace any occurrence of a local media path (or its basename) in
 *  the reply text with a clean "[添付ファイル参照]" marker. LLMs often
 *  hallucinate a human-readable path into the body even when the real
 *  file lives elsewhere, and surfacing raw /home/... paths to the end
 *  user is noisy at best.
 *
 *  `scrubBasenames` additionally strips the bare basename (e.g. "ECHO"
 *  when the path is "/home/.../ECHO"). Use this only for payload media
 *  paths where the basename is an internal ID — NOT for text-derived
 *  paths where the basename is usually a human-meaningful word (the
 *  file's name) the user wants to see in the sentence. */
function scrubHallucinatedPaths(text: string, mediaPaths: string[], scrubBasenames: boolean = true): string {
  if (!text) return text;
  let out = text;
  const candidates = new Set<string>();
  for (const p of mediaPaths) {
    candidates.add(p);
    if (scrubBasenames) candidates.add(path.basename(p));
  }
  // Match absolute-looking local paths that look like tool output.
  // /home/..., /tmp/..., /var/... → "[添付ファイル参照]". We intentionally
  // keep this regex narrow so normal prose (e.g. "/etc/passwd" in a
  // security discussion) survives, but anything ending in a media-like
  // extension on an absolute path gets stripped.
  const pathRe = /\/(?:home|tmp|var|Users)\/[^\s「」()[\],]+\.(?:png|jpg|jpeg|gif|webp|svg|pdf|mp3|mp4|mov|wav|txt|md|csv|json|html|xml|zip|docx|xlsx|pptx)/gi;
  out = out.replace(pathRe, "[添付ファイル参照]");
  // Also strip any exact matches of our resolved mediaPaths or their
  // basenames, in case they arrived in a form the generic regex above
  // missed (e.g. wrapped in quotes or brackets).
  for (const c of candidates) {
    if (!c) continue;
    const escaped = c.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    out = out.replace(new RegExp(escaped, "g"), "[添付ファイル参照]");
  }
  // Collapse "「[添付ファイル参照]」" variants back to bare marker so
  // Japanese quote brackets don't end up around an empty token.
  out = out.replace(/「\s*\[添付ファイル参照\]\s*」/g, "[添付ファイル参照]");
  return out.trim();
}

interface UploadedOutboundAttachment {
  name: string;
  object_id: string;
  content_type: string;
  size?: number;
  download_url?: string;
}

// Shared publish routine used by BOTH the channel outbound adapter
// `sendPayload` and the multimodal turn helper gateway.ts invokes after
// agentCommandFromIngress returns. Splitting it out here keeps all the
// "AgenTrux event shape" logic (attachments, text scrubbing, request
// context lookup) in a single place.
//
// Returns true if an event was published, false if the payload was
// empty and nothing was sent.
export async function publishOutboundPayload(
  payload: {
    text?: string | null;
    mediaUrl?: string | null;
    mediaUrls?: string[] | null;
  },
  pluginConfig: any,
  logger: {
    info?: (m: string) => void;
    warn?: (m: string) => void;
    error?: (m: string) => void;
  },
): Promise<boolean> {
  const active = getActiveRequest();
  if (!active) {
    logger.warn?.("[agentrux] publishOutboundPayload called without active request context — dropping");
    return false;
  }
  const account = resolveAccountFromPluginConfig(pluginConfig);
  const creds = getCredentials();
  const resultTopicId = active.resultTopicId || account.resultTopicId;
  const requestId = active.requestId;
  const conversationKey = active.conversationKey;

  // Upload whatever arrives in payload.mediaUrls (auto-merged by
  // OpenClaw from producer tool results like image_generate).
  const mediaPaths = collectPayloadMediaPaths(payload);
  const rawText = typeof payload?.text === "string" ? payload.text : "";

  const uploadedAttachments: UploadedOutboundAttachment[] = [];
  const inlineTextBlocks: string[] = [];
  for (const p of mediaPaths) {
    const prepared = await preparePayloadMediaForPublish(p, creds, resultTopicId, logger);
    if (prepared.kind === "attachment") {
      uploadedAttachments.push(prepared.attachment);
    } else if (prepared.kind === "inlineText") {
      inlineTextBlocks.push(`[添付: ${prepared.name}]\n${prepared.text}\n[/添付]`);
    }
  }

  // Scrub tool-generated media paths from the reply text. These are
  // internal IDs like "/home/.../image-1---abc.png" the user should
  // never see.
  let cleanedText = scrubHallucinatedPaths(rawText, mediaPaths, true);
  if (inlineTextBlocks.length > 0) {
    cleanedText = [cleanedText, ...inlineTextBlocks].filter((s) => s && s.length > 0).join("\n\n");
  }

  const allAttachments = uploadedAttachments;
  if (!cleanedText && allAttachments.length === 0) {
    return false;
  }

  const messageForPublish = cleanedText || "(添付ファイルを受信)";
  const responsePayload: any = {
    request_id: requestId,
    conversation_key: conversationKey,
    status: "completed",
    message: messageForPublish,
  };
  if (allAttachments.length > 0) {
    responsePayload.attachments = allAttachments;
  }

  await authRequest(creds, "POST", `/topics/${resultTopicId}/events`, {
    type: "openclaw.response",
    payload: responsePayload,
  });
  logger.info?.(
    `[agentrux] Published openclaw.response req=${requestId} text=${messageForPublish.length}c attachments=${allAttachments.length}`,
  );
  return true;
}

/** Read the pluginConfig we stashed at register time so gateway.ts can
 *  invoke publishOutboundPayload without re-discovering it. */
let _sharedPluginConfig: any = null;
export function setSharedPluginConfig(cfg: any): void {
  _sharedPluginConfig = cfg;
}
export function getSharedPluginConfig(): any {
  return _sharedPluginConfig;
}

/** Convert one local file into a publishable AgenTrux attachment.
 *  Text files below the inline threshold are returned as `inlineText`
 *  instead so the caller can fold them into the message body like we
 *  do for inbound text attachments. */
async function preparePayloadMediaForPublish(
  localPath: string,
  creds: Credentials,
  resultTopicId: string,
  log: {
    info?: (m: string) => void;
    warn?: (m: string) => void;
  },
  forceAttachment: boolean = false,
): Promise<
  | { kind: "inlineText"; name: string; text: string }
  | { kind: "attachment"; attachment: UploadedOutboundAttachment }
  | { kind: "missing"; path: string; reason: string }
> {
  try {
    const stat = fs.statSync(localPath);
    if (!stat.isFile()) return { kind: "missing", path: localPath, reason: "not a regular file" };
    const name = path.basename(localPath);
    const ext = path.extname(name).toLowerCase();
    const contentType = OUTBOUND_MIME_BY_EXT[ext] || "application/octet-stream";
    if (!forceAttachment && OUTBOUND_TEXT_MIME.test(contentType) && stat.size <= OUTBOUND_TEXT_INLINE_MAX_BYTES) {
      const text = fs.readFileSync(localPath, "utf-8");
      return { kind: "inlineText", name, text };
    }
    const uploaded = await uploadFile(creds, resultTopicId, localPath, contentType);
    log.info?.(`[agentrux] Uploaded outbound ${name} (${contentType}) → ${uploaded.object_id}`);
    return {
      kind: "attachment",
      attachment: {
        name,
        object_id: uploaded.object_id,
        content_type: contentType,
        size: stat.size,
        download_url: uploaded.download_url,
      },
    };
  } catch (err: any) {
    log.warn?.(`[agentrux] preparePayloadMediaForPublish failed for ${localPath}: ${err?.message ?? err}`);
    return { kind: "missing", path: localPath, reason: err?.message ?? "unknown" };
  }
}

// ---------------------------------------------------------------------------
// Plugin entry — object format (same as openclaw-nostr SDK pattern)
// ---------------------------------------------------------------------------

const plugin = {
  id: "agentrux-openclaw-plugin",
  name: "AgenTrux",
  description: "Agent-to-Agent authenticated Pub/Sub via AgenTrux topics",
  register(api: any) {

  const rawLogger = api.logger || {};
  const logger = {
    info: (...a: any[]) => (rawLogger.info || console.log)(...a),
    warn: (...a: any[]) => (rawLogger.warn || console.warn)(...a),
    error: (...a: any[]) => (rawLogger.error || console.error)(...a),
  };

  // Store PluginRuntime at register time (SDK pattern from openclaw-nostr)
  setPluginRuntime(api.runtime);

  const pluginConfig = api.pluginConfig
    || api.config?.plugins?.entries?.["agentrux-openclaw-plugin"]?.config
    || {};
  // Make the resolved plugin config visible to the gateway module so
  // it can invoke publishOutboundPayload without re-discovering it.
  setSharedPluginConfig(pluginConfig);

  // =======================================================================
  // CHANNEL PLUGIN REGISTRATION
  // =======================================================================

  const agentruxChannelPlugin = {
    id: "agentrux",
    meta: {
      id: "agentrux",
      label: "AgenTrux",
      selectionLabel: "AgenTrux Pub/Sub",
      docsPath: "agentrux",
      blurb: "Agent-to-Agent authenticated Pub/Sub via AgenTrux topics",
    },
    capabilities: {
      chatTypes: ["direct" as const],
      media: true,
    },
    // File delivery is handled entirely by OpenClaw's built-in
    // tool-result media merge path:
    //
    //   1. Tools like `image_generate`, `browser.screenshot`, `read`
    //      return `details.media.mediaUrls: [...]` with the produced
    //      paths.
    //   2. OpenClaw's `extractToolResultMediaArtifact` picks those up
    //      and stashes them into `state.pendingToolMediaUrls`.
    //   3. On the next assistant reply block, OpenClaw auto-merges
    //      the pending paths into `payload.mediaUrls`.
    //   4. Our `sendPayload` adapter below reads `ctx.payload.mediaUrls`
    //      and uploads each file to MinIO as an attachment.
    //
    // messageToolHints: Slack level — channel characteristics only.
    agentPrompt: {
      messageToolHints: () => [
        "- AgenTrux is a Pub/Sub channel. Replies and file attachments " +
        "are delivered as `openclaw.response` events.",
        "- Files produced by `write` and `image_generate` are " +
        "automatically attached to your reply.",
        "- For `exec`-produced files, append `&& echo MEDIA:<path>` " +
        "so the file is auto-attached.",
        "- `message` with `action: \"upload-file\"` and `filePath` is " +
        "available as a manual fallback.",
      ],
    },
    // ---------------------------------------------------------------
    // Slack-pattern: messaging adapter
    // Slack uses normalizeTarget + targetResolver to let the `message`
    // tool route to the correct channel/user. AgenTrux has a single
    // delivery target (the configured result topic), so we accept
    // anything as a valid target.
    // ---------------------------------------------------------------
    messaging: {
      normalizeTarget: (raw: string) => (raw?.trim() || "agentrux:default"),
      targetResolver: {
        looksLikeId: (raw: string) => Boolean(raw?.trim()),
        hint: "<any — routes to configured result topic>",
      },
    },
    // ---------------------------------------------------------------
    // Slack-pattern: actions adapter
    // Slack exposes send/upload-file/react/... via describeMessageTool.
    // The action names appear in the `message` tool's `action` enum,
    // which is how the agent learns it can upload files. handleAction
    // does the actual upload via publishOutboundPayload (our MinIO
    // upload path), mirroring Slack's handleSlackMessageAction which
    // calls loadWebMedia → Slack files API.
    // ---------------------------------------------------------------
    actions: {
      describeMessageTool: () => ({
        actions: ["send", "upload-file"] as const,
        capabilities: ["media"] as const,
      }),
      supportsAction: ({ action }: { action: string }) =>
        action === "send" || action === "upload-file",
      handleAction: async (ctx: any) => {
        const action = ctx.action;
        const params = ctx.params ?? {};

        if (action === "upload-file") {
          const filePath =
            (typeof params.filePath === "string" && params.filePath.trim()) ||
            (typeof params.path === "string" && params.path.trim()) ||
            (typeof params.media === "string" && params.media.trim()) ||
            "";
          if (!filePath) {
            return {
              content: [{ type: "text", text: "upload-file requires filePath, path, or media" }],
              details: { status: "error" },
            };
          }
          const caption =
            (typeof params.initialComment === "string" && params.initialComment) ||
            (typeof params.message === "string" && params.message) ||
            (typeof params.caption === "string" && params.caption) ||
            "";
          const published = await publishOutboundPayload(
            { text: caption || undefined, mediaUrls: [filePath] },
            pluginConfig,
            logger,
          );
          return {
            content: [{
              type: "text",
              text: published
                ? `Uploaded ${path.basename(filePath)} to AgenTrux.`
                : "upload-file: nothing published (missing request context)",
            }],
            details: { status: published ? "ok" : "empty" },
          };
        }

        if (action === "send") {
          const text =
            (typeof params.message === "string" && params.message) ||
            (typeof params.text === "string" && params.text) ||
            "";
          const mediaUrls: string[] = [];
          if (typeof params.media === "string" && params.media.trim())
            mediaUrls.push(params.media.trim());
          if (Array.isArray(params.mediaUrls))
            for (const m of params.mediaUrls)
              if (typeof m === "string" && m.trim()) mediaUrls.push(m.trim());
          const published = await publishOutboundPayload(
            { text: text || undefined, mediaUrls: mediaUrls.length ? mediaUrls : undefined },
            pluginConfig,
            logger,
          );
          return {
            content: [{
              type: "text",
              text: published ? "Sent." : "send: nothing published",
            }],
            details: { status: published ? "ok" : "empty" },
          };
        }

        return {
          content: [{ type: "text", text: `Unsupported action: ${action}` }],
          details: { status: "error" },
        };
      },
    },
    // Channel-provided write tool that delivers the file to the user.
    // Standard `write` creates files but doesn't deliver them (no
    // details.media in its result). This tool writes the file AND
    // publishes it as an attachment via direct AgenTrux API call.
    // Runs in the openclaw-node subprocess (separate from gateway),
    // so it calls authRequest directly instead of publishOutboundPayload.
    agentTools: () => [{
      name: "agentrux_deliver",
      description:
        "Deliver a file to the user. Two modes:\n" +
        "  1. filePath: deliver an EXISTING file (created by exec, convert, etc.)\n" +
        "  2. path + content: create a NEW text file and deliver it\n" +
        "Use filePath for binary files (images, PDFs). " +
        "Use path + content for text files you want to create from scratch.\n" +
        "Do NOT use this for image_generate or browser.screenshot results — " +
        "those are automatically delivered without any extra step.",
      parameters: {
        type: "object" as const,
        properties: {
          filePath: { type: "string" as const, description: "Absolute path to an existing file to deliver" },
          path: { type: "string" as const, description: "File path for new text file (relative to workspace)" },
          content: { type: "string" as const, description: "Text content to write (for new files only)" },
        },
      },
      async execute(_id: string, args: { filePath?: string; path?: string; content?: string }) {
        const workspaceDir = process.env.OPENCLAW_WORKSPACE || "/home/nobuokitagawa/.openclaw/workspace";
        let absPath: string;
        let contentType: string;

        if (args.filePath) {
          // Mode 1: deliver existing file
          absPath = path.resolve(workspaceDir, args.filePath);

          // Guard: files under ~/.openclaw/media/ are auto-delivered
          // by OpenClaw's media merge pipeline (image_generate,
          // browser.screenshot, etc.). Return success so the agent
          // does not retry.
          const autoMergeDir = path.join(os.homedir(), ".openclaw", "media");
          if (absPath.startsWith(autoMergeDir + path.sep) || absPath.startsWith(autoMergeDir + "/")) {
            return { content: [{ type: "text", text: "This file is auto-delivered by OpenClaw — no extra step needed." }] };
          }

          if (!fs.existsSync(absPath)) {
            return { content: [{ type: "text", text:
              `File not found: ${absPath}. ` +
              "If this was produced by image_generate or browser.screenshot, " +
              "it is already auto-delivered — no agentrux_deliver needed." }] };
          }
          const ext = path.extname(absPath).toLowerCase();
          const mimeMap: Record<string, string> = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
            ".gif": "image/gif", ".webp": "image/webp", ".pdf": "application/pdf",
            ".txt": "text/plain", ".csv": "text/csv", ".json": "application/json",
            ".html": "text/html", ".xml": "application/xml", ".md": "text/markdown",
          };
          contentType = mimeMap[ext] || "application/octet-stream";
        } else if (args.path && args.content != null) {
          // Mode 2: create new text file
          absPath = path.resolve(workspaceDir, args.path);
          fs.mkdirSync(path.dirname(absPath), { recursive: true });
          fs.writeFileSync(absPath, args.content, "utf-8");
          contentType = "text/plain";
        } else {
          return { content: [{ type: "text", text: "Provide filePath (existing file) or path + content (new file)" }] };
        }

        // Publish directly via AgenTrux API (subprocess-safe).
        try {
          const creds = getCredentials();
          const topicId = pluginConfig.resultTopicId || "";
          if (topicId) {
            const uploaded = await uploadFile(creds, topicId, absPath, contentType);
            await authRequest(creds, "POST", `/topics/${topicId}/events`, {
              type: "openclaw.response",
              payload: {
                request_id: `agentrux_deliver-${Date.now()}`,
                conversation_key: "default",
                status: "completed",
                message: `(添付ファイル: ${path.basename(absPath)})`,
                attachments: [{
                  name: path.basename(absPath),
                  object_id: uploaded.object_id,
                  content_type: contentType,
                  size: fs.statSync(absPath).size,
                  download_url: uploaded.download_url,
                }],
              },
            });
            logger.info(`[agentrux] agentrux_deliver published ${path.basename(absPath)} → ${uploaded.object_id}`);
          }
        } catch (err: any) {
          logger.warn(`[agentrux] agentrux_deliver publish failed: ${err?.message ?? err}`);
        }
        return {
          content: [{
            type: "text",
            text: args.filePath
              ? `Delivered ${path.basename(absPath)}`
              : `Created and delivered ${path.basename(absPath)} (${args.content!.length} bytes)`,
          }],
        };
      },
    }],
    config: {
      listAccountIds: () => {
        if (pluginConfig.commandTopicId && pluginConfig.resultTopicId && pluginConfig.agentId) {
          return ["default"];
        }
        return [];
      },
      resolveAccount: (_cfg: any, _accountId?: string | null) => {
        return resolveAccountFromPluginConfig(pluginConfig);
      },
      isEnabled: (account: AgenTruxAccount) =>
        !!(account.commandTopicId && account.resultTopicId && account.agentId),
      isConfigured: (account: AgenTruxAccount) =>
        !!(account.commandTopicId && account.resultTopicId && account.agentId),
    },
    outbound: {
      deliveryMode: "direct" as const,
      // Publish each reply block as an openclaw.response event.
      // payload.mediaUrls (auto-merged by OpenClaw) are uploaded to
      // MinIO as attachments.
      sendPayload: async (ctx: any) => {
        const published = await publishOutboundPayload(
          {
            text: ctx?.payload?.text,
            mediaUrl: ctx?.payload?.mediaUrl,
            mediaUrls: ctx?.payload?.mediaUrls,
          },
          pluginConfig,
          logger,
        );
        return {
          ok: true,
          messageId: published ? `agentrux-payload-${Date.now()}` : "empty-dropped",
        };
      },
      // sendText: fallback for legacy code paths.
      sendText: async (ctx: any) => {
        const text = typeof ctx?.text === "string" ? ctx.text : "";
        if (!text) return { ok: true, messageId: "empty" };
        const published = await publishOutboundPayload(
          { text },
          pluginConfig,
          logger,
        );
        return {
          ok: true,
          messageId: published ? `agentrux-${Date.now()}` : "empty-dropped",
        };
      },
    },
    gateway: agentruxGateway,
  };

  if (typeof api.registerChannel === "function") {
    // Must use { plugin: ... } wrapper (same as openclaw-nostr) for proper
    // gateway runtime binding. Direct object registration breaks subagent context.
    api.registerChannel({ plugin: agentruxChannelPlugin });
    logger.info("[agentrux] Registered as ChannelPlugin");
  } else {
    logger.warn("[agentrux] registerChannel not available — tools-only mode");
  }

  // =======================================================================
  // TOOLS
  // =======================================================================

  api.registerTool(
    {
      name: "agentrux_activate",
      description:
        "Connect to AgenTrux with a one-time activation code. " +
        "Returns script_id, client_secret, and available topics.",
      parameters: {
        type: "object",
        properties: {
          activation_code: { type: "string", description: "One-time activation code (ac_...)" },
          base_url: { type: "string", description: "AgenTrux API URL (default: https://api.agentrux.com)" },
        },
        required: ["activation_code"],
      },
      async execute(_id: string, params: { activation_code: string; base_url?: string }) {
        const baseUrl = params.base_url || "https://api.agentrux.com";
        const r = await httpJson("POST", `${baseUrl}/auth/activate`, {
          activation_code: params.activation_code,
        });
        if (r.status !== 200) {
          return { content: [{ type: "text", text: `Activation failed: ${JSON.stringify(r.data)}` }] };
        }
        const creds = { base_url: baseUrl, script_id: r.data.script_id, clientSecret: r.data.client_secret };
        saveCredentials(creds);
        credentials = creds;
        const grants = (r.data.grants || []).map((g: any) => `  - ${g.topic_id} (${g.action})`).join("\n");
        return {
          content: [{ type: "text", text: `Connected to AgenTrux!\nScript ID: ${r.data.script_id}\nTopics:\n${grants}` }],
        };
      },
    },
    { optional: true },
  );

  // Data plane tools — available for non-ingress sessions (cron,
  // heartbeat, subagent, CLI). Blocked during ingress turns where
  // the reply is published automatically by the channel adapter.

  api.registerTool({
    name: "agentrux_publish",
    description: "Publish an event to an AgenTrux topic. Not available during ingress processing.",
    parameters: {
      type: "object",
      properties: {
        topic_id: { type: "string", description: "UUID of the topic" },
        event_type: { type: "string", description: "Event type (e.g. 'message.send')" },
        payload: { type: "object", description: "JSON payload" },
        correlation_id: { type: "string", description: "Optional correlation ID" },
        reply_topic: { type: "string", description: "Optional reply topic UUID" },
      },
      required: ["topic_id", "event_type", "payload"],
    },
    async execute(_id: string, params: any) {
      if (getActiveRequest() !== null) {
        return { content: [{ type: "text", text: "Not available during ingress — your reply is published automatically." }] };
      }
      const creds = getCredentials();
      const body: any = { type: params.event_type, payload: params.payload };
      if (params.correlation_id) body.correlation_id = params.correlation_id;
      if (params.reply_topic) body.reply_topic = params.reply_topic;
      const result = await authRequest(creds, "POST", `/topics/${params.topic_id}/events`, body);
      return { content: [{ type: "text", text: `Event published (event_id: ${result.event_id})` }] };
    },
  });

  api.registerTool({
    name: "agentrux_read",
    description: "Read events from an AgenTrux topic. Not available during ingress processing.",
    parameters: {
      type: "object",
      properties: {
        topic_id: { type: "string", description: "UUID of the topic" },
        limit: { type: "number", description: "Max events to return (default 10)" },
        event_type: { type: "string", description: "Filter by event type" },
      },
      required: ["topic_id"],
    },
    async execute(_id: string, params: any) {
      if (getActiveRequest() !== null) {
        return { content: [{ type: "text", text: "Not available during ingress — use this from a non-ingress session." }] };
      }
      const creds = getCredentials();
      const query = new URLSearchParams();
      query.set("limit", String(params.limit || 10));
      if (params.event_type) query.set("type", params.event_type);
      const result = await authRequest(creds, "GET", `/topics/${params.topic_id}/events?${query}`);
      const items = result.items || [];
      if (items.length === 0) return { content: [{ type: "text", text: "No events found." }] };
      const lines = items.map((e: any) =>
        `[seq:${e.sequence_no}] ${e.type} — ${JSON.stringify(e.payload)} (${e.correlation_id || "-"})`,
      );
      return { content: [{ type: "text", text: `${items.length} events:\n${lines.join("\n")}` }] };
    },
  });

  api.registerTool({
    name: "agentrux_send_message",
    description: "Send a message to another agent and wait for a reply. Not available during ingress processing.",
    parameters: {
      type: "object",
      properties: {
        topic_id: { type: "string", description: "Target agent's topic UUID" },
        reply_topic: { type: "string", description: "Your topic UUID for replies" },
        message: { type: "string", description: "Message text" },
        timeout_seconds: { type: "number", description: "Wait timeout (default 30)" },
      },
      required: ["topic_id", "reply_topic", "message"],
    },
    async execute(_id: string, params: any) {
      if (getActiveRequest() !== null) {
        return { content: [{ type: "text", text: "Not available during ingress — your reply is published automatically." }] };
      }
      const creds = getCredentials();
      const corrId = `msg-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
      const timeout = (params.timeout_seconds || 30) * 1000;
      await authRequest(creds, "POST", `/topics/${params.topic_id}/events`, {
        type: "message.request",
        payload: { text: params.message },
        correlation_id: corrId,
        reply_topic: params.reply_topic,
      });
      const start = Date.now();
      while (Date.now() - start < timeout) {
        const result = await authRequest(creds, "GET", `/topics/${params.reply_topic}/events?limit=20`);
        for (const e of result.items || []) {
          if (e.correlation_id === corrId) {
            return { content: [{ type: "text", text: `Reply:\n${JSON.stringify(e.payload, null, 2)}` }] };
          }
        }
        await new Promise((r) => setTimeout(r, 2000));
      }
      return { content: [{ type: "text", text: `No reply within ${params.timeout_seconds || 30}s.` }] };
    },
  });

  api.registerTool(
    {
      name: "agentrux_redeem_grant",
      description: "Redeem an invite code for cross-account topic access.",
      parameters: {
        type: "object",
        properties: { invite_code: { type: "string", description: "Invite code (inv_...)" } },
        required: ["invite_code"],
      },
      async execute(_id: string, params: { invite_code: string }) {
        const creds = getCredentials();
        const r = await httpJson("POST", `${creds.base_url}/auth/redeem-invite-code`, {
          invite_code: params.invite_code,
          script_id: creds.script_id,
          client_secret: creds.clientSecret,
        });
        if (r.status >= 400) {
          return { content: [{ type: "text", text: `Failed: ${JSON.stringify(r.data)}` }] };
        }
        invalidateToken();
        return {
          content: [{ type: "text", text: `Access granted! Topic: ${r.data.topic_id} (${r.data.action})` }],
        };
      },
    },
    { optional: true },
  );

  }, // end register()
};

export default plugin;
