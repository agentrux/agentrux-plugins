/**
 * Shared authentication utilities for AgenTrux Flowise nodes.
 * Handles JWT lifecycle: authenticate, cache, refresh with rotation, retry on 401.
 */

interface TokenState {
    accessToken: string;
    refreshToken: string;
    expiresAt: number; // epoch ms
}

const tokenCache: Map<string, TokenState> = new Map();

async function httpFetch(
    url: string,
    method: string,
    body?: unknown,
    headers?: Record<string, string>,
): Promise<{ status: number; data: any }> {
    const init: RequestInit = {
        method,
        headers: {
            'Content-Type': 'application/json',
            ...headers,
        },
    };
    if (body !== undefined) {
        init.body = JSON.stringify(body);
    }
    const resp = await fetch(url, init);
    const data = await resp.json().catch(() => ({}));
    return { status: resp.status, data };
}

async function authenticate(
    baseUrl: string,
    scriptId: string,
    secret: string,
): Promise<TokenState> {
    const resp = await httpFetch(`${baseUrl}/auth/token`, 'POST', {
        script_id: scriptId,
        secret,
    });
    if (resp.status >= 400) {
        throw new Error(`AgenTrux authentication failed (${resp.status}): ${JSON.stringify(resp.data)}`);
    }
    return {
        accessToken: resp.data.access_token,
        refreshToken: resp.data.refresh_token,
        expiresAt: new Date(resp.data.expires_at).getTime(),
    };
}

async function refreshTokenRequest(
    baseUrl: string,
    currentRefreshToken: string,
): Promise<TokenState | null> {
    const resp = await httpFetch(`${baseUrl}/auth/refresh`, 'POST', {
        refresh_token: currentRefreshToken,
    });
    if (resp.status >= 400) {
        return null;
    }
    return {
        accessToken: resp.data.access_token,
        refreshToken: resp.data.refresh_token,
        expiresAt: new Date(resp.data.expires_at).getTime(),
    };
}

async function redeemGrant(
    baseUrl: string,
    grantToken: string,
    scriptId: string,
    secret: string,
): Promise<void> {
    const resp = await httpFetch(`${baseUrl}/auth/redeem-grant`, 'POST', {
        token: grantToken,
        script_id: scriptId,
        secret,
    });
    if (resp.status >= 400) {
        throw new Error(`Grant redemption failed (${resp.status}): ${JSON.stringify(resp.data)}`);
    }
}

/**
 * Get a valid access token, refreshing or re-authenticating as needed.
 */
export async function getValidToken(
    baseUrl: string,
    scriptId: string,
    secret: string,
    grantToken?: string,
): Promise<string> {
    const cacheKey = `${baseUrl}::${scriptId}`;
    let state = tokenCache.get(cacheKey);

    // Return cached token if still valid (30s buffer)
    if (state && state.expiresAt > Date.now() + 30_000) {
        return state.accessToken;
    }

    // Try refresh if we have a refresh token
    if (state?.refreshToken) {
        const refreshed = await refreshTokenRequest(baseUrl, state.refreshToken);
        if (refreshed) {
            tokenCache.set(cacheKey, refreshed);
            return refreshed.accessToken;
        }
    }

    // Redeem grant token if provided (first-time cross-account setup)
    if (grantToken) {
        try {
            await redeemGrant(baseUrl, grantToken, scriptId, secret);
        } catch {
            // Grant may already be redeemed, continue to authenticate
        }
    }

    // Full authentication
    state = await authenticate(baseUrl, scriptId, secret);
    tokenCache.set(cacheKey, state);
    return state.accessToken;
}

/**
 * Make an authenticated request with automatic token management and 401 retry.
 */
export async function authenticatedFetch(
    baseUrl: string,
    scriptId: string,
    secret: string,
    method: string,
    path: string,
    body?: unknown,
    grantToken?: string,
): Promise<any> {
    const token = await getValidToken(baseUrl, scriptId, secret, grantToken);
    const resp = await httpFetch(`${baseUrl}${path}`, method, body, {
        Authorization: `Bearer ${token}`,
    });

    // On 401, invalidate cache and retry once
    if (resp.status === 401) {
        const cacheKey = `${baseUrl}::${scriptId}`;
        tokenCache.delete(cacheKey);
        const newToken = await getValidToken(baseUrl, scriptId, secret, grantToken);
        const retry = await httpFetch(`${baseUrl}${path}`, method, body, {
            Authorization: `Bearer ${newToken}`,
        });
        if (retry.status >= 400) {
            throw new Error(`AgenTrux request failed (${retry.status}): ${JSON.stringify(retry.data)}`);
        }
        return retry.data;
    }

    if (resp.status >= 400) {
        throw new Error(`AgenTrux request failed (${resp.status}): ${JSON.stringify(resp.data)}`);
    }
    return resp.data;
}
