/**
 * Shared transport layer — token management, auto-activation, grant redemption.
 *
 * Both AgenTrux (action) and AgenTruxTrigger (poll) nodes use this module
 * so that credential handling logic is defined exactly once.
 */
import { NodeOperationError } from 'n8n-workflow';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface TokenState {
	accessToken: string;
	refreshToken: string;
	expiresAt: number; // epoch ms
}

export interface ActivationResult {
	scriptId: string;
	secret: string;
	grants: Array<{ grant_id: string; topic_id: string; action: string }>;
}

export interface ResolvedCredentials {
	baseUrl: string;
	scriptId: string;
	secret: string;
}

/** Minimal interface shared by IExecuteFunctions / IPollFunctions / IHookFunctions */
export interface HttpHelper {
	helpers: {
		httpRequest(options: any): Promise<any>;
	};
	getNode(): any;
}

// ---------------------------------------------------------------------------
// Caches (module-level, persist across executions within the n8n process)
// ---------------------------------------------------------------------------

/** JWT cache: cacheKey → TokenState */
const tokenCache: Map<string, TokenState> = new Map();

/** Activation cache: "baseUrl::activationToken" → ActivationResult */
const activationCache: Map<string, ActivationResult> = new Map();

/** Set of cache keys where the grant token has already been redeemed */
const grantRedeemedKeys: Set<string> = new Set();

// ---------------------------------------------------------------------------
// Low-level HTTP
// ---------------------------------------------------------------------------

export async function rawHttp(
	ctx: HttpHelper,
	method: 'GET' | 'POST' | 'PUT' | 'DELETE',
	url: string,
	body?: Record<string, unknown>,
	headers?: Record<string, string>,
): Promise<{ status: number; body: any }> {
	const options: any = {
		method,
		url,
		headers: { 'Content-Type': 'application/json', ...headers },
		returnFullResponse: true,
		json: true,
		ignoreHttpStatusErrors: true,
	};
	if (body) options.body = body;
	const response = await ctx.helpers.httpRequest(options);
	return { status: response.statusCode ?? 200, body: response.body ?? response };
}

// ---------------------------------------------------------------------------
// Activation (one-time)
// ---------------------------------------------------------------------------

export async function activateScript(
	ctx: HttpHelper,
	baseUrl: string,
	activationToken: string,
): Promise<ActivationResult> {
	// Return cached result if already activated in this process
	const cacheKey = `${baseUrl}::${activationToken}`;
	const cached = activationCache.get(cacheKey);
	if (cached) return cached;

	const resp = await rawHttp(ctx, 'POST', `${baseUrl}/auth/activate`, {
		token: activationToken,
	});
	if (resp.status >= 400) {
		throw new NodeOperationError(
			ctx.getNode(),
			`Activation failed (${resp.status}): ${JSON.stringify(resp.body)}`,
		);
	}

	const result: ActivationResult = {
		scriptId: resp.body.script_id,
		secret: resp.body.secret,
		grants: resp.body.grants ?? [],
	};
	activationCache.set(cacheKey, result);
	return result;
}

// ---------------------------------------------------------------------------
// Grant token redemption (one-time, idempotent)
// ---------------------------------------------------------------------------

async function redeemGrantOnce(
	ctx: HttpHelper,
	baseUrl: string,
	grantToken: string,
	scriptId: string,
	secret: string,
): Promise<void> {
	const key = `${baseUrl}::${scriptId}::${grantToken}`;
	if (grantRedeemedKeys.has(key)) return;

	const resp = await rawHttp(ctx, 'POST', `${baseUrl}/auth/redeem-grant`, {
		token: grantToken,
		script_id: scriptId,
		secret,
	});
	// 4xx is OK — grant may already be consumed
	grantRedeemedKeys.add(key);

	if (resp.status >= 400 && resp.status !== 409) {
		// Log but don't throw — the grant may have been redeemed in a previous session
	}
}

// ---------------------------------------------------------------------------
// Credential resolution (activation token → script credentials)
// ---------------------------------------------------------------------------

export async function resolveCredentials(
	ctx: HttpHelper,
	credentials: Record<string, any>,
): Promise<{ resolved: ResolvedCredentials; activationResult?: ActivationResult }> {
	const baseUrl = (credentials.baseUrl as string).replace(/\/+$/, '');
	const authMode = credentials.authMode as string;

	if (authMode === 'activationToken') {
		const activationToken = credentials.activationToken as string;
		if (!activationToken) {
			throw new NodeOperationError(ctx.getNode(), 'Activation token is required');
		}
		const result = await activateScript(ctx, baseUrl, activationToken);
		return {
			resolved: { baseUrl, scriptId: result.scriptId, secret: result.secret },
			activationResult: result,
		};
	}

	// scriptCredentials mode
	const scriptId = credentials.scriptId as string;
	const secret = credentials.secret as string;
	if (!scriptId || !secret) {
		throw new NodeOperationError(ctx.getNode(), 'Script ID and Secret are required');
	}

	// Auto-redeem grant token if provided
	const grantToken = (credentials.grantToken as string) || '';
	if (grantToken) {
		await redeemGrantOnce(ctx, baseUrl, grantToken, scriptId, secret);
	}

	return { resolved: { baseUrl, scriptId, secret } };
}

// ---------------------------------------------------------------------------
// JWT token lifecycle
// ---------------------------------------------------------------------------

async function authenticate(
	ctx: HttpHelper,
	baseUrl: string,
	scriptId: string,
	secret: string,
): Promise<TokenState> {
	const resp = await rawHttp(ctx, 'POST', `${baseUrl}/auth/token`, {
		script_id: scriptId,
		secret,
	});
	if (resp.status >= 400) {
		throw new NodeOperationError(
			ctx.getNode(),
			`Authentication failed (${resp.status}): ${JSON.stringify(resp.body)}`,
		);
	}
	return {
		accessToken: resp.body.access_token,
		refreshToken: resp.body.refresh_token,
		expiresAt: new Date(resp.body.expires_at).getTime(),
	};
}

async function tryRefresh(
	ctx: HttpHelper,
	baseUrl: string,
	refreshToken: string,
): Promise<TokenState | null> {
	const resp = await rawHttp(ctx, 'POST', `${baseUrl}/auth/refresh`, {
		refresh_token: refreshToken,
	});
	if (resp.status >= 400) return null;
	return {
		accessToken: resp.body.access_token,
		refreshToken: resp.body.refresh_token,
		expiresAt: new Date(resp.body.expires_at).getTime(),
	};
}

export async function getValidToken(
	ctx: HttpHelper,
	creds: ResolvedCredentials,
): Promise<string> {
	const cacheKey = `${creds.baseUrl}::${creds.scriptId}`;
	let state = tokenCache.get(cacheKey);

	// Valid token with 30s buffer
	if (state && state.expiresAt > Date.now() + 30_000) {
		return state.accessToken;
	}

	// Try refresh
	if (state?.refreshToken) {
		const refreshed = await tryRefresh(ctx, creds.baseUrl, state.refreshToken);
		if (refreshed) {
			tokenCache.set(cacheKey, refreshed);
			return refreshed.accessToken;
		}
	}

	// Full authentication
	state = await authenticate(ctx, creds.baseUrl, creds.scriptId, creds.secret);
	tokenCache.set(cacheKey, state);
	return state.accessToken;
}

export function invalidateTokenCache(creds: ResolvedCredentials): void {
	tokenCache.delete(`${creds.baseUrl}::${creds.scriptId}`);
}

// ---------------------------------------------------------------------------
// Authenticated request with auto-retry on 401
// ---------------------------------------------------------------------------

export async function authenticatedRequest(
	ctx: HttpHelper,
	method: 'GET' | 'POST' | 'PUT' | 'DELETE',
	creds: ResolvedCredentials,
	path: string,
	body?: Record<string, unknown>,
): Promise<any> {
	const token = await getValidToken(ctx, creds);
	const url = `${creds.baseUrl}${path}`;
	const resp = await rawHttp(ctx, method, url, body, {
		Authorization: `Bearer ${token}`,
	});

	// On 401 — invalidate cache and retry once
	if (resp.status === 401) {
		invalidateTokenCache(creds);
		const newToken = await getValidToken(ctx, creds);
		const retry = await rawHttp(ctx, method, url, body, {
			Authorization: `Bearer ${newToken}`,
		});
		if (retry.status >= 400) {
			throw new NodeOperationError(
				ctx.getNode(),
				`Request failed (${retry.status}): ${JSON.stringify(retry.body)}`,
			);
		}
		return retry.body;
	}

	if (resp.status >= 400) {
		throw new NodeOperationError(
			ctx.getNode(),
			`Request failed (${resp.status}): ${JSON.stringify(resp.body)}`,
		);
	}
	return resp.body;
}
