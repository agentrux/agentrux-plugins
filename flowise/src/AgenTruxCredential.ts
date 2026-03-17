import { INodeParams, INodeCredential } from 'flowise-components';

class AgenTruxCredential implements INodeCredential {
    label: string;
    name: string;
    version: number;
    description: string;
    inputs: INodeParams[];

    constructor() {
        this.label = 'AgenTrux API';
        this.name = 'authPubSubApi';
        this.version = 1.0;
        this.description = 'Credentials for AgenTrux A2A authentication';
        this.inputs = [
            {
                label: 'Base URL',
                name: 'baseUrl',
                type: 'string',
                default: 'http://localhost:8000',
                placeholder: 'https://your-agentrux-server.example.com',
                description: 'Base URL of the AgenTrux API server (no trailing slash)',
            },
            {
                label: 'Script ID',
                name: 'scriptId',
                type: 'string',
                default: '',
                description: 'UUID of the script to authenticate as',
            },
            {
                label: 'Secret',
                name: 'secret',
                type: 'password',
                default: '',
                description: 'Secret for the script (from activation or rotation)',
            },
            {
                label: 'Grant Token',
                name: 'grantToken',
                type: 'password',
                default: '',
                optional: true,
                description: 'Optional grant token to redeem cross-account access before first use',
            },
        ];
    }
}

module.exports = { credClass: AgenTruxCredential };
