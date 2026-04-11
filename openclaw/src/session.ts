/**
 * Session key mapping: conversation_key → OpenClaw sessionKey.
 * Validates, sanitizes, and hashes to prevent injection.
 */

import { createHash } from "crypto";

const ALLOWED_CHARS = /^[a-zA-Z0-9\-_\/]+$/;
const MAX_KEY_LENGTH = 128;

export function buildSessionKey(
  agentId: string,
  topicId: string,
  conversationKey: string,
): string {
  const sanitized = conversationKey.replace(/[^a-zA-Z0-9\-_\/]/g, "");
  if (!sanitized || sanitized.length > MAX_KEY_LENGTH) {
    throw new Error(`Invalid conversation_key: must be 1-${MAX_KEY_LENGTH} chars, alphanumeric/-/_/ only`);
  }

  const scope = createHash("sha256")
    .update(`${topicId}:${sanitized}`)
    .digest("hex")
    .slice(0, 16);

  return `agent:${agentId}:agentrux:${scope}`;
}

export function validateConversationKey(key: unknown): string {
  if (typeof key !== "string" || !key.trim()) {
    throw new Error("conversation_key is required");
  }
  const trimmed = key.trim();
  if (trimmed.length > MAX_KEY_LENGTH) {
    throw new Error(`conversation_key too long (max ${MAX_KEY_LENGTH})`);
  }
  if (!ALLOWED_CHARS.test(trimmed)) {
    throw new Error("conversation_key contains invalid characters");
  }
  return trimmed;
}
