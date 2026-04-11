/**
 * AgenTrux Channel Gateway v8 — SSE hint + Pull drain + SDK dispatch.
 *
 * Follows the openclaw-nostr ChannelPlugin pattern:
 *   - PluginRuntime from register() (NOT ctx.runtime)
 *   - resolveAgentRoute → finalizeInboundContext → recordInboundSession
 *     → dispatchReplyWithBufferedBlockDispatcher
 *   - startAccount is called by Gateway (not self-started)
 *
 * Transport layer (SSE/Pull/waterline/credentials) carried over from v7.
 */

import {
  type Credentials,
  loadCredentials,
  saveCredentials,
  AGENTRUX_DIR,
  WATERLINE_PATH,
} from "./credentials";
import { httpJson, pullEvents, ensureToken, invalidateToken, authRequest } from "./http-client";
import { consumeBootstrapFile, getBootstrapPath, TransientActivationError } from "./activation-core";
import { wrapMessage } from "./sanitize";
import { getPluginRuntime } from "./runtime";
import {
  setActiveRequest,
  clearActiveRequest,
  publishOutboundPayload,
  getSharedPluginConfig,
} from "./index";
import * as https from "https";
import * as http from "http";
import * as fs from "fs";
import * as path from "path";
import * as os from "os";
// Inbound image handling flows through the OpenClaw dispatch pipeline's
// `replyOptions.images` field. pi-ai translates the Claude-style
// ImageContent into the provider-specific vision block (Claude image
// block, OpenAI image_url, Gemini inlineData) automatically.
interface ImageContent {
  type: "image";
  data: string;       // base64-encoded bytes, no `data:` prefix
  mimeType: string;
}

// ---------------------------------------------------------------------------
// Persistent waterline
// ---------------------------------------------------------------------------
//
// Path constants come from ./credentials — see the comment at the top of
// credentials.ts for why they live there and not next to the code that
// uses them.

const WATERLINE_DIR = AGENTRUX_DIR;

function loadWaterlineMap(): Record<string, number> {
  try {
    if (fs.existsSync(WATERLINE_PATH)) {
      const data = JSON.parse(fs.readFileSync(WATERLINE_PATH, "utf-8"));
      if (typeof data.waterline === "number") return {};
      if (typeof data === "object" && data !== null) return data;
    }
  } catch {}
  return {};
}

function loadWaterline(topicId: string): number | null {
  const map = loadWaterlineMap();
  return typeof map[topicId] === "number" ? map[topicId] : null;
}

function saveWaterline(topicId: string, waterline: number): void {
  try {
    if (!fs.existsSync(WATERLINE_DIR)) {
      fs.mkdirSync(WATERLINE_DIR, { recursive: true, mode: 0o700 });
    }
    const map = loadWaterlineMap();
    map[topicId] = waterline;
    const tmp = WATERLINE_PATH + ".tmp";
    fs.writeFileSync(tmp, JSON.stringify(map), { mode: 0o600 });
    fs.renameSync(tmp, WATERLINE_PATH);
  } catch {}
}

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface AgenTruxAccount {
  commandTopicId: string;
  resultTopicId: string;
  agentId: string;
  baseUrl: string;
  pollIntervalMs: number;
  maxConcurrency: number;
  subagentTimeoutMs: number;
  execPolicy: { enabled: boolean; allowedCommands: string[] };
}

interface AgenTruxAttachment {
  name: string;
  object_id: string;
  content_type: string;
  download_url?: string;
}

interface AgenTruxEvent {
  event_id: string;
  sequence_no: number;
  type: string;
  payload: {
    request_id?: string;
    conversation_key?: string;
    message?: string;
    text?: string;
    attachments?: AgenTruxAttachment[];
  };
}

const TEXT_CONTENT_TYPES = /^(text\/|application\/json|application\/xml|application\/javascript|application\/typescript)/;
// audio/video content types are detected so we can hold them in a
// separate branch: v0.9.0 only runs describeImageFile on image/*,
// audio and video currently fall through to the URL-reference path
// until transcribeAudioFile and describeVideoFile are wired in.
const MEDIA_CONTENT_TYPES = /^(image|audio|video)\//;
const MAX_INLINE_SIZE = 50 * 1024;

// MIME mapping used by the outbound publisher lives in index.ts — see
// OUTBOUND_MIME_BY_EXT there. The gateway does not need its own table
// anymore since resolveSandboxUrls was removed in v0.13.0.

// ---------------------------------------------------------------------------
// ChannelPlugin gateway — startAccount (called by OpenClaw Gateway)
// ---------------------------------------------------------------------------

export const agentruxGateway = {
  startAccount: async (ctx: any): Promise<void> => {
    const account = ctx.account as AgenTruxAccount;
    const accountId: string = ctx.accountId ?? "default";
    const abortSignal: AbortSignal = ctx.abortSignal;
    const log = ctx.log;

    // --- SDK functions ---
    const pluginRuntime = getPluginRuntime();
    log?.info?.(`[agentrux] ctx keys=${JSON.stringify(Object.keys(ctx))} channelRuntime=${!!ctx.channelRuntime}`);
    // Prefer ctx.channelRuntime (gateway-bound, has allowGatewaySubagentBinding)
    const channelRT = ctx.channelRuntime ?? pluginRuntime.channel;
    const { loadConfig } = pluginRuntime.config;
    const { resolveAgentRoute } = channelRT.routing;
    const {
      finalizeInboundContext,
      dispatchReplyWithBufferedBlockDispatcher,
    } = channelRT.reply;
    const { recordInboundSession, resolveStorePath } = channelRT.session;

    // 1. Credentials: load existing, OR consume a one-shot BOOTSTRAP.md.
    //
    //    Activation flow (mirrors OpenClaw's own ~/.openclaw/workspace/
    //    BOOTSTRAP.md ritual — see https://docs.openclaw.ai/start/bootstrapping):
    //
    //      a. If ~/.agentrux/credentials.json exists, load it. Done.
    //      b. Else if ~/.agentrux/BOOTSTRAP.md exists, read its activation
    //         code, exchange it for credentials by calling /auth/activate
    //         exactly once, write credentials.json atomically, and DELETE
    //         the bootstrap file so the ritual never runs twice.
    //      c. Else (no creds, no bootstrap file): channel disabled.
    //
    //    Why this design (vs. an activationCode field in openclaw.json):
    //
    //    - openclaw.json is a permanent config file, but the activation
    //      code is single-use and time-limited. Holding it in config means
    //      it gets quoted/copied/cached and survives long after it has been
    //      consumed.
    //    - OpenClaw's auto-restart loop (10 attempts, exponential backoff)
    //      treats any "channel exited" event as cause for retry. A 4xx from
    //      /auth/activate is permanent, but a config-driven activation has
    //      no way to mark "this code is dead" — it would burn the rate
    //      limit forever. The BOOTSTRAP.md pattern handles this by renaming
    //      the file to BOOTSTRAP.md.failed-<ts> on a 4xx, so the next loop
    //      iteration sees no file and goes quiet.
    //    - For 5xx / network failures the file is left untouched and we
    //      THROW so the auto-restart loop kicks in and retries — that is
    //      the correct behavior for transient errors.
    //
    //    The contract is pinned in src/__tests__/bootstrap.test.ts.
    invalidateToken();
    let creds = loadCredentials();
    if (!creds) {
      const baseUrl = account.baseUrl || "https://api.agentrux.com";
      let bootstrap;
      try {
        bootstrap = await consumeBootstrapFile({ baseUrl });
      } catch (err) {
        if (err instanceof TransientActivationError) {
          // Re-throw so OpenClaw treats this as a crash and the auto-restart
          // loop retries. The BOOTSTRAP.md file is still in place so the
          // next attempt can succeed.
          log?.error?.(`[agentrux] ${err.message}`);
          throw err;
        }
        throw err;
      }

      if (bootstrap.kind === "ok") {
        log?.info?.(
          `[agentrux] Activated via ${getBootstrapPath()}: script_id=${bootstrap.credentials.script_id}`,
        );
        creds = bootstrap.credentials;
      } else if (bootstrap.kind === "permanent-failure") {
        log?.error?.(
          `[agentrux] BOOTSTRAP.md activation rejected (HTTP ${bootstrap.httpStatus} ${bootstrap.errorCode}): ${bootstrap.errorMessage}`,
        );
        log?.error?.(
          `[agentrux] Quarantined to ${bootstrap.failedFilePath}. Issue a new activation code and write it to ${getBootstrapPath()}.`,
        );
        return;
      } else if (bootstrap.kind === "validation-failure") {
        log?.error?.(
          `[agentrux] BOOTSTRAP.md is malformed: ${bootstrap.reason}. Quarantined to ${bootstrap.failedFilePath}.`,
        );
        return;
      } else if (bootstrap.kind === "creds-already-present") {
        // This branch should be impossible because we are in the
        // `if (!creds)` block, but keep it explicit so the type checker
        // is happy and so a future refactor cannot lose the distinction.
        log?.warn?.(
          `[agentrux] BOOTSTRAP.md found alongside existing credentials.json — quarantined to ${bootstrap.failedFilePath} without calling /auth/activate (would burn the single-use code).`,
        );
        return;
      } else {
        // bootstrap.kind === "no-file"
        log?.warn?.(
          "[agentrux] No credentials at ~/.agentrux/credentials.json — channel disabled.",
        );
        log?.warn?.(
          `[agentrux] To activate: write your one-time activation code to ${getBootstrapPath()} and restart the gateway.`,
        );
        return;
      }
    }

    // 2. Waterline
    const topicId = account.commandTopicId;
    const processedEvents = new Set<string>();
    let reconnectAttempts = 0;

    const saved = loadWaterline(topicId);
    let waterline: number;
    if (saved !== null) {
      waterline = saved;
      log?.info?.(`[agentrux] Resuming from saved waterline=${waterline} topic=${topicId}`);
    } else {
      waterline = 0;
      try {
        let cursor = 0;
        while (true) {
          const batch = await pullEvents(creds, account.commandTopicId, cursor, 50);
          if (batch.length === 0) break;
          cursor = batch[batch.length - 1].sequence_no;
        }
        waterline = cursor;
        saveWaterline(topicId, waterline);
        log?.info?.(`[agentrux] First startup: skipped to waterline=${waterline}`);
      } catch (err: any) {
        log?.warn?.(`[agentrux] Failed to fetch initial waterline: ${err?.message}. Starting from 0.`);
      }
    }

    // 3. Pull-based event drain
    let drainRunning = false;
    const drainEvents = async (): Promise<void> => {
      if (drainRunning) return;
      drainRunning = true;
      try {
        while (!abortSignal.aborted) {
          const batch = await pullEvents(creds!, account.commandTopicId, waterline);
          if (batch.length === 0) break;
          for (const event of batch as AgenTruxEvent[]) {
            if (abortSignal.aborted) break;
            await processEvent(event);
          }
        }
      } finally {
        drainRunning = false;
      }
    };

    // 4. Process a single event — SDK dispatch pattern
    const retryCount = new Map<string, number>();
    const MAX_RETRIES = 3;

    const processEvent = async (event: AgenTruxEvent): Promise<void> => {
      if (event.sequence_no <= waterline) return;
      if (processedEvents.has(event.event_id)) return;

      const payload = event.payload;
      if (!payload?.message && !payload?.text) {
        advanceWaterline(event);
        return;
      }

      const rawMessage = payload.message ?? payload.text ?? "";
      const conversationKey = payload.conversation_key ?? "default";
      const requestId = payload.request_id ?? event.event_id;

      log?.info?.(`[agentrux] Processing event ${event.event_id} seq=${event.sequence_no} req=${requestId}`);

      // --- SDK dispatch (following openclaw-nostr pattern) ---
      const cfg = loadConfig();
      const agentruxTo = `agentrux:topic:${account.resultTopicId}`;

      // Resolve inbound attachments. Images are fetched, base64-encoded,
      // and collected into `resolved.images` for the multimodal path
      // below — we never inline them into the text body. Text files and
      // other binaries flow into `resolved.textBlock`.
      const resolved = await resolveAttachments(payload.attachments ?? [], creds!, topicId, log);
      const combinedRaw = rawMessage + resolved.textBlock;
      const message = wrapMessage(combinedRaw);
      const hasImages = resolved.images.length > 0;

      // Stash the per-event identity so the outbound `sendPayload`
      // adapter in index.ts can stamp the published openclaw.response
      // with the correct request_id / conversation_key and route it to
      // the correct result topic. Cleared in the `finally` below.
      setActiveRequest({
        requestId,
        conversationKey,
        resultTopicId: account.resultTopicId,
      });

      const route = resolveAgentRoute({
        cfg,
        channel: "agentrux",
        accountId,
        peer: { kind: "direct", id: conversationKey },
      });

      // Dispatch to agent. Both text-only and image-bearing turns use
      // the same OpenClaw block reply pipeline
      // (`dispatchReplyWithBufferedBlockDispatcher`):
      //
      //   - Inbound image bytes are passed via `replyOptions.images`,
      //     which `GetReplyOptions` exposes on the dispatch pipeline.
      //     pi-ai translates ImageContent into the provider-specific
      //     vision block so the primary model sees the bytes directly.
      //
      //   - Tool-produced output paths (image_generate results, etc.)
      //     flow via the dispatch pipeline's internal
      //     `consumePendingToolMediaIntoReply`, which merges
      //     `state.pendingToolMediaUrls` into each assistant block
      //     reply's `payload.mediaUrls`. Our `deliver` callback below
      //     receives those and publishes via `publishOutboundPayload`.
      //
      // The earlier split between "multimodal path via
      // agentCommandFromIngress" and "text path via
      // dispatchReplyWithBufferedBlockDispatcher" caused image-output
      // loss in multi-tool turns: embedded run payloads
      // (buildEmbeddedRunPayloads) only parse MEDIA:<path> directives
      // from assistant text, they do NOT consume
      // pendingToolMediaUrls. Unifying on the block pipeline fixes
      // that.
      const inboundCtx = finalizeInboundContext({
        Body: message,
        BodyForAgent: message,
        RawBody: combinedRaw,
        CommandBody: combinedRaw,
        BodyForCommands: combinedRaw,
        From: `agentrux:${conversationKey}`,
        To: agentruxTo,
        SessionKey: route.sessionKey,
        AccountId: route.accountId,
        ChatType: "direct",
        ConversationLabel: `AgenTrux/${conversationKey}`,
        Provider: "agentrux",
        Surface: "agentrux",
        SenderId: conversationKey,
        SenderName: conversationKey,
        MessageSid: event.event_id,
        Timestamp: Date.now(),
        CommandAuthorized: true,
        OriginatingChannel: "agentrux",
        OriginatingTo: agentruxTo,
      });

      const storePath = resolveStorePath(undefined, { agentId: route.agentId });
      await recordInboundSession({
        storePath,
        sessionKey: route.sessionKey,
        ctx: inboundCtx,
        updateLastRoute: {
          sessionKey: route.sessionKey,
          channel: "agentrux",
          to: agentruxTo,
          accountId: route.accountId,
        },
        onRecordError: (err: unknown) => {
          log?.warn?.(`[agentrux] Failed updating session meta: ${String(err)}`);
        },
      });

      if (hasImages) {
        log?.info?.(
          `[agentrux] Dispatching turn with ${resolved.images.length} image(s) for req=${requestId}`,
        );
      }

      let turnCompleted = false;
      try {
        await dispatchReplyWithBufferedBlockDispatcher({
          ctx: inboundCtx,
          cfg,
          // Pass inbound images to the agent via replyOptions.images.
          // The block reply pipeline forwards this into pi-ai as the
          // vision content block on the user message.
          replyOptions: hasImages ? { images: resolved.images } : undefined,
          dispatcherOptions: {
            // Publish each block reply from THIS (gateway) process,
            // where `activeRequest` is populated. A custom `deliver`
            // is required because Sub-process plugin instances have a
            // different activeRequestStack / sharedPluginConfig and
            // would silently drop the publish.
            deliver: async (replyPayload: any) => {
              await publishOutboundPayload(
                {
                  text: replyPayload?.text,
                  mediaUrl: replyPayload?.mediaUrl,
                  mediaUrls: replyPayload?.mediaUrls,
                },
                getSharedPluginConfig() ?? {
                  commandTopicId: account.commandTopicId,
                  resultTopicId: account.resultTopicId,
                  agentId: account.agentId,
                },
                log ?? { info: () => {}, warn: () => {}, error: () => {} },
              );
            },
          },
        });
        turnCompleted = true;
      } catch (err: any) {
        const attempts = (retryCount.get(event.event_id) ?? 0) + 1;
        retryCount.set(event.event_id, attempts);
        if (attempts >= MAX_RETRIES) {
          log?.error?.(`[agentrux] dispatch failed ${MAX_RETRIES} times for ${event.event_id}, skipping: ${err?.message ?? err}`);
          retryCount.delete(event.event_id);
          // Advance waterline to prevent infinite retry loop
          advanceWaterline(event);
        } else {
          log?.warn?.(`[agentrux] dispatch failed for ${event.event_id} (attempt ${attempts}/${MAX_RETRIES}): ${err?.message ?? err}`);
          // Don't advance — will retry on next drain
        }
      }

      // Cleanup
      clearActiveRequest(requestId);
      if (turnCompleted) {
        advanceWaterline(event);
      }
    };

    const advanceWaterline = (event: AgenTruxEvent): void => {
      processedEvents.add(event.event_id);
      if (event.sequence_no > waterline) {
        waterline = event.sequence_no;
        saveWaterline(topicId, waterline);
      }
      if (processedEvents.size > 10_000) {
        const entries = [...processedEvents];
        entries.splice(0, entries.length - 5_000);
        processedEvents.clear();
        entries.forEach(e => processedEvents.add(e));
      }
    };

    // 5. SSE monitoring loop
    const sseLoop = async (): Promise<void> => {
      while (!abortSignal.aborted) {
        try {
          const token = await ensureToken(creds!);
          const url = new URL(`${creds!.base_url}/topics/${account.commandTopicId}/events/stream`);
          if (waterline > 0) url.searchParams.set("after_sequence_no", String(waterline));
          const mod = url.protocol === "https:" ? https : http;

          await new Promise<void>((resolve, reject) => {
            const req = mod.request(url, {
              method: "GET",
              headers: {
                Authorization: `Bearer ${token}`,
                Accept: "text/event-stream",
                "Cache-Control": "no-cache",
              },
            }, (res) => {
              if (res.statusCode === 401) {
                invalidateToken();
                res.resume();
                reject(new Error("SSE auth expired"));
                return;
              }
              if (res.statusCode !== 200) {
                res.resume();
                reject(new Error(`SSE HTTP ${res.statusCode}`));
                return;
              }

              reconnectAttempts = 0;
              log?.info?.("[agentrux] SSE connected");

              let buffer = "";
              res.on("data", (chunk: Buffer) => {
                buffer += chunk.toString();
                const lines = buffer.split("\n");
                buffer = lines.pop() ?? "";

                for (const line of lines) {
                  if (line.startsWith("data: ")) {
                    drainEvents().catch(err =>
                      log?.error?.(`[agentrux] drainEvents error: ${err}`));
                    break;
                  }
                }
              });
              res.on("end", () => resolve());
              res.on("error", reject);
            });

            req.on("error", reject);
            const onAbort = () => { req.destroy(); resolve(); };
            abortSignal.addEventListener("abort", onAbort, { once: true });
            req.on("close", () => abortSignal.removeEventListener("abort", onAbort));
            req.end();
          });
        } catch (err: any) {
          if (abortSignal.aborted) break;
          log?.warn?.(`[agentrux] SSE disconnected: ${err?.message ?? err}. Reconnecting...`);
        }

        if (abortSignal.aborted) break;
        const delay = Math.min(1000 * Math.pow(2, reconnectAttempts), 60_000);
        reconnectAttempts++;
        await sleep(delay, abortSignal);
      }
    };

    // 6. Safety Poller
    const pollerLoop = async (): Promise<void> => {
      await sleep(account.pollIntervalMs, abortSignal);
      while (!abortSignal.aborted) {
        try {
          await drainEvents();
        } catch (err: any) {
          log?.warn?.(`[agentrux] Poller error: ${err?.message ?? err}`);
        }
        await sleep(account.pollIntervalMs, abortSignal);
      }
    };

    // 7. Start
    log?.info?.(`[agentrux] Gateway starting: topic=${account.commandTopicId} agent=${account.agentId}`);
    await Promise.all([sseLoop(), pollerLoop()]);
    log?.info?.("[agentrux] Gateway stopped");
  },
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

interface ResolvedAttachments {
  /** Text to concatenate into the message body: inlined text files and
   *  URL references for non-image binaries (audio/video/pdf/...). */
  textBlock: string;
  /** Images, one entry per inbound `image/*` attachment, base64 encoded
   *  and in the exact shape pi-ai / agentCommandFromIngress expect.
   *  Empty when no image attachments are present. */
  images: ImageContent[];
}

/** Download a URL straight into memory. Follows one HTTP redirect and
 *  returns the concatenated Buffer — no tmp file, no disk I/O. There
 *  is intentionally no size cap: the caller trusts AgenTrux's existing
 *  MinIO upload limits and lets the vision provider enforce its own
 *  cap downstream so an oversized image surfaces as a provider error
 *  instead of being silently dropped mid-stream. */
async function fetchUrlToBuffer(url: string): Promise<Buffer> {
  return await new Promise<Buffer>((resolve, reject) => {
    const mod = url.startsWith("https:") ? https : http;
    const req = mod.get(url, (res) => {
      if (res.statusCode && res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
        res.resume();
        fetchUrlToBuffer(res.headers.location).then(resolve, reject);
        return;
      }
      if (!res.statusCode || res.statusCode >= 400) {
        res.resume();
        reject(new Error(`HTTP ${res.statusCode} fetching attachment`));
        return;
      }
      const chunks: Buffer[] = [];
      res.on("data", (chunk: Buffer) => chunks.push(chunk));
      res.on("end", () => resolve(Buffer.concat(chunks)));
      res.on("error", reject);
    });
    req.on("error", reject);
  });
}

async function resolveAttachments(
  attachments: AgenTruxAttachment[],
  creds: Credentials,
  topicId: string,
  log?: any,
): Promise<ResolvedAttachments> {
  if (attachments.length === 0) return { textBlock: "", images: [] };

  const blocks: string[] = [];
  const images: ImageContent[] = [];

  for (const att of attachments) {
    let downloadUrl = att.download_url;
    if (!downloadUrl && att.object_id) {
      try {
        const payloadInfo = await authRequest(creds, "GET", `/topics/${topicId}/payloads/${att.object_id}`);
        downloadUrl = payloadInfo.download_url;
        log?.info?.(`[agentrux] Resolved download_url for ${att.name} (${att.object_id})`);
      } catch (err: any) {
        log?.warn?.(`[agentrux] Failed to resolve download_url for ${att.name}: ${err?.message}`);
      }
    }

    if (!downloadUrl) {
      blocks.push(`[添付: ${att.name}] (download_url 取得不可)`);
      continue;
    }

    const isText = TEXT_CONTENT_TYPES.test(att.content_type);
    const isImage = /^image\//.test(att.content_type);
    const isOtherMedia = MEDIA_CONTENT_TYPES.test(att.content_type) && !isImage;

    if (isText) {
      // Small text files (HTML, JSON, source code, ...) are inlined into
      // the message body so the agent reads them directly without a tool
      // call. Large ones fall back to a URL reference.
      try {
        const content = await fetchUrl(downloadUrl);
        if (content.length <= MAX_INLINE_SIZE) {
          blocks.push(`[添付: ${att.name}]\n${content}\n[/添付]`);
        } else {
          blocks.push(`[添付: ${att.name}] (${Math.round(content.length / 1024)}KB)\nURL: ${downloadUrl}`);
        }
      } catch (err: any) {
        log?.warn?.(`[agentrux] Failed to fetch attachment ${att.name}: ${err?.message}`);
        blocks.push(`[添付: ${att.name}] URL: ${downloadUrl}`);
      }
    } else if (isImage) {
      // Fetch the signed MinIO bytes, base64-encode them, and hand the
      // result to the agent via agentCommandFromIngress({ images }) in
      // the caller. This is the canonical multimodal input path: pi-ai
      // translates the ImageContent into the provider-specific format
      // (Claude image block, OpenAI image_url data URL, etc.) so the
      // primary agent model sees the bytes as a first-class vision
      // input — no URL leakage, no pre-description, no hardcoded model.
      //
      // IN ADDITION to the vision input, we also stage the bytes to a
      // local workspace file and append the resolved path to the text
      // block. This is needed because the agent may decide to MODIFY
      // the image (flip, crop, resize, re-generate with variation, ...)
      // and the tools that do so (`image_generate`, shell / ffmpeg,
      // Python scripts, ...) take a file path or URL, not a vision
      // content block. Without an explicit local path the agent
      // hallucinates placeholders like "https://your-image-source.com/..."
      // and the tool fails. Surfacing the real path lets the agent feed
      // it straight into whatever editor it picks.
      //
      // The `image` core tool is automatically offered to the agent by
      // OpenClaw when it sees "full" profile; that tool will try to
      // re-fetch the image and frequently hallucinates its base64
      // argument, which produces noisy `[tools] image failed` logs and
      // wastes a provider round trip. Configure the agent with
      // `tools.deny: ["image"]` in ~/.openclaw/openclaw.json to silence
      // it — the multimodal path below is unaffected because it feeds
      // the bytes through agentCommandFromIngress's `images` field, not
      // the core tool. See doc/plugins/agentrux-openclaw-plugin.md.
      try {
        const buffer = await fetchUrlToBuffer(downloadUrl);
        images.push({
          type: "image",
          data: buffer.toString("base64"),
          mimeType: att.content_type,
        });
        log?.info?.(`[agentrux] Staging image ${att.name} (${buffer.length} bytes) as multimodal input`);

        // Persist to a workspace file so image-editing tools have a
        // real path to open. We use the OpenClaw workspace dir when
        // present (same convention stock channels use) and fall back
        // to tmpdir. The filename is prefixed with the topic/payload
        // id so concurrent inbound images don't collide.
        try {
          const workspaceDir = path.join(os.homedir(), ".openclaw", "workspace", "agentrux-inbound");
          fs.mkdirSync(workspaceDir, { recursive: true });
          const safeName = att.name.replace(/[^A-Za-z0-9._-]/g, "_");
          const localPath = path.join(workspaceDir, `${att.object_id || Date.now()}-${safeName}`);
          fs.writeFileSync(localPath, buffer);
          blocks.push(
            `[受信画像: ${att.name}]\n` +
            `ローカルパス: ${localPath}\n` +
            `(画像を編集・変換するツール (image_generate / shell / Python 等) ` +
            `を呼ぶ場合は URL ではなく上記のローカルパスを使ってください。)`,
          );
          log?.info?.(`[agentrux] Persisted inbound image to ${localPath}`);
        } catch (persistErr: any) {
          log?.warn?.(`[agentrux] Failed to persist inbound image ${att.name}: ${persistErr?.message ?? persistErr}`);
        }
      } catch (err: any) {
        log?.warn?.(`[agentrux] Failed to stage image ${att.name}: ${err?.message}`);
        // Fall back to a URL reference so the user still sees something.
        blocks.push(`[画像: ${att.name}] (取得失敗: ${err?.message ?? "unknown"}) URL: ${downloadUrl}`);
      }
    } else if (isOtherMedia) {
      // Audio / video: no Buffer-based multimodal API yet. Follow-up
      // will wire up the transcribe/describe counterparts the same way
      // the image path feeds `images[]` above.
      blocks.push(`[添付: ${att.name}] (${att.content_type})\nURL: ${downloadUrl}`);
    } else {
      // Other binary (pdf, docx, zip, ...): leave a URL reference.
      blocks.push(`[添付: ${att.name}] (${att.content_type})\nURL: ${downloadUrl}`);
    }
  }

  const textBlock = blocks.length > 0 ? "\n\n" + blocks.join("\n\n") : "";
  return { textBlock, images };
}

// NOTE: the legacy `resolveSandboxUrls` helper (which regex-parsed
// "sandbox:" URLs out of agent reply text and uploaded the pointed-to
// files) lived here through v0.12.x. v0.13.0 replaces it with the
// channel's standard `sendPayload` outbound adapter in index.ts, which
// walks `ctx.payload.mediaUrls` directly — that is the same contract
// every stock OpenClaw channel uses for tool-produced outbound media,
// and it means the agent never embeds raw file paths in the reply body
// that the user would see.

function fetchUrl(url: string): Promise<string> {
  return new Promise((resolve, reject) => {
    const u = new URL(url);
    const mod = u.protocol === "https:" ? https : http;
    const req = mod.request(u, { method: "GET" }, (res) => {
      if (res.statusCode && res.statusCode >= 400) {
        res.resume();
        reject(new Error(`HTTP ${res.statusCode}`));
        return;
      }
      let data = "";
      res.setEncoding("utf-8");
      res.on("data", (chunk: string) => { data += chunk; });
      res.on("end", () => resolve(data));
      res.on("error", reject);
    });
    req.on("error", reject);
    req.end();
  });
}

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise(resolve => {
    if (signal?.aborted) { resolve(); return; }
    const timer = setTimeout(resolve, ms);
    signal?.addEventListener("abort", () => { clearTimeout(timer); resolve(); }, { once: true });
  });
}
