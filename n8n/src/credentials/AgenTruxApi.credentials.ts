import {
	ICredentialTestRequest,
	ICredentialType,
	INodeProperties,
} from 'n8n-workflow';

/**
 * AgenTrux API credential — Activation Code connect.
 *
 * The user pastes a one-time Activation Code (`act_...`) issued from the
 * AgenTrux Console. The nodes redeem it once into a Script credential
 * (client_id / client_secret) and cache the result on disk keyed by
 * sha256(code), so re-running the workflow never re-redeems the consumed
 * code. The single-use code itself is the only secret stored in n8n.
 *
 * The "Test" button hits the unauthenticated /a2a discovery endpoint to
 * verify the Base URL is reachable WITHOUT consuming the Activation Code —
 * redemption happens lazily on first node execution instead.
 */
export class AgenTruxApi implements ICredentialType {
	name = 'agenTruxApi';
	displayName = 'AgenTrux API';
	documentationUrl = 'https://github.com/agentrux/agentrux-plugins';

	properties: INodeProperties[] = [
		{
			displayName: 'Base URL',
			name: 'baseUrl',
			type: 'string',
			default: 'https://api.agentrux.com',
			placeholder: 'https://api.agentrux.com',
			description: 'Base URL of the AgenTrux API server (no trailing slash)',
			required: true,
		},
		{
			displayName: 'Activation Code',
			name: 'activationCode',
			type: 'string',
			typeOptions: { password: true },
			default: '',
			required: true,
			placeholder: 'act_...',
			description:
				'One-time Activation Code issued from the AgenTrux Console (Script → Issue Activation Code). Redeemed once and cached; safe to leave as-is across runs.',
		},
	];

	// Connectivity test — uses the unauthenticated /a2a discovery endpoint so
	// pressing "Test" does not consume the single-use Activation Code.
	test: ICredentialTestRequest = {
		request: {
			baseURL: '={{$credentials.baseUrl}}',
			url: '/a2a',
			method: 'GET',
		},
	};
}
