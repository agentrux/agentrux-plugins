import {
	IExecuteFunctions,
	INodeExecutionData,
	INodeType,
	INodeTypeDescription,
	NodeOperationError,
} from 'n8n-workflow';

import {
	resolveCredentials,
	authenticatedRequest,
	invalidateTokenCache,
	rawHttp,
	type ResolvedCredentials,
	type ActivationResult,
} from '../../transport/apiRequest';

export class AgenTrux implements INodeType {
	description: INodeTypeDescription = {
		displayName: 'AgenTrux',
		name: 'agenTrux',
		icon: 'fa:satellite-dish',
		group: ['transform'],
		version: 1,
		subtitle: '={{$parameter["resource"] + " / " + $parameter["operation"]}}',
		description: 'Interact with AgenTrux — publish & read events, manage grants',
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
			// ── Resource ──
			{
				displayName: 'Resource',
				name: 'resource',
				type: 'options',
				noDataExpression: true,
				options: [
					{ name: 'Topic', value: 'topic' },
					{ name: 'Auth', value: 'auth' },
				],
				default: 'topic',
			},
			// ── Auth operations ──
			{
				displayName: 'Operation',
				name: 'operation',
				type: 'options',
				noDataExpression: true,
				displayOptions: { show: { resource: ['auth'] } },
				options: [
					{
						name: 'Redeem Grant Token',
						value: 'redeemGrant',
						description: 'Redeem a grant token for cross-account topic access',
					},
				],
				default: 'redeemGrant',
			},
			// ── Topic operations ──
			{
				displayName: 'Operation',
				name: 'operation',
				type: 'options',
				noDataExpression: true,
				displayOptions: { show: { resource: ['topic'] } },
				options: [
					{ name: 'Publish Event', value: 'publishEvent', description: 'Publish an event to a topic' },
					{ name: 'Read Events', value: 'readEvents', description: 'List events with cursor pagination' },
					{ name: 'Get Event', value: 'getEvent', description: 'Get a single event by ID' },
					{ name: 'Upload Payload', value: 'uploadPayload', description: 'Create payload metadata and upload binary data' },
					{ name: 'Download Payload', value: 'downloadPayload', description: 'Get payload metadata and download binary data' },
				],
				default: 'publishEvent',
			},

			// ================================================================
			// Auth — Redeem Grant Token
			// ================================================================
			{
				displayName: 'Grant Token',
				name: 'grantToken',
				type: 'string',
				typeOptions: { password: true },
				default: '',
				required: true,
				displayOptions: { show: { resource: ['auth'], operation: ['redeemGrant'] } },
				placeholder: 'gtk_...',
				description: 'Grant token shared by the topic owner',
			},

			// ================================================================
			// Topic — common: Topic ID
			// ================================================================
			{
				displayName: 'Topic ID',
				name: 'topicId',
				type: 'string',
				default: '',
				required: true,
				displayOptions: { show: { resource: ['topic'] } },
				description: 'UUID of the target topic',
			},

			// ================================================================
			// Topic — Publish Event
			// ================================================================
			{
				displayName: 'Event Type',
				name: 'eventType',
				type: 'string',
				default: '',
				required: true,
				displayOptions: { show: { resource: ['topic'], operation: ['publishEvent'] } },
				placeholder: 'order.created',
				description: 'Type identifier for the event',
			},
			{
				displayName: 'Payload (JSON)',
				name: 'payload',
				type: 'json',
				default: '{}',
				displayOptions: { show: { resource: ['topic'], operation: ['publishEvent'] } },
				description: 'JSON payload for the event (inline mode)',
			},
			{
				displayName: 'Additional Fields',
				name: 'publishOptions',
				type: 'collection',
				placeholder: 'Add Field',
				default: {},
				displayOptions: { show: { resource: ['topic'], operation: ['publishEvent'] } },
				options: [
					{
						displayName: 'Correlation ID',
						name: 'correlationId',
						type: 'string',
						default: '',
						description: 'Optional correlation ID for request tracing',
					},
					{
						displayName: 'Reply Topic',
						name: 'replyTopic',
						type: 'string',
						default: '',
						description: 'Optional reply topic for request-reply patterns',
					},
					{
						displayName: 'Payload Ref (Object ID)',
						name: 'payloadRef',
						type: 'string',
						default: '',
						description: 'UUID of a previously uploaded payload object (ref mode — overrides inline payload)',
					},
				],
			},

			// ================================================================
			// Topic — Read Events
			// ================================================================
			{
				displayName: 'Limit',
				name: 'limit',
				type: 'number',
				default: 50,
				typeOptions: { minValue: 1, maxValue: 200 },
				displayOptions: { show: { resource: ['topic'], operation: ['readEvents'] } },
				description: 'Maximum number of events to return (max 200)',
			},
			{
				displayName: 'Cursor',
				name: 'cursor',
				type: 'string',
				default: '',
				displayOptions: { show: { resource: ['topic'], operation: ['readEvents'] } },
				description: 'Pagination cursor from a previous response (next_cursor)',
			},
			{
				displayName: 'Event Type Filter',
				name: 'eventTypeFilter',
				type: 'string',
				default: '',
				displayOptions: { show: { resource: ['topic'], operation: ['readEvents'] } },
				description: 'Filter events by type (optional)',
			},

			// ================================================================
			// Topic — Get Event
			// ================================================================
			{
				displayName: 'Event ID',
				name: 'eventId',
				type: 'string',
				default: '',
				required: true,
				displayOptions: { show: { resource: ['topic'], operation: ['getEvent'] } },
				description: 'UUID of the event to retrieve',
			},

			// ================================================================
			// Topic — Upload Payload
			// ================================================================
			{
				displayName: 'Content Type',
				name: 'contentType',
				type: 'string',
				default: 'application/octet-stream',
				displayOptions: { show: { resource: ['topic'], operation: ['uploadPayload'] } },
				description: 'MIME type of the payload',
			},
			{
				displayName: 'Binary Property',
				name: 'binaryProperty',
				type: 'string',
				default: 'data',
				displayOptions: { show: { resource: ['topic'], operation: ['uploadPayload'] } },
				description: 'Name of the binary property containing the data to upload',
			},

			// ================================================================
			// Topic — Download Payload
			// ================================================================
			{
				displayName: 'Object ID',
				name: 'objectId',
				type: 'string',
				default: '',
				required: true,
				displayOptions: { show: { resource: ['topic'], operation: ['downloadPayload'] } },
				description: 'UUID of the payload object to download',
			},
			{
				displayName: 'Binary Property',
				name: 'binaryPropertyDownload',
				type: 'string',
				default: 'data',
				displayOptions: { show: { resource: ['topic'], operation: ['downloadPayload'] } },
				description: 'Name of the binary property to store the downloaded data',
			},
		],
	};

	async execute(this: IExecuteFunctions): Promise<INodeExecutionData[][]> {
		const items = this.getInputData();
		const returnData: INodeExecutionData[] = [];

		const credentials = await this.getCredentials('agenTruxApi');
		const { resolved: creds, activationResult } = await resolveCredentials(this, credentials);

		for (let i = 0; i < items.length; i++) {
			try {
				const resource = this.getNodeParameter('resource', i) as string;
				const operation = this.getNodeParameter('operation', i) as string;

				// ── Auth operations ──
				if (resource === 'auth') {
					if (operation === 'redeemGrant') {
						const grantToken = this.getNodeParameter('grantToken', i) as string;
						const resp = await rawHttp(this, 'POST', `${creds.baseUrl}/auth/redeem-grant`, {
							token: grantToken,
							script_id: creds.scriptId,
							secret: creds.secret,
						});
						if (resp.status >= 400) {
							throw new NodeOperationError(
								this.getNode(),
								`Grant redemption failed (${resp.status}): ${JSON.stringify(resp.body)}`,
							);
						}
						// Invalidate JWT cache so next request picks up new scope
						invalidateTokenCache(creds);
						returnData.push({ json: resp.body });
					}
					continue;
				}

				// ── Topic operations ──
				const topicId = this.getNodeParameter('topicId', i) as string;

				if (operation === 'publishEvent') {
					const eventType = this.getNodeParameter('eventType', i) as string;
					const payloadRaw = this.getNodeParameter('payload', i);
					const payload = typeof payloadRaw === 'string' ? JSON.parse(payloadRaw) : payloadRaw;
					const options = this.getNodeParameter('publishOptions', i, {}) as {
						correlationId?: string;
						replyTopic?: string;
						payloadRef?: string;
					};

					const reqBody: Record<string, unknown> = { type: eventType };
					if (options.payloadRef) {
						reqBody.payload_ref = options.payloadRef;
					} else {
						reqBody.payload = payload;
					}
					if (options.correlationId) reqBody.correlation_id = options.correlationId;
					if (options.replyTopic) reqBody.reply_topic = options.replyTopic;

					const result = await authenticatedRequest(
						this, 'POST', creds, `/topics/${topicId}/events`, reqBody,
					);
					returnData.push({ json: result });

				} else if (operation === 'readEvents') {
					const limit = this.getNodeParameter('limit', i) as number;
					const cursor = this.getNodeParameter('cursor', i) as string;
					const eventTypeFilter = this.getNodeParameter('eventTypeFilter', i) as string;

					let path = `/topics/${topicId}/events?limit=${limit}`;
					if (cursor) path += `&cursor=${encodeURIComponent(cursor)}`;
					if (eventTypeFilter) path += `&type=${encodeURIComponent(eventTypeFilter)}`;

					const result = await authenticatedRequest(this, 'GET', creds, path);
					returnData.push({ json: result });

				} else if (operation === 'getEvent') {
					const eventId = this.getNodeParameter('eventId', i) as string;
					const result = await authenticatedRequest(
						this, 'GET', creds, `/topics/${topicId}/events/${eventId}`,
					);
					returnData.push({ json: result });

				} else if (operation === 'uploadPayload') {
					const contentType = this.getNodeParameter('contentType', i) as string;
					const binaryProperty = this.getNodeParameter('binaryProperty', i) as string;
					const binaryData = this.helpers.assertBinaryData(i, binaryProperty);
					const buffer = await this.helpers.getBinaryDataBuffer(i, binaryProperty);

					const createResp = await authenticatedRequest(
						this, 'POST', creds, `/topics/${topicId}/payloads`,
						{
							content_type: binaryData.mimeType || contentType,
							size: buffer.length,
						},
					);

					await this.helpers.httpRequest({
						method: 'PUT',
						url: createResp.upload_url,
						body: buffer,
						headers: { 'Content-Type': binaryData.mimeType || contentType },
					});

					returnData.push({
						json: {
							object_id: createResp.object_id,
							expiration: createResp.expiration,
							status: 'uploaded',
						},
					});

				} else if (operation === 'downloadPayload') {
					const objectId = this.getNodeParameter('objectId', i) as string;
					const binaryProperty = this.getNodeParameter('binaryPropertyDownload', i) as string;

					const resp = await authenticatedRequest(
						this, 'GET', creds, `/topics/${topicId}/payloads/${objectId}`,
					);

					const binaryBuffer = await this.helpers.httpRequest({
						method: 'GET',
						url: resp.download_url,
						encoding: 'arraybuffer',
					});

					const binaryObj = await this.helpers.prepareBinaryData(
						Buffer.from(binaryBuffer as ArrayBuffer),
						undefined,
						resp.content_type,
					);

					returnData.push({
						json: {
							object_id: resp.object_id,
							content_type: resp.content_type,
							size: resp.size,
						},
						binary: { [binaryProperty]: binaryObj },
					});
				}
			} catch (error: any) {
				if (this.continueOnFail()) {
					returnData.push({ json: { error: error.message } });
					continue;
				}
				throw error;
			}
		}

		// ── Prepend activation info if this was a first-time setup ──
		if (activationResult) {
			const infoItem: INodeExecutionData = {
				json: {
					_setup: 'AUTO_ACTIVATED',
					_message:
						'Script activated successfully! Switch your credential to "Script Credentials" mode and enter the values below.',
					script_id: activationResult.scriptId,
					secret: activationResult.secret,
					grants: activationResult.grants,
				},
			};
			returnData.unshift(infoItem);
		}

		return [returnData];
	}
}
