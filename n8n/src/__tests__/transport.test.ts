/**
 * Unit tests for the AgenTrux n8n transport layer + credential definition.
 *
 * These cover the pure, side-effect-free helpers (prefix normalization,
 * ttl_expired detection) and the credential surface (fields + placeholders).
 * Network paths (redeem / token / read / publish) are exercised against a
 * mocked httpRequest helper.
 */

import { AgenTruxApi } from '../credentials/AgenTruxApi.credentials';
import {
	ensureTopPrefix,
	isTtlExpired,
	oldestAvailable,
	listTopics,
	readEvents,
	publishEvent,
	type HttpHelper,
	type ResolvedCredentials,
} from '../transport/apiRequest';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const CREDS: ResolvedCredentials = {
	baseUrl: 'https://api.agentrux.com',
	clientId: 'crd_test',
	clientSecret: 'aks_test',
};

/** Build a mock HttpHelper whose httpRequest returns queued responses by URL. */
function mockCtx(handler: (opts: any) => { statusCode: number; body: any }): {
	ctx: HttpHelper;
	calls: any[];
} {
	const calls: any[] = [];
	const ctx: HttpHelper = {
		getNode: () => ({ name: 'AgenTrux' }),
		helpers: {
			httpRequest: async (opts: any) => {
				calls.push(opts);
				const { statusCode, body } = handler(opts);
				return { statusCode, body };
			},
		},
	};
	return { ctx, calls };
}

// ---------------------------------------------------------------------------
// Pure helpers
// ---------------------------------------------------------------------------

describe('ensureTopPrefix', () => {
	test('adds top_ to a bare UUID', () => {
		expect(ensureTopPrefix('abc-123')).toBe('top_abc-123');
	});
	test('leaves an already-prefixed id unchanged', () => {
		expect(ensureTopPrefix('top_abc-123')).toBe('top_abc-123');
	});
	test('trims whitespace', () => {
		expect(ensureTopPrefix('  abc  ')).toBe('top_abc');
	});
});

describe('ttl_expired detection', () => {
	const ttlBody = {
		detail: { details: { reason: 'ttl_expired', oldest_available_evt_id: 'evt_oldest' }, next_action: 'cursor_advance' },
	};
	test('isTtlExpired recognises the server shape', () => {
		expect(isTtlExpired(ttlBody)).toBe(true);
		expect(isTtlExpired({ events: [] })).toBe(false);
	});
	test('oldestAvailable extracts the re-anchor cursor', () => {
		expect(oldestAvailable(ttlBody)).toBe('evt_oldest');
		expect(oldestAvailable({})).toBeNull();
	});
});

// ---------------------------------------------------------------------------
// Credential definition
// ---------------------------------------------------------------------------

describe('AgenTruxApi credential', () => {
	const cred = new AgenTruxApi();
	const names = cred.properties.map((p) => p.name);

	test('exposes baseUrl + activationCode', () => {
		expect(names).toContain('baseUrl');
		expect(names).toContain('activationCode');
	});
	test('activationCode is a password field with act_ placeholder', () => {
		const prop = cred.properties.find((p) => p.name === 'activationCode')!;
		expect(prop.typeOptions?.password).toBe(true);
		expect(prop.placeholder).toContain('act_');
	});
	test('baseUrl placeholder uses api.agentrux.com (not example.com)', () => {
		const prop = cred.properties.find((p) => p.name === 'baseUrl')!;
		expect(prop.placeholder).toContain('api.agentrux.com');
		expect(JSON.stringify(cred.properties)).not.toContain('example.com');
	});
	test('does not store raw script credentials (activation-code only)', () => {
		expect(names).not.toContain('clientSecret');
		expect(names).not.toContain('scriptId');
	});
});

// ---------------------------------------------------------------------------
// Network paths (mocked)
// ---------------------------------------------------------------------------

describe('readEvents', () => {
	test('sends after/limit/order/exclude_self and returns events + next', async () => {
		const { ctx, calls } = mockCtx((opts) => {
			if (String(opts.url).endsWith('/oauth/token')) {
				return { statusCode: 200, body: { access_token: 'aat_x', expires_in: 600 } };
			}
			return {
				statusCode: 200,
				body: { events: [{ event_id: 'evt_1' }], next: { after: 'evt_1', has_more: false } },
			};
		});
		const res = await readEvents(ctx, CREDS, 'top_t', {
			after: 'evt_0',
			limit: 50,
			order: 'asc',
			excludeSelf: true,
		});
		expect(res.events).toHaveLength(1);
		expect(res.next.after).toBe('evt_1');
		const eventsCall = calls.find((c) => String(c.url).includes('/events'));
		expect(eventsCall.qs).toMatchObject({ after: 'evt_0', limit: 50, order: 'asc', exclude_self: 'true' });
	});

	test('maps a ttl_expired 404 to a re-anchor signal instead of throwing', async () => {
		const { ctx } = mockCtx((opts) => {
			if (String(opts.url).endsWith('/oauth/token')) {
				return { statusCode: 200, body: { access_token: 'aat_x', expires_in: 600 } };
			}
			return {
				statusCode: 404,
				body: { detail: { details: { reason: 'ttl_expired', oldest_available_evt_id: 'evt_old' } } },
			};
		});
		const res = await readEvents(ctx, CREDS, 'top_t', { limit: 10 });
		expect(res.ttlExpired).toBe(true);
		expect(res.oldest).toBe('evt_old');
	});
});

describe('publishEvent', () => {
	test('sends event_type + inline payload and returns event_id', async () => {
		const { ctx, calls } = mockCtx((opts) => {
			if (String(opts.url).endsWith('/oauth/token')) {
				return { statusCode: 200, body: { access_token: 'aat_x', expires_in: 600 } };
			}
			return { statusCode: 200, body: { event_id: 'evt_new' } };
		});
		const result = await publishEvent(ctx, CREDS, 'top_t', {
			eventType: 'hello.world',
			payload: { msg: 'hi' },
		});
		expect(result.event_id).toBe('evt_new');
		const publishCall = calls.find((c) => c.method === 'POST' && String(c.url).includes('/events'));
		expect(publishCall.body).toMatchObject({ event_type: 'hello.world', payload: { msg: 'hi' } });
	});

	test('object-ref mode omits inline payload', async () => {
		const { ctx, calls } = mockCtx((opts) => {
			if (String(opts.url).endsWith('/oauth/token')) {
				return { statusCode: 200, body: { access_token: 'aat_x', expires_in: 600 } };
			}
			return { statusCode: 200, body: { event_id: 'evt_obj' } };
		});
		await publishEvent(ctx, CREDS, 'top_t', {
			eventType: 'file.shared',
			payloadObjectId: 'pob_1',
		});
		const publishCall = calls.find((c) => c.method === 'POST' && String(c.url).includes('/events'));
		expect(publishCall.body.payload_object_id).toBe('pob_1');
		expect(publishCall.body.payload).toBeUndefined();
	});
});

describe('listTopics', () => {
	test('normalises items into {topic_id, name, actions}', async () => {
		const { ctx } = mockCtx((opts) => {
			if (String(opts.url).endsWith('/oauth/token')) {
				return { statusCode: 200, body: { access_token: 'aat_x', expires_in: 600 } };
			}
			return {
				statusCode: 200,
				body: { items: [{ topic_id: 'top_a', display_name: 'Alpha', actions: ['read', 'write'] }] },
			};
		});
		const topics = await listTopics(ctx, CREDS);
		expect(topics).toEqual([{ topic_id: 'top_a', name: 'Alpha', actions: ['read', 'write'] }]);
	});
});
