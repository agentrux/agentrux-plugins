import {
	ICredentialTestRequest,
	ICredentialType,
	INodeProperties,
} from 'n8n-workflow';

export class AgenTruxApi implements ICredentialType {
	name = 'agenTruxApi';
	displayName = 'AgenTrux API';
	documentationUrl = 'https://github.com/your-org/AgenTrux';

	properties: INodeProperties[] = [
		{
			displayName: 'Base URL',
			name: 'baseUrl',
			type: 'string',
			default: 'http://localhost:8000',
			placeholder: 'https://your-agentrux-server.example.com',
			description: 'Base URL of the AgenTrux API server (no trailing slash)',
			required: true,
		},
		{
			displayName: 'Auth Mode',
			name: 'authMode',
			type: 'options',
			options: [
				{
					name: 'Activation Token (Initial Setup)',
					value: 'activationToken',
					description: 'Use an activation token for first-time setup — credentials will be returned on first use',
				},
				{
					name: 'Script Credentials',
					value: 'scriptCredentials',
					description: 'Use script_id + secret obtained from activation',
				},
			],
			default: 'activationToken',
		},

		// ── Activation Token mode ──
		{
			displayName: 'Activation Token',
			name: 'activationToken',
			type: 'string',
			typeOptions: { password: true },
			default: '',
			required: true,
			displayOptions: { show: { authMode: ['activationToken'] } },
			placeholder: 'atk_...',
			description: 'One-time activation token issued from the AgenTrux console',
		},

		// ── Script Credentials mode ──
		{
			displayName: 'Script ID',
			name: 'scriptId',
			type: 'string',
			default: '',
			required: true,
			displayOptions: { show: { authMode: ['scriptCredentials'] } },
			placeholder: 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx',
			description: 'UUID of the script (from activation response)',
		},
		{
			displayName: 'Secret',
			name: 'secret',
			type: 'string',
			typeOptions: { password: true },
			default: '',
			required: true,
			displayOptions: { show: { authMode: ['scriptCredentials'] } },
			description: 'Script secret (from activation response)',
		},

		// ── Optional: Grant Token (auto-redeemed on first use) ──
		{
			displayName: 'Grant Token (Optional)',
			name: 'grantToken',
			type: 'string',
			typeOptions: { password: true },
			default: '',
			displayOptions: { show: { authMode: ['scriptCredentials'] } },
			placeholder: 'gtk_...',
			description: 'Optional grant token — automatically redeemed on first use for cross-account access',
		},

		// ── Optional: Webhook Secret ──
		{
			displayName: 'Webhook Secret',
			name: 'webhookSecret',
			type: 'string',
			typeOptions: { password: true },
			default: '',
			description: 'HMAC-SHA256 secret for verifying incoming webhook payloads (Trigger node only)',
		},
	];

	// Connectivity test — uses the unauthenticated /a2a health endpoint
	test: ICredentialTestRequest = {
		request: {
			baseURL: '={{$credentials.baseUrl}}',
			url: '/a2a',
			method: 'GET',
		},
	};
}
