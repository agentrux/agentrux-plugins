import {
	ICredentialTestRequest,
	ICredentialType,
	INodeProperties,
} from 'n8n-workflow';

export class AgenTruxApi implements ICredentialType {
	name = 'agenTruxApi';
	displayName = 'AgenTrux API';
	documentationUrl = 'https://github.com/agentrux/agentrux';

	properties: INodeProperties[] = [
		{
			displayName: 'Base URL',
			name: 'baseUrl',
			type: 'string',
			default: 'http://localhost:8000',
			placeholder: 'https://api.agentrux.com',
			description: 'Base URL of the AgenTrux API server (no trailing slash)',
			required: true,
		},
		{
			displayName: 'Auth Mode',
			name: 'authMode',
			type: 'options',
			options: [
				{
					name: 'Activation Code (Initial Setup)',
					value: 'activationCode',
					description: 'Use an activation code for first-time setup — credentials will be returned on first use',
				},
				{
					name: 'Script Credentials',
					value: 'scriptCredentials',
					description: 'Use script_id + client_secret obtained from activation',
				},
			],
			default: 'activationCode',
		},

		// ── Activation Code mode ──
		{
			displayName: 'Activation Code',
			name: 'activationCode',
			type: 'string',
			typeOptions: { password: true },
			default: '',
			required: true,
			displayOptions: { show: { authMode: ['activationCode'] } },
			placeholder: 'ac_...',
			description: 'One-time activation code issued from the AgenTrux console',
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
			name: 'clientSecret',
			type: 'string',
			typeOptions: { password: true },
			default: '',
			required: true,
			displayOptions: { show: { authMode: ['scriptCredentials'] } },
			description: 'Client Secret (from activation response)',
		},

		// ── Optional: Invite Code (auto-redeemed on first use) ──
		{
			displayName: 'Invite Code (Optional)',
			name: 'inviteCode',
			type: 'string',
			typeOptions: { password: true },
			default: '',
			displayOptions: { show: { authMode: ['scriptCredentials'] } },
			placeholder: 'inv_...',
			description: 'Optional invite code — automatically redeemed on first use for cross-account access',
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
