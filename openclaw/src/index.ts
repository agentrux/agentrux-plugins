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
// DeviceCredentials persistence は device-code-setup.ts に inline (credentials.ts が
// gitignore 対象のため、 device flow 関連は分離 module で管理 — spec §4-1)。
import {
  type DeviceCredentials,
  saveDeviceCredentials,
  setupViaDeviceCode,
} from "./device-code-setup";
import {
  type TopologyDeclaration,
  type InstallResult,
  installTopology,
} from "./topology-install";
import { httpJson, authRequest, invalidateToken, uploadFile, ensureTopPrefix } from "./http-client";
import { agentruxGateway, type AgenTruxAccount, type MessagingTopic } from "./gateway";
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
  // Inbound event_type — used to pick the matching outbound format so a
  // `composer.text` from the AgenTrux Composer SPA gets a `composer.text`
  // reply (renderable as markdown), and legacy `openclaw.*` peers still
  // see `openclaw.response`.
  inboundEventType?: string;
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

function resolveMessagingTopics(raw: any[]): MessagingTopic[] {
  if (!Array.isArray(raw)) return [];
  return raw
    .filter((t: any) => t && typeof t.id === "string" && typeof t.topicId === "string")
    .map((t: any) => ({
      id: t.id,
      topicId: t.topicId,
      mode: (["read", "write", "readwrite"].includes(t.mode) ? t.mode : "read") as MessagingTopic["mode"],
      listen: t.listen === true,
    }));
}

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
    messagingTopics: resolveMessagingTopics(pluginConfig.messagingTopics),
    // Default true: command/result topics overlap is the common Composer
    // setup and self-echo would otherwise spin the LLM in a loop.
    excludeOwnEvents: pluginConfig.excludeOwnEvents !== false,
    // Default false: skip backlog accumulated while offline. Opt-in only.
    resumeFromLastSeq: pluginConfig.resumeFromLastSeq === true,
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
  payload_object_id: string;       // pob_<uuid> (Phase 2.4a SSOT)
  content_type: string;
  size?: number;
  presigned_get_url?: string;       // 別 GET /payloads/{pob_id} 由来、 TTL 短いので参考程度
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

  // Composer-family inbound (event_type starts with `composer.`) ⇒ reply in
  // the Composer SPA shape so the page renders the answer. Otherwise stick
  // to the legacy `openclaw.response` shape that openclaw-native peers
  // consume.
  const isComposerInbound = (active.inboundEventType ?? "").startsWith("composer.");
  const topicPath = `/topics/${ensureTopPrefix(resultTopicId)}/events`;
  const baseMetadata = { request_id: requestId, conversation_key: conversationKey };

  if (isComposerInbound) {
    // Server contract (publish_event.py): `payload` (inline JSON) and
    // top-level `payload_object_id` are MUTUALLY EXCLUSIVE per event.
    // To carry both an attachment and a text reply, split into
    // 1 event per attachment (`composer.upload`, object_ref) plus
    // an optional final `composer.text` (inline payload), grouped via
    // `metadata.group_id` per docs/04_design/messaging/composer_event_format.md.
    const groupId = crypto.randomUUID();
    // composer_event_format.md §3-2: group は metadata.group_id (UUIDv4) のみで
    // 結束。 payload に attached_pob_ids 等の冗長フィールドは入れない。
    let acceptedAttachments = 0;
    for (const att of allAttachments) {
      if (!att.payload_object_id || !att.payload_object_id.startsWith("pob_")) {
        logger.warn?.(
          `[agentrux] skipping attachment with invalid payload_object_id: ${att.payload_object_id}`,
        );
        continue;
      }
      await authRequest(creds, "POST", topicPath, {
        event_type: "composer.upload",
        payload_object_id: att.payload_object_id,
        metadata: {
          ...baseMetadata,
          group_id: groupId,
          filename: att.name,
          content_type: att.content_type,
          size_bytes: att.size,
        },
      });
      acceptedAttachments += 1;
    }
    let publishedCount = acceptedAttachments;
    if (cleanedText) {
      await authRequest(creds, "POST", topicPath, {
        event_type: "composer.text",
        payload: { content: cleanedText, format: "markdown" },
        metadata: { ...baseMetadata, group_id: groupId },
      });
      publishedCount += 1;
    }
    if (publishedCount === 0) {
      return false;
    }
    logger.info?.(
      `[agentrux] Published composer reply req=${requestId} group=${groupId} text=${cleanedText.length}c attachments=${acceptedAttachments}`,
    );
    return true;
  }

  // Legacy openclaw.response: single event with attachments[] in payload.
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
  await authRequest(creds, "POST", topicPath, {
    event_type: "openclaw.response",
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
    log.info?.(`[agentrux] Uploaded outbound ${name} (${contentType}) → ${uploaded.payload_object_id}`);
    return {
      kind: "attachment",
      attachment: {
        name,
        payload_object_id: uploaded.payload_object_id,
        content_type: contentType,
        size: stat.size,
        presigned_get_url: uploaded.presigned_get_url,
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
      messageToolHints: () => {
        const hints = [
          "- AgenTrux is a Pub/Sub channel. Replies and file attachments " +
          "are delivered as `openclaw.response` events.",
          "- Files produced by `write` and `image_generate` are " +
          "automatically attached to your reply.",
          "- For `exec`-produced files, append `&& echo MEDIA:<path>` " +
          "so the file is auto-attached.",
          "- `message` with `action: \"upload-file\"` and `filePath` is " +
          "available as a manual fallback.",
        ];
        if (resolvedAccount.messagingTopics.length > 0) {
          hints.push(
            "- Messaging topics available via `agentrux_publish` / `agentrux_read` " +
            "(use the `topic` parameter with the logical name):",
          );
          for (const mt of resolvedAccount.messagingTopics) {
            const live = mt.listen ? ", live" : "";
            hints.push(`    - "${mt.id}" (${mt.mode}${live})`);
          }
        }
        return hints;
      },
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
        // workspaceDir 解決: env override > $HOME/.openclaw/workspace。
        // 旧コードは fallback を作者の dev path "/home/nobuokitagawa/..." に
        // hardcode していて、 別 user の VM で create-new-file mode が
        // EACCES で破綻していた (2026-05-27 修正)。
        const workspaceDir =
          process.env.OPENCLAW_WORKSPACE ||
          path.join(os.homedir(), ".openclaw", "workspace");
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

        // Publish directly via AgenTrux API (subprocess-safe). Composer
        // SPA reads `event.payload_ref` (server's top-level
        // `payload_object_id`) and ignores `payload.attachments[]`, so
        // when the inbound was a Composer event, publish via the
        // object_ref path (event_type `composer.upload`, no inline
        // payload). Otherwise keep the legacy openclaw.response shape
        // for openclaw-native peers.
        try {
          const creds = getCredentials();
          const topicId = pluginConfig.resultTopicId || "";
          if (topicId) {
            const uploaded = await uploadFile(creds, topicId, absPath, contentType);
            const fileSize = fs.statSync(absPath).size;
            const fileName = path.basename(absPath);
            const isComposerInbound = (getActiveRequest()?.inboundEventType ?? "").startsWith("composer.");
            const topicPath = `/topics/${ensureTopPrefix(topicId)}/events`;
            if (isComposerInbound) {
              // composer_event_format.md §3-2: standalone attachment でも
              // group_id を付けると receiver の rendering convention に乗る。
              const groupId = crypto.randomUUID();
              await authRequest(creds, "POST", topicPath, {
                event_type: "composer.upload",
                payload_object_id: uploaded.payload_object_id,
                metadata: {
                  group_id: groupId,
                  filename: fileName,
                  content_type: contentType,
                  size_bytes: fileSize,
                  request_id: `agentrux_deliver-${Date.now()}`,
                },
              });
            } else {
              await authRequest(creds, "POST", topicPath, {
                event_type: "openclaw.response",
                payload: {
                  request_id: `agentrux_deliver-${Date.now()}`,
                  conversation_key: "default",
                  status: "completed",
                  message: `(添付ファイル: ${fileName})`,
                  attachments: [{
                    name: fileName,
                    payload_object_id: uploaded.payload_object_id,
                    content_type: contentType,
                    size: fileSize,
                    presigned_get_url: uploaded.presigned_get_url,
                  }],
                },
              });
            }
            logger.info(`[agentrux] agentrux_deliver published ${fileName} → ${uploaded.payload_object_id}`);
          }
        } catch (err: any) {
          // 実 publish 失敗を agent に正直に返す (旧 code は常に "Delivered" を
          // 返していて、 agent が成功と誤認識する bug があった 2026-05-27 修正)。
          const msg = err?.message ?? String(err);
          logger.warn(`[agentrux] agentrux_deliver publish failed: ${msg}`);
          return {
            content: [{
              type: "text",
              text: `agentrux_deliver failed: ${msg}`,
            }],
          };
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
        "Connect to AgenTrux with a one-time activation code (act_...). " +
        "Returns the OAuth client_id / client_secret pair issued for this Script. " +
        "Either this tool or `agentrux_setup_via_device_code` (RFC 8628 device code) is " +
        "available for initial setup; the operator chooses based on context.",
      parameters: {
        type: "object",
        properties: {
          activation_code: { type: "string", description: "One-time activation code (act_...)" },
          base_url: { type: "string", description: "AgenTrux API URL (default: https://api.agentrux.com)" },
        },
        required: ["activation_code"],
      },
      async execute(_id: string, params: { activation_code: string; base_url?: string }) {
        const baseUrl = params.base_url || "https://api.agentrux.com";
        // Phase 1.9+ endpoint: POST /auth/redeem-activation-code body {code}.
        const r = await httpJson("POST", `${baseUrl}/auth/redeem-activation-code`, {
          code: params.activation_code,
        });
        if (r.status !== 200 || !r.data?.client_id || !r.data?.client_secret) {
          return { content: [{ type: "text", text: `Activation failed (${r.status}): ${JSON.stringify(r.data)}` }] };
        }
        const creds = {
          base_url: baseUrl,
          client_id: String(r.data.client_id),
          client_secret: String(r.data.client_secret),
          script_id: r.data.script_id ? String(r.data.script_id) : undefined,
        };
        saveCredentials(creds);
        credentials = creds;
        // /auth/redeem-activation-code does not return grants; capabilities
        // live on the JWT scope claim from /oauth/token. Surface the IDs
        // so the user can verify the binding from Console.
        const scriptLine = creds.script_id ? `\nScript ID: ${creds.script_id}` : "";
        return {
          content: [{ type: "text", text: `Connected to AgenTrux!${scriptLine}\nClient ID: ${creds.client_id}` }],
        };
      },
    },
    { optional: true },
  );

  // Plain Device Code (RFC 8628、 RAR なし) で初回 setup を行う tool。
  // SSOT: docs/04_design/auth/device_code_setup_v1.md §4-1
  // 1) POST /oauth/register で DCR (public client)
  // 2) setupViaDeviceCode() を呼んで device_code を発行
  //    → onUserCode で user_code + verification_uri を logger.info に出す
  // 3) polling 完了で {access_token, refresh_token, scope, ...} を取得
  // 4) device_credentials.json に保存 (既存 credentials.json client_secret 経路と並列)
  //
  // 注: v1 では本 tool が device credential を保存するのみで、 plugin runtime の
  // token refresh への統合は別 sub-step (spec §4-1)。 既存 activation_code 経路 user は
  // 影響を受けない。
  // Topology Request Flow v1 (script-initiated、 1-step auth + topology setup)。
  // SSOT: docs/04_design/auth/topology_request_v1.md
  // **推奨 default**: plugin が topology を declare → Console picker で user が approve →
  // 1 TX で Script + Topics + Grants 作成 + token 発行。 plain device code (option) より
  // user UX が seamless (browser auto-open + topic picker 統合)。
  api.registerTool(
    {
      name: "agentrux_install_topology",
      description:
        "Set up AgenTrux via Topology Request Flow v1 (recommended default). " +
        "Declares Script + Topics + Grants upfront, opens Console picker in user's " +
        "browser automatically, user approves → all resources created + token issued " +
        "in 1 step. Use this as the primary setup path for new agents.",
      parameters: {
        type: "object",
        properties: {
          base_url: {
            type: "string",
            description: "AgenTrux API URL (default: https://api.agentrux.com)",
          },
          client_name: {
            type: "string",
            description: "DCR client_name (defaults to 'openclaw-<hostname>-<timestamp>')",
          },
          script_name: {
            type: "string",
            description: "Name of the Script to create (e.g. 'weather-bot')",
          },
          description: {
            type: "string",
            description: "Script description shown to operator in picker (≤256 chars)",
          },
          topics: {
            type: "array",
            description:
              "Topic declarations. Each item: {ref, name, retention_s?, intent?, grants: [{scope, binding_name?}]}",
            items: {
              type: "object",
              properties: {
                ref: { type: "string" },
                name: { type: "string" },
                retention_s: { type: "number" },
                intent: { type: "string" },
                grants: {
                  type: "array",
                  items: {
                    type: "object",
                    properties: {
                      scope: { type: "string", enum: ["read", "write"] },
                      binding_name: { type: "string" },
                    },
                    required: ["scope"],
                  },
                },
              },
              required: ["ref", "name", "grants"],
            },
          },
        },
        required: ["script_name", "topics"],
      },
      async execute(
        _id: string,
        params: {
          base_url?: string;
          client_name?: string;
          script_name: string;
          description?: string;
          topics: Array<{
            ref: string;
            name: string;
            retention_s?: number;
            intent?: string;
            grants: Array<{ scope: "read" | "write"; binding_name?: string }>;
          }>;
        },
      ) {
        const baseUrl = params.base_url || "https://api.agentrux.com";
        const clientName =
          params.client_name ||
          `openclaw-${process.env.HOSTNAME || "host"}-${Date.now()}`;

        // 1) DCR
        const dcrResp = await httpJson("POST", `${baseUrl}/oauth/register`, {
          client_name: clientName,
          token_endpoint_auth_method: "none",
        });
        if (dcrResp.status !== 201 || !dcrResp.data?.client_id) {
          return {
            content: [
              {
                type: "text",
                text: `DCR failed (${dcrResp.status}): ${JSON.stringify(dcrResp.data)}`,
              },
            ],
          };
        }
        const dcrClientId = String(dcrResp.data.client_id);

        // 2) Build TopologyDeclaration
        const declaration: TopologyDeclaration = {
          script_name: params.script_name,
          description: params.description || "(no description)",
          topics: params.topics.map((t) => ({
            ref: t.ref,
            name: t.name,
            retention_s: t.retention_s ?? 86400,
            intent: t.intent ?? null,
          })),
          grants: params.topics.flatMap((t) =>
            t.grants.map((g) => ({
              topic_ref: t.ref,
              scope: g.scope,
              binding_name: g.binding_name ?? `${t.ref}-${g.scope}`,
            })),
          ),
        };

        // 3) installTopology + browser auto-open
        try {
          const result: InstallResult = await installTopology({
            baseUrl,
            clientId: dcrClientId,
            declaration,
            onUserCode: (info) => {
              logger.info(
                `[agentrux] Console picker URL: ${info.verificationUriComplete}`,
              );
              logger.info(
                `[agentrux]   user_code: ${info.userCode} (expires in ${info.expiresIn}s)`,
              );
              // Browser auto-open (Node child_process)
              try {
                const { spawn } = require("node:child_process");
                const opener =
                  process.platform === "darwin"
                    ? "open"
                    : process.platform === "win32"
                      ? "start"
                      : "xdg-open";
                spawn(opener, [info.verificationUriComplete], {
                  detached: true,
                  stdio: "ignore",
                }).unref();
                logger.info("[agentrux] Opening browser automatically...");
              } catch (e: any) {
                logger.warn(
                  `[agentrux] browser auto-open failed (${e?.message ?? e}); open the URL manually`,
                );
              }
            },
          });
          // Persist as DeviceCredentials (reusing existing storage shape).
          const creds: DeviceCredentials = {
            base_url: baseUrl,
            dcr_client_id: dcrClientId,
            access_token: result.accessToken,
            refresh_token: result.refreshToken,
            issued_at_unix: Math.floor(result.grantedAtMs / 1000),
            expires_in: result.expiresIn,
            scope: result.scope,
          };
          saveDeviceCredentials(creds);

          const topicLines = Object.entries(result.topicIdMap)
            .map(([ref, id]) => `  ${ref} → ${id}`)
            .join("\n");
          return {
            content: [
              {
                type: "text",
                text:
                  `AgenTrux Topology install complete!\n` +
                  `client_id: ${dcrClientId}\n` +
                  `script_id: ${result.scriptId}\n` +
                  `alias_id: ${result.aliasId}\n` +
                  `topic_id_map:\n${topicLines}\n` +
                  `grants: ${result.grants.length}\n` +
                  `Saved to: ~/.agentrux/device_credentials.json`,
              },
            ],
          };
        } catch (err: any) {
          const errName = err?.name || "Error";
          const msg = err?.message || String(err);
          return {
            content: [
              {
                type: "text",
                text: `Topology install failed (${errName}): ${msg}`,
              },
            ],
          };
        }
      },
    },
    { optional: true },
  );

  api.registerTool(
    {
      name: "agentrux_setup_via_device_code",
      description:
        "(Option, not default) Plain RFC 8628 device code flow without RAR — credential-only " +
        "setup. After this, the user must manually create topics/grants in AgenTrux Console. " +
        "Prefer `agentrux_install_topology` (default recommended) when you know what topics/grants " +
        "the script needs, since it sets up auth + topology in one step.",
      parameters: {
        type: "object",
        properties: {
          base_url: {
            type: "string",
            description: "AgenTrux API URL (default: https://api.agentrux.com)",
          },
          client_name: {
            type: "string",
            description: "DCR client_name (defaults to 'openclaw-<hostname>-<timestamp>')",
          },
          scope: {
            type: "array",
            items: { type: "string" },
            description:
              "Scope vocabulary (default: ['topic.read', 'topic.write']). " +
              "Allowed values: topic.read, topic.write, openid, email, profile.",
          },
          timeout_seconds: {
            type: "number",
            description: "Total deadline seconds in [60, 600] (default 600 = RFC 8628 TTL)",
          },
        },
        required: [],
      },
      async execute(
        _id: string,
        params: {
          base_url?: string;
          client_name?: string;
          scope?: string[];
          timeout_seconds?: number;
        },
      ) {
        const baseUrl = params.base_url || "https://api.agentrux.com";
        const clientName =
          params.client_name ||
          `openclaw-${process.env.HOSTNAME || "host"}-${Date.now()}`;
        const scope = params.scope || ["topic.read", "topic.write"];

        // 1) DCR — POST /oauth/register で public client を作る
        const dcrResp = await httpJson("POST", `${baseUrl}/oauth/register`, {
          client_name: clientName,
          token_endpoint_auth_method: "none",
        });
        if (dcrResp.status !== 201 || !dcrResp.data?.client_id) {
          return {
            content: [
              {
                type: "text",
                text: `DCR failed (${dcrResp.status}): ${JSON.stringify(dcrResp.data)}`,
              },
            ],
          };
        }
        const dcrClientId = String(dcrResp.data.client_id);

        // 2) setupViaDeviceCode を呼んで device_code 発行 + polling
        try {
          const result = await setupViaDeviceCode({
            baseUrl,
            clientId: dcrClientId,
            scope,
            timeoutSeconds: params.timeout_seconds,
            onUserCode: (info) => {
              // operator に user_code を chat 出力 (logger.info は chat console 経由)
              logger.info(
                `[agentrux] Please open ${info.verificationUriComplete} ` +
                  `and enter user_code: ${info.userCode} (expires in ${info.expiresIn}s)`,
              );
            },
          });

          // 3) device_credentials.json に保存
          const creds: DeviceCredentials = {
            base_url: baseUrl,
            dcr_client_id: dcrClientId,
            access_token: result.accessToken,
            refresh_token: result.refreshToken,
            issued_at_unix: Math.floor(result.grantedAtMs / 1000),
            expires_in: result.expiresIn,
            scope: result.scope,
            id_token: result.idToken,
          };
          saveDeviceCredentials(creds);

          return {
            content: [
              {
                type: "text",
                text:
                  `AgenTrux device code setup complete!\n` +
                  `client_id: ${dcrClientId}\n` +
                  `scope: ${result.scope.join(" ")}\n` +
                  `Saved to: ~/.agentrux/device_credentials.json\n` +
                  `Note: plugin runtime token refresh integration is a separate sub-step. ` +
                  `For now, you may need to restart the plugin or configure it to use the ` +
                  `new device_credentials.json path.`,
              },
            ],
          };
        } catch (err: any) {
          const errName = err?.name || "Error";
          const msg = err?.message || String(err);
          return {
            content: [
              {
                type: "text",
                text: `Device code setup failed (${errName}): ${msg}`,
              },
            ],
          };
        }
      },
    },
    { optional: true },
  );

  // Data plane tools — available for non-ingress sessions (cron,
  // heartbeat, subagent, CLI). Blocked during ingress turns UNLESS
  // the target topic is a configured messaging topic (loosely-coupled
  // messaging that is independent of the command/result pair).

  const resolvedAccount = resolveAccountFromPluginConfig(pluginConfig);
  // Build lookup sets for messaging topic access control.
  // Normalize via ensureTopPrefix so set membership matches regardless of
  // whether the config / agent supplied the `top_<uuid>` or bare UUID form.
  const messagingWritable = new Set<string>(); // topicIds the agent may write to during ingress
  const messagingReadable = new Set<string>(); // topicIds the agent may read from during ingress
  for (const mt of resolvedAccount.messagingTopics) {
    if (mt.mode === "write" || mt.mode === "readwrite") messagingWritable.add(ensureTopPrefix(mt.topicId));
    if (mt.mode === "read" || mt.mode === "readwrite") messagingReadable.add(ensureTopPrefix(mt.topicId));
  }

  // Resolve a logical topic name to its UUID. Returns the input unchanged
  // if it's already a UUID or not a known logical name.
  const messagingTopicByName = new Map<string, MessagingTopic>();
  for (const mt of resolvedAccount.messagingTopics) {
    messagingTopicByName.set(mt.id, mt);
  }

  // Always returns a `top_<uuid>` form so callers (.has() lookup, HTTP path)
  // compare against a single normalized shape.
  function resolveTopicParam(params: { topic?: string; topic_id?: string }): string {
    let raw: string;
    if (params.topic && typeof params.topic === "string") {
      const mt = messagingTopicByName.get(params.topic);
      if (mt) raw = mt.topicId;
      else raw = params.topic;  // unknown logical name — treat as a UUID the user typed
    } else {
      raw = params.topic_id ?? "";
    }
    return raw ? ensureTopPrefix(raw) : "";
  }

  api.registerTool({
    name: "agentrux_publish",
    description:
      "Publish an event to an AgenTrux topic. " +
      "Use the 'topic' parameter with a logical name (e.g. 'sensor-out') " +
      "for configured messaging topics, or 'topic_id' with a UUID. " +
      "During ingress processing, only messaging topics are available " +
      "(the command/result reply is published automatically).",
    parameters: {
      type: "object",
      properties: {
        topic: { type: "string", description: "Messaging topic logical name (e.g. 'sensor-out'). Preferred over topic_id." },
        topic_id: { type: "string", description: "Topic UUID (used if topic name is not provided)" },
        event_type: { type: "string", description: "Event type (e.g. 'message.send')" },
        payload: { type: "object", description: "JSON payload" },
        correlation_id: { type: "string", description: "Optional correlation ID" },
        reply_topic: { type: "string", description: "Optional reply topic UUID" },
      },
      required: ["event_type", "payload"],
    },
    async execute(_id: string, params: any) {
      const topicId = resolveTopicParam(params);
      if (!topicId) {
        return { content: [{ type: "text", text: "Either 'topic' (logical name) or 'topic_id' (UUID) is required." }] };
      }
      if (getActiveRequest() !== null && !messagingWritable.has(topicId)) {
        return { content: [{ type: "text", text: "Not available during ingress for this topic — your reply is published automatically. Only configured messaging topics are allowed." }] };
      }
      const creds = getCredentials();
      // Phase 2.2 SSOT: body は {event_type, payload, metadata?, payload_object_id?}。
      // 旧 root field (type / correlation_id / reply_topic) は廃止、 metadata に折りたたむ。
      const body: any = { event_type: params.event_type, payload: params.payload };
      const meta: Record<string, unknown> = {};
      if (params.correlation_id) meta.correlation_id = params.correlation_id;
      if (params.reply_topic) meta.reply_topic = params.reply_topic;
      if (Object.keys(meta).length > 0) body.metadata = meta;
      const result = await authRequest(creds, "POST", `/topics/${ensureTopPrefix(topicId)}/events`, body);
      return { content: [{ type: "text", text: `Event published to "${params.topic || topicId}" (event_id: ${result.event_id})` }] };
    },
  });

  api.registerTool({
    name: "agentrux_read",
    description:
      "Read events from an AgenTrux topic. " +
      "Use the 'topic' parameter with a logical name (e.g. 'sensor-in') " +
      "for configured messaging topics, or 'topic_id' with a UUID. " +
      "During ingress processing, only messaging topics are available.",
    parameters: {
      type: "object",
      properties: {
        topic: { type: "string", description: "Messaging topic logical name (e.g. 'sensor-in'). Preferred over topic_id." },
        topic_id: { type: "string", description: "Topic UUID (used if topic name is not provided)" },
        limit: { type: "number", description: "Max events to return (default 10)" },
        event_type: { type: "string", description: "Filter by event type" },
      },
    },
    async execute(_id: string, params: any) {
      const topicId = resolveTopicParam(params);
      if (!topicId) {
        return { content: [{ type: "text", text: "Either 'topic' (logical name) or 'topic_id' (UUID) is required." }] };
      }
      if (getActiveRequest() !== null && !messagingReadable.has(topicId)) {
        return { content: [{ type: "text", text: "Not available during ingress for this topic. Only configured messaging topics are allowed." }] };
      }
      const creds = getCredentials();
      const query = new URLSearchParams();
      query.set("limit", String(params.limit || 10));
      query.set("order", "desc");
      if (params.event_type) query.set("type", params.event_type);
      const result = await authRequest(creds, "GET", `/topics/${ensureTopPrefix(topicId)}/events?${query}`);
      // Phase 2.5a SSOT: response.events (旧 items は廃止)、 event.{event_id, sequence_number, event_type, payload, metadata?}
      const events = result.events || [];
      if (events.length === 0) return { content: [{ type: "text", text: `No events found on "${params.topic || topicId}".` }] };
      const lines = events.map((e: any) =>
        `[seq:${e.sequence_number}] ${e.event_type} — ${JSON.stringify(e.payload)} (${e.metadata?.correlation_id || "-"})`,
      );
      return { content: [{ type: "text", text: `${events.length} events on "${params.topic || topicId}":\n${lines.join("\n")}` }] };
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
      // Phase 2.2 SSOT: correlation_id / reply_topic は metadata に折りたたむ
      await authRequest(creds, "POST", `/topics/${ensureTopPrefix(params.topic_id)}/events`, {
        event_type: "message.request",
        payload: { text: params.message },
        metadata: { correlation_id: corrId, reply_topic: params.reply_topic },
      });
      const start = Date.now();
      while (Date.now() - start < timeout) {
        const result = await authRequest(creds, "GET", `/topics/${ensureTopPrefix(params.reply_topic)}/events?limit=20&order=desc`);
        // Phase 2.5a SSOT: response.events、 metadata.correlation_id で照合
        for (const e of result.events || []) {
          if (e.metadata?.correlation_id === corrId) {
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
      description:
        "(Disabled in current server release.) Redeem an invite code for " +
        "cross-account topic access. In Phase 1.9+ this operation requires " +
        "a Console session and cannot be performed from a Script credential.",
      parameters: {
        type: "object",
        properties: { invite_code: { type: "string", description: "Invite code (inv_...)" } },
        required: ["invite_code"],
      },
      async execute(_id: string, _params: { invite_code: string }) {
        // /auth/redeem-invite-code requires ConsoleSessionDep (user-authenticated
        // browser session) on the current server, not a Script Bearer token.
        // The Script-level path that this tool used to take is gone.
        return {
          content: [
            {
              type: "text",
              text:
                "Cross-account grant redemption is performed from the AgenTrux " +
                "Console (signed-in user session) in the current server release. " +
                "Open Console → Aliases → Grants and accept the invite there.",
            },
          ],
        };
      },
    },
    { optional: true },
  );

  }, // end register()
};

export default plugin;

// Topology Request Flow v1 install helper を public API として再 export。
// SSOT: docs/04_design/auth/topology_request_v1.md
export {
  InstallAbortedError,
  InstallAuthError,
  InstallConfigError,
  InstallDeniedError,
  InstallError,
  InstallTimeoutError,
  buildAuthorizationDetails,
  installTopology,
  validateDeclaration,
} from "./topology-install";
export type {
  GrantScope,
  InstallPendingInfo,
  InstallResult,
  InstallResultGrant,
  InstallTopologyOptions,
  OnUserCode,
  TopologyDeclaration,
  TopologyGrantSpec,
  TopologyTopicSpec,
} from "./topology-install";

// Plain Device Code Setup (RFC 8628、 RAR なし) を public API として再 export。
// SSOT: docs/04_design/auth/device_code_setup_v1.md
export { setupViaDeviceCode } from "./device-code-setup";
export type {
  DeviceCodeSetupPending,
  DeviceCodeSetupResult,
  SetupViaDeviceCodeOptions,
} from "./device-code-setup";
