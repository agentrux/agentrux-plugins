import {
	IDataObject,
	IExecuteFunctions,
	ILoadOptionsFunctions,
	INodeExecutionData,
	INodePropertyOptions,
	INodeType,
	INodeTypeDescription,
} from 'n8n-workflow';

import {
	listTopics,
	publishEvent,
	readEvents,
	resolveCredentials,
	type HttpHelper,
} from '../../transport/apiRequest';

/**
 * AgenTrux action node — communicate over a Topic.
 *
 *   - Publish Event: post an event to a topic.
 *   - Read Events:   cursor-paginated pull of events from a topic.
 *   - List Topics:   list the topics this credential can reach.
 *
 * Topics are chosen from a dropdown (loadOptions) populated from GET /topics,
 * so a human picks by name rather than pasting a raw UUID.
 */
export class AgenTrux implements INodeType {
	description: INodeTypeDescription = {
		displayName: 'AgenTrux',
		name: 'agenTrux',
		icon: 'fa:satellite-dish',
		group: ['transform'],
		version: 1,
		subtitle: '={{$parameter["operation"]}}',
		description: 'Publish and read events on AgenTrux topics',
		defaults: {
			name: 'AgenTrux',
		},
		inputs: ['main'],
		outputs: ['main'],
		credentials: [
			{
				name: 'agenTruxApi',
				required: true,
			},
		],
		properties: [
			{
				displayName: 'Operation',
				name: 'operation',
				type: 'options',
				noDataExpression: true,
				options: [
					{ name: 'Publish Event', value: 'publishEvent', description: 'Publish an event to a topic', action: 'Publish an event' },
					{ name: 'Read Events', value: 'readEvents', description: 'Read events from a topic (cursor pagination)', action: 'Read events' },
					{ name: 'List Topics', value: 'listTopics', description: 'List topics this credential can reach', action: 'List topics' },
				],
				default: 'publishEvent',
			},

			// ── Topic selector (publish + read) ──
			{
				displayName: 'Topic Name or ID',
				name: 'topicId',
				type: 'options',
				typeOptions: { loadOptionsMethod: 'getTopics' },
				default: '',
				required: true,
				displayOptions: { show: { operation: ['publishEvent', 'readEvents'] } },
				description:
					'Topic to use. Choose from the list, or specify an ID using an <a href="https://docs.n8n.io/code/expressions/">expression</a>.',
			},

			// ── Publish Event ──
			{
				displayName: 'Event Type',
				name: 'eventType',
				type: 'string',
				default: '',
				required: true,
				displayOptions: { show: { operation: ['publishEvent'] } },
				placeholder: 'order.created',
				description: 'Type identifier for the event',
			},
			{
				displayName: 'Payload (JSON)',
				name: 'payload',
				type: 'json',
				default: '{}',
				displayOptions: { show: { operation: ['publishEvent'] } },
				description: 'Inline JSON payload for the event (ignored when a Payload Object ID is set)',
			},
			{
				displayName: 'Additional Fields',
				name: 'publishOptions',
				type: 'collection',
				placeholder: 'Add Field',
				default: {},
				displayOptions: { show: { operation: ['publishEvent'] } },
				options: [
					{
						displayName: 'Metadata (JSON)',
						name: 'metadata',
						type: 'json',
						default: '{}',
						description: 'Optional metadata object (e.g. correlation_id, reply_topic)',
					},
					{
						displayName: 'Payload Object ID',
						name: 'payloadObjectId',
						type: 'string',
						default: '',
						placeholder: 'pob_...',
						description: 'ID of a previously uploaded payload object (object-ref mode — overrides inline payload)',
					},
				],
			},

			// ── Read Events ──
			{
				displayName: 'Limit',
				name: 'limit',
				type: 'number',
				default: 50,
				typeOptions: { minValue: 1, maxValue: 100 },
				displayOptions: { show: { operation: ['readEvents'] } },
				description: 'Max number of events to return (1–100)',
			},
			{
				displayName: 'Order',
				name: 'order',
				type: 'options',
				options: [
					{ name: 'Oldest First (asc)', value: 'asc' },
					{ name: 'Newest First (desc)', value: 'desc' },
				],
				default: 'asc',
				displayOptions: { show: { operation: ['readEvents'] } },
				description: 'Sort order of returned events',
			},
			{
				displayName: 'After Cursor',
				name: 'after',
				type: 'string',
				default: '',
				placeholder: 'evt_...',
				displayOptions: { show: { operation: ['readEvents'] } },
				description: 'Return events after this event_id (cursor from a previous read)',
			},
			{
				displayName: 'Event Type Filter',
				name: 'eventTypeFilter',
				type: 'string',
				default: '',
				displayOptions: { show: { operation: ['readEvents'] } },
				description: 'Only return events of this type (optional)',
			},
			{
				displayName: 'Exclude Own Events',
				name: 'excludeSelf',
				type: 'boolean',
				default: false,
				displayOptions: { show: { operation: ['readEvents'] } },
				description: 'Whether to drop events this credential published itself (server-side echo filter)',
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

	async execute(this: IExecuteFunctions): Promise<INodeExecutionData[][]> {
		const items = this.getInputData();
		const returnData: INodeExecutionData[] = [];

		const credentials = await this.getCredentials('agenTruxApi');
		const creds = await resolveCredentials(this as unknown as HttpHelper, credentials);

		for (let i = 0; i < items.length; i++) {
			try {
				const operation = this.getNodeParameter('operation', i) as string;

				if (operation === 'listTopics') {
					const topics = await listTopics(this as unknown as HttpHelper, creds);
					for (const t of topics)
						returnData.push({ json: t as unknown as IDataObject, pairedItem: { item: i } });
					continue;
				}

				const topicId = this.getNodeParameter('topicId', i) as string;

				if (operation === 'publishEvent') {
					const eventType = this.getNodeParameter('eventType', i) as string;
					const payloadRaw = this.getNodeParameter('payload', i, {});
					const payload =
						typeof payloadRaw === 'string' ? JSON.parse(payloadRaw || '{}') : (payloadRaw as Record<string, unknown>);
					const options = this.getNodeParameter('publishOptions', i, {}) as {
						metadata?: unknown;
						payloadObjectId?: string;
					};
					const metadata =
						typeof options.metadata === 'string'
							? JSON.parse(options.metadata || '{}')
							: ((options.metadata as Record<string, unknown>) ?? {});

					const result = await publishEvent(this as unknown as HttpHelper, creds, topicId, {
						eventType,
						payload,
						metadata,
						payloadObjectId: options.payloadObjectId || undefined,
					});
					returnData.push({ json: result, pairedItem: { item: i } });
				} else if (operation === 'readEvents') {
					const limit = this.getNodeParameter('limit', i) as number;
					const order = this.getNodeParameter('order', i) as 'asc' | 'desc';
					const after = this.getNodeParameter('after', i, '') as string;
					const eventTypeFilter = this.getNodeParameter('eventTypeFilter', i, '') as string;
					const excludeSelf = this.getNodeParameter('excludeSelf', i, false) as boolean;

					const res = await readEvents(this as unknown as HttpHelper, creds, topicId, {
						after: after || undefined,
						limit,
						order,
						eventType: eventTypeFilter || undefined,
						excludeSelf,
					});
					for (const evt of res.events) returnData.push({ json: evt, pairedItem: { item: i } });
					if (res.events.length === 0) {
						returnData.push({
							json: { _empty: true, next: res.next, ttl_expired: !!res.ttlExpired },
							pairedItem: { item: i },
						});
					}
				}
			} catch (error: any) {
				if (this.continueOnFail()) {
					returnData.push({ json: { error: error.message }, pairedItem: { item: i } });
					continue;
				}
				throw error;
			}
		}

		return [returnData];
	}
}
