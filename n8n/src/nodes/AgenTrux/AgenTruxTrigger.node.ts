import {
	ILoadOptionsFunctions,
	INodePropertyOptions,
	INodeType,
	INodeTypeDescription,
	ITriggerFunctions,
	ITriggerResponse,
} from 'n8n-workflow';
import * as http from 'http';
import * as https from 'https';

import {
	ensureToken,
	ensureTopPrefix,
	invalidateToken,
	listTopics,
	readEvents,
	resolveCredentials,
	USER_AGENT,
	type HttpHelper,
	type ResolvedCredentials,
} from '../../transport/apiRequest';

/**
 * AgenTrux Trigger — SSE hint + Pull.
 *
 * The AgenTrux server's SSE stream (GET /topics/{id}/events/stream) is
 * HINT-ONLY: each `event: hint` frame says "there is something new" but
 * carries no event body. So this trigger uses SSE purely as a low-latency
 * wake-up signal and pulls the actual events via GET /events from a cursor
 * (the event_id of the last event it emitted). A periodic poll runs as a
 * safety net in case an SSE hint is missed or the stream drops.
 *
 * Cursor (waterline) is kept in memory and skips to the latest event on
 * (re)start — an at-least-once design that avoids replaying a long backlog
 * after a restart (the same default the OpenClaw plugin uses). There is no
 * guaranteed per-node disk state in n8n, so we do not persist it.
 */
export class AgenTruxTrigger implements INodeType {
	description: INodeTypeDescription = {
		displayName: 'AgenTrux Trigger',
		name: 'agenTruxTrigger',
		icon: 'fa:satellite-dish',
		group: ['trigger'],
		version: 1,
		subtitle: '={{$parameter["topicId"]}}',
		description: 'Trigger on new AgenTrux events (SSE hint + Pull)',
		defaults: {
			name: 'AgenTrux Trigger',
		},
		inputs: [],
		outputs: ['main'],
		credentials: [
			{
				name: 'agenTruxApi',
				required: true,
			},
		],
		properties: [
			{
				displayName: 'Topic Name or ID',
				name: 'topicId',
				type: 'options',
				typeOptions: { loadOptionsMethod: 'getTopics' },
				default: '',
				required: true,
				description:
					'Topic to listen on. Choose from the list, or specify an ID using an <a href="https://docs.n8n.io/code/expressions/">expression</a>.',
			},
			{
				displayName: 'Event Type Filter',
				name: 'eventType',
				type: 'string',
				default: '',
				description: 'Only emit events of this type (optional)',
			},
			{
				displayName: 'Exclude Own Events',
				name: 'excludeSelf',
				type: 'boolean',
				default: true,
				description: 'Whether to drop events this credential published itself (avoids self-echo loops)',
			},
			{
				displayName: 'Safety Poll Interval (Seconds)',
				name: 'pollIntervalSeconds',
				type: 'number',
				default: 60,
				typeOptions: { minValue: 5 },
				description: 'How often to Pull as a fallback in case an SSE hint is missed',
			},
		],
	};

	methods = {
		loadOptions: {
			async getTopics(this: ILoadOptionsFunctions): Promise<INodePropertyOptions[]> {
				const credentials = await this.getCredentials('agenTruxApi');
				const creds = await resolveCredentials(this as unknown as HttpHelper, credentials);
				const topics = await listTopics(this as unknown as HttpHelper, creds);
				return topics.map((t) => ({
					name: t.actions.length ? `${t.name} (${t.actions.join('/')})` : t.name,
					value: t.topic_id,
				}));
			},
		},
	};

	async trigger(this: ITriggerFunctions): Promise<ITriggerResponse> {
		const credentials = await this.getCredentials('agenTruxApi');
		const creds: ResolvedCredentials = await resolveCredentials(this as unknown as HttpHelper, credentials);
		const ctx = this as unknown as HttpHelper;

		const topicId = ensureTopPrefix(this.getNodeParameter('topicId') as string);
		const eventType = (this.getNodeParameter('eventType', '') as string) || undefined;
		const excludeSelf = this.getNodeParameter('excludeSelf', true) as boolean;
		const pollIntervalMs = Math.max(5, this.getNodeParameter('pollIntervalSeconds', 60) as number) * 1000;

		let cursor = ''; // in-memory waterline (event_id of last emitted event)
		let stopped = false;
		let draining = false;
		let reconnectAttempts = 0;
		let pollTimer: NodeJS.Timeout | undefined;
		let sseReq: http.ClientRequest | undefined;

		const emit = (events: any[]): void => {
			if (events.length > 0) {
				this.emit([this.helpers.returnJsonArray(events)]);
			}
		};

		const sleep = (ms: number): Promise<void> =>
			new Promise((resolve) => {
				const t = setTimeout(resolve, ms);
				if (typeof t.unref === 'function') t.unref();
			});

		// Drain everything after the current cursor, emitting in FIFO order and
		// advancing the cursor. Guarded so overlapping hints don't double-pull.
		const drain = async (): Promise<void> => {
			if (draining || stopped) return;
			draining = true;
			try {
				while (!stopped) {
					const res = await readEvents(ctx, creds, topicId, {
						after: cursor || undefined,
						limit: 100,
						order: 'asc',
						eventType,
						excludeSelf,
					});
					if (res.ttlExpired) {
						// Pinned cursor aged out of retention — re-anchor to the
						// oldest still-retained event and continue FIFO.
						cursor = res.oldest || '';
						continue;
					}
					if (res.events.length === 0) break;
					emit(res.events);
					cursor = String(res.events[res.events.length - 1].event_id);
				}
			} catch (error) {
				this.logger?.warn?.(`[agentrux] drain error: ${(error as Error).message}`);
			} finally {
				draining = false;
			}
		};

		// On (re)start, skip to the latest event so we don't replay a backlog.
		const skipToLatest = async (): Promise<void> => {
			try {
				const res = await readEvents(ctx, creds, topicId, { limit: 1, order: 'desc', excludeSelf });
				if (res.events.length > 0) cursor = String(res.events[0].event_id);
			} catch (error) {
				this.logger?.warn?.(`[agentrux] skipToLatest failed: ${(error as Error).message}`);
			}
		};

		// One SSE connection. Each `data:` line is a hint → kick a drain.
		const openSse = (): Promise<void> =>
			new Promise<void>((resolve, reject) => {
				ensureToken(ctx, creds)
					.then((token) => {
						const u = new URL(`${creds.baseUrl}/topics/${topicId}/events/stream`);
						if (excludeSelf) u.searchParams.set('exclude_self', 'true');
						const mod = u.protocol === 'https:' ? https : http;
						const headers: Record<string, string> = {
							Authorization: `Bearer ${token}`,
							Accept: 'text/event-stream',
							'Cache-Control': 'no-cache',
							'User-Agent': USER_AGENT,
						};
						// Phase 2.5b: SSE resume is via the Last-Event-ID header.
						if (cursor) headers['Last-Event-ID'] = cursor;

						sseReq = mod.request(u, { method: 'GET', headers }, (res) => {
							if (res.statusCode === 401) {
								invalidateToken(creds);
								res.resume();
								reject(new Error('SSE auth expired'));
								return;
							}
							if (res.statusCode !== 200) {
								res.resume();
								reject(new Error(`SSE HTTP ${res.statusCode}`));
								return;
							}
							reconnectAttempts = 0;
							this.logger?.info?.('[agentrux] SSE connected');

							let buffer = '';
							res.on('data', (chunk: Buffer) => {
								buffer += chunk.toString();
								const lines = buffer.split('\n');
								buffer = lines.pop() ?? '';
								for (const line of lines) {
									// Hint-only stream: any data line means "pull now".
									if (line.startsWith('data:')) {
										void drain();
										break;
									}
								}
							});
							res.on('end', () => resolve());
							res.on('error', reject);
						});
						sseReq.on('error', reject);
						sseReq.end();
					})
					.catch(reject);
			});

		// Reconnecting SSE loop with exponential backoff.
		const sseLoop = async (): Promise<void> => {
			while (!stopped) {
				try {
					await openSse();
				} catch (error) {
					if (stopped) break;
					this.logger?.warn?.(`[agentrux] SSE disconnected: ${(error as Error).message}; reconnecting`);
				}
				if (stopped) break;
				const delay = Math.min(1000 * 2 ** reconnectAttempts, 60_000);
				reconnectAttempts++;
				await sleep(delay);
			}
		};

		// --- Start the listeners (production "active workflow" path) ---
		await skipToLatest();
		void sseLoop();
		pollTimer = setInterval(() => void drain(), pollIntervalMs);
		if (typeof pollTimer.unref === 'function') pollTimer.unref();

		// "Listen for test event" / manual execution: surface the most recent
		// events so the user sees output immediately.
		const manualTriggerFunction = async (): Promise<void> => {
			const res = await readEvents(ctx, creds, topicId, {
				limit: 10,
				order: 'desc',
				eventType,
				excludeSelf,
			});
			const events = res.events.slice().reverse();
			if (events.length > 0) {
				cursor = String(events[events.length - 1].event_id);
				emit(events);
			}
		};

		const closeFunction = async (): Promise<void> => {
			stopped = true;
			if (pollTimer) clearInterval(pollTimer);
			try {
				sseReq?.destroy();
			} catch {
				// already closed
			}
		};

		return { closeFunction, manualTriggerFunction };
	}
}
