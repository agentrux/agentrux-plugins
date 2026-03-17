import {
	IPollFunctions,
	IWebhookFunctions,
	IHookFunctions,
	INodeType,
	INodeTypeDescription,
	IWebhookResponseData,
	INodeExecutionData,
	NodeOperationError,
} from 'n8n-workflow';
import { createHmac, timingSafeEqual } from 'crypto';

import {
	resolveCredentials,
	getValidToken,
	invalidateTokenCache,
	rawHttp,
} from '../../transport/apiRequest';

export class AgenTruxTrigger implements INodeType {
	description: INodeTypeDescription = {
		displayName: 'AgenTrux Trigger',
		name: 'agenTruxTrigger',
		icon: 'fa:satellite-dish',
		group: ['trigger'],
		version: 1,
		subtitle: '={{$parameter["triggerMode"]}}',
		description: 'Trigger workflows on new AgenTrux events via webhook or polling',
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
		webhooks: [
			{
				name: 'default',
				httpMethod: 'POST',
				responseMode: 'onReceived',
				path: 'agentrux-webhook',
			},
		],
		polling: true,
		properties: [
			{
				displayName: 'Trigger Mode',
				name: 'triggerMode',
				type: 'options',
				options: [
					{
						name: 'Polling',
						value: 'polling',
						description: 'Poll for new events periodically',
					},
					{
						name: 'Webhook',
						value: 'webhook',
						description: 'Receive hint notifications via webhook push',
					},
				],
				default: 'polling',
				description: 'How to receive new events',
			},
			// ── Polling fields ──
			{
				displayName: 'Topic ID',
				name: 'topicId',
				type: 'string',
				default: '',
				required: true,
				displayOptions: { show: { triggerMode: ['polling'] } },
				description: 'UUID of the topic to poll',
			},
			{
				displayName: 'Limit',
				name: 'limit',
				type: 'number',
				default: 50,
				typeOptions: { minValue: 1, maxValue: 200 },
				displayOptions: { show: { triggerMode: ['polling'] } },
				description: 'Maximum events per poll (max 200)',
			},
			{
				displayName: 'Event Type Filter',
				name: 'eventTypeFilter',
				type: 'string',
				default: '',
				displayOptions: { show: { triggerMode: ['polling'] } },
				description: 'Filter events by type (optional)',
			},
			// ── Webhook fields ──
			{
				displayName: 'Webhook Setup',
				name: 'webhookNotice',
				type: 'notice',
				default: '',
				displayOptions: { show: { triggerMode: ['webhook'] } },
				description:
					'After activating this workflow, copy the webhook URL shown below and register it in the AgenTrux console: Console → Topics → {topic} → Webhooks. AgenTrux sends hint notifications (topic_id + sequence_no) to this URL on each publish.',
			},
			{
				displayName: 'Timestamp Tolerance (Seconds)',
				name: 'timestampTolerance',
				type: 'number',
				default: 300,
				displayOptions: { show: { triggerMode: ['webhook'] } },
				description: 'Maximum age of the webhook timestamp header in seconds',
			},
		],
	};

	webhookMethods = {
		default: {
			async checkExists(this: IHookFunctions): Promise<boolean> {
				return true;
			},
			async create(this: IHookFunctions): Promise<boolean> {
				return true;
			},
			async delete(this: IHookFunctions): Promise<boolean> {
				return true;
			},
		},
	};

	async webhook(this: IWebhookFunctions): Promise<IWebhookResponseData> {
		const req = this.getRequestObject();
		const credentials = await this.getCredentials('agenTruxApi');
		const webhookSecret = credentials.webhookSecret as string;
		const triggerMode = this.getNodeParameter('triggerMode') as string;

		if (triggerMode !== 'webhook') {
			return { workflowData: [] };
		}

		// Verify HMAC-SHA256 signature if secret is configured
		// Signature format: "sha256={hex_digest}" where HMAC input = raw body bytes only
		if (webhookSecret) {
			const signatureHeader = req.headers['x-agentrux-signature'] as string;

			if (!signatureHeader) {
				return {
					webhookResponse: { status: 401, body: { error: 'Missing X-AgenTrux-Signature header' } },
				};
			}

			// Replay prevention: check timestamp inside the JSON body
			const tolerance = this.getNodeParameter('timestampTolerance', 300) as number;
			const bodyTs = (req.body as any)?.timestamp;
			if (typeof bodyTs === 'number') {
				const nowSec = Math.floor(Date.now() / 1000);
				if (Math.abs(nowSec - bodyTs) > tolerance) {
					return {
						webhookResponse: { status: 401, body: { error: 'Timestamp expired' } },
					};
				}
			}

			// HMAC verification: sign raw body bytes only (no timestamp prefix)
			const rawBody = (req as any).rawBody
				? (req as any).rawBody
				: Buffer.from(typeof req.body === 'string' ? req.body : JSON.stringify(req.body), 'utf-8');

			const expected = 'sha256=' + createHmac('sha256', webhookSecret)
				.update(rawBody)
				.digest('hex');

			const expectedBuffer = Buffer.from(expected);
			const actualBuffer = Buffer.from(signatureHeader);

			if (expectedBuffer.length !== actualBuffer.length || !timingSafeEqual(expectedBuffer, actualBuffer)) {
				return {
					webhookResponse: { status: 401, body: { error: 'Invalid signature' } },
				};
			}
		}

		const body = req.body as INodeExecutionData['json'];
		return {
			workflowData: [[{ json: body }]],
		};
	}

	async poll(this: IPollFunctions): Promise<INodeExecutionData[][] | null> {
		const triggerMode = this.getNodeParameter('triggerMode') as string;
		if (triggerMode !== 'polling') return null;

		const credentials = await this.getCredentials('agenTruxApi');
		const { resolved: creds } = await resolveCredentials(this as any, credentials);

		const topicId = this.getNodeParameter('topicId') as string;
		const limit = this.getNodeParameter('limit', 50) as number;
		const eventTypeFilter = this.getNodeParameter('eventTypeFilter', '') as string;

		const staticData = this.getWorkflowStaticData('node');
		const cursor = (staticData.cursor as string) || '';

		let path = `/topics/${topicId}/events?limit=${limit}`;
		if (cursor) path += `&cursor=${encodeURIComponent(cursor)}`;
		if (eventTypeFilter) path += `&type=${encodeURIComponent(eventTypeFilter)}`;

		const token = await getValidToken(this as any, creds);

		let resp = await rawHttp(this as any, 'GET', `${creds.baseUrl}${path}`, undefined, {
			Authorization: `Bearer ${token}`,
		});

		// On 401 — invalidate and retry
		if (resp.status === 401) {
			invalidateTokenCache(creds);
			const newToken = await getValidToken(this as any, creds);
			resp = await rawHttp(this as any, 'GET', `${creds.baseUrl}${path}`, undefined, {
				Authorization: `Bearer ${newToken}`,
			});
		}

		if (resp.status >= 400) {
			throw new NodeOperationError(
				this.getNode(),
				`Poll failed (${resp.status}): ${JSON.stringify(resp.body)}`,
			);
		}

		const items: any[] = resp.body.items || [];
		if (resp.body.next_cursor) {
			staticData.cursor = resp.body.next_cursor;
		}

		if (items.length === 0) return null;

		return [items.map((event: any) => ({ json: event }))];
	}
}
