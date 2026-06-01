/**
 * Shared transport layer for the AgenTrux n8n nodes.
 *
 * Defined once and used by the AgenTrux (action) node, the AgenTrux Trigger
 * (SSE-hint + Pull) node, and the loadOptions topic dropdown so the auth /
 * request logic lives in exactly one place.
 *
 * Auth model (current AgenTrux server, Phase 1.9+):
 *   1. Activation Code (single-use `act_...`) is redeemed once via
 *      POST /auth/redeem-activation-code -> { client_id: "crd_...",
 *      client_secret: "aks_..." }. n8n re-tests and re-runs credentials
 *      repeatedly, so the redeemed pair is cached to disk keyed by
 *      sha256(activation_code); re-running never re-redeems the consumed
 *      code (mirrors the Dify plugin's validate_activation()).
 *   2. The script credential is exchanged for a short-lived access token
 *      (`aat_<JWT>`) via the OAuth 2.1 client_credentials grant
 *      (POST /oauth/token, form-encoded). There is no refresh token; the
 *      token is re-issued on expiry or on a 401.
 *
 * Data plane (current server):
 *   - Publish: POST /topics/{top_id}/events { event_type, payload, metadata?,
 *     payload_object_id? } -> { event_id }.
 *   - Read:    GET /topics/{top_id}/events?after=<evt_id>&limit=&order= ->
 *     { events: [...], next: { after, has_more, url } }. The cursor is the
 *     event_id (NOT a sequence number).
 *   - Topic IDs must carry the `top_` prefix on every data-plane path.
 *
 * The AgenTrux prod ALB rejects requests with no User-Agent (403 from
 * awselb/2.0), so every request sends a stable UA.
 */
import { createHash } from 'crypto';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { NodeOperationError } from 'n8n-workflow';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

export const USER_AGENT = 'agentrux-n8n-plugin/0.x (+n8n)';
export const DEFAULT_BASE_URL = 'https://api.agentrux.com';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface ResolvedCredentials {
	baseUrl: string;
	clientId: string; // crd_<uuid>
	clientSecret: string; // aks_<plain>
	scriptId?: string; // scr_<uuid>, display only
}

interface TokenState {
	accessToken: string; // aat_<JWT>
	expiresAt: number; // epoch ms
}

/**
 * Minimal structural interface shared by IExecuteFunctions /
 * ILoadOptionsFunctions / ITriggerFunctions — everything the transport layer
 * needs from an n8n execution context.
 */
export interface HttpHelper {
	helpers: {
		httpRequest(options: Record<string, unknown>): Promise<unknown>;
	};
	getNode(): unknown;
}

interface FullResponse {
	status: number;
	body: any;
}

// ---------------------------------------------------------------------------
// Process-level caches (persist across executions within one n8n process)
// ---------------------------------------------------------------------------

/** Token cache: `${baseUrl}::${clientId}` -> TokenState */
const tokenCache: Map<string, TokenState> = new Map();

// ---------------------------------------------------------------------------
// Low-level HTTP (via n8n's request helper, so proxy/cert config is honoured)
// ---------------------------------------------------------------------------

async function httpRequestFull(
	ctx: HttpHelper,
	options: Record<string, unknown>,
): Promise<FullResponse> {
	const headers = {
		'User-Agent': USER_AGENT,
		...((options.headers as Record<string, string>) ?? {}),
	};
	const response = (await ctx.helpers.httpRequest({
		returnFullResponse: true,
		ignoreHttpStatusErrors: true,
		...options,
		headers,
	})) as { statusCode?: number; body?: unknown };

	let body: any = response.body;
	if (typeof body === 'string') {
		try {
			body = JSON.parse(body);
		} catch {
			// keep raw string body (e.g. error pages)
		}
	}
	return { status: response.statusCode ?? 0, body };
}

// ---------------------------------------------------------------------------
// ID prefix normalization
// ---------------------------------------------------------------------------

/**
 * Ensure a topic id carries the `top_` prefix. `pipe_router` enforces it on
 * every data-plane path; callers may hold a bare UUID (e.g. typed by a user),
 * so we normalize at the boundary.
 */
export function ensureTopPrefix(topicId: string): string {
	const s = String(topicId ?? '').trim();
	return s.startsWith('top_') ? s : `top_${s}`;
}

// ---------------------------------------------------------------------------
// Activation-code disk cache (idempotent redemption of a single-use code)
// ---------------------------------------------------------------------------

function activationCacheDir(): string {
	const home = process.env.AGENTRUX_HOME || path.join(os.homedir(), '.agentrux');
	return home;
}

function activationCacheFile(): string {
	return path.join(activationCacheDir(), 'n8n_activated.json');
}

function fingerprint(code: string): string {
	return createHash('sha256').update(code, 'utf-8').digest('hex');
}

interface ActivationCacheEntry {
	base_url: string;
	client_id: string;
	client_secret: string;
	script_id?: string;
	activated_at: number;
}

function loadActivationCache(): Record<string, ActivationCacheEntry> {
	try {
		const file = activationCacheFile();
		if (!fs.existsSync(file)) return {};
		const raw = JSON.parse(fs.readFileSync(file, 'utf-8'));
		return raw && typeof raw === 'object' ? raw : {};
	} catch {
		return {};
	}
}

function saveActivationCache(cache: Record<string, ActivationCacheEntry>): void {
	try {
		const dir = activationCacheDir();
		if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
		const file = activationCacheFile();
		const tmp = `${file}.tmp`;
		fs.writeFileSync(tmp, JSON.stringify(cache, null, 2), { mode: 0o600 });
		fs.renameSync(tmp, file); // atomic publish
	} catch {
		// cache is best-effort; a write failure just means we redeem next time
	}
}

/**
 * Redeem an Activation Code into Script credentials, idempotent via the
 * sha256(code) fingerprint cache. Re-running with the same (already consumed)
 * code resolves from disk instead of hitting the single-use server endpoint.
 */
export async function redeemActivationCode(
	ctx: HttpHelper,
	baseUrl: string,
	activationCode: string,
): Promise<{ clientId: string; clientSecret: string; scriptId?: string }> {
	const fp = fingerprint(activationCode);
	const cache = loadActivationCache();
	const entry = cache[fp];
	if (entry && entry.base_url === baseUrl && entry.client_id && entry.client_secret) {
		return {
			clientId: entry.client_id,
			clientSecret: entry.client_secret,
			scriptId: entry.script_id,
		};
	}

	const resp = await httpRequestFull(ctx, {
		method: 'POST',
		url: `${baseUrl}/auth/redeem-activation-code`,
		body: { code: activationCode },
		json: true,
	});
	if (resp.status !== 200 || !resp.body?.client_id || !resp.body?.client_secret) {
		throw new NodeOperationError(
			ctx.getNode() as never,
			`Activation failed (${resp.status}): ${JSON.stringify(resp.body)}`,
		);
	}

	const fresh: ActivationCacheEntry = {
		base_url: baseUrl,
		client_id: String(resp.body.client_id),
		client_secret: String(resp.body.client_secret),
		script_id: resp.body.script_id ? String(resp.body.script_id) : undefined,
		activated_at: Math.floor(Date.now() / 1000),
	};
	cache[fp] = fresh;
	saveActivationCache(cache);
	return { clientId: fresh.client_id, clientSecret: fresh.client_secret, scriptId: fresh.script_id };
}

// ---------------------------------------------------------------------------
// Credential resolution (n8n credential object -> ResolvedCredentials)
// ---------------------------------------------------------------------------

export async function resolveCredentials(
	ctx: HttpHelper,
	credentials: Record<string, unknown>,
): Promise<ResolvedCredentials> {
	const baseUrl = (String(credentials.baseUrl ?? '').replace(/\/+$/, '') || DEFAULT_BASE_URL).trim();
	const activationCode = String(credentials.activationCode ?? '').trim();
	if (!activationCode) {
		throw new NodeOperationError(ctx.getNode() as never, 'Activation Code is required');
	}
	const { clientId, clientSecret, scriptId } = await redeemActivationCode(ctx, baseUrl, activationCode);
	return { baseUrl, clientId, clientSecret, scriptId };
}

// ---------------------------------------------------------------------------
// Access-token lifecycle (client_credentials grant)
// ---------------------------------------------------------------------------

export async function ensureToken(ctx: HttpHelper, creds: ResolvedCredentials): Promise<string> {
	const key = `${creds.baseUrl}::${creds.clientId}`;
	const cached = tokenCache.get(key);
	if (cached && cached.expiresAt > Date.now() + 60_000) {
		return cached.accessToken;
	}

	const form = new URLSearchParams({
		grant_type: 'client_credentials',
		client_id: creds.clientId,
		client_secret: creds.clientSecret,
	}).toString();

	const resp = await httpRequestFull(ctx, {
		method: 'POST',
		url: `${creds.baseUrl}/oauth/token`,
		body: form,
		headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
	});
	if (resp.status !== 200 || !resp.body?.access_token) {
		throw new NodeOperationError(
			ctx.getNode() as never,
			`Token request failed (${resp.status}): ${JSON.stringify(resp.body)}`,
		);
	}
	const expiresIn = typeof resp.body.expires_in === 'number' ? resp.body.expires_in : 600;
	const state: TokenState = {
		accessToken: String(resp.body.access_token),
		expiresAt: Date.now() + expiresIn * 1000,
	};
	tokenCache.set(key, state);
	return state.accessToken;
}

export function invalidateToken(creds: ResolvedCredentials): void {
	tokenCache.delete(`${creds.baseUrl}::${creds.clientId}`);
}

// ---------------------------------------------------------------------------
// Authenticated request (Bearer; one retry on 401 after re-issuing the token)
// ---------------------------------------------------------------------------

export async function agentruxApiRequest(
	ctx: HttpHelper,
	method: 'GET' | 'POST' | 'PUT' | 'DELETE',
	creds: ResolvedCredentials,
	urlPath: string,
	opts: { body?: Record<string, unknown>; qs?: Record<string, unknown> } = {},
): Promise<any> {
	const url = `${creds.baseUrl}${urlPath}`;
	const doRequest = async (token: string): Promise<FullResponse> =>
		httpRequestFull(ctx, {
			method,
			url,
			body: opts.body,
			qs: opts.qs,
			json: true,
			headers: { Authorization: `Bearer ${token}` },
		});

	let resp = await doRequest(await ensureToken(ctx, creds));
	if (resp.status === 401) {
		invalidateToken(creds);
		resp = await doRequest(await ensureToken(ctx, creds));
	}
	if (resp.status >= 400) {
		throw new NodeOperationError(
			ctx.getNode() as never,
			`Request failed (${resp.status}): ${JSON.stringify(resp.body)}`,
		);
	}
	return resp.body;
}

// ---------------------------------------------------------------------------
// Topic listing (for the loadOptions dropdown)
// ---------------------------------------------------------------------------

export interface TopicSummary {
	topic_id: string;
	name: string;
	actions: string[];
}

export async function listTopics(ctx: HttpHelper, creds: ResolvedCredentials): Promise<TopicSummary[]> {
	const body = await agentruxApiRequest(ctx, 'GET', creds, '/topics');
	const items: any[] = body?.items ?? [];
	return items
		.filter((it) => it && it.topic_id)
		.map((it) => ({
			topic_id: String(it.topic_id),
			name: String(it.display_name || it.name || it.topic_id),
			actions: Array.isArray(it.actions) ? it.actions.map(String) : [],
		}));
}

// ---------------------------------------------------------------------------
// Event read (Pull) with ttl_expired cursor handling
// ---------------------------------------------------------------------------

function detailOf(body: any): any {
	return body?.detail ?? body;
}

export function isTtlExpired(body: any): boolean {
	const d = detailOf(body);
	return d?.details?.reason === 'ttl_expired' || d?.next_action === 'cursor_advance';
}

export function oldestAvailable(body: any): string | null {
	const d = detailOf(body);
	const oldest = d?.details?.oldest_available_evt_id;
	return typeof oldest === 'string' && oldest.length > 0 ? oldest : null;
}

export interface ReadEventsParams {
	after?: string;
	limit?: number;
	order?: 'asc' | 'desc';
	eventType?: string;
	excludeSelf?: boolean;
}

export interface ReadEventsResult {
	events: any[];
	next: { after?: string; has_more?: boolean; url?: string };
	ttlExpired?: boolean;
	oldest?: string | null;
}

/**
 * GET /topics/{top_id}/events. Returns the raw page plus a ttl_expired signal
 * so callers can re-anchor an aged-out cursor instead of failing. A 401 is
 * retried once after re-issuing the token; other 4xx/5xx throw.
 */
export async function readEvents(
	ctx: HttpHelper,
	creds: ResolvedCredentials,
	topicId: string,
	params: ReadEventsParams,
): Promise<ReadEventsResult> {
	const top = ensureTopPrefix(topicId);
	const url = `${creds.baseUrl}/topics/${top}/events`;
	const qs: Record<string, string | number> = { limit: params.limit ?? 50 };
	if (params.after) qs.after = params.after;
	if (params.order) qs.order = params.order;
	if (params.eventType) qs.type = params.eventType;
	if (params.excludeSelf) qs.exclude_self = 'true';

	const doRequest = async (token: string): Promise<FullResponse> =>
		httpRequestFull(ctx, {
			method: 'GET',
			url,
			qs,
			json: true,
			headers: { Authorization: `Bearer ${token}` },
		});

	let resp = await doRequest(await ensureToken(ctx, creds));
	if (resp.status === 401) {
		invalidateToken(creds);
		resp = await doRequest(await ensureToken(ctx, creds));
	}
	if (resp.status === 404 && isTtlExpired(resp.body)) {
		return { events: [], next: {}, ttlExpired: true, oldest: oldestAvailable(resp.body) };
	}
	if (resp.status >= 400) {
		throw new NodeOperationError(
			ctx.getNode() as never,
			`Read failed (${resp.status}): ${JSON.stringify(resp.body)}`,
		);
	}
	return { events: resp.body?.events ?? [], next: resp.body?.next ?? {} };
}

// ---------------------------------------------------------------------------
// Event publish
// ---------------------------------------------------------------------------

export interface PublishParams {
	eventType: string;
	payload?: Record<string, unknown>;
	metadata?: Record<string, unknown>;
	payloadObjectId?: string;
}

export async function publishEvent(
	ctx: HttpHelper,
	creds: ResolvedCredentials,
	topicId: string,
	params: PublishParams,
): Promise<any> {
	const top = ensureTopPrefix(topicId);
	const body: Record<string, unknown> = { event_type: params.eventType };
	// publish_event.py: inline `payload` and top-level `payload_object_id`
	// are mutually exclusive per event.
	if (params.payloadObjectId) {
		body.payload_object_id = params.payloadObjectId;
	} else {
		body.payload = params.payload ?? {};
	}
	if (params.metadata && Object.keys(params.metadata).length > 0) {
		body.metadata = params.metadata;
	}
	return agentruxApiRequest(ctx, 'POST', creds, `/topics/${top}/events`, { body });
}
